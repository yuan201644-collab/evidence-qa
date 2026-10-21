# eval —— 评测集与一键跑分

责任人：C。这一块对应初赛评分表里 **40 分的客观分**：
功能指标 1（基础 NL2SQL 单表，10 分）+ 功能指标 2（基础 RAG 单文档，10 分）
+ 性能指标 1（NL2SQL 执行准确率，10 分）+ 性能指标 2（RAG 答案忠实度，10 分）。
这 40 分不是评委印象分，是用例数决定、用阈值决定，所以它必须**先于**系统做完。

## 目录

```
eval/
├─ cases/
│  ├─ nl2sql.jsonl        15 条（简单/中等/困难 各 5）
│  ├─ rag.jsonl           21 条（单文档/扫描件/表格行/目录混乱/公式/多跳）
│  ├─ rag_multihop.jsonl  20 条（跨文档 / 跨条款多跳专项，全 hard）
│  └─ robust_cases.jsonl  18 条 鲁棒性回归（错别字 / 别名 / 模糊输入 / 时间词 / 非标准格式）
├─ examples/
│  └─ predictions.example.jsonl   预测文件格式示例（nl2sql / robust / rag 三种一行一条）
├─ metrics.py             NL2SQL 结果集判定 + 指标聚合 + 70/80/90 档位
├─ rag_metrics.py         RAG 检索命中率 + 答案忠实度 + 单题通过计数
├─ preflight_rag_env.py   RAG 环境预检（依赖 / 索引 / 模型可用性）
├─ test_metrics.py        NL2SQL 判定器单元测试（不需要 pytest）
├─ test_rag_metrics.py    RAG 判定器单元测试（含字段名回归闸门）
├─ test_validate_cases.py 用例结构校验器的回归测试
├─ test_runner_selfcheck.py  oracle 自证闸门的回归测试（防新增套件漏接线）
├─ runner.py              一键跑分
├─ tools/
│  ├─ local_check.py      本机无 Docker 时，用 SQLite 把用例真跑一遍
│  ├─ validate_cases.py   用例结构校验（覆盖上面四个用例集）
│  ├─ parse_health.py     语料解析体检
│  └─ enrich_gold_snippets.py  从语料生成器逐字取引用片段，补进 rag.jsonl
└─ reports/               跑分产物（不入库，除 .gitkeep）
```

## 跑法

```bash
# 0) 先确认用例本身没写错
python eval/tools/validate_cases.py

# 1) 判定器自证：这一步必须 100%，否则后面所有分数都不可信
python eval/runner.py --predictor oracle --dialect sqlite

# 2) 空基线（09-23 要交的就是这个）
python eval/runner.py --predictor dry-run --suite all

# 3) 本地用例自检（不需要数据库）
python eval/tools/local_check.py

# 3.5) 判定器口径的单元测试（改过 metrics.py / rag_metrics.py 必跑）
python eval/test_metrics.py
python eval/test_rag_metrics.py

# 3.6) 校验器 / 自证闸门自身的回归测试（改过 validate_cases.py 或 runner 自证段必跑）
python eval/test_validate_cases.py
python eval/test_runner_selfcheck.py

# 4) 容器起来后，用真实 PostgreSQL 跑
python eval/runner.py --predictor oracle          # 先自证
python eval/runner.py --predictor jsonl --predictions preds.jsonl
python eval/runner.py --predictor http --endpoint http://127.0.0.1:8000/api/v1/query
```

**两条实测踩过的跑分坑（2026-10-02，DeepSeek 真实链路跑分时踩中）**：

1. **runner 读不到 `DATABASE_URL` 会回落到内存 SQLite**：**2026-10-04 起不再静默** ——
   回落时向 stderr 打印醒目告警，报告 `meta.dialect_source` 记为 `auto_fallback`、`meta.dialect` 为 `sqlite`。
   对外口径要求真实 PostgreSQL，判分时**显式传 `--dsn`**（或确保 shell 已加载 `.env` 的 `DATABASE_URL`）；
   出报告后先核对 `meta.dialect` 是否为 `postgres`（`dialect_source` 应为 `explicit` 或 `env_dsn`）再报数。
   若确实要用本地内存库，显式加 `--dialect sqlite` 即可消除告警（rag / multihop 套件不建执行器，不会告警）。
