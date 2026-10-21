"""normalizer.py —— 要素识别与改写（中级任务 1）。

**接口唯一真相**：``docs/要素识别与改写_接口_v0.1.md``。本文只做实现，不重复设计论证；
两者的对应关系在各类 / 各函数的注释里以「§n」标注。

一句话职责：把不标准的用户输入（业务别名 / 错别字 / 全角字符 / 中英混排 / 口语冗余 /
相对时间词）改写成标准问句，并把「改了什么、依据是什么」**逐字留痕** ——
改写记录作为 trace 第 1 步（``tool="normalizer"``）返回给前端，是可展示、可追溯的。

三层识别（§12.2，顺序即优先级，不可交换）：
    A 层  元数据派生词表（表名 / 中文说明）—— 确定性命中，零维护
    B 层  人工别名表（infra/schema_meta.aliases.zh.json）—— 确定性命中，一词一条
    C 层  语义兜底（向量召回 → 回库校验 → 采纳）—— 概率性，**只在 A/B 都没命中时启用**，
          由编排层注入 embedder（本模块不 import app.rag，保持零耦合，§12.4）。

**红线（§4.1 / §12.3）**：无法消解的输入必须什么都不改、如实记进 ``unresolved``，
交由澄清环节（中级任务 3）处理。改错比不改更糟 —— rob-11 那类「禁止硬猜」的
底线用例就是被这条红线保护的。

本模块是纯函数风格、无副作用、不连库、不加载模型；import 期不做任何 IO。
"""

from __future__ import annotations

import os
import json
import re
import time as _time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from functools import lru_cache
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# 路径（均为仓库内固定位置；import 期不读）
# ---------------------------------------------------------------------------

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ALIASES_PATH = os.path.join(_ROOT, "infra", "schema_meta.aliases.zh.json")
SCHEMA_META_PATH = os.path.join(_ROOT, "infra", "schema_meta.json")
DESC_PATH = os.path.join(_ROOT, "infra", "schema_meta.descriptions.zh.json")
MANIFEST_PATH = os.path.join(_ROOT, "data", "corpus", "manifest.json")


def _metadata_paths() -> tuple[str, str, str | None]:
    """Resolve schema resources together so an isolated runtime uses its own vocabulary."""
    explicit_schema = os.environ.get("SCHEMA_META_PATH", "").strip()
    schema_path = explicit_schema or SCHEMA_META_PATH
    schema_parent = os.path.dirname(os.path.abspath(schema_path))
    schema_stem = os.path.splitext(os.path.basename(schema_path))[0]
    explicit_desc = os.environ.get("SCHEMA_META_DESCRIPTIONS_PATH", "").strip()
    desc_path = explicit_desc or (
        os.path.join(schema_parent, schema_stem + ".descriptions.zh.json")
        if explicit_schema else DESC_PATH
    )
    explicit_aliases = os.environ.get("SCHEMA_META_ALIASES_PATH", "").strip()
    aliases_path = explicit_aliases or (
        os.path.join(schema_parent, schema_stem + ".aliases.zh.json")
        if explicit_schema else ALIASES_PATH
    )
    return schema_path, desc_path, aliases_path

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[A-Za-z]{2,}")


# ---------------------------------------------------------------------------
# 数据结构（§4；刻意不进 contracts.py，见接口文档 §4 开头的论证）
# ---------------------------------------------------------------------------


class ChangeKind(str, Enum):
    """改动类型，取值与 eval/cases/robust_cases.jsonl 的 degradation 字段对齐（§4.1）。"""

    ALIAS = "alias"
    TYPO = "typo"
    FULLWIDTH = "fullwidth"
    MIXED_LANG = "mixed_lang"
    FILLER = "filler"
    TIME = "time"


class TextChange(BaseModel):
    """一处改写的完整记录 —— 可解释性的最小单元（§4.2）。"""

    model_config = ConfigDict(extra="forbid")

    kind: ChangeKind
    before: str
    after: str
    span: tuple[int, int]  # 相对**原文**的字符位置，供前端高亮
    reason: str
    source: str


class NormalizedEntity(BaseModel):
    """识别到的要素（§4.3）。对账必须按 via 分组，不得合并报数。"""

    model_config = ConfigDict(extra="forbid")

    surface: str
    canonical: str
    kind: str  # "table" / "column" / "value" / "doc"
    via: str  # "exact" / "desc" / "alias" / "semantic"，可靠性依次递减


class TimeExpression(BaseModel):
    """时间表达式（§4.4 / §6）：归一化区间一律**左闭右开**，落在 SQL WHERE 上无需换算。"""

    model_config = ConfigDict(extra="forbid")

    surface: str
    kind: str  # "day" / "month" / "quarter" / "year" / "range"
    offset: int
    start: str  # ISO 日期
    end: str  # ISO 日期，**开区间端点**
    anchor: str  # 计算时用的基准，随结果留存（§6.1）


class NormalizeResult(BaseModel):
    """主输出（§4.5）。original 原样留存，normalized 交给路由与下游工具。"""

    model_config = ConfigDict(extra="forbid")

    original: str
    normalized: str
    changes: list[TextChange] = []
    entities: list[NormalizedEntity] = []
    times: list[TimeExpression] = []
    unresolved: list[str] = []
    degraded_kinds: list[ChangeKind] = []
    elapsed_ms: float = 0.0


class AliasTable(BaseModel):
    """人工别名表（§7.2），存于 infra/schema_meta.aliases.zh.json ——
    与 schema_meta.json（生成物）完全分离，重跑生成脚本不会把它抹掉（§10）。

    ``notes`` 是文件自带的非语义说明（取值核实日期等），不参与匹配。
    """

    model_config = ConfigDict(extra="forbid")

    version: str
    updated_at: str
    notes: list[str] = []
    tables: dict[str, list[str]] = {}
    table_mentions: dict[str, list[str]] = {}
    columns: dict[str, list[str]] = {}
    values: dict[str, dict[str, list[str]]] = {}
    docs: dict[str, list[str]] = {}


# ---------------------------------------------------------------------------
# 加载：别名表 + 元数据派生词表（带进程内缓存，按文件 mtime 失效）
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _load_alias_table_cached(path: str, mtime_ns: int) -> AliasTable:
    import json

    with open(path, encoding="utf-8") as f:
        return AliasTable.model_validate(json.load(f))


def load_alias_table(path: str | None = None) -> AliasTable:
    schema_path, _, configured_path = _metadata_paths()
    explicit_schema = os.environ.get("SCHEMA_META_PATH", "").strip()
    explicit_aliases = os.environ.get("SCHEMA_META_ALIASES_PATH", "").strip()
    if explicit_schema and not explicit_aliases and path is None:
        # The checked-in alias asset is authored for Chinook. Never silently reuse
        # it for an explicitly selected database with a different identity.
        try:
            with open(schema_path, encoding="utf-8") as stream:
                database = str(json.load(stream).get("database") or "").casefold()
        except (OSError, ValueError):
            database = ""
        if database != "chinook" and (
            not configured_path or not os.path.isfile(configured_path)
        ):
            return AliasTable(version="runtime-no-matching-aliases", updated_at="not-provided")
    p = path or configured_path
    if p is None or not os.path.isfile(p):
        if os.environ.get("SCHEMA_META_PATH", "").strip():
            return AliasTable(version="runtime-no-aliases", updated_at="not-provided")
        raise FileNotFoundError(p or ALIASES_PATH)
    st = os.stat(p)
    return _load_alias_table_cached(os.path.abspath(p), st.st_mtime_ns)


@lru_cache(maxsize=8)
def _load_json_cached(path: str, mtime_ns: int) -> dict:
    import json

    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_json(path: str) -> dict:
    st = os.stat(path)
    return _load_json_cached(os.path.abspath(path), st.st_mtime_ns)


@dataclass
class Vocab:
    """元数据派生词表（§12.2 A 层）：全部来自库结构与语料清单，零人工维护。

    table_desc_alias / doc_titles 只用于**识别**（record-only），不改写文本 ——
    这些说法本身就是「标准说法」，路由（routing_signals）同样能从元数据派生它们，
    改写反而制造无意义的 diff；需要改写的「俗称」只属于 B 层别名表（§2.3 约定）。
    """

    tables: list[str] = field(default_factory=list)
    table_desc_alias: dict[str, str] = field(default_factory=dict)  # "客户" -> "customer"
    doc_titles: dict[str, str] = field(default_factory=dict)  # "…维保服务合同" -> "KB-001"
    #: 列说明原文，键形如 "projects.procurement_way"。只供错别字层当
    #: 「已知正确说法」参考词库用（见 `_pass_typo`），不参与任何改写。
    column_desc: dict[str, str] = field(default_factory=dict)


