"""RAG 侧的三项指标：检索命中率 / 答案忠实度 / 单题通过计数。

口径（对应初赛评分表）：
    检索命中率 —— 「RAG 检索命中率」，用于定位问题出在检索还是生成。
    答案忠实度 —— 性能指标 2「RAG 答案忠实度：生成答案与检索内容的相关性和事实准确性」，
                  10 分，≥70% 得 6 / ≥80% 得 8 / ≥90% 得 10。
    单题通过  —— `docs/评测口径_v0.1.md` 3.3 节（L196-200）：每题 1 分的**二元计数分**，
                  对应基础功能里的「RAG 计数 10 分」。判据是三件事同时成立：
                  `answer_points` 全部命中 + `forbidden_entities` 一条未出现 + 有非空引用。
                  它**不合成一个布尔**，而是下钻三个子条件单独报——
                  演示时要能回答「这题为什么不算对」。

为什么忠实度先用「要点覆盖 + 禁答实体」而不是直接上 LLM 裁判：
    LLM 裁判本身需要被验证（它也会有偏差），而 C 这条线要先给出一个确定性的、
    每次跑结果一样的下限口径，让 A/B 有可对比的锚点。LLM 裁判作为 `--judge llm`
    的可选增强，必须先在带人工标注的子集上校准通过才能作为对外口径。

⚠️ 引用字段名是「双轨」，两侧不可混用（2026-09-20 修正）：
    预测侧（`app/contracts.py` 的 `DocRef`）：`doc_id` / `section_path` / `snippet` / `kind`
    用例侧（`eval/cases/rag.jsonl` 的 `gold_refs`）：`doc`（完整文件名）/ `anchor`（章节串）
    本模块用 `_ref_doc_id()` / `_ref_anchor()` 做归一，两侧都能读。
    历史缺陷：旧版两侧都按 `doc`/`anchor` 取值，接入真实契约 refs 后
    ① 传契约形状 → 全判未命中（命中率归零，假阴性）
    ② 传 doc-only → 退化为「文档级命中」（虚高，假阳性）
    且 oracle 直接拿 `gold_refs` 当检索结果，天然同形状 → 自证式全绿，掩盖此缺陷。

所有口径都是纯函数，可在没有模型、没有数据库的情况下单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

NORMALISE_STRIP = re.compile(r"[\s\u3000，。、；：！？,.;:!?（）()【】\[\]「」\"'`·\-—–_/]+")


def normalise(text: str) -> str:
    """去掉空白与标点后的小写形式。中英文混排时按字符级比较更稳。"""
    return NORMALISE_STRIP.sub("", (text or "").lower())


#: 金额单位换算表：单位 → 折合「元」的倍数。
#: 2026-10-18 实测：标准答案写 `4482717.50元`，系统答「448.271750万元」——
#: 两者是同一个数，却因为单位写法不同判成「未命中要点」，四道公告金额题全丢。
#: 判分器必须按数值比较，不能按字符串比较。
_MONEY_UNITS = {
    "亿元": 100_000_000,
    "亿": 100_000_000,
    "万元": 10_000,
    "万": 10_000,
    "元": 1,
}

#: 匹配「数值 + 可选单位」：1,234.5 万元 / ￥4482717.50 / 685万
_MONEY_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(亿元|亿|万元|万|元)?"
)


def _money_values(text: str) -> set[int]:
    """把文本里所有「数值+单位」抽成**以元为单位的整数分**。

    只对同时带金额单位的片段取值 —— 裸数字（数量、编号、年份）不算，
    否则「5 个工作日」里的 5 会被当成 5 元，制造出一堆假命中。

    精度：用整数「分」而不是浮点，避免 448.271750 万 × 10000 的浮点误差。
    """
    values: set[int] = set()
    for match in _MONEY_RE.finditer(text or ""):
        raw, unit = match.group(1), match.group(2)
        if not unit:
            continue  # 没有单位的不算金额
        try:
            amount = float(raw.replace(",", ""))
        except ValueError:
            continue
        scale = _MONEY_UNITS.get(unit)
        if scale is None:
            continue
        values.add(round(amount * scale * 100))
    return values


def _hit_any(answer_norm: str, keywords: list[str], answer_raw: str | None = None) -> bool:
    """任一关键词命中即算覆盖。

    除了原来的子串判定，另加一条**金额数值相等**的判定：
    `4482717.50元` 与 `448.271750万元` 视为同一次命中（见 `_money_values`）。

    ⚠ 金额必须从**原文**（`answer_raw`）取，不能用 `answer_norm`：
    `normalise()` 会删掉空白与标点，**小数点也一起没了** ——
    「448.271750万元」变成「448271750万元」，再乘 10000 就大了六个数量级，
    与「4482717.50元」永远对不上（2026-10-18 实测踩过）。
    归一化只适用于子串比对，数值比对必须在原文上做。
    """
    answer_amounts = _money_values(answer_raw if answer_raw is not None else answer_norm)
    for keyword in keywords:
        if not keyword:
            continue
        needle = normalise(keyword)
        if needle and needle in answer_norm:
            return True
        # 关键词本身带金额 → 按数值比一次
        keyword_amounts = _money_values(keyword)
        if keyword_amounts and keyword_amounts & answer_amounts:
            return True
    return False


@dataclass
class PointResult:
    point: str
    hit: bool


@dataclass
class FaithfulnessResult:
    points: list[PointResult] = field(default_factory=list)
    hallucinated: list[str] = field(default_factory=list)
    coverage: float = 0.0
    clean: bool = True

    @property
    def all_hit(self) -> bool:
        """要点是否**全部**命中。

        区别于 `coverage`（比例分）：评测口径 3.3 的单题判据用的是「全中」，
        不是「覆盖率高」——漏一个要点就不算这题通过。
        """
        return bool(self.points) and all(p.hit for p in self.points)

    @property
    def forbidden_absent(self) -> bool:
        """禁答实体一条都没出现。与 `clean` 同义；单独命名以对齐口径用词。"""
        return not self.hallucinated

    def score(self, coverage_weight: float = 1.0, hallucination_penalty: float = 0.25) -> float:
        """忠实度得分。

        幻觉按次数线性扣分并夹到 0——因为「编了一条不存在的事实」比「漏答一条要点」
        更严重：前者是知识问答的致命伤，后者只是不完整。
        """
        if not self.points:
            return 0.0
        cov = sum(1 for p in self.points if p.hit) / len(self.points)
        penalty = hallucination_penalty * len(self.hallucinated)
        return max(0.0, min(1.0, coverage_weight * cov - penalty))

    def to_dict(self) -> dict:
        return {
            "coverage": round(self.coverage, 4),
            "score": round(self.score(), 4),
            "points_total": len(self.points),
            "points_hit": sum(1 for p in self.points if p.hit),
            "all_hit": self.all_hit,
            "forbidden_absent": self.forbidden_absent,
            "missed_points": [p.point for p in self.points if not p.hit],
            "hallucinated_entities": self.hallucinated,
        }


def _appears_as_value(entity: str, answer_norm: str) -> bool:
    """禁答实体是否**作为一个完整的值**出现在答案里（而不是某个更长值的一部分）。

    **为什么不能直接 `entity in answer`**（2026-10-18 实测）：

        禁答实体「5 个工作日」，答案「……应当在答复期满后 15 个工作日内提起投诉」

    `in` 判定为命中，于是把一条**完全正确**的回答（15 个工作日正是《政府采购法》
    第五十二条的法定时限）记成幻觉，单题通过由 True 变 False。
    这是子串匹配的通病：数值前面多一位数字，就被当成了另一个值。

    判据：每次出现都必须满足**数字边界**——命中片段的左邻或右邻不能同时是数字。
    例：
        「5 个工作日」在「15 个工作日」里 → 左邻 '1' 与首字 '5' 都是数字 → 不算出现
        「5 个工作日」在「共 5 个工作日内」 → 左邻 '共'/空格 → 算出现
        「3 天」在「13 天」里 → 不算；在「约 3 天」里 → 算

    注意只对**以数字开头/结尾**的实体启用严格边界；纯文字实体
    （如「虚构的某公司」）保持原样的子串判定。
    """
    # 判据：命中片段的**首尾如果本身是数字**，那它旁边不能还是数字
    # ——否则说明落在同一个数里面。若首字不是数字，左边怎么邻都无所谓
    #（「35 个工作日」里匹配「个工作日」是合法出现）。
    needle = normalise(entity)
    if not needle:
        return False
    head_is_digit = needle[0].isdigit()
    tail_is_digit = needle[-1].isdigit()
    start = answer_norm.find(needle)
    while start >= 0:
        end = start + len(needle)
        left_ok = not (head_is_digit and start > 0 and answer_norm[start - 1].isdigit())
        right_ok = not (tail_is_digit and end < len(answer_norm) and answer_norm[end].isdigit())
        if left_ok and right_ok:
            return True
        start = answer_norm.find(needle, start + 1)
    return False


def evaluate_faithfulness(case: dict, answer: str) -> FaithfulnessResult:
    """按用例声明的要点与禁答实体评估一条回答。

    用例字段（见 eval/cases/rag.jsonl 与 eval/README.md）：
        answer_points: [{"point": "…", "match_any": ["关键词1", "关键词2"]}]
            —— 一条要点命中任一关键词即算覆盖。用关键词而不是语义相似度，
               是为了让判定可复现；语义判定留给后续 LLM 裁判。
        forbidden_entities: ["不该出现在答案里的实体"]
            —— 出现即计一次幻觉。注意：只有当该实体确实不在任何标准答案要点里时，
               才应写进这个字段，否则会把正确回答判成幻觉。
    """
    answer_norm = normalise(answer)
    points = [
        PointResult(
            p.get("point", ""),
            _hit_any(answer_norm, list(p.get("match_any") or []), answer_raw=answer),
        )
        for p in (case.get("answer_points") or [])
    ]
    hallucinated = [
        e for e in (case.get("forbidden_entities") or []) if _appears_as_value(e, answer_norm)
    ]

    res = FaithfulnessResult(points=points, hallucinated=hallucinated)
    res.coverage = (sum(1 for p in points if p.hit) / len(points)) if points else 0.0
    res.clean = not hallucinated
    return res


# --------------------------------------------------------------------------------------
# 引用归一（兼容契约形状与用例形状）
# --------------------------------------------------------------------------------------
#: 契约里文档引用的类型标记值（`app/contracts.py` 的 `DocRef.kind`）
DOC_KIND = "doc"

#: 文档编号前缀。语料里同一份文档有两种写法，见 normalize_doc_id()。
_DOC_ID_RE = re.compile(r"^(KB-\d+)", re.I)


def normalize_doc_id(value: str | None) -> str:
    """把文档标识归一到同一套写法（`KB-001` 形式）。

    语料里**同一份文档有两种写法**：
      · `data/corpus/manifest.json` 的 `id`：`KB-001`
      · `data/corpus/raw/` 的文件名：`KB-001_智能计算设备年度维保服务合同.pdf`

    契约 `DocRef.doc_id` 按设计是「文档编号」，但链路里两种写法都可能出现
    （B 的切片来自文件名，A 的引用来自登记表）。两侧都过一遍本函数，
    比较才不会因为写法不同而静默失配。

    不符合 `KB-XXX` 形式的值**原样返回** —— 这样命名不规范的输入会自然暴露成
    `unresolved_doc_ids`，而不是被悄悄改写成看似合法的值。
    """
    s = str(value or "").strip()
    m = _DOC_ID_RE.match(s)
    return m.group(1).upper() if m else s


def ref_doc_id(item: dict) -> str | None:
    """取一条引用的文档标识，兼容两种形状。

    预测侧（契约 `DocRef`）：`{"kind": "doc", "doc_id": "KB-001", ...}`
    用例侧（`gold_refs`）：  `{"doc": "KB-001_智能计算设备年度维保服务合同.pdf", "anchor": "第二章 质保条款"}`
    """
    v = item.get("doc_id") or item.get("doc")
    return str(v) if v else None


def ref_anchor(item: dict) -> str | None:
    """取一条引用的块锚点（章节串）。契约叫 `section_path`，用例叫 `anchor`。"""
    v = item.get("section_path") or item.get("anchor")
    return str(v) if v else None


def ref_snippet(item: dict) -> str:
    """取引用原文片段。字段缺失或非字符串时返回空串（不抛异常）。"""
    v = item.get("snippet")
    return v if isinstance(v, str) else ""


def only_doc_refs(items: list[dict]) -> tuple[list[dict], int]:
    """过滤出文档类引用，返回 `(文档引用列表, 被剔除的条数)`。

    契约 `AskResponse.refs` 是 `SqlRef | DocRef` **混装**（靠 `kind` 判别）。
    检索命中的判定只该看文档引用——不过滤的话 `SqlRef` 会混进检索列表。
    `gold_refs` 形状没有 `kind` 字段，按文档引用处理（旧用例不必改）。
    """
    kept, dropped = [], 0
    for it in items:
        kind = it.get("kind")
        if kind is None or kind == DOC_KIND:
            kept.append(it)
        else:
            dropped += 1
    return kept, dropped


# --------------------------------------------------------------------------------------
# 检索命中
# --------------------------------------------------------------------------------------
@dataclass
class RetrievalResult:
    hit: bool
    matched_rank: int | None = None
    reason: str = ""
    considered: int = 0
    """实际参与判定的文档引用条数（已剔除 SqlRef）。"""
    dropped_non_doc: int = 0
    """被剔除的非文档引用条数（契约里的 SqlRef）。"""
    exact: bool = False
    """命中是否为「文档+块锚点」**精确**命中。

    `False` 只表示退化成**文档级宽松**命中，**不表示未命中** ——
    `hit` 的口径不变（精确与文档级都算命中）。本字段专门用于把
    「严格命中数 / 宽松命中数」**并列**报出来：主分数仍取 `hit`，
    但只有精确命中才证明章节定位是对的（B 于 2026-10-05 提出）。"""

    def to_dict(self) -> dict:
        return {
            "hit": self.hit,
            "exact": self.exact,
            "matched_rank": self.matched_rank,
            "reason": self.reason,
            "considered": self.considered,
            "dropped_non_doc": self.dropped_non_doc,
        }


def evaluate_retrieval(case: dict, retrieved: list[dict]) -> RetrievalResult:
    """检索命中率：top-k 检索结果里是否至少命中一个标准证据块。

    判定用「文档 + 块锚点」而不是文本相似度：
    标准答案里写死 gold_refs，命中与否是集合交集，没有主观空间。
    块锚点用章节目录串（如 "3.2 质保条款"）而不是页码——
    页码会随解析器换版本而变，目录串是稳定的。

    ⚠️ 两侧字段名**不同**，本函数用 `ref_doc_id()` / `ref_anchor()` 归一：
      检索结果（预测侧，契约 `DocRef`）：`{"kind": "doc", "doc_id": ..., "section_path": ...}`
      标准答案（`gold_refs`）：         `{"doc": "…pdf", "anchor": "第二章 质保条款"}`

    只给文档不给锚点时退化为「文档级命中」，并在 reason 里标注，
    避免把宽松判定伪装成严格判定。

    ⚠️ 判定顺序：**先扫完所有结果的精确锚点，再退到文档级**（2026-10-07 修）。
    早期版本在循环里遇到「同文档但没有锚点」的结果就 `return` 文档级命中，
    后面的精确锚点根本没被检查 —— 布尔 `hit` 不受影响，但逐题 `reason`
    会把精确命中误标成宽松命中，报告的「严格命中数」因此偏低、低估检索质量。
    B 于 2026-10-05 的实测正是如此：`reason` 只标出 10 题精确，独立复核其实是 14 题。
    """
    if not retrieved:
        return RetrievalResult(False, None, "检索结果为空")

    docs, dropped = only_doc_refs(retrieved)
    if not docs:
        return RetrievalResult(
            False,
            None,
            f"检索结果里没有文档引用（已剔除 {dropped} 条非 doc 引用）",
            considered=0,
            dropped_non_doc=dropped,
        )

    gold_refs = list(case.get("gold_refs") or [])
    gold_docs = {normalize_doc_id(ref_doc_id(r)) for r in gold_refs if ref_doc_id(r)}
    gold_pairs = {
        (normalize_doc_id(ref_doc_id(r)), ref_anchor(r)) for r in gold_refs if ref_anchor(r)
    }

    # ── 第一遍：全量找「文档+块锚点」精确命中 ──
    # 必须**扫完**再退，不能遇到文档级候选就 return（见 docstring 的顺序说明）。
    # 布尔 `hit` 与单遍版本完全一致（精确/文档级都算命中），变的只是 reason 与
    # 「严格命中数」的统计口径 —— 所以主分数不变、oracle 自证不受影响。
    if gold_pairs:
        for rank, item in enumerate(docs, start=1):
            pair = (normalize_doc_id(ref_doc_id(item)), ref_anchor(item))
            if pair in gold_pairs:
                return RetrievalResult(
                    True, rank, "文档+块锚点精确命中", len(docs), dropped, exact=True
                )

    # ── 第二遍：没有精确命中，才退到文档级（宽容，但 reason 如实标注）──
    for rank, item in enumerate(docs, start=1):
        doc, anchor = normalize_doc_id(ref_doc_id(item)), ref_anchor(item)
        if doc in gold_docs and (not gold_pairs or not anchor):
            return RetrievalResult(
                True,
                rank,
                "仅文档级命中（标准答案未给块锚点或检索结果未给锚点）",
                len(docs),
                dropped,
            )
    return RetrievalResult(False, None, "top-k 内未命中任何标准证据块", len(docs), dropped)


# --------------------------------------------------------------------------------------
# 引用校验（口径 L200 的两件事，**分开报**，不合成一个布尔）
# --------------------------------------------------------------------------------------
@dataclass
class CitationResult:
    """一条回答的引用体检结果。

    口径原文（`docs/评测口径_v0.1.md` L200）：
    「**必须带出处引用**（引用片段非空、**且指向实际检索到的文档**）」

    后半句比前半句难，所以拆成两个**独立属性**——合成一个布尔的话，
    将来哪一项失分看不出来（A 于 2026-09-20 的评审意见）。
    """

    total: int = 0
    """回答里引用总条数（含 SqlRef）。"""
    doc_refs: int = 0
    """其中文档引用条数。"""
    nonempty_snippet_count: int = 0
    """snippet 非空的文档引用条数。"""
    resolved_doc_ids: list[str] = field(default_factory=list)
    """能解析到真实文档的文档标识（去重、排序）。"""
    unresolved_doc_ids: list[str] = field(default_factory=list)
    """解析不到真实文档的文档标识（去重、排序）——疑似编造。"""
    dropped_non_doc: int = 0

    @property
    def snippet_nonempty(self) -> bool:
        """子条件一：至少一条文档引用的 `snippet` 非空。"""
        return self.nonempty_snippet_count > 0

    @property
    def points_to_real_doc(self) -> bool:
        """子条件二：至少一条引用指向真实文档（不是编出来的文档标识）。"""
        return len(self.resolved_doc_ids) > 0

    @property
    def ok(self) -> bool:
        """两条子条件同时成立才叫「引用合格」。

        ⚠️ 报告里必须同时给出上面两个属性——`ok` 只用于汇总，
        不能拿它顶替「哪一条不满足」。
        """
        return self.snippet_nonempty and self.points_to_real_doc

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "doc_refs": self.doc_refs,
            "nonempty_snippet_count": self.nonempty_snippet_count,
            "snippet_nonempty": self.snippet_nonempty,
            "points_to_real_doc": self.points_to_real_doc,
            "resolved_doc_ids": self.resolved_doc_ids,
            "unresolved_doc_ids": self.unresolved_doc_ids,
            "dropped_non_doc": self.dropped_non_doc,
            "ok": self.ok,
        }


def evaluate_citations(
    refs: list[dict],
    *,
    known_doc_ids: Iterable[str] | None = None,
    retrieved_doc_ids: Iterable[str] | None = None,
) -> CitationResult:
    """校验一条回答的引用是否满足口径 L200 的两条子条件。

    「指向实际检索到的文档」的判定基准，按优先级：
      1. `retrieved_doc_ids` —— 本题**实际检索返回**的文档标识集合（最严格、最贴题意）
      2. `known_doc_ids`     —— 知识库已知文档集（如 `data/corpus/manifest.json` 的 `id`）
      3. 都没给 → 无法判定「指向真实文档」，此时不在 `resolved` 里给假象，
         也不假装通过（由调用方按 `citation_basis` 决定是否采信）。

    文档标识的归一同样走 `ref_doc_id()`，所以契约的 `KB-001` 与用例的
    `KB-001_智能计算设备年度维保服务合同.pdf` 需要同一套写法才可比；
    两套写法的互转由调用方在传基准集合时完成（见 `eval/README.md`）。
    """
    refs = list(refs or [])
    docs, dropped = only_doc_refs(refs)

    nonempty = sum(1 for it in docs if ref_snippet(it).strip())
    ids = [d for d in (ref_doc_id(it) for it in docs) if d]

    basis = retrieved_doc_ids if retrieved_doc_ids is not None else known_doc_ids
    if basis is None:
        resolved: list[str] = []
        unresolved = sorted({normalize_doc_id(d) for d in ids})
    else:
        basis_set = {normalize_doc_id(b) for b in basis}
        norm_ids = {normalize_doc_id(d) for d in ids}
        resolved = sorted(d for d in norm_ids if d in basis_set)
        unresolved = sorted(d for d in norm_ids if d not in basis_set)

    return CitationResult(
        total=len(refs),
        doc_refs=len(docs),
        nonempty_snippet_count=nonempty,
        resolved_doc_ids=resolved,
        unresolved_doc_ids=unresolved,
        dropped_non_doc=dropped,
    )


# --------------------------------------------------------------------------------------
# 单题通过（口径 3.3：每题 1 分的二元计数分，下钻三个子条件）
# --------------------------------------------------------------------------------------
@dataclass
class CaseVerdict:
    """一道 RAG 用例的通过判定。

    口径 3.3（L196-200）：「单题通过」= 三件事同时成立
      ① `answer_points` 全部命中
      ② `forbidden_entities` 一条都没出现
      ③ 有非空引用

    对应基础功能里的 RAG 计数 10 分（每题 1 分）。
    **不合成一个布尔**：`passed` 只是三者的与，明细照样留在 `details` 里——
    演示时要能回答「这题为什么不算对」，这是可解释性的落点。
    """

    case_id: str = ""
    gold_points_all_hit: bool = False
    forbidden_absent: bool = False
    citation_ok: bool = False
    citation_basis: str = "none"
    """引用「指向真实文档」这一条用的基准：`retrieved` / `known` / `none`。"""
    details: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.gold_points_all_hit and self.forbidden_absent and self.citation_ok

    def to_dict(self) -> dict:
        return {
            "id": self.case_id,
            "passed": self.passed,
            "gold_points_all_hit": self.gold_points_all_hit,
            "forbidden_absent": self.forbidden_absent,
            "citation_ok": self.citation_ok,
            "citation_basis": self.citation_basis,
            "details": self.details,
        }


def judge_case(
    case: dict,
    answer: str,
    refs: list[dict],
    *,
    known_doc_ids: Iterable[str] | None = None,
    retrieved_doc_ids: Iterable[str] | None = None,
) -> CaseVerdict:
    """按口径 3.3 判定一道 RAG 用例是否通过，并留下三个子条件的明细。

    引用那条口径的取值，按基准是否可得分两档（避免「没给基准就当通过」的假阳性，
    也避免「没给基准就全判死」的假阴性）：
      有基准 → 用 `CitationResult.ok`（snippet 非空 **且** 指向真实文档）
      无基准 → 退到 `snippet_nonempty`，并在 `citation_basis="none"` 里留痕
    """
    faith = evaluate_faithfulness(case, answer)
    cite = evaluate_citations(
        refs, known_doc_ids=known_doc_ids, retrieved_doc_ids=retrieved_doc_ids
    )
    if retrieved_doc_ids is not None:
        basis = "retrieved"
    elif known_doc_ids is not None:
        basis = "known"
    else:
        basis = "none"
    citation_ok = cite.ok if basis != "none" else cite.snippet_nonempty

    return CaseVerdict(
        case_id=str(case.get("id", "")),
        gold_points_all_hit=faith.all_hit,
        forbidden_absent=faith.forbidden_absent,
        citation_ok=citation_ok,
        citation_basis=basis,
        details={
            "points_total": len(faith.points),
            "points_hit": sum(1 for p in faith.points if p.hit),
            "missed_points": [p.point for p in faith.points if not p.hit],
            "hallucinated_entities": faith.hallucinated,
            "refs": cite.to_dict(),
        },
    )


# --------------------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------------------
@dataclass
class RagTally:
    total: int = 0
    retrieval_hits: int = 0
    exact_hits: int = 0
    """命中里「文档+块锚点」**精确**命中的题数（严格口径）。"""
    doc_level_hits: int = 0
    """命中里退化为**文档级宽松**命中的题数。`exact_hits + doc_level_hits == retrieval_hits`。"""
    faithfulness_sum: float = 0.0
    passed: int = 0
    """口径 3.3 的「单题通过」计数（每题 1 分，对应基础功能 RAG 计数 10 分）。"""
    approximated: int = 0
    """命中率里有几题在吃「答案引用」的近似值。
    非零说明预测方没给真召回（`retrieved`），只用契约名 `refs` 落了答案引用——
    这些题的命中率被**系统性低估**（答案引用 ⊆ 召回），报告里必须如实标注，
    不能让「近似值」看起来像「真召回跑出来的数」。"""
    cases: list[dict] = field(default_factory=list)

    def add(
        self,
        case_id: str,
        ret: RetrievalResult,
        faith: FaithfulnessResult | None,
        verdict: CaseVerdict | None = None,
        approximated: bool = False,
    ) -> None:
        self.total += 1
        if ret.hit:
            self.retrieval_hits += 1
            if ret.exact:
                self.exact_hits += 1
            else:
                self.doc_level_hits += 1
        if approximated:
            self.approximated += 1
        if faith is not None:
            self.faithfulness_sum += faith.score()
        if verdict is not None and verdict.passed:
            self.passed += 1
        self.cases.append(
            {
                "id": case_id,
                "retrieval": ret.to_dict(),
                **({"retrieval_approximated": True} if approximated else {}),
                **({"faithfulness": faith.to_dict()} if faith else {}),
                **({"verdict": verdict.to_dict()} if verdict else {}),
            }
        )

    @property
    def hit_rate(self) -> float:
        return self.retrieval_hits / self.total if self.total else 0.0

    @property
    def exact_hit_rate(self) -> float:
        """严格口径命中率：只数「文档+块锚点」精确命中，**与 `hit_rate` 并列展示**。

        用途：区分「真的定位到章节」（精确）与「只命中了文档」（宽松兜底）——
        后者不能证明章节定位正确。主分数仍取 `hit_rate`（含宽松兜底），
        本值只作**并列参考**，不改变任何判定与退出码。
        """
        return self.exact_hits / self.total if self.total else 0.0

    @property
    def faithfulness(self) -> float:
        return self.faithfulness_sum / self.total if self.total else 0.0

    @property
    def pass_rate(self) -> float:
        """单题通过率（口径 3.3 的计数分比例）。"""
        return self.passed / self.total if self.total else 0.0

    def failing_cases(self) -> list[dict]:
        """没通过的题 + 卡在哪个子条件——演示时「这题为什么不算对」的答案。"""
        out = []
        for c in self.cases:
            v = c.get("verdict")
            if v and not v["passed"]:
                out.append(
                    {
                        "id": c["id"],
                        "gold_points_all_hit": v["gold_points_all_hit"],
                        "forbidden_absent": v["forbidden_absent"],
                        "citation_ok": v["citation_ok"],
                        "missed_points": v.get("details", {}).get("missed_points", []),
                        "hallucinated_entities": v.get("details", {}).get("hallucinated_entities", []),
                        "unresolved_doc_ids": v.get("details", {}).get("refs", {}).get("unresolved_doc_ids", []),
                        "nonempty_snippet_count": v.get("details", {}).get("refs", {}).get("nonempty_snippet_count", 0),
                    }
                )
        return out

    def summary(self) -> dict:
        """汇总。`retrieval_approximated` 非零表示有这么多题的命中率在用
        「答案引用」近似召回（会低估）—— 0 才是真召回跑出来的命中率，
        报告与终端都要把它显示出来，别让近似值看起来像真值。"""
        from metrics import accuracy_band

        return {
            "total": self.total,
            "retrieval_hits": self.retrieval_hits,
            "hit_rate": round(self.hit_rate, 4),
            "hit_rate_bands": accuracy_band(self.hit_rate),
            "retrieval_approximated": self.approximated,
            # 严格/宽松并列（不改变主分数）：只有精确命中才证明章节定位正确。
            "exact_hits": self.exact_hits,
            "doc_level_hits": self.doc_level_hits,
            "exact_hit_rate": round(self.exact_hit_rate, 4),
            "faithfulness": round(self.faithfulness, 4),
            "faithfulness_bands": accuracy_band(self.faithfulness),
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "pass_rate_bands": accuracy_band(self.pass_rate),
        }
