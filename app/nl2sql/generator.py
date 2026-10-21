"""generator.py - 把「自然语言问题 + 库结构」交给大模型，产出候选 SQL。

**它在链路里的位置**：pipeline 调它拿候选 SQL → validator 静态校验 → executor 执行。
它自己**不做任何校验**，也不连库 —— 只负责"让模型写出一条 SQL 并把它干净地取出来"。
把校验拆出去的理由：模型写错是常态，必须有一道**确定性**的关卡兜住，
而"让模型自查"不是确定性关卡（它总说自己写对了）。

**为什么提示词里给的是「带中文说明的字段清单」而不是标准 DDL**：
    DDL（CREATE TABLE）里没有业务说明，而"供应商 → customer""流派 → genre"
    这类映射恰恰要靠说明才能建立。见 schema.render_ddl 的完整说明。

**三个必须守的工程细节**（都是踩过的坑）：

    ① **模型不会老实听话**。即使提示词写了"只输出 SQL、不要 Markdown"，
       它仍会回 ```sql ... ``` 或"好的，以下是 SQL："。所以必须先剥壳再交给
       校验器 —— 否则校验器会把一句完全正确的 SQL 判成"解析失败"。
       剥壳动作会记进 `notes`，不在暗处发生。
    ② **LLM 调用失败不能抛异常穿透链路**。本模块把异常收敛成 `GenerationResult.error`，
       由 pipeline 决定"重试还是回错误答案"。理由：网络抖动、限流是常态，
       让一个 URLError 冒到 HTTP 层只会变成 500，而用户该看到的是"这条没答上来"。
    ③ **不猜 max_tokens**。国产推理模型的思维链**计入** max_tokens，
       上限给小了会表现为"想一半就断"，返回的 content 是空的。
       所以上限由调用方（app/tools/nl2sql.py 的工厂）按环境变量显式传入，
       本模块不设默认值硬编码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.nl2sql.followup import followup_prompt

#: 系统提示。写成常量便于单测直接引用、也便于将来做 A/B 对比提示词版本。
SYSTEM_PROMPT = """你是一个 PostgreSQL 数据库查询专家。你的任务是把用户的中文问题转写成**一条**可直接执行的只读 SQL。