def _desc_keyword(desc: str) -> str:
    """从中文说明里取主干词：「艺术家（乐队/歌手）表。」→「艺术家」。

    只取首个标点前的片段、剥掉「表」尾 —— 派生规则刻意保守，宁可少收录。
    """
    #   分隔符含冒号：列说明常写成「采购方式：公开招标、竞争性磋商…」，
    #   冒号后是取值枚举而不是词的一部分。不切冒号会把整句当主干词，
    #   词库里就没有「采购方式」这个真正该认得说法。
    head = re.split(r"[。，；：:,;（(/]", desc, maxsplit=1)[0].strip()
    if head.endswith("表"):
        head = head[:-1]
    return head


#: 说明文件里「也叫…」「简称…」「即…」「又称…」引出的同义说法。
#: 为什么要专门抽这些：主干词只取首个标点前那一段，而业务上对同一张表的
#: 叫法往往不止一个——采购单位这张表，公告原文里写的是「采购单位」，用户问的
#: 却是「采购人」；中标结果表，用户说的是「中标」。实测这两个词不在词表里时，
#: 「按采购人统计中标金额合计」会被澄清器判成没说统计对象而反问
#: （ranking_without_object / measure_without_object），整题不产出 SQL。
#: 这些说法本来就是说明文件里写明的标准叫法，抽进来不引入任何元数据之外的知识。
_ALIAS_LEAD = re.compile(r"(?:也叫|又称|简称|即是|即|或称)[：: ]*")
_ALIAS_ITEM = re.compile(r"^([^\s、，,；;（）()。]{2,12})")
#: 别名里不该出现的词：它们是说法的引导词或泛指，抽进来会让任何问句都命中。
_ALIAS_STOPWORDS = frozenset(
    {"中标供应商", "采购项目", "采购公告", "采购单位", "采购代理机构", "标段", "本表"}
)


def _desc_aliases(desc: str) -> list[str]:
    """从说明的第二句起抽出「也叫…」引出的同义说法。

    只扫**主干词之后**的片段，避免把主干词自己再收一遍；每个说法还要过
    `_ALIAS_STOPWORDS` 与「不能被别的说法包含」两道，保持词表干净。
    """
    rest = re.split(r"[。，；,;（(/]", desc, maxsplit=1)
    if len(rest) < 2:
        return []
    found: list[str] = []
    for lead in _ALIAS_LEAD.finditer(desc):
        if lead.start() < len(rest[0]):
            continue
        tail = desc[lead.end():]
        for chunk in re.split(r"[、，,；;。]", tail):
            match = _ALIAS_ITEM.match(chunk.strip())
            if not match:
                break
            word = match.group(1).strip()
            if not word or word in _ALIAS_STOPWORDS or word in found:
                break
            found.append(word)
    return found


def load_vocab() -> Vocab:
    schema_path, desc_path, _ = _metadata_paths()
    sm = _load_json(schema_path)
    vocab = Vocab(tables=[t["table"] for t in sm.get("tables", [])])
    try:
        dz = _load_json(desc_path)
        table_descriptions = dz.get("tables") or {}
        if isinstance(table_descriptions, dict):
            description_rows = table_descriptions.items()
        elif isinstance(table_descriptions, list):
            description_rows = (
                (row.get("table"), row.get("description", ""))
                for row in table_descriptions if isinstance(row, dict)
            )
        else:
            description_rows = ()
        # 分两遍：先收齐所有主干词，再收别名。别名过滤要拿**全部**主干词来比对
        # （见下），一遍扫下来后面的表还看不见，会漏掉跨表撞词。
        keywords: dict[str, str] = {}
        for name, desc in description_rows:
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(desc, str) or not desc.strip():
                continue
            name = name.strip()
            kw = _desc_keyword(desc)
            if len(kw) >= 2:
                vocab.table_desc_alias.setdefault(kw, name)
                keywords[name] = kw
        for name, desc in description_rows:
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(desc, str) or not desc.strip():
                continue
            name = name.strip()
            kw = keywords.get(name)
            if kw is None:
                continue
            for alias in _desc_aliases(desc):
                # 只补「不会误伤」的说法：等于主干词、过短、或与**任何**一张表的
                # 主干词互为前缀的，一律不收录——一个词同时指向两张表，澄清器
                # 拿它判断「用户在说哪个对象」时就等于没判断。
                # 判据刻意用「互为前缀」而不是「互相包含」：包含关系里，用户在
                # 长说法里说出短词是**正常**的（「采购项目」里含「项目」，
                # 用户确实在说项目），挡掉会漏真别名；而前缀关系（「采购」↔
                # 「采购项目」）才是真正的指向不明。
                if len(alias) < 2 or alias == kw:
                    continue
                if any(
                    alias.startswith(other) or other.startswith(alias)
                    for other in keywords.values()
                ):
                    continue
                vocab.table_desc_alias.setdefault(alias, name)
        # 列说明单独收一份，只给错别字层当参考词库（键 = 表名.列名）
        columns_block = dz.get("columns") or {}
        if isinstance(columns_block, dict):
            for key, value in columns_block.items():
                if isinstance(key, str) and isinstance(value, str) and value.strip():
                    vocab.column_desc[str(key).strip()] = value.strip()
    except OSError:
        pass  # 说明文件缺失时 A 层退化为纯表名匹配，不致命
    runtime_corpus = os.environ.get("RAG_CORPUS_DIR", "").strip()
    runtime_manifest = os.environ.get("RAG_CORPUS_MANIFEST_PATH", "").strip()
    if runtime_manifest:
        try:
            mf = _load_json(runtime_manifest)
            for doc in mf.get("documents", []):
                stem = os.path.splitext(str(doc.get("file", "")))[0]
                title = str(doc.get("title") or re.sub(r"^KB-\d+_", "", stem))
                if title:
                    vocab.doc_titles.setdefault(title, str(doc.get("id") or stem))
        except (OSError, ValueError, AttributeError):
            pass
    elif runtime_corpus:
        for filename in sorted(os.listdir(runtime_corpus)) if os.path.isdir(runtime_corpus) else []:
            stem, extension = os.path.splitext(filename)
            if extension.lower() not in {".pdf", ".docx", ".doc", ".md", ".txt"}:
                continue
            title = re.sub(r"^KB-\d+_", "", stem)
            if title:
                vocab.doc_titles.setdefault(title, stem)
    else:
        try:
            mf = _load_json(MANIFEST_PATH)
            for doc in mf.get("documents", []):
                stem = os.path.splitext(str(doc.get("file", "")))[0]
                title = re.sub(r"^KB-\d+_", "", stem)
                if title:
                    vocab.doc_titles.setdefault(title, str(doc.get("id", "")))
        except OSError:
            pass
    return vocab


# ---------------------------------------------------------------------------
# 编辑引擎：改写作用于「工作串」，span 换算回**原文**坐标（前端高亮要用）
# ---------------------------------------------------------------------------


class _Edit:
    __slots__ = ("start", "end", "replacement", "change_kwargs", "entity")

    def __init__(self, start, end, replacement, change_kwargs, entity=None):
        self.start = start
        self.end = end
        self.replacement = replacement
        self.change_kwargs = change_kwargs
        self.entity = entity


class _Editor:
    """维护「当前串 → 原文」的坐标映射；apply 从右往左执行，保证坐标不失效。"""

    def __init__(self, text: str):
        self.text = text
        self._orig = list(range(len(text)))  # 当前串每个字符对应的原文下标
        self.changes: list[TextChange] = []
        self.entities: list[NormalizedEntity] = []

    def apply(self, edits: list[_Edit]) -> None:
        for e in sorted(edits, key=lambda x: x.start, reverse=True):
            before = self.text[e.start : e.end]
            ostart = self._orig[e.start]
            oend = self._orig[e.end - 1] + 1
            if e.replacement is not None and e.replacement != before:
                self.changes.append(
                    TextChange(span=(ostart, oend), before=before, after=e.replacement, **e.change_kwargs)
                )
                self.text = self.text[: e.start] + e.replacement + self.text[e.end :]
                self._orig = (
                    self._orig[: e.start]
                    + [ostart] * len(e.replacement)
                    + self._orig[e.end :]
                )
            if e.entity is not None:
                self.entities.append(e.entity)


