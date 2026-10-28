"""三个人共用的数据结构契约。

本模块定义系统内所有跨模块传递的数据形状：
    - Ref 家族（BaseRef / SqlRef / DocRef）：一条「引用」——答案从哪来
    - TraceStep：一步工具调用的事实记录，可解释性的载体
    - ToolResult：一个工具的返回值
    - AskRequest / AskResponse：HTTP 接口的请求体与响应体

设计说明与字段来源见 `docs/契约设计_v0.6.md`（事实记录沿用 v0.5，新增可选深度检索请求字段）。
字段名一旦冻结（mock 链路跑通后），任何变更必须先改本文件、再三人同步。

不变量（invariants）：
    1. `BaseRef.snippet` 必须非空 —— 评测口径要求「引用片段非空」。
    2. `AskResponse.refs` == 所有 `TraceStep.hit` 的并集去重。
    3. `AskResponse.trace` 里每一步都带 `tool`，拼接后仍能分辨归属。
    4. `DocRef.char_start` / `char_end` 可为空；有偏移时去重按偏移，无偏移时退回
       按 (doc_id, page, section_path) 去重。
    5. `SqlRef` 有行主键时按行去重；无行主键但有 `result_id` 时按查询结果区分，
       两种定位都没有时沿用原表/字段级兼容去重。
    6. `DocRef` 一个只对应一页；跨页由 B 侧拆成多个 ref。
    7. `DocRef.bboxes` 是几何高亮依据；`char_start/char_end` 只做文本级定位，
       两者用途不同，不要互相替代。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ============================================================================
# 一、引用（Ref）
# ============================================================================


class BaseRef(BaseModel):
    """引用的公共部分。不应直接实例化，请用 SqlRef 或 DocRef。

    `snippet`（引用原文片段）必须非空：评测口径规定「没有引用的答案即使内容对，
    也不算这条通过」，所以它是硬要求，放在基类一处定义。
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="类型标记，用于区分 SqlRef / DocRef")
    snippet: str = Field(
        min_length=1,
        description="引用原文片段，必须非空",
    )


class SqlRef(BaseRef):
    """结构化链路的引用：命中哪张表、哪个字段、哪一行或哪次查询结果。"""

    kind: Literal["sql"] = "sql"
    table: str = Field(min_length=1, description="命中哪张表")
    column: str | None = Field(default=None, description="命中哪个字段")
    row_pk: str | None = Field(default=None, description="命中哪一行（主键值）")
    result_id: str | None = Field(
        default=None,
        min_length=1,
        description="查询结果级证据的本轮定位标识；不代表数据库行主键",
    )


