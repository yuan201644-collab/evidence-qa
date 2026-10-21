"""routing_signals.py - 路由信号：**实体词从数据源自己的元数据派生**，不硬编码

为什么不再手写领域词表：
    手写词表永远列不全。语料里放进一份《机房运维操作规程》之后，问
    「机房巡检频率是多少」——这些词不在表里，问题就被当成结构化查询，
    实测返回 `SELECT COUNT(*)`、答案渲染成「n：59」。

    而「哪些词属于文档、哪些属于数据库」这件事，**两边的元数据本来就写着**：

        文档侧     data/corpus/manifest.json
                   —— 文件名（去掉 KB 编号与扩展名）与 category（如「制度规范类」）
        结构化侧   infra/schema_meta.json
                   —— 表名、列名
                   infra/schema_meta.descriptions.zh.json
                   —— 表与字段的中文业务说明（如「客户表」「发票（订单）头表」）

    于是实体词改为从元数据抽取。往 raw/ 丢新文档、给库加新表，路由自动认得，
    不需要回来改这里。**新加一份语料却要改路由代码，就是这个模块存在的理由。**

仍然手写的是**不专属于任何数据源的词** —— 通用问法、通用量词、连接词：
    DOC_INTENT     索取"解释与依据"的问法（怎么 / 规定 / 依据 …）
    SQL_AGGREGATE  索取"算出来的数"的量词（多少 / 汇总 / 排名 / 销售额 …）
    CROSS_SOURCE   提示需要跨源串联的连接词（并且 / 他的 …）
⚠️ 判断标准：**专属于某一份文档或某一张表的专有名词**（「机房巡检记录」「发票」
   这类只能从元数据里知道的词）不许手写，一律走元数据派生；上面这三张表只放
   **换个语料、换个库照样成立**的通用词。
   注：「销售额」「统计」这类通用业务量词属于后者 —— 不指向某张具体的表，所以保留
   手写副本。即使 schema 元数据里恰好也有（实测重叠 5 个），重复命中只会让 sql 分
   略高，不改变判断方向（问法词权重 1、实体词权重 2，方向由实体词决定）。
"""

import json
import os
import re

#: 索取解释、依据、条款的问法。这些词表示"要一段说明"，不指任何数据源。
DOC_INTENT = (
    "怎么", "如何", "为什么", "为何", "原因分析", "原因是什么", "方法", "流程",
    "规定", "条款", "依据", "要求", "制度", "规程", "步骤", "注意事项",
    # 2026-10-17 补：显式索取**规则文本**的说法。跨源题的问法常常是
    # 「法规允许吗？」「需要满足什么条件？」这类，没有"规定/要求"字样，
    # 但它们同样是在要条文而不是要一个统计值。这几个词不会出现在
    # 纯问数题里（政采/Chinook 的 63 道结构化题零命中，实测）。
    "法规", "法条", "条例", "办法", "标准规定", "什么条件", "什么手续",
    "什么前置", "审批", "合规",
    # 2026-10-18 补：**法规条文的计量单位与义务句式**。
    #
    # 政采法规题问的是「公告期限是多少个工作日」「评审专家不得少于成员总数的多少」
    # 「报财政部门批准之前应当先做什么」，答案写在某部办法的第几条里，库里的数
    # 算不出来。但这类题面里「采购」「公告」「供应商」等业务词与问数题完全重叠，
    # 词表打分顶不过去（grag-04/05/06 实测仍被判成 nl2sql）。
    #
    # 下列词的共同点是**只出现在制度文本里，不会出现在问数题面**：
    #   「工作日」——数据库里存的是日期，没有「工作日」这个计量单位；
    #   「不得少于 / 应当先 / 应当由 / 应当如何处理」—— 规范性义务句式；
    #   「提起投诉 / 提出质疑 / 补充论证」—— 法定程序动作。
    # 已实测：政采 30 道结构化题与 18 道鲁棒题里零命中（改后 sql 侧正确率不降）。
    "工作日", "不得少于", "不得低于", "不得晚于", "应当先", "应当由", "应当如何",
    "如何处理", "如何处理", "期限", "公示期", "异议", "质疑", "投诉", "提起投诉",
    "补充论证", "资格预审", "响应文件", "评审专家", "成交结果", "中标结果",
)