def _find_all(text: str, term: str) -> list[int]:
    out, i = [], text.find(term)
    while i != -1:
        out.append(i)
        i = text.find(term, i + 1)
    return out


def _is_cjk(ch: str) -> bool:
    """是否汉字。只判常用汉字区，标点/字母/数字一律不算。"""
    return bool(ch) and "\u4e00" <= ch <= "\u9fff"


def _right_boundary_ok(
    text: str,
    start: int,
    term: str,
    long_terms: "set[str] | frozenset[str] | None" = None,
) -> bool:
    """片段右侧是否构成词边界 —— 决定它能不能被当成独立词改写。

    **为什么需要它**（2026-10-18 实测）：中文没有空格分词，别名表与错别字表里的
    两字/三字词只要碰巧是更长词的构件，就会被 `_find_all` 当独立词切出来：

        拟采用单一来源采购|方式…   →「采购人」命中前两字，改写成「单一来源buyers式」
        达到公开招标数额|标准…     →「招标数」改写成 buyers，成了「公开buyers额标准」

    替换结果里出现「buyers式」「buyers额」这种产物 —— 词名插进了词中间，语义彻底变了。

    **判据（2026-10-18 第二版，前两版均已废弃）**：

        右侧不是汉字              → 独立词，放行
        右侧是汉字，但「片段+右侧若干汉字」命中了已知更长词 → 是更长词的前缀，拒绝
        右侧是汉字，且找不到任何更长已知词                    → 拒绝

    最后一条是关键，也是前两版踩过的地方：
      · 第一版按「量词白名单」放行（数/量/个/条）→ 「曲风|的名字」「流派|的名字」
        里的「的」不在白名单，正常别名全被挡，app/test_normalizer.py 四条用例当场失败。
      · 第二版改成「只要找不到更长已知词就放行」→ 词表外的法规术语
        （「招标数额标准」不在任何表名/列名词表里）接不住，「招标数」照样被误改。

    第三版把默认值翻过来：**右侧紧跟汉字时，除非能证明它是某个更长已知词的构件，
    否则一律视为非独立词、不改写。** 中文里一个词后面直接粘汉字，绝大多数情况是
    更长词的前半截；放过它可能改错（把「采购方式」改成「采购buyers式」，整题作废），
    挡掉它最多只是不改写（下游按原文理解，代价小得多）。宁可漏，不可误。

    `long_terms` 传 None 时退化为「紧跟汉字即拒」，与正确行为一致
    （旧注释说这是"保守行为"，其实它就是正确行为）。

    左边界刻意不查：中文里「的采购人」「，采购人」「3 个采购人」左边都可能是
    汉字或标点，而左邻多为助词；右侧能挡住踩到的全部实例，先只做右侧。
    """
    end = start + len(term)
    if end >= len(text):
        return True
    nxt = text[end]
    if not _is_cjk(nxt):
        return True
    # ① 右侧是虚词/助词：它在语法上标记「前面的词到此为止」，片段必然是独立词。
    #    「曲风|的名字」「采购人|或者」都属这一类，不能因为右边是汉字就挡。
    if nxt in _FUNCTION_CHARS:
        return True
    # ② 右侧是实词首字：它极可能与片段粘成更长词（采购|方式、招标|数额），
    #    只有能证明「片段+右侧」构不成任何已知更长词时才放行。
    if not long_terms:
        return False
    for n in range(1, 7):
        if end + n > len(text):
            break
        if not _is_cjk(text[end + n - 1]):
            break
        if text[start:end + n] in long_terms:
            return False
    return False


#: 虚词 / 助词 / 语法后缀 —— 紧跟别名右侧时不影响别名的独立性。
#:
#: 收词原则：只收**不会与前面的名词粘成新词**的字。中文里这些字跟在名词后面
#: 只起连接或标记作用（的/或/和/与/及/等/是/有/在/为/有/把/被/给/对/向/从）。
#: 反例（绝不能收）：数/量/个/条/份/次/额/金/名 —— 它们是实词首字，
#: 「招标|数|额标准」的「数」与片段粘成了更长术语，收了就会误改。
_FUNCTION_CHARS = frozenset("的或和与及等是在为有把被给对向从该其这那每个各都也就又还并而但因所以之掉了吗呢吧么")


def _find_alias(
    text: str,
    term: str,
    long_terms: "set[str] | frozenset[str] | None" = None,
) -> list[int]:
    """别名专用查找：只返回右侧构成词边界的位置（判据见 `_right_boundary_ok`）。"""
    return [
        i
        for i in _find_all(text, term)
        if _right_boundary_ok(text, i, term, long_terms)
    ]


def _longer_known_terms(table: AliasTable, vocab: Vocab) -> set[str]:
    """收集「已知的更长词」，供 `_right_boundary_ok` 判断构词吞并。

    来源都是系统本来就认识、不是为本次改动新加的：
      ① 别名表自身的全部条目（表别名 + 值级别名）—— 「招标公告」在此列，
         于是「招标公告」不会被当作「招标」的独立出现；
      ② 文档题名（manifest 派生的知识库文档名）。

    只保留长度 ≥ 3 的词：别名本身多为两字，更长的两字词不存在，
    留着只会让循环白跑。
    """
    terms: set[str] = set()
    for aliases in table.tables.values():
        terms.update(aliases)
    for mapping in table.values.values():
        for aliases in mapping.values():
            terms.update(aliases)
    terms.update(table.table_mentions)
    terms.update(getattr(vocab, "doc_titles", {}) or {})
    return {t for t in terms if len(t) >= 3}


def _collect(edits: list[_Edit], start: int, end: int) -> bool:
    """判定 [start,end) 是否与已有编辑重叠（occupied 判定）。"""
    return any(not (end <= e.start or start >= e.end) for e in edits)


# ---------------------------------------------------------------------------
# 各处理 pass
# ---------------------------------------------------------------------------

#: 只转写**全角字母/数字**（FF10–FF19 / FF21–FF3A / FF41–FF5A）。
#: 全角标点（，？！）不动 —— 它们是中文行文的一部分，转了只会弄脏标准问句。
_FW_ALNUM_RE = re.compile(r"[Ｆ-Ｚｆ-ｚ０-９]+")


def _pass_fullwidth(editor: _Editor, kinds: set[ChangeKind]) -> None:
    edits: list[_Edit] = []
    for m in _FW_ALNUM_RE.finditer(editor.text):
        fixed = unicodedata.normalize("NFKC", m.group(0))
        if fixed == m.group(0):
            continue
        edits.append(
            _Edit(
                m.start(),
                m.end(),
                fixed,
                dict(kind=ChangeKind.FULLWIDTH, reason="全角字母/数字转半角", source="rule:fullwidth"),
            )
        )
    if edits:
        editor.apply(edits)
        kinds.add(ChangeKind.FULLWIDTH)


#: 口语冗余。刻意只收「去掉不损义」的寒暄/语气/客套成分；
#: 「最多的那个」这类**承载语义**的口语不算 filler（rob-11 红线：不许硬猜）。
#: ⚠️ 写法教训：`帮我?一下?` 的真实语义是「帮+我?+**一**+**下**?」——一/下是必需字符
#: （量词只作用于紧邻的一个字符），并不是「(帮我)?(一下)?」。多字口语词必须显式枚举。
#: ⚠️ 「哈」不能裸收：它是专有名词的构词字（哈尔滨、哈密），裸收会把
#: 「哈尔滨工程大学」改成「尔滨工程大学」，取值过滤整条失效。故只认句末/标点前的「哈」。
_FILLER_RE = re.compile(
    r"(麻烦|劳驾|拜托了?|帮我一下|帮我|帮一下|帮个忙|查一下|查查看|查查|"
    r"看一下|看下|看看|就是那个|就是说|那个……+|……+|谢谢|多谢|辛苦了|"
    r"哈(?=[\s，,。！？!?；;、]|$)|[呀嘛哦嗯呃唉])+"
)


