"""多轮追问接续提示：从本轮已经算出来的事实里组合出可点的下一问。

## 为什么不用大模型生成建议

两层原因，第二层更重要：

1. **非确定性**。同一问题两次提问得到不同建议，与「同一问题的答案逐字可复现」
   这套系统正对外的卖点矛盾。上一轮实测已确认两条链路都是确定性的。
2. **可能建议出不存在的问法**。模型凭直觉造出库里没有的表、没有的字段，
   用户一点就是一个错误答案 —— 把「可解释」做成了「可误导」。
   而建议是**最容易被当成承诺**的东西：系统主动端上来的问题，用户默认它答得出。

所以这里的口径是：**只从已经落地的结构里选词**。表名来自 `infra/schema_meta.json`，
字段的一两个真实取值来自刚才那条 SQL 的结果行，章节标题来自刚才真正召回的切片。
一个词如果没在事实里出现过，就进不了建议。

## 四类来源（按可信度排序）

- `measure` / `dimension`：换一个度量或维度问同一张表（结构化链路）
- `schema`：换一张相邻的表问（沿外键，结构化链路）
- `document`：问同一文档的另一个章节（非结构化链路）
- `口径`：把本轮被澄清的口径反过来问，给用户一个"重新选择"的入口

## 为什么建议必须是自包含的完整问句

`app/memory.py` 的回溯改写只在会话已经有状态时生效（`_get_locked` 对新会话返回
`None`，首轮不落状态），且只认 `_FOLLOWUP_MARKERS` 里那 16 个词。写成
「那它的呢？」这种挂靠上下文的问法，在首轮或措辞不匹配时会退化成一句没有指代的
孤句。赛题「连续 5 轮以上准确」的测法是连点五下，每次都当成独立提问来答 ——
自包含既能保证这一点，也正好当作 `_NEW_QUESTION_PREFIXES` 所服务的那种「新问题」。

代价是少了「那…呢」的语言错觉，这个代价是划算的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
import threading

from app.contracts import Suggestion as _ContractSuggestion

#: 一次最多给几条。多了变成菜单，反而让人不知道点哪个。
MAX_SUGGESTIONS = 3

#: 表名 -> 中文说法。缺失时退回「表名 表」，不猜。
_TABLE_CN: dict[str, str] = {
    "album": "专辑",
    "artist": "艺术家",
    "customer": "客户",
    "employee": "员工",
    "genre": "音乐流派",
    "invoice": "发票",
    "invoice_line": "发票明细",
    "media_type": "媒体类型",
    "playlist": "播放列表",
    "playlist_track": "播放列表曲目",
    "track": "曲目",
}

#: 数值字段不需要「列出取值」，也不适合「按它分组」。
#: 这些是**结构判断**不是类别枚举，所以用后缀/类型规则，不用白名单。
_ID_SUFFIXES = ("_id",)
_NUMERIC_TYPES = ("INT", "NUMERIC", "DECIMAL", "REAL", "DOUBLE", "FLOAT", "MONEY", "BIGINT", "SMALLINT")

#: 字段说明里出现这些词，说明它不是给人看的业务字段，而是结构性描述
#: （外键指向谁、主键是什么）。
#:
#: 为什么必须拦：`schema_meta.json` 里的 description 是机器生成的整句，例如
#: `support_rep_id` 的原文是「负责该客户的客服代表，外键指向 employee.employee_id。」。
#: 早期实现按「取到第一个标点为止」当标签，切出半句话拼成了
#: 「客户的负责该客户的客服代表，外键指向 employee.employee_id总计是多少？」
#: —— 不是中文。宁可这个字段不出现，也不要出现这种句子。
_STRUCTURAL_HINTS = (
    "外键", "主键", "指向", "唯一标识", "自关联", "关联表", "映射",
    # 「该行/本行/此行」是写给人看的表格定位语，塞进问句会变成
    # 「发票明细的该行成交单价总计是多少？」—— 用户不知道「该行」指哪一行。
    "该行", "本行", "此行", "前述", "上述",
)

#: 这些字段名虽然不含 `_id`，但语义上是主键或外键，按类别字段处理会问出
#: 「按 invoice_id 分组分别有多少条」这种没人想问的问题。
_NOT_DIMENSIONS = frozenset(
    {"id", "rowid", "uuid", "guid", "fingerprint", "hash", "checksum"}
)

#: 自由文本字段：可以「列出取值」但分组没意义。
_FREETEXT_HINTS = ("address", "postal", "phone", "fax", "email", "url", "note", "remark", "comment")

#: 列名带这些词，基本可以确认是**低基数类别列**，适合分组。
#: 放在高基数判断之前做反向确认 —— 例如 `billing_country` 同时含 "billing"
#: 与 "country"，只有这里先放行，才不会被别的规则误杀。
_CATEGORY_HINTS = (
    "country", "state", "city", "region", "province", "district",
    "type", "category", "kind", "status", "level", "grade", "class",
    "genre", "gender", "currency", "language", "unit",
)

#: 列名带这些词，说明**每行一个值**，分组出来基本每组 1 条，没有分析价值。
#: `first_name`/`last_name`/`company`/`title`/`name`（曲目名、专辑名）都命中。
_HIGH_CARDINALITY_HINTS = (
    "first_name", "last_name", "full_name", "middle_name", "nickname",
    "company", "organization", "org_", "title", "subject", "headline",
    "name", "composer", "artist", "author", "lyric", "description", "summary",
)


#: 建议的对外类型**直接复用契约里的 `Suggestion`**，不在这里另定义一个同名
#: dataclass。踩过的坑：本模块原本自带一个 `Suggestion` dataclass，而
#: `app/contracts.py` 里也有一个同名的 pydantic 模型；`AskResponse.suggestions`
#: 拿到 dataclass 实例会直接抛
#: `ValidationError: suggestions.0 Input should be a valid dictionary or instance of Suggestion`。
#: 两个同名类型必须保持同步本身就是隐患，干脆只留一个。
Suggestion = _ContractSuggestion


@dataclass
class SuggestionContext:
    """算建议需要的事实。全部来自本轮已经发生的事，不额外查询、不额外调模型。"""

    #: 本轮命中的结构化表名（按出现顺序，已去重）
    tables: list[str] = field(default_factory=list)
    #: 结果集列名（有 SQL 结果时）
    columns: list[str] = field(default_factory=list)
    #: 行数据。只取前若干行读**真实取值**，不做聚合推断。
    rows: list[dict[str, Any]] = field(default_factory=list)
    #: 本轮命中的文档标题与章节（非结构化链路）
    doc_titles: list[str] = field(default_factory=list)
    section_paths: list[str] = field(default_factory=list)
    #: 本轮是否发生了澄清；发生则给一条「换个口径」的回退建议
    clarified: bool = False
    #: 用户本轮问的原话，用来避免建议与刚问过的问题重复
    question: str = ""


_catalog_lock = threading.Lock()
_catalog_cache: dict[str, Any] = {}


def _catalog():
    """惰性加载结构目录，进程内缓存一次。

    复用 `app.nl2sql.schema.SchemaCatalog` 而不是自己读 JSON —— 表名与字段名的
    解析口径必须和真正生成 SQL 的那条链路完全一致，否则建议里会出现
    「schema_meta 里有、但 SQL 校验器不认」的字段。
    """
    with _catalog_lock:
        cached = _catalog_cache.get("catalog")
        if cached is not None:
            return cached
        try:
            from app.nl2sql.schema import SchemaCatalog

            catalog = SchemaCatalog.load()
        except Exception:  # noqa: BLE001 - 建议是附加功能，取不到就退化成没有建议
            catalog = None
        _catalog_cache["catalog"] = catalog
        return catalog


def reset_cache() -> None:
    """测试用：丢掉进程内缓存。"""
    with _catalog_lock:
        _catalog_cache.clear()


def _is_high_cardinality_name(name: str) -> bool:
    """列名看起来像「每行一个值」的高基数字段（姓名、公司、标题、曲名）。

    为什么用名字规则而不是数基数：`schema_meta.json` 里每列的 `sample_values`
    最多只给 8 个，是被截断的。`customer.country` 真值域约 24 个国家、样例里却
    只有 8 个 —— 数多样性会把真实类别列误判成高基数。`row_count` 可靠但只到表级，
    判断不到列级。所以只能用列名的语义特征，配合 `_CATEGORY_HINTS` 反向确认。
    """
    if any(hint in name for hint in _CATEGORY_HINTS):
        return False
    return any(hint in name for hint in _HIGH_CARDINALITY_HINTS)


def _is_dimension(column_name: str, column_type: str) -> bool:
    """这个字段适不适合「按它分组」。

    判断依据是结构（主外键命名 + 数值类型 + 自由文本 + 高基数文本），不是一张
    人工白名单 —— 白名单在换库时会静默失效，而结构规则不会。

    ⚠️ 高基数文本列（姓名、公司名、地址、曲目名）必须挡掉。
    早期实现只看「是不是文本」，于是把 `customer.first_name` 当成维度，问出
    「按客户名分组，客户分别有多少条？」—— 59 个客户 59 个名字，答案全是 1，
    是一条**没有分析价值但看起来很正常**的建议。
    """
    name = (column_name or "").lower()
    if not name or name in _NOT_DIMENSIONS:
        return False
    if name.endswith(_ID_SUFFIXES):
        return False
    if any(hint in name for hint in _FREETEXT_HINTS):
        return False
    #:「名字/标题/公司」这类列即使不是自由文本，基数也接近行数，分组没意义。
    if _is_high_cardinality_name(name):
        return False
    upper = (column_type or "").upper()
    return not any(upper.startswith(t) for t in _NUMERIC_TYPES)


def _table_label(name: str) -> str:
    cn = _TABLE_CN.get((name or "").lower())
    return cn if cn else f"{name} 表"


def _clean_label(description: str) -> str | None:
    """把字段说明压成一个能塞进问句的短标签；压不出来返回 None。

    只接受**短、且不含结构性描述**的说明。压不出来时调用方会放弃这条建议 ——
    这是刻意的：把 `support_rep_id` 这种字段名直接写进中文问句，
    读起来像程序日志，不如不出。
    """
    text = (description or "").strip()
    if not text:
        return None
    for hint in _STRUCTURAL_HINTS:
        if hint in text:
            return None
    # 只取第一句/第一段，且要足够短。切完仍带标点的丢掉。
    for sep in ("。", "；", "，", "、", "(", "（"):
        text = text.split(sep)[0].strip()
    if not text or len(text) > 12:
        return None
    if text.endswith(("。", "，", "；", "、", ".", ",")):
        return None
    return text


def _table_meta(table: str):
    catalog = _catalog()
    if catalog is None:
        return None
    try:
        return catalog.get_table(table)
    except Exception:  # noqa: BLE001
        return None


def _column_meta(table: str, column: str):
    meta = _table_meta(table)
    if meta is None:
        return None
    try:
        return meta.column(column)
    except Exception:  # noqa: BLE001
        return None


def _column_label(table: str, column: str) -> str | None:
    """字段的中文短标签。取不到干净的说明就返回 None，**不退回字段名**。"""
    col = _column_meta(table, column)
    if col is None:
        return None
    return _clean_label(col.description or "")


def _column_type(table: str, column: str) -> str:
    """字段的 SQL 类型字符串；取不到返回空串（当成非数值处理）。"""
    col = _column_meta(table, column)
    return (col.type or "") if col is not None else ""


def _measure_candidates(table: str, exclude: Iterable[str]) -> list[str]:
    """这张表里适合被问「总计」的数值字段。

    ⚠️ 必须同时排除主键与外键（名字以 `_id` 结尾）。早期实现只按类型判断，
    于是把 `support_rep_id` 当成"可求总计的度量"，问出
    「客户的开票客服编号总计是多少？」—— 对 ID 求和没有业务含义。
    """
    meta = _table_meta(table)
    if meta is None:
        return []
    blocked = {c.lower() for c in exclude}
    out: list[str] = []
    for col in meta.columns:
        name = col.name.lower()
        if name in blocked or col.is_primary_key:
            continue
        if name.endswith(_ID_SUFFIXES) or name in _NOT_DIMENSIONS:
            continue
        upper = (col.type or "").upper()
        if any(upper.startswith(t) for t in _NUMERIC_TYPES):
            out.append(col.name)
    return out


def _dimension_candidates(table: str, exclude: Iterable[str]) -> list[str]:
    """这张表里适合被问「按它分组」的类别字段。"""
    meta = _table_meta(table)
    if meta is None:
        return []
    blocked = {c.lower() for c in exclude}
    out: list[str] = []
    for col in meta.columns:
        name = col.name.lower()
        if name in blocked or col.is_primary_key:
            continue
        if _is_dimension(col.name, col.type):
            out.append(col.name)
    return out


def _sample_value(column: str, rows: Sequence[dict[str, Any]]) -> str | None:
    """从本轮结果行里取一个**真实出现过的**字段取值。

    只读前若干行 —— 值来自刚才那条 SQL 真的返回的数据，所以「只看「美国」的
    客户有多少」这类建议在数据层面一定答得出来。

    ⚠️ 调用方只能对**维度字段**调它，绝不能对度量字段调。
    聚合结果（如 `customer_count = 59`）不是某一行的标识符，拿它当筛选值会生成
    「只看「59」这个客户计数的客户有多少？」—— 这句话**恰好也能算出答案**（编号
    59 的客户有 1 个），不报错、不崩，但问的是另一个问题。这是最坏的一类错误：
    看起来完全正常。
    """
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        value = row.get(column)
        if value is None:
            continue
        text = str(value).strip()
        # 空串、纯数字、超长自由文本都不适合塞进问句里。
        # 纯数字多半是编号，写进中文问句里没有可读性。
        if not text or len(text) > 24 or text.isdigit():
            continue
        # 至少要含一个字母或汉字，排除 "-"、"N/A" 这类占位值。
        if not any(ch.isalpha() for ch in text):
            continue
        return text
    return None


def _looks_chinese(text: str) -> bool:
    """值里有没有汉字。英文取值（曲目名、专辑名）拼进中文问句会很生硬。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def suggest(context: SuggestionContext) -> list[Suggestion]:
    """按 context 组合建议。**纯函数**：不查库、不调模型、不发网络请求。"""
    out: list[Suggestion] = []
    seen_questions: set[str] = set()
    asked = (context.question or "").strip()

    def push(question: str, reason: str) -> None:
        text = " ".join((question or "").split())
        if not text or len(out) >= MAX_SUGGESTIONS:
            return
        if text in seen_questions:
            return
        # 与刚问过的问题一模一样就不要推了。
        if asked and text == asked:
            return
        seen_questions.add(text)
        out.append(Suggestion(question=text, reason=reason))

    for table in context.tables[:1]:
        label = _table_label(table)
        columns = [str(c) for c in context.columns]

        # ① 换一个度量：同表里另一个数值字段的总计。
        for measure in _measure_candidates(table, columns)[:1]:
            measure_label = _column_label(table, measure)
            if not measure_label:
                continue
            push(
                f"{label}的{measure_label}总计是多少？",
                f"沿用刚才的{label}，换成另一个数值字段",
            )

        # ② 换一个维度：同表里另一个类别字段的分组计数。
        for dimension in _dimension_candidates(table, columns)[:1]:
            dim_label = _column_label(table, dimension)
            if not dim_label:
                continue
            push(
                f"按{dim_label}分组，{label}分别有多少条？",
                f"沿用刚才的{label}，改成按字段分组",
            )

        # ③ 数据驱动：拿结果里真实出现过的一个取值追问。
        #
        # ⚠️ 三条硬约束，每条都对应一个真实踩过的坑：
        #   a) 只在**维度字段**上取值 —— 聚合结果（customer_count=59）不是标识符，
        #      拿它当筛选值会问出「编号 59 的客户有几个」，恰好答得出、看着正常，
        #      其实答的是另一个问题；
        #   b) 优先**中文取值** —— 「只看「For Those About To Rock」的…」不像人话；
        #   c) 问句里**不出现字段名** —— 那是给程序员看的，用户不知道什么叫 track_name。
        #      没有中文标签就干脆不推这条，宁缺毋滥。
        fallback_value: tuple[str, str] | None = None
        for column in columns:
            if not _is_dimension(column, _column_type(table, column)):
                continue
            value = _sample_value(column, context.rows)
            if not value:
                continue
            # 值本身带汉字就可以直接问，不需要字段的中文标签。
            if _looks_chinese(value):
                fallback_value = (value, "")
                break
            if fallback_value is None:
                fallback_value = (value, _column_label(table, column) or "")
        if fallback_value is not None:
            value, value_label = fallback_value
            # 值本身有汉字（「北京」「质保条款」）就不用再挂字段名，直接问；
            # 否则必须靠字段标签把它挂上去，且**绝不能把 `track_name` 这种
            # 字段名直接写进问句** —— 那是给程序员看的。
            question = (
                f"只看「{value}」的{label}有多少？"
                if not value_label
                else f"只看「{value}」的{label}有多少？"
                if _looks_chinese(value)
                else f"只看{value_label}为「{value}」的{label}有多少？"
            )
            if _looks_chinese(value) or value_label:
                push(question, "取值来自刚才的结果，不是凭空举的例子")

        # ④ 沿外键换一张相邻的表。
        neighbor = _neighbor_table(table, columns)
        if neighbor is not None:
            push(
                f"{_table_label(neighbor)}有多少个？",
                f"与刚才的{label}直接关联的表",
            )

    # ⑤ 非结构化：同一文档的另一个章节。
    for title, section in zip(context.doc_titles, context.section_paths):
        if not title or not section:
            continue
        push(
            f"《{title}》里{section}是怎么规定的？",
            "章节名来自刚才召回的片段",
        )
        break

    # ⑥ 兜底：本轮发生了澄清，给一个「重新选口径」的入口。
    if context.clarified and len(out) < MAX_SUGGESTIONS:
        push("换个口径重新问一次", "上一轮因信息不足被追问过，可以换一种说法")

    return out[:MAX_SUGGESTIONS]