class DocRef(BaseRef):
    """非结构化链路的引用：命中哪份文档、哪一页、哪个章节、哪一段。

    ⚠️ 约定：**一个 DocRef 只对应一页**。跨页的引用由 B 侧按页拆成多个 DocRef。
       当前 chunker 合并要求「同页」，天然不会跨页；此约定为将来放宽合并预留，
       届时契约无需再动。

    两套定位坐标，用途不同，别混：

    - **文本级定位**：char_start / char_end —— 在「全文按块阅读顺序虚拟拼接」
      得到的字符序号。只用于排序、去重、上下文定位。
      ⚠️ 它不是 PDF 像素坐标，**从中反推不出页面位置，不能用来做高亮**。
    - **几何级定位**：bboxes（框）+ page_width / page_height（换算基准）——
      前端画高亮框靠这三个。PyMuPDF 的 bbox 是 PDF 原生坐标系
      （**原点左上、单位 point**），要渲染成像素必须知道页面原始尺寸，
      否则光有框也算不出比例。

    doc_title 用于引用卡片显示（"来自《XX 制度》"）；
    doc_id 是内部标识（可能是 "1" 这种不可读的值），不要直接展示给用户。

    以上字段全部可空——渐进式填充，拿不到的留 None 即可，不影响链路跑通。
    """

    kind: Literal["doc"] = "doc"
    doc_id: str = Field(
        min_length=1,
        description='命中哪份文档（内部标识）。前端显示请用 doc_title',
    )
    doc_title: str | None = Field(
        default=None,
        description=(
            '文档对外显示名，如 "员工报销制度"。引用卡片显示用；'
            'doc_id 是内部值（如 "1"），不可直接展示。'
            "取值优先 manifest.json，没有则退回文件名。拿不到可留空"
        ),
    )
    page: int | None = Field(
        default=None,
        ge=1,
        description="第几页。一个 DocRef 只对应一页，跨页由 B 侧拆成多个 ref",
    )
    section_path: str | None = Field(
        default=None,
        description='章节路径，如 "第 3 章 > 3.2 报销标准"',
    )
    char_start: int | None = Field(
        default=None,
        ge=0,
        description=(
            "片段在「全文按块阅读顺序虚拟拼接」中的起始字符下标（从 0 起算）。"
            "仅用于文本级定位 / 排序 / 去重，**不做像素高亮**；高亮请用 bboxes。"
            "拿不到可留空"
        ),
    )
    char_end: int | None = Field(
        default=None,
        ge=0,
        description=(
            "同上，结束字符下标（不含）。仅用于文本级定位 / 去重，"
            "**不做像素高亮**；高亮请用 bboxes。拿不到可留空"
        ),
    )
    bboxes: list[tuple[float, float, float, float]] = Field(
        default_factory=list,
        description=(
            "本引用要渲染高亮的矩形集合，每个元素 (x0, y0, x1, y1)，"
            "按阅读顺序排列，且与 page 同页。当前覆盖整个切片；"
            "将来若做片段级高亮，语义才会分化（届时考虑改名）。"
            "坐标系：原点左上、单位 point（PDF 原生，来自 PyMuPDF）。"
            "**要换算成渲染像素，必须配合 page_width / page_height。**"
        ),
    )
    page_width: float | None = Field(
        default=None,
        gt=0,
        description=(
            "本页原始宽度（point）。前端把 bboxes 换算成像素的比例基准，"
            "缺了它 bboxes 画不出来。"
            "⚠️ 同一 PDF 里不同页可能不同尺寸，务必按页取，不要用文档级统一值"
        ),
    )
    page_height: float | None = Field(
        default=None,
        gt=0,
        description=(
            "本页原始高度（point）。同 page_width，按页取。拿不到可留空"
        ),
    )
    extraction_method: Literal["ocr", "text_layer", "plain"] | None = Field(
        default=None,
        description=(
            "这段证据的提取方式：ocr＝扫描页走离线 OCR；"
            "text_layer＝PDF 自带文字层；plain＝md/txt/docx 等纯文本来源。"
            "按页判定（loader 逐页分派），一个 DocRef 只对应一页，故取值唯一；"
            "摘要/融合类来源可能混合多页，此时如实留空。拿不到留 None"
        ),
    )


#: 「一条引用」的联合类型。
#: ⚠️ 不能写成 `list[BaseRef] = Field(discriminator="kind")`——
#:    pydantic v2 会报 TypeError，要求判别式加在联合类型上而非列表上。
#:    漏写判别式会导致子类字段静默丢失（不报错）。
RefT = Annotated[SqlRef | DocRef, Field(discriminator="kind")]


# ============================================================================
# 二、调用记录（TraceStep）
# ============================================================================


class TraceStep(BaseModel):
    """一步工具调用的事实记录。

    这是可解释性评分的唯一载体：必须是代码埋点录下来的客观事实，
    不能用模型自己叙述的「思维链」充当（不可验证、易事后合理化）。
    """

    model_config = ConfigDict(extra="forbid")

    step_id: int = Field(ge=1, description="第几步，从 1 开始")
    tool: str = Field(min_length=1, description="调了什么工具")
    input: dict[str, Any] = Field(default_factory=dict, description="传给工具的入参")
    output: dict[str, Any] = Field(default_factory=dict, description="工具返回的产出")
    elapsed_ms: float = Field(ge=0, description="这一步耗时（毫秒）")
    hit: list[RefT] | None = Field(
        default=None,
        description="这一步命中了什么来源。与 AskResponse.refs 同一套结构",
    )