必须遵守：
1. 只输出一条 SELECT 语句。禁止任何写操作（INSERT / UPDATE / DELETE / DROP / ALTER / CREATE / TRUNCATE）。
2. 只能使用「库结构」里出现过的表名与字段名，拼写必须完全一致；不要臆造字段或表。
3. 只输出 SQL 本身：不要解释、不要 Markdown 代码块、不要加任何前后缀文字。
4. 使用 PostgreSQL 语法。
5. 结果列请起英文小写下划线别名（例如 customer_count），不要用中文别名，也不要用引号包裹别名。
6. 分组聚合时，SELECT 里的每个非聚合列都必须出现在 GROUP BY 里（PostgreSQL 强制要求，漏了会直接报错）。
7. 时间范围用左闭右开区间（`>= 起点 AND < 次日/次年`），不要用 BETWEEN，也不要用依赖"当前日期"的函数。
8. 用户要求"排序 / 前 N 名 / 最多 / 最少"时必须写 ORDER BY；有并列风险时补上次级排序键（如按编号或名称升序），保证结果确定。
9. 用户说"前 N 名 / 最高的 N 个"时要写 LIMIT N。
10. 用户询问增长率、同比或环比时，结果统一用百分数表示： (本期值 - 对比期值) / NULLIF(对比期值, 0) * 100；结果别名以 `_percent` 结尾。
11. 如果问题中明确写出"用户明确补充的增长率比较周期（对比期/基期）：X"，必须将 X 作为增长率的对比期；保留原问题中已明确的本期，不得把 X 当成本期再推算其上一期。
12. 用户只要求一个"姓名/名称"列时，如果库中由 first_name 和 last_name 分列存储，应合并成一个结果列；只有用户明确要求名、姓分别输出时才拆成两列。
13. 用户询问"订单数/发票数/笔数"等记录数量时，使用 COUNT(*) 或 COUNT(主键)，不得误用金额求和；只有明确询问销售额、营收或金额时才对金额字段求和。"""

#: 代码块围栏。模型最常见的违规形态。
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.S)
#: `<sql> ... </sql>` 形态（少见但存在）。
_TAG_RE = re.compile(r"<sql>(.*?)</sql>", re.S | re.I)
#: 行首的 `SQL:` / `SQL：` 之类的标签。
_LABEL_RE = re.compile(r"^\s*(?:sql|查询语句|语句)\s*[:：]\s*", re.I)
#: 语句真正的起点。
_START_RE = re.compile(r"(?im)^[ \t]*(select|with)\b")


@dataclass
class GenerationResult:
    """一次生成的结果。

    Attributes:
        sql: 剥壳后取到的候选 SQL（未校验）。取不到时为 None。
        raw: 模型原始输出，出错时用于事后对账（不要丢，调试时全靠它）。
        error: 失败原因（一句话人话）。None 表示这次调用本身成功了。
        notes: 非阻断性处理留痕（如"剥离了 Markdown 代码块"）。
    """

    sql: str | None = None
    raw: str = ""
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def requirements_prompt(requirements) -> str:
    """把要求清单渲染成生成提示词里的【用户要求（必须落实）】块。

    为什么生成器也要看清单（S2-F F4）：清单此前只喂给校验器——校验器
    能发现"继承来的『巴西』没写进 SQL"，但生成器根本不知道有这条要求，
    修一次错一次。把要求前置到生成期，才是"让模型第一次就写对"。

    `source="session"` 的条目是上一轮继承来的（如"只看巴西的呢？"里的
    巴西），必须显式标注，否则模型会把它们当成用户本轮新说的话而犹豫。

    纯函数；`requirements` 为 None 或无条目时返回空串（不占提示词）。
    """
    if requirements is None:
        return ""
    items = [
        item for item in getattr(requirements, "requirements", ()) or ()
        if item.actionable
    ]
    if not items:
        return ""
    kind_names = {
        "object": "业务对象",
        "cross_source_object": "跨源对象",
        "measure": "统计口径",
        "time": "时间范围",
        "value_filter": "取值过滤",
        "numeric_range": "数值条件",
        "negation": "排除条件",
        "grouping": "分组维度",
        "ranking": "排名/前 N",
        "distinct_grain": "去重口径",
        "document_scope": "文档范围",
        "value_scope": "取值口径",
        "join_grain": "关联粒度",
    }
    lines = []
    for item in items:
        origin = "（上一轮继承，必须延续到本轮 SQL）" if item.source == "session" else ""
        label = kind_names.get(item.kind, item.kind)
        normalized = (item.normalized or "").strip()
        suffix = f"（即 {normalized}）" if normalized and normalized != item.surface else ""
        lines.append(f"- {label}：{item.surface}{suffix}{origin}")
    return "\n".join(lines)


def build_prompts(
    question,
    catalog,
    *,
    hint="",
    dialect="postgres",
    previous_error=None,
    previous_sql=None,
    time_anchor=None,
    selected_table_names=None,
    planning_constraints=None,
    followup_context=None,
    requirements=None,
):
    """拼出 (system, user) 两条提示。**纯函数**，不调用模型 —— 便于单测直接断言提示内容。

    为什么把上文失败原因也塞进提示词：这是"修复重试"的关键。
    不带错误信息重试等于让模型再猜一次，大概率还是同一个错（模型有很强的
    "同一问题同一写法"倾向）；带上"表 x 里没有字段 y"它才知道往哪改。
    """
    lines = [
        "【库结构】",
        catalog.render_ddl(table_names=selected_table_names),
        "",
        "【用户问题】",
        (question or "").strip(),
    ]

    # 多轮拼接文本会带上前几轮的原话（prepare_turn 把「…\n用户补充或修改：本轮」
    # 整段传下来）。清单的 `original` 只含**本轮原话**（extract_requirements 的
    # 契约），把它单独标出来，让模型知道本轮真正要答的是哪一句 —— 否则模型会把
    # 前几轮的条件（如上一轮的「只看巴西」）当成仍然有效，写进本轮 SQL。
    # 实测踩过：t2「只看巴西的发票」→ t3「那有多少位员工？」，t3 的 SQL 仍带
    # billing_country='Brazil' 答出 0。
    latest_turn = ((getattr(requirements, "original", "") or "")).strip()
    if latest_turn and latest_turn != (question or "").strip() and latest_turn in (question or ""):
        lines += [
            "",
            "【本轮用户实际提出（以此为准）】",
            latest_turn,
            "「用户问题」里「用户补充或修改：」之前的内容是**历史轮次**，只提供背景，"
            "不得把其中只属于历史轮次的条件（如对象、国家、时间）写进本轮 SQL；"
            "历史条件是否延续，以上面的「用户要求」清单为准。",
        ]

    if planning_constraints:
        lines += ["", "【结构化候选与约束】", str(planning_constraints).strip(),
                  "只可按这些已核对候选理解问题；若关系或口径不足，不得自行编造。"]

    hint = (hint or "").strip()
    if hint:
        lines += ["", "【补充线索】", hint]

    continuation = followup_prompt(followup_context)
    if continuation:
        lines += ["", "【多轮查询修改约束】", continuation]

    requirement_lines = requirements_prompt(requirements)
    if requirement_lines:
        lines += [
            "",
            "【用户要求（必须落实）】",
            requirement_lines,
            "以上要求必须逐条体现在 SQL 里；做不到的不要假装做到，"
            "也不要添加用户没有提出的条件。",
        ]

    if time_anchor:
        lines += [
            "",
            "【时间基准】",
            "把所有相对时间（今年/本月/上个月/最近）都相对这一天计算："
            + str(time_anchor).strip(),
        ]

    if previous_error:
        lines += ["", "【上一次尝试失败，请修正后重写】"]
        if previous_sql:
            lines += ["上次生成的 SQL：", str(previous_sql).strip()]
        lines += [
            "失败原因：" + str(previous_error).strip(),
            "请只输出修正后的 SQL。",
        ]

    lines += ["", "请输出 SQL："]
    return SYSTEM_PROMPT, "\n".join(lines)


def extract_sql(text):
    """从模型输出里剥出 SQL 文本。剥不出来返回空串。

    处理顺序：`<sql>` 标签 → Markdown 代码块 → `SQL:` 标签 → 从第一行 SELECT/WITH 起截断。

    最后一步是兜底：模型说"好的，以下是查询："这类前言时，前面几句都没有 SQL 关键字，
    直接切到真正的语句起点即可 —— 比"整段丢给解析器然后判失败"要准得多。

    **最后还要做一次"到底有没有 SQL"的检查**（`\bselect\b|\bwith\b`）：
        "抱歉，我无法回答这个问题。"这类拒答文本里一个 SQL 关键字都没有。
        若不在这里判掉，它会被当成"拿到了 SQL"交给校验器，于是
        ① 报出来的错是"SQL 解析失败"（把人往语法上引），而不是"模型没给出 SQL"；
        ② 重试逻辑会带着这个牛头不对马嘴的错误信息去问模型。
        判据只看"有没有语句起始关键字"，不做语法判断 —— 语法归 validator 管。
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    tag = _TAG_RE.search(raw)
    if tag:
        raw = tag.group(1).strip()

    fence = _FENCE_RE.search(raw)
    if fence:
        raw = fence.group(2).strip()
    else:
        # 有开围栏没闭合（模型被 max_tokens 截断）——把围栏那一行去掉，留下后面的内容
        raw = re.sub(r"^\s*```[A-Za-z0-9_+-]*\s*$", "", raw, flags=re.M).strip()

    raw = _LABEL_RE.sub("", raw)

    if raw and not re.match(r"^\s*(select|with)\b", raw, re.I):
        match = _START_RE.search(raw)
        if match:
            raw = raw[match.start():]

    raw = raw.strip()
    if not re.search(r"\b(select|with)\b", raw, re.I):
        return ""
    return raw


