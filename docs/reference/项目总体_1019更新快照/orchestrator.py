"""编排层：把一句提问变成「调哪些工具、按什么顺序、结果怎么拼」的计划。

**它在系统中的位置**：提问进来 → 编排层判断要调哪些工具 → 工具各自执行 →
编排层合并结果 → 回答出去。三层里唯一知道「全局」的一层。

**本文件的三个关键设计**（对应方案骨架第 2.2 节）：

  机制一 · 混合式路由
      先用**规则粗筛**（便宜、可预测、今天就能跑），置信度不足时**留好接口**
      升级为模型规划（09-24 之后放开）。不是二选一，是一条渐进的路。

  机制二 · 统一工具协议
      所有工具都走 `tool(payload) -> ToolResult`，**契约无需改动**。
      这正是「融合而非替换」在代码上的体现：新增能力不破坏既有约定。

  机制三 · 工具注册表
      编排层从注册表按名字 / 按能力找工具，而不是硬编码 if-else。
      换实现（mock → KAG）只换注册的东西，编排层不动。

**当前状态（09-21）**：规则式路由 + mock 工具，目标是「闭环先跑通」。
    真实图谱能力在 09-24 之后接入，接入点见 `build_default_orchestrator`。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import routing_signals
from app.contracts import (
    AskResponse,
    ContextContinuation,
    DataSource,
    ResultSetInfo,
    SqlRef,
    Suggestion,
    ToolResult,
    TraceStep,
    merge_trace,
)
from app.result_sets import ResultSetStore
from app.clarifier import SemanticClarifier
from app.evidence_ledger import FactPlan, ledger_for_response
from app.progress import ProgressCallback, emit_progress, fail_active_progress, progress_scope
from app.fact_planner import (
    WORK_ORDER_ID_SEARCH_RE,
    build_work_order_sla_plan,
    requests_first_response,
    requests_repair_completion,
)
from app.nl2sql.config import env_int, time_anchor_from_env
from app.nl2sql.followup import resolve_followup
from app.nl2sql.normalizer import NormalizeResult, normalize
from app.nl2sql.planning_models import QueryIntent
from app.condition_archive import merge_condition_archive
from app.query_requirements import (
    REQUIREMENTS_VERSION,
    QueryRequirements,
    RequirementExtractor,
    extract_requirements,
)
from app.memory import SessionMemory
from app.tools.base import CAPABILITY_DOC, CAPABILITY_GRAPH, CAPABILITY_TABLE
from app.tools.registry import ToolRegistry


#: 要求清单里「来自上一轮待澄清的原问题」这类的来源标记。
#: 与 `session`（上一轮已成立的条件档案）区分开：`session` 是**仍然生效**的旧条件，
#: 本轮补一句就作数；`pending_origin` 是**用户上一轮问却没问完的那个问题本身**，
#: 只在 `pending_clarification_resumed` 这一种动作下才回灌。
_PENDING_ORIGIN_SOURCE = "pending_origin"


# ============================================================================
# 路由：决定「这一问要调哪些工具」
# ============================================================================


@dataclass
class RoutePlan:
    """一次路由的产出：按顺序要调的工具，以及每个工具该拿到什么线索。

    Attributes:
        steps: 有序的执行清单，每项是 (工具名, 附加线索)。
               顺序有意义 —— 后面的工具常常要用前面查出来的东西当线索。
        reason: 为什么这么路由，人话一句。**必须填**：
                这是可解释性的一部分，也是模型规划升级后核对差异的基准。
        confidence: 规则粗筛的可信度（high / medium）。medium 表示
                    「规则说不准，将来该升级给模型」，现在先用规则扛着。
        need_clarify: 问题本身信息不足、该先反问用户。
        clarify_question: 需要澄清时的反问句。
    """

    steps: list[tuple[str, str]]
    reason: str
    confidence: str = "high"
    need_clarify: bool = False
    clarify_question: str = ""
    fact_plan: FactPlan | None = None
    query_intent: QueryIntent | None = None
    review_fact_plan: FactPlan | None = None
    review_coverage: str | None = None


#: 路由信号来自 `app/routing_signals.py`：
#: **实体词（哪些词属于文档 / 哪些属于数据库）从两边的元数据派生** ——
#: 文档侧读 `data/corpus/manifest.json`，结构化侧读 `infra/schema_meta*.json`。
#: 于是往 raw/ 丢新文档、给库加新表都不用回来改代码。
#: 手写的只剩"问法词"（怎么 / 多少 / 并且 …），它们不指任何数据源。
#: 曾经的 _KEYWORD_TABLE / _KEYWORD_DOC 就是反面教材：语料里加了《机房运维操作规程》，
#: 词表里没有「机房」「巡检」，于是「机房巡检频率是多少」被判成结构化查询，
#: 实测返回 SELECT COUNT(*) 并把答案渲染成「n：59」。


#: 书名号题名：《…》。中文里引用一份具名文档的确定性标记，
#: 用它而不是业务词判断「这道题在点名文档」，见 `_plan` 里 named_doc 的注释。
_NAMED_DOC_RE = re.compile(r"《[^》]{2,}》")


def _signal_desc(sig: dict, side: str) -> str:
    """把命中信号写成一句能放进 reason 的话（可解释性要求：说清"凭什么这么判"）。

    把实体词与问法词分开写：实体词是"问题里出现了某份文档 / 某张表的词"，
    问法词是"想要什么形态的答案"。演示时要能一眼看出判据是哪几个词。
    """
    terms = sig["doc_terms"] if side == "doc" else sig["sql_terms"]
    intents = sig["doc_intent"] if side == "doc" else sig["sql_intent"]
    parts = []
    if terms:
        parts.append("实体词「" + "、".join(terms[:3]) + "」")
    if intents:
        parts.append("问法词「" + "、".join(intents[:2]) + "」")
    return "、".join(parts) if parts else "（无）"


def _continuation_info(previous_question: str | None) -> ContextContinuation | None:
    """把「本轮接续了上一轮」做成可在界面上展示的事实。

    只在**确实接续**时返回对象，否则返回 None —— 前端可以直接判有无，
    不必再判断字段是否填了空串。

    `round_index` 用接缝行数算：`SessionMemory._combine` 每次累加一行
    「用户补充或修改：」，所以上一轮文本里该标记的**出现次数加一**
    就是本轮轮次。不单独记账，是为了和实际送去.index_corpus 的同一份字符串保持
    同步 —— 两处各记一份迟早会漂。
    """
    if not previous_question or not previous_question.strip():
        return None
    text = previous_question.strip()
    seams = text.count("用户补充或修改：")
    return ContextContinuation(
        previous_question=text,
        round_index=seams + 2,
    )


def route(question: str, registry: ToolRegistry | None = None) -> RoutePlan:
    """规则式路由：按信号打分判断该调哪些工具、什么顺序。

    设计上是**粗筛**不是精判：宁可多调一个工具（代价可控），不可漏调
    （漏调会直接答错）。所以有信号就加工具，不追求最小工具集。

    判断依据（打分细节见 `routing_signals.analyse`）：
        doc_score = 命中文档实体词 * 2 + 命中文档问法词
        sql_score = 命中结构化实体词 * 2 + 命中聚合问法词
    实体词权重更高 —— 它直接指出"这里出现了某份文档 / 某张表的名字"，
    问法词只说明"想要什么形态的答案"，单独不足以定位数据源。

    Args:
        question: 用户原话。
        registry: 工具注册表，用于校验「要调的工具是否真的存在」。

    Returns:
        `RoutePlan`。`steps` 里每一项是 (工具名, 线索)。
    """
    q = question.strip()

    # 独立的工单—合同演示链路默认不注册；仅在显式启用且问题含演示工单编号时触发。
    if registry is not None and registry.has("cross_source_demo"):
        demo_match = WORK_ORDER_ID_SEARCH_RE.search(q)
        if demo_match is not None:
            if requests_first_response(q) and not requests_repair_completion(q):
                return RoutePlan(
                    steps=[],
                    reason="当前隔离原型仅实现修复完成及其与首次响应的组合判定",
                    confidence="high",
                    need_clarify=True,
                    clarify_question=(
                        "当前演示原型可核验修复完成时限；若还要核验首次响应，请同时说明"
                        "是否也要判断修复完成时限。"
                    ),
                )
            work_order_id = demo_match.group(1).upper()
            return RoutePlan(
                steps=[("cross_source_demo", work_order_id)],
                reason="问题包含演示工单编号，调用隔离的结构化记录—合同文档核验工具",
                confidence="high",
                fact_plan=build_work_order_sla_plan(
                    work_order_id,
                    include_first_response=requests_first_response(q),
                ),
            )

    # ── 先判：问题是不是太空，需要澄清 ────────────────────────────────
    if len(q) < 4:
        return RoutePlan(
            steps=[],
            reason="问题太短，缺少可判断的要素",
            need_clarify=True,
            clarify_question="您想问的是哪个数据？请补充一下对象（比如某个客户、某份文档或某个年份）。",
        )

    sig = routing_signals.analyse(q)
    want_doc = sig["doc_score"] > 0
    want_table = sig["sql_score"] > 0
    # 书名号题名：题面里写了《…》，是在**点名一份文档**，不是在问库里的某张表。
    #
    # 2026-10-18 实测事故：政采 6 道「《某项目采购公告》中载明的预算金额是多少？」
    # 全部被判成 nl2sql —— 因为 schema 说明里也有「采购」「公告」「预算」「金额」
    # 这些词（它们本来就是同一套业务词汇），sql_score 轻松压过 doc_score。
    # 这类题的答案只存在于那份公告原文里，结构化查询根本查不到。
    #
    # 判据用**书名号**而不是词表：书名号是中文里「引用一份具名文档」的确定性标记，
    # 不依赖语料，换任何文档集都成立；而「公告」这类词两边都有，天然不可靠。
    named_doc = bool(_NAMED_DOC_RE.search(q))
    # 法规用语密集出现（≥2 个）：问的是**制度条文**，不是在数库里的记录。
    #
    # 2026-10-18 实测：grag-04「竞争性谈判小组……评审专家人数不得少于成员总数的
    # 多少？……小组应当由几人以上单数组成？」同时命中「不得少于」「应当由」
    # 「评审专家」三个法规用语、得分 3，可结构化侧「招标/评审/询价/谈判/项目/标准」
    # 这些**招投标业务词**得分 6，仍被判成问数题。业务词本来就是两侧共有的，
    # 靠加减分区分不了；而一题里密集出现多个规范性用语，几乎只可能是问答条款。
    #
    # 阈值 2 而非 1：单个「要求」「规定」在问数题里也会出现（「要求补货次数汇总」），
    # 两个以上才足以认定是条文型问法。政采 30 道结构化题里最多命中 1 个（实测）。
    regulation_heavy = len(sig["doc_intent"]) >= 2
    # 图谱只由显式连接词触发，而且要求两侧**各自独有**的信号同时在场：
    # 「员工」这类词既在《员工手册》标题里、又在 employee 表说明里，光凭它
    # 加上一个「以及」会把「列出每位员工的编号与姓名，以及他负责的客户」这种
    # 纯结构化问题误判成多跳（实测发生过）。
    cross_source = len(sig["connectors"]) > 0
    cross_source_candidate = cross_source and len(sig["doc_only"]) > 0 and len(sig["sql_only"]) > 0
    graph_available = registry is None or registry.has("graph_reason")
    want_graph = cross_source_candidate and graph_available
    graph_unavailable = cross_source_candidate and not graph_available

    steps: list[tuple[str, str]] = []
    hints: list[str] = []

    if want_graph:
        # 跨源多跳：图谱工具自己会串联业务数据与文档，不需要先跑另外两个
        steps.append(("graph_reason", ""))
        hints.append("命中跨源连接词，判断为需要多跳串联")
        reason = (
            "同时出现数据信号「" + "、".join((sig["sql_terms"] + sig["sql_intent"])[:3])
            + "」与文稿信号「" + "、".join((sig["doc_terms"] + sig["doc_intent"])[:3])
            + "」，且有连接词「" + "、".join(sig["connectors"][:2])
            + "」→ 走图谱推理（多跳）"
        )
        confidence = "high"
    elif graph_unavailable:
        # Experiments without a verified graph backend use the real SQL and RAG tools.
        steps.extend((("nl2sql", ""), ("doc_search", "")))
        hints.append("未启用真实图谱后端，按 SQL 后文档检索的普通跨源工具链执行")
        reason = "跨源连接需求存在，但图谱工具不可用，退回结构化查询与真实文档检索"
        confidence = "medium"
    elif (want_table and not sig["doc_intent"] and any(
        term in sig["sql_intent"]
        for term in ("统计", "数量", "个数", "订单数", "人数", "记录数",
                     "汇总", "总计", "合计", "占比", "平均", "排名")
    )):
        # 明确的统计问法优先按结构化查询处理；表关系词或文档标题中的同词
        # 不能把“关联订单数”一类 SQL 问题改路由到文档/图谱。
        steps.append(("nl2sql", ""))
        hints.append("命中明确统计问法，优先走结构化查询")
        reason = (
            "命中" + _signal_desc(sig, "sql") + " → 明确请求统计结果，走结构化查询"
        )
        confidence = "high"
    elif regulation_heavy:
        # 放在统计问法之后、普通文档判定之前：条文型问法即便同时带「多少」
        # 也是在问条款里的数字（「至少多少个工作日」），不是库里的统计值。
        steps.append(("doc_search", ""))
        hints.append("命中多条法规用语：" + "、".join(sig["doc_intent"][:3]))
        reason = (
            "一题内密集命中规范性用语「" + "、".join(sig["doc_intent"][:3])
            + "」→ 问的是制度条文，走文档检索"
        )
        confidence = "high"
    elif named_doc:
        # 书名号点名文档：文档优先于结构化。放在统计问法之后，是因为
        # 「《…》里共有几条」这种仍可能是在数结构化记录，统计诉求更明确。
        steps.append(("doc_search", ""))
        hints.append("题面用书名号点名文档，按文档检索处理")
        reason = (
            "题面出现「《「…」》」形式的文档题名 → 答案在那份文档原文里，"
            "走文档检索"
        )
        confidence = "high"
    elif want_doc and sig["doc_score"] > sig["sql_score"]:
        # ⚠ 严格大于，不是 `>=`。
        #
        # 2026-10-18 实测：`>=` 让**平局**归了文档侧，代价是三道明确的问数题
        # 被判成文档检索 ——「库里一共有多少个标包？」（「标包」既是 packages 表的
        # 说法、也出现在公告文档里）、「发布了多少条采购公告？」（「公告」两侧都有）。
        # 平局意味着两侧证据一样强，此时该按「要数就走库」的常理处置：
        # 问数题的正确答案是一个统计值，查库能算出来；查文档只能捞到原文。
        steps.append(("doc_search", ""))
        hints.append("命中文稿实体词：" + "、".join((sig["doc_terms"] + sig["doc_intent"])[:3]))
        reason = (
            "命中" + _signal_desc(sig, "doc") + " → 问题需要从文档原文中查证"
        )
        confidence = "high"
    elif want_table:
        steps.append(("nl2sql", ""))
        hints.append("命中结构化信号：" + "、".join((sig["sql_terms"] + sig["sql_intent"])[:3]))
        reason = (
            "命中" + _signal_desc(sig, "sql") + " → 问题要的是「算出来的数」"
        )
        confidence = "high"
    else:
        # 两边的信号都没命中 —— 规则说不好，先按最简单的路走
        steps.append(("nl2sql", ""))
        reason = "文档与结构化信号都没命中，按默认走结构化查询（规则置信度不足，待模型规划接手）"
        confidence = "medium"
        hints.append("规则未命中，属低置信度")

    # 存在性校验：注册表里没有的工具不能出现在计划里（否则执行时才炸）
    if registry is not None:
        existing: list[tuple[str, str]] = []
        for name, hint in steps:
            if registry.has(name):
                existing.append((name, hint))
        if len(existing) == 0:
            existing.append((registry.names()[0], ""))
            reason = reason + "；计划中的工具均未注册，回落为注册表首选工具"
        steps = existing

    return RoutePlan(steps=steps, reason=reason + "（" + "；".join(hints) + "）", confidence=confidence)


# ============================================================================
# 编排器
# ============================================================================


@dataclass
class ToolRun:
    """一次工具调用的留档：调了谁、拿到什么、花了多久、为什么调它。

    这是**可解释性的第一手材料** —— 上游的 `AskResponse.trace` 就是
    从这些留档拼出来的。之所以不只留 ToolResult，是因为「为什么调它」
    这个信息不在 ToolResult 里，但评测和答辩都会问。
    """

    tool_name: str
    reason: str
    result: ToolResult
    elapsed_ms: float


def _bounded_cell(value: Any) -> str:
    """把结果单元格裁成可安全放进提示词/记忆的短文本。"""
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").strip()
    return text[:120]


class Orchestrator:
    """编排器：路由 → 调度 → 合并 → 回答。

    最小可用的完整闭环。当前不含模型自动规划（那是 09-24 之后的事），
    但**接口已经给它留好位置**（`route()` 的 confidence=medium 就是升级信号）。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        graph_backend: str = "mock",
        fact_plan_executor=None,
        session_memory: SessionMemory | None = None,
        semantic_clarifier: SemanticClarifier | None = None,
        result_set_store: ResultSetStore | None = None,
    ) -> None:
        self.registry = registry
        self.graph_backend = graph_backend
        self.fact_plan_executor = fact_plan_executor
        self.session_memory = session_memory or SessionMemory(
            max_sessions=env_int("SESSION_MEMORY_MAX_SESSIONS", 256, minimum=1),
            ttl_seconds=env_int("SESSION_MEMORY_TTL_SECONDS", 1800, minimum=60),
            max_context_chars=env_int("SESSION_MEMORY_MAX_CONTEXT_CHARS", 4000, minimum=256),
        )
        self.semantic_clarifier = semantic_clarifier or SemanticClarifier()
        self.result_set_store = result_set_store or ResultSetStore.from_env()

    # ── 要求清单的抽取入口（独立成方法，便于集成测试打在真实接线上）──
    @staticmethod
    def _clarify_object_labels() -> tuple[str, ...]:
        """当前业务库里真实存在的业务对象说法，供澄清话术举例子用。

        **为什么要有这个方法**：澄清话术原先写死「（如客户、发票或员工）」，
        那是 chinook 的业务对象。政采库上一张都没有，追问时却照样这么说，
        等于把用户往错方向引。

        **为什么从 schema 元数据读而不是查 catalog**：`catalog` 要经链路装配
        才能拿到，而澄清发生在装配之前（追问时根本不需要连库）。元数据文件
        本来就在本地，读一次缓存住即可。

        **为什么取表描述的第一个短语而不是表名**：表名是 `packages`
        `announcements` 这种英文，直接塞进中文话术里用户看不懂；表描述写的是
        「采购包组。一行代表一个项目里的一个包（标段）」——第一个句号之前那句
        就是这套库自己的业务对象叫法。

        读不到元数据时返回空元组：话术退化成不举例子的完整句子，仍然可读。
        """
        cached = getattr(Orchestrator, "_OBJECT_LABELS_CACHE", None)
        if cached is not None:
            return cached
        labels: list[str] = []
        try:
            path = os.environ.get("SCHEMA_META_PATH") or str(
                Path(__file__).resolve().parents[1] / "infra" / "schema_meta.json"
            )
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            for table in payload.get("tables") or []:
                if not isinstance(table, dict):
                    continue
                description = str(table.get("description") or "").strip()
                if not description:
                    continue
                head = description.split("。", 1)[0].strip()
                # 「采购公告。一行代表一份公告…」→「采购公告」
                # 「中标结果。一行代表一个包的中标记录」→「中标结果」
                if head and len(head) <= 12 and head not in labels:
                    labels.append(head)
        except (OSError, ValueError):
            labels = []
        result = tuple(labels)
        Orchestrator._OBJECT_LABELS_CACHE = result
        return result

    @staticmethod
    def _extract_requirements_for(
        norm: NormalizeResult,
        spoken: str,
        inherited: QueryRequirements | None = None,
    ) -> QueryRequirements:
        """从**用户本轮原话**抽要求清单。

        为什么必须是独立方法而不是内联一行：清单的 `surface`/`span` 是给
        「可解释」留痕的，必须指向用户真实说过的字。抽成一个方法，集成测试
        才能在不启动大模型、不连数据库的前提下打在**编排真正调用的那条线**上
        —— 早先的测试直接调 `extract_requirements()`，绕过了这里，
        所以接线退回旧写法时测试照样全绿（该缺陷由变异测试发现）。

        ⚠ 参数是**用户本轮原话**（`original_question`），不是
        `norm.original`、更不是 `norm.normalized`。

        这里踩过一个很深的坑，务必看清：`prepare_turn` 交给下游的是
        **多轮拼接文本**（`原句 + "\\n用户补充或修改：" + 本轮`），
        第 3 轮时 `norm.original` 会长成

            平台上一共有多少位客户？\\n用户补充或修改：那2025 年的呢？\\n用户补充或修改：只看巴西的呢？

        于是每一轮都在**重新解析前面所有轮**。更糟的是第 2 轮的「去年」
        在入档时已被改写成 `2025`，第三轮解析拼接文本时它作为 `literal`
        再次出现 —— 档案以为用户本轮又提了一遍时间条件，
        「只看巴西的呢？」想收窄范围也甩不掉，答案里就多出一段
        用户没问的时间口径说明。（该缺陷由 `_spy_req.py` 打点实录定位。）

        所以要两件事同时成立才正确：
          1. 抽清单只看**本轮原话**（本参数）；
          2. 跨轮继承只走 `inherited`（档案），不由拼接文本回灌。

        留痕指向不存在的文本，比不留痕更糟 —— 用户从没在第 3 轮说过 "2025"。

        `inherited` 是上一轮仍生效的要求（F3）。接续轮里用户只说「男的有多少？」
        时，"员工"这个对象由它带过来，否则澄清器会追问一个用户上一轮已经说过的
        对象。非接续轮传 `None`，行为与加入本特性之前完全一致。

        `spoken` 为空时兜底用 `norm.original`（防御性，正常路径用不到）。
        """
        return extract_requirements(spoken or norm.original, norm, inherited=inherited)

    # ── 对外主入口 ───────────────────────────────────────────────────
    def answer(
        self,
        question: str,
        session_id: str = "default",
        *,
        retrieval_mode: str = "standard",
        progress_callback: ProgressCallback | None = None,
    ) -> AskResponse:
        """Serialize requests sharing one session while allowing other sessions to proceed."""
        if retrieval_mode not in {"standard", "deep"}:
            raise ValueError("retrieval_mode 只能是 standard 或 deep")
        with progress_scope(progress_callback):
            try:
                with self.session_memory.serialize_request(session_id):
                    return self._answer_serialized(
                        question, session_id, retrieval_mode=retrieval_mode
                    )
            except Exception:
                fail_active_progress()
                emit_progress("answer", "整理并返回回答", "failed")
                raise

    def _answer_serialized(
        self,
        question: str,
        session_id: str,
        *,
        retrieval_mode: str = "standard",
    ) -> AskResponse:
        """回答一个提问，返回契约定义的 `AskResponse`。

        链路顺序：**先改写，再路由**（docs/要素识别与改写_接口_v0.1.md §8.1）——
        路由用的是子串匹配，用户说「买家」时若不先改写成 customer，路由根本认不出实体词。
        """
        original_question = question
        previous_query_state = self.session_memory.query_state(session_id)
        # 必须在 prepare_turn 之前取值：prepare 会清空 pending 状态，
        # 之后读到的「上一轮历史」可能已经变成别的样子。
        previous_history = self.session_memory.last_question(session_id)
        # 追问前那一轮的原始提问：**必须在 prepare_turn 之前取**。
        # prepare_turn 在「取消」「换新问题」两个分支里会清空待澄清状态，
        # 之后就读不到了。任务 3 的核心判据是"补充条件后在同一会话内继续回答
        # 原来那个问题"，而原问题里的指标与年度只存在于这条记录里 ——
        # 参见下方 pending_requirements 的用法。
        pending_origin = self.session_memory.pending_prompt(session_id)
        prepared_question, session_action, previous_question = self.session_memory.prepare_turn(
            session_id, question
        )
        question = prepared_question
        continuation = _continuation_info(previous_question)

        if session_action == "pending_cancelled":
            # 取消澄清不算接续：用户是在退出上下文，不是接着往下问。
            #
            # trace 里必须留一条记录（2026-10-19 修）。此前这里返回 `trace=[]`，
            # 于是"这一轮到底是被识别成取消、还是链路崩了什么都没做"在运行记录里
            # 完全看不出来 —— 任务 3 的题包复核把这一轮判成「多余追问」，
            # 而实际行为是对的（系统正确地没有追问）。这是可解释性的缺口，
            # 不是行为错误：**该记录的确定性事实没有记录**。
            # 这条 step 不调任何工具、不碰数据库，只记会话动作本身。
            cancel_step = TraceStep(
                step_id=1,
                tool="session_memory",
                input={"question": original_question},
                output={
                    "session_action": session_action,
                    "pending_question": pending_origin or "",
                    "cleared": True,
                },
                elapsed_ms=0,
                hit=None,
            )
            return AskResponse(
                answer="已取消待澄清的问题。",
                trace=[cancel_step],
                refs=[],
                sql=None,
                need_clarify=False,
                context_continuation=None,
            )

        emit_progress("understand", "识别问题要素", "started")
        # ① 要素识别与改写（中级任务 1）。时间基准取 APP_TIME_ANCHOR（跑分固定 2024-06-15），
        #    未设置则取当前时间 —— 见 app/nl2sql/config.time_anchor_from_env。
        norm: NormalizeResult = normalize(question, time_anchor=time_anchor_from_env())
        norm_step = TraceStep(
            step_id=1,
            tool="normalizer",
            input={"question": original_question, "session_action": session_action},
            output=norm.model_dump(mode="json"),
            elapsed_ms=norm.elapsed_ms,
            # 改写记录塞不进 hit（RefT 只有 SqlRef / DocRef），放 output（§8.3）：
            # hit=None 时 collect_refs 完全不受影响，契约不动、refs 不变量不破。
            hit=None,
        )
        # ② 标准问句往下走：路由与工具看到的都是改写后的句子（§8.2 第 1 条）
        if norm.normalized.strip():
            question = norm.normalized

        clarification_started = time.perf_counter()
        # F3：接续轮把上一轮仍生效的要求带过来。
        # 只在真的接续时给 —— 非接续轮（新话题）沿用旧条件会让用户
        # 上一轮问过的东西污染这一轮，比不继承更糟。
        archive_before = self.session_memory.condition_archive(session_id)
        is_followup_turn = session_action in (
            "short_followup_attached",
            "pending_clarification_resumed",
        )
        inherited = None
        #: 接续轮**先把本轮清单与档案预合并**，再从合并后的档案取继承条。
        #: ⚠ 不能直接用 `archive_before`（上一轮答完时的档案）：本轮"换对象"
        #: 的判定发生在 merge 里，merge 却原本排在**生成之后**（见
        #: `_save_condition_archive`）—— 时序错位导致上一轮仍 in_force 的
        #: 条件被原样继承。实测踩过：t2「只看巴西的发票」→ t3「那有多少位
        #: 员工？」，巴西 value_filter 以 source=session 被塞进 t3 清单，
        #: 生成器照「上一轮继承，必须延续」把 Brazil 写进了员工 SQL。
        #: 预合并后巴西已 stale，继承自然不带它。合并是纯函数，提前算与
        #: 答后算结果一致；算出的档案在执行成功后直接落库，不再二次合并。
        pending_archive = None
        if is_followup_turn and archive_before is not None:
            try:
                pending_archive = merge_condition_archive(
                    archive_before,
                    self._extract_requirements_for(norm, original_question),
                    question=original_question,
                    is_followup=True,
                )
            except Exception:
                pending_archive = None  # 预合并失败退回旧行为（答后再合并）
        if is_followup_turn and archive_before is not None:
            source_archive = pending_archive or archive_before
            active = source_archive.active_requirements
            if active:
                inherited = QueryRequirements(
                    version=REQUIREMENTS_VERSION,
                    original="",
                    normalized_question="",
                    requirements=active,
                )
        # ⚠ 清单**只从本轮用户原话提取**，不从 `question`（已拼接上一轮原文）。
        #    `prepare_turn` 会把上一轮原句与本轮拼在一起交给下游（历史行为），
        #    归一化后上一轮的"去年"会被 normalizer 锚成 `2025` 并以 `literal`
        #    身份混进本轮清单 —— 于是档案以为用户本轮又提了一次时间条件，
        #    收窄范围（「只看巴西的呢？」）也甩不掉它，答案里多出一段用户
        #    没问的时间口径说明。继承是**档案**的职责（上面的 `inherited`），
        #    不该由拼接文本再灌一遍。
        requirements = self._extract_requirements_for(norm, original_question, inherited)
        # ── 追问后恢复：把「上一轮已确认的原问题」并进本轮清单 ──────────
        #
        # 需求来源：任务 3 题包把「补充条件后在**同一会话内**继续回答原问题」
        # 当核心判据。但上面这条 extract 只抽本轮原话（第 2 步已论证为什么），
        # 于是第 2 轮用户只补一句「2023 年相比 2022 年」时，清单里只有两条
        # time、没有 measure「销售额」；第 2 轮只补「销售额」时，清单里没有
        # 那对年度。生成器拿到的 question 又是本轮原话（app/orchestrator.py:785
        # 的 turn_question），跨轮只有 requirements 这一条通道 —— 缺什么，
        # 模型就只能自由发挥。
        #
        # 2026-10-19 实测（eval/reports/mainline/t35-chinook-20261018T100041Z）：
        # 正是这条通道缺失，导致「销售额增长率是多少？→ 2023 年相比 2022 年」
        # 生成 `invoice_date >= '2023-01-01' AND < '2023-01-02'`（年度区间退化成
        # 单日），「2023 年相比 2022 年的增长率是多少？→ 销售额」变成
        # `GROUP BY billing_country` 的国家销售额列表。
        #
        # 只并**原问题里有、本轮没再提**的类别（`_augment_from_inherited` 的
        # 按 kind 规则），本轮原话永远优先；重复调用是幂等的。
        pending_requirements = None
        pending_origin_used = False
        # `short_followup_attached` 也要走这条：T3-STEP-03 t3「改成 2021 年」是
        # **成功回答之后**的修订句，此时没有 pending_question，动作是
        # `short_followup_attached` 而不是 `pending_clarification_resumed`；
        # 上一轮的「2023 年销售额增长率」全在 `previous_history`（= `last_question`）
        # 里，不看它就会把「改成 2021 年」当成一个没有业务对象的新问题，
        # 反过来追问「要统计的业务对象是什么」（实测就是这么失败的）。
        if (session_action == "short_followup_attached"
                and not pending_origin and previous_history):
            pending_origin = previous_history
        if (session_action in ("pending_clarification_resumed",
                               "short_followup_attached")
                and pending_origin):
            # ⚠ pending_origin 是**上一轮拼好的链**（`begin_clarification` 存的是
            #   拼接后的 question），不是"最初那句原问题"。多轮追问时它会越积越长：
            #   STEP-02 实测 pending_origin =
            #   「销售额增长率是多少？\n用户明确补充的增长率比较周期（对比期/基期）：2023 年」。
            #   把这条链整段重新抽一遍，`_attach_growth_pair` 就会把那句补充里
            #   的 2023 认成本期，**这正是对的**；但若把它当成"原问题"再拼给下游，
            #   接缝会重复出现、时间约束会自相印证，模型反而看不出还有一期没给。
            #   因此这里只用它**补类别**（口径、对象），时间一律以本轮清单为准。
            pending_requirements = self._extract_requirements_for(
                normalize(pending_origin, time_anchor=time_anchor_from_env()),
                pending_origin,
            )
            pending_requirements = replace(
                pending_requirements,
                requirements=tuple(
                    item if item.source == _PENDING_ORIGIN_SOURCE
                    else replace(item, source=_PENDING_ORIGIN_SOURCE)
                    for item in pending_requirements.requirements
                ),
            )
            merged = RequirementExtractor._augment_from_inherited(
                list(requirements.requirements), pending_requirements
            )
            if len(merged) != len(requirements.requirements):
                pending_origin_used = True
                requirements = replace(requirements, requirements=tuple(merged))
            # 跨轮补充时，「本期 / 基期」只能从**整条原话链**判出来：
            # 抽取器按设计只看本轮原话（见 `_extract_requirements_for` 的注释），
            # 本轮往往只有一句「2022 年」，另一期在 `pending_origin` 里。
            # 合并完再用整条链补一次 growth 的两段区间 —— 抽取器对
            # 单轮片段判不出时本就不会写这两个字段，所以这里是纯补，
            # 不会覆盖本轮已经判定的口径。
            #
            # 条件不能只看 `pending_origin_used`：`short_followup_attached`
            # （对上一轮成功结果的修订句，如「改成 2021 年」）里，上一轮已把
            # 口径整批继承下来（`inherited.applied=True, count=6`），增量合并
            # 一条也加不出来，`pending_origin_used` 因此恒为 False，
            # 基期的修订就永远落不到 `growth_base` 上。实测（T3-STEP-03 t3）：
            # SQL 仍算 2023 对 2022，2021 只落在 WHERE 里当垃圾条件。
            # 所以只要这一轮带着 `pending_origin`、且合并后清单里有 growth
            # 要求，就重判一次 —— `attach_growth_pair` 本身是幂等的。
            has_growth = any(item.kind == "growth" for item in requirements.requirements)
            if pending_origin_used or (has_growth and pending_origin):
                # ⚠ 必须用**整条链**（pending_origin + 本轮原话）去判，不能只用
                #   pending_origin：那一段里没有本轮刚补的「2022 年」，
                #   两期永远凑不齐，`attach_growth_pair` 每次都返回原样。
                #   `question` 正是 prepare_turn 拼好的那条链。
                requirements = RequirementExtractor.attach_growth_pair(
                    requirements,
                    normalize(question, time_anchor=time_anchor_from_env()),
                )
        # F3：把继承清单也交给澄清器。
        #
        # ⚠ **这里传的 `inherited` 在实践中是冗余的**，保留它只为把契约定死：
        #    `_extract_requirements_for` 已经把继承条以 `source="session"` 并进
        #    `requirements`，所以澄清器看 `clist.has_object` 就已经能判对。
        #    实测：把第 4 个参数去掉，端到端行为完全不变（变异 M2 抓不到差异）。
        #    之所以仍显式传，是因为"澄清器该不该看继承"是独立契约 —— 若将来
        #    有人在 `_extract_requirements_for` 之外另建清单，或改掉合并策略，
        #    这里不会跟着悄悄退化。直接调用 `decide()` 的单元测试也依赖它。
        clarification = self.semantic_clarifier.decide(
            question, norm, requirements, inherited,
            object_labels=self._clarify_object_labels(),
        )
        emit_progress(
            "understand", "识别问题要素", "completed",
            elapsed_ms=norm.elapsed_ms + (time.perf_counter() - clarification_started) * 1000.0,
        )
        clarification_step = TraceStep(
            step_id=2,
            tool="semantic_clarifier",
            input={"question": question},
            output={
                "status": clarification.status,
                "missing_slots": list(clarification.missing_slots),
                "reason_code": clarification.reason_code,
                # 清单入 trace：出问题时能直接看到"系统当时认为用户要什么"，
                # 而不用从 SQL 反推。这也是"可解释"要留的那一层痕迹。
                "requirements": [
                    {
                        "kind": item.kind,
                        "surface": item.surface,
                        "normalized": item.normalized,
                        "unresolved": item.unresolved,
                        "requirement_id": item.requirement_id,
                        # `source` 必须入 trace：`session` 表示这条是**继承**来的、
                        # 用户本轮并没说过。少了它，从 trace 看不出"系统当时认为
                        # 用户要什么"里哪些是本轮原话、哪些是上一轮带过来的，
                        # 排查多轮问题时会把继承条误当成用户本轮的要求。
                        "source": item.source,
                    }
                    for item in requirements.requirements
                ],
                # 继承留痕（S2-F/F5）：出问题时能看出这一轮**是否**接着上一轮问。
                # 只记条数与是否生效，条目的明细在上面的 `requirements` 里
                # （以 `source="session"` 标出），重复列会让两边容易不一致。
                "inherited": {
                    "applied": inherited is not None,
                    "count": len(inherited.requirements) if inherited else 0,
                },
                # 追问恢复留痕（任务 3）：本轮从**待澄清的原问题**带回了哪些要求。
                # 只记原文与来源类别，条目的完整明细在上面 requirements 里
                # （以 `source="pending_origin"` 标出）。出问题时能一眼看出
                # 「这一轮补了基期，指标是从上一轮原问题带回来的」。
                "pending_origin": {
                    "applied": pending_origin_used,
                    "question": pending_origin or "",
                    "kinds": sorted({
                        item.kind for item in requirements.requirements
                        if item.source == _PENDING_ORIGIN_SOURCE
                    }),
                },
                "intent": {
                    "objects": list(clarification.intent.objects),
                    "measures": list(clarification.intent.measures),
                    "time_expressions": list(clarification.intent.time_expressions),
                    "dimensions": list(clarification.intent.dimensions),
                    "filters": list(clarification.intent.filters),
                    "target_grain": clarification.intent.target_grain,
                    "confidence": clarification.intent.confidence,
                },
            },
            elapsed_ms=(time.perf_counter() - clarification_started) * 1000.0,
            hit=None,
        )
        if clarification.status == "ask":
            # ⚠ 存进去的必须是**用户真说过的话**，不能存拼接后的 `question`。
            #    `question` 在这一轮可能已经带着上一轮的接缝（「销售额增长率是多少？
            #    \n用户明确补充的…：2023 年」），再把本轮这次的补充拼上去，
            #    下一轮的 `pending_origin` 就是两段接缝嵌套的链；而它既是
            #    "待澄清的原问题"、又要被重新抽成清单，嵌套会让 `_attach_growth_pair`
            #    把补充里的年份当成本期，反过来把真正缺的那一期盖掉。
            #    实测 T3-STEP-02：t2 只补「2023 年」后仍应追问，t3 补「2022 年」
            #    时却按国家分组列出了 20 行。
            #    用户原话链由 `_history_question` 单独维护（它拼的是原话，不是改写）。
            #    ⚠ 但 `_history_question` 对 `pending_clarification_resumed` 要求
            #      `prepared_question`，它在这里拿不到，会退回 `original_question`，
            #      于是 `pending_origin` 退化成「去年」「2023 年」这种纯补充片段，
            #      原意图整句丢掉，下一轮澄清器反而追问指标 —— 这是修掉上面那条
            #      嵌套问题之后暴露出来的第二个坑。
            #
            #    正确做法是**显式拼原话**：待澄清的原问题 = 历史原话 + 本轮补充，
            #    两者都是用户真说过的字，接缝只用本模块的 `_HISTORY_SEAM`。
            self.session_memory.begin_clarification(
                session_id,
                self._pending_chain(pending_origin, original_question),
                clarification.missing_slots,
            )
            return AskResponse(
                answer=clarification.question,
                trace=merge_trace([norm_step, clarification_step]),
                refs=[],
                sql=None,
                need_clarify=True,
                suggestions=self._suggestions(question, [], [], clarified=True),
                context_continuation=continuation,
            )

        emit_progress("route", "选择查询与检索路径", "started")
        try:
            plan = route(question, self.registry)
        except Exception:
            emit_progress("route", "选择查询与检索路径", "failed")
            raise
        emit_progress("route", "选择查询与检索路径", "completed")
        plan.query_intent = clarification.intent
        if plan.fact_plan is None:
            # Single-source and ordinary two-source routes establish their
            # requested facts before tools run. This review plan is descriptive;
            # it does not replace route-specific execution or the strict
            # work-order executor above.
            from app.fact_review import build_route_fact_plan

            review_plan = build_route_fact_plan(
                [tool_name for tool_name, _hint in plan.steps],
                question,
                plan.query_intent,
            )
            if review_plan is not None:
                plan.review_coverage, plan.review_fact_plan = review_plan
                plan.review_fact_plan.validate()

        followup_instruction = None
        followup_step = None
        if (
            session_action == "short_followup_attached"
            and previous_query_state
            and any(tool_name == "nl2sql" for tool_name, _hint in plan.steps)
        ):
            followup_instruction = resolve_followup(original_question, previous_query_state)
            if followup_instruction is not None:
                followup_step = TraceStep(
                    step_id=3,
                    tool="followup_resolver",
                    input={
                        "latest_user_turn": original_question,
                        "previous_columns": list(followup_instruction.previous_columns),
                        "previous_row_count": followup_instruction.previous_row_count,
                    },
                    output={
                        "operation": followup_instruction.operation,
                        "target": followup_instruction.target,
                        "previous_sql": followup_instruction.previous_sql,
                    },
                    elapsed_ms=0.0,
                    hit=None,
                )

        if plan.fact_plan is not None:
            # 先校验私有事实依赖图，再执行该演示路径；无效计划不得启动工具调用。
            plan.fact_plan.validate()

        # 需要澄清：直接返回，一条工具都不调（省钱、也避免瞎猜）
        if plan.need_clarify:
            self.session_memory.begin_clarification(
                session_id,
                question,
                ("user_supplied_context",),
            )
            return AskResponse(
                answer=plan.clarify_question,
                # 改写已经发生就必须留痕，否则「改了但没留痕」可解释性断链（§8.2 第 3 条）
                trace=merge_trace([norm_step, clarification_step]),
                refs=[],
                sql=None,
                need_clarify=True,
                suggestions=self._suggestions(question, [], [], clarified=True),
                context_continuation=continuation,
            )

        fact_run = None
        if plan.fact_plan is not None and self.fact_plan_executor is not None:
            work_order_id = next(
                (hint for tool_name, hint in plan.steps if tool_name == "cross_source_demo"),
                "",
            )
            fact_started = time.perf_counter()
            fact_run = self.fact_plan_executor.run(
                plan.fact_plan,
                {"question": question, "work_order_id": work_order_id},
            )
            answer_text = self._fact_plan_answer(plan.fact_plan, fact_run)
            fact_result = ToolResult(
                data=answer_text,
                trace=fact_run.trace,
                refs=fact_run.refs,
            )
            runs = [ToolRun(
                tool_name="fact_plan",
                reason=plan.reason,
                result=fact_result,
                elapsed_ms=(time.perf_counter() - fact_started) * 1000.0,
            )]
        else:
            # ⚠ 传给工具的是**本轮**问句，不是多轮拼接文本（prepare_turn 的产物）。
            #   拼接文本会把前几轮的原话原样带给生成器，模型会把上一轮的条件
            #   （如「只看巴西」）当成仍然有效写进本轮 SQL —— 实测踩过：
            #   t3「那有多少位员工？」的 SQL 仍带 billing_country='Brazil' 答出 0。
            #   跨轮上下文由 requirements（含继承条）与 followup_context 传递，
            #   这里改写仅针对本轮原话（与清单的口径一致，见 _extract_requirements_for）。
            turn_norm = normalize(original_question, time_anchor=time_anchor_from_env())
            turn_question = turn_norm.normalized.strip() or original_question
            runs = self._execute(
                turn_question, plan,
                followup_context=(followup_instruction.as_payload() if followup_instruction else None),
                retrieval_mode=retrieval_mode,
                requirements=requirements,
            )

        # ── 合并：这是编排层真正独有的活 ─────────────────────────────
        emit_progress("assemble", "关联证据并整理回答", "started")
        # 把所有工具各自产生的步骤拼成一条总链；merge_trace 会重排 step_id，
        # 但**保留每步的 tool 字段** —— 前端才能分辨哪步属于哪个工具。
        # normalizer 那步排在最前（它是链路的第 1 步）。
        all_steps: list[TraceStep] = [norm_step, clarification_step]
        if followup_step is not None:
            all_steps.append(followup_step)
        all_refs = []
        for run in runs:
            all_steps.extend(run.result.trace)
            all_refs.extend(run.result.refs)

        merged_trace = merge_trace(all_steps)
        merged_refs = self._collect_final_refs(runs, merged_trace)
        emit_progress("assemble", "关联证据并整理回答", "completed")

        # Build the request-scoped fact ledger for every supported route. The
        # HTTP layer negotiates only whether this projection is serialized;
        # opting out of the card must not opt out of the verification path.
        fact_review = None
        from app.fact_review import build_execution_fact_review, build_fact_review

        if plan.fact_plan is not None and fact_run is not None:
            fact_review = build_fact_review(plan.fact_plan, fact_run, merged_refs)
        elif plan.review_fact_plan is not None and plan.review_coverage is not None:
            fact_review = build_execution_fact_review(
                plan.review_coverage,
                plan.review_fact_plan,
                runs,
                merged_refs,
            )

        tool_clarification = self._tool_clarification(runs)
        if tool_clarification:
            clarify_text = tool_clarification
            self.session_memory.begin_clarification(
                session_id,
                question,
                ("user_supplied_context",),
            )
            return AskResponse(
                answer=clarify_text,
                trace=merged_trace,
                refs=merged_refs,
                sql=self._extract_sql(runs),
                need_clarify=True,
                suggestions=self._suggestions(question, runs, merged_refs, clarified=True),
                context_continuation=continuation,
                fact_review=fact_review,
            )

        if self._execution_succeeded(runs):
            # 存什么：**用户原话链**，不是改写文本。
            #
            # 反例一（对象丢失）：只存本轮原话时，「改成 2026-10-15 呢？」这类
            #   残句会把对象「采购公告」从历史里挤掉，下一轮再也找不回来。
            # 反例二（改写复利）：存改写文本时，`normalize` 每轮都会把上一轮
            #   的 `announcements` 再改一次 —— 实测三轮后变成
            #   `announcementsss`，对象词彻底失效，且时间条件被错拼成
            #   单日 `2026-10-16`，与上一轮区间冲突。
            #
            # 所以：拿**本轮拼接前的历史原文**（previous_history）+ 本轮原话，
            # 用同一个接缝拼起来。两条都不会发生：对象留在链里、实体词始终原始。
            #
            # ⚠ 追问轮的上一轮**没有成功过**，`previous_history`
            #   （= `last_successful_question`）是空的，`_history_question`
            #   于是退回只存本轮原话（「2022 年」）。历史一存就只剩一句残片，
            #   下一轮「改成 2021 年」再也找不到「销售额增长率」，
            #   被当成没有业务对象的新问题反过来追问（实测 T3-STEP-03 t3）。
            #   追问恢复的这一轮，上一轮的语境在 `pending_origin` 里，用它续接。
            history_text = (
                self._pending_chain(pending_origin, original_question)
                if session_action == "pending_clarification_resumed" and pending_origin
                else self._history_question(
                    session_action, prepared_question,
                    previous_history, original_question)
            )
            self.session_memory.save_success(
                session_id,
                history_text,
                query_state=self._query_state_from_runs(runs),
            )
            self._save_condition_archive(
                session_id, archive_before, requirements, norm, is_followup_turn,
                pending_archive=pending_archive,
            )

        result_set_info = self._create_result_set(runs, session_id=session_id)
        answer = self._compose(question, runs)
        answer += self._coverage_note(runs)
        if result_set_info is not None:
            if result_set_info.complete:
                answer += " 完整结果可在下方分页查看，也可下载 CSV。"
            else:
                answer += " 下方是当前可取得的部分结果，不能视为完整清单；未提供完整 CSV。"
        elif any(
            isinstance(run.result.data, dict)
            and isinstance(run.result.data.get("rows"), list)
            and len(run.result.data["rows"]) > self.result_set_store.page_size
            for run in runs
        ):
            answer += " 大结果暂未能安全暂存，因此不能提供分页或完整导出。"

        return AskResponse(
            answer=answer,
            trace=merged_trace,
            refs=merged_refs,
            sql=self._extract_sql(runs),
            need_clarify=False,
            result_set=result_set_info,
            suggestions=self._suggestions(
                question, runs, merged_refs, clarified=False
            ),
            data_source=self._data_source(runs),
            context_continuation=continuation,
            fact_review=fact_review,
        )

    def _create_result_set(
        self,
        runs: list[ToolRun],
        *,
        session_id: str,
    ) -> ResultSetInfo | None:
        """Cache large SQL results without placing row payloads in the chat trace."""
        for run in runs:
            data = run.result.data
            if not isinstance(data, dict):
                continue
            rows = data.get("rows")
            columns = data.get("columns")
            if not isinstance(rows, list) or not isinstance(columns, list):
                continue
            if len(rows) <= self.result_set_store.page_size:
                continue
            return self.result_set_store.put(
                columns,
                rows,
                complete=not bool(data.get("possible_truncation")),
                session_id=session_id,
            )
        return None

    @staticmethod
    def _suggestions(
        question: str,
        runs: list[ToolRun],
        refs: list,
        *,
        clarified: bool = False,
    ) -> list[Suggestion]:
        """从本轮已经算出的事实里组合追问建议。

        ⚠️ **整段包在 try/except 里，且失败只返回空列表**。
        建议是附加功能：它坏了不能影响答案返回 —— 用户拿到答案才是主线，
        少几条推荐无所谓。所以这里绝不允许异常冒泡到 `answer()`。
        """
        try:
            from app.suggestions import from_trace_and_rows

            columns: list[str] = []
            rows: list[dict] = []
            for run in runs:
                data = run.result.data
                if not isinstance(data, dict):
                    continue
                raw_rows = data.get("rows")
                if not isinstance(raw_rows, list) or not raw_rows:
                    continue
                raw_cols = data.get("columns")
                if isinstance(raw_cols, list):
                    columns = [str(c) for c in raw_cols]
                elif isinstance(raw_rows[0], dict):
                    columns = [str(k) for k in raw_rows[0]]
                rows = [r for r in raw_rows if isinstance(r, dict)]
                break

            steps = [step for run in runs for step in run.result.trace]
            return from_trace_and_rows(
                question=question,
                trace=steps,
                refs=list(refs),
                rows=rows,
                columns=columns,
                clarified=clarified,
            )
        except Exception:  # noqa: BLE001 - 见 docstring：建议失败不能影响答案
            return []

    @staticmethod
    def _tool_clarification(runs: list[ToolRun]) -> str:
        for run in runs:
            data = run.result.data
            if not isinstance(data, dict) or not data.get("need_clarify"):
                continue
            question = data.get("clarify_question") or data.get("clarification") or ""
            if isinstance(question, str) and question.strip():
                return question.strip()
        return ""

    @staticmethod
    def _coverage_note(runs: list[ToolRun]) -> str:
        """P1(c) 裁定的第三处留痕：把未落实/证不出的要求写进回答正文。

        裁定原文：warn 档下 unknown「放行 + 核验卡标黄」—— 但核验卡要展开
        才看得见，导出 CSV 或流式纯文本场景里就没有了。回答正文是唯一
        **无处不在**的出口。SC-12 的教训：答案写「0」却不说「至今」没落实，
        用户拿到一个碰巧数字。

        **只对 warn/enforce 档生效**（off 档根本没跑核对，编不出说明）；
        repair 在 warn 档会重生成/拦下，能落到回答里的主要是 unknown 与
        enforce 档的残余 —— 判据统一取 repair/unknown 两类。
        """
        gate = (os.environ.get("NL2SQL_REQUIREMENT_GATE") or "warn").strip().lower()
        if gate == "off":
            return ""
        notes: list[str] = []
        seen: set[str] = set()
        for run in runs:
            data = run.result.data
            checks: list[dict] = []
            if isinstance(data, dict):
                coverage = data.get("requirement_coverage")
                if isinstance(coverage, dict):
                    checks = [
                        c for c in coverage.get("checks", [])
                        if isinstance(c, dict)
                    ]
            if not checks:
                # 兜底：trace 步骤里找（mock 链路或旧结构）。
                for step in run.result.trace:
                    if step.tool != "sql_requirement_validator":
                        continue
                    output = step.output if isinstance(step.output, dict) else {}
                    checks = [
                        c for c in output.get("checks", [])
                        if isinstance(c, dict)
                    ]
            for check in checks:
                if check.get("outcome") not in {"repair", "unknown"}:
                    continue
                message = (check.get("message") or "").strip()
                if not message:
                    continue
                key = message
                if key in seen:
                    continue
                seen.add(key)
                # 用户的原词优先呈现：message 是模板话术，surface 才是
                # 「今年」「至今」这类词 —— 用户扫一眼就知道说的是哪个条件。
                surface = (check.get("surface") or "").strip()
                # 模板话术自带句号，拼接处剥掉一个，避免「。。」叠句。
                notes.append(
                    f"「{surface}」{message.rstrip('。')}" if surface and surface not in message else message.rstrip("。")
                )
        if not notes:
            return ""
        head = "其中说明：" if len(notes) == 1 else "其中说明（共 {0} 项条件未完全落实或未能证实）：".format(len(notes))
        return " " + head + "；".join(notes[:3]) + ("；等 {0} 项".format(len(notes)) if len(notes) > 3 else "") + "。"

    @staticmethod
    def _execution_succeeded(runs: list[ToolRun]) -> bool:
        if not runs:
            return False
        executed = False
        for run in runs:
            data = run.result.data
            if isinstance(data, dict) and data.get("error"):
                return False
            if any(step.tool not in {"normalizer", "semantic_clarifier"}
                   for step in run.result.trace):
                executed = True
        return executed

    def _save_condition_archive(
        self,
        session_id: str,
        archive_before,
        requirements: QueryRequirements,
        norm: NormalizeResult,
        is_followup_turn: bool,
        pending_archive=None,
    ) -> None:
        """回答成功后把本轮清单并入会话条件档案（F0/F3）。

        **只在真的答完之后写**。澄清轮、执行失败的轮次都不写 —— 那些轮
        用户的要求并没有被落实，把它们记成"已生效条件"会让下一轮继承一个
        从未成立的前提。

        接续轮在回答**之前**已经做过同一次合并（`pending_archive`，为的是
        让继承条吃上本轮的 stale 判定），这里直接落库那份档案，不再合并
        第二次 —— 两次合并会对同一份 incoming 重复记账（added 条目翻倍）。

        合并失败（`degraded=True`）时仍然写入：档案里会标 `unknown` 操作且
        **旧条件一条不丢**，让下一轮至少不会因为合并失败而丢上下文。
        """
        if pending_archive is not None:
            self.session_memory.save_condition_archive(session_id, pending_archive)
            return
        try:
            archive = merge_condition_archive(
                archive_before,
                requirements,
                question=norm.original or requirements.original,
                is_followup=is_followup_turn,
            )
        except Exception:
            # 合并本身出错不能把已经答完的这一轮变成失败。
            # 退化为「本轮清单自成一份档案」，下一轮的继承少一点，但不崩。
            return
        self.session_memory.save_condition_archive(session_id, archive)

    #: `SessionMemory._combine` 生成的接缝（两处必须一致，见 tests/test_mainline_clarifier_memory.py）。
    _HISTORY_SEAM = "用户补充或修改："

    @classmethod
    def _history_question(cls, session_action: str, prepared_question: str,
                          previous_history: str | None, original_question: str) -> str:
        """生成本轮要存进会话历史的文本 —— **始终是用户原话链**。

        为什么不直接存 `prepared_question`（拼接链）：它是 `_combine` 的产物，
        但拼接链上游已经把每轮原话送进过 `normalize`，下一轮再拼就带着
        改写结果跑第二遍归一化 —— 实测 `announcements` 三轮后变成
        `announcementsss`（`_pass_alias` 认不出已知英文表名，又追加了一次复数形），
        时间条件也被错拼成单日。

        为什么不直接存 `original_question`：残句追问（「改成 2026-10-15 呢？」
        「其中招标公告有几条？」）本身没有业务对象，只存它会把对象挤掉。

        折中：`previous_history`（上一轮存的那条原话链，可能为 None）+
        本轮原话，用同一个接缝拼。两者都是用户手打的文字，不含任何改写产物。
        """
        if not previous_history:
            return original_question
        if session_action not in ("short_followup_attached",
                                  "pending_clarification_resumed"):
            return original_question
        return (previous_history.strip() + "\n" + cls._HISTORY_SEAM
                + original_question.strip())[:8000]

    @classmethod
    def _pending_chain(cls, pending_origin: str | None, original_question: str) -> str:
        """待澄清时存进会话的「原问题链」—— 下一轮 `pending_prompt` 就取它。

        与 `_history_question` 同源（都是原话 + `_HISTORY_SEAM`），但**接的是待澄清
        链本身，不是 `previous_history`**。

        ⚠ 为什么不能用 `previous_history`（= `last_question`，即 `last_successful_question`）：
        追问轮**根本走不到 `save_success`**，那个字段从头到尾是空串。旧写法因此
        一路退化成只存本轮的纯补充片段（「2023 年」「去年」），原意图整句丢失，
        下一轮澄清器判不出 measure / object，反过来追问「要比哪个指标」、
        「要统计哪个业务对象」—— `pending_origin` 明明是空的，`applied=False` 就是这么来的。

        正确链条：本轮开头的 `pending_prompt()`（上一轮的待澄清原话链）+
        本轮原话，两者都是用户真说过的字，接缝用同一个 `_HISTORY_SEAM`。
        这样三轮里的 `pending_origin` 依次是
        「销售额增长率是多少？」→「销售额增长率是多少？\\n用户补充或修改：2023 年」
        →（再补 2022 年那一轮开头就能看到两期），原意图一直在链里。
        """
        current = (original_question or "").strip()
        if not pending_origin:
            return current
        return (pending_origin.strip() + "\n" + cls._HISTORY_SEAM + current)[:8000]

    @staticmethod
    def _query_state_from_runs(runs: list[ToolRun]) -> dict[str, Any] | None:
        """Retain bounded SQL metadata needed to interpret the next turn."""
        for run in reversed(runs):
            data = run.result.data
            if not isinstance(data, dict):
                continue
            sql = data.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                continue
            columns = data.get("columns")
            row_count = data.get("row_count")
            rows = data.get("rows")
            state: dict[str, Any] = {
                "sql": sql,
                "columns": list(columns) if isinstance(columns, list) else [],
                "row_count": row_count if isinstance(row_count, int) else None,
            }
            # 首行留一份（有界）：下一轮的「他/这家/那家」指的就是这行。
            # 2026-10-18 gmt-03 第 2 轮实测：第 1 轮答出 `buyer_name：清华大学`，
            # 第 2 轮「他的项目一共花了多少钱？」生成 `SUM(budget_yuan)` 却**没有任何
            # 采购人筛选条件** —— 因为它只知道「上一轮问了采购人」，不知道选中的是谁。
            # 条件档案记的是「用户说过什么」，而选中项来自**查询结果**，两者不同源。
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                state["first_row"] = {
                    str(k)[:64]: _bounded_cell(v) for k, v in list(rows[0].items())[:16]
                }
            return state
        return None

    @staticmethod
    def _data_source(runs: list[ToolRun]) -> DataSource | None:
        """本轮问答实际读的是哪一份数据 —— 库，或语料库。

        **两种来源都要标**：
          - 结构化链路 → 哪个库。界面上只显示 `customer` 这样的表名时，用户分不清
            答案来自哪一份数据；库里同时跑着 Chinook 与 AdventureWorks 两个示例库，
            不标来源就会让人以为是同一套。
          - 文档链路 → 哪一批文档。只显示文档标题同样说不清"这是哪一批材料"。

        答辩时「你这套系统读的是什么数据」几乎是必问的，**主动标明比被问出来强**
        （口径见 `app/contracts.py::DataSource`）。

        取不到就返回 None，前端据此整行不渲染 —— **不回落成"未知来源"之类的假值**。
        """
        for run in runs:
            data = run.result.data
            if not isinstance(data, dict):
                continue
            # 结构化：只有真生成出 SQL 才谈"哪个库"。
            # data["catalog"] 是工具侧给的可序列化 dict（data_source() 的产物），
            # 不是 SchemaCatalog 对象 —— ToolResult 要整只 JSON 序列化。
            if data.get("sql"):
                source = data.get("catalog")
                if isinstance(source, dict) and source:
                    return DataSource(**source)
                continue
            # 文档：只有真召回了语料才谈"哪一批文档"。
            if data.get("text"):
                from app.rag.corpus import corpus_data_source

                source = corpus_data_source(data.get("corpus_index"))
                if source is not None:
                    return source
        return None

    # ── 内部：执行计划 ───────────────────────────────────────────────
    def _execute(
        self, question: str, plan: RoutePlan,
        *, followup_context: dict[str, Any] | None = None,
        retrieval_mode: str = "standard",
        requirements: QueryRequirements | None = None,
    ) -> list[ToolRun]:
        """按计划顺序调工具。前一步查到的实体名会作为线索传给后一步。"""
        runs: list[ToolRun] = []
        carried_hint = ""
        carried_facts = ""

        for step_index, (tool_name, base_hint) in enumerate(plan.steps, start=1):
            tool = self.registry.get(tool_name)
            if tool is None:
                continue
            stage_id = f"tool_{tool_name}_{step_index}"
            label = {
                "nl2sql": "查询结构化数据",
                "doc_search": "检索并核对文档",
                "graph_reason": "补充关系处理",
                "cross_source_demo": "核对跨源事实",
            }.get(tool_name, "执行信息检索")
            emit_progress(stage_id, label, "started")
            hint = base_hint or carried_hint
            payload = {"question": question, "hint": hint}
            if tool_name == "doc_search":
                payload["retrieval_mode"] = retrieval_mode
                # 跨源链路里 nl2sql 先跑，它算出的结论必须交给文档侧，
                # 否则文档侧生成器不知道数据部分已经有答案，会如实回答
                # 「文档中没有这个数据」，两段拼起来自相矛盾。
                # 实测：gov-x-A01 的答案是「count_question：4。；根据现有资料
                # 无法回答…因为文档中没有中央预算的信息」——数据算对了、
                # 法规也检索对了，只有拼接是错的。
                if carried_facts:
                    payload["known_facts"] = carried_facts
                    # 只把「已知事实」放进提示词还不够：实测 glm-4-flash 收到
                    # 完整的 <已知事实> 段后，仍然在答案开头写「根据现有资料
                    # 无法回答…有几个的问题」，再接上法规——它把原题当整体，
                    # 先逐项核对了一遍。真正有效的改法是**不让它看到原题**：
                    # 数据那半已经答完了，交给文档侧生成的就该只是规定那半。
                    #
                    # 注意用 answer_question 而不是改 question：检索必须仍用
                    # 原问句——法规的召回靠原题里的法规措辞，换成子问会掉召回。
                    doc_question = self._doc_only_question(question)
                    if doc_question:
                        payload["answer_question"] = doc_question
            if tool_name == "nl2sql" and followup_context:
                payload["followup_context"] = followup_context
            if tool_name == "nl2sql" and requirements is not None:
                # 要求清单由本层用**用户原话**抽好（见 `_extract_requirements_for`），
                # 随工具入参传下去。pipeline 里不再抽第二次 —— 它会拿到改写后的
                # 问句，span 对不上原文，对外解释就失真。
                payload["requirements"] = requirements
            if plan.query_intent is not None:
                payload["query_intent"] = {
                    "objects": list(plan.query_intent.objects),
                    "measures": list(plan.query_intent.measures),
                    "time_expressions": list(plan.query_intent.time_expressions),
                    "dimensions": list(plan.query_intent.dimensions),
                    "filters": list(plan.query_intent.filters),
                    "target_grain": plan.query_intent.target_grain,
                    "source_question": plan.query_intent.source_question,
                }

            t0 = time.time()
            try:
                result = tool.run(payload)
            except Exception:
                emit_progress(stage_id, label, "failed")
                raise
            elapsed = (time.time() - t0) * 1000.0
            emit_progress(stage_id, label, "completed", elapsed_ms=elapsed)

            # 把这一步查到的实体名留给下一步用 —— 这就是「链」的雏形。
            # 现在只有单工具，所以用不上；跨源多工具接进来时这里是关键接缝。
            picked = self._pick_entity(result)
            if picked:
                carried_hint = picked
            # 结构化查询的结论要整体留给文档侧（不是只挑一个实体名）。
            # 与 carried_hint 分开存：hint 是检索线索，facts 是已经确定的事实，
            # 两者在提示词里位置和作用都不同。
            facts = self._carry_facts(result)
            if facts:
                carried_facts = facts

            runs.append(
                ToolRun(
                    tool_name=tool_name,
                    reason=plan.reason,
                    result=result,
                    elapsed_ms=elapsed,
                )
            )
        return runs

    @staticmethod
    def _collect_final_refs(runs: list[ToolRun], merged_trace: list[TraceStep]) -> list:
        """决定最终写进 `AskResponse.refs` 的是哪些引用。

        ⚠️ 这里是本文件最容易被写错的一处，改动前请看完下面这段。

        **为什么不能无脑用 `collect_refs(trace)`**（B 在 09-21 意见里指出）：
            `TraceStep.hit` 的语义是「**这一步命中了什么**」，不是「最终引用了什么」。
            以 RAG 工具为例（`app/rag/answer.py`），它的三步是：
                第 1 步 retrieve —— hit 挂**全量召回**（RETRIEVE_TOP_K 条）
                第 2 步 rerank   —— hit 同样挂全量召回
                第 3 步 generate —— hit 挂重排后的 top_k 条
            于是 `collect_refs` 会把第 1、2 步的全量召回也汇总进来，
            得到的是「召回了几条」而不是「答案引用了几条」。
            而工具自己在 `refs` 里用 `_parse_citations` 做过一次过滤
            （只保留答案里真的标了 [n] 的那几条）—— 那份过滤会被丢掉。
            后果：引用卡片列出 N 条召回片段，而不是答案真正依据的那几条。

        **判定顺序**：
            ① 工具返回的 `refs` 非空 → 用它。
               理由：同一工具内部最清楚「哪些是最终引用」——RAG 的引用是
               模型真的标注过的、nl2sql 的是真的查到的那行。
               **这不破坏契约不变量**：工具的 refs 本身就是它自己 trace.hit 的子集，
               逐条 `snippet` 非空、可追溯，只是比 trace 汇总更窄更准。
            ② 所有工具的 `refs` 都为空 → 回退 `collect_refs(trace)`。
               理由：兜底。宁可给出「宽一点但不错」的引用，也不让答案变成无引用
               （评测口径规定「没有引用的答案不算通过」）。
        """
        tool_refs = [ref for run in runs for ref in run.result.refs]
        ledger = ledger_for_response(tool_refs, merged_trace)
        return ledger.response_refs()

    # ── 内部：跨源问句切成「只管规定那半」 ───────────────────────────
    @staticmethod
    def _doc_only_question(question: str) -> str:
        """从跨源问句里切出「只问规定/依据」的子问，切不出返回空串。

        只在前一步已经产出 `known_facts`（即数据那半已被 nl2sql 答完）时才会被调用。

        为什么必须切（2026-10-17 实测）：把完整的跨源原题交给文档侧生成器，
        即使提示词里已经写明「数据部分已由查询答完，不要声明资料中没有该数据」，
        glm-4-flash 仍然会在答案开头写「根据现有资料无法回答…有几个的问题，
        因为提供的文档片段中没有提及…」，然后再接上正确的法规回答。
        它把原题当成一个整体逐项核对，数据项文档答不了就声明一句。
        这不是措辞不够强的问题——`<已知事实>` 段已确认完整进了提示词。
        所以改成**不让它看到原题**：交给文档侧的只有规定那半。

        切分规则（刻意保守，宁可切不出也不切错）：
        1. 逐个扫描问号，取**第一个**其后面紧跟白名单衔接词的问号作为切点；
        2. 切出的子问至少要 `_DOC_QUESTION_MIN_CHARS` 个字符。

        为什么是「第一个接衔接词的问号」而不是「最后一个问号」：
        跨源题的两半都可能带问号。实测 gov-x-A01 的写法是
        「…项目有几个？同时请结合法规说明，采用单一来源方式在程序上有什么
        前置要求？」——末尾那个问号属于**子问自身**，取最后一个问号会得到空串。
        真正的切点标志是「问号后面开始一个新话题」，也就是衔接词。

        实测 15 道政采跨源题全部满足（第二段一律以「同时」开头），
        但这里不写死只认「同时」——真实用户的写法更散，白名单宁宽勿窄，
        真正的保护来自「必须接衔接词」与「切不出就退回原题」。
        """
        text = (question or "").strip()
        if not text:
            return ""
        for index, char in enumerate(text):
            if char not in "？?":
                continue
            tail = text[index + 1:].strip()
            if len(tail) < _DOC_QUESTION_MIN_CHARS:
                continue
            if not tail.startswith(_DOC_QUESTION_LEADS):
                continue
            return tail
        return ""

    @staticmethod
    def _carry_facts(result: ToolResult) -> str:
        """把结构化查询算出的结论整理成一段可直接交给文档侧的事实文本。

        与 `_pick_entity` 的分工：那个挑**一个实体名**当检索线索，这个交出
        **整段结论**当已知事实。跨源问句里两者都需要 —— 文档检索要靠实体名
        命中正确的法规，而生成要靠结论知道「数据部分已经答完了」。

        取 `conclusion` 而不是自己拼 rows：conclusion 是 nl2sql 侧按题面口径
        算好的自然语言结论（如「查询结果：count：4。」），自己拼行数据容易
        在有多列/多行时写出与题面不符的表述。

        非结构化工具的产出（纯字符串）返回空串 —— 文档不好给文档当事实用。
        """
        data = result.data
        if not isinstance(data, dict):
            return ""
        conclusion = data.get("conclusion")
        if not isinstance(conclusion, str) or not conclusion.strip():
            return ""
        # 行数提示要带上：多行结果只说「共 17 行」而不给内容，文档侧会误以为
        # 数据没答出来（实测 A02 就是这样，实际上 SQL 已经答完了，只是行多）。
        row_count = data.get("row_count")
        text = conclusion.strip()
        if isinstance(row_count, int) and row_count > 1:
            text += "（共 " + str(row_count) + " 行结果）"
        return text

    @staticmethod
    def _pick_entity(result: ToolResult) -> str:
        """从工具产出里挑一个能被下一步当线索用的实体名。挑不到返回空串。"""
        data = result.data
        # 纯字符串形态（生成式 RAG 的产出）里挑不出结构化实体，直接放弃
        if isinstance(data, str):
            return ""
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            for key in ("name", "row_pk"):
                value = data[0].get(key)
                if isinstance(value, str) and value:
                    return value
        if isinstance(data, dict):
            for key in ("name", "start_node"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    @staticmethod
    def _extract_sql(runs: list[ToolRun]) -> str | None:
        """把结构化查询用过的 SQL 取出来填进响应。

        ⚠️ **这段曾经是坏的，2026-10-01 修**。原实现写
        `getattr(run.result, "sql", None)`，但 `run.result` 是 **ToolResult**
        （契约里 `extra="forbid"`，只有 data / trace / refs 三个字段），
        它身上永远没有 `sql` 属性 → `AskResponse.sql` 恒为 None。
        代价不只是"字段空着"：`eval/runner.py` 的 HTTP 预测器正是从响应体的
        `sql` 字段取预测 SQL 的，字段恒空等于**真实链路根本没法被评测脚本打分**。

        取值顺序（都不动契约）：
            ① 从 trace 里**倒着**找最后一个带 `sql` 的步骤输出。工具把「生成的 SQL」
               与「实际执行的 SQL」都记在步骤 output 里 —— 这正是可解释性的单一事实
               来源；倒着找保证拿到最后那条（真正执行的），而不是中途被否决的草稿。
            ② 兼容形态：`result.data` 是带 `sql` 键的字典时也认。
        """
        for run in runs:
            for step in reversed(run.result.trace or []):
                value = (step.output or {}).get("sql")
                if isinstance(value, str) and value.strip():
                    return value
            data = run.result.data
            if isinstance(data, dict):
                value = data.get("sql")
                if isinstance(value, str) and value.strip():
                    return value
        return None

    @staticmethod
    def _fact_plan_answer(plan: FactPlan, fact_run) -> str:
        """把私有事实计划的最终记录投影为兼容的纯文本答案。"""
        final_task_id = "sla_verdict" if "sla_verdict" in plan.tasks else None
        if final_task_id is not None:
            final_record = fact_run.records.get(final_task_id)
            if final_record is not None and final_record.state == "supported":
                if isinstance(final_record.value, str):
                    return final_record.value

        # Preserve facts established before a safe stop. The cross-source
        # questions explicitly ask for opened_at first; returning only the
        # blocking reason hid a supported fact.
        #
        # ⚠️ 这里曾经直接拼 `f"opened_at={opened_at}；"` —— 把内部字段名当成了
        # 给用户看的文案，于是每个早停回答都以「opened_at=2026-09-18T09:00:00+08:00；」
        # 开头。那是 trace 里的键值对泄漏进答案，不是回答。改成中文陈述句，
        # 并把 ISO 时间里的 T 去掉（保留时区后缀，它是事实的一部分）。
        supported_prefix = ""
        work_record = fact_run.records.get("work_order_facts")
        if work_record is not None and work_record.state == "supported":
            value = work_record.value if isinstance(work_record.value, dict) else {}
            opened_at = value.get("opened_at")
            if opened_at:
                supported_prefix = f"工单受理时间 {_readable_time(opened_at)}；"

        stop_reason_text = {
            "customer_scope_conflict": "设备客户与合同客户不一致，无法确认合同适用范围。",
            "mapping_basis_unapproved": "映射依据未被当前业务规则确认受控，无法确认合同关系。",
            "validity_window_not_cover": "映射有效期或合同有效期未覆盖工单日期。",
            "ambiguous_active_contracts": "存在多个有效合同映射，无法唯一确定适用合同。",
            "contract_status_inactive": "候选合同状态不是有效状态（如暂停或失效），不能据此适用合同修复时限。",
            "mapping_unresolved": "设备与合同的受控映射未能建立。",
            "event_time_reversed": "受理事件晚于服务恢复事件，时间顺序异常，无法计算修复耗时。",
            "key_event_missing": "缺少计算所需的关键事件，无法计算修复耗时。",
            "equipment_master_missing": "设备主数据缺失，无法确认设备与合同关系。",
        }
        diagnostic_text = {
            "document_retrieval_empty": "合同文档检索没有返回可用内容。",
            "document_reference_missing": "合同文档检索结果没有形成该合同的可定位原文引用。",
            "document_scope_excluded": "合同原文明确排除该设备。",
            "document_scope_unproven": "结构化映射已存在，但文档没有足够的适用范围证据。",
            "document_sla_ambiguous": "合同原文存在多个修复时限，当前无法确定适用条款。",
            "document_sla_unstated": "合同原文明确未约定修复完成总时限。",
            "document_sla_unparsed": "已检索到合同原文，但尚未可靠解析出修复完成时限。",
            "document_sla_override_unresolved": "合同原文含有特别或优先约定，当前无法可靠确定适用时限。",
            "document_sla_scope_unproven": "修复时限条款未能证明适用于当前设备。",
        }

        # A deterministic cross-source blocker outranks a later document
        # diagnostic.  For example, reversed event timestamps and an
        # unstated SLA may both be observed, but the former is the primary
        # reason the requested duration cannot be computed.  Otherwise the
        # final answer hides a material data-quality failure even though it is
        # present in the alignment trace.
        if fact_run.stop_reason_code in stop_reason_text:
            explanation = stop_reason_text[fact_run.stop_reason_code]
            if fact_run.stop_reason_code == "ambiguous_active_contracts":
                mapping_record = fact_run.records.get("controlled_contract_mapping")
                mapping_value = (
                    mapping_record.value if mapping_record is not None else None
                )
                candidates = (
                    mapping_value.get("candidate_contracts", [])
                    if isinstance(mapping_value, dict) else []
                )
                if isinstance(candidates, list) and candidates:
                    candidate_details = []
                    for candidate in candidates:
                        if not isinstance(candidate, dict):
                            continue
                        contract_id = str(candidate.get("contract_id") or "候选合同")
                        basis = str(candidate.get("mapping_basis") or "依据缺失")
                        mapping_period = (
                            f"{candidate.get('mapping_valid_from')}至{candidate.get('mapping_valid_to')}"
                        )
                        contract_period = (
                            f"{candidate.get('contract_effective_from')}至{candidate.get('contract_effective_to')}"
                        )
                        candidate_details.append(
                            f"{contract_id}（映射依据 {basis}，映射有效期 {mapping_period}，"
                            f"合同有效期 {contract_period}，状态 {candidate.get('contract_status') or '缺失'}）"
                        )
                    if candidate_details:
                        explanation = (
                            "存在多个满足结构化适用条件的合同候选："
                            + "；".join(candidate_details)
                            + "。因此无法唯一确定适用合同；以上候选均有结构化记录支持，不选取其中一份。"
                        )
            if fact_run.stop_reason_code == "mapping_basis_unapproved":
                policy_step = next((
                    step for step in fact_run.trace
                    if step.tool == "cross_source_policy_check"
                ), None)
                if policy_step is not None:
                    output = policy_step.output
                    observations = output.get("observations", [])
                    bases = [
                        str(item.get("mapping_basis"))
                        for item in observations
                        if isinstance(item, dict) and item.get("mapping_basis")
                    ]
                    policy_label = (
                        f"{output.get('policy_id') or policy_step.input.get('policy_id')} "
                        f"v{output.get('version') or policy_step.input.get('version')}"
                    )
                    if bases:
                        explanation = (
                            f"映射依据 {', '.join(bases)} 未列入受控策略 {policy_label} 的允许清单，"
                            "无法确认合同关系。"
                        )
            return supported_prefix + "无法判断：" + explanation

        for task_id in plan.validate():
            record = fact_run.records.get(task_id)
            if record is None or record.state == "supported":
                continue
            if (record.diagnostic_code == "document_sla_override_unresolved"
                    and isinstance(record.value, dict)):
                signed_limit = record.value.get("signed_limit_hours")
                draft_limits = record.value.get("unsigned_draft_limit_hours", [])
                if signed_limit is not None and draft_limits:
                    draft_text = "、".join(str(value) for value in draft_limits)
                    return (
                        supported_prefix
                        + f"无法判断：签署正文约定修复时限 {signed_limit} 小时，未签署补充稿写 {draft_text} 小时；"
                        "档案未消解冲突，不能将未签稿视为已生效变更。"
                    )
            if record.diagnostic_code in diagnostic_text:
                return supported_prefix + "无法判断：" + diagnostic_text[record.diagnostic_code]
            if isinstance(record.value, str) and record.value.strip():
                return supported_prefix + "无法判断：" + record.value
            reason = fact_run.skipped.get(task_id)
            if reason:
                return supported_prefix + "无法判断：" + reason
        return supported_prefix + "无法判断：事实计划没有形成全部受证据支持的结论。"

    # ── 内部：把结果拼成一句人话 ─────────────────────────────────────
    @staticmethod
    def _compose(question: str, runs: list[ToolRun]) -> str:
        """把各工具的产出拼成最终答案。

        `ToolResult.data` 在契约里是 `Any`，所以这里有责任把**各种自然形态**都接住。
        ⚠️ 踩过的坑（B 在 09-21 意见里指出）：最初只认 `list[dict]` 和 `dict`，
           而生成式 RAG 的产出就是一段**纯字符串**。结果是链路通、trace 对、
           RAG 答案本身也对，**只有拼装这一步把答案丢了**，用户看到
           「已执行查询，但没有取到可用的结果。」——最坏的一类失败：不报错，但答案是错的。

        当前支持四种形态（按判定顺序）：
            1. `dict` 且带 `conclusion` → 直接用（图谱工具已做完串联和概括）
            2. `str`                    → 直接当一段文本（生成式 RAG 的自然产出）
            3. `list[dict]`             → 取首行的值对
            4. `list[str]`              → 拼接
        """
        if len(runs) == 0:
            return "没有可用工具，无法回答这个问题。"

        # 图谱工具自带结论，优先级最高（它已经把多跳串完了）
        for run in runs:
            if run.tool_name == "graph_reason" and isinstance(run.result.data, dict):
                conclusion = run.result.data.get("conclusion")
                if isinstance(conclusion, str) and conclusion:
                    return conclusion

        parts: list[str] = []
        for run in runs:
            text = _compose_one(run.result.data)
            if text:
                parts.append(text)
        if len(parts) == 0:
            return "已执行查询，但没有取到可用的结果。"
        text = "；".join(parts)
        # 只在结尾没有标点时才补句号。生成式答案（真 RAG 返回的 str）自己带「。」，
        # 无条件追加会拼出「计算。。」——这个只在接入真 RAG 之后才暴露，
        # 因为 mock 工具返回的是 list[dict]，拼出来本就没有结尾标点。
        if not text.endswith(("。", "！", "？", "；", ".", "!", "?", "…")):
            text += "。"
        return text


# 跨源问句切成「只问规定那半」用的两个常量，见 `Orchestrator._doc_only_question`。
# 衔接词白名单宁宽勿窄：这里只是"看起来像第二问"的形状判据，真正的保护是
# 「问号后必须有内容」「子问不少于最小长度」「切不出就退回原题」三条兜底。
_DOC_QUESTION_LEADS: tuple[str, ...] = (
    "同时", "另外", "此外", "并", "以及", "再", "结合", "对照", "顺带",
)
_DOC_QUESTION_MIN_CHARS = 8


# ISO 8601 的「日期T时间」分隔符：只在严格贴日期与时间的 T 上生效，
# 避免把普通文本里的字母 T 也替换掉（见 `_readable_time` 的说明）。
_ISO_T_RE = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})")