# ============================================================================
# 三、工具返回值（ToolResult）
# ============================================================================


class ToolResult(BaseModel):
    """统一工具签名：tool(input) -> ToolResult。

    字段名由 docs/功能清单与技术框架_v1.md 第 98 行定死，不要加第四个字段。
    """

    model_config = ConfigDict(extra="forbid")

    data: Any = Field(description="工具的原始产出：NL2SQL 是结果行，RAG 是检索片段")
    trace: list[TraceStep] = Field(default_factory=list, description="内部产生的步骤")
    refs: list[RefT] = Field(default_factory=list, description="本工具的引用列表")


# ============================================================================
# 四、HTTP 接口（AskRequest / AskResponse）
# ============================================================================


class AskRequest(BaseModel):
    """POST /api/ask 的请求体。字段名由功能清单第 97 行定死。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, description="用户原话")
    session_id: str = Field(min_length=1, description="会话标识，多轮对话依赖它")
    retrieval_mode: Literal["standard", "deep"] = Field(
        default="standard",
        description="文档检索路径；deep 表示在项目 RAG 上追加 KAG 候选检索",
    )


class ResultSetInfo(BaseModel):
    """A short-lived handle for viewing a bounded SQL result set."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=32, description="不可预测的短期结果集标识")
    row_count: int = Field(ge=0, description="当前可查看的行数；complete=false 时是已取得的部分行数")
    complete: bool = Field(description="是否已取得查询结果全集")
    page_size: int = Field(ge=1, le=200, description="建议的分页大小")
    expires_at: str = Field(min_length=1, description="结果集缓存的过期时间，ISO 8601 格式")


class ResultSetPage(BaseModel):
    """One page of a cached SQL result set."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=32)
    columns: list[str]
    rows: list[dict[str, Any]]
    offset: int = Field(ge=0)
    page_size: int = Field(ge=1, le=200)
    row_count: int = Field(ge=0, description="当前可查看的行数")
    complete: bool
    next_offset: int | None = Field(default=None, ge=0)
    expires_at: str = Field(min_length=1)


class Suggestion(BaseModel):
    """一条可点击的追问建议。

    与 `TraceStep` 同一条原则：**建议必须是代码从本轮已算出的事实里组合出来的**，
    不是模型自由生成的。`reason` 就是这条建议的依据，前端会显示在问句下方 ——
    评委能看出「这条追问为什么会出现」，而不是凭空冒出来的推荐。
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, description="可直接提交的完整问句")
    reason: str = Field(default="", description="这条建议的依据，用于界面展示")