2. **生成预测必须设时间基准 `APP_TIME_ANCHOR=2024-06-15`**（评测基准日，
   见 `.env.example`「运行环境」节）。robust 的时间用例（rob-12~18）全按它归一化，
   不设则按系统当前时间，「去年 / 本季度」类用例必挂。

## 跑分产物带代码版本

每份跑分报告（JSON 与 Markdown）都带 `meta.git_commit`。这是
[`docs/评测口径_v0.1.md`](../docs/评测口径_v0.1.md) L238 的**硬要求**：
分数必须能对应到某一次具体的代码状态，否则「上周 92% 这周 87%」没法排查是改坏了还是运气。

```json
"meta": {
  "generated_at": "2026-09-20T16:13:02+00:00",
  "git_commit": "c06611e",
  "git_dirty": false,
  "predictor": "oracle",
  "dialect": "sqlite"
}
```

- `git_commit` 取 `git rev-parse --short HEAD`。**取不到就写 `"unknown"`**
  （没装 git、裸源码包、非仓库目录），不让评测因为缺版本信息跑不出分。
- `git_dirty` 是本实现**额外**加的字段（口径没要求）：`true` 表示跑分时工作区
  有未提交改动，这种分数**不可复现**。排查「改坏了还是运气」时它是第一个要排除的
  干扰项，所以顺手记下来。为 `true` 时 Markdown 报告和终端都会打警告。

**纪律**：要拿分数做横向对比（M3 之后连续跑分看趋势），先 `git commit` 再跑分，
让 `git_dirty` 为 `false` —— 否则两个数字之间的差异归因不了。

## 判定口径（v1.0.0，本文件即冻结版）

口径改动必须全员同步，因为它同时决定 A/B 的优化方向和对外报数的可信度。

### NL2SQL：执行结果集一致，不做 SQL 文本匹配

| 细则 | 做法 | 为什么 |
|---|---|---|
| 比较对象 | 两边 SQL 的实际执行结果 | 同一个问题有多种正确写法，比文本会把正确解判成错 |
| 行序 | **仅当 `gold_sql` 含 `ORDER BY` 时比对**；不含则忽略 | 没写 ORDER BY 时数据库返回顺序本来就不确定；写了 ORDER BY 顺序就是答案的一部分（口径 L88） |
| 列序 | 无关，但列数必须一致 | SELECT 列顺序不同不算错；列数不同说明取错了字段 |
| 浮点 | 绝对容差 `1e-6`，相对容差 `1e-6` | `NUMERIC` 与 float 的表示差异不该判错 |
| 空结果 | 空 = 空判通过，但标记 `degenerate` | 恒为空集的用例没有区分度，必须在报告里点名替换 |
| 报错 | 单独计 `error`，不并入 fail | 需要区分「SQL 写错跑不通」和「跑通了但答案不对」 |
| 列序宽松的假阳性 | 只有列序差异时判 `pass_loose`，单独计数 | 「列序无关」会带来 `(1,2)` 与 `(2,1)` 判等的风险，必须让它可见、可复核 |

`pass` 与 `pass_loose` 都计入主口径准确率；报告同时给出**严格口径**（只算 `pass`），
两者之差就是需要人工复核的条数。两者相等即说明没有这类歧义。

**行序怎么落到代码里**：`compare_resultsets()` 是纯函数，不自己解析 SQL——它只接一个
`order_matters=` 布尔量；由 `runner.py` 用 `metrics.gold_sql_has_order_by(gold_sql)`
判定后传进去。判定只看**顶层**的 `ORDER BY`：窗口函数的 `OVER (ORDER BY x)` 与
子查询里的 `ORDER BY` 都不算（前者不决定结果集顺序，后者外层仍无序）。
优先走 sqlglot 的 AST，未安装 sqlglot 时退到正则——**评测不允许因为解析器缺失就跑不出分**。
报告里 `order_sensitive` 是含 `ORDER BY` 用例的单独一档：排序写错只在这一档扣分，
混进全量算会被无序用例的通过数稀释掉。

