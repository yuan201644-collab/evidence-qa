# infra —— 数据底座与元数据导出

责任人：C。这是全队唯一强耦合点中的第 4 条接口（`schema_meta.json`）的产地。

## 实测状态（2026-09-18，Docker Desktop for Windows）

本目录的**每一项都已在真实环境实跑过**，不再是「经人工审校」：

| 项 | 状态 | 实测证据 |
|---|---|---|
| `docker-compose.yml` | 已实跑 | 容器 Up (healthy)，PostgreSQL 16.15，pgvector 0.8.6，`track` 3503 行 |
| `docker-compose.aw.yml` | 已实跑 | 容器 Up (healthy)，PostgreSQL 16.9，80 个对象（71 表 + 9 视图），`Person.Person` 19972 行 |
| `export_schema_meta.py` | 已连库跑过 | chinook 11 表 / 64 字段 / 11 外键；adventureworks 6 schema / 80 表 / 607 字段 / 90 外键 |
| `gen_schema_meta_from_ddl.py` | 已实跑 | 无数据库环境下产出与连库口径逐字段一致（仅多一个 `schema` 键的差异） |
| `verify_infra.py` | 已实跑 | 21 项检查全 PASS，退出码 0（含正负例双向验证） |
| 契约 `schema_meta_contract.py` | 已升级 v1.1.1 | 新增 `schema` 字段；补 `row_count >= 0` 约束 |
| 离线交付包 | 已做并实测 | `release/ict-db-images-v1.0.0.tar.gz`（375MB），`docker load` 19 秒恢复两个 tag |

两个镜像在 compose 里都按 **tag + digest 双写**方式引用（`image: repo:tag@sha256:...`）：
tag 给人读，digest 保证拿到的永远是同一份内容。实测固定后重建容器仍能正常启动
（8 秒到 healthy），且不触发联网拉取。

镜像摘要：
- `pgvector/pgvector@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b`
- `chriseaton/adventureworks@sha256:07e46e828d40cb2ce728025662aae68c289a811244fd9297d189a7c42c3598f5`

本机实测环境：Docker Hub 直连**不可用**（`registry-1.docker.io` 连接超时），
两个镜像都是经加速地址拉取后打回原 tag 的；离线包可直接绕过这一限制。

## 目录

```
infra/
├─ docker-compose.yml              PostgreSQL 16 + pgvector（业务库，项目名 ict）
├─ docker-compose.aw.yml           AdventureWorks 加压库（项目名 ict-aw，独立记账）
├─ initdb/                         容器首次启动时按文件名字典序执行
│  ├─ 01_extensions.sql            在默认库启用 pgvector
│  ├─ 02_chinook.sql               Chinook 官方 DDL + 全量数据（600KB，勿手改，见下）
│  └─ 03_chinook_extensions.sql    切到 chinook 库再启用 pgvector
├─ healthcheck/                    数据就绪探针（healthcheck 用 -f 执行，见「陷阱一」）
│  ├─ chinook.sql                  断言 track = 3503
│  └─ adventureworks.sql           断言对象数 = 80
├─ scripts/
│  ├─ schema_meta_contract.py      schema_meta.json 的 pydantic v2 契约（v1.1.1）
│  ├─ gen_schema_meta_from_ddl.py  静态解析生成（不需要数据库）
│  ├─ export_schema_meta.py        连库导出（权威口径）
│  ├─ verify_infra.py              数据底座一键验收（21 项）
│  └─ fetch_chinook.sh             重新抓取官方建库脚本
├─ schema_meta.json                产物：交给 A 做 Schema Linking（Chinook）
├─ schema_meta.adventureworks.json 产物：压力库元数据（6 schema / 80 表 / 607 字段）
└─ schema_meta.descriptions.zh.json  人工维护的中文业务说明
```

## 起容器

```bash
cp .env.example .env                                                  # 先看一遍 POSTGRES_PASSWORD
docker compose --env-file .env -f infra/docker-compose.yml up -d      # 业务库（回环 127.0.0.1:5432）
docker compose --env-file .env -f infra/docker-compose.aw.yml up -d   # 压力库（可选，回环 127.0.0.1:5433）
docker compose --env-file .env -f infra/docker-compose.yml ps         # db 应为 healthy
```

### `--env-file .env` 不能省（2026-10-03 实测）

compose 只在**项目目录**里找 `.env`，而项目目录默认取「第一个 `-f` 文件所在目录」——
也就是 `infra/`。但本项目的 `.env` 按约定放在**仓库根**（`app/nl2sql/config.py`
与 `export_schema_meta.py` 读的都是根目录那份）。省掉 `--env-file` 会在 `up` **之前**就报：

