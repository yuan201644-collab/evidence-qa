"""nl2sql 工具的**真实**实现：自然语言 → SQL → 查库 → 回结果行。

**它与 `mock_nl2sql.py` 的关系**：同一个工具名（`nl2sql`）、同一套入参、同一个
`ToolResult` 三字段结构。编排层、路由规则、前端、评测脚本都感知不到换了实现 ——
这正是「统一工具协议」要达到的效果（`registry.py` 开头那段讲的就是这件事）。

**本文件是「注入点」，也是唯一一处跨链路耦合**：
    `app/nl2sql/` 包对非结构化链路保持零 import 依赖（见该包 docstring），
    它需要的大模型客户端由这里注入。注入时 import 了 `app/rag/llm.py` 的
    `OpenAILikeClient` —— 这是**一处、惰性**的耦合，而不是散在各处。
    ⚠️ 它仍然是耦合：B 若重命名/移动那个模块，这里会坏。
       更干净的做法是把 LLM 客户端抽到中立的 `app/llm.py`（两侧共用），
       已记入意见箱，**不擅自改 B 的文件**。
       因此这里的 import 包了 try/except，坏掉时给一句能直接照做的报错，
       而不是一个 `ModuleNotFoundError: app.rag.llm` 把人引到错的方向。
"""

from __future__ import annotations

import os
from typing import Any

from app.contracts import ToolResult
from app.tools.base import CAPABILITY_TABLE, BaseTool, ToolSpec

#: 工具名。必须与 mock 完全一致 —— 它会被写进每一条历史 TraceStep，改名等于改历史记录。
TOOL_NAME = "nl2sql"

#: 生成 SQL 时允许的最大输出 token 数。
#: 给 16000 而不是"够写一条 SQL 的量"：国产推理模型的**思维链计入该上限**，
#: 上限给小了的表现是"想一半就断"、返回空内容（实测结论，见项目 KAG 阶段四记录）。
#: 这是**上限**不是用量，给足不会额外花钱。
DEFAULT_MAX_TOKENS = 16000