def generate_sql(
    question,
    catalog,
    client,
    *,
    hint="",
    dialect="postgres",
    previous_error=None,
    previous_sql=None,
    time_anchor=None,
    max_tokens=None,
    selected_table_names=None,
    planning_constraints=None,
    followup_context=None,
    requirements=None,
):
    """让模型生成一条候选 SQL。**不抛异常**，失败信息放在返回值里。

    Args:
        client: 任何具备 `chat(system, user) -> str` 的对象。
            （现有实现是 `app/rag/llm.py` 的 OpenAILikeClient；本模块不 import 它，
              由外层注入，见包 docstring 的零耦合纪律。）
        max_tokens: 输出上限。**必须由外层给**，因为国产推理模型的思维链计入该上限，
            给小了会静默返回空内容（见模块 docstring ③）。

    Returns:
        `GenerationResult`。`error` 非空表示这次没拿到可用 SQL。
    """
    system, user = build_prompts(
        question,
        catalog,
        hint=hint,
        dialect=dialect,
        previous_error=previous_error,
        previous_sql=previous_sql,
        time_anchor=time_anchor,
        selected_table_names=selected_table_names,
        planning_constraints=planning_constraints,
        followup_context=followup_context,
        requirements=requirements,
    )

    chat_kwargs = {}
    if max_tokens is not None:
        chat_kwargs["max_tokens"] = max_tokens

    try:
        raw = _call_client(client, system, user, chat_kwargs)
    except Exception as exc:  # noqa: BLE001 —— 网络/鉴权/限流都算"这次没答上来"，不是程序崩溃
        return GenerationResult(raw="", error="调用大模型失败：" + _short(exc))

    notes = []
    sql = extract_sql(raw)
    if not sql:
        return GenerationResult(
            sql=None,
            raw=raw,
            error="大模型没有返回可用的 SQL（输出为空或不含 SELECT 语句）",
            notes=notes,
        )

    # 留痕：模型没守"只输出 SQL"的规矩时，我们做了什么
    if raw.strip() != sql:
        notes.append("模型输出含解释或代码块，已剥离为纯 SQL")

    return GenerationResult(sql=sql, raw=raw, notes=notes)


def _call_client(client, system, user, kwargs):
    """按客户端能力调用 chat。

    为什么要探测 `max_tokens`：现有 `OpenAILikeClient.chat(system, user)` 的签名
    里没有这个参数，而 max_tokens 只在**构造时**可传。传不进去时宁可降级调用
    （用端点自己的默认上限），也不要因为签名不匹配让整条链路崩掉。
    """
    if not kwargs:
        return client.chat(system, user)
    try:
        return client.chat(system, user, **kwargs)
    except TypeError:
        return client.chat(system, user)


def _short(exc, limit=200):
    text = str(exc).strip().replace("\n", " ")
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


__all__ = [
    "SYSTEM_PROMPT",
    "GenerationResult",
    "build_prompts",
    "extract_sql",
    "generate_sql",
    "requirements_prompt",
]
