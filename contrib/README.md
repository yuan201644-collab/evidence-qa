# 开源贡献说明：KAG 抽取层改进

本目录是**新增的开放成果**，不属于问答系统运行时的一部分。
删掉 `contrib/` 整个目录，`start-gov.cmd` 与 `start.cmd` 都能照常启动 ——
它存在的目的只有一个：把「我们对开源项目 KAG 做了什么改进」从报告里的叙述
变成别人能下载、能读、能跑的代码。

---

## 一、上游坐标（已逐行核实）

表 A —— R1/R2/R3 所在的抽取层（`kag/builder/component/extractor/`）：

| 项 | 值 |
| --- | --- |
| 项目 | OpenSPG / KAG |
| 版本 | 0.8.0 |
| 许可证 | Apache-2.0 |
| 缺陷文件 | `kag/builder/component/extractor/schema_free_extractor.py` |
| 缺陷位置 | 第 521 行（同步抽取路径 `_invoke`） |
| 对照位置 | 第 530–561 行（异步抽取路径 `_ainvoke`） |
| 下游受影响点 | 同文件第 344–346 行（`assemble_sub_graph_with_triples`） |
| 默认建图链 | `kag/builder/default_chain.py:77` `DefaultUnstructuredBuilderChain`，第 141 行 `node.invoke(item)` → 走**同步**路径 |

表 B —— R4 所在的结果解析层（`kag/builder/prompt/default/`）：

| 项 | 值 |
| --- | --- |
| 缺陷文件 | `kag/builder/prompt/default/util.py` |
| 缺陷函数 | `load_knowIE_data`（第 8–54 行） |
| 抛错位置 | 第 51–53 行 `raise ValueError("the output KnowUnit str is invalid: " ...)` |
| 调用点 | `kag/builder/prompt/default/knowledge_unit.py:250`（`parse_response` 内） |
| 全库事实 | 搜索 `strict=` 命中 **0** 处；搜索 `control char` / `0x1f` / `unescape_control` 均 **0** 处 |

> 核实方式：在未打补丁的 KAG 0.8.0 源码（`fdab15b`）上直接读文件定位行号，
> 不是根据日志或二手记录推断。表 B 的两处「0 处命中」是对整棵 `kag/` 源码树
> 做全文搜索的结果。
>
> 两个缺陷是**独立**的：R1 让边写不进图，R4 让整篇文档的建图直接中断。
> 修复也互不影响，可以分别提 PR。

---

## 二、五项改进

### R1 三元组结构修复（同步路径）

上游第 521 行：

```python
triples = (self.triples_extraction(passage, filtered_entities),)
```

多了一个尾逗号。`triples_extraction` 返回的本来是 `List[list]`，加尾逗号后
返回值变成**外层长度为 1 的元组，唯一元素才是三元组列表**。

这份数据被交给 `assemble_sub_graph_with_triples`，那里第 344–346 行是：

```python
for tri in triples:
    if tri is None or len(tri) != 3:
        continue
```

循环只转一圈，`tri` 拿到的是整个三元组列表而不是一条三元组；
`len(tri)` 是「三元组条数」，几乎不可能恰好等于 3，于是整批 `continue`
——**所有关系边被静默丢弃，建出的图里只剩实体节点，没有边。**

只有当某段文本恰好抽出 3 条三元组时，这 3 条会被误当成一条三元组的主谓宾，
产生 1 条语义错误的边。这解释了「坏图里仍能看到零星关系边」这个现象。

异步路径 `_ainvoke` 第 554 行用 `asyncio.gather(...)` 拿到的就是正常的
`List[list]`，**没有**这个缺陷。而默认建图链走的是同步 `_invoke`，
所以在默认路径上这个缺陷**必现**，与并发数、数据量都无关。

修复方式：把数据交给 `assemble_sub_graph` 之前，用 `unpack_triples()`
把两种布局都归一成 `list[list]`。修的是数据布局，不碰上游抽取算法。

### R2 实体名归一延后到连线时刻

上游比较实体名用的是 `processing_phrases`
（`kag/common/utils.py:196`）：