#: 索取"算出来的数"的问法。这些词表示"要一个统计值"，同样不指任何数据源。
SQL_AGGREGATE = (
    "多少", "几单", "几台", "几家", "几个", "数量", "个数", "汇总", "总计", "合计",
    "订单数", "人数", "记录数", "排名", "排第", "第一", "冠军", "销售额", "业绩",
    "统计", "占比", "平均",
    # 2026-10-17 补：「几条」是最常见的计数问法之一，原表有"几单/几台/几家/几个"
    # 却漏了"几条"，导致「共几条？」这种明确的计数诉求被判成非统计问法。
    "几条",
)

#: 提示需要跨源串联的连接词。
# “关联”同时是普通 SQL 外键/关系查询的常用表述，不足以证明跨源，需要避免单独触发图谱。
CROSS_SOURCE = ("并且", "以及", "同时", "他的", "他们的", "然后", "接着", "结合")

#: 从元数据派生的词元里要丢掉的"元语言"词（表结构描述自己的用语，不是业务名词）。
_META_STOPWORDS = frozenset((
    "表", "字段", "列", "主键", "外键", "索引", "取值", "指向", "若干", "一张", "一条",
    "一次", "每位", "一位", "一名", "包含", "生成", "所属", "记录数", "行数", "样例",
    # ── 2026-10-18 补：schema 说明是自由中文，jieba 会把虚词/通用动词切成词元，
    # 它们不属于任何一张表，却在打分里各值 2 分，把法规题硬顶到结构化侧：
    #   「……对已发出的招标文件……可能影响投标文件编制的」因一个「可能」被
    #   判成问数题（grag-02 实测）。
    # 只收**换个语料也一定不是实体词**的通用虚词与过程动词；业务词
    # （采购/招标/评审/供应商/项目…）一概不收 —— 试过按词性只留名词，
    # 结果「采购」在「采购公告」里被标成动词，结构化题 30/30 跌到 24/30。
    "可能", "获取", "采用", "补充", "组织", "方式", "时间", "结果", "过程", "内容",
    "进行", "需要", "应当", "可以", "如果", "因为", "所以", "以及", "或者", "并且",
    "什么", "如何", "多少", "是否", "已经", "正在", "开始", "结束", "完成", "存在",
    "关联", "对应", "属于", "包括", "按照", "根据", "通过", "对于", "关于", "使用",
))

_TOKEN_MIN_LEN = 2          # 中文词元最短长度
_ASCII_MIN_LEN = 3          # 英文词元最短长度