```
error while interpolating services.db.environment.POSTGRES_PASSWORD:
required variable POSTGRES_PASSWORD is missing a value: POSTGRES_PASSWORD 未设置，请先 cp .env.example .env
```

⚠️ **修法只能用 `--env-file .env`，不要用 `--project-directory .`** —— 后者会把 compose 里
**所有相对路径的基准**从 `infra/` 改成仓库根，于是两个只读挂载 `./initdb`、`./healthcheck`
全部指向不存在的目录。Docker 不会报错，而是**静默建出空目录挂进去**，症状极具迷惑性：

```
容器 Up、Healthy 检查却是 unhealthy
psql: error: /opt/healthcheck/chinook.sql: No such file or directory
```

此时数据库本身是好的（数据卷与 `initdb` 都正常），只有健康检查挂掉。若想确认挂载基准，
看容器的 `com.docker.compose.project.working_dir` 标签即可，它应指向本仓库的 `infra/`：

```bash
docker inspect ict-db --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
```

两个库用**各自独立的 compose 项目名**（`ict` / `ict-aw`）。这不是洁癖：
同名项目下 `up --remove-orphans` 会把另一个库直接删掉，那是在演示前丢环境。

**冷启动实测**（删掉数据卷从零重建，等价于评委机器上的首次运行）：
两条 `up` 命令发出后 **11 秒**两库同时到 healthy——其中包含 Chinook 全量数据的灌库。
`start_period` 设 60s 对慢机器留了充分余量。

`healthy` 在本项目里的含义**比通常更强**——它同时要求引擎可连与数据就绪，
理由见下一节。

## 一键验收（演示前必跑）

```bash
python infra/scripts/verify_infra.py --require-aw
```

输出是逐项 `PASS / FAIL / SKIP` 加期望值与实测值对照，退出码 0/1 可直接接 CI。
`--require-aw` 把压力库从「可选」升级为「硬检查」，演示当天用这个。
它比 `docker compose ps` 多做三件 `ps` 做不到的事：

1. 逐表比对 `schema_meta.json` 的行数与真库——元数据与库脱节是静默故障，
   不会报错，只会让 A 的 Schema Linking 基于错误信息工作；
2. 确认 pgvector 扩展真的装了、版本是多少；
3. 确认压力库连的是**有数据的那个库**，而不是空壳库（见陷阱二）。

## 三个实测出来的陷阱（必读，都是踩过的）

### 陷阱一：`docker-entrypoint-initdb.d` 只跑一次，`pg_isready` 不查数据

`initdb/` 下的脚本**只在数据卷为空时执行一次**。卷里一旦已有数据（例如上次跑过、
或数据卷是从别处拷来的），建库脚本会被整体跳过——此时容器照样报 healthy，
而 `track` 表是空的。

根因：`pg_isready` 只证明「引擎在监听端口」，它不检查目标库是否存在，
也不检查表里有没有数据。

应对：`healthcheck` 升级为双条件——`pg_isready` **且** 探针脚本返回 1。
探针 SQL 放在 `healthcheck/*.sql`，用 `psql -f` 执行而不是 `-c` 传字符串：
SQL 里有引号，塞进 compose 的 shell 命令要经过 YAML → compose 变量插值 → `/bin/sh`
三层转义，既容易写错也无法评审。放进文件后，断言逻辑可读、可 diff、可单独执行。

已做双向验证，不是只测了通过路径：

| 场景 | 主库探针 | 压力库探针 |
|---|---|---|
| 正常（有数据） | 输出 `1` | 输出 `1` |
| 库是空壳（对象数 0） | psql 退出码 3，不输出 1 | 输出 `0` |
| 库不存在（无 track 表） | psql 退出码 3，不输出 1 | psql 退出码 3 |

三种异常都不会被误判为就绪。`restart: unless-stopped` 不会因 unhealthy 重启容器，
所以把数据断言放进健康检查不会造成反复重启。

### 陷阱二：AdventureWorks 镜像自带的库名是 `postgres`，不是 `adventureworks`

镜像把数据烘焙在 PGDATA 里，而 compose 的 `POSTGRES_DB` 只决定
「entrypoint 额外创建哪个库」。若写成 `adventureworks`，entrypoint 会新建一个同名空库：

```
db=postgres        -> 80 个对象   ← 数据在这里
db=adventureworks  ->  0 个对象   ← 空壳，但 pg_isready 照样通过
```

于是出现最难查的一类故障：**连上了，但一张表都没有**。
本目录的 compose 与 `.env` 都已改成 `postgres`，健康检查也断言对象数 80。
（本机数据卷里由旧配置产生的空壳库已清理。）

### 陷阱三：AdventureWorks 的 schema 名保留大小写，必须加双引号