def _pass_filler(editor: _Editor, kinds: set[ChangeKind]) -> None:
    edits: list[_Edit] = []
    for m in _FILLER_RE.finditer(editor.text):
        edits.append(
            _Edit(
                m.start(),
                m.end(),
                "",
                dict(kind=ChangeKind.FILLER, reason="口语冗余（寒暄/语气/客套），不影响语义", source="rule:filler"),
            )
        )
    if not edits:
        return
    editor.apply(edits)
    kinds.add(ChangeKind.FILLER)
    # 收尾清理：去掉被掏空后残留的孤立标点/空格。同样走编辑引擎，
    # 保证「当前串 → 原文」的坐标映射不被破坏（span 高亮依赖它）。
    tidy: list[_Edit] = []
    _FILLER_TIDY = [
        (re.compile(r"^[，,、；;。.\s]+"), ""),  # 开头残留标点
        (re.compile(r"[，,、；;。\s]+$"), ""),  # 结尾残留标点
        (re.compile(r"([，,、；;。？！?!.])\1+"), r"\1"),  # 重复标点
        (re.compile(r"\s+([，,、；;。？！?!.])"), r"\1"),  # 标点前多余空格
        (re.compile(r"\s{2,}"), " "),  # 连续空格
    ]
    for rx, repl in _FILLER_TIDY:
        for m in rx.finditer(editor.text):
            if m.group(0) == repl:
                continue
            tidy.append(
                _Edit(
                    m.start(),
                    m.end(),
                    repl,
                    dict(kind=ChangeKind.FILLER, reason="口语冗余清除后的标点收尾", source="rule:filler"),
                )
            )
    if tidy:
        editor.apply(tidy)


def _book_title_spans(text: str) -> list[tuple[int, int]]:
    """书名号《…》覆盖的区间。

    **为什么表别名要让路**（2026-10-18 实测）：书名号里是**文档题名**，
    整段是一个整体对象。可表别名在前一步先把里面的词换掉了：

        《颍东区食用菌菌种设备采购项目（三次）公开招标公告》
      → 《颍东区食用菌菌种设备projects（三次）公开announcements》

    题目问的是「这份公告里写的预算金额」，被换成 projects/announcements 之后
    既丢了文档身份，又让引擎以为要查表。政采 14 道文档题里有 4 道是这么坏的。

    判据只认《…》这一种显式标记 —— 中文里没有别的符号能无歧义地圈定题名，
    而题目作者写《》的意图正是「这是一份文档的名字」。
    """
    spans: list[tuple[int, int]] = []
    start = text.find("《")
    while start >= 0:
        end = text.find("》", start + 1)
        if end < 0:
            break
        spans.append((start, end + 1))
        start = text.find("《", end + 1)
    return spans


def _pass_alias(editor: _Editor, table: AliasTable, vocab: Vocab, kinds: set[ChangeKind]) -> None:
    """B 层（人工别名，命中即改写）+ A 层（表名 / 中文说明 / 文档题名，识别留痕不改写）。"""
    text = editor.text
    edits: list[_Edit] = []
    # 书名号内的整段题名保护区：表别名不得在此区间内改写（见 _book_title_spans）。
    title_spans = _book_title_spans(text)
    # 「已知的更长词」集合：只算一次，供下面两处边界判断共用。
    longer_terms = _longer_known_terms(table, vocab)

    def in_title(start: int, end: int) -> bool:
        """命中区间是否落在书名号内 —— 题名整段受保护，任何层都不得改写。"""
        return any(a <= start and end <= b for a, b in title_spans)

    def add(start, end, replacement, kind, via, canonical, ekind, source, reason, surface):
        entity = NormalizedEntity(surface=surface, canonical=canonical, kind=ekind, via=via)
        ck = dict(kind=kind, reason=reason, source=source)
        edits.append(_Edit(start, end, replacement, ck, entity))

    # 1) 表别名（改写为表名）—— 长词优先，避免「消费方」被「消费」截胡
    #    `_find_alias` 额外要求右侧构成词边界：否则「采购人」会把
    #    「单一来源采购方式」切成「单一来源buyers式」（2026-10-18 实测）。
    #    书名号内的题名整段跳过（见 _book_title_spans）。
    for tbl, aliases in table.tables.items():
        for term in sorted(set(aliases), key=len, reverse=True):
            for i in _find_alias(text, term, longer_terms):
                if in_title(i, i + len(term)):
                    continue
                if _collect(edits, i, i + len(term)):
                    continue
                add(i, i + len(term), tbl, ChangeKind.ALIAS, "alias", tbl, "table",
                    f"alias:{tbl}", f"别名表 {tbl} 条目命中", term)
                kinds.add(ChangeKind.ALIAS)
    # Some phrases identify a table but also carry the requested metric. Keep
    # their surface text intact so downstream SQL generation retains that intent.
    for tbl, terms in table.table_mentions.items():
        for term in sorted(set(terms), key=len, reverse=True):
            for i in _find_all(text, term):
                if in_title(i, i + len(term)):
                    continue
                if _collect(edits, i, i + len(term)):
                    continue
                add(i, i + len(term), None, ChangeKind.ALIAS, "mention", tbl, "table",
                    f"mention:{tbl}", f"识别表实体但保留指标说法：{term}", term)
    # 2) 值级别名（改写为库中真实取值，§7）
    for col, mapping in table.values.items():
        for value, aliases in mapping.items():
            for term in sorted(set(aliases), key=len, reverse=True):
                for i in _find_all(text, term):
                    if in_title(i, i + len(term)):
                        continue
                    if _collect(edits, i, i + len(term)):
                        continue
                    add(i, i + len(term), value, ChangeKind.ALIAS, "alias", value, "value",
                        f"alias:{col}={value}", f"值级别名 {col} 命中", term)
                    kinds.add(ChangeKind.ALIAS)
    # 3) 列别名（改写为列名）
    for col, aliases in table.columns.items():
        colname = col.split(".", 1)[-1]
        for term in sorted(set(aliases), key=len, reverse=True):
            for i in _find_all(text, term):
                if _collect(edits, i, i + len(term)):
                    continue
                add(i, i + len(term), colname, ChangeKind.ALIAS, "alias", colname, "column",
                    f"alias:{col}", f"列别名 {col} 命中", term)
                kinds.add(ChangeKind.ALIAS)
    # 4) 表英文名原样出现（via=exact，不改写）
    for tbl in vocab.tables:
        for m in re.finditer(r"\b" + re.escape(tbl) + r"\b", text):
            if _collect(edits, m.start(), m.end()):
                continue
            add(m.start(), m.end(), None, ChangeKind.ALIAS, "exact", tbl, "table",
                "exact:table", "表名原样出现", tbl)
    # 5) 表中文说明主干词（via=desc，不改写 —— 见 Vocab 注释）
    for kw, tbl in sorted(vocab.table_desc_alias.items(), key=lambda x: len(x[0]), reverse=True):
        for i in _find_all(text, kw):
            if _collect(edits, i, i + len(kw)):
                continue
            add(i, i + len(kw), None, ChangeKind.ALIAS, "desc", tbl, "table",
                "desc:table", f"元数据说明「{kw}」命中", kw)
    # 6) 文档别名 / 题名（record-only：改写成 KB 编号会弄丢路由的文档实体词，§2.3）
    doc_map: dict[str, str] = {}
    for did, aliases in table.docs.items():
        for a in aliases:
            doc_map.setdefault(a, did)
    for title, did in vocab.doc_titles.items():
        doc_map.setdefault(title, did)
    for term, did in sorted(doc_map.items(), key=lambda x: len(x[0]), reverse=True):
        for i in _find_all(text, term):
            if _collect(edits, i, i + len(term)):
                continue
            via = "exact" if term in vocab.doc_titles else "alias"
            add(i, i + len(term), None, ChangeKind.ALIAS, via, did, "doc",
                f"doc:{did}", f"文档「{did}」命中", term)

    if edits:
        editor.apply(edits)


def _lev1(a: str, b: str) -> bool:
    """错别字判定（保守策略）：等长、**首字相同**、其余仅差一字。

    首字相同是刻意的收紧：错别字多发生在词的中后段（艺术佳→艺术家、单介→单价），
    而放开首字会大面积误伤正常词（实测：『多少行』的『少行』距曲风『流行』仅差一字
    → 会被改成 Pop）。宁可漏纠、不可错改（§4.1）。
    """
    return (
        len(a) == len(b)
        and bool(a) and a[0] == b[0]
        and sum(x != y for x, y in zip(a, b)) == 1
    )


#: 高频虚字。含这些字的片段不参与错别字纠错 —— 它们是句子骨架（「方法**是**什么」），
#: 跨词边界的片段（法是→法国）极易撞上值别名（法/法国）造成毁灭性误改，
#: 实测会把路由信号词「方法」毁掉、整条路由翻转。
_STOP_CHARS = set("的是了不我这有在人他她它和与就都也很呢吧啊吗么")