```python
re.sub("[^A-Za-z0-9\u4e00-\u9fa5 ]", " ", phrase.lower()).strip()
```

它保留内部空格。而同一实体的写法在「实体列表」与「三元组端点」里常常只差
空格或换行 —— PDF 排版把机构名、产品名折成两行是最常见的来源。
严格比较判定不等，实体被当成新节点二次插入，本应接上的边只能落到兜底类型。

修复方式：连线前用 `grounding_text()` 匹配，它在上游**同一字符类规则**之上
再去掉全部空白。匹配成功则把端点替换成实体列表里的规范写法；
未匹配则原样保留，行为与上游一致。

只放宽了空白，没有放宽别的字符 —— `test_kag_normalize.py` 里专门有断言
守住这条边界（标点、大小写仍按上游口径处理，不同实体不会被判为相同）。

### R3 图谱质量可观测量

建图后统计边类型分布，产出：

- `semantic_edge_ratio`：边类型不属于 `OTHER_TYPE` 等兜底值的边 / 全部边
- `other_edge_ratio`：兜底类型边占比

**口径边界（重要）**：它只衡量「边有没有被赋上关系类型」，
**不**衡量「关系语义对不对」。后者只能人工抽样核对。
报告里引用这个数字时必须带这句限定，否则会被读成「关系抽取准确率」。

### R4 知识单元解析不再被单个裸换行打断

这不是我们主动去找的缺陷，是**建库时被卡住才查出来的**。2026-10-22 在服务器上
用 KAG 0.8.0 的网页端建「非结构化 + 混合索引」知识库，导入一篇带 30 行项目
名单表格的立项公告，构建卡住不出图。日志里 7 分钟刷了 13 次：

```
kag/builder/prompt/default/knowledge_unit.py:250  parse_response
  kag/builder/prompt/default/util.py:51
    ValueError: the output KnowUnit str is invalid: ...
json.decoder.JSONDecodeError: Invalid control character at: line 80 column 10 (char 3640)
```

根因：模型在 JSON 字符串值里写了一个**裸换行符**，而 `json.loads` 默认
`strict=True`，遇到字符串内部的 0x00–0x1F 直接拒绝。

上游 `util.py:8-54` 写了 5 层 fallback（截大括号、重拼大括号、剥 markdown 围栏、
末尾补 `}`），逐层核对过：**没有一层处理字符串内的裸控制字符**。
整棵 `kag/` 源码树里 `strict=` 一次都没出现过。所以 5 层全部落空，最后在 :51 抛错。

修复只改一个参数：`json.loads(..., strict=False)`。Python 对该参数的定义就是
「允许字符串内部出现控制字符」，除此之外不放松任何语法 —— 缺括号、多余逗号、
非法转义照旧报错，`test_knowie_parse.py` 第 4 组专门守这条边界。

顺带记录同一函数里两处不影响主结论的问题（可单独提）：

- `:14-16` 的 `left_pos`/`right_pos` 在 `respond` 上计算，却用来切
  `extract_ret_str` —— 后者是 `:12` 经 `modify_knowledge_unit`（`:160-167`）
  改写、长度已变的字符串，长度不等时切片位置是错的；
- `:23` 的正则 `(?<="Content": ")(.*?)(?=",\n    "Knowledge Type")` 只匹配
  英文键，中文提示词下键名是「内容」「知识类型」，这条永不命中。

### R6 编号字面召回（补一条检索通道）

这一条不是「上游代码写错了」，而是**上游默认组合少了一路**，所以处理方式与
R1/R4 不同：不改上游、不提 Issue，而是补一个检索器。

病灶有两层，缺一层都不成立：

1. Neo4j 全文索引 `_default_text_index` 用 `standard-no-stop-words` 分析器，
   `NYS212001` 这类「字母+数字」连写串不产出可检索 token，
   `search_text` 按编号查恒为 0 条；
2. 默认混合检索只有向量 + 图谱两路，向量对只差一位的编号（`NYS212001` /
   `NYS212002`）几乎不可区分，图谱里编号又不是实体属性，链不上去。

