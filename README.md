# 知证 VeriMind（askdata-agent）

多模态数据环境下的**可解释精准问数/问答智能体**。

产品展示名称为**知证 VeriMind**。整体问答机制称为事实—证据核验架构（FEVA）；技术名称与当前实现范围见[品牌与技术命名说明](docs/品牌与技术命名_v0.1.md)。

项目总标志与功能图标采用统一的可复用矢量资产，供前端、架构图、项目报告和演示材料共同使用。源文件位于 `web/public/brand/`，设计与使用要求见[品牌图标设计规范](docs/品牌图标设计规范_v0.1.md)，图标清单可查看[图标总览](docs/brand/图标总览.html)。

用户用自然语言提问 → 系统自主判断这个答案该「算」还是该「查」→ 精准执行 → 给出**带完整溯源证据**的答案。

> **「算」还是「查」——两条链路的区别（新人先看这段）**
>
> | | 结构化链路（算） | 非结构化链路（查） |
> |---|---|---|
> | 数据长什么样 | **表格**：一张专辑、一个客户、一笔消费 | **文档**：PDF 里的一段文字 |
> | 怎么找答案 | SQL 精确匹配 + 聚合（求和/计数/排序） | 算相似度，取最像的几个片段 |
> | 典型问题 | 「销量最高的三张专辑」——答案不在任何一行里，要从几千行**统计出来** | 「报销标准是多少」——业务库里没这条数据，只能去文档里**翻出来** |
>
> ⚠️ **别被"两个库"误导**：两条链路的数据**可能住在同一个数据库实例里**——本项目就是如此，
> `chinook` 库里同时有业务表（`Album`/`Track`/`Customer`）和 B 建的 pgvector 向量表（`chunk_vectors`）。
> **「结构化 / 非结构化」说的是数据的形态，不是存放的位置。**
> 准确问法是：「这个答案是要**算**出来的，还是要**查**出来的？」

| 项 | 内容 |
|---|---|
| 赛事 | 第三届「中国电子杯」高校 ICT 产教融合创新大赛 |
| 赛道 | 网信产业应用赛道 |
| 赛题 | 赛题八（命题单位：中电云计算技术有限公司 / 中国电子云） |
| 节点 | 校内初赛提交截止 **2026-10-20**（负责人于 2026-10-10 更正） |

当前冲刺排期见 [初赛截止纠正与当前排期](docs/初赛截止纠正与当前排期_20261010.md)。旧资料中按 10-25 倒排的时间表不再执行；内部目标为 10-18 前完成交付包与演练、10-19 提交并核对回执，10-20 只处理截止日应急。具体截止时刻与提交通道另行核对，不默认按 24:00。

本机前后端统一从仓库根目录运行 `start.cmd`；新版三入口前端固定使用 `http://127.0.0.1:5184/`。启动器会核对页面版本和实际源码目录；端口被旧页面或其他工作目录占用时会提示处理，不会悄悄换端口。页面与启动规则的整合记录见[主线与前端版本统一记录](docs/architecture/主线与前端版本统一记录_v0.1_20261016.md)。

---

## 许可证

| 范围 | 许可证 | 文件 |
|---|---|---|
| 本仓库（`app/`、`eval/`、`tests/`、`infra/`、`scripts/`、`web/`、`docs/` 等） | **GNU AGPL-3.0-or-later** | [`LICENSE`](LICENSE)（34,523 B，FSF 官方原文，未改动）；`pyproject.toml` 的 `license` 字段 |
| `contrib/`（对上游 OpenSPG/KAG 的派生修改与可复用工具） | **Apache-2.0** | [`contrib/LICENSE`](contrib/LICENSE) + [`contrib/NOTICE`](contrib/NOTICE) |

**为什么整体用 AGPL-3.0**：直接依赖 `pymupdf==1.28.2` 是"AGPL-3.0 或 Artifex 商业许可"双许可，被以库形式链接进一个对外提供服务的 Web 应用；AGPL 第 13 条要求通过网络交互的用户能够获得完整对应源码。选择"整体同许可"而非"换成宽松许可的解析库"，是为了在提交期内把许可义务显式履行到位，而不是把它留成风险。