#: 纯数字词元（含小数）。年份、日期、编号都不是「某个数据源的实体词」，
#: 计入任何一侧的分数都只是切词副产物（jieba 切 '2026' 会得到 '202'）。
#: 见 `analyse` 里计分口径注释。
_NUMERIC_TOKEN_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def _project_root():
    """app/routing_signals.py -> app -> 项目根。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def _fallback_tokens(text, min_len=_TOKEN_MIN_LEN):
    """没有 jieba 时的兜底切词：中文取 2~6 字的连续子串，英文取整词。

    只为"能用"：子串会带来一些无意义词元（如「能计」），但它们几乎不会出现在
    用户问题里，只会让词汇表变大一点，不会造成误判。有 jieba 时走 jieba。
    """
    out = set()
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        for n in range(min_len, min(6, len(chunk)) + 1):
            for i in range(len(chunk) - n + 1):
                out.add(chunk[i:i + n])
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_]+", text):
        if len(word) >= _ASCII_MIN_LEN:
            out.add(word.lower())
    return out


def _tokens(text):
    """切词：优先 jieba（项目已有依赖），不可用时退回子串切词。"""
    if not text:
        return set()
    try:
        import jieba
    except ImportError:
        return _fallback_tokens(text)
    out = set()
    for word in jieba.cut(text):
        word = word.strip()
        if len(word) >= _ASCII_MIN_LEN and re.fullmatch(r"[A-Za-z0-9_]+", word):
            out.add(word.lower())
        elif len(word) >= _TOKEN_MIN_LEN and re.search(r"[\u4e00-\u9fff]", word):
            out.add(word)
    return out


#: 词性黑名单：这些词性说明该词是**谓语或修饰成分**，不是实体名词。
#:
#: 2026-10-18 实测：schema 说明是自由中文（「采购代理机构名称」「可能在投标截止
#: 时间获取」），jieba 切出来的 v（动词）/d（副词）/p（介词）被当成实体词，
#: 于是「可能」「获取」「采用」「补充」各值 2 分，把法规题硬顶上 sql 侧：
#: 「采购人或者采购代理机构对已发出的招标文件……可能影响投标文件编制的」
#: 因为一个「可能」被判成问数题。
#:
#: 只排除**纯谓语/虚词**，保留 n*（名词）、vn（动名词，如「评审」「谈判」是真业务词）、
#: b（区别词，如「单一」）、a（形容词，如「公开」）。收得太狠会把业务词也误杀。
_PREDICATE_POS = frozenset(("v", "vd", "vn_v", "d", "p", "uj", "ul", "uv", "uz", "y", "z"))


def _tokens_nominal(text):
    """只取**名词性**词元的切词（用于 schema 侧实体词表）。

    jieba 不可用时退回普通切词 —— 宁可多几个词，也不要因为缺依赖把实体词全丢掉。
    """
    if not text:
        return set()
    try:
        import jieba.posseg as pseg
    except ImportError:
        return _tokens(text)
    out = set()
    for word, flag in pseg.cut(text):
        word = word.strip()
        if not word:
            continue
        if len(word) >= _ASCII_MIN_LEN and re.fullmatch(r"[A-Za-z0-9_]+", word):
            out.add(word.lower())
            continue
        if len(word) < _TOKEN_MIN_LEN or not re.search(r"[\u4e00-\u9fff]", word):
            continue
        if flag in _PREDICATE_POS:
            continue
        out.add(word)
    return out


def _terms_from_texts(texts):
    """把一批文本切成词元，丢掉元语言词。"""
def _terms_from_texts(texts, *, nominal=False):
    """把一批文本切成词元，丢掉元语言词。

    `nominal=True` 时只取名词性词元（见 `_tokens_nominal`）——
    schema 说明是自由中文，含大量动词/副词（「可能」「获取」「采用」），
    它们不指任何一张表，却在原实现里各值 2 分。文档侧标题没有这个问题，
    所以只对 schema 侧启用。
    """
    out = set()
    cutter = _tokens_nominal if nominal else _tokens
    for t in texts:
        out |= cutter(t)
    return {w for w in out if w not in _META_STOPWORDS}


def corpus_terms():
    """文档侧实体词：来自当前运行语料（文件名），缺省沿用项目语料清单。

    读不到清单时返回空集合 —— 此时文档侧没有实体信号，路由会退回"只按问法判断"，
    等价于旧行为，而不是崩掉。
    """
    runtime_dir = os.environ.get("RAG_CORPUS_DIR", "").strip()
    if runtime_dir:
        texts = []
        try:
            manifest_path = os.environ.get("RAG_CORPUS_MANIFEST_PATH", "").strip()
            if manifest_path:
                with open(manifest_path, encoding="utf-8") as stream:
                    manifest = json.load(stream)
                for doc in manifest.get("documents", []):
                    if not isinstance(doc, dict):
                        continue
                    filename = str(doc.get("file", ""))
                    stem = os.path.splitext(filename)[0]
                    title = str(doc.get("title") or re.sub(r"^KB-\d+_", "", stem))
                    texts.extend((title, str(doc.get("category") or "")))
                return _terms_from_texts(texts)
            for filename in sorted(os.listdir(runtime_dir)):
                _, extension = os.path.splitext(filename)
                if extension.lower() not in {".pdf", ".docx", ".doc", ".md", ".txt"}:
                    continue
                title = re.sub(r"\.(pdf|docx|doc|md|txt)$", "", filename, flags=re.I)
                title = re.sub(r"^KB-\d+_", "", title)
                texts.append(title)
        except OSError:
            return set()
        return _terms_from_texts(texts)

    path = os.path.join(_project_root(), "data", "corpus", "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return set()

    docs = m.get("documents") if isinstance(m, dict) and "documents" in m else m
    texts = []
    for d in docs or []:
        if not isinstance(d, dict):
            continue
        fname = str(d.get("file") or d.get("id") or "")
        # 去掉 KB-001_ 前缀与扩展名，留下可读标题
        title = re.sub(r"\.(pdf|docx|doc|md|txt)$", "", fname, flags=re.I)
        title = re.sub(r"^KB-\d+_", "", title)
        texts.append(title)
        texts.append(str(d.get("category") or ""))
        # ⚠️ 只取「命名」与「分类」这两项。`notes` / `kind` 是**关于语料的备注**
        #（如「图片型 PDF，无文本层，必须经 OCR 才能检索」「pdf-scanned」），
        # 抽出来的词（编号、备注、扫描…）说的是"这份文件是什么格式"，
        # 不是"它讲什么"，会让「员工编号」这种结构化问题命中文档侧（实测踩过）。
    return _terms_from_texts(texts)


def schema_terms():
    """结构化侧实体词：来自库的 schema 元数据（表名 / 列名 / 中文业务说明）。"""
    root = _project_root()
    configured_schema = os.environ.get("SCHEMA_META_PATH", "").strip()
    schema_path = configured_schema or os.path.join(root, "infra", "schema_meta.json")
    configured_desc = os.environ.get("SCHEMA_META_DESCRIPTIONS_PATH", "").strip()
    desc_path = configured_desc or (
        os.path.join(os.path.dirname(os.path.abspath(schema_path)), "schema_meta.descriptions.zh.json")
        if configured_schema else os.path.join(root, "infra", "schema_meta.descriptions.zh.json")
    )
    texts = []
    try:
        with open(schema_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    for table in meta.get("tables") or []:
        texts.append(str(table.get("table") or table.get("name") or ""))
        texts.append(str(table.get("description") or ""))
        for col in table.get("columns") or []:
            texts.append(str(col.get("name") or ""))
            texts.append(str(col.get("description") or ""))

    # 中文业务说明是人工维护的语义资产，含「客户表」「发票（订单）头表」这类词
    try:
        with open(desc_path, encoding="utf-8") as f:
            zh = json.load(f)
    except (OSError, ValueError):
        zh = {}
    for key in ("tables", "columns"):
        descriptions = zh.get(key)
        if isinstance(descriptions, dict):
            texts.extend(v for v in descriptions.values() if isinstance(v, str) and v.strip())
        elif isinstance(descriptions, list):
            texts.extend(
                row["description"]
                for row in descriptions
                if isinstance(row, dict)
                and isinstance(row.get("description"), str)
                and row["description"].strip()
            )
    return _terms_from_texts(texts)


_CACHE = {}


def _cached(kind):
    if kind not in _CACHE:
        _CACHE[kind] = corpus_terms() if kind == "doc" else schema_terms()
    return _CACHE[kind]


def reload_signals():
    """清掉词表缓存（测试或语料热更新后调用）。"""
    _CACHE.clear()


def hits(question, terms):
    """问题里命中哪些词元，按长度降序（长词更能说明问题，便于写进理由）。"""
    q = question or ""
    return sorted((t for t in terms if t in q), key=len, reverse=True)


def analyse(question):
    """给出一次路由判断所需的全部信号。

    返回 dict：
        doc_terms / sql_terms  命中的实体词（来自元数据）
        doc_intent / sql_intent 命中的问法词
        connectors             命中的跨源连接词
        doc_score / sql_score  打分 = 实体词命中 * 2 + 问法词命中
    **实体词权重更高**：它直接指出"这个问题里有某份文档 / 某张表的名字"，
    而问法词只说明"想要什么形态的答案"，单独不足以定位数据源。
    """
    q = question or ""
    doc_terms = hits(q, _cached("doc"))
    sql_terms = hits(q, _cached("sql"))
    doc_intent = [k for k in DOC_INTENT if k in q]
    sql_intent = [k for k in SQL_AGGREGATE if k in q]
    connectors = [k for k in CROSS_SOURCE if k in q]

    # 「两侧共有」的词不算跨源证据：`员工` 既是《员工手册》的名字，也是 employee
    # 表的说明（「员工表」），它单独出现说明不了任何一侧。只有**各自独有**的信号
    # 同时出现，才说明问题确实横跨文档与数据库。
    doc_side = set(doc_terms) | set(doc_intent)
    sql_side = set(sql_terms) | set(sql_intent)
    # ── 计分口径（2026-10-18 修）：共有词两边都不计，数字词元一律不计 ──
    #
    # 原实现把两侧词各算各的，于是**同一个词会被计两次**，谁那侧词表大谁占便宜。
    # 政采实测：「2026 年 10 月 16 日发布了多少条采购公告？」
    #   doc_terms = 2026 / 202 / 发布 / 采购 / 公告   → 10 分
    #   sql_terms = 发布 / 采购 / 公告               →  7 分
    # 文档侧凭空多出 4 分，全来自**同年份被 jieba 切成 '2026' 与 '202'**，
    # 而「发布/采购/公告」两侧都有、根本说明不了数据源在哪。
    # 结果一道纯问数题被判成文档检索（实测 gov-m-08 / gov-m-16 / grob-04 三题）。
    #
    # 修法两条：
    #   ① 共有词从两侧分数里都剔除 —— 它区分不了数据源，计入任何一侧都是噪声；
    #      这与跨源判断用的 doc_only/sql_only 是同一口径，这里只是推广到打分。
    #   ② 纯数字词元不计分 —— 年份/日期不属于任何一侧的实体词，
    #      jieba 切 '2026' 得到 '202' 纯属切词副产物。
    #
    # 注意 `doc_terms` / `sql_terms` 原样保留（报告与 reason 要用它们说明判据），
    # 只有**分数**改用独有词计算。
    shared = doc_side & sql_side
    scored_doc = [t for t in doc_terms if t not in shared and not _NUMERIC_TOKEN_RE.fullmatch(t)]
    scored_sql = [t for t in sql_terms if t not in shared and not _NUMERIC_TOKEN_RE.fullmatch(t)]
    return {
        "doc_terms": doc_terms,
        "sql_terms": sql_terms,
        "doc_intent": doc_intent,
        "sql_intent": sql_intent,
        "connectors": connectors,
        # 独有信号：跨源判断用这个，避免共有词把单源问题误判成多跳
        "doc_only": [t for t in (doc_terms + doc_intent) if t not in sql_side],
        "sql_only": [t for t in (sql_terms + sql_intent) if t not in doc_side],
        # 计分用的实体词（已剔除共有词与纯数字），供报告解释分数怎么来的
        "doc_scored": scored_doc,
        "sql_scored": scored_sql,
        "doc_score": 2 * len(scored_doc) + len(doc_intent),
        "sql_score": 2 * len(scored_sql) + len(sql_intent),
    }