`Person` / `Sales` / `Production` / `Purchasing` / `HumanResources` 是建库时带双引号创建的。
裸写 `select * from person.person` 会被 PostgreSQL 折叠成小写，报
`relation "person.person" does not exist`。正确写法是 `"Person"."Person"`。

这就是契约 v1.1.0 加 `schema` 字段的原因：没有它，多 schema 库既无法表达，
A 生成的 SQL 也必然跑不通。取限定名请用 `TableMeta.sql_name()`，
它按 PostgreSQL 规则决定是否加引号，不要自己拼 `f"{schema}.{table}"`。

## 两套 schema_meta 生成器为什么都保留

`schema_meta.json` 是 C 在 **09-19** 交给 A 的交付物。当初容器能否在 09-19 前跑起来
不受 C 控制（本机没有 Docker），所以拆成两条路径：

| 路径 | 何时用 | 可信度 |
|---|---|---|
| `gen_schema_meta_from_ddl.py` | 容器还没有，A 又已经要开工 | 表结构/外键/行数/样例值可靠；无数据库侧 COMMENT |
| `export_schema_meta.py` | 容器起来之后 | 权威。以它覆盖前者的输出 |

容器现已跑通，两条路径都回归验证过：**产出逐字段一致**，差异只有新增的 `schema` 键。
静态路径仍保留——它是「评委机器只有 Python、没有 Docker」时的兜底，不是历史遗留。

实跑顺序：

```bash
python infra/scripts/gen_schema_meta_from_ddl.py                    # 1) 无数据库也能先给 A
python infra/scripts/export_schema_meta.py                          # 2) 连库重导，权威口径
python infra/scripts/verify_infra.py --require-aw                   # 3) 验收：元数据是否等于真库
```

导出压力库元数据（一条命令，`--all-schemas` 自动扫全部业务 schema，免得手写漏掉）：

```bash
python infra/scripts/export_schema_meta.py \
  --dsn "postgresql://postgres:postgres@127.0.0.1:5433/postgres" \
  --database adventureworks --all-schemas --samples 5 \
  --descriptions none --out infra/schema_meta.adventureworks.json
```

脚本会自己读 `.env`（仓库根 → `infra/`，已存在的环境变量优先），
所以 README 里 `cp .env.example .env` 之后可以直接跑，不需要先 `export`。

## schema_meta.json 的契约

契约定义在 `scripts/schema_meta_contract.py`（pydantic v2），变更记录在
`docs/CHANGELOG_schema_meta.md`。**改字段 = 改契约 = 必须全员同步**（协作规范第一条）。

当前版本 **1.1.1**：`TableMeta` 与 `ForeignKeyRef` 各带 `schema` 字段，默认 `public`。
默认值的存在使得 v1.0.0 的旧产物读进来仍然合法——升级不破坏存量。

### 两套版本号不是一回事（容易混，先看这里）

| 编号 | 写在哪 | 什么时候升 |
|---|---|---|
| 文档版本 `v0.1` / `v0.2` | `docs/契约设计_v0.1.md` 的「版本记录」表 | 设计文档的措辞、章节、字段命名讨论有改动时 |
| 契约版本 `1.1.1` | `scripts/schema_meta_contract.py` 的 `CONTRACT_VERSION` | **只有字段增删改**才升，下游按它判断兼容性 |

两者互相独立、不换算。`契约设计_v0.1.md` 管的是 `app/contracts.py`
（代码契约：Ref / TraceStep / ToolResult / AskRequest / AskResponse），
本目录的 `CONTRACT_VERSION` 管的是 `schema_meta.json`（元数据契约）——是两个不同的接口。
每个版本的变更内容与原因，见 `docs/CHANGELOG_schema_meta.md`。

## 02_chinook.sql 的纪律

该文件由官方工具生成，文件头自带 WARNING「此文件由工具生成，改动会丢失」。
因此：**只允许用 `scripts/fetch_chinook.sh` 整份覆盖，不允许手工编辑**。
覆盖之后必须重跑 `gen_schema_meta_from_ddl.py`，否则 schema_meta.json 与脚本会脱节。

## 中文说明为什么单独一个文件

`schema_meta.json` 是可重新生成的技术元数据；中文说明是人工资产。
混在一起意味着每重跑一次生成器就要重写 64 条说明。
所以说明落在 `schema_meta.descriptions.zh.json`，生成器负责合并，并打印覆盖率——
覆盖率不是 100% 时会列出缺失项，这比「悄悄用空字符串」强。

取值时先试限定名（`Sales.SalesOrderHeader.TotalDue`）再退回裸表名（`album.title`）。
退回是为了让 v1.0.0 时代写的 Chinook 说明一个字都不用改；多 schema 说明则用限定名，
避免同名表撞车。

## sample_values 会带出敏感列的真实取值（交付前必看）