def _neighbor_table(table: str, exclude_columns: Sequence[str]) -> str | None:
    """沿外键找一张直接相邻的表，返回表名。找不到返回 None。"""
    catalog = _catalog()
    if catalog is None:
        return None
    try:
        meta = catalog.get_table(table)
    except Exception:  # noqa: BLE001
        return None
    if meta is None:
        return None
    for fk in meta.foreign_keys:
        ref = (fk.ref_table or "").strip()
        if ref and ref.lower() != table.lower():
            return ref
    return None


def from_trace_and_rows(
    *,
    question: str,
    trace: Sequence[Any] | None,
    refs: Sequence[Any] | None,
    rows: Sequence[dict[str, Any]] | None,
    columns: Sequence[str] | None,
    clarified: bool,
) -> list[Suggestion]:
    """从 **本轮已经产出的 trace / refs / 结果行** 组装 context 并算建议。

    刻意只吃这三个东西：它们全都是链路里已经存在的事实。多一个数据源就多一处
    可能失败、可能过期的依赖 —— 而建议本身是锦上添花，不值得让主链路为它冒险。

    ⚠️ 调用方必须把整个调用包在 try/except 里：**建议失败绝不能影响答案返回**。
    """
    tables: list[str] = []
    doc_titles: list[str] = []
    section_paths: list[str] = []

    for ref in refs or []:
        kind = getattr(ref, "kind", None)
        if kind == "sql":
            name = getattr(ref, "table", None)
            if name and name not in tables:
                tables.append(name)
        elif kind == "doc":
            title = getattr(ref, "doc_title", None)
            section = getattr(ref, "section_path", None)
            if title and title not in doc_titles:
                doc_titles.append(title)
            if section and section not in section_paths:
                section_paths.append(section)

    # refs 为空时退回 trace 的 hit —— 两条来源同一套结构，但真出现差异时
    # 多一层兜底比少一层便宜。
    if not tables and not doc_titles:
        for step in trace or []:
            for ref in getattr(step, "hit", None) or []:
                kind = getattr(ref, "kind", None)
                if kind == "sql":
                    name = getattr(ref, "table", None)
                    if name and name not in tables:
                        tables.append(name)
                elif kind == "doc":
                    title = getattr(ref, "doc_title", None)
                    section = getattr(ref, "section_path", None)
                    if title and title not in doc_titles:
                        doc_titles.append(title)
                    if section and section not in section_paths:
                        section_paths.append(section)

    # ⚠️ 这里**刻意不做**「按结果列名挑表」的排序。
    #
    # 曾经加过一版：同一句话连问 5 次，建议时而挂 `customer`、时而挂 `invoice`，
    # 我判断是 refs 顺序不稳，就按「结果列命中哪张表最多」重排。
    # 后来查清楚了，**那个判断是错的**：
    #   模型每次生成的 SQL 并不相同 —— 有时 `FROM customer GROUP BY country`，
    #   有时 `FROM invoice GROUP BY billing_country`。两次的列名都是
    #   `country`/`customer_count`，按列名根本区分不了谁先谁后（实测重排后
    #   依然不稳定）。而那次 `FROM invoice` 是真换了表查，建议跟着挂到 invoice
    #   **本来就是对的**，不是 bug。
    # 结论：建议挂在哪张表，就该跟着这次真正执行的 SQL 走。SQL 变、建议变是
    # 一致行为，不是漂移。硬掰成固定表反而会让建议和刚给出的答案脱节。
    return suggest(
        SuggestionContext(
            tables=tables,
            columns=[str(c) for c in (columns or [])],
            rows=list(rows or []),
            doc_titles=doc_titles,
            section_paths=section_paths,
            clarified=clarified,
            question=question,
        )
    )


__all__ = [
    "MAX_SUGGESTIONS",
    "Suggestion",
    "SuggestionContext",
    "from_trace_and_rows",
    "reset_cache",
    "suggest",
]