class DataSource(BaseModel):
    """本轮问答实际读的是哪一份数据。

    **两种来源都要标**，不能只标数据库：
      - 结构化链路 → 哪个库（`chinook` / `adventureworks`）
      - 文档链路   → 哪个语料库（多少份文档、哪些格式）

    为什么文档侧也要：只显示文档标题，用户同样分不清答案来自哪一批材料。
    答辩时「你这套系统读的是什么数据」几乎是必问的 —— **主动标明比被问出来强**。

    刻意**不含**连接信息：不暴露主机、端口、账号、连接串。这些是运维信息，
    不是业务信息，真实企业问数系统也不会把它显示给业务用户。
    库名/语料规模足以让用户判断"这是哪一份数据"。
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(
        default="database",
        description="来源类型：database（结构化库）或 corpus（文档语料库）",
    )
    database: str | None = Field(
        default=None, description="库名，如 chinook；文档链路为 None"
    )
    dialect: str | None = Field(
        default=None, description="数据库方言，如 postgres；文档链路为 None"
    )
    label: str = Field(min_length=1, description="给人看的展示名，如「Chinook 示例库（PostgreSQL）」或「文档语料库（15 份 · 4 种格式）」")
    document_count: int | None = Field(
        default=None, ge=0, description="语料库文档份数；结构化链路为 None"
    )
    formats: list[str] = Field(
        default_factory=list, description="语料库覆盖的文件格式，如 ['docx','md','pdf','txt']"
    )


class ContextContinuation(BaseModel):
    """本轮答案是否**接续了上一轮问句**（多轮对话上下文保持的外显）。

    为什么要单独一个字段、而不是让前端去翻 trace：
      1. 追溯要看 trace 第 1 步 input 里的 `session_action`，那是**埋点**语义，
         不是展示契约 —— 前端为了显示一句话去解析 trace，等于把内部实现
         结构当接口用了，将来挪一步就断。
      2. 赛题「连续 5 轮以上准确」是**演示项**：评委现场连点几问，
         界面上必须看得出"这一轮是接着上一轮的"，否则等于没做。
         后端已经在 `SessionMemory._combine` 里拼了上下文，
         但那行接缝只存在于送往模型的字符串里，用户永远看不见。

    只放**看得懂的轮次号和原文**，不放 internal action 字符串
    （`short_followup_attached` / `pending_clarification_resumed` 这类）。
    """

    model_config = ConfigDict(extra="forbid")

    previous_question: str = Field(
        min_length=1,
        description="本轮实际接续的上一轮问句原文（累积文本，可能已包含更早轮次的补充）",
    )
    round_index: int = Field(
        default=2, ge=2,
        description="本轮是这次连续对话的第几问；接续首段时为 2。仅作展示计数，不参与判定",
    )


class DatabaseColumnInfo(BaseModel):
    """A safe, read-only description of one database column."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    type: str = Field(default="")
    nullable: bool = True
    is_primary_key: bool = False
    description: str = ""
    foreign_key_to: str | None = None


class DatabaseTableInfo(BaseModel):
    """One table from the same schema catalog used by SQL generation."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: str = Field(alias="schema", min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    column_count: int = Field(ge=0)
    columns: list[DatabaseColumnInfo] = Field(default_factory=list)


class DatabaseSchemaResponse(BaseModel):
    """Browsable schema metadata; it contains no sample row values or secrets."""

    model_config = ConfigDict(extra="forbid")

    database: str | None = None
    label: str = Field(min_length=1)
    dialect: str = Field(min_length=1)
    contract_version: str = ""
    table_count: int = Field(ge=0)
    tables: list[DatabaseTableInfo] = Field(default_factory=list)


class DatabaseStatusResponse(BaseModel):
    """Live database connectivity plus the identity of the loaded schema catalog."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["connected", "unavailable", "not_used", "catalog_mismatch"]
    database: str | None = None
    catalog_database: str | None = None
    label: str = Field(min_length=1)
    dialect: str = Field(min_length=1)
    table_count: int = Field(ge=0)
    checked_at: str = Field(min_length=1)


class UnstructuredDocumentInfo(BaseModel):
    """Safe document inventory for the configured RAG index."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    extension: str = Field(min_length=1)
    indexed_chunk_count: int | None = Field(default=None, ge=0)


class UnstructuredDatabaseResponse(BaseModel):
    """Live state of the document search store, without exposing DSNs or text."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    status: Literal["connected", "not_initialized", "memory", "not_used", "unavailable"]
    backend: Literal["pgvector", "memory", "mock"]
    label: str = Field(min_length=1)
    database: str | None = None
    schema_name: str | None = Field(default=None, alias="schema")
    corpus_document_count: int = Field(ge=0)
    indexed_document_count: int | None = Field(default=None, ge=0)
    chunk_count: int | None = Field(default=None, ge=0)
    vector_extension_ready: bool | None = None
    warning: str | None = None
    checked_at: str = Field(min_length=1)
    tables: list[DatabaseTableInfo] = Field(default_factory=list)
    documents: list[UnstructuredDocumentInfo] = Field(default_factory=list)