`contrib/` 用 Apache-2.0 是另一回事：它要向上游 Apache-2.0 的 KAG 提交补丁，同许可最容易合并。Apache-2.0 → AGPL-3.0 是单向兼容，两层不冲突。

第三方资源的来源、版本、许可证与义务清单见 `../AIC赛题材料/开源及第三方资源使用清单_v0.2_20261024.md`。

---

## 当前前端启动入口（2026-10-16）

在仓库根目录运行 `start.cmd`，统一访问 **http://127.0.0.1:5184/**；不要从 `agent_temp` 或旧临时工作目录启动产品页面。首页应有“问数、问答、跨源问题”三个入口。

端口已被旧页面或其他工作目录占用时，启动器会报错，不再自动复用或递增端口。本目录旧前端可通过 `powershell -ExecutionPolicy Bypass -File .\start.ps1 -Restart` 更新；该命令也会重启后端，正在进行的问答应先结束。前端依赖安装后，可在 `web` 目录运行 `npm run test:runtime` 核对页面版本和实际源码目录。前端与后端使用同一次主线提交；仅页面来源校验不能证明后端已更新。

---

## 一、系统结构

五层：**交互层 → 编排层 → 工具层 → 数据层 → 模型层**

```
交互层   对话页 / 溯源视图
   ↓ HTTP  POST /api/ask 或 POST /api/ask/stream（交互界面用阶段流；最终均为同一响应结构）
编排层   会话恢复 → 要素改写/澄清 → 意图路由 → 工具调度 → trace 埋点
   ↓ 统一工具签名   tool(input) → ToolResult{data, trace, refs}
工具层   NL2SQL：表字段定位 → 外键路径候选 → SQL 生成/只读校验/语义检查 ｜ RAG 工具
   ↓
数据层   业务数据表 + 文档切片向量表          ｜  文档原始语料  ｜  评测集
          ▲ 二者是同一 PostgreSQL 实例里的两类表（见上方「算」还是「查」）
模型层   国产 LLM API ｜ bge 本地小模型 ｜ RapidOCR（离线）
```

## 二、目录结构

```
.
├── app/                    # 后端
│   ├── contracts.py        # ★ 三个人共用契约（改动需全员同步，见 §6.7）
│   ├── orchestrator.py     # 编排层：路由 / 规划 / 调度 / 重试 / trace
│   ├── memory.py           # 多轮上下文
│   ├── clarifier.py        # 澄清交互
│   ├── main.py             # FastAPI 入口（/health、兼容问答与流式阶段问答、PDF 溯源文件）
│   ├── nl2sql/             # 结构化链路（算：查业务数据表 + SQL 聚合，见文首表格）
│   │   ├── normalizer.py   #   要素识别与改写
│   │   ├── linker.py       #   元数据/别名候选定位（当前关键词降级，向量未接入）
│   │   ├── join_planner.py #   按已声明外键生成候选路径
│   │   ├── generator.py    #   SQL 生成
│   │   ├── validator.py    #   sqlglot 校验 + 安全闸门
│   │   ├── semantic_validator.py # 关联条件与部分粒度风险检查
│   │   └── executor.py     #   执行与结果格式化
│   ├── rag/                # 非结构化链路（查：检索文档找段落，见文首表格；实际结构见下方注）
│   │   ├── tokenizer.py    #   分词（BM25 倒排用）
│   │   ├── schemas.py      #   RAG 内部数据结构（共享契约从 app.contracts 导入）
│   │   ├── ingest/         #   解析侧
│   │   │   ├── loader.py   #     PDF 解析，抽文本块（带主字号）
│   │   │   ├── toc.py      #     目录层级识别（支持内嵌目录 + 字号容错）
│   │   │   └── chunker.py  #     层级感知切片
│   │   ├── retrieval/      #   检索侧
│   │   │   ├── indexer.py  #     建索引
│   │   │   ├── pgvector_store.py  # 向量存取
│   │   │   └── retriever.py       # 混合检索 + 重排
│   │   └── resources/      #   停用词表等静态资源
│   ├── tools/              # 工具层（统一 ToolResult 签名）
│   │   ├── base.py         #   工具抽象（ToolSpec 名片 + run 入口）
│   │   ├── registry.py     #   工具注册表（按名 / 按能力查）
│   │   └── mock_*.py       #   各工具的可运行假实现（编排层不等真实现）
│   └── test_contracts.py   # 契约测试（23 条）
│   └── test_mock_pipeline.py  # mock 链路测试（9 条：路由 / 合并 / 去重 / 澄清 / HTTP）
├── web/                    # 前端：对话页 + 溯源视图（Vue 3）
├── eval/                   # 评测
│   ├── cases/              #   nl2sql.jsonl / rag.jsonl / robust_cases.jsonl
│   └── runner.py           #   一键跑三项指标
├── data/
│   └── corpus/             # 知识库文档语料（≥10 份，涵盖不同类型格式）+ 校验工具
├── infra/                  # docker-compose、DB 导入脚本、schema_meta.json
├── docs/                   # 设计报告、技术说明书
│   └── opinions/           # ★ 三人互提意见 / 报 bug 的收件箱（to-A / to-B / to-C）
├── release/                # 打包产物（提交用 ZIP）
├── contrib/                # 开源贡献：KAG 抽取层改进（不参与运行，见下方说明）
├── pyproject.toml          # 依赖声明（uv 管理）
├── uv.lock                 # 全量版本锁定
└── .gitignore
```