`nl2sql.jsonl` 15 条里有 11 条含 `ORDER BY`。在本次修正之前，这些用例的排序正确性
**完全没有被检验**（`_multiset_eq` 无条件按多重集比较）——排序写反照样拿满分。

### RAG：检索命中率 / 忠实度 / 单题通过 分开算

- **检索命中率**：top-k 里是否命中标准证据块。判定用「文档 + 块锚点」的集合交集，
  不用文本相似度——相似度没有稳定的判定线，会让分数不可复现。
  锚点用**规范化章节标题**（去全角空格、统一为单空格），不用页码：
  页码会随解析器换版本而变，目录串是稳定的。
  检索结果只给文档不给锚点时，退化为「文档级命中」并在 `reason` 里标注，
  不让宽松判定伪装成严格判定。
- **答案忠实度**：`要点覆盖率 − 0.25 × 幻觉次数`，下限夹到 0。
  幻觉比漏答更严重——漏答只是不完整，编造是知识问答的致命伤。
  要点命中用关键词（`match_any`）而不是语义相似度，保证可复现；
  语义判定留给后续的 LLM 裁判，且 LLM 裁判必须先在有标注的子集上校准通过才能对外用。
- **单题通过（口径 3.3，每题 1 分）**：对应基础功能里的 **RAG 计数 10 分**。
  判据是三个子条件**同时**成立：`gold_points` 全中 + `forbidden_points` 不出现
  + 有合格引用。三者**不合成一个布尔**——`judge_case()` 返回的 `CaseVerdict`
  把三项各自列出来，报告里也各自成列，演示时要能当场回答「这题为什么不算对」。
  注意是「全中」不是「覆盖率高」：漏一个要点就不算这题通过。

#### 引用字段名是「双轨」，两侧不可混用

这是 2026-09-20 修掉的一处**静默判错**，改之前不影响任何已有分数，但一旦接入
A 的真实契约就会全线出错：

| 侧 | 来源 | 字段名 |
|---|---|---|
| 预测侧 | 契约 `DocRef`（`app/contracts.py`） | `kind` / `doc_id` / `section_path` / `snippet` |
| 用例侧 | `cases/rag.jsonl` 的 `gold_refs` | `doc`（完整文件名）/ `anchor`（章节串）/ `snippet` |
| 桥 | `data/corpus/manifest.json` 的 `id` | `KB-001`…`KB-015`，与 `raw/` 文件名前缀一一对应 |

两侧统一在 `rag_metrics.py` 内归一（`ref_doc_id()` / `ref_anchor()` / `normalize_doc_id()`），
判定前先按 `kind == "doc"` 过滤，免得契约里的 `SqlRef` 混进检索列表。

**历史缺陷（已修）**：旧实现两侧都按 `doc`/`anchor` 取值。于是
① 传契约形状 → 两个判定分支都 False → 一律「未命中」，**命中率归零且不报错**（漏判）；
② 只给 doc 不给 anchor → 退化为「文档级命中」，**分数虚高**（误判）。
之所以自测没发现：`runner.py` 的 oracle 直接拿 `gold_refs` 当检索结果，
而 gold 天然是 `doc`/`anchor` 形状——**自证式全绿**。
现在 oracle 回放的是**契约形状** refs，字段名一旦改错，oracle 命中率立刻掉下来。

#### 「引用合格」也是两件事，分开报

口径 L200 的原文是「必须带出处引用（**引用片段非空**、**且指向实际检索到的文档**）」。
`CitationResult` 把这两条做成两个独立属性 `snippet_nonempty` / `points_to_real_doc`，
合成结果只放在 `ok` 里供汇总用。