class UnstructuredDatabaseStatusResponse(BaseModel):
    """Connection-only status for the active document search store."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["connected", "unavailable", "memory", "not_used"]
    backend: Literal["pgvector", "memory", "mock"]
    label: str = Field(min_length=1)
    checked_at: str = Field(min_length=1)


class FactReviewCheck(BaseModel):
    """A deterministic check result backed by structured execution output."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    result: Literal["passed", "failed", "unknown", "not_run"]
    # 可选的一句话说明。只在"结论需要解释"时填（如查询条件没落实的具体原因），
    # 普通二进制结论留 None —— 不为了整齐给每条都编一句话。
    detail: str | None = Field(
        default=None,
        description="可读的结论说明；仅在该结论需要解释时提供",
    )


class FactReviewItem(BaseModel):
    """A projected ledger fact; this is not sentence-level answer verification."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    summary: str | None = None
    status: Literal["candidate", "supported", "contradicted", "insufficient", "ambiguous"]
    execution_status: Literal["completed", "not_run", "failed"]
    evidence_link_status: Literal["complete", "partial", "unavailable"]
    checks: list[FactReviewCheck] = Field(default_factory=list)
    evidence_ref_indices: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=list,
        description="0-based indexes into this response's refs",
    )
    reason_code: str | None = None
    reason: str | None = None

    # 以下三个字段是方向三为「可溯源事实核验」补的（v2 契约的向后兼容扩展）。
    # 原契约只能表达「这条事实判成什么状态」，表达不了「判出什么值」「凭哪一块」——
    # 而对公示语料做计数核验时，值本身和它落在哪个块才是要展示的东西。
    value: str | None = Field(
        default=None,
        description=(
            "判定出的值（如「38 项」）。与 status 分工：status 说的是这条成不成立，"
            "value 说的是成立时具体是多少。非计数类的事实可以不填。"
        ),
    )
    criterion: str | None = Field(
        default=None,
        description=(
            "这条事实的判定口径，即 label 的前半段（如「AH24（安徽 2024 年度）」）——"
            "指明它属于哪份文档、哪一年。界面用它做分组，避免与 label 重复显示。"
        ),
    )
    evidence_chunk: str | None = Field(
        default=None,
        description=(
            "本行证据所在的原文块标识（如「AH24_v2_split_4」），与 "
            "evidence_ref_indices 指向同一处、两种表示：索引供前端跳转，"
            "块名供人与日志直接引用。取不到时留空，不要填占位串。"
        ),
    )


class FactReview(BaseModel):
    """Optional, versioned projection of a request-scoped fact ledger."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2"] = "2"
    coverage: Literal[
        "structured_query",
        "document_qa",
        "cross_source_work_order_sla",
        "cross_source_unplanned",
    ]
    verification_mode: Literal["strict", "ablation", "source_specific"]
    verification_scope: Literal[
        "query_execution_and_evidence",
        "document_retrieval_and_citation",
        "fact_dependencies_and_business_checks",
        "cross_source_execution_only",
    ]
    scope_note: str = Field(min_length=1)
    items: list[FactReviewItem] = Field(default_factory=list)


class AskResponse(BaseModel):
    """POST /api/ask 的响应体。字段名由功能清单第 97 行定死。

    不变量：refs == 所有 trace 里 hit 的并集去重。
    """

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(description="最终答案；需要澄清时这里装反问句")
    trace: list[TraceStep] = Field(default_factory=list, description="完整调用链")
    refs: list[RefT] = Field(default_factory=list, description="引用总表（hit 并集去重）")
    sql: str | None = Field(default=None, description="走的 SQL 语句（仅结构化链路）")
    need_clarify: bool = Field(default=False, description="是否需要用户补充信息")
    result_set: ResultSetInfo | None = Field(default=None, description="大结果集的分页查看信息")
    suggestions: list[Suggestion] = Field(
        default_factory=list,
        description="追问建议；从本轮检索/查询结果组合而来，点击即可继续提问",
    )
    data_source: DataSource | None = Field(
        default=None,
        description="结构化链路的数据源标识；未走 SQL 时为 None（文本链路谈不上「哪个库」）",
    )
    context_continuation: ContextContinuation | None = Field(
        default=None,
        description=(
            "本轮是否接续了上一轮问句（多轮对话上下文保持）。"
            "未接续（首轮、或没识别出追问）时为 None —— 不返回空壳对象，"
            "前端直接判有无即可，不必再判字段是否填了。"
        ),
    )
    fact_review: FactReview | None = Field(
        default=None,
        description="Versioned, opt-in fact ledger projection; scope notes define what was and was not verified",
    )

    @model_validator(mode="after")
    def validate_fact_review_indices(self) -> "AskResponse":
        if self.fact_review is None:
            return self
        ref_count = len(self.refs)
        for item in self.fact_review.items:
            if len(set(item.evidence_ref_indices)) != len(item.evidence_ref_indices):
                raise ValueError("fact_review evidence_ref_indices 不能重复")
            if any(index >= ref_count for index in item.evidence_ref_indices):
                raise ValueError("fact_review evidence_ref_indices 超出本响应 refs 范围")
        return self