结构化主线补齐目前仍处于“代码已接入、待测试”阶段。澄清与短追问状态存于后端单进程内存；表字段定位当前使用元数据、中文描述和别名的关键词匹配，没有启用向量召回。跨源事实核验仍走已有独立业务计划，不会被 SQL 关联规划替代。实现范围和未完成项见 `docs/architecture/主线补齐实现交接_v0.1.md`；本段不代表任务 3/4/5 已通过赛题验收。

> **注**：`app/rag/` 的实际结构是 `ingest/`（解析）+ `retrieval/`（检索）两个子包，
> 上表已按磁盘实况更新。设计初稿里这些文件是平铺在 `rag/` 下的，09-20 更正。

### 2.1 `contrib/`：对开源项目 KAG 的改进（不参与运行）

`contrib/` 与 `app/`、`web/` 不是一类东西：**删掉它，系统照常启动**。
它单独存在的理由，是把「我们对开源项目做了什么」从报告里的叙述变成可下载、
可读、可复算的代码 —— 这也是 AIC 赛题方向三与「开放成果与复用价值」的落点。

内容（详见 `contrib/README.md`）：

| 文件 | 作用 | 需要 KAG |
| --- | --- | --- |
| `contrib/kag/kag_normalize.py` | 三元组结构归一、实体名归一、图谱质量指标 | 否 |
| `contrib/kag/test_kag_normalize.py` | 上述逻辑的离线自测（17 项断言） | 否 |
| `contrib/kag/knowie_parse.py` | 知识单元解析复刻 + `strict` 开关 + 裸控制字符诊断 | 否 |
| `contrib/kag/test_knowie_parse.py` | 上述逻辑的离线自测（29 项断言，设 `KAG_UTIL_PY` 后 37 项） | 否 |
| `contrib/kag/upstream_patch_knowie_parse.diff` | 可直接应用的上游补丁（5 行） | 否 |
| `contrib/kag/audit_retrieval.py` | 检索/溯源审计（doc/page/section 命中率、MRR） | 否 |
| `contrib/kag/kag_improve.py` | 接入 KAG 抽取器，注册为 `askdata_schema_free_extractor` | 是 |
| `contrib/kag/builder.example.yaml` | 建图配置示例 | 是 |

不需要装 KAG 就能验证的部分：

```bash
python contrib/kag/test_kag_normalize.py
python contrib/kag/test_knowie_parse.py
```

两个上游缺陷，性质不同：