def _spans_touch(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """两个区间是否有任何交叠（含部分重叠，不只是完全包含）。"""
    return not (a_end <= b_start or b_end <= a_start)


def _extends_to_longer_known(
    text: str, start: int, end: int, pairs: dict[str, tuple[str, str]]
) -> bool:
    """片段能否在原位置向左/右延伸成另一个更长的已知词。

    用于在错别字层挡掉「长词被切出碎片」的误改：
    「采购项目」切成「采购项」后与「采购人」只差一字，但它左右各接一个字
    就是已知词「采购项目」，说明它是一个长词的中段而不是独立的词。
    """
    longest = max((len(term) for term in pairs), default=0)
    if longest <= end - start:
        return False
    for left in range(max(0, start - longest + 1), start + 1):
        for right in range(end, min(len(text), left + longest) + 1):
            if right - left <= end - start:
                continue
            if text[left:right] in pairs:
                return True
    return False


def _pass_typo(editor: _Editor, table: AliasTable, vocab: Vocab, kinds: set[ChangeKind],
               unresolved: list[str]) -> None:
    """错别字层：对**未被占用**的片段，与已知词做「等长差一字」比对（§rob-05/rob-09）。

    候选有歧义时（两个不同目标都只差一字）不采纳 —— 改错比不改更糟（§4.1）。
    """
    text = editor.text
    known_entities = {(entity.canonical, entity.kind) for entity in editor.entities}
    protected_mentions = [
        (start, start + len(term))
        for terms in table.table_mentions.values()
        for term in terms
        for start in _find_all(text, term)
    ]
    # 前几层（别名层、描述词层）已经认出来的实体，其**原样片段**同样受保护。
    # 这些片段已经被赋予了一个明确的对象，错别字层再去改写它们，等于用
    # 「差一字」的模糊证据推翻「完全相等」的确切证据。
    # 实测事故：「采购项目」里的「采购项」被描述词层认出 projects（「项目」），
    # 错别字层又把它当「采购人」的错别字改写成 buyers，于是同一个短语
    # 产出了两个互相冲突的对象，「政采库里一共有多少个采购项目？」最后查了 buyers。
    for entity in editor.entities:
        surface = getattr(entity, "surface", "") or ""
        if len(surface) < 2:
            continue
        for start in _find_all(text, surface):
            protected_mentions.append((start, start + len(surface)))
    # 候选词库：全部 zh 说法 -> (canonical, kind)
    pairs: dict[str, tuple[str, str]] = {}
    for tbl, aliases in table.tables.items():
        for a in aliases:
            pairs[a] = (tbl, "table")
    for col, aliases in table.columns.items():
        for a in aliases:
            pairs[a] = (col.split(".", 1)[-1], "column")
    for mapping in table.values.values():
        for value, aliases in mapping.items():
            for a in aliases:
                pairs[a] = (value, "value")
    for kw, tbl in vocab.table_desc_alias.items():
        pairs.setdefault(kw, (tbl, "table"))
    # 列说明的主干词也要进词库。理由：这些词是**已知的正确说法**，
    # 进了词库才能被 `_extends_to_longer_known` 认出来，从而挡住
    # 「长词被切出碎片」的误改。
    # 实测：「采购方式」（projects.procurement_way 的说明主干词）被切成
    # 「采购方」，因与「采购人」只差一字，被改写成 buyers；
    # 于是「按采购方式分组，各自的中标金额平均值是多少？」挂了 buyers 对象，
    # 生成器顺着 buyers 编出 `awards.supplier_id = buyers.buyer_id` 这种
    # 根本不存在的连接。
    for col, desc in (getattr(vocab, "column_desc", None) or {}).items():
        kw = _desc_keyword(desc) if isinstance(desc, str) else ""
        if len(kw) >= 2:
            pairs.setdefault(kw, (col.split(".", 1)[-1], "column"))

    runs = [(m.start(), m.end()) for m in _CJK_RE.finditer(text)]
    edits: list[_Edit] = []
    # 边界判据用的「已知更长词」集合 = 候选词库 + 列说明主干词 + 文档题名。
    # 这里必须用**错别字层自己认的全部词**，不能只用别名表那份：
    # 「招标数额标准」「采购方式」都不在别名表里，只靠别名会被判成独立词而误改。
    typo_long_terms: set[str] = {term for term in pairs if len(term) >= 3}
    typo_long_terms.update(getattr(vocab, "doc_titles", {}) or {})
    for start, end in runs:
        seg = text[start:end]
        for length in range(min(len(seg), 6), 1, -1):
            for off in range(0, len(seg) - length + 1):
                span_s, span_e = start + off, start + off + length
                surface = seg[off : off + length]
                if _collect(edits, span_s, span_e) or any(
                    _spans_touch(mention_start, mention_end, span_s, span_e)
                    for mention_start, mention_end in protected_mentions
                ):
                    continue
                if any(ch in _STOP_CHARS for ch in surface):
                    continue  # 含虚字的片段不纠错（防跨词边界误改，见 _STOP_CHARS 注释）
                hits = {term: tgt for term, tgt in pairs.items() if len(term) == length and _lev1(surface, term)}
                if not hits:
                    continue
                # ⚠ 片段只是**文本里更长已知词的组成部分**时，不能当错别字改。
                #
                # 「采购项目」里的「采购项」与词表里的「采购人」等长、只差一个字，
                # 会被当成错别字改写成 buyers；而用户说的明明是 projects。
                # 这类撞车的共同形状是：被改写的片段，其**两侧紧邻字符**拼起来
                # 恰好构成另一个更长的已知词/已识别对象。
                # 判据只看「片段在原地能否延伸成更长的已知词」，不看词表大小，
                # 所以加词不会让这种误改变多。
                if _extends_to_longer_known(text, span_s, span_e, pairs):
                    continue
                # ⚠ 片段右侧紧跟汉字时，它不是独立词，不能当错别字改。
                #
                # `_extends_to_longer_known` 只认「能延伸成词表里的更长词」，
                # 而延伸出来的常是**词表外的法规/业务术语**：
                #
                #     达到公开招标|数|额标准…  →「招标数」与「招标人」只差一字，
                #                                改写成 buyers，句子变成「公开buyers额标准」
                #
                # 「招标数额标准」是政府采购法里的术语，不在任何表名/列名词表里，
                # 所以延伸判据接不住。而中文里三字片段右边紧挨着汉字时，
                # 它几乎总是更长词的前半截（招标|数额、采购|方式、中标|金额）。
                # 判据与表别名层 `_right_boundary_ok` 同源：先要求自身构成词边界。
                # 允许的后缀（数量/金额/个/条…）与那边共用同一个集合，
                # 免得两处对「算不算独立词」给出不同答案。
                if not _right_boundary_ok(text, span_s, surface, typo_long_terms):
                    continue
                targets = {(c, k) for c, k in hits.values()}
                # If this entity is already grounded by an exact alias or schema
                # description, do not reinterpret another nearby phrase as a typo
                # for the same target (e.g. 客户 ... 消费金额 -> customer).
                if targets and targets <= known_entities:
                    continue
                # ⚠ 顺序有语义：两字片段的「不采纳」必须在「报歧义」**之前**。
                #
                # 两字模糊匹配会与复合词内部的普通二字词撞车（见下方原注释）。
                # 这类撞车**既不能采纳改写，也不能报歧义** —— 因为它压根不是
                # 一个独立的词，而是长词被切出来的碎片：
                #
                #     「工单编号」→ 切出「单编」→ 命中错别字表「单价/单据/单曲」
                #     「该工单是否刚好达到合同修复上限？工单编号 WO-QF-002。」
                #
                # 用户写的是完全正常的「工单编号」，系统却因为一个不存在的错别字
                # 追加 unresolved 信号，把整题拦进澄清（实测 CS14 三题全中）。
                #
                # 原实现把 `len(targets) > 1` 放在 `length < 3` 之前，两字片段
                # **绕过了它下面这条本该拦住它的防护**，仍然产生拦截信号。
                # 2026-10-16 把两条判断换序，治本。
                if length < 3:
                    # Two-character fuzzy matches collide with ordinary words inside compounds:
                    # e.g. 「合同期」/「合同主表」→「employee」 via 「同事」, or 「说明」→
                    # 「Hip Hop/Rap」 via 「说唱」. Exact aliases still run earlier; only
                    # low-confidence two-character typo rewrites are withheld (robustness > recall).
                    continue
                if len(targets) > 1:
                    unresolved.append(
                        f"「{surface}」疑似错别字但存在多个改写目标（{'、'.join(sorted(hits))}），留待澄清"
                    )
                    continue
                canonical, ekind = targets.pop()
                term = next(iter(hits))
                edits.append(
                    _Edit(
                        span_s,
                        span_e,
                        canonical,
                        dict(kind=ChangeKind.TYPO, reason=f"与已知词「{term}」仅差一个字", source=f"fuzzy:{term}"),
                        NormalizedEntity(surface=surface, canonical=canonical, kind=ekind, via="alias"),
                    )
                )
                kinds.add(ChangeKind.TYPO)
    if edits:
        editor.apply(edits)


#: 相对时间词（§6）。含「今」的取**整期（日历对齐）**，2026-09-22 已拍板（§6.2.2）。
#: 长词在前：保证「上个季度」整体命中，而不是被「上季度」截胡。
#:
#: 2026-10-18 补日粒度三词（今天/昨天/前天）。原先只有年、月、季三档，
#: 「昨天发布了多少条公告？」识别不出任何时间要素，模型只能自己猜日期 ——
#: 实测猜成 2023-04-18，与锚点相差两年多，结果必然全错。
#: 之所以必须补进**这张表**而不是交给模型：日历对齐（几号到几号）
#: 是确定性事实，让模型算等于把可验证的东西换成不可验证的。
_TIME_WORDS = (
    "今天", "昨天", "前天",
    "前年", "去年", "今年",
    "上个月", "上月", "本月", "这个月",
    "上个季度", "上季度", "本季度", "这个季度",
)


def _resolve_time(word: str, anchor: date) -> tuple[str, str, str, str, int]:
    """返回 (kind, label, start_iso, end_iso, offset)；区间左闭右开。"""
    y, m = anchor.year, anchor.month
    q = (m - 1) // 3 + 1

    def _month_base(yy: int, mm: int) -> tuple[date, date]:
        start = date(yy, mm, 1)
        end = date(yy + (mm == 12), 1 if mm == 12 else mm + 1, 1)
        return start, end

    def _quarter_base(yy: int, qq: int) -> tuple[date, date]:
        start = date(yy, 3 * (qq - 1) + 1, 1)
        end_m = 3 * qq + 1
        return start, date(yy + (end_m == 13), 1 if end_m == 13 else end_m, 1)

    if word == "前天":
        # 日粒度：区间是**那一天**，左闭右开 [当日, 次日)。
        # 不能用 date(y, m, d - 2) 手算 —— 跨月会溢出，交给 timedelta。
        s = anchor - timedelta(days=2)
        return "day", s.isoformat(), s.isoformat(), (s + timedelta(days=1)).isoformat(), -2
    if word == "昨天":
        s = anchor - timedelta(days=1)
        return "day", s.isoformat(), s.isoformat(), (s + timedelta(days=1)).isoformat(), -1
    if word == "今天":
        return (
            "day",
            anchor.isoformat(),
            anchor.isoformat(),
            (anchor + timedelta(days=1)).isoformat(),
            0,
        )
    if word == "前年":
        s, e = date(y - 2, 1, 1), date(y - 1, 1, 1)
        return "year", f"{y - 2} 年", s.isoformat(), e.isoformat(), -2
    if word == "去年":
        s, e = date(y - 1, 1, 1), date(y, 1, 1)
        return "year", f"{y - 1} 年", s.isoformat(), e.isoformat(), -1
    if word == "今年":
        s, e = date(y, 1, 1), date(y + 1, 1, 1)
        return "year", f"{y} 年", s.isoformat(), e.isoformat(), 0
    if word in ("上个月", "上月"):
        yy, mm = (y - 1, 12) if m == 1 else (y, m - 1)
        s, e = _month_base(yy, mm)
        return "month", f"{yy} 年 {mm} 月", s.isoformat(), e.isoformat(), -1
    if word in ("本月", "这个月"):
        s, e = _month_base(y, m)
        return "month", f"{y} 年 {m} 月", s.isoformat(), e.isoformat(), 0
    if word in ("上季度", "上个季度"):
        yy, qq = (y - 1, 4) if q == 1 else (y, q - 1)
        s, e = _quarter_base(yy, qq)
        return "quarter", f"{yy} 年第{qq}季度", s.isoformat(), e.isoformat(), -1
    if word in ("本季度", "这个季度"):
        s, e = _quarter_base(y, q)
        return "quarter", f"{y} 年第{q}季度", s.isoformat(), e.isoformat(), 0
    raise ValueError(f"未知时间词：{word!r}")


def _pass_time(
    editor: _Editor, anchor: date, kinds: set[ChangeKind], times: list[TimeExpression]
) -> list[tuple[int, int]]:
    """相对时间词 → 绝对写法。返回**改写后**各段落在文本里的位置区间。

    返回值是给 `_pass_iso_time` 用的：日粒度词（今天/昨天/前天）改写成
    ISO 形态后，若不告诉下游「这几段是改写出来的」，ISO 扫描会把它们
    当成用户原话里写的日期再记一遍，同一个时间出现两条要求。
    """
    pattern = re.compile("(" + "|".join(_TIME_WORDS) + ")")
    edits: list[_Edit] = []
    spans: list[tuple[int, int]] = []
    for m in pattern.finditer(editor.text):
        kind, label, start, end, offset = _resolve_time(m.group(1), anchor)
        times.append(
            TimeExpression(
                surface=m.group(1),
                kind=kind,
                offset=offset,
                start=start,
                end=end,
                anchor=anchor.isoformat(),
            )
        )
        edits.append(
            _Edit(
                m.start(),
                m.end(),
                label,
                dict(kind=ChangeKind.TIME, reason=f"相对时间「{m.group(1)}」按基准 {anchor.isoformat()} 归一化为 {label}（{start}~{end}，左闭右开）",
                     source="lexicon:time"),
            )
        )
    if edits:
        editor.apply(edits)
        kinds.add(ChangeKind.TIME)
        # 改写后的位置：直接在**改写后的文本**里找替换标签。
        # 不自己累加偏移量——`apply` 的内部布局属于它的实现细节，
        # 外部按它推算偏移，一旦它改成从右往左应用就会全错。
        # 找标签是幂等的：标签本身就是最终文本里的确定串。
        for edit in edits:
            label = edit.replacement
            if not label:
                continue
            start = editor.text.find(label)
            while start >= 0:
                if (start, start + len(label)) not in spans:
                    spans.append((start, start + len(label)))
                    break
                start = editor.text.find(label, start + 1)

    return spans

#: 绝对日期：`YYYY年` 可省（省则取锚点年）；月、日可逐级省略。
#: 「2026年10月16日」→ day；「2026年10月」→ month；「2026年」→ year；
#: 「10月16日」→ 锚点年的 day。要求「年」可省但**必须带「月」**，避免把
#: 「3 个项目」里的孤立数字误当日期。
#:
#: 2026-10-18 补：数字与「月/日/年」之间允许空白。用户常写「10 月 15 日」
#: （中文排版习惯在数字与汉字间加空格），原正则要求紧邻，导致
#: 「从 10 月 15 日到 17 日」整句**一个日期都认不出**（times 为空），
#: 时间条件只能靠大模型从原文猜。
_ABSOLUTE_TIME_RE = re.compile(
    r"(?:(?P<year>(?:19|20)\d{2})\s*年)"
    r"(?:(?P<month>\d{1,2})\s*月(?:\s*(?P<day>\d{1,2})\s*[日号])?)?"
    r"|"
    r"(?P<month2>\d{1,2})\s*月(?:\s*(?P<day2>\d{1,2})\s*[日号])?"
)
#: 只有「年」没有「月日」的写法（「2026年」）。
_ABS_YEAR_ONLY_RE = re.compile(r"(?:(?P<year>(?:19|20)\d{2})\s*年)(?!\s*\d{1,2}\s*月)")

#: 时间区间：「从 10 月 15 日到 17 日」「10月15日至10月17日」「15日~17日」。
#:
#: 为什么必须单独识别而不能靠两个独立日期：区间是**一个**时间条件，
#: 拆成两个独立「day」后，下游会把它们当成「同时满足 15 日 和 17 日」，
#: 或者只取其中一个（实测第 3 轮只取了 15 日那天）。
#:
#: 尾端允许省略年、月：「从 10 月 15 日到 17 日」的「17 日」沿用前端的年月。
_RANGE_TIME_RE = re.compile(
    r"(?:(?P<half>以?前|以?后)\s*)?"
    r"(?P<start>"
    r"(?:(?P<y1>(?:19|20)\d{2})\s*年)?"
    r"(?:(?P<m1>\d{1,2})\s*月)?"
    r"\s*(?P<d1>\d{1,2})\s*[日号]"
    r")"
    r"\s*(?:到|至|~|～|—|-)\s*"
    r"(?P<end>"
    r"(?:(?P<y2>(?:19|20)\d{2})\s*年)?"
    r"(?:(?P<m2>\d{1,2})\s*月)?"
    r"\s*(?P<d2>\d{1,2})\s*[日号]"
    r")"
)


def _pass_absolute_time(
    editor: _Editor, anchor: date, kinds: set[ChangeKind], times: list[TimeExpression],
    *, skip_spans: list[tuple[int, int]] | None = None,
) -> None:
    """解析绝对日期（YYYY年MM月DD日），补进 times。

    与相对时间词（`_TIME_WORDS`）互补：相对词覆盖「去年/本月」等指代，
    这里覆盖明确写出的日历日期。两者都命中时各自独立记录（题面极少同时
    出现；若出现，区间合并的语义歧义交给下游核验报 unknown，不在此硬裁）。
    """
    edits: list[_Edit] = []
    skipped = skip_spans or []
    for m in _ABSOLUTE_TIME_RE.finditer(editor.text):
        if any(lo <= m.start() < hi for lo, hi in skipped):
            continue
        year_txt = m.group("year")
        month_txt = m.group("month") or m.group("month2")
        day_txt = m.group("day") or m.group("day2")
        # 只有「5 月」这种**孤立的月份**不在这里处理：它由 `_pass_time` 的
        # 词表路径按相对时间解释（「5 月」= 锚点年的 5 月），两边都认会使
        # 同一个片段产生两条 TimeExpression 与两次改写（二次改写会被
        # test_normalizer 的幂等性用例抓住）。
        # 这里只认「带年」或「带日」的明确写法。
        if year_txt is None and day_txt is None:
            continue
        if month_txt is None:
            # 只有「2026年」→ 年粒度。
            yy = int(year_txt) if year_txt else anchor.year
            if not year_txt:
                continue
            start = date(yy, 1, 1)
            end = date(yy + 1, 1, 1)
            kind, label, offset = "year", f"{yy} 年", 0
        else:
            yy = int(year_txt) if year_txt else anchor.year
            mm = int(month_txt)
            dd = int(day_txt) if day_txt else None
            if not (1 <= mm <= 12) or (dd is not None and not (1 <= dd <= 31)):
                continue
            try:
                if dd is not None:
                    start = date(yy, mm, dd)
                    end = start + timedelta(days=1)
                    kind, label, offset = "day", f"{yy} 年 {mm} 月 {dd} 日", 0
                else:
                    start = date(yy, mm, 1)
                    end = date(yy + (mm == 12), 1 if mm == 12 else mm + 1, 1)
                    kind, label, offset = "month", f"{yy} 年 {mm} 月", 0
            except ValueError:  # 2月30日等不可能日期
                continue
        # 未来日期超出锚点太远不认（避免把编号「2026年007号」当日期——
        # 严格说编号带「第/号」，此处只是宽防线）
        if start > anchor + timedelta(days=366):
            continue
        times.append(
            TimeExpression(
                surface=m.group(0),
                kind=kind,
                offset=offset,
                start=start.isoformat(),
                end=end.isoformat(),
                anchor=anchor.isoformat(),
            )
        )
        edits.append(
            _Edit(
                m.start(),
                m.end(),
                label,
                dict(kind=ChangeKind.TIME, reason=f"绝对日期「{m.group(0)}」解析为 {label}（{start.isoformat()}~{end.isoformat()}，左闭右开）",
                     source="lexicon:time"),
            )
        )
    if edits:
        editor.apply(edits)
        kinds.add(ChangeKind.TIME)


#: ISO 形态的日期 `YYYY-MM-DD`。
#:
#: 2026-10-18 补：`_pass_absolute_time` 只认「YYYY年MM月DD日」，
#: 而 `_TIME_WORDS` 里的日粒度词（今天/昨天/前天）**改写成 ISO 形态**
#: （「今天」→「2026-10-17」）。于是一次归一化把相对词换成了下游认不出的
#: 写法，二次归一化时 `times` 为空 —— 时间条件凭空消失，
#: 要求核验器拿不到区间就无法判 SQL 里的日期对不对。
#: 补这条不是为二次归一化兜底，是因为 ISO 本来就是用户会写的形态
#: （「2026-10-16 发布了多少条公告？」）。
_ISO_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})(?!\d)")