class Nl2SqlTool(BaseTool):
    """结构化查询工具的真实实现。

    构造**不做任何重活**：不读元数据文件、不连库、不发请求，只记下参数。
    重活全部推迟到第一次 `run` —— 因为工具是在**服务启动时**注册的
    （`build_default_orchestrator`），启动阶段任何一次网络/文件失败都会让整个服务起不来。
    """

    def __init__(self, pipeline=None, *, max_attempts=None, max_refs=None):
        """
        Args:
            pipeline: 已经装配好的 `Nl2SqlPipeline`。测试传假件即可完全离线跑。
            max_attempts / max_refs: 只在本类自己造 pipeline 时生效（见 `_pipeline`）。
        """
        self._provided_pipeline = pipeline
        self._pipeline = None
        self._max_attempts = max_attempts
        self._max_refs = max_refs
        self._last_sql: str | None = None

    # ── 名片 ───────────────────────────────────────────────────────────
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=TOOL_NAME,
            capabilities=(CAPABILITY_TABLE,),
            description=(
                "查询业务数据库（Chinook：客户、发票、发票明细、曲目、专辑、艺术家、"
                "播放列表、员工等结构化表）。适用于需要**算出来**的问题：谁排第一、"
                "一共多少、按什么分组、合计/平均/最大最小、两个条件下的交集。"
                "注意：制度、流程、合同条款这类写在文档里的内容不属于本工具，"
                "要查文档请用文档检索工具。"
            ),
            input_schema={
                "question": "用户原话",
                "hint": "上游传下来的线索（如已经问到的某个人名），可空",
                "time_anchor": "时间基准日（YYYY-MM-DD），用于解释『今年/本月』这类相对时间，可空",
            },
            returns=(
                "一次结构化查询的产出：一句结论（conclusion）+ 结果行（rows，"
                "每条是一组「字段名 -> 值」）+ 列名（columns）+ 行数（row_count）+ "
                "实际执行的 SQL（sql）"
            ),
            cost_hint="中",
        )

    # ── 干活 ───────────────────────────────────────────────────────────
    def run(self, payload: dict[str, Any]) -> ToolResult:
        question = payload.get("question") or ""
        hint = payload.get("hint") or ""
        time_anchor = payload.get("time_anchor") or None
        query_intent = payload.get("query_intent") or {}
        followup_context = payload.get("followup_context") or None
        requirements = payload.get("requirements") or None

        from app.nl2sql.pipeline import build_conclusion

        outcome = self._get_pipeline().run(
            question, hint=hint, time_anchor=time_anchor,
            target_grain=query_intent.get("target_grain"),
            query_intent=query_intent,
            followup_context=followup_context,
            requirements=requirements,
        )
        self._last_sql = outcome.sql

        data: dict[str, Any] = {
            "conclusion": build_conclusion(
                outcome.rows,
                outcome.columns,
                error=outcome.error,
                automatic_limit=outcome.automatic_limit,
                result_limit_reached=outcome.result_limit_reached,
                sql=outcome.sql,
            ),
            "rows": outcome.rows,
            "columns": list(outcome.columns),
            "row_count": outcome.row_count,
            "sql": outcome.sql,
            "attempts": outcome.attempts,
            "need_clarify": outcome.need_clarify,
            "automatic_limit": outcome.automatic_limit,
            "possible_truncation": outcome.result_limit_reached,
        }
        if outcome.clarify_question:
            data["clarify_question"] = outcome.clarify_question
        if outcome.error:
            # 也放一份在 data 里：前端拿 data 就能展示"为什么没算出来"，
            # 不必去翻 trace。trace 里同样有（那是可解释性的正式载体）。
            data["error"] = outcome.error
        # 带上数据来源标识，好让编排层回答「这条 SQL 连的是哪个库」（AskResponse.data_source）。
        # 只有真生成出 SQL 才带 —— 没查库时谈"哪个库"没有意义。
        # 存的是 data_source() 这个可序列化 dict 而非目录对象：ToolResult 会被整只
        # model_dump_json（HTTP 与评测脚本都做），SchemaCatalog 对象塞进去就是
        # PydanticSerializationError（实测 app/test_nl2sql.py 序列化往返炸在这）。
        # dict 每次重建只多几个小字符串，量级可忽略。
        if outcome.sql:
            data["catalog"] = self._get_pipeline().catalog.data_source()
        if outcome.requirement_coverage is not None:
            # S2 闸门结论。**透传 dict 而非对象**：ToolResult 会被
            # model_dump_json，覆盖对象里含 QueryRequirement，Pydantic 不认。
            data["requirement_coverage"] = _coverage_dict(outcome.requirement_coverage)

        return ToolResult(data=data, trace=outcome.trace, refs=outcome.refs)

    def last_sql(self) -> str | None:
        """上一次真正执行的 SQL（编排层写进 `AskResponse.sql` 用）。

        与 mock 的同名方法保持同一契约，换实现不影响调用方。
        """
        return self._last_sql

    # ── 惰性装配 ───────────────────────────────────────────────────────
    def _get_pipeline(self):
        """第一次用到时才装配链路（读元数据、造客户端、造执行器）。"""
        if self._provided_pipeline is not None:
            return self._provided_pipeline
        if self._pipeline is None:
            self._pipeline = _build_pipeline(
                max_attempts=self._max_attempts,
                max_refs=self._max_refs,
            )
        return self._pipeline