| | R1（建图丢边） | R4（解析中断） |
| --- | --- | --- |
| 位置 | `schema_free_extractor.py:521` | `util.py:10/18/29/42/49` |
| 触发 | 默认建图链走**同步**路径，尾逗号把三元组包两层，下游按 `len(tri) != 3` **整批丢边** | 模型在 JSON 值里写裸换行，`json.loads` 默认 `strict=True` 直接拒绝，**整篇文档建图中断** |
| 证据 | 离线断言：5 条三元组 → 0 条边；归一后 → 5 条边 | 离线断言 + **真实上游函数**前后对照（需设 `KAG_UTIL_PY`） |
| 修复后 | 同批语料语义关系边占比 0.57% → 33.41%（152/455） | 同一输入 5 层重试全落空 → 一次通过 |
| 复现门槛 | 需装 KAG | 只需上游 `util.py` 一个文件 |

注意两件事：① 0.57% → 33.41% 这个口径只说明「边被赋上了关系类型」，
不说明关系语义正确；② 完整复现那组数字需要团队调优的提示词与 OpenSPG 环境，
未随仓库发布，`contrib/README.md` 第五节写清了边界。
R1 已提 Issue [#758](https://github.com/OpenSPG/KAG/issues/758)；R4 补丁已就绪，
Issue 正文见 `contrib/kag/UPSTREAM_ISSUE_R4.md`。

## 三、技术选型（不可动摇项见项目行为基准 §三「技术框架」）

后端 **FastAPI + pydantic v2**；编排层**自研**（不用 LangChain 全家桶），单智能体 + 工具调用；
业务库 **PostgreSQL（Docker）**；**sqlglot** 做 SQL 校验与方言解耦；**pgvector** 为主；
国产 LLM API 生成 + **bge** 本地小模型做向量与重排；**RapidOCR** 离线 OCR；monorepo + docker-compose。

## 四、四条原则（写代码前先读）

1. **契约先行，但不硬冻结** —— 09-19 已出 **v0.2 契约 + 契约测试（19 条全绿）**，并在 mock 链路上跑通一次完整问答；09-21 编排层闭环落地，契约测试扩到 **23 条**。接口变更仍先改契约、全员同步（流程见 §6.7）。
   > 「不硬冻结」的意思是：**允许改，但不允许一个人悄悄改**。当前字段已经稳定，改动要走 §6.7 的三步。
2. **可解释性用代码埋点 trace，禁止用 LLM 思维链充当** —— 评分要的是确定性事实记录。
3. **数据层预留 SQL 方言适配** —— 换达梦/人大金仓应当只是换一个 sqlglot dialect 参数。
4. **评测集第 1 周必须落地** —— 没有评测集，客观分不可验证。

## 五、协作规范

- **契约是唯一真理**：`contracts.py` 冻结后任何字段改动必须全员同步，不得各自改。
- **分支策略**：见下一节「六、分支与日常操作」。
- **评测驱动**：任何功能改动提交前跑一次 `eval/runner.py`，把三项指标（NL2SQL 执行准确率 / RAG 忠实度 / 检索命中率）贴进提交说明。**没有跑分数据的"改好了"不予采信。**
- **每周一晚跑分对账**：三项指标对比上周，据此决定本周削减什么，而不是继续加功能。
- **文档即代码**：设计/技术文档放 `docs/`，每份标注版本号与更新日期。
- **临时文件统一放 `./agent_temp`**，任务收尾清理，不留在仓库里（已在 `.gitignore` 中忽略）。
- **禁止把密钥、Token、账号密码写进代码或提交进仓库**：真实配置写 `.env`（不入库），仓库只保留 `.env.example`。

## 六、分支与日常操作

### 6.1 我们的分支

> ⚠️ **2026-09-20 改名**：分支名原来是 `feat/nl2sql` 这种带斜杠的写法，现已**全部改成中划线**。
> 原因是本机 AI 环境有个 bug——带斜杠的分支名会静默失败，严重时会把整个 `.git/refs/` 目录删掉（已出过一次事故）。
> **如果你本地还留着旧名字的分支，按下面这段同步：**
> ```bash
> git branch -m feat/nl2sql feat-nl2sql     # B 用 feat/rag → feat-rag；C 用 feat/eval → feat-eval
> rm -rf .git/refs/heads/feat               # 重要：清掉那个危险的斜杠目录
> git fetch origin
> git push --set-upstream origin feat-nl2sql
> ```

| 分支 | 负责人 | 负责范围 | 备份负责人 |
|---|---|---|---|
| `main` | —— | **唯一基线，谁都不直接推**。只在合并请求（PR）通过后由负责人更新 | —— |
| `feat-nl2sql` | 袁润熙 | `app/contracts.py`、`app/orchestrator.py`、`app/memory.py`、`app/clarifier.py`、`app/nl2sql/`、`app/api.py` | 待定 |
| `feat-rag` | 胡雨航 | `app/rag/`、`app/tools/`、`web/`（前端） | 待定 |
| `feat-eval` | 祖启源 | `infra/`、`eval/`、`data/`、`docs/`（统稿） | 待定 |

> 分支按**模块**命名，不按人名——分工是会变的，写人名的分支名会随分工失效。
> 改**自己范围之外**的文件属于跨模块改动，先在群里说一声再动。

### 6.2 每天怎么做（照着抄就行）

**核心一句话：开工前先对齐，收工前先推送。**

你的本地副本是**快照**，不是实时视图。哪怕只有你一个人用，只要你换过电脑、或在网页上点过合并，本地就已经落后了。所以下面这套顺序不要跳步。

#### 开工四步（每天第一次动手前跑一遍）

```bash
# ① 先站回 main，把本地基线更新到最新
git checkout main
git pull origin main
#    看到 "Already up to date." 或 "Fast-forward" 都正常
#    如果报 "Your local changes would be overwritten" → 先 git stash（见 §6.5）
#    做完这步，你本地才真正知道项目当前长什么样

# ①.5 看一眼有没有人给你写了东西（5 秒）
git log --oneline -3 -- docs/opinions/
#    有你不认识的提交 → 打开 docs/opinions/to-你.md 看一眼
#    三个收件箱谁都能读、谁都能写；状态标着「待处理」的就是还没人管的

# ② 再切到自己的分支（第一次 clone 之后直接 checkout，不需要 -b，分支已经在远端了）
git checkout feat-nl2sql                   # 袁润熙
git checkout feat-rag                      # 胡雨航
git checkout feat-eval                     # 祖启源

# ③ 把刚拉到的 main 合进自己的分支（关键：避免最后一天大冲突）
git merge main
#    预期：Fast-forward，或产生一个 merge commit，都正常
#    ⚠️ 如果出现 CONFLICT → 别硬改，按 §6.5 处理，或直接问负责人

# ④ 确认状态对了再动手
git status
#    预期：On branch feat-xxx，且没有异常文件
```

> **为什么不能省掉 ①、③？**
> 有人会想"我本地文件都全，不用拉"。但 main 上**随时可能出现你本地没有的文件**
> （实测 09-19：本地 main 落后远端 6 个提交、3 个文件，含 `docs/评测口径_v0.1.md`）；
> 09-20 又实测了一次：本地 main 落后 1 个提交（PR #9 的合并提交），差的就是 C 的整套 `infra/`。
> 少了 ①③ 你就是在**一个几天前的旧世界里写代码**，等开 PR 时才发现对不上。

#### 收工五步（改完就做，不要攒）

```bash
# ⑤ 提交推送（git add 写具体路径，不要写 git add .）
git add app/rag/loader.py
git commit -m "feat(rag): 加上 PDF 解析"
git push

# ⑥ 到 GitHub 开一个新的合并请求（PR）
#    推完后终端会打印链接，点开即是 PR 页面，填标题点创建
#    ⚠️ 必须「新开」一个 PR：已经合并过的旧 PR 不会自动重新打开，
#       你可以往同一分支再推，但 PR 一定要另外建一个
```

#### 两条对等的纪律

- **开工前先拉**（①③）—— 防自己落后，别在旧世界里干活。
- **改动尽早合**（⑤⑥）—— 防别人落后。你迟迟不合并，别人按旧接口写的东西就会返工。
  这也是 §6.7「契约怎么改」存在的原因：改契约不是一个人的事。

### 6.3 三条必须记住的纪律

1. **`git add` 永远写具体文件路径，不写 `git add .`**
   写 `.` 会把你**所有**改动无差别打包，包括你不想提交的东西（密钥、临时测试文件）。
   一旦提交，密钥就进了 git 历史，清理成本极高。
2. **`git commit` 之前先跑一次 `git status`**，确认第一行显示的是你自己的分支。
   看到 `On branch main` 就**停下来，先切分支**。
3. **改完就推、推完就开 PR，不要攒。**
   攒得越久，和别人的改动撞车越厉害。每天合并一次，冲突永远是小的。

### 6.4 两个必须知道的事实

- ⚠️ **`main` 分支没有强制保护。**
  GitHub 免费版私有仓库不支持分支保护规则，所以**技术上谁都能直接往 main 推**——拦住这件事的只有大家的自觉。
  **请不要这么做**；即使你推成功了，负责人也会需要回退它。
- ⚠️ **不要用 `git push --force`。** 它会覆盖远端历史，把别人已经推上去的东西抹掉。
  确实需要用的时候，先问负责人。

### 6.5 遇到问题怎么办

| 报错 / 现象 | 原因 | 怎么办 |
|---|---|---|
| `Your local changes would be overwritten by checkout` | 改了一半没提交就想切分支 | 先 `git add` + `git commit`，或 `git stash` 暂存起来 |
| `! [rejected] ... non-fast-forward` | 远端有你本地没有的提交 | 先 `git pull origin main` 合并，再推 |
| `CONFLICT (content): Merge conflict in xxx.py` | 你和别人改了同一处 | 打开文件找 `<<<<<<<` `=======` `>>>>>>>` 标记，手动决定留哪段，删掉标记，再 `git add` + `git commit` |
| 提交里混进了不想提交的文件 | 用了 `git add .` | 用 `git reset HEAD <文件名>` 把它移出暂存区，再重新 `git add` 具体路径 |

**卡住超过半小时就问负责人，不要自己硬试**——尤其是出现 `reset`、`force`、`rebase` 这些词的命令。

### 6.6 共享文件谁说了算

下面这些文件不止一个人会碰到。**改之前先看这张表**，不要自己直接动。

| 文件 / 区域 | 主负责人 | 改动规则 |
|---|---|---|
| `app/contracts.py` | 袁润熙 | 走 §6.7 变更流程，B、C 点头后才合 |
| `docs/评测口径_v0.1.md` | 祖启源 | **写评测代码前必读**；改动需 A、B 确认（见该文件 §六） |
| `infra/schema_meta.json` | 祖启源 | 表结构变了当天更新，不要攒 |
| `eval/cases/*.jsonl` | 祖启源 | 加用例可以，改判定口径需 A、B 一起定 |
| `eval/runner.py` | 祖启源 | 指标算法改动需 A、B 复核 |
| `.env.example` | 祖启源 | 新增变量在群里说一声，别人好同步本地 `.env` |
| `.gitignore` | 袁润熙 | **改前先在群里说**。忽略规则是「写错一条就静默生效」的重灾区：文件被挡了 `git add` 不会报错，只会默默什么都不加 |
| `.gitattributes` | 祖启源 | 行尾策略会影响所有人的工作区（LF/CRLF 转换），改动前先在群里说 |
| `pyproject.toml` / `uv.lock` | 胡雨航 | 依赖变更会影响所有人的环境；加/删包后在群里说一声，别人好同步 |
| `docs/opinions/to-*.md` | 收件人 | **谁都能追加（含收件人自己）**，但**不能改别人的条目**；状态由**动手改的那个人**更新（谁修复谁关单） |
| `README.md` | 袁润熙 | 任何人都能提 PR，不是负责人的专利 |

> 主负责人 = **最终拍板的人 + 出问题第一个被找的人**，不等于「只有他能改」。
> 每张表都建议再认领一个「备份负责人」：主负责人不在时有人能接。备份人选待 B、C 确认后补上。

### 6.7 契约怎么改

契约**可以**改，但流程必须对：

1. **谁都能提** —— 发现字段不够用，在群里贴三样东西：要改什么、为什么、改完的示例 JSON。
2. **三个人都点头** —— 只要有一个人没回复，就先不改（这玩意儿一动，三条链路同时受影响）。
3. **改完补三件** —— `contracts.py`、示例 JSON、契约测试，缺一样不算改完。
4. **版本号 +1，群里说一声改了什么。**

当前主线契约基线：**v0.4**。当前分支包含尚未验收的 **v0.5 候选**（统一事实记录与查询结果级定位）以及 **v0.6 候选**（用户可选的 KAG 深度检索请求字段）。两者均未作为已发布或已验收契约；分别见 `docs/契约设计_v0.5.md` 与 `docs/契约设计_v0.6.md`。v0.4 基线说明仍见 `docs/契约设计_v0.4.md`。
本次契约自检数量以运行输出为准，不引用过期的历史计数。
**跑法**：`uv run python app/test_contracts.py`，期望看到 `ALL PASSED`。

> 上一版这里写的是「v0.1，待跑通后冻结」——已经过时，09-20 更正。

### 6.8 写评测代码前先读口径

**给祖启源：动手写 `eval/` 下任何代码之前，先读 `docs/评测口径_v0.1.md`。**

这份文件定义了"一道题怎样算答对"——列顺序算不算、浮点差多少算对、空结果怎么判、RAG 忠实度谁来判。
它不是可选的参考材料：**40 分客观分全靠它算**，口径不定死，三个人跑出来的分数没法互相比较。

- 口径要改 → 走该文件 §六 的流程（三人确认 + 升版本号）
- 不要在 `runner.py` 里自己发明一套判定规则——**代码里写死的口径，必须和那份文件一致**

---

## 七、快速开始

> 一键启动脚本随底座建设同步补充（见 `infra/docker-compose.yml`）。
> 依赖统一用 **uv** 管理（不再是 pip + requirements.txt）：`pyproject.toml` 写直接依赖，`uv.lock` 锁全量版本，二者都已进仓库。

### 7.1 依赖与运行环境（uv）

```bash
# ① 安装 uv（只需一次；已装可跳过）
pip install uv
#   Windows PowerShell 也可用官方脚本：irm https://astral.sh/uv/install.ps1 | iex

# ② 按 uv.lock 建 .venv 并装好所有依赖（等价于以前的 pip install -r requirements.txt）
uv sync

# ③ 之后所有运行都走 uv run，不用手动 activate
#    ⚠️ 下面三条命令都要传文件路径；不传会去找 data/corpus/1.pdf（默认值，尚未与实际语料对齐）
uv run python -m app.rag.ingest.loader data/corpus/raw/KB-001.pdf   # 解析 PDF，抽文本块
uv run python -m app.rag.ingest.toc    data/corpus/raw/KB-001.pdf   # 识别章节层级
uv run python -m app.rag.ingest.chunker data/corpus/raw/KB-001.pdf  # 层级感知切片
uv run uvicorn app.main:app --reload   # 启 FastAPI（/health + /api/ask）

# ⑥ 自检（跑完打印 ALL PASSED）
uv run python app/test_contracts.py       # 契约测试
uv run python app/test_mock_pipeline.py   # mock 链路测试（路由 / trace 合并 / refs 去重）

# ⑦ 图谱后端开关（默认 mock，不用改代码）
#    GRAPH_BACKEND=mock  编排层用假图谱实现，离线可跑、不依赖 OpenSPG
#    GRAPH_BACKEND=kag   接真实图谱（等 3 天验证期出结论后再启用）

# ④ 加新依赖：自动更新 pyproject.toml 和 uv.lock，记得一起提交
uv add <包名>        # 例如：uv add sentence-transformers

# ⑤ 移除依赖
uv remove <包名>
```

> ⚠️ **本机 WorkBuddy 终端特例**：在本机 WorkBuddy 内置终端里跑 `uv sync` / `uv add` 偶尔会卡在构建临时目录清理（报 `SAFE_DELETE_FAIL_CLOSED` / 回收站不可用）。
> 这是本机 Python 安全删除钩子的限制，不是 uv 的问题。前缀加一行即可绕过：
> ```bash
> export UV_CACHE_DIR="$TEMP/uv_cache"        # PowerShell: $env:UV_CACHE_DIR="$env:TEMP\uv_cache"
> uv sync
> ```
> 普通 PowerShell / CMD / 本机其它终端不需要这步。

### 7.2 拉取与本地配置

```bash
git clone https://github.com/yuan201644-collab/askdata-agent.git
cd askdata-agent
cp .env.example .env        # 填入本地配置（该文件不入库）
```

### 7.3 环境变量与模型权重（RAG 链路）

`.env` 由 `app/rag/__init__.py` 统一触发加载——任何 `app.rag.*` 子模块被导入前就已完成，
所以直接运行 `python -m app.rag.retrieval.reranker` 这类脚本同样读得到配置
（曾经只有 `answer.py` / `llm.py` 顺手 import 了 config，直接运行其它模块会读不到，
进而回退成 HF 仓库名、静默开始下载上 GB 权重，表现成"运行到一半无提示退出"）。

三件最容易踩的事：

1. **不要留空赋值。** `BGE_RERANKER_MODEL=` 这种空值既不是"用默认"也不是"用本地"，
   而是一个会传给模型加载器的空字符串（报 `Repo id must be in the form ...`）；
   空的 `HF_ENDPOINT` 会让 httpx 抛 `UnsupportedProtocol`。
   `app/rag/config.py` 现在把 `.env` 里的空值按「未设置」处理，但不要依赖这条兜底。
2. **权重优先指向本地目录。** 写 HF 仓库名时，缓存缺失就会下载（bge-m3 约 2GB、重排约 2.2GB）：
   ```
   BGE_RERANKER_MODEL=D:/askdata-agent/models/bge-reranker-v2-m3
   BGE_EMBEDDING_MODEL=D:/askdata-agent/models/bge-m3
   ```
3. **直连 huggingface.co 不通的环境走镜像**：`HF_ENDPOINT=https://hf-mirror.com`。
   `HF_HUB_OFFLINE=1` 只在权重已完整缓存后才设——权重不在本机时设 1 会直接加载失败。

RAG 入口（`python -m app.ask_demo`）启动时会先打印**预检**：HF 变量当前值、向量后端是
memory 还是 pgvector、两个模型权重在哪、索引后语料/切片/向量数是否一致。
权重不可用会**直接报错并给出修法**，不会回落到下载；加载权重前也会打印一行进度，
让"正在加载"与"卡住/被杀"可以区分。单进程全链路内存峰值约 6GB，并发运行前先看可用内存。

---

## 八、这份 README 就是团队规范

**本文件是团队规则的唯一入口。** 分支怎么开、PR 怎么提、共享文件谁拍板、契约怎么改——上面第六节已经写完，不需要再去别处找规则。

- 更细的技术背景（评分项反推、技术框架约束、待决策项）→ `docs/功能清单与技术框架_v1.md`
- 评测怎么判分（写 `eval/` 前必读）→ `docs/评测口径_v0.1.md`
- 各自的分工与交付节点 → `docs/分工文档/` 下三份《工作任务》docx
- **给别人提意见 / 报 bug** → `docs/opinions/to-A.md`、`to-B.md`、`to-C.md`（三个收件箱，谁都能读、谁都能写）

> ⚠️ **分支名 2026-09-20 改过**（`feat/xxx` → `feat-xxx`），本地还留着旧名字的按 §6.1 顶部的说明同步。
>
> `AGENTS.md` 是负责人本机的 AI 行为基准，不随仓库分发，**队友不需要读它**——队友该遵守的都已写进本文件。