def _pass_range_time(
    editor: _Editor, anchor: date, kinds: set[ChangeKind], times: list[TimeExpression],
) -> list[tuple[int, int]]:
    """识别「从 X 到 Y」这类**显式时间区间**，产出一条 end 为开区间的 TimeExpression。

    返回被识别到的原文区间（span），供 `_pass_absolute_time` 跳过 ——
    否则区间两端会被再各认一次，变成两个独立日期，下游会把它们当成
    「同时满足 15 日 和 17 日」，或者只取其中一个（实测取到了 15 日）。

    端点缺省规则（中文的省略习惯）：
      · 尾端没写年 → 沿用首端的年（首端也没写 → 锚点年）
      · 尾端没写月 → 沿用首端的月
      · 跨年推断：首端没有年份、首端月 > 尾端月，且尾端日期不晚于首端 →
        认为是跨年区间（12 月到次年 1 月），尾端年 +1。

    区间一律**左闭右开**：end = 尾端日 + 1 天，与其它时间表达式同口径。
    """
    spans: list[tuple[int, int]] = []
    for m in _RANGE_TIME_RE.finditer(editor.text):
        d1 = int(m.group("d1"))
        d2 = int(m.group("d2"))
        m1_txt, m2_txt = m.group("m1"), m.group("m2")
        # 首端没有月份（「15 日到 17 日」）无法定位，不认 —— 宁可漏，不可猜错。
        if m1_txt is None:
            continue
        m1 = int(m1_txt)
        m2 = int(m2_txt) if m2_txt else m1
        y1 = int(m.group("y1")) if m.group("y1") else anchor.year
        if m.group("y2"):
            y2 = int(m.group("y2"))
        elif y1 == anchor.year and m2 < m1:
            y2 = y1 + 1
        else:
            y2 = y1
        try:
            start_day = date(y1, m1, d1)
            end_day = date(y2, m2, d2)
        except ValueError:
            continue
        if end_day < start_day:
            continue
        if end_day > anchor + timedelta(days=366):
            continue
        surface = m.group(0).strip()
        times.append(
            TimeExpression(
                surface=surface,
                kind="range",
                offset=0,
                start=start_day.isoformat(),
                end=(end_day + timedelta(days=1)).isoformat(),
                anchor=anchor.isoformat(),
            )
        )
        spans.append((m.start(), m.end()))
        kinds.add(ChangeKind.TIME)
    return spans


