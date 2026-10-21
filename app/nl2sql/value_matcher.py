"""把用户问句中的人话取值（「男」「巴西」「已发货」）映射到库里的规范取值。

## 为什么需要单独一个模块

`app/query_requirements.py` 的 `value_filter` 只认词表里登记过的别名（
`load_vocab()` 的 `alias`），而词表是围绕**业务对象**（客户/发票/员工）建的，
不覆盖**列取值**（性别 = 男/女、婚姻状况 = 已婚/未婚）。结果是：

    问：男的有多少？    → 提取到的只有 measure，没有取值条件
    问：女性员工有多少？ → 能答（靠「员工」这个词），但 SQL 不会带 gender 过滤

前一句被澄清闸门拦下（追问「要统计什么对象」），后一句**答出错数**
（返回全部员工数而不是女性员工数）—— 后者比前者更糟。

## 判定原则（三条硬纪律）

1. **取值只从元数据来，不从列名猜。** 候选列由 `RuntimeCapability.category_columns()`
   给出，取值由 `normalized_values()` 给出；两处都拿不到就返回空，
   绝不用「gender 列里应该有 M/F」这种常识补。
2. **匹配走说明文本，不走裸取值。** AdventureWorks 的 `Employee.Gender` 与
   `Employee.MaritalStatus` **共享取值 `M`**（Male / Married）。若拿用户词
   直接对裸取值做匹配，「男」会同时命中两列 → 判为歧义 → 永远不产生条件。
   改为先看**列说明里有没有对该取值的语义解释**（`Gender` 的说明是
   「M 表示男性，F 表示女性」，`MaritalStatus` 是「M 表示已婚，S 表示未婚」），
   含「男性」的那一列才是「男」的归属。
3. **命中不唯一就不产生条件。** 宁可漏（少一个过滤条件，闸门会如实标黄），
   不可误（把「男」当婚姻状况，SQL 条件写错且没有任何提示）。

## 与词表的关系

本模块不替代 `app/query_requirements.py` 的别名词表，而是补它的空档：
`ValueMatcher.match()` 产出的仍是 `value_filter` 类要求，
`surface` 记用户原词（展示用），`normalized` 记**库里的规范取值**（机器用）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.nl2sql.capability_runtime import RuntimeCapability


#: 中文数字/量词等问法词，绝不可能是取值。出现即整段跳过。
_QUESTION_WORDS = (
    "多少", "几个", "几位", "几名", "几张", "几条", "几笔", "几家", "几种",
    "哪些", "哪个", "哪家", "哪种", "什么", "怎么", "如何", "是否",
    "统计", "汇总", "合计", "总计", "平均", "分别", "排序",
)

#: 片段两端要剥掉的功能词。
#: 取值是实词块，「有/的/是/在」这类是句子骨架，留在片段里会让
#: 「女性员工有多少」切出「女性员工有」这种串（长度对不上释义，永远匹配不上）。
#: ⚠ 只剥**两端**，不剥中间：「在职」中间的「在」、「男性」的「性」都不能动。
_EDGE_STOPWORDS = (
    "有", "的", "是", "在", "了", "吗", "呢", "吧", "啊", "和", "与", "或",
    "把", "被", "给", "对", "从", "向", "为", "所", "之", "而", "并", "及",
)

#: 片段里若含这些词，说明它是句子骨架而不是取值，整段丢弃。
#: 例：「公司有多少员工」的「公司有多少员工」—— 含「多少」已被切走，
#: 剩下「公司」与「员工」两个实词块，都不该被当成取值。
_USAGE_WORDS = (
    "员工", "客户", "发票", "订单", "产品", "部门", "供应商", "专辑", "艺术家",
    "公司", "数量", "金额", "总数", "人数", "记录", "数据", "信息", "情况",
)


#: 说明文本里"把这个取值翻译成人话"的常见句式。
#: 之所以要认这几种，是因为取值本身（`M`）无法与用户词（「男」）建立联系，
#: 唯一能建立联系的桥是说明文本。**没有这个桥就不匹配** —— 这是纪律 2。
_VALUE_GLOSS_PATTERNS = (
    r"{value}\s*(?:表示|代表|指|即|为|是)\s*([^，。；;,、\s]+)",
    r"{value}\s*=\s*([^，。；;,、\s]+)",
    r"([^，。；;,、\s]+)\s*(?:记(?:为|作)|编码为|对应)\s*{value}",
)

#: 用户词与说明释义之间允许的等价关系。只做**双向包含**，不做同义词表。
#: 例：用户说「男」，说明释义是「男性」→ 「男」是「男性」的子串 → 命中。
#: 反向也成立（用户说「男性」，释义「男」）。
#: ⚠ 刻意**不**做「男/男性/Male/M」这类人工同义表：换个库、换种语言就要重维护，
#: 而且一旦写错就是静默写错 SQL。释义文本本身就是项目自己的元数据，跟着它走。
_MIN_OVERLAP = 1

#: 双向包含里「谁包含谁」的宽度上限。
#:
#: 单字取值（`M`/`F`、`男`/`女`）确实存在，所以 `_MIN_OVERLAP = 1` 不能取消；
#: 但**短词被长取值包含**这一侧必须封顶，否则任何 1-2 字片段都会撞上长取值：
#:
#:     用户问「有多少条中标记录…」→ 切出片段「中标」
#:     某列的采样取值是「清华大学测试服务器采购项目（第二次）中标公告」
#:     「中标」in 整条标题 → 命中 → 生成一条冒充取值筛选的条件（`source=literal`）
#:
#: 命中之后不但多了一个错条件，还会让「本轮有没有提到业务对象」判成没有，
#: 把本来能答的问题推进澄清追问。2026-10-17 在政采库上实测到 3 题被这么拦。
#:
#: 规则：用户词比取值短时，只允许取值本身也短（≤ 该长度）。
#: 释义一侧（gloss）同理 —— 释义是「男性」这种 1-3 字短语，不是整句。
_MAX_VALUE_LEN = 8

#: 单字（1 字）侧更严的封顶。
#:
#: 单字是最容易乱撞的一档：「有」撞上「三优建设有限公司」、「会」撞上
#: 「…服务中心劳务服务项目」。这类命中既不是取值也不可能是取值 ——
#: 真正的单字取值只有 `M`/`F`、`男`/`女`、`是`/`否` 这种，长度都是 1。
#: 所以单字只允许与**单字取值**相等，或（释义一侧）落在 2 字的释义短语里
#: （「男」→ 释义「男性」）。两字及以上的用户词仍走 `_MAX_VALUE_LEN`。
_SINGLE_CHAR_MAX_VALUE_LEN = 2


@dataclass(frozen=True)
class ValueMatch:
    """一个用户词到库内取值的映射。"""

    table: str
    column: str
    value: str        # 库里的规范取值，进 SQL 用
    surface: str      # 用户原词，展示用
    span: tuple[int, int]
    confidence: str   # "exact" | "gloss" | "ambiguous"

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}={self.value}"


def _clean_phrase(text: str) -> str:
    """剥掉问法词与标点，留下可能是取值的片段。"""
    out = text
    for word in _QUESTION_WORDS:
        out = out.replace(word, " ")
    out = re.sub(r"[？?。！!，,、；;：:\s]+", " ", out)
    return out.strip()


def _split_phrases(text: str) -> list[tuple[str, int, int]]:
    """把问句切成候选取值片段，返回 (片段, 起, 止)。

    切分依据是停用词与标点，不做分词 —— 取值通常是 1-4 字的整块，
    中文分词在这里只会把「男」切没或把「巴西」切碎。

    片段切出来后还要过两道筛：剥掉两端的骨架词（`少女员工有多少` → `女性员工`
    再 → `女性`），以及丢弃含业务对象词的骨架段（`公司有多少员工` 的 `公司`
    不是取值）。两道筛都**只做减法**，不做任何猜测性扩展。
    """
    cleaned = text
    for word in _QUESTION_WORDS:
        cleaned = cleaned.replace(word, "\u0000")
    parts: list[tuple[str, int, int]] = []
    for segment in re.split(r"[\u0000？?。！!，,、；;：:\s]+", cleaned):
        if not segment:
            continue
        for piece in _trim_to_value(segment):
            start = text.find(piece)
            if start < 0:
                continue
            parts.append((piece, start, start + len(piece)))
    return parts


def _trim_to_value(segment: str) -> list[str]:
    """从骨架段里剥出候选取值块。

    做法：按业务对象词切段（它们一定不是取值），再对每段剥两端骨架词。
    Examples:
        「女性员工有」 → 按「员工」切 → 「女性」 → ['女性']
        「公司有」     → 按「公司」切 → ['']   → []
        「男的有」     → 无对象词 → 剥「有」 → ['男']
    """
    pieces: list[str] = []
    chunks = [segment]
    for word in _USAGE_WORDS:
        nxt: list[str] = []
        for chunk in chunks:
            nxt.extend(chunk.split(word))
        chunks = nxt
    for chunk in chunks:
        piece = chunk
        # 两端反复剥骨架词（「的的女」这种极端情形也一并处理）
        changed = True
        while changed and piece:
            changed = False
            for word in _EDGE_STOPWORDS:
                if piece.startswith(word) and len(piece) > len(word):
                    piece = piece[len(word):]
                    changed = True
                if piece.endswith(word) and len(piece) > len(word):
                    piece = piece[:-len(word)]
                    changed = True
        if piece:
            pieces.append(piece)
    return pieces



def _glosses(description: str, value: str) -> tuple[str, ...]:
    """从列说明里抽出「value 表示 X」的 X 部分。抽不到 → 空元组。"""
    if not description or not value:
        return ()
    found: list[str] = []
    for pattern in _VALUE_GLOSS_PATTERNS:
        regex = pattern.replace("{value}", re.escape(value))
        for hit in re.finditer(regex, description):
            gloss = hit.group(1).strip()
            if gloss and gloss != value:
                found.append(gloss)
    return tuple(dict.fromkeys(found))


def _overlaps(surface: str, candidate: str) -> bool:
    """长度门：两侧都必须短，且任一侧超过上限就拒。

    ⚠ 名字叫 overlaps，但它**不做包含判断** —— 实现上只有长度约束。
    这是本模块的既有语义，历代用例都建立在它之上（「男」对「男性」靠长度
    放行，而不是靠 `in`）。这里不改语义，只把 docstring 写实。

    「两侧都要过上限」是 2026-10-18 补的，起因是政采 grob-18：
        问句「嗯……就是那些中了标的钱，加起来总共有多少啊（金额是 0 的不算）？」
        切成一个片段「就是那些中了标的钱」（9 字）
        某列采样取值里有「卫星信道频谱分析仪」（8 字）
    旧实现只卡"更长的那一侧"（`len(longer) <= max(len(shorter), limit)`），
    于是 8 字取值与 9 字片段两边都"不超"，被判成 `exact` 命中 ——
    一个整句碎片冒充成了取值筛选条件，真正的指标反而没了落点，整题被拦。

    教训：**上限必须同时约束两侧**。只约束一侧时，与上限等长的长串会互相
    认定成命中，而它们之间可能毫无关系。

    既有用例（都仍然成立）：
        「中标」  vs 「…中标公告」        → 拒（取值是整条标题，超 8 字）
        「有」    vs 「三优建设有限公司」  → 拒（单字侧上限定为 2）
        「男」    vs 「男性」              → 收（1 字 vs 2 字，都在限内）
        「男」    vs 「男」                → 收
        「已婚」  vs 「已婚」              → 收
        「北京」  vs 「北京市海淀区」      → **收**（5 字 vs 6 字，都在限内）
            这一条刻意保留：改成长度门之外的包含判断会连带影响
            「男」/「男性」这类既有用例，超出本次修复范围。
    """
    a = surface.casefold().strip()
    b = candidate.casefold().strip()
    if len(a) < _MIN_OVERLAP or len(b) < _MIN_OVERLAP:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    limit = _SINGLE_CHAR_MAX_VALUE_LEN if len(shorter) == 1 else _MAX_VALUE_LEN
    if len(shorter) > limit or len(longer) > max(len(shorter), limit):
        return False
    return True


class ValueMatcher:
    """按能力层的分类列与取值，把用户词映射到规范取值。

    构造时传 `RuntimeCapability`；传 `None` 时 `match()` 恒返回空元组
    （**不是**抛异常，也不是退化到常识猜测）—— 与能力层「拿不到依据就
    判 unknown」的纪律一致。
    """

    def __init__(self, capability: RuntimeCapability | None) -> None:
        self._capability = capability

    def match(
        self,
        text: str,
        *,
        tables: tuple[str, ...] | None = None,
        max_matches: int = 8,
    ) -> tuple[ValueMatch, ...]:
        """在 `text` 里找能映射到库内取值的片段。

        Args:
            text: 用户问句原文。
            tables: 限定候选表；`None` 表示用能力层可见的全部表。
            max_matches: 上限，防止在超长文本上退化成全表扫描。

        Returns:
            按出现位置排序的 `ValueMatch` 元组。命中不唯一（同一片段能落到
            多个列/取值）的候选**不出现在结果里** —— 纪律 3。
        """
        if self._capability is None or not text:
            return ()

        candidates = self._candidate_columns(tables)
        if not candidates:
            return ()

        # 片段 → [(table, column, value, confidence)]
        resolved: dict[tuple[str, int, int], list[tuple[str, str, str, str]]] = {}
        for phrase, start, end in _split_phrases(text):
            if len(phrase) > 24:
                # 超过 24 字的片段不可能是取值，是没切干净的长句
                continue
            hits: list[tuple[str, str, str, str]] = []
            for table, column in candidates:
                hits.extend(self._match_column(table, column, phrase))
            if not hits:
                continue
            resolved[(phrase, start, end)] = hits

        out: list[ValueMatch] = []
        for (phrase, start, end), hits in resolved.items():
            distinct = {(t, c, v) for t, c, v, _ in hits}
            if len(distinct) != 1:
                # 落到多个列/取值 —— 不猜
                continue
            table, column, value = next(iter(distinct))
            confidence = next(conf for t, c, v, conf in hits if (t, c, v) == (table, column, value))
            out.append(ValueMatch(
                table=table, column=column, value=value,
                surface=phrase, span=(start, end), confidence=confidence,
            ))
            if len(out) >= max_matches:
                break
        return tuple(sorted(out, key=lambda item: item.span))

    # ── 内部 ────────────────────────────────────────────────────────────
    def _candidate_columns(self, tables: tuple[str, ...] | None) -> tuple[tuple[str, str], ...]:
        assert self._capability is not None
        out: list[tuple[str, str]] = []
        for table in (tables if tables is not None else self._all_tables()):
            for column in self._capability.category_columns(table):
                if self._capability.normalized_values(table, column):
                    out.append((table, column))
        return tuple(out)

    def _all_tables(self) -> tuple[str, ...]:
        """能力层可见的全部表名。

        ⚠ 属性名是 `catalog`，**不是** `_catalog`。写错时这里是**静默返回空**
        的（`tables=None` 时调用方拿不到任何候选列，取值匹配整条失效且不报错）
        —— 实测踩过一次：`男的有多少？` 完全没有取值条件，而链路各环节都正常。
        所以下面用 `getattr` 双重兜底，并在两处都拿不到时返回空元组。
        """
        assert self._capability is not None
        catalog = getattr(self._capability, "catalog", None)
        if catalog is None:
            catalog = getattr(self._capability, "_catalog", None)
        names = getattr(catalog, "table_names", None)
        if callable(names):
            return tuple(names())
        return ()


    def _match_column(self, table: str, column: str, phrase: str) -> list[tuple[str, str, str, str]]:
        """在一个列上做匹配。返回 (table, column, value, confidence)。"""
        assert self._capability is not None
        values = self._capability.normalized_values(table, column)
        description = self._capability.business_meaning(table, column)
        hits: list[tuple[str, str, str, str]] = []

        for value in values:
            # ① 用户词与取值本身一致（「巴西」=「Brazil」不会命中，但
            #    「已发货」=「已发货」这类中文取值会）
            if _overlaps(phrase, value):
                hits.append((table, column, value, "exact"))
                continue
            # ② 走说明文本的释义桥（「男」→ Gender 的「M 表示男性」）
            for gloss in _glosses(description, value):
                if _overlaps(phrase, gloss):
                    hits.append((table, column, value, "gloss"))
                    break
        return hits


__all__ = ["ValueMatch", "ValueMatcher"]
