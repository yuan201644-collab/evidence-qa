"""query_requirements.py —— 把一轮提问拆成**可对账的要求清单**。

**它解决什么问题**：澄清器原先只看"句子里有没有某个词"（增长率、他/她、
改写歧义），看不到"这句话一共提了几项要求、哪几项没有依据"。于是出现两类
静默失败：

  ① 「2023年有多少客户？」—— 用户提了"2023 年"和"客户数量"两项要求，
     而当前库没有客户注册时间列，系统却算了**全量客户数**；
  ② 「有多少位？」—— 用户只提了指标、没提对象，系统却挑一张表猜着答。

两类失败长得很不一样，根因是同一个：**没有一份"用户到底要什么"的清单**。
本模块就产出这份清单，交给 `SemanticClarifier` 判定该不该反问。

**设计原则（对齐实施方案 §5.1）**：

1. **从原话提取，不从改写结果提取**（§5.1 第 1 条）。改写会在"无法消解时
   什么都不改"，但也可能在别名替换时把用户原话里那个更有信息量的说法换掉。
   故 `extract()` 同时接收 `original` 与 `normalized`，**以原文为准**，
   归一化只用来填 `normalized` 字段。
2. **每项要求必须能回到原话**（§5.1 第 2 条）。所有 `source="literal"` 的要求
   都带 `span`，且构造时用原文 substring 复核一遍；对不上就降级为未解析。
   系统补的默认值只能标 `source="default"`，**不允许冒充用户提供**。
3. **解析不了 ≠ 没有这项要求**（§5.1 第 7 条）。扫到实质约束信号却归不了类时，
   记 `unresolved=True` 或进 `unresolved_spans`，绝不静默丢弃。
4. **不额外调模型**（§5.1 第 8 条）。全部走既有词表与正则，零网络、零模型加载。

**能力判定为什么不在本模块做**：方案 §5.2 设想用
`infra/query_capabilities.v0.1.json` 静态声明"该源有没有某一列"。本项目实跑
过三个不同 schema（Chinook / AdventureWorks / 跨源演示库），静态表要手工维护
三套且必然与真实元数据漂移。改为**运行时从 `SchemaCatalog` 的列说明判定**，
见 `app/nl2sql/capability_runtime.py::RuntimeCapability` —— 零配置、
自动适配任意 schema，代价是判定依赖列的 `description` 写得够清楚（现有
`infra/schema_meta.*.descriptions.zh.json` 已满足）。

⚠ **本模块只负责"用户说了什么"，不负责"系统做不做得到"**。后者是
`app/nl2sql/capability_runtime.py` + `app/nl2sql/requirement_validator.py`
的职责（S2），两件事的失败处置完全不同：缺要求 → 追问（`app/clarifier.py`）；
要求有了却做不到 → 拦截或如实标黄（`app/nl2sql/pipeline.py`）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace

from app.nl2sql.normalizer import NormalizeResult, load_vocab
from app.nl2sql.planning_models import QueryRequirement, QueryRequirements
from app.nl2sql.value_matcher import ValueMatcher

#: 清单版本。改变"什么叫一条要求"时必须升版本 —— 它会被写进 trace，
#: 报告里要能说清某次运行用的是哪一版口径（实施方案 §10.2 要求可对账）。
#:
#: 版本含义：
#:   s1.v1 —— 首版：11 类要求，`requirement_id` 为空
#:   s2.v1 —— 增 `value_scope`（条件作用域）与 `join_grain`（关联粒度）两类，
#:            并给每条要求编上稳定 `requirement_id`
REQUIREMENTS_VERSION = "s2.v1"

# ---------------------------------------------------------------------------
# 词表：全部与既有实现保持一致，避免同一件事两套说法
# ---------------------------------------------------------------------------

#: 指标词。**与 `app/clarifier.py::_MEASURE_HINTS` 同源**：那里是既有澄清规则
#: 用来判"缺指标"的词表，这里复用同一套，否则会出现"澄清器认为有指标、
#: 要求清单认为没有"的自相矛盾。
_MEASURE_TERMS = (
    "销售额", "销售金额", "收入", "营收", "客户数", "订单数", "订单量", "数量",
    "利润", "成本", "金额", "发票数", "销售额同比",
)

#: 聚合信号。用户说"合计/平均/最高"时，指标即使没写全也说明他在要一个数。
#: ⚠️ `最高/最低/最大/最小/最多/最少` **不在此列** —— 它们同时是排名信号，
#: 见下方 `_RANKING_RE`。放两处会让「金额最高的3张发票」同时记成
#: "要一个聚合值"和"要前 3 条"，两句要求互相打架。
_AGGREGATE_TERMS = ("合计", "总计", "总共", "一共", "平均", "均值")

#: 「要几个」类问法。中文里"多少/几"本身就是指标信号，
#: 但它**必须配一个对象**才有意义 —— 这正是 SC-02「有多少位？」要拦的东西。
_COUNT_QUESTION_RE = re.compile(r"(?:多少|几)(?:位|个|名|张|条|笔|次|家|种)?")

#: 否定词。按实施方案 §5.4.1「数值与否定」一行，`不在巴西` 与 `在巴西`
#: 语义相反，必须作为**结构**保留而不是当普通筛选。
_NEGATION_TERMS = ("不在", "不属于", "除了", "不含", "不包括", "排除", "非")

#: 数值比较。方向与开闭边界都要留住 —— `>100` 与 `>=100` 在边界样本上
#: 产生可验证差异（SC-15 vs SC-16）。
_NUMERIC_COMPARISON_RE = re.compile(
    r"(?P<op>大于等于|不小于|不低于|大于|超过|多于|高于"
    r"|小于等于|不大于|不超过|低于|少于|小于|等于|就是)"
    r"\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>元|块|个|张|条|笔|次|天|小时|年|月)?"
)

#: 排名 / 前 N 条。`金额最高的3张发票` 要能同时认出"最高"与"3 张"。
#: `count` 可缺省（"最高的那张"也是排名，只是没给 N）。
_RANKING_RE = re.compile(
    r"(?P<direction>最高|最低|最大|最小|最多|最少)"
    r"(?:\s*的)?\s*(?P<count>\d+)?\s*(?P<unit>位|个|名|张|条|笔|次|家|种)?"
    r"|前\s*(?P<count2>\d+)\s*(?P<unit2>位|个|名|张|条|笔|次|家|种)?"
)

#: 分组信号。
#:
#: ⚠ `每个/每` 与 `_GRAIN_TERMS` 里的 `每位/每个/每一` **字面重叠，是刻意的**：
#: 中文里同一个词既可能是分组维度（GROUP BY）也可能是结果粒度。
#: 处理办法不在词表里删名（删了会漏掉真正的粒度要求），而是在 `_collect`
#: 里让粒度**跳过已被 grouping 占用的跨度**，保证一词一类。
#: 实测：「每个客户」→ grouping；「每一位客户」→ join_grain（"每"先被
#: grouping 按更短匹配吃掉，"每一位"整体留给粒度）。
_GROUPING_TERMS = ("每个", "各个", "按", "分别", "分组", "每")

#: 「按 X 排列」的排序形态。裸「按」既可能是分组维度（按国家统计）也可能是
#: 排序（按名称升序排列），靠**后文形态**区分，不靠词表增减。
#: 命中时该「按」不算分组要求 —— 它说的是 ORDER BY，不是 GROUP BY。
#: 实测：93 题里有 28 处「按 X 升序/降序」被记成分组要求；
#: 多数题的 SQL 里另有真正的 GROUP BY 所以没暴露，但像
#: 「查询销售金额最高的 100 条记录，金额相同按 sale_id 升序」这种
#: 纯排序句就会被要求核验器判成「要求按维度分组，但 SQL 没有 GROUP BY」。
_SORT_TAIL_RE = re.compile(
    r"^\s*[^，。；？,]{1,20}?(?:升序|降序|从高到低|从低到高|从大到小|从小到大|排列|排序)"
)

#: 指代。**只收"必须靠上下文才能落实"的那几个**：
#: 「这份/该/上述/它」在无上下文时不可能指对；「这个」太泛不收，误伤面大。
#:
#: ⚠️ `其` **整个不收**。它在现代汉语问句里几乎不单独作指代，
#: 出现的场合全是「其它/其中/其实/尤其」这类固定词。曾用 `其(?=中|一|实)`
#: 想只放固定搭配进来，结果把「其中金额最高的是哪张发票？」
#: 「其实我想看客户数量」都判成了指代未落实 —— 两句本身都完整。
#: 想收紧时宁可漏（少追问一次），不可误（对完整问句追问）。
#:
#: ⚠️ 裸 `它` 要加 `(?<!其)`：「其它客户的开票总额是多少？」里那个
#: `它` 是「其它」的构件，被判成指代后会追问"「它」指的是哪个对象"。
_DEIXIS_RE = re.compile(r"(?:这份|该|上述|(?<!其)它|此份)")

#: 文档信号。用于识别"问的是文档而不是库表"。
#:
#: ⚠️ 词表必须覆盖**文档载体词**（记录表/检查表/规格书/预案/承诺函…），
#: 不能只有**文档体裁词**（合同/条款/规定…）。原表只收体裁词，于是
#: 「机房运维巡视检查记录表中，噪音的标准上限是多少？」命不中任何一项，
#: 澄清器 :280 的 document_scope 豁免不生效，掉进 measure_without_object
#: 被追问「请补充要统计的业务对象（如客户、发票或员工）」——而这题问的是
#: 文档里的标准值，跟业务对象无关。
#:
#: 实测（eval/diagnose_clarifier_blocks.py）：RAG 套件 14 道被澄清器拦下的题里
#: 9 道是因为命不中本表。补词后这些题能走到检索链路。
#:
#: 补词原则：只收**明确指向一份文档**的载体名，不收「表」这种裸字
#: ——它会命中「报表/表格/表达的…」，误伤面太大（与 :113 对「这个」的取舍同理：
#: 想收紧时宁可漏，不可误）。
_DOC_TERMS = (
    # 体裁词（原有）
    "合同", "条款", "规定", "制度", "规范", "白皮书", "文档", "报告", "手册", "协议",
    # 载体词（2026-10-16 补：原表缺失导致 document_scope 豁免失效）
    "记录表", "检查表", "规格书", "说明书", "预案", "承诺函", "验收报告",
    "作业指导书", "操作规程", "台账", "清单", "指引", "办法", "标准",
)

#: 相对时间词（含常见变体）。绝对时间由 `_ABSOLUTE_TIME_RE` 负责。
_RELATIVE_TIME_TERMS = (
    "今年", "去年", "明年", "上个月", "上月", "本月", "这个月",
    "上周", "本周", "这周", "上个季度", "上季度", "本季度", "这个季度",
    "今天", "昨天", "明天", "至今", "年初至今", "本月至今",
)

#: 绝对时间：年 / 年-月 / 年-月-日 / 年季度。刻意比 normalizer 的
#: `_EXPLICIT_PERIOD_RE` 多认「季度」，因为 SC-11 就是"两个明确季度"，
#: 而把两个季度折叠成一个年份正是方案 §5.3 点名要避免的。
#:
#: ⚠️ 中文季度常写作「第一季度」，`年` 与 `一` 之间夹着 `第`。
#: 少了这个 `第?`，`2023年第一季度` 会被末位的 `|年` 兜底吃掉，
#: 只留下裸年份 `2023` —— 两个季度的对比于是退化成"同一年的两半"，
#: 而这正是 §5.3 要求避免的坍缩。
#: 季度分支必须排在 `|年` 之前，否则同样被兜底抢先。
_ABSOLUTE_TIME_RE = re.compile(
    r"(?P<year>20\d{2})\s*(?:"
    r"年\s*第?\s*(?P<quarter>[一二三四1-4])\s*季度"
    r"|年\s*(?P<month>\d{1,2})\s*月(?:\s*(?P<daym>\d{1,2})\s*日)?"
    r"|[-/](?P<month2>\d{1,2})(?:[-/](?P<day>\d{1,2}))?"
    r"|年"
    r")?"
)

#: 编号字面量（工单号、合同号…）。与 `app/nl2sql/disambiguator.py` 同模式，
#: 这里不 import 它只为避免把执行器依赖带进纯函数模块。
_LITERAL_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{2,}[A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)(?![A-Za-z0-9])"
)

#: 指代的锚定窗口（字符）。指代词之后这么远之内出现编号字面量，即认为对象已给出。
#: 句式是「该工单……工单编号 WO-QF-001。」——编号常在**句末或下一句开头**，
#: 故窗口要能跨过一个句号（实测最远约 30 字符）。
_DEIXIS_ANCHOR_WINDOW = 48

#: 子句边界字符：用于 `_has_preceding_clause_content` 判断"指代词前面那句有多长"。
#: 问号也算边界 —— 「哪台设备有告警？它是什么设备？」里先行语在问号前那句，
#: 该函数会回溯到上上个边界取内容（见其 docstring）。
_CLAUSE_BOUNDARIES = frozenset("。！？；；，,、\n\r!?;")

#: 同句先行语的**最短实质长度**（非空白字符数）。低于这个长度视为
#: 「它有多少？」这类开场裸指代，仍然追问。
#:
#: 取 4 的依据：中文里能承载一个可辨识对象的最小名词短语约 2~3 字
#: （"设备""那台设备""承诺方"），加一点余量到 4，避免
#: 「它呢？」「这个它？」这类噪声被误判成有先行语。
_DEIXIS_SAME_CLAUSE_MIN = 4

#: 条件作用域信号（S2 新增 `value_scope`）。
#: 这些词说明**条件该落在哪个层级**，而不是"有没有条件"。
#: 实施方案 §5.4.1「条件作用域」一行：同一个筛选放在 WHERE 与放在
#: HAVING 结果不同，用户说了"每个"就必须按分组来，不能一把 SUM 全表。
_SCOPE_TERMS = ("筛选", "过滤", "条件", "范围内", "之内", "以外")

#: 关联粒度信号（S2 新增 `join_grain`）。
#: 用户明确要求"按某个对象的粒度汇总"时，一对多展开会让父表字段被重复累计
#: —— 这正是 `semantic_validator._grain_warnings` 报的那类风险。
#:
#: ⚠ **只收带量词的强形式**（`每位/每个/每一`），裸 `每` 不收：
#: 「每」在中文里太泛（"每天/每次/每月"都可能是时间维度），
#: 而分组信号 `_GROUPING_TERMS` 已经收了它。
#: 一词一类的落地见 `_collect` 里的 `taken_grouping`。
#:
#: ⚠⚠ **`每` 单字绝不能进 `_GRAIN_TERMS`**：
#: `_find_all` 是「全词表扫一遍原文」，不是「从长到短择优」。
#: 一旦 `每` 入表，「每一位」会同时被 `每`(span 0-1) 与 `每位`(span 0-2)
#: 命中两次，而 `_dedupe` 只按 span 去重、**不按 kind 仲裁**，
#: 结果是 grouping 与 join_grain 同时出现、判据互相打架
#: （一边要 GROUP BY，一边要逐条去重）。实测见
#: `tests/test_requirement_validator.py::test_join_grain_*`。
_GRAIN_TERMS = ("每位", "每个", "每一")


def _deixis_is_anchored(
    text: str, dexis_end: int, literal_spans: tuple[tuple[int, int], ...]
) -> bool:
    """判断一个指代词是否被「锚定」——即它指的对象用户已经在同一段里说过了。

    为什么需要这个：`「该」`「`这份`」这类词本身不携带对象，但**很多句子里
    对象就在附近给出了** ——

        「请核验**该**工单的合同服务恢复时限，计算…… 工单编号 WO-QF-001。」
        「**该**工单是否刚好达到合同修复上限？…… 工单编号 WO-QF-002。」
        「请确认……再核验**该**合同的服务恢复时限。 工单编号 WO-QF-013。」

    这类句子若判未解析，系统会追问用户"该指什么"，而答案他已经写在同一段里了。
    追问等于否认用户已提供的信息，比不追问更糟。

    ⚠ 窗口**刻意跨句**：编号常跟在句末或下一句开头（上面第 3 例就是），
    按"同句"限死会把它们全判成未解析（实测 CS14 误伤 7/32）。

    判据**刻意不看被修饰的名词**（工单/合同/设备一视同仁），只看附近有没有编号。
    理由：本模块不认识业务对象名，硬编码"该工单"就得同时维护"该合同""该设备"
    "该订单"……漏一个就退化回误伤。编号是**跨领域稳定**的锚定信号。

    锚定方式二：**同句内的先行名词短语**（2026-10-16 补）
    -----------------------------------------------------
    编号不是唯一的锚定形态。中文里更常见的是**先行语就在同一句的前半句**：

        「机房巡视检查记录表中，哪台设备有告警？**它**是什么设备，告警数是多少？」
        「售后服务承诺函的承诺方是哪家公司？**它**在维保合同里是甲方还是乙方？」

    这两例的「它」都有明确先行语（"那台设备" / "承诺方"），但都不是编号，
    原判据一律判未解析 → 追问「请说明「它」指的是哪个对象或哪份文档」
    —— 而答案用户已经写在同句里了。实测 RAG 套件误伤 3 题。

    判据刻意**不用名词词表**：那正是 :227 警告过的维护陷阱。改用**位置形状**：
    指代词之前、**同一子句内**已有足够长的实质内容（≥ `_DEIXIS_SAME_CLAUSE_MIN` 个
    非空白字符），说明这是一个"先前提、后指代"的完整句子，而不是
    「它的质保期是多久？」这种**通篇只有一个指代、没有先前提**的开场句。

    两条边界刻意为「宁可漏，不可误」（与 :113 对「这个」的取舍同理）：
      - **遇到问号就截断**：`「哪台设备有告警？」` 的先行语在问号**之前**，
        所以取问号后的片段会落空 —— 因此窗口是"从上一个子句边界到指代词"，
        问号与逗号/分号/句号一视同仁当边界，取**最近一个边界之后**的内容。
        「哪台设备有告警？它是什么设备」→ 「它」前面那个子句是空的（上一个
        边界紧邻问号），于是**判不锚定**……这正是我们要避免的误伤，
        所以边界取**前一个子句**：见下方实现，窗口回溯到上上个边界。
      - **长度门槛**：`「它有多少？」` 这种裸指代确实该追问，靠门槛挡住。

    Args:
        text: 原文。
        dexis_end: 指代词命中结束位置。
        literal_spans: 编号字面量在原文中的 (start, end) 列表。

    Returns:
        True 表示存在锚定（编号或同句先行语），该指代不算未解析。
    """
    window_start = max(0, dexis_end - _DEIXIS_ANCHOR_WINDOW)
    window_end = min(len(text), dexis_end + _DEIXIS_ANCHOR_WINDOW)
    if any(
        window_start <= start and end <= window_end for start, end in literal_spans
    ):
        return True

    return _has_preceding_clause_content(text, dexis_end)


def _has_preceding_clause_content(text: str, dexis_end: int) -> bool:
    """指代词之前是否已有**实质内容**——即它有一个同句先行语。

    为什么要看"前两个子句"而不是"前一个"：

        「哪台设备有告警？它是什么设备？」
                    ↑ 边界   ↑ 指代
        取"前一个子句"会得到空串（问号紧邻「它」）。

    中文问句里「A？它 B？」是**陈述与追问**的自然衔接，先行语在问号前那句。
    所以回溯到上上个边界，取**这两个边界之间**的内容。

    长度门槛 `_DEIXIS_SAME_CLAUSE_MIN` 挡住「它有多少？」这类开场裸指代。
    """
    prefix = text[:dexis_end]
    # 子句边界：中英文标点 + 换行。问号也算边界（见 docstring 第 1 条边界说明）。
    cuts = [i for i, ch in enumerate(prefix) if ch in _CLAUSE_BOUNDARIES]
    cuts.append(len(prefix))
    # 取"最近两个边界"之间的内容：最后一段是空串时自动回退到再前一段。
    start = cuts[-3] + 1 if len(cuts) >= 3 else 0
    segment = prefix[start:].strip()
    return len(segment) >= _DEIXIS_SAME_CLAUSE_MIN


def _follows_number(text: str, start: int) -> bool:
    """`start` 之前跳过空白后的字符是不是数字 —— 用来识别「区间端点不含」。

    「（2024-01-01 含，2025-01-01 不含）」里那个「不含」紧跟在一个日期之后，
    描述的是右端点开闭；而「除了巴西」「不含运费」的否定词前面不是数字，
    说的是排除一个业务取值。两者混为一谈会让写对的 SQL 被判 repair。

    只回溯到最近的子句边界为止，避免把前一个子句末尾的数字误当成本句的前缀。
    """
    index = start - 1
    while index >= 0 and text[index].isspace():
        index -= 1
    if index < 0:
        return False
    if text[index] in _CLAUSE_BOUNDARIES:
        return False
    return text[index].isdigit()


def _ranking_direction_is_sort_only(
    text: str,
    hit: "re.Match[str]",
    ranking_hits: list["re.Match[str]"],
) -> bool:
    """「最高的」这类方向词是否只是在说明排序方向，而不是「取前 1 名」。

    返回 True 表示**不**该把它当成一条独立排名要求。

    判据（只看位置，不引入词表）：

        (1) 同一句里，它后面（到句末为止）还有一条**带条数的排名要求**
            （「前 10 个」「最高 3 个」），条数由那条去管，方向词归它；
        (2) 或者它后面紧跟的是「前 N」这种以条数开头的排名要求。

    为什么不能一律默认"没写数字就是取一条"：
        「返回金额最高的前 10 个商品」是这个句式最常见的形态。
        把「最高的」记成"取前 1 名"后，它与「前 10 个」同时存在，
        SQL 无论写 LIMIT 1 还是 LIMIT 10 都有一条过不去，
        三轮重试后整题放弃作答（实测 30 道规模题的三档 Q07 全部如此）。

    为什么也不能一律不认方向词：
        「金额最高的那张发票是哪张？」里没有任何别的条数，
        此时"默认取一条"是正确解读，不能丢。

    宁可漏判不可误判的方向：本判据只把**明显有别的条数**的情形降级；
    判断不了时返回 False（保留原行为，让它继续作为排名要求）。
    """
    tail_start = hit.end()
    # 找句末：方向词之后的第一处句读。跨句的「前 10 个」不算同一诉求。
    boundary = len(text)
    for index in range(tail_start, len(text)):
        if text[index] in _CLAUSE_BOUNDARIES:
            boundary = index
            break

    for other in ranking_hits:
        if other.start() <= hit.start():
            continue
        if other.start() >= boundary:
            continue
        other_count = other.group("count") or other.group("count2")
        if other_count:
            return True
        # 「前 N」形态但 N 被正则漏掉的情况不该出现；这里不猜，跳过。
    return False


def _attach_time_ranges(
    requirements: list["QueryRequirement"],
    normalized: "NormalizeResult | None",
) -> list["QueryRequirement"]:
    """把要素识别算出的**确定区间**挂到对应的 time 要求上。

    **为什么需要这一步**：`_RELATIVE_TIME_TERMS` 命中「去年」时，
    `_Match.normalized` 记的是原词本身，于是要求清单里的
    `normalized="去年"`。核对器 `_time_expression_matches` 只能从
    normalized 里抽年份做子串比对，「去年」抽不到年份，一律判
    「无法静态证明等价」后放行 —— 写错区间的 SQL 拦不住（2026-10-17 实测：
    「去年」的预测区间是 2022，标准是 2023，仍被判通过）。

    而 `NormalizeResult.times` 里其实有 `start`/`end`，是日历归一化的结果。

    匹配方式：**按 surface 文本对齐**，不按序号 —— 时间要求清单里可能同时
    有词表命中的和从要素识别补入的，序号对不上；surface 就是原话片段，
    两边同源，文本相等即是同一段时间。同一 surface 出现多次时取第一个，
    与 `_augment_from_normalizer` 里「相对时间每轮只取一个锚点」一致。

    `normalized` 为 None（纯词表模式）时原样返回，行为与改动前完全一致。
    """
    if normalized is None or not normalized.times:
        return requirements

    # surface 是用户原话片段；同名出现多次只取一个区间，与提取规则一致。
    # 键统一剥空白，理由同下方对齐处（归一化改写会插空格）。
    ranges: dict[str, tuple[str, str]] = {}
    for expression in normalized.times:
        surface = "".join((expression.surface or "").split())
        if not surface or surface in ranges:
            continue
        start = (expression.start or "").strip()
        end = (expression.end or "").strip()
        if start and end:
            ranges[surface] = (start, end)

    if not ranges:
        return requirements

    # 多轮拼接链里会同时出现多个时间：「2026-10-16 发布了多少条采购公告？
    # 用户补充或修改：改成 2026-10-15 呢？ … 其中招标公告有几条？」
    # 若每个 time 要求各自挂上自己那段区间，下游会拿**最早**那段去拼 SQL
    # （实测第 4 轮拼成 `kind='招标公告' AND publish_date=2026-10-16`，
    # 正确应是 10-15~10-17 区间）。
    #
    # 对话语义：**后说的覆盖先说的**。所以只给最后一条 time 要求挂区间，
    # 其余置空 —— 空区间在核验器里是「不校验」，不会误判；
    # 而挂错区间会直接把时间条件锁死，比不校验严重得多。
    last_time_index = -1
    for index, item in enumerate(requirements):
        if item.kind == "time" and item.time_range is None:
            last_time_index = index

    out: list[QueryRequirement] = []
    for index, item in enumerate(requirements):
        if item.kind != "time" or item.time_range is not None:
            out.append(item)
            continue
        if index != last_time_index:
            out.append(item)
            continue
        # 优先按原话片段对齐；片段对不上时退回按 normalized 对齐，
        # 覆盖「词表把 normalized 记成了别的写法」的情况。
        # 两边比对前都剥掉空白：归一化改写会在数字间插空格
        # （「2026年10月16日」→「2026 年 10 月 16 日」），原话片段
        # 与要素识别的 surface 因空格差异永远对不上（2026-10-17 实测）。
        surface_key = "".join((item.surface or "").split())
        normalized_key = "".join((item.normalized or "").split())
        span = ranges.get(surface_key)
        if span is None:
            span = ranges.get(normalized_key)
        if span is None:
            out.append(item)
            continue
        out.append(replace(item, time_range=span))
    return out


@dataclass(frozen=True)
class _Match:
    """一次词表命中的内部记录。构造 `QueryRequirement` 前统一过一遍校验。"""

    kind: str
    surface: str
    start: int
    end: int
    normalized: str = ""
    unresolved: bool = False


def _find_all(text: str, terms) -> list[_Match]:
    """逐个词表项做 substring 查找，返回带真实位置的命中。

    **不用 `re` 拼 alternation**：词表里有中文与特殊字符，拼正则既难读
    又容易因为转义问题漏掉；`str.find` 循环在这点数据量上完全够用，
    而且位置天然准确。
    """
    found: list[_Match] = []
    for term in terms:
        start = 0
        while True:
            index = text.find(term, start)
            if index < 0:
                break
            found.append(_Match(kind="", surface=term, start=index, end=index + len(term)))
            start = index + len(term)
    return found


def _overlaps(candidate: _Match, taken: list[_Match]) -> bool:
    """与已接受的命中区间重叠则丢弃。

    重叠是必然的：「销售额同比」同时命中 `销售额` 与 `销售额同比`，
    「不低于」同时命中 `低于` 与 `不低于`。规则是**先长后短**，
    长的赢 —— 因为长词的信息量严格更多（`销售额同比` 指明了比较关系）。
    """
    return any(candidate.start < item.end and item.start < candidate.end for item in taken)


def _dedupe(matches: list[_Match]) -> list[_Match]:
    """按长度降序接受互不重叠的命中，再按出现位置排序。"""
    ordered = sorted(matches, key=lambda item: (-(item.end - item.start), item.start))
    taken: list[_Match] = []
    for item in ordered:
        if not _overlaps(item, taken):
            taken.append(item)
    return sorted(taken, key=lambda item: item.start)


class RequirementExtractor:
    """从一轮提问里抽出要求清单。**纯函数、无 IO、不加载模型。**

    构造时可选传入 `vocab_loader`（测试用），默认走 normalizer 的 `load_vocab()`；
    可选传入 `capability`（`RuntimeCapability`）启用取值匹配，
    不传时取值只走词表（`normalized=None` 的测试路径不受影响）。
    """

    def __init__(self, vocab_loader=None, capability=None) -> None:
        self._vocab_loader = vocab_loader or load_vocab
        self._capability = capability
        self._value_matcher = ValueMatcher(capability) if capability is not None else None

    # ── 对外主入口 ─────────────────────────────────────────────────────
    def extract(
        self,
        question: str,
        normalized: NormalizeResult | None = None,
        *,
        scope: str = "",
        inherited: "QueryRequirements | None" = None,
    ) -> QueryRequirements:
        """产出一轮的要求清单。

        Args:
            question: **本轮原话**。清单以它为准（§5.1 第 1 条）。
            normalized: 要素识别结果。只用来补对象、时间和"未消解"信号；
                `None` 时退化为纯词表模式（假件测试用）。
            scope: 本轮明确的数据源范围。空串 = 未声明，用默认问数库。
            inherited: 上一轮仍生效的要求（来自 `ConditionArchive`）。
                本轮**一个字都没提**的旧条件由此补进来，标 `source="session"`。
                为 `None` 时行为与改动前完全一致（不继承）。
        """
        original = (question or "").strip()
        normalized_question = (normalized.normalized if normalized else original) or original
        if not original:
            return QueryRequirements(
                version=REQUIREMENTS_VERSION,
                original="",
                normalized_question="",
                scope=scope,
            )

        matches = self._collect(original)
        requirements = self._to_requirements(original, matches)

        # 归一化文本里的对象名要补进来：正常情况原文已含（如"客户"），
        # 但用户说俗称「买家」时原文没有"客户"二字，只有改写后才出现。
        # 这类对象**仍然记 source="literal"**，因为它的依据是用户真说了
        # 「买家」——`surface` 记用户原词，`normalized` 记标准名。
        requirements = self._augment_from_normalizer(original, normalized, requirements)

        # 能力层取值匹配。**排在别名词表之后、继承之前**：
        # 词表命中（"巴西"→Brazil）比说明文本推导（"男"→gender=M）更直接，
        # 让直接的先占位，避免同一片段被两条要求争抢。
        requirements = self._augment_from_capability(original, requirements)

        # 会话继承。**只补本轮完全没提的 kind**，且标 source="session"：
        # 「员工有多少 → 那去年呢」里"员工"必须带过来，否则澄清器会
        # 追问一个用户上一轮已经说过的对象（F3 缺陷）。
        requirements = self._augment_from_inherited(requirements, inherited)

        # 时间区间回填。**必须排在所有补入之后**：继承来的时间要求也得有区间。
        # 词表命中相对时间时 `normalized` 存的是原词（「去年」），核对器拿它
        # 比对不了 SQL 里的日期字面量；要素识别其实已算出确定的左闭右开区间，
        # 这里把它挂到要求上，核对器才能按区间判，而不是一律放行。
        requirements = _attach_time_ranges(requirements, normalized)

        spans = tuple(
            dict.fromkeys(
                item
                for item in (
                    self._unresolved_spans(original, requirements)
                    + tuple(normalized.unresolved if normalized else ())
                )
                if item
            )
        )
        return QueryRequirements(
            version=REQUIREMENTS_VERSION,
            original=original,
            normalized_question=normalized_question,
            scope=scope,
            requirements=tuple(requirements),
            unresolved_spans=spans,
            # 指标落点判定只在本轮必填指标**没有被对象兜住**时才可能改变结论，
            # 但代价很低（一次元数据遍历），无条件算一次，避免下游各自再算。
            measure_locus=_locus_of_measure(
                normalized_question or original,
                tuple(
                    item.surface
                    for item in requirements
                    if item.kind == "measure" and item.normalized == "noun"
                ),
                self._capability,
            ),
        )

    # ── 内部：收集 ─────────────────────────────────────────────────────
    def _collect(self, text: str) -> list[_Match]:
        """把原文扫成一组互不重叠的候选命中。"""
        raw: list[_Match] = []

        # 编号字面量的位置先算出来：后面的指代判定要用它判断"有没有被锚定"。
        literal_spans = tuple(
            (hit.start(1), hit.end(1)) for hit in _LITERAL_RE.finditer(text)
        )

        # 指标：先收长的（"销售额同比" 优先于 "销售额"）
        #
        # ⚠️ 裸指标词（金额/数量/利润/成本/收入）**只表示"问的是这个指标"**，
        # 不表示"要聚合"。用 normalized="noun" 标出来，让核对器知道：
        # 「金额最高的3张发票」里 SQL 用 `ORDER BY total DESC LIMIT 3` 就是对的，
        # 不该因为"没 SUM"被标黄 —— 用户压根没要求聚合。
        # 只有 `_AGGREGATE_TERMS` / `count_question` 命中时才是**聚合要求**。
        for item in _find_all(text, _MEASURE_TERMS):
            raw.append(_Match(kind="measure", surface=item.surface, start=item.start, end=item.end,
                              normalized="noun"))

        # 聚合信号本身也是指标信号（"一共多少" 没说指标名但显然在要一个数）
        for term in _AGGREGATE_TERMS:
            for item in _find_all(text, (term,)):
                raw.append(_Match(kind="measure", surface=item.surface, start=item.start, end=item.end,
                                  normalized="aggregate"))

        # 数量问法："多少 / 几" —— 归为指标，但**标记 normalized**，
        # 便于澄清器区分"用户说了算哪个指标"与"用户只想问个数"。
        for hit in _COUNT_QUESTION_RE.finditer(text):
            raw.append(_Match(kind="measure", surface=hit.group(0), start=hit.start(), end=hit.end(),
                              normalized="count_question"))

        # 时间：相对词
        for term in _RELATIVE_TIME_TERMS:
            for item in _find_all(text, (term,)):
                raw.append(_Match(kind="time", surface=item.surface, start=item.start, end=item.end,
                                  normalized=term))

        # 时间：绝对年份 / 月 / 日 / 季度
        for hit in _ABSOLUTE_TIME_RE.finditer(text):
            surface = hit.group(0)
            if not hit.group("year"):
                continue
            raw.append(_Match(kind="time", surface=surface, start=hit.start(), end=hit.end(),
                              normalized=self._absolute_normalized(hit)))

        # 数值比较
        for hit in _NUMERIC_COMPARISON_RE.finditer(text):
            raw.append(_Match(kind="numeric_range", surface=hit.group(0),
                              start=hit.start(), end=hit.end(),
                              normalized=f"{hit.group('op')}{hit.group('value')}"
                                         + (hit.group("unit") or "")))

        # 否定（先记成独立要求，语义是"这个词作用于某个筛选"）
        #
        # ⚠ 「不含」要区分两种用法：
        #   (a) 否定筛选：「除了巴西」「不含运费」——要求 SQL 有反向条件
        #   (b) 区间开闭：「（2024-01-01 含，2025-01-01 不含）」——
        #       它是在说明右边界的开闭，正确 SQL 就是
        #       `sale_date >= '2024-01-01' AND sale_date < '2025-01-01'`，
        #       本来就没有也不需要 NOT IN / <> / NOT LIKE。
        #       把 (b) 当成 (a)，会让**写对了的 SQL** 被判成
        #       「问题要求排除某些取值，但 SQL 里没有任何反向筛选条件」，
        #       三轮重试后整题放弃（实测 30 道规模题里三档 Q04/Q05/Q06/Q08 共 12 题全部如此）。
        #
        # 判据只用位置：紧邻在前的若非空白字符是数字，说明它说的是
        # 「某个日期/数值 不含」这类区间端点描述，不是排除一个业务取值。
        # 不引入新词表 —— 业务取值的否定形态太多，加词必然漏。
        for item in _find_all(text, _NEGATION_TERMS):
            if item.surface in ("不含", "不包括") and _follows_number(text, item.start):
                continue
            raw.append(_Match(kind="negation", surface=item.surface, start=item.start, end=item.end))

        # 取值筛选：**从 normalizer 的 value 类要素补**，不另设词表。
        # 母方案 §5.2 明令「不能按常识猜取值」——「巴西」之所以能成为一条
        # 筛选要求，唯一依据是元数据里真的有这个取值。
        # 这里只收词面命中的部分；要素层补全在 `_augment_from_normalizer`。

        # 排名 / 前 N
        #
        # ⚠ 「最高的」有两种用法，必须分开，否则会与同一句里的「前 N 个」打架：
        #
        #   (a) 排序方向：「返回金额最高的前 10 个商品」——
        #       「最高的」只说明 ORDER BY ... DESC，真正的条数在「前 10 个」里。
        #       此时若把「最高的」也当成一条"取前 1 名"的要求，就会与
        #       「前 10 个」的要求互相冲突：SQL 无论写 LIMIT 1 还是 LIMIT 10
        #       都有一条要求过不去，三轮重试后整题放弃作答。
        #       实测：30 道规模题里 S1K/S10K/S100K 三档的 Q07 全部因此失败。
        #
        #   (b) 独立排名：「金额最高的那张发票是哪张？」——
        #       句中没有任何别的条数，此时"默认取一条"是正确解读。
        #
        # 判据只用位置（同一句里后面还有没有"前 N"），不引入词表：
        # 「其后紧跟着 前N/前 N 个」即视为方向词，交给「前 N」那条要求去管。
        ranking_hits = list(_RANKING_RE.finditer(text))
        for hit in ranking_hits:
            count = hit.group("count") or hit.group("count2")
            direction = hit.group("direction") or "前"
            if hit.group("direction") is not None and count is None:
                if _ranking_direction_is_sort_only(text, hit, ranking_hits):
                    continue
            raw.append(_Match(kind="ranking", surface=hit.group(0),
                              start=hit.start(), end=hit.end(),
                              normalized=f"{direction}{(count or '').strip()}"))

        # 分组
        #
        # ⚠ 裸「按」要区分 GROUP BY 与 ORDER BY：
        #   「按账单国家统计销售额」        → 分组
        #   「金额相同按 sale_id 升序」      → 排序，不是分组
        # 判据只用后文形态（`_SORT_TAIL_RE`），不加减词表：
        # 词表两头都动过，加词会漏、删词会误，形态判据对两类都稳。
        # 同一句里两个「按」各自独立判断，互不影响。
        for item in _find_all(text, _GROUPING_TERMS):
            if item.surface == "按" and _SORT_TAIL_RE.match(text[item.end:]):
                continue
            raw.append(_Match(kind="grouping", surface=item.surface, start=item.start, end=item.end))

        # 指代：**本身不是一条"要求"，而是"要求没落实"的证据**。
        # 故记 unresolved=True，由澄清器决定追问。
        #
        # ⚠ 判定必须区分「指代有没有被锚定」——只看词面会把 CS14 那一整类
        # 「该工单……工单编号 WO-QF-001」误判成缺对象（实测 32 题里误伤 7 题）。
        # 这类句子指代对象就在同一句里给出了，追问反而是在否认用户已经说过的信息。
        # 锚定判据见 `_deixis_is_anchored`，与被修饰名词无关（工单/合同/设备都算）。
        for hit in _DEIXIS_RE.finditer(text):
            if _deixis_is_anchored(text, hit.end(), literal_spans):
                continue
            raw.append(_Match(kind="object", surface=hit.group(0), start=hit.start(), end=hit.end(),
                              unresolved=True, normalized="deixis"))

        # 跨源编号
        for hit in _LITERAL_RE.finditer(text):
            raw.append(_Match(kind="cross_source_object", surface=hit.group(1),
                              start=hit.start(1), end=hit.end(1), normalized=hit.group(1)))

        # 文档词
        for item in _find_all(text, _DOC_TERMS):
            raw.append(_Match(kind="document_scope", surface=item.surface,
                              start=item.start, end=item.end))

        # 对象：**直接从元数据派生词表扫原文**，不依赖调用方传 NormalizeResult。
        #
        # 这是刻意与 `_augment_from_normalizer` 并存的两条路：
        #   本函数用 A 层词表（表名 + 中文说明）扫原文，覆盖"客户/发票/员工"
        #   这类标准说法；`_augment_from_normalizer` 再补上 B 层别名（"买家"）。
        # 只留后者会让本模块在 `normalized=None` 时（假件测试、纯文本调用）
        # 完全认不出对象 —— 那正是澄清器最需要它的场景。
        for item in self._object_matches(text):
            raw.append(item)

        # 条件作用域（S2）。**只在已有其它要求时才记**：单说一个"筛选"
        # 不构成一条独立要求，它修饰的是同句里那个筛选/分组。
        # 否则「帮我筛选一下」这种空指令会被记成一条 `value_scope` 要求，
        # 核对器随后要求 SQL 里有对应结构 —— 凭空造出一个验收条件。
        if len(raw) > 1:
            for item in _find_all(text, _SCOPE_TERMS):
                raw.append(_Match(kind="value_scope", surface=item.surface,
                                  start=item.start, end=item.end))

        # 关联粒度（S2）。同理只在有对象或指标时才记。
        # ⚠ 一词一类：**裸「每」撞上「每位/每个/每一」时要粒度赢**。
        # 「每个客户的发票总额」→ grouping（要按客户分组的汇总）；
        # 「每一位客户的发票总额」→ join_grain（要逐条结果）。
        # 若两边都记，核对器会同时去找 GROUP BY 和验去重粒度，判据打架。
        #
        # ⚠⚠ 为什么必须让粒度赢：`_GROUPING_TERMS` 里那个裸「每」会把
        # 「每一位」的前一个字先占成 grouping(0-1)，而 join_grain(0-2) 更长
        # 更具体。按通用区间重叠判「先到先得」会让 join_grain 被静默吃掉
        # （本分支最初就是这样不生效的：实测只有 grouping，没有 join_grain）。
        # 故这里反向操作：**把被更长粒度命中覆盖的 grouping 撤掉**。
        if len(raw) > 1:
            grain_hits = tuple(_find_all(text, _GRAIN_TERMS))
            for item in grain_hits:
                raw[:] = [
                    m for m in raw
                    if not (
                        m.kind == "grouping"
                        and m.start == item.start
                        and m.end < item.end
                    )
                ]
                raw.append(_Match(kind="join_grain", surface=item.surface,
                                  start=item.start, end=item.end))

        return _dedupe(raw)

    def _object_matches(self, text: str) -> list[_Match]:
        """从元数据词表里扫出问题提到的实体对象。

        `desc_alias`（如「客户」→`customer`）优先于裸表名（`customer`）：
        中文问句里出现英文表名的概率低，先收中文能避免同一个对象被记两次。
        """
        found: list[_Match] = []
        try:
            vocab = self._vocab_loader()
        except Exception:
            # 词表读不出来（元数据缺失、隔离运行）时**不阻断**：
            # 少认一个对象顶多让澄清器更保守，不该让整条链路崩掉。
            return found

        for alias, table in getattr(vocab, "table_desc_alias", {}).items():
            if len(alias) < 2:
                continue
            for item in _find_all(text, (alias,)):
                found.append(
                    _Match(kind="object", surface=item.surface, start=item.start,
                           end=item.end, normalized=table)
                )
        for table in getattr(vocab, "tables", ()):
            if len(table) < 3 or not table.isascii():
                continue
            for item in _find_all(text, (table,)):
                found.append(
                    _Match(kind="object", surface=item.surface, start=item.start,
                           end=item.end, normalized=table)
                )
        return found

    @staticmethod
    def _absolute_normalized(hit: re.Match) -> str:
        """把绝对时间命中压成一个规范串，便于下游比较与去重。"""
        year = hit.group("year")
        quarter = hit.group("quarter")
        month = hit.group("month") or hit.group("month2")
        day = hit.group("day")
        if quarter:
            lookup = {"一": "1", "二": "2", "三": "3", "四": "4"}
            return f"{year}-Q{lookup.get(quarter, quarter)}"
        if month and (day or hit.group("daym")):
            return f"{year}-{int(month):02d}-{int(day or hit.group('daym')):02d}"
        if month:
            return f"{year}-{int(month):02d}"
        return f"{year}"

    # ── 内部：转成类型化要求 ────────────────────────────────────────────
    def _to_requirements(self, original: str, matches: list[_Match]) -> list[QueryRequirement]:
        """把命中转成 `QueryRequirement`，并**逐条用原文复核**。"""
        out: list[QueryRequirement] = []
        for item in matches:
            # §5.1 第 2 条：每项必须能回到原话。切出来的片段对不上原文，
            # 说明位置算错了 —— 宁可不报这条要求，也不能报一条假的。
            if original[item.start:item.end] != item.surface:
                continue
            out.append(
                QueryRequirement(
                    kind=item.kind,  # type: ignore[arg-type]
                    surface=item.surface,
                    span=(item.start, item.end),
                    normalized=item.normalized,
                    unresolved=item.unresolved,
                    source="literal",
                    # 用 span 编号：原文不变则 id 稳定，清单合并/补抽时也不漂移。
                    requirement_id=f"{item.kind}:{item.start}-{item.end}",
                )
            )
        return out

    def _augment_from_normalizer(
        self,
        original: str,
        normalized: NormalizeResult | None,
        requirements: list[QueryRequirement],
    ) -> list[QueryRequirement]:
        """用要素识别结果补齐对象与时间，**不覆盖已有要求**。

        补进来的对象 `span` 指向原话里那个词（俗称/别名）的位置；
        找不到位置的对象**不补** —— 一条指不回原文的对象要求会把澄清
        引向一个用户根本没提的东西。
        """
        if normalized is None:
            return requirements

        known_objects = {
            item.normalized or item.surface
            for item in requirements
            if item.kind == "object"
        }
        added: list[QueryRequirement] = []

        for entity in normalized.entities:
            if entity.kind == "value":
                # 取值筛选：只补元数据登记过的取值（`via=alias` 即走词表命中）。
                # 认不出的取值不补 —— 补了就会要求 SQL 里出现一个
                # 系统自己都说不清来源的条件。
                if entity.via not in ("alias", "exact"):
                    continue
                span = self._locate_surface(original, entity.surface, entity.canonical)
                if span is None:
                    continue
                added.append(
                    QueryRequirement(
                        kind="value_filter",
                        surface=original[span[0]:span[1]],
                        span=span,
                        normalized=entity.canonical,
                        source="session",
                        requirement_id=f"value_filter:{span[0]}-{span[1]}",
                    )
                )
                continue
            if entity.kind not in ("table", "doc"):
                continue
            canonical = entity.canonical
            if not canonical or canonical in known_objects:
                continue
            span = self._locate_surface(original, entity.surface, canonical)
            if span is None:
                continue
            added.append(
                QueryRequirement(
                    kind="object",
                    surface=original[span[0]:span[1]],
                    span=span,
                    normalized=canonical,
                    source="literal",
                    requirement_id=f"object:{span[0]}-{span[1]}",
                )
            )
            known_objects.add(canonical)

        # 时间：要素识别算出的区间比词表更准（它做了日历归一化），
        # 但**只在词表没认出时间时才补**，避免同一段时间出现两条要求。
        if not any(item.kind == "time" for item in requirements):
            for expression in normalized.times:
                span = self._locate_surface(original, expression.surface, expression.surface)
                if span is None:
                    continue
                added.append(
                    QueryRequirement(
                        kind="time",
                        surface=original[span[0]:span[1]],
                        span=span,
                        normalized=f"{expression.start}~{expression.end}",
                        source="literal",
                    )
                )
                break  # 「相对时间每轮只取一个锚点」（§5.1 第 4 条）

        return requirements + added

    @staticmethod
    def _locate_surface(original: str, *candidates: str) -> tuple[int, int] | None:
        """在原文里找出这些候选串中第一个真实出现的位置。"""
        for candidate in candidates:
            text = (candidate or "").strip()
            if not text:
                continue
            index = original.find(text)
            if index >= 0:
                return (index, index + len(text))
        return None

    # ── 内部：能力层取值 ────────────────────────────────────────────────
    def _augment_from_capability(
        self, original: str, requirements: list[QueryRequirement]
    ) -> list[QueryRequirement]:
        """用能力层补取值条件（「男」→ `gender=M`）。

        **为什么需要这一路**：别名词表（`load_vocab().alias`）是围绕**业务对象**
        建的（客户/发票/员工），不覆盖**列取值**（性别、婚姻状况、订单状态）。
        结果是「男的有多少？」提取到的只有 measure —— 澄清器看不出缺什么，
        而 SQL 也不会带 gender 过滤，**答出一个没加条件的数字还不报错**。

        取值从元数据来（列说明 + 采样值），**不从列名猜**；判定细节见
        `app/nl2sql/value_matcher.py` 的三条硬纪律。能力层缺席时本函数是空操作。
        """
        if self._value_matcher is None:
            return requirements

        # 已被要求覆盖的区间：同一片段不重复记（词表/编号/时间都可能先占位）
        taken = [(item.span[0], item.span[1]) for item in requirements]
        existing_values = {
            (item.normalized or "").casefold()
            for item in requirements
            if item.kind == "value_filter"
        }

        try:
            matches = self._value_matcher.match(original)
        except Exception:
            # 能力层数据异常不该阻断整条链路：少一条取值条件顶多让闸门标黄，
            # 不该让用户的问题答不出来。
            return requirements

        added: list[QueryRequirement] = []
        for match in matches:
            span = match.span
            if any(span[0] < end and start < span[1] for start, end in taken):
                continue
            if match.value.casefold() in existing_values:
                continue
            added.append(
                QueryRequirement(
                    kind="value_filter",
                    surface=original[span[0]:span[1]],
                    span=span,
                    # normalized 记**库里的规范取值**（`M`），不是用户原词（「男」）：
                    # 下游要拿它进 SQL，必须是与列取值可比的那个值。
                    normalized=match.value,
                    source="literal",
                    requirement_id=f"value_filter:{span[0]}-{span[1]}",
                )
            )
            existing_values.add(match.value.casefold())
            taken.append(span)
        return requirements + added

    # ── 内部：会话继承 ──────────────────────────────────────────────────
    @staticmethod
    def _augment_from_inherited(
        requirements: list[QueryRequirement],
        inherited: "QueryRequirements | None",
    ) -> list[QueryRequirement]:
        """把上一轮仍生效、且本轮没再提的要求补进来。

        **颗粒度按 kind 判**，不按 (kind, normalized)：
        「2023年有多少客户」→「那员工呢」里，`time 2023` 与本轮无冲突，
        应当继承；但如果按 normalized 判，本轮也可能有别的 time 而把旧的挤掉。

        规则：
          - 本轮**已有**该 kind 的要求 → 不继承该 kind（用户本轮的话优先）。
            这一条同时挡住了"两个不同对象混进同一份清单"。
          - 继承来的标 `source="session"`，`span=(0,0)`（它不来自本轮原文，
            编一个假位置会让"能否回到原话"的校验失真）。
          - `required=False`：继承条件是**提示性**的。用户换了话题但没说不用
            旧条件时，它应该帮助理解，而不该让核对器判 `repair` 打回 SQL。
        """
        if inherited is None:
            return requirements

        present_kinds = {item.kind for item in requirements}
        # 对象类只要有一个就够 —— 本轮说了"员工"，就不该再把"客户"带进来
        if present_kinds & {"object", "cross_source_object"}:
            present_kinds |= {"object", "cross_source_object"}

        added: list[QueryRequirement] = []
        for item in inherited.actionable:
            if item.kind in present_kinds:
                continue
            # 动作型要求不继承（2026-10-18 gmt-03 第 2 轮实测）：
            # 第 1 轮「…最多？有几个？」产出 `ranking:最多`，第 2 轮
            # 「他的项目一共花了多少钱？」什么都不问排名，却因为继承了
            # `ranking` 被核对器判「问题要求取前几名，但 SQL 没有排序条件」，
            # 一条完全正确的求和 SQL 被打回。
            # 排名与分组说的是「这一轮怎么算」，属于一次性指令；跨轮该继承的
            # 是**筛选范围**（对象/时间/取值），不是上一轮的动作。
            if item.kind in _NON_INHERITABLE_KINDS:
                continue
            added.append(
                QueryRequirement(
                    kind=item.kind,
                    surface=item.surface,
                    span=(0, 0),
                    normalized=item.normalized,
                    unresolved=item.unresolved,
                    source="session",
                    requirement_id=f"session:{item.kind}:{item.normalized or item.surface}",
                    required=False,
                )
            )
        return requirements + added

    def _unresolved_spans(
        self, original: str, requirements: list[QueryRequirement]
    ) -> tuple[str, ...]:
        """记下"看起来在提条件、但没归成任何要求"的片段。

        当前实现刻意保守：只在**完全没有解析出任何要求**时提示一句，
        不试图去猜"这段文字是不是漏了"。猜错会把正常问题误判成缺项，
        代价比漏报高得多 —— 而漏报有 §9.5 的独立新题兜底。
        """
        if requirements:
            return ()
        if not original:
            return ()
        return ("未从本轮提问中解析出任何可执行要求",)


def _default_scope() -> str:
    """当前进程的业务库身份。

    实施方案 §5.2 要求来源身份用「逻辑数据源 + schema + 表 + 字段」，
    不能用同名表或中文名关联不同库。这里给出**逻辑数据源**这一层；
    schema/表/字段由 catalog 在执行期补。
    """
    return (os.environ.get("NL2SQL_DATABASE", "") or "").strip()


def _default_capability():
    """按当前元数据构造能力层；任何一步失败都返回 `None`。

    **刻意吞掉异常**：取值匹配是"锦上添花"的一路（词表已能覆盖大部分取值），
    它坏了不该让整条问答链路崩掉。返回 `None` 时提取器自动退化为纯词表模式，
    行为与加入本特性之前一致。
    """
    try:
        from app.nl2sql.capability_runtime import load_capability
        from app.nl2sql.schema import SchemaCatalog

        return load_capability(SchemaCatalog.load())
    except Exception:
        return None


def _locus_of_measure(
    text: str,
    requirement_surfaces: tuple[str, ...],
    capability,
) -> bool:
    """指标说法能否落到**某个具体的列**上。

    **要解决的两件不同的事**：
      - 「有多少位？」—— 指标是"数量"，但数什么完全没说，必须追问（SC-02）；
      - 「各地市中标金额的平均值是多少？」—— 指标已经点名到 `awards.award_yuan`，
        再追问「请补充要统计的业务对象」就是误伤。

    后者的判据**不是**题面里出现了某张表对应的业务对象词（那句话里一个表名
    都没提），而是：题面里的指标说法能在能力层的**列说明**里找到落点。

    为什么用列说明而不是词表：词表要穷举（中标金额/成交金额/预算金额/单价…），
    政采、chinook、AdventureWorks 三套库各有自己的说法，穷举必然漏；而每张表
    的列说明本来就写着「中标金额，单位元」「项目预算金额，单位元」，是这套系统
    里唯一可信的指标释义来源。这也与 `ValueMatcher` 用列说明做释义桥同源。

    匹配方式：题面里的指标片段与列说明的**首段**（第一个逗号/句号之前）互相包含，
    且列只能是三类之一：金额列、数量列，或**枚举分类列**
    （`amount_columns` / `quantity_columns` / `category_columns`）。
    不加这条限制，「项目名称」这种自由文本列的说明也会被当成指标落点。

    ⚠ 枚举分类列是 2026-10-18 补进来的，起因是政采鲁棒题 grob-16
    「预算及次没法判断的项木一共有多少个？」被拦：题面点名要数的对象是
    `projects.budget_level` 的 unknown 那一档（列说明首段正是「预算级次」），
    可它既不是金额列也不是数量列，于是被当成"没有落点"追问。
    枚举列的取值域是可穷举的（central/local/unknown、公告类型…），
    用户点名其一时"数什么"就是确定的；而自由文本列（名称、地址、备注）
    的取值域无法穷举，"数它"没有业务含义，所以仍排除在外。
    这条限制同时也是「金额是多少？」仍会被追问的原因 ——
    裸"金额"不是任何列说明的首段。

    capability 为 None（纯词表模式）时返回 False：没有元数据就没有落点判据，
    宁可按原逻辑追问，也不凭空放行。
    """
    if capability is None or not text:
        return False
    catalog = getattr(capability, "catalog", None)
    names = getattr(catalog, "table_names", None)
    if not callable(names):
        return False

    phrases = []
    for surface in requirement_surfaces:
        cleaned = "".join((surface or "").split())
        # 裸指标词本身不算落点：「数量是多少？」「金额是多少？」里的"数量/金额"
        # 与任何一张金额列、数量列的说明都能对上（说明里就写着"数量"），
        # 可用户压根没说数谁的数量 —— 那正是要追问的情形。
        # 要求至少 3 个字，等于要求带限定语（"中标金额""预算金额""采购数量"）。
        if len(cleaned) >= 3 and cleaned not in phrases:
            phrases.append(cleaned)
    # 提取器只能给出指标词本身（"金额"），给不出它的修饰语（"中标"）——
    # 而修饰语恰恰是决定落点的那部分（中标金额 vs 预算金额是两张表两列）。
    # 所以再从**整句**里取"指标词前面紧邻的那几个字"拼成候选短语。
    haystack = "".join(text.split())
    for surface in requirement_surfaces:
        core = "".join((surface or "").split())
        if len(core) < 2:
            continue
        cursor = haystack.find(core)
        while cursor >= 0:
            for span in (2, 3, 4):
                if cursor - span >= 0:
                    candidate = haystack[cursor - span : cursor + len(core)]
                    if candidate not in phrases:
                        phrases.append(candidate)
            cursor = haystack.find(core, cursor + 1)
    # ⚠ 不能在这里"phrases 为空就 return False"。
    #
    # 这里原本是短路返回，2026-10-18 改成继续往下走：下面除了按 `phrases`
    # 匹配列说明首段，还有一条**不依赖 phrases** 的判据 —— 整句直接点名了
    # 某个枚举列（见循环里的第三条路）。归一器把「预算及次」改写成
    # `budget_level` 之后，measure 清单里一个 noun 都不剩，phrases 必然是空的，
    # 短路会让那条判据永远执行不到。
    #
    # 代价可控：phrases 为空时循环里只剩第三条路，而它要求
    # 「列名或列说明首段（≥4 字）原样出现在句子里」。

    for table in names():
        columns: list[str] = []
        category_columns: tuple[str, ...] = ()
        try:
            category_columns = capability.category_columns(table)
            columns.extend(capability.amount_columns(table))
            columns.extend(capability.quantity_columns(table))
            columns.extend(category_columns)
        except Exception:
            continue
        for column in dict.fromkeys(columns):
            try:
                description = capability.business_meaning(table, column) or ""
            except Exception:
                continue
            head = re.split(r"[,，。;；]", description, maxsplit=1)[0].strip()
            if len(head) < 2:
                continue
            for phrase in phrases:
                if phrase in head or head in phrase:
                    return True
            # 第三条路：整句里点名了这个**列本身**。
            #
            # `phrases` 是从"measure 要求的 surface"派生的，而 measure 要求只收
            # 归一化后仍留在句中的指标词。词被归一器改写成英文列名后（政采
            # grob-16「预算及次」→「budget_level」），measure 清单里只剩
            # 「一共」「多少个」，列名本身进不了 `phrases`。
            #
            # 判据取**列名或列说明首段**出现在句中。这两者都是"点到了具体字段"
            # 的直接证据：用户既然说了「预算级次」，他要数的是什么就是确定的，
            # 哪怕他接着说的是「没法判断的」（取值由过滤条件给，不需要再问）。
            #
            # 只对枚举分类列开这个口子，不对自由文本列开：
            # `project_name`/`buyer_name` 的说明首段是「项目名称」「采购人名称」，
            # 「名称」两个字太泛，谁都能点上；而「预算级次」「采购方式」
            # 「公告类型」是可穷举维度，点名即确定。
            bare_column = column.split(".")[-1]
            named = (
                bare_column
                and len(bare_column) >= 3
                and bare_column.casefold() in haystack.casefold()
            ) or _head_is_specific(head, haystack)
            if named and column in category_columns:
                return True
    return False


#: 列说明首段里可用于判定"点名"的最短长度。
#: 「名称」「备注」「地址」这类两字泛词在任何问句里都可能出现，不能当证据；
#: 三字以上（预算级次 / 采购方式 / 公告类型）才算点名了具体维度。
_SPECIFIC_HEAD_MIN = 4

#: 不跨轮继承的要求种类。见 `_augment_from_inherited` 里的说明：
#: 它们描述「这一轮怎么算」，属于一次性动作指令，不是可延续的筛选范围。
_NON_INHERITABLE_KINDS = frozenset({"ranking", "grouping"})


def _head_is_specific(head: str, haystack: str) -> bool:
    """列说明首段是否够具体，且出现在句子里。

    「预算级次：central 中央预算…」的首段在第一个中文冒号处就该截断 ——
    否则截出来的是整条说明，`in` 判断永远不成立。
    """
    label = head.split("：", 1)[0].split(":", 1)[0].strip()
    if len(label) < _SPECIFIC_HEAD_MIN:
        return False
    return label in haystack


def extract_requirements(
    question: str,
    normalized: NormalizeResult | None = None,
    *,
    scope: str | None = None,
    inherited: QueryRequirements | None = None,
    capability=None,
) -> QueryRequirements:
    """模块级便捷入口。编排层与澄清器都走它，保证同一份快照。

    `capability=None` 时按当前元数据现建一个（见 `_default_capability`）；
    显式传 `capability=False` 可关掉取值匹配（纯词表模式，测试用）。
    """
    if capability is False:
        engine = None
    elif capability is None:
        engine = _default_capability()
    else:
        engine = capability
    return RequirementExtractor(capability=engine).extract(
        question,
        normalized,
        scope=_default_scope() if scope is None else scope,
        inherited=inherited,
    )


__all__ = [
    "REQUIREMENTS_VERSION",
    "RequirementExtractor",
    "extract_requirements",
]
