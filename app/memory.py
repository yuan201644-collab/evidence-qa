"""Bounded per-session context for clarification and short follow-up questions."""

from __future__ import annotations

import re
import threading
import time
from copy import deepcopy
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


_FOLLOWUP_MARKERS = (
    "那去年", "那今年", "改成", "改为", "换成", "换为", "只看", "也看", "再看",
    "按去年", "按今年", "它的", "这个", "上述", "上一问", "这个范围",
    # 「其中/这些/那些/这部分」是在**上一轮结果集内**再筛，没有上文就无从解释。
    # 它们与业务对象词同现也不会改变这一点（「其中招标公告有几条」照样要上文）。
    "其中", "这些", "那些", "这部分", "那部分",
    # 「把 X 也带上」「两个维度一起看」—— 2026-10-18 gmt-01 第 5 轮实测：
    # 「把地区也带上，两个维度一起看」既没有新的统计对象，也没有时间范围，
    # 但「也带上」「一起看」「两个维度」都在指上一轮已有的分组，脱离上文无法解释。
    "也带上", "一起看", "两个维度", "这个维度", "那个维度", "再加上", "再加上去",
)
#: 「改了筛选/范围但没重说对象」的追问形态（2026-10-18 政采多轮实测）：
#:   「从 10 月 15 日到 17 日一共多少条？」—— 有统计动词「多少条」、有一个新时间范围，
#:   却没有任何业务对象词，对象只能来自上一轮。
#:   「按天分开看，每天各多少条？」—— 改了分组维度，同样没有业务对象。
#: 这类句子不该靠往 `_FOLLOWUP_MARKERS` 里堆词来兜（无穷无尽），
#: 而是用「没有自带业务对象」这个结构性判据。
#: 业务对象词只收**各业务库里稳定存在的实体名**；不收「数据」「记录」这类泛指。
_ENTITY_WORDS = (
    "项目", "公告", "标包", "包", "中标", "成交", "采购人", "供应商", "代理机构",
    "机构", "合同", "预算", "品目", "商品", "地区", "省份", "城市", "级次",
    "方式", "客户", "发票", "员工", "销售额", "订单", "产品", "库存", "销售",
    "法规", "条例", "条款", "文档",
)
#: 度量词：说的是「算什么指标」，不是「算谁」。单独出现时仍然需要上文给对象
#: （「取金额最高的前 5 家」——金额是度量，谁的最高要看上文）。
_MEASURE_WORDS = ("金额", "数量", "条数", "个数", "件数", "总额", "总价")
#: 统计/分组/罗列动词：出现在句子里说明「要算个数或分组」，但没说算谁。
_AGGREGATE_WORDS = (
    "多少", "几", "各", "一共", "总共", "合计", "总计", "分组", "分开看", "分别",
    "平均", "占比", "排名", "列出", "统计", "汇总",
    "最高", "最低", "最多", "最少", "最大", "最小", "前 5", "前5", "前 10", "前10",
)
#: 「按 <维度> 分组/分组看/看」——维度词是分组依据，不算业务对象。
_GROUP_DIMENSION_RE = re.compile(
    r"按\s*[^，,。？?]{1,8}?\s*(?:分组|分开看|看|统计|汇总|排序|排名)"
)
_CANCEL_WORDS = {"取消", "取消问题", "不用了", "算了", "停止", "结束澄清"}
_NEW_QUESTION_PREFIXES = ("新问题", "另外", "换个问题", "重新问", "再问一个", "还有一个问题")


@dataclass
class SessionState:
    session_id: str
    pending_question: str = ""
    pending_slots: tuple[str, ...] = ()
    last_successful_question: str = ""
    last_successful_query: dict[str, Any] | None = None
    confirmed_context: dict[str, str] = field(default_factory=dict)
    #: 跨轮条件档案（F0）。**存的是上一轮仍生效的要求清单**，
    #: 让"这一轮用户少说了什么"能从句法层面判断，而不是靠拼字符串猜。
    #: 类型是 `app.condition_archive.ConditionArchive`，这里存 `Any`
    #: 是为了不让 `app.memory` 依赖编排层的模块（避免循环导入）。
    condition_archive: Any = None
    updated_at: float = field(default_factory=time.monotonic)


def _has_nominal_subject(text: str) -> bool:
    """句子是否还残留名词性成分（名词/专名/动名词）。

    用 jieba 词性标注判断，不用词表。判「追问残句」时，只要还剩一个像样的
    名词，就认为这句话自己说了对象，不该接上文。
    jieba 不可用时保守返回 True（当成完整问题），理由同上：漏判追问比误接安全。
    """
    try:
        import jieba.posseg as pseg
    except Exception:
        return True
    for _word, flag in pseg.cut(text):
        # n 名词系列、vn 动名词、eng 英文专名、m+q 量词短语都不算对象；
        # 这里只认「实义名词」，且排除纯度量/维度词本身。
        if flag in ("n", "nr", "ns", "nt", "nz", "vn") and len(_word) >= 2:
            return True
    return False