def _pass_iso_time(
    editor: _Editor, anchor: date, kinds: set[ChangeKind], times: list[TimeExpression],
    *, skip_spans: list[tuple[int, int]] | None = None,
) -> None:
    """解析 ISO 形态日期（YYYY-MM-DD），补进 times。

    与 `_pass_absolute_time` 并列，区间同为左闭右开 [当日, 次日)。
    **只记 times，不改写文本** —— 文本已经是标准形态，改了没有收益，
    反而可能打乱用户原话的留痕（`surface`/`span` 要指向用户真实说过的字）。

    `skip_spans` 是被前面几步**改写出来的**区间（相对时间词被换成 ISO 写法）。
    那些位置在用户原话里不是日期，跳过——否则「今天」会被记两次：
    一次是 surface='今天'，一次是改写后的 '2026-10-17'，
    下游按区间去核验时看到两个同日要求，会判成"用户提了两个条件"。
    """
    skipped = skip_spans or []
    for m in _ISO_DATE_RE.finditer(editor.text):
        if any(lo <= m.start() < hi for lo, hi in skipped):
            continue
        try:
            start = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if start > anchor + timedelta(days=366):
            continue
        surface = m.group(0)
        if any(item.surface == surface for item in times):
            continue
        times.append(
            TimeExpression(
                surface=surface,
                kind="day",
                offset=0,
                start=start.isoformat(),
                end=(start + timedelta(days=1)).isoformat(),
                anchor=anchor.isoformat(),
            )
        )


# ---------------------------------------------------------------------------
# C 层：语义兜底（§12）。只在 A/B 都没命中时启用；模型只产候选，回库校验后才采纳。
# ---------------------------------------------------------------------------