`ColumnMeta.sample_values` 是导出时按列采集的若干个去重取值（`--samples 5`），
供下游做要素识别与别名挖掘。它是**无差别采集**的：不区分列名，只看有没有值。

两份产物的实际情况（实测统计）：

| 产物 | 带 `sample_values` 的列 | 其中敏感列 |
|---|---|---|
| `schema_meta.json`（Chinook） | 64 列（全部） | 无敏感列，但含 email / phone / address 一类演示数据 |
| `schema_meta.adventureworks.json` | 574 列 | `Person.Password` 的 `PasswordHash`、`PasswordSalt`，是 5 组真实取值 |

**风险判断**：AdventureWorks 是微软官方示例库，那两个字段是它自带的演示哈希、公开可查，
不是真实用户凭据，本轮交付不构成风险项。**但机制本身是隐患** ——
换成任何真实业务库，`sample_values` 就会把该库的敏感列取值原样写进元数据文件。

**本轮的决定**：不改生成逻辑。重跑导出会让 `generated_at` 变化、整个 1.2MB 文件 diff 全变，
为这点收益不值。只在文档里登记清楚。

**接真实业务库之前必须做的事**（交付前检查项，不是可选项）：
在 `export_schema_meta.py` 的采集逻辑里加敏感列过滤，规则建议为 ——
列名（不区分大小写）匹配 `password | passwd | salt | secret | token | ssn | credit | card | iban`
时跳过 `sample_values` 采集，只保留类型与可空性。

## 镜像拉不动的备选通道

Docker Hub 直连受限时，实测可用的加速地址（拉完重新打回原 tag，compose 不用改）：

```bash
docker pull docker.m.daocloud.io/pgvector/pgvector:pg16
docker tag  docker.m.daocloud.io/pgvector/pgvector:pg16 pgvector/pgvector:pg16

docker pull docker.1panel.live/chriseaton/adventureworks:postgres
docker tag  docker.1panel.live/chriseaton/adventureworks:postgres chriseaton/adventureworks:postgres
```

两个加速地址都是第三方公共服务，某天失效属正常。

## 离线交付包（已做并实测）

`release/ict-db-images-v1.0.0.tar.gz` —— 两个镜像一个包，**375MB**，已实测可载入：

```bash
# 目标机（无需外网）
docker load -i release/ict-db-images-v1.0.0.tar.gz
docker compose -f infra/docker-compose.yml up -d
```

实测数据：`docker save` 20 秒；`docker load` 19 秒，恢复出
`pgvector/pgvector:pg16` 与 `chriseaton/adventureworks:postgres` 两个 tag。

体积说明：两个镜像的 `docker images` 显示 621MB + 1.02GB，但 `docker save` 出的包只有
383MB（压缩后 375MB）——因为 `docker images` 报的是**解压后**体积，而 tar 里装的是
已经压缩过的层 blob。按 `docker images` 的数字估离线包大小会高估约 4 倍。

注意 `release/*` 在 `.gitignore` 里（交付压缩包不入库），所以这个包是**本地物理交接**的产物，
需要随提交物一起拷贝，不会出现在仓库里。

## 尚未做的事（诚实登记，别当已完成）

- [ ] **`schema_meta.adventureworks.json` 无中文说明**：607 个字段没有业务含义描述，
      导出时用了 `--descriptions none`。加压项要真正跑起来，需要补一份说明文件。
- [ ] **中文说明没有同义词/别名表**：`schema_meta.descriptions.zh.json` 只有业务含义，
      没有别名词条。若下游表级召回需要别名特征，要扩这个文件（形态待与 A 定）。
- [ ] **离线交付包走哪个渠道给评委未定**：包已做好（见上节），但它 375MB，
      不进仓库、也不适合直接塞进 git。走「随交付物提交 / 单独介质 / 依赖加速地址」
      哪条路，取决于初赛提交通道的附件大小限制，需负责人确认后写进交付说明。

### 已闭环（保留记录，避免下次重复登记）

- [x] **离线交付包**：已做并实测，见「离线交付包（已做并实测）」一节。
      本节旧版曾写「未做」，与本节第 17 行的能力表自相矛盾，属文档未同步，已更正。
- [x] **压力库镜像的 tag 已固定到 digest**：`docker-compose.aw.yml` 里已是
      `chriseaton/adventureworks:postgres@sha256:07e46e82…`，与 `pgvector` 那侧写法一致。
- [x] **契约 v1.1.1 已知会 A**：2026-09-19 的评审意见中 A 就 1.1.1 的编号体系
      提出了澄清建议，说明该版本已同步到 A 侧。
- [x] **「68 张表」的说法来自本目录旧版注释，属我方笔误，已删**：
      实测为 71 张基表 + 9 个视图 = 80 个对象。