# ============================================================================
# 五、工具函数
# ============================================================================


def merge_trace(steps: list[TraceStep]) -> list[TraceStep]:
    """把多个工具的 trace 拼成总 trace，并重排 step_id。

    拼接时保留每步的 `tool` 字段，让前端仍能分辨哪步属于哪个工具。
    """
    merged: list[TraceStep] = []
    for i, step in enumerate(steps, start=1):
        merged.append(step.model_copy(update={"step_id": i}))
    return merged


def collect_refs(trace: list[TraceStep]) -> list[RefT]:
    """从 trace 里汇总所有引用，并去重。

    去重规则：同一 (kind, 定位字段) 视为同一条；保留首次出现的 snippet。
        - SqlRef：有真实行主键时按 (table, column, row_pk) 去重；否则若有查询结果级 `result_id`，按
          (table, column, result_id) 区分本轮不同聚合/查询结果；两者都没有时沿用旧表/字段粒度
        - DocRef：**有字符偏移时**按 (doc_id, char_start, char_end) 去重（更精确，
          同一页的不同段落能区分开）；**偏移为空时**退回
          (doc_id, page, section_path) 去重（兼容没算偏移的来源）。
    ⚠️ 非定位字段（如 `extraction_method`）**不参与去重**，同样取首次出现那条：
       定位一致即为同一条证据，不要在合并时挑选或覆写这些字段，否则同一条
       证据会因为来自哪条路径而显得不一致。`orchestrator._dedup_refs` 与本函数
       口径必须保持一致（两处都改或都不改）。
    这是 `AskResponse.refs` 的唯一正确来源——不要手动往 refs 里塞东西。
    """
    seen: set[tuple[str, ...]] = set()
    out: list[RefT] = []
    for step in trace:
        for ref in step.hit or []:
            if isinstance(ref, SqlRef):
                if ref.row_pk:
                    key = ("sql", ref.table, ref.column or "", ref.row_pk)
                elif ref.result_id:
                    key = ("sql_result", ref.table, ref.column or "", ref.result_id)
                else:
                    key = ("sql", ref.table, ref.column or "", "")
            elif ref.char_start is not None or ref.char_end is not None:
                key = (
                    "doc",
                    ref.doc_id,
                    str(ref.char_start if ref.char_start is not None else ""),
                    str(ref.char_end if ref.char_end is not None else ""),
                )
            else:
                key = ("doc", ref.doc_id, str(ref.page or ""), ref.section_path or "")
            if key in seen:
                continue
            seen.add(key)
            out.append(ref)
    return out


__all__ = [
    "BaseRef",
    "SqlRef",
    "DocRef",
    "RefT",
    "TraceStep",
    "ToolResult",
    "AskRequest",
    "ResultSetInfo",
    "ResultSetPage",
    "Suggestion",
    "DataSource",
    "ContextContinuation",
    "DatabaseColumnInfo",
    "DatabaseTableInfo",
    "DatabaseSchemaResponse",
    "DatabaseStatusResponse",
    "UnstructuredDocumentInfo",
    "UnstructuredDatabaseResponse",
    "UnstructuredDatabaseStatusResponse",
    "AskResponse",
    "merge_trace",
    "collect_refs",
]