`snippet` 从哪来：`cases/rag.jsonl` 的 `gold_refs` 原本**没有片段正文**，于是这条判据
根本无法被验证、oracle 也必然判不过。现在每条的 `snippet` 由
`tools/enrich_gold_snippets.py` 从语料生成器 `data/corpus/tools/build_corpus.py` 的
`DOCS_ALL` 里**逐字取出**（标题行 + 该标题下的正文段落），一个字都不手打。
改语料后重跑 `python eval/tools/enrich_gold_snippets.py --write` 即可。

「指向真实文档」的判定基准：优先 `retrieved_doc_ids`（本题实际检索返回的文档集，最严），
其次 `known_doc_ids`（`manifest.json` 的文档编号集）。两者都没给时，
`citation_basis` 记 `none`，只查片段非空并留痕，**不假装通过也不假装失败**。

#### 召回列表与答案引用是两份，不要混用

2026-09-22 按 A / B 的意见拆开（B 独立发现了同一件事）。预测里现在是三个字段：

| 字段 | 装什么 | 服务哪些判定 |
|---|---|---|
| `answer` | 答案文本 | 要点覆盖、禁答实体（忠实度） |
| `retrieved` | **检索阶段**的全量命中（Top-K） | **检索命中率** |
| `answer_refs` | **答案实际引用**的那几条 | 引用校验、单题通过 |

**为什么必须分开**：B 的工具把「全量召回」挂在 `TraceStep.hit` 上，只有 `generate`
挂重排后的 top_k。从 trace 汇总出的引用因此是「召回了几条」而不是「答案引用了几条」，
用它算命中率会把「检索对了但模型没引用」也判成没命中 —— **系统性低估**。

兼容性：**两条选择链各自兜底，方向不能搞反**（2026-09-22 19:05 A 审查报出过一次反例）：

| 判定 | 取值链 | 缺省行为 |
|---|---|---|
| 检索命中率 | `retrieved` → `answer_refs` | 没有真召回时用答案引用**近似** |
| 引用校验 / 单题通过 | `answer_refs` → `retrieved` | 没有答案引用时退回召回列表 |

字段名适配：`retrieved`（本工具历史叫法）/ `answer_refs`（拆分后显式名）/ `refs`
（契约 `AskResponse.refs`，历史上两用，**现在按答案引用处理**）。三者都认，
旧预测文件不会因为缺字段被判 0 分。

⚠️ **命中率这条兜底不能删**。曾经改成一律取 `pred.retrieved`，后果是：
只写 `refs` 的旧格式预测 `retrieved` 为空 → `evaluate_retrieval` 判「检索结果为空」→
命中率**整题归零**，而同一份预测的引用校验照样通过 —— 一条预测里
命中率 0.0 / 单题通过 1/1 自相矛盾，非常隐蔽。A 在审查里揪出来的就是这个。

用答案引用兜底只是**近似**（答案引用 ⊆ 召回，只低估不高估），
因此会逐题记 `retrieval_approximated`，汇总进 `overall.retrieval_approximated`，
报告与终端都会警告「这一档分数不能当真实检索能力对外报」。
要拿真值就让预测方补 `retrieved`（检索阶段全量 Top-K）。

只有当 `retrieved` 与 `answer_refs` 确实不同源时，才会把前者作为更严的
`retrieved_doc_ids` 基准传下去；两者相同时不传（传了就是自己跟自己比）。

回归用例：`test_rag_metrics.py::test_prediction_ref_split`（两侧方向都测）、
`test_jsonl_predictor_old_format`（旧格式端到端，去掉兜底立刻红）。

### 档位换算（初赛评分表原文）

`≥90% → 10 分`，`≥80% → 8 分`，`≥70% → 6 分`，低于 70% → 0 分。
`runner.py` 每次同时输出三档是否达标，不是只给一个总数——
这是为了「用数据决定下周砍什么」，而不是继续加功能。

## 用例规模为什么是 21 而不是 10

原文档位要求「至少 10 个测试用例，每个正确得 1 分」。
10 条样本里错 1 条，准确率掉到 90%——正好卡在档位边界上，一阵风就跨档。首版扩到
15 条后，错 1 条是 93.3%，仍在 90% 档内；M6 再补 3 条扫描件数字题与 3 条表格行题，
共 21 条，使 OCR 与表格文本处理各有至少 4 条独立回归，避免只凭单题判断链路是否可用。