class Embedder(Protocol):
    """注入接口（§12.4）：编排层把 RAG 侧的 bge embedder 传进来，本模块不 import app.rag。"""

    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class SemanticResolver:
    """候选矩阵常驻内存（Chinook 仅 75 条，约 300KB，§12.4），无需第二个向量库。"""

    def __init__(
        self,
        candidates: list[tuple[str, str, str]],  # (zh 说法, canonical, kind)
        embedder: Embedder,
        threshold: float = 0.62,
        margin: float = 0.03,
    ):
        self.threshold = threshold
        self.margin = margin
        self._embedder = embedder
        self._candidates = self._validate(candidates)
        self._vecs = embedder.embed_many([c[0] for c in self._candidates])

    @staticmethod
    def _validate(candidates: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
        """回库校验（§12.3 第 2 条）：候选指向的表 / 字段必须真的存在。校验不过直接丢弃。"""
        schema_path, _, _ = _metadata_paths()
        sm = _load_json(schema_path)
        tables = {t["table"] for t in sm.get("tables", [])}
        columns = {t["table"] + "." + c["name"] for t in sm.get("tables", []) for c in t.get("columns", [])}
        out: list[tuple[str, str, str]] = []
        for surface, canonical, kind in candidates:
            if kind == "table" and canonical in tables:
                out.append((surface, canonical, kind))
            elif kind == "column" and canonical in columns:
                out.append((surface, canonical, kind))
            elif kind == "value":
                out.append((surface, canonical, kind))  # 取值候选来自别名表本身，构建期即真实
        return out

    def resolve(self, surface: str) -> tuple[str, str, float, bool] | None:
        """返回 (canonical, kind, score, margin_ok)；低于阈值返回 None。**只产候选，不下结论**。"""
        if not self._candidates:
            return None
        q = self._embedder.embed_many([surface])[0]
        sims = sorted(((_cos(q, v), i) for i, v in enumerate(self._vecs)), reverse=True)
        top, top_i = sims[0]
        if top < self.threshold:
            return None
        second = sims[1][0] if len(sims) > 1 else 0.0
        # 候选元组是 (zh 说法, canonical, kind) —— 别解包错序（这里错过一次）
        _surface, canonical, kind = self._candidates[top_i]
        return canonical, kind, top, (top - second) >= self.margin


def build_semantic_resolver(
    embedder: Embedder,
    aliases: AliasTable | None = None,
    threshold: float = 0.62,
    margin: float = 0.03,
) -> SemanticResolver:
    """组装 C 层：候选 = 表/字段中文说明 + 值级别名的 zh 说法（全部经回库校验）。"""
    _, desc_path, _ = _metadata_paths()
    table = aliases or load_alias_table()
    dz = _load_json(desc_path)
    candidates: list[tuple[str, str, str]] = []
    for name, desc in (dz.get("tables") or {}).items():
        kw = _desc_keyword(str(desc))
        if len(kw) >= 2:
            candidates.append((kw, name, "table"))
    for col, desc in (dz.get("columns") or {}).items():
        kw = _desc_keyword(str(desc))
        if len(kw) >= 2:
            candidates.append((kw, col, "column"))
    for col, mapping in table.values.items():
        for value, alias_list in mapping.items():
            for a in alias_list:
                candidates.append((a, value, "value"))
    return SemanticResolver(candidates, embedder, threshold=threshold, margin=margin)


# ---------------------------------------------------------------------------
# 主函数（§5）
# ---------------------------------------------------------------------------


def normalize(
    question: str,
    *,
    aliases: AliasTable | None = None,
    time_anchor: datetime | None = None,
    semantic: SemanticResolver | None = None,
) -> NormalizeResult:
    """把不标准的用户输入改写成标准问句。

    - 纯函数、无副作用、可单独跑测试；
    - aliases 不传时从 ``infra/schema_meta.aliases.zh.json`` 加载（带进程内缓存）；
    - time_anchor 不传时取当前时间；【跑分时必须显式传入固定值】（§6.1，基准已定 2024-06-15）；
    - semantic 不传时 C 层关闭（默认）；由编排层注入 embedder 后启用（§12.4）。

    幂等性（可写单测）：``normalize(normalize(q).normalized).changes == []``
    """
    t0 = _time.perf_counter()
    original = question
    kinds: set[ChangeKind] = set()
    times: list[TimeExpression] = []
    unresolved: list[str] = []

    if not question.strip():
        return NormalizeResult(original=original, normalized=question, elapsed_ms=0.0)

    # 中英混排：只判定不改写（rob-07）—— 题干里的英文可能是表名提示也可能是噪声，
    # 交给下游按既有逻辑处理，这里只负责如实记类型
    if _CJK_RE.search(question) and _LATIN_RE.search(question):
        kinds.add(ChangeKind.MIXED_LANG)

    editor = _Editor(question)
    table = aliases or load_alias_table()
    vocab = load_vocab()

    _pass_fullwidth(editor, kinds)
    _pass_filler(editor, kinds)
    _pass_alias(editor, table, vocab, kinds)
    _pass_typo(editor, table, vocab, kinds, unresolved)

    anchor = (time_anchor or datetime.now()).date()
    rewritten = _pass_time(editor, anchor, kinds, times)
    # 区间必须先于绝对日期跑：区间的两端若被单点 pass 各认一次，
    # 下游会看到两个独立 date 而不是一段区间（见 _pass_range_time 注释）。
    range_spans = _pass_range_time(editor, anchor, kinds, times)
    # 跳过 `_pass_time` 改写出的片段：相对时间词（「去年」）已被换成
    # 「2023年」这类绝对写法，若再被这里认一次，同一个条件会产出两条
    # TimeExpression（下游按条数核验时会判成"用户提了两个时间条件"）。
    _pass_absolute_time(editor, anchor, kinds, times,
                        skip_spans=rewritten + range_spans)
    # ISO 形态另走一条：与「YYYY年MM月DD日」形态不重叠，只记 times 不改写文本。
    # 传改写区间，避免把「今天」被换成的 ISO 写法当成用户原话里的第二个日期。
    _pass_iso_time(editor, anchor, kinds, times, skip_spans=rewritten + range_spans)

    if semantic is not None:
        _pass_semantic(editor, semantic, unresolved, kinds)

    entities = editor.entities
    if not entities and not times:
        unresolved.append("未识别到任何可定位的数据要素（表 / 字段 / 取值 / 文档），交由澄清环节处理")

    elapsed = (_time.perf_counter() - t0) * 1000.0
    return NormalizeResult(
        original=original,
        normalized=editor.text,
        changes=editor.changes,
        entities=entities,
        times=times,
        unresolved=unresolved,
        degraded_kinds=[k for k in ChangeKind if k in kinds],
        elapsed_ms=round(elapsed, 3),
    )


def _pass_semantic(editor: _Editor, semantic: SemanticResolver, unresolved: list[str],
                   kinds: set[ChangeKind]) -> None:
    """C 层：对未被占用的中文片段做语义兜底。只认「词」（2~6 字滑窗），
    不试整句 —— 整句去和字段说明比相似度没有意义。"""
    text = editor.text
    edits: list[_Edit] = []
    for m in _CJK_RE.finditer(text):
        seg = m.group(0)
        for length in range(min(len(seg), 6), 1, -1):
            for off in range(0, len(seg) - length + 1):
                span_s, span_e = m.start() + off, m.start() + off + length
                surface = seg[off : off + length]
                if _collect(edits, span_s, span_e):
                    continue
                hit = semantic.resolve(surface)
                if hit is None:
                    continue
                canonical, kind, score, margin_ok = hit
                if not margin_ok:
                    unresolved.append(f"「{surface}」语义候选存在歧义（{canonical}，分差不足），留待澄清")
                    continue
                # resolve 返回的 kind 已是要素类型（table / column / value），直接用
                edits.append(
                    _Edit(
                        span_s,
                        span_e,
                        canonical,
                        dict(kind=ChangeKind.ALIAS, reason=f"语义兜底命中（相似度 {score:.2f}），已回库校验通过",
                             source="semantic"),
                        NormalizedEntity(surface=surface, canonical=canonical, kind=kind, via="semantic"),
                    )
                )
                kinds.add(ChangeKind.ALIAS)
    if edits:
        editor.apply(edits)


__all__ = [
    "AliasTable",
    "ChangeKind",
    "Embedder",
    "NormalizedEntity",
    "NormalizeResult",
    "SemanticResolver",
    "TextChange",
    "TimeExpression",
    "Vocab",
    "build_semantic_resolver",
    "load_alias_table",
    "load_vocab",
    "normalize",
]