`anchor_chunk_retriever` 的做法是加第三路：正则抽编号 → Chunk 标签上
`CONTAINS` → 按命中锚点数排序。**不重建索引、不改分析器**，因为改分析器
（例如换 `cjk`）会改变全库检索行为，风险与收益不成比例。

接入沿用 R1 的注册表方式：

```python
@RetrieverABC.register("anchor_chunk_retriever")
class AnchorChunkRetriever(RetrieverABC):
    ...
```

实测（同一问题「NYS212001 的项目名称、申请人、所在单位？」）：

| | 引用 | 答案 |
| --- | --- | --- |
| 补丁前 | 0 条 | 「未检索到编号为 NYS212001 的项目记录」 |
| 补丁后 | `ATT_65041_01` | 项目名称／张艺／传媒与艺术学院，三字段全对 |

检索器自己的日志是 `anchors=['NYS212001'] hits=1 returned=1`——全表只命中
那一块，没有误召回。

**边界（引用时必须一起说）**：

- 只对问句里带编号的问题有效；锚点为空时直接返回空，不产生额外开销；
- 只在混合检索路径生效，naive 路径的检索器是硬编码单路向量；
- 178 个 Chunk 全表 `CONTAINS` 是毫秒级，十万级语料必须换成 `cjk` 分析器
  或独立倒排，否则全表扫不可接受；
- 它提升的是「编号题能不能答」，不是整体答对率——17 题里依赖编号的只有
  一题，量级是 7/17 → 8/17，不能宣称「检索准确率整体提升」。

---

## 三、可执行证据

本目录里能离线跑的部分（不需要 KAG、不需要大模型、不需要图数据库）：

```bash
python test_kag_normalize.py      # R1/R2/R3 的纯逻辑，17 项断言
python test_knowie_parse.py       # R4 的解析健壮性，29 项断言
python test_anchor_query.py       # R6 的锚点抽取与排序，20 项断言
```

`test_kag_normalize.py` 最能说明问题的一条是：

| 场景 | 上游 `assemble_sub_graph_with_triples` 能接纳的边数 |
| --- | --- |
| 一段文本抽出 5 条三元组，按上游同步布局 `(list,)` 传入 | **0**（整批丢边） |
| 同一份数据，经 `unpack_triples()` 归一后传入 | **5** |
| 恰好 3 条，按上游布局传入 | **1**（错误的那一条） |
| 恰好 3 条，归一后传入 | **3** |

这不是模拟：`count_usable_edges()` 逐条复刻了上游第 344–346 行的过滤条件。

`test_knowie_parse.py` 的验证更进一步 —— 它**直接拿上游真实的 `util.py` 跑**。
设一个环境变量指向上游文件即可（不复制、不内嵌上游源码，只在内存里 `exec`）：

```bash
# Windows PowerShell
$env:KAG_UTIL_PY="<你的 KAG 源码>/kag/builder/prompt/default/util.py"
python test_knowie_parse.py
```

不设这个变量时第 6 组自动跳过，其余 29 项照常跑。设了之后会额外验证 8 项：

| 断言 | 结果 |
| --- | --- |
| 原版源码里没有 `strict=` 字样 | 通过（印证 5 层都没处理控制字符） |
| 真实 `load_knowIE_data` 对同一输入抛 `ValueError` | 通过 |
| 抛的正是 `the output KnowUnit str is invalid:` | 通过 |
| 在内存里给同一份源码打上补丁后能解析 | 通过 |
| 补丁后的结果与我们的复刻实现逐字一致 | 通过 |
| 正常 JSON 在补丁后结果不变（无副作用） | 通过 |

也就是说：**这个修复不是我们"以为"有效，是在上游原函数上跑出来的。**
（运行时 stderr 里会出现上游 logger 的 traceback，那是 5 层重试各自失败的现场，
正是缺陷证据本身，不是测试失败。）

---

## 四、团队在同批语料上的观测值

以下数字来自团队的实验记录，**本目录的示例配置无法直接复现**（原因见第五节）。
引用时应标注为「团队实验观测」，而非「本仓库可复现结果」。

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 语义关系边占比 | 0.57% | 33.41%（152/455） |
| 兜底类型（Others，NER 口径）占比 | 41.1% | 2.4% |