## 用例维护规范

1. **不用手写预期结果**。用例只给 `gold_sql`，预期结果由 `runner.py` 执行后得到。
   手抄数字出错时，你会去怀疑系统，而不是怀疑用例。
2. **`expected_columns` 必须与真实列数一致**，`local_check.py` 会核对。
3. **TOP-N 用例必须声明 `top_n` 与 `order_key_index`**，`local_check.py` 会自动检查
   边界是否并列。并列时结果不确定（模型给了另外几首并列项该不该判错无法回答），
   应当改成阈值过滤，或补足次级排序键。
4. **答案要点要拆细**。一条 `point` 对应一个可独立判定的事实，
   否则模型答对一半也拿满分。
5. **`forbidden_entities` 只能写「确实不该出现」的实体**。判据是**子串包含**
   （`normalise(e) in answer`），所以有两层坑：
   - 若某个实体在正确答案里本来就要出现，写进去就会把正确回答判成幻觉；
   - **中文里裸词必然被它的否定形式包含**：「不一致」含「一致」、
     「不是同一家公司」含「是同一家公司」。写裸词等于让标准答案自己判不过 ——
     oracle 回放的答案就是要点文本用「；」拼接的，必然触发幻觉判据。
     ⇒ 要禁「答成一致」，就写**带主语的完整断言**（如 `两份文件一致`），
     把否定副词「不」挤到断言之外；方向性错误另有 `answer_points[].match_any` 兜底，
     防护不会丢。
   `validate_cases.py` 会拦这类冲突；没拦住的话 `--predictor oracle` 也会因掉分报错。
6. **`gold_refs[].snippet` 不许手写**。它是「引用片段非空」这条判据的证据，
   手抄一段等于往标准答案里塞未经核对的内容。新增/改动用例后跑
   `python eval/tools/enrich_gold_snippets.py --write` 从语料生成器逐字取；
   干跑（不加 `--write`）会打印每一条取得的片段与命中方式，核对无误再写。
7. **多跳用例（`kind: "multi_hop"`）的 `gold_refs` 必须跨 ≥2 个「文档 + 章节」组合**。
   判据是 `(doc, anchor)` 去重后 ≥ 2，**不是**「必须跨文档」——同一份文档里不同章节的
   **跨条款**推理（如 `rag-mh-09`：第 5 章「应急响应分级」+ 第 2 章「组织机构」）是合法多跳。
   `validate_cases.py` 会拦「标了 multi_hop 却只指向同一处」的假多跳。
   `rag_multihop.jsonl` 是**专项难度题集**，20/20 全 hard 属刻意设计，
   不适用主集的「三档全覆盖」要求（豁免写在 `DIFFICULTY_SPREAD_SUITES`，有回归测试钉住）。
8. **改了 `metrics.py` / `rag_metrics.py` 就重跑两个单测**；改了 `validate_cases.py`
   就重跑 `test_validate_cases.py`。改了判定相关的用例结构，还要确认
   `--predictor oracle` 仍是全 100%——oracle 掉的每一分都指向判定器或用例，
   不指向 A/B 的系统。
9. 每改一条用例，跑一次 `validate_cases.py` + `local_check.py` + `--predictor oracle`。

## 已知未验证项

- [ ] `--predictor http` 未对真实接口跑过：`ToolResult` 的字段名以 A 的契约冻结版为准，
      当前兼容 `{sql, answer, retrieved}` 与 `{data: {...}}` 两种形状
- [ ] 忠实度目前只有确定性口径，LLM 裁判尚未接入与校准
- [x] ~~口径 2.3 的「**金额类字段 1 分钱以内算对**」未实现~~ —— **已闭合（PR #46 / #47，2026-10-05）**：
      `eval/metrics.py` 新增 `MONEY_ABS_TOL = Decimal("0.01")`。列被 `column_types`
      标注为 `money` 时走**绝对** `0.01` 容差、**不叠加**相对容差（相对容差在 1e5 量级
      会放过 10 元级误差，而口径要的是「1 分钱」这种绝对口径）。当前 nl2sql 有 **5 条**
      用例带 money 列标注。改 `metrics.py` 后须重跑 `eval/test_metrics.py`。