class SessionMemory:
    """Single-process bounded state with serialized turns and no cross-session sharing."""

    def __init__(self, *, max_sessions: int = 256, ttl_seconds: int = 1800,
                 max_context_chars: int = 4000) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_context_chars = max(256, int(max_context_chars))
        self._states: OrderedDict[str, SessionState] = OrderedDict()
        self._lock = threading.RLock()
        self._request_locks: dict[str, tuple[Any, int]] = {}

    @contextmanager
    def serialize_request(self, session_id: str) -> Iterator[None]:
        """Serialize whole answer turns for one session without blocking other sessions."""
        key = session_id or "default"
        with self._lock:
            entry = self._request_locks.get(key)
            if entry is None:
                lock = threading.RLock()
                users = 1
            else:
                lock, users = entry
                users += 1
            self._request_locks[key] = (lock, users)

        acquired = False
        try:
            lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            with self._lock:
                current = self._request_locks.get(key)
                if current is not None and current[0] is lock:
                    if current[1] <= 1:
                        self._request_locks.pop(key, None)
                    else:
                        self._request_locks[key] = (lock, current[1] - 1)

    def prepare_question(self, session_id: str, question: str) -> tuple[str, str]:
        """Resolve a pending clarification or attach an obvious short follow-up."""
        prepared, action, _previous = self.prepare_turn(session_id, question)
        return prepared, action

    def prepare_turn(self, session_id: str, question: str) -> tuple[str, str, str | None]:
        """同 `prepare_question`，但额外返回**被接续的上一轮原文**。

        第三个返回值只用于展示（界面上告诉用户「这一轮是接着上一轮的」），
        不参与任何判定 —— 判定仍看 action 字符串与 normalizer 里的接缝。

        为什么要多这一层而不是直接扩 `prepare_question` 的返回值：
        后者已被多处调用与测试按二元组解包（`tests/test_mainline_clarifier_memory.py`
        就有 5 处），改成三元组会让这些调用点全部静默错位。留旧签名做薄封装，
        新调用方用新方法，两边都不用改。
        """
        clean = (question or "").strip()
        with self._lock:
            state = self._get_locked(session_id)
            if state is None:
                return clean, "", None

            if state.pending_question:
                if clean.casefold() in _CANCEL_WORDS:
                    state.pending_question = ""
                    state.pending_slots = ()
                    state.updated_at = time.monotonic()
                    self._touch_locked(session_id, state)
                    return "", "pending_cancelled", None
                if any(clean.startswith(prefix) for prefix in _NEW_QUESTION_PREFIXES):
                    state.pending_question = ""
                    state.pending_slots = ()
                    state.updated_at = time.monotonic()
                    self._touch_locked(session_id, state)
                    return clean, "pending_replaced_by_new_question", None
                previous = state.pending_question
                combined = self._combine_pending(
                    previous, clean, state.pending_slots
                )
                state.pending_question = ""
                state.pending_slots = ()
                state.updated_at = time.monotonic()
                self._touch_locked(session_id, state)
                return combined, "pending_clarification_resumed", previous

            previous = state.last_successful_question
            if previous and self._is_followup(clean):
                state.updated_at = time.monotonic()
                self._touch_locked(session_id, state)
                return self._combine(previous, clean), "short_followup_attached", previous
            return clean, "", None

    def last_question(self, session_id: str) -> str | None:
        """上一轮成功时存进历史的原话链。

        与 `prepare_turn` 的第三个返回值不同：那个是「本轮拼接所用的上文」，
        已被 `_combine` 掺入了本轮内容；这个纯粹是**上一轮结束时的历史原文**，
        供调用方在保存本轮历史时续接，避免把改写产物再喂进归一化。
        """
        with self._lock:
            state = self._get_locked(session_id)
            return state.last_successful_question if state else None

    def begin_clarification(self, session_id: str, question: str,
                            missing_slots: tuple[str, ...]) -> None:
        with self._lock:
            state = self._get_locked(session_id) or SessionState(session_id=session_id)
            state.pending_question = (question or "")[-self.max_context_chars:]
            state.pending_slots = tuple(missing_slots)
            state.updated_at = time.monotonic()
            self._touch_locked(session_id, state)

    def save_success(self, session_id: str, question: str,
                     confirmed_context: dict[str, str] | None = None,
                     query_state: dict[str, Any] | None = None) -> None:
        with self._lock:
            state = self._get_locked(session_id) or SessionState(session_id=session_id)
            state.pending_question = ""
            state.pending_slots = ()
            state.last_successful_question = (question or "")[-self.max_context_chars:]
            state.last_successful_query = self._bounded_query_state(query_state)
            state.confirmed_context = dict(confirmed_context or {})
            state.updated_at = time.monotonic()
            self._touch_locked(session_id, state)

    def query_state(self, session_id: str) -> dict[str, Any] | None:
        """Return the last successful structured query facts for this session."""
        with self._lock:
            state = self._get_locked(session_id)
            return deepcopy(state.last_successful_query) if state and state.last_successful_query else None

    def condition_archive(self, session_id: str):
        """取本会话的条件档案（F0）。没有则返回 `None`。

        **不深拷**：`ConditionArchive` 是不可变 dataclass，合并只产出新实例，
        不会就地改动。深拷反而会在每轮多复制一遍整份清单。
        """
        with self._lock:
            state = self._get_locked(session_id)
            return state.condition_archive if state else None

    def save_condition_archive(self, session_id: str, archive) -> None:
        """写回本会话的条件档案。"""
        with self._lock:
            state = self._get_locked(session_id) or SessionState(session_id=session_id)
            state.condition_archive = archive
            state.updated_at = time.monotonic()
            self._touch_locked(session_id, state)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._states.pop(session_id, None)

    def pending(self, session_id: str) -> SessionState | None:
        with self._lock:
            return self._get_locked(session_id)

    @staticmethod
    def _bounded_query_state(query_state: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(query_state, dict):
            return None
        sql = query_state.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return None
        columns = query_state.get("columns")
        if not isinstance(columns, (list, tuple)):
            columns = []
        row_count = query_state.get("row_count")
        bounded = {
            "sql": sql.strip()[-8000:],
            "columns": [str(item)[:128] for item in columns[:64]],
            "row_count": int(row_count) if isinstance(row_count, int) and row_count >= 0 else None,
        }
        # 上一轮结果首行（有界）。「他/这家/那家」指的就是它。
        # 这一层会重建字典，所以**新增字段必须在这里显式搬运** ——
        # 2026-10-18 实测就是漏在这一步：`_query_state_from_runs` 已经写好了
        # `first_row`，被这里整个丢掉，下游拿到的永远是 None。
        first_row = query_state.get("first_row")
        if isinstance(first_row, dict) and first_row:
            bounded["first_row"] = {
                str(key)[:64]: str(value)[:120]
                for key, value in list(first_row.items())[:16]
            }
        return bounded

    @staticmethod
    def _combine(original: str, supplement: str) -> str:
        return (original.strip() + "\n用户补充或修改：" + supplement.strip())[:8000]

    @classmethod
    def _combine_pending(
        cls, original: str, supplement: str, pending_slots: tuple[str, ...]
    ) -> str:
        if pending_slots == ("comparison_period",):
            combined = (
                original.strip()
                + "\n用户明确补充的增长率比较周期（对比期/基期）："
                + supplement.strip()
            )
            return combined[:8000]
        return cls._combine(original, supplement)

    @staticmethod
    def _is_followup(question: str) -> bool:
        text = question.strip()
        if len(text) > 24:
            return False
        # 句首「那」是中文里最典型的接续句式（「那按员工分组呢」「那去年呢」）。
        if text.startswith("那"):
            return True
        # 人称代词单独判：多轮里「他/她」几乎总是指上一轮提到的人或物。
        # 不能只收「它的」——中文最常用的指代是「他总共消费了多少」这种。
        if re.match(r"^(他|她|它|他们|她们|它们)", text):
            return True
        if any(marker in text for marker in _FOLLOWUP_MARKERS):
            return True
        # 结构性判据：要算数/要分组，但整句**没有名词性主语** → 对象来自上文。
        # 只在短句上生效：长句往往自带完整业务描述，误判成追问会污染上下文。
        wants_aggregate = any(word in text for word in _AGGREGATE_WORDS)
        if not wants_aggregate:
            return False
        # 「按 X 分组/按 X 看」里的 X 是**分组维度**，不是业务对象：
        # 「按地区分组看看」「按供应商分组，取前 5 家」都是承接上文换维度。
        stripped = _GROUP_DIMENSION_RE.sub("", text)
        for word in _MEASURE_WORDS:
            stripped = stripped.replace(word, "")
        if any(word in stripped for word in _ENTITY_WORDS):
            return False
        # 词表永远漏（「采购单位」「中介」「活儿」都不在表里）。再加一道
        # **词性判据**：句子若还有名词性成分，说明它自己说了对象，不是残句。
        # 这一道是词表兜不住的场合的硬保险 —— 宁可漏判追问，也不要把
        # 单轮题错接上文（接错会直接改掉查询对象，比漏接严重得多）。
        return not _has_nominal_subject(stripped)

    def _get_locked(self, session_id: str) -> SessionState | None:
        now = time.monotonic()
        expired = [key for key, value in self._states.items()
                   if now - value.updated_at > self.ttl_seconds]
        for key in expired:
            self._states.pop(key, None)
        state = self._states.get(session_id)
        if state is not None:
            self._states.move_to_end(session_id)
        return state

    def _touch_locked(self, session_id: str, state: SessionState) -> None:
        self._states[session_id] = state
        self._states.move_to_end(session_id)
        while len(self._states) > self.max_sessions:
            self._states.popitem(last=False)


__all__ = ["SessionMemory", "SessionState"]