检索侧对照实验（同一批语料、同一组问题）：

| 实验 | 结果 | 说明 |
| --- | --- | --- |
| 二十题配对：标准 RAG | 9/20 | — |
| 二十题配对：深度提示、无 KAG | 7/20 | — |
| 二十题配对：加 KAG | 12/20 | 配对检验 p=0.375，**不显著** |
| 真实采购 8 题 × 3 次：项目 RAG | 9/24 | 单题组件耗时 12.41 s |
| 真实采购 8 题 × 3 次：向量补充 | 20/24 | 105.72 s |
| 真实采购 8 题 × 3 次：图谱混合 | 22/24 | 193.69 s |

需要同时说清的两件事：

1. 二十题那组 **p=0.375 不显著**，不能宣称「KAG 带来显著增益」。
   能站住的表述是「12/20 高于 9/20，方向为正，样本量不足以判定显著」。
2. 8 题那组的起点是 9/24，说明该组题的难度分布与二十题不同（底子更差），
   两组数字不能互相印证，只能各自陈述。
3. 耗时从 12.41 s 涨到 193.69 s（约 15.6 倍），这是**必须主动披露的成本**，
   否则「图谱混合更好」会被读成免费午餐。

---

## 五、可复现性边界（务必如实告知评审）

本目录能让别人复现**机制**，不能复现**那组数字**，原因是：

1. **提示词未发布。** 团队真实运行配置在抽取器下挂了三个自定义提示词类型
   （`strict_ner` / `strict_std` / `strict_triple`），它们按政府采购、
   招投标语料调过，属业务资产。`builder.example.yaml` 刻意省略这三项，
   走上游默认提示词，产出的图会明显弱于 33.41% 这个观测值。
2. **需要外部服务。** 完整跑通需要 OpenSPG 图服务（默认 `127.0.0.1:8887`）
   与两套模型密钥（抽取 LLM、向量模型 bge-m3）。示例配置里用环境变量
   `${KAG_LLM_API_KEY}` / `${KAG_EMBEDDING_*}` 占位，仓库中不含任何真实密钥。
3. **语料口径。** 152/455 是在团队自建的 15 份政府采购类文档上得到的，
   换语料后比例会变。这个数字的意义是「同一批语料、修复前后」的对比，
   不是 KAG 的通用指标。

因此，诚实的表述是：

> 我们定位并修复了 KAG 0.8.0 默认建图链上一个会静默丢弃全部关系边的缺陷，
> 修复后同批语料的语义关系边占比从 0.57% 升到 33.41%；
> 修复逻辑与离线自测已开源，完整复现需自备提示词与 OpenSPG 环境。

**R4 是例外：它完全可复现。** 上表三条限制（提示词、外部服务、语料口径）
对它都不成立 —— 它只需要上游 `util.py` 一个文件，任何装了 Python 的机器都能验，
而且验的是上游原函数而不是我们的复刻。这也是为什么报告里应该把 R4 和 R1
分开讲：R1 的强项是「影响大」（整个建图链路无关系边），
R4 的强项是「证据硬」（评审自己就能跑出前后对照）。

**R6 介于两者之间。** 它的纯逻辑（抽编号、排序）可以离线跑 20 项断言，
任何人验得了；但要复现「0 引用 → 正确引用」这一步，需要 KAG 环境 + 一个
带编号的语料（本仓库不含语料）。所以对外表述应分成两句：
「抽取与排序逻辑可离线验证」+「端到端效果在团队服务器上实测，语料不外发」。

---

## 六、与上游的协作状态