- [ ] 口径 2.4 的 **`unsupported` 机制**未实现：口径允许把排在后续里程碑的功能标记为
      `unsupported` 并从分母剔除（但输出必须单列几条、为什么，且对外报分不得剔除），
      `runner.py` 目前没有这个概念 —— M2 之前若有用例覆盖尚未实现的功能会用到。
- [ ] **PR #48 之后 RAG 报数须复跑，冻结基线前不要引用旧数**：`eval/cases/rag.jsonl`
      移出 5 条**题目未问**的 `answer_points`（rag-e-03 / rag-m-02 / rag-m-03 /
      rag-h-03 / rag-h-05），忠实度分母口径收窄；同时 `eval/rag_metrics.py` 的
      `evaluate_retrieval()` 改为「先全量找精确命中 `(doc,anchor)`、找不到才退文档级」
      的**两遍扫描**，报告新增 `exact_hits / doc_level_hits / exact_hit_rate` 的
      **严格/宽松并列**一行 —— 布尔 `hit` 与主分 `hit_rate` **口径不变**。
      RAG 套件的真实预测本就待跑，两件事一并复跑后再谈基线。
- [x] ~~真实 PostgreSQL 跑分未执行（本机无 Docker）~~ —— **已闭合（2026-09-18）**：
      容器起来后已跑 `--predictor oracle --dialect postgres`，
      NL2SQL 15/15、鲁棒性 11/11、RAG 命中率与忠实度均 100.00%，退出码 0。
      结论：判定链路在真实 PostgreSQL 上与在 SQLite 上一致，此前的 SQLite 自证不是平台假象。
- [x] ~~L2 真实基线（A/B 双链路接通后用 `jsonl` 模式跑）~~ —— **结构化两条已闭合（2026-10-02）**：
      DeepSeek-V4.1-Flash 真实链路（`jsonl` 通道）跑出 NL2SQL **15/15 = 100%**
      （`reports/run-deepseek-v4.1-flash-20261002T065619Z.md`）与
      鲁棒性退化输入 **15/16 = 93.75%**、澄清 0/2
      （`reports/run-deepseek-robust-20261002T070612Z.md`）。
      **尚未闭合的剩两块**：RAG 套件的真实预测（需向量依赖齐备的环境）与
      `--predictor http` 对真实接口的验证。

## 基线快照

L0 空基线与 L1 判定器自证的实测结果已冻结在
[`reports/BASELINE_v1.0.0.md`](reports/BASELINE_v1.0.0.md)。
重跑命令：`python eval/runner.py --suite all --predictor oracle --dialect sqlite`（须全 100%）
与 `python eval/runner.py --suite all --predictor dry-run`（须全 0%）。

**2026-09-18 追加**：在真实 PostgreSQL 上重跑同一组自证，结果与 SQLite 一致（全 100%）。
两份报告留在 `reports/`（`run-oracle-<时间戳>.md`），`dialect` 字段可区分是哪一次。
容器起来后应以真实 PostgreSQL 的结果为准对外报数。

**2026-10-02 追加**：第一批真实链路分数产出（DeepSeek-V4.1-Flash，`jsonl` 通道，postgres）——
NL2SQL 15/15 = 100%、鲁棒性退化输入 15/16 = 93.75%（澄清 0/2，对应中级任务 3 未实现），
报告见上方「已知未验证项」的闭合记录。是否冻结为正式基线（BASELINE v1.2）由 C 按基线流程定夺。

**2026-10-09 追加**：本文件同步两处口径变更 —— 口径 2.3（金额 `0.01` 绝对容差）已在
PR #46 / #47 落地，上一版把它记成「未实现」属过期描述，本次改正；PR #48 收窄了 RAG
必答点并给检索判定加了两遍扫描与严格/宽松并列统计（见上方「已知未验证项」）。
本文件是判定口径的冻结版本，**改口径必须同时改这里**。