def _readable_time(value: Any) -> str:
    """把 ISO 8601 时间转成给用户看的形态：去掉日期与时间之间的 `T`。

    **为什么需要它**：事实计划里 `opened_at` 的值形如
    `2026-09-18T09:00:00+08:00`。这个值直接拼进答案时会带出那个 `T`，
    而 `T` 是 ISO 8601 的分隔符，不是时间的一部分 —— 给人的观感就是
    「一串机器字段」。只去掉 `T`，**保留时区后缀**：时区是事实的一部分，
    跨时区场景下它决定了这个时间点到底指哪一刻。

    ⚠️ **必须按 ISO 日期时间形状匹配，不能无条件替换第一个 `T`**：
    实测 `"no-T-here"` 会被无脑替换成 `"no- -here"` —— 把一串正常文本
    改坏了。只有严格形如 `日期T时间` 的位置才该动。

    非字符串（None / 数字 / 字典）一律退化为 `str(value)`，不抛异常 ——
    这是文案加工，不该成为回答失败的原因。
    """
    text = value if isinstance(value, str) else str(value)
    return _ISO_T_RE.sub(r"\1 \2", text)


def _dedup_refs(refs: list) -> list:
    """给一批引用去重，保留首次出现的顺序。

    去重口径与 `app/contracts.collect_refs` **保持一致**（同一 (kind, 定位字段) 视为同一条；
    无行主键的 SQL 结果引用另用 result_id 区分），
    差别只在于：这里是「给一堆 refs 去重」，而 `collect_refs` 是「从 trace 里捞 refs 再去重」。

    为什么要去重：跨工具链路里，图谱工具和文档检索工具可能命中同一份文档的同一段，
    不去重的话引用卡片会出现两条一模一样的。
    """
    from app.contracts import DocRef, SqlRef

    seen: set[tuple[str, ...]] = set()
    out: list = []
    for ref in refs:
        if isinstance(ref, SqlRef):
            if ref.row_pk:
                key = ("sql", ref.table, ref.column or "", ref.row_pk)
            elif ref.result_id:
                key = ("sql_result", ref.table, ref.column or "", ref.result_id)
            else:
                key = ("sql", ref.table, ref.column or "", "")
        elif isinstance(ref, DocRef):
            if ref.char_start is not None or ref.char_end is not None:
                key = (
                    "doc",
                    ref.doc_id,
                    str(ref.char_start if ref.char_start is not None else ""),
                    str(ref.char_end if ref.char_end is not None else ""),
                )
            else:
                key = ("doc", ref.doc_id, str(ref.page or ""), ref.section_path or "")
        else:
            key = ("other", repr(ref))
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def _compose_one(data: Any) -> str:
    """把单个工具的产出转成一段可读文本。转不出来返回空串。

    单独抽成函数是为了让上面 `_compose` 的四种分支一眼看清，
    也便于将来加新形态（比如带分页的结果集）。
    """
    # 形态 1 / 2：最直接的两种
    if isinstance(data, str):
        # 生成式 RAG 的产出就是一段答案文本，直接用。
        # B 的 RagAnswerTool 返回的就是这个形态（app/rag/answer.py:153）。
        return data.strip()

    if isinstance(data, dict):
        for key in ("text", "conclusion", "answer"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value.strip()
        return ""

    if isinstance(data, list):
        if len(data) == 0:
            return ""
        if isinstance(data[0], dict):
            row = data[0]
            pairs = [str(k) + "：" + str(v) for k, v in row.items() if v is not None]
            return "、".join(pairs)
        if isinstance(data[0], str):
            return "；".join(str(item) for item in data if item)

    return ""


# ============================================================================
# 装配：把注册表和工具拼成一个能用的编排器
# ============================================================================


def build_default_orchestrator(
    graph_backend: str | None = None,
    doc_search_backend: str | None = None,
    nl2sql_backend: str | None = None,
    fact_plan_validator_mode: str | None = None,
) -> Orchestrator:
    """装配默认编排器。

    `nl2sql_backend` 决定结构化能力用哪一版：
        - `real`（默认）：真实链路 —— 大模型生成 SQL → sqlglot 校验 → 连库执行
          （见 app/nl2sql/ 与 app/tools/nl2sql.py）
        - `mock`：预置结果，只用于离线编排测试

    `graph_backend` 决定图谱能力用哪一版：
        - `mock`（默认）：用预置结果的假实现，验证期可离线跑
        - `kag`：接真实 KAG / OpenSPG（**09-24 验证通过后才有**）
        - `disabled`：不注册图谱工具；用于真实图谱尚未实现时的普通 SQL+RAG 对照链

    `doc_search_backend` 决定文档能力：
        - `rag`（默认）：真实语料级 RAG，首次文档查询时惰性建索引
        - `rag_multihop`：同上，但在检索外面加「查询分解 + 多轮检索 + 只生成一次」
          （见 app/rag/multihop.py；工具名与协议不变，编排层感知不到差别）
        - `mock`：预置结果，只用于离线编排测试

    两个开关走环境变量 `GRAPH_BACKEND` / `DOC_SEARCH_BACKEND`，不是改代码。

    ⚠️ 现在只有 mock 分支可用。`kag` 分支先留出结构，等验证期出结论再填，
       这样「编排层不等图谱」这条纪律在代码上是可见的。
    """
    if graph_backend is None:
        graph_backend = os.environ.get("GRAPH_BACKEND", "mock")
    if doc_search_backend is None:
        doc_search_backend = os.environ.get("DOC_SEARCH_BACKEND", "rag")
    if nl2sql_backend is None:
        nl2sql_backend = os.environ.get("NL2SQL_BACKEND", "real")
    graph_backend = graph_backend.lower()
    doc_search_backend = doc_search_backend.lower()
    nl2sql_backend = nl2sql_backend.lower()

    from app.tools.mock_graph_reason import MockGraphReasonTool
    from app.tools.nl2sql import build_nl2sql_tool

    registry = ToolRegistry()
    # 工具名恒为 nl2sql，与后端无关 —— 路由规则、前端历史记录、评测脚本都因此不用改。
    registry.register(build_nl2sql_tool(nl2sql_backend))
    if os.environ.get("CROSS_SOURCE_DEMO_ENABLED", "0").strip() == "1":
        from app.tools.cross_source_demo import CrossSourceDemoTool

        registry.register(CrossSourceDemoTool())

    fact_plan_executor = None
    if os.environ.get("CROSS_SOURCE_FACT_PLAN_ENABLED", "0").strip() == "1":
        if os.environ.get("CROSS_SOURCE_DEMO_ENABLED", "0").strip() != "1":
            raise ValueError(
                "CROSS_SOURCE_FACT_PLAN_ENABLED=1 需要同时显式启用隔离演示路由 "
                "CROSS_SOURCE_DEMO_ENABLED=1"
            )
        from app.cross_source_fact_plan import build_cross_source_fact_plan_executor

        fact_dsn = os.environ.get("CROSS_SOURCE_FACT_PLAN_DSN") or os.environ.get(
            "CROSS_SOURCE_DEMO_DSN", ""
        )
        validator_mode = (
            fact_plan_validator_mode
            if fact_plan_validator_mode is not None
            else os.environ.get("CROSS_SOURCE_FACT_PLAN_VALIDATOR_MODE", "strict")
        ).strip().lower()
        if validator_mode not in {"strict", "ablation"}:
            raise ValueError(
                "CROSS_SOURCE_FACT_PLAN_VALIDATOR_MODE 只能是 strict 或 ablation"
            )
        fact_plan_executor = build_cross_source_fact_plan_executor(
            fact_dsn,
            schema_path=os.environ.get("CROSS_SOURCE_FACT_PLAN_SCHEMA_PATH") or None,
            documents_dir=os.environ.get("CROSS_SOURCE_FACT_PLAN_DOCS_DIR") or None,
            db_schema=os.environ.get("CROSS_SOURCE_FACT_PLAN_DB_SCHEMA", "cross_source_demo"),
            verification_enabled=validator_mode == "strict",
        )
    if doc_search_backend == "rag":
        from app.tools.doc_search import RagDocSearchTool

        registry.register(RagDocSearchTool())
    elif doc_search_backend == "rag_multihop":
        from app.tools.doc_search_multihop import MultiHopDocSearchTool

        registry.register(MultiHopDocSearchTool())
    elif doc_search_backend == "mock":
        from app.tools.mock_doc_search import MockDocSearchTool

        registry.register(MockDocSearchTool())
    else:
        raise ValueError(
            "DOC_SEARCH_BACKEND 只能是 rag、rag_multihop 或 mock，当前值："
            + repr(doc_search_backend)
        )
    # “kag”目前仍是待实现的兼容占位；真实实验应显式使用 disabled，不能把 mock 当真实证据。
    if graph_backend in {"mock", "kag"}:
        # TODO(09-24 后): 换成 app.tools.kag_graph_reason.KagGraphReasonTool()
        # 验证期未通过前不进这个分支，避免把「跑不通」带进演示链路。
        registry.register(MockGraphReasonTool())
    elif graph_backend != "disabled":
        raise ValueError("GRAPH_BACKEND 只能是 mock、kag 或 disabled，当前值：" + repr(graph_backend))

    return Orchestrator(
        registry,
        graph_backend=graph_backend,
        fact_plan_executor=fact_plan_executor,
    )


__all__ = [
    "Orchestrator",
    "RoutePlan",
    "ToolRun",
    "build_default_orchestrator",
    "route",
]