**当前状态：已向上游提交 Issue —— [OpenSPG/KAG#758](https://github.com/OpenSPG/KAG/issues/758)
（2026-10-22，状态 `open`）。PR 尚未提交。**

正文存档见 `kag/UPSTREAM_ISSUE.md`（英/中双版）。不掩盖「PR 还没提」这一点，
是因为「生态贡献」要经得起追问。

### 6.1 一个必须正视的历史：PR #583 报过同一个缺陷，被关闭且未合并

检索上游时发现 [PR #583](https://github.com/OpenSPG/KAG/pull/583)（作者 `Like0x`，
2025-05-19 提交）报的正是本题：描述原文「triples has been wrongly set as a tuple,
it should be `List[list]`」，并直接链接 `schema_free_extractor` 的函数位置。
该 PR 于 **2025-06-27 被关闭，`merged_at: null`（未合并）**；其当前 diff 只剩
`README.md` 徽章链接一行改动（`changed_files: 1`），抽取器那行修复**从未出现在可评审的 diff 里**。

从外部无法判断是「修复本身被否决」还是「fork 分叉导致 diff 丢失」。这件事改变了申报策略：

- 直接把同一处改动再提一次 PR，大概率得到同样结果；
- 所以顺序调整为 **先 Issue、再 PR**：Issue 里明说「#583 报过、被关了、我无法判断原因，
  请就这个 bug 本身给一个结论」，把讨论钉在缺陷事实而不是提案形式上；
- Issue 正文以「最小复现 + 行为对照表」为主，不依赖任何业务语料或团队私有组件，
  上游维护者无需信任我们的实验数字就能自行判断。

### 6.2 下一步动作

1. ~~提 Issue~~ —— **已完成**，[#758](https://github.com/OpenSPG/KAG/issues/758)：
   同步/异步路径行为不一致、默认建图链静默丢边，附 40 行无依赖复现脚本与预期/实际对照；
2. **提 PR**（进行中）：不等回应也可先按上游贡献规范提交，只改 R1
   （去尾逗号），不动提示词、不动业务逻辑，把改动压到最小可评审范围，
   并把 `test_kag_normalize.py` 里的复现改成 pytest 用例；
3. **R2 单独提 Issue 讨论** —— 它改变的是实体比对松紧度（`processing_phrases`
   会删除全部非字母数字汉字字符），属于口径选择，应由上游决定是否接受，
   而不是由下游默认改变行为。

R1 是明确的 bug，提 PR 阻力最小；R2 是策略取舍，先讨论再改。
这个区分本身就是「懂上游」的证据，答辩时可以直接讲。

### 6.3 R4 的状态：补丁已就绪，Issue 待提

R4 与 R1 性质不同，处置也不同：

| | R1 | R4 |
| --- | --- | --- |
| 性质 | 明确的 bug（数据布局错） | 明确的 bug（解析过严） |
| 是否改行为语义 | 否，只把错的数据摆正 | 是，放行了原先拒绝的输入 |
| 完成度 | Issue #758 已提，PR 待提 | 补丁 + 离线自测已就绪 |
| 复现难度 | 需装 KAG 才能跑真实链路 | **不需要 KAG**，真实上游函数可直接验 |
| 提议动作 | 直接提 PR | 先提 Issue 附补丁，请维护者定 strict 口径 |

R4 的补丁在 `kag/upstream_patch_knowie_parse.diff`，5 行改动，只加
`strict=False`，不动 fallback 结构与错误消息。上游若担心「放得太宽」，
可以只对 `:10` 那一层加，其余四层保留严格模式 —— 这一点在 Issue 里已经写明，
把选择权交给维护者。

---

## 七、许可证与合规

| 项 | 说明 |
| --- | --- |
| 本目录许可证 | Apache-2.0（见 `LICENSE`） |
| 上游许可证 | KAG / OpenSPG 为 Apache-2.0 |
| 兼容性 | 兼容。`kag_improve.py` 继承上游 `SchemaFreeExtractor`，属衍生作品，故沿用同一许可证 |
| 是否修改上游文件 | 否。新增文件 + 配置覆盖的方式接入，不打补丁到上游源码树 |
| 是否分发上游代码 | 否。本目录只含我们自己写的代码，运行时依赖用户自行安装上游 |

「不改上游源码、靠注册表覆盖」这个选择是有意的：升级 KAG 时不会产生
补丁冲突，也让 PR 更容易被接受。

---

## 八、目录文件清单

| 文件 | 作用 | 是否需要 KAG |
| --- | --- | --- |
| `README.md` | 本文件 | 否 |
| `LICENSE` | Apache-2.0 全文 | 否 |
| `kag_normalize.py` | R1/R2/R3 的纯逻辑：`unpack_triples` / `grounding_text` / `build_graph_metrics` | **否** |
| `test_kag_normalize.py` | 上述纯逻辑的离线自测，17 项断言 | **否** |
| `knowie_parse.py` | R4 的纯逻辑：复刻上游 5 层 fallback + `strict` 开关 + 裸控制字符诊断 | **否** |
| `test_knowie_parse.py` | R4 的离线自测，29 项断言；设 `KAG_UTIL_PY` 后额外 8 项直接跑真实上游函数 | **否** |
| `upstream_patch_knowie_parse.diff` | R4 的可直接应用补丁（5 行改动） | 否 |
| `audit_retrieval.py` | 检索/溯源审计：把引用与人工标注的标准出处比对，算 doc/page/section 命中率与 MRR | **否** |
| `kag_improve.py` | 把 R1/R2 挂到 KAG 抽取器上，注册为 `askdata_schema_free_extractor` | 是 |
| `builder.example.yaml` | 建图配置示例，标明与上游默认配置的关键差异 | 是 |
| `UPSTREAM_ISSUE.md` | R1 的上游 Issue 正文（英/中双版），含 40 行无依赖复现脚本 | 否 |
| `UPSTREAM_ISSUE_R4.md` | R4 的上游 Issue 正文（英/中双版）+ 建议补丁说明 | 否 |
| `anchor_query.py` | R6 的纯逻辑：编号抽取 `extract_anchors` / 命中排序 `rank_hits` | **否** |
| `test_anchor_query.py` | R6 的离线自测，20 项断言 | **否** |
| `anchor_chunk_retriever.py` | R6 的检索器：注册 `anchor_chunk_retriever`，Cypher `CONTAINS` 扫 Chunk | 是（需 Neo4j 直连） |

一次完整的最小使用流程：

```bash
# 1. 不需要 KAG 就能验证全部修复逻辑
python test_kag_normalize.py
python test_knowie_parse.py

# 2. 想验证 R4 确实修好了上游原函数（可选）
export KAG_UTIL_PY=/path/to/kag/builder/prompt/default/util.py
python test_knowie_parse.py

# 3. 装上游后接入（示例）
pip install openspg-kag==0.8.0
export PYTHONPATH="$PWD:$PYTHONPATH"
python -c "import kag_improve"   # 触发注册

# 4. 用 builder.example.yaml 建图（需自备 OpenSPG 与模型密钥）

# 5. 建图后算质量指标
python -c "
from kag_normalize import build_graph_metrics
import json
edges = [(e['s'], e['type'], e['o']) for e in json.load(open('edges.json', encoding='utf-8'))]
print(json.dumps(build_graph_metrics(edges), ensure_ascii=False, indent=2))
"
```

---

## 九、在材料里怎么引用本目录

- 技术报告「三、AI 技术与开源及第三方资源选择」→ 说明为什么选 KAG、
  为什么用注册表覆盖而不是改源码；
- 技术报告「四、设计与实现过程」→ R1 的定位过程（同步/异步路径对比）
  是很好的「关键实现」素材，比贴架构图有说服力；R6 的「先证明索引不索引
  编号，再补一条字面通道」是第二个可讲的定位过程；
- 技术报告「六、创新性与开放价值」→ 用第三节的可执行证据 +
  第六节的上游协作计划，避免只写「我们做了优化」；
- 答辩时若被问「这算不算实质改进」，直接打开 `test_kag_normalize.py`
  跑一遍，用「修复前 0 条边 / 修复后 5 条边」回答问题。

同时**必须**在《开源及第三方资源使用清单》里新增一行：
资源名称 KAG（OpenSPG），版本 0.8.0，许可证 Apache-2.0，
使用方式「作为知识图谱抽取与检索组件被修改后使用」，
自主开发内容「抽取器同步路径三元组结构修复、实体名归一策略、
图谱质量指标脚本」，合规状态「兼容」。