def _coverage_dict(coverage) -> dict[str, Any] | None:
    """把要求覆盖结论压成可 JSON 化的小字典。

    `status` 三态语义（S2 计划 §12.1）：pass = 全部落实；repair = 有明确
    「要求被丢掉」的证据；unknown = 看懂了 SQL 但证不出是否满足。
    ⚠ unknown **不是**静默遗漏：它如实标黄并附说明。静默遗漏的判据是
    「未落实、未告知」。

    `coverage is None`（闸门 off，或没跑核对）时返回 None，**不造空结论**
    —— 空结论会被读成「核对过且通过」。
    """
    if coverage is None:
        return None
    return {
        "status": coverage.status,
        "version": coverage.version,
        "checks": [
            {
                "requirement_id": c.requirement_id,
                # kind/surface 进 dict 的原因：编排层 `_coverage_note` 要把
                # 未落实要求**用用户的原词**写进回答正文（P1(c) 第三留痕），
                # 只有 message 不够 —— message 可能不含原词（如 unknown 的
                # 模板话术），surface 才是「今年」「至今」这类用户说的词。
                "kind": c.details.get("kind", ""),
                "surface": c.details.get("surface", ""),
                "outcome": c.outcome,
                "reason_code": c.reason_code,
                "message": c.message,
                "sql_scope": c.sql_scope,
                # S2-F F5：未落实的继承条要能被前端认出来。继承条是上一轮
                # 的条件延续到本轮，用户看到「巴西」被标黄时需要知道它不是
                # 本轮新提的。要求条目的 id 形如 session:<kind>:<normalized>
                # （见 app/query_requirements.py::_augment_from_inherited）。
                "inherited": c.requirement_id.startswith("session:"),
            }
            for c in coverage.checks
        ],
    }


def _build_pipeline(*, max_attempts=None, max_refs=None):
    """装配真实的「生成 → 校验 → 执行」链路。"""
    from app.nl2sql.config import env_int
    from app.nl2sql.pipeline import (
        DEFAULT_MAX_ATTEMPTS,
        DEFAULT_MAX_REFS,
        Nl2SqlPipeline,
    )

    attempts = max_attempts or env_int("NL2SQL_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
    refs = max_refs or env_int("NL2SQL_MAX_REFS", DEFAULT_MAX_REFS)

    return Nl2SqlPipeline(
        _build_llm_client(),
        max_attempts=attempts,
        max_refs=refs,
        # max_tokens 交给客户端承载（见 _build_llm_client），此处不再重复传 ——
        # 两处都设会让"到底谁生效"变成需要读两个文件才能回答的问题。
    )


def _build_llm_client():
    """造大模型客户端（OpenAI 兼容协议，DeepSeek-V4.1-Flash 走这条路）。

    ⚠️ 这里 import 了 B 的模块，是本项目**唯一**一处结构化链路对非结构化链路的依赖。
       包 try/except 的理由见本文件开头：要让人一眼看出"是 B 的模块动了"，
       而不是顺着 `app.rag.llm` 这个名字去怀疑 RAG 检索出了问题。
    """
    try:
        from app.rag.llm import OpenAILikeClient
    except ImportError as exc:  # pragma: no cover - 取决于仓库状态
        raise RuntimeError(
            "nl2sql 无法装配大模型客户端：导入 app/rag/llm.py 失败。"
            "该模块提供的是通用的 OpenAI 兼容客户端（供 A 的编排层与 B 的 RAG 共用，"
            "见 .env.example 中 LLM_* 一节的说明）。"
            "若它被重命名或移动，请同步修改 app/tools/nl2sql.py 的 _build_llm_client。"
            "原始错误：" + str(exc)
        ) from exc

    from app.nl2sql.config import env_int

    return OpenAILikeClient(max_tokens=env_int("NL2SQL_MAX_TOKENS", DEFAULT_MAX_TOKENS))


def build_nl2sql_tool(backend=None):
    """按后端名造 nl2sql 工具。编排层只认工具名，不认后端。

    Args:
        backend: `real`（默认）或 `mock`。None 时读环境变量 `NL2SQL_BACKEND`。
    """
    if backend is None:
        backend = os.environ.get("NL2SQL_BACKEND", "real")
    backend = str(backend).strip().lower()

    if backend == "real":
        return Nl2SqlTool()
    if backend == "mock":
        # 只用于离线编排测试：真实链路的测试若走 mock，测的是"假件会不会返回字典"，
        # 没有任何意义。
        from app.tools.mock_nl2sql import MockNl2SqlTool

        return MockNl2SqlTool()
    raise ValueError("NL2SQL_BACKEND 只能是 real 或 mock，当前值：" + repr(backend))


__all__ = ["TOOL_NAME", "Nl2SqlTool", "build_nl2sql_tool"]
