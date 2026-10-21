# 冻结基线 v1.1.0 —— 真实 PostgreSQL 上重跑

- 冻结日期：2026-09-18
- 责任人：C
- 前序基线：[`BASELINE_v1.0.0.md`](BASELINE_v1.0.0.md)（2026-09-17，在本地 SQLite 上完成）
- 本版差异：**执行环境从 SQLite 换成真实 PostgreSQL 16.15**，其余口径与用例完全未动

---

## 为什么要有这一版

v1.0.0 基线的两处自证是在本地 SQLite 上完成的（当时开发机没有 Docker）。
SQLite 与 PostgreSQL 在类型转换、除零、字符串比较、`LIMIT` 边界上的行为并不完全一致，
所以「SQLite 上全对」并不能推出「PostgreSQL 上全对」——这是平台假象，不是结论。

本次容器起来后，用同一套用例、同一套判定口径在真实 PostgreSQL 上重跑，结果是**一致的**。
这一版把该结论固化成可入库的证据（`eval/reports/*` 里只有 `BASELINE_*.md` 会被提交，
带时间戳的原始报告不入库，所以需要这份摘要）。

## 环境

```
主库       ict-db    pgvector/pgvector:pg16    PostgreSQL 16.15    pgvector 0.8.6
连接        127.0.0.1:5432/chinook（回环绑定）
数据        Chinook 11 表 / 64 字段 / 11 外键，track 3503 行
镜像摘要    pgvector@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b
```

## L0 空基线（`--predictor dry-run`）

```
python eval/runner.py --suite all --predictor dry-run --dialect postgres

NL2SQL：0/15 = 0.00%   （报错 15，仅列序不同 0）
鲁棒性：0/11 = 0.00%   （退化输入 0.00% / 澄清 0.00%）
RAG：命中率 0.00%，忠实度 0.00%
```

**证明的是**：跑分脚本一条命令能跑完三套件、能连上真实 PG、能产出报告。
**不证明**：任何系统能力。全 0 是预期值，不是缺陷。

## L1 判定器自证（`--predictor oracle`）

```
python eval/runner.py --suite all --predictor oracle --dialect postgres

NL2SQL：15/15 = 100.00%   （报错 0，仅列序不同 0）
鲁棒性：11/11 = 100.00%   （退化输入 100.00% / 澄清 100.00%）
RAG：命中率 100.00%，忠实度 100.00%
退出码 0
```

分档明细（真实 PG）：

| 套件 | 合计 | 分档 |
|---|---|---|
| NL2SQL | 15/15 = 100.00% | easy 5/5、medium 5/5、hard 5/5 |
| NL2SQL（按范围） | — | single_table 8/8、multi_table 7/7 |
| 鲁棒性 | 11/11 = 100.00% | 退化输入 9/9、澄清 2/2 |
| RAG | 命中率 100.00% / 忠实度 100.00% | 15 条 |

**严格口径**：不含「仅列序不同」的通过率同为 100.00%，列序歧义 0 条，无需人工复核。

**证明的是**：判定器与执行链路本身没有 bug，且该结论在 PostgreSQL 上同样成立。
**不证明**：任何系统能力。oracle 是把标准答案当预测喂给判定器。

## 与 v1.0.0（SQLite）的对照

| 指标 | SQLite（v1.0.0） | PostgreSQL（本版） | 是否一致 |
|---|---|---|---|
| NL2SQL | 15/15 = 100.00% | 15/15 = 100.00% | 一致 |
| 鲁棒性 | 11/11 = 100.00% | 11/11 = 100.00% | 一致 |
| RAG 命中率 | 100.00% | 100.00% | 一致 |
| RAG 忠实度 | 100.00% | 100.00% | 一致 |
| L0 空基线 | 全 0% | 全 0% | 一致 |

结论：此前基于 SQLite 的结论不是平台假象。判定链路在两个平台上行为一致。

## 冷启动复现（本版基线所在环境是怎么来的）

上面所有数字都不是在「攒了很久的库」上跑出来的，而是在**彻底删掉数据卷后从零重建**的实例上跑的：

```bash
docker compose -f infra/docker-compose.yml down -v      # 连数据卷一起删
docker compose -f infra/docker-compose.aw.yml down -v
docker compose -f infra/docker-compose.yml up -d       # ↓ 两条 up 命令发出后 11 秒
docker compose -f infra/docker-compose.aw.yml up -d    #   两库同时 healthy
```

实测：两库 healthy 用时 **11 秒**（含 Chinook 全量灌库）。
冷启动后 `verify_infra.py --require-aw` 21/21 PASS，本报告的 L1 自证同样 100%。

## 复跑方式

```bash
docker compose -f infra/docker-compose.yml up -d
python infra/scripts/verify_infra.py            # 先确认数据底座与元数据一致
python eval/runner.py --suite all --predictor dry-run --dialect postgres   # 须全 0%
python eval/runner.py --suite all --predictor oracle  --dialect postgres   # 须全 100%
```

两条都必须通过。任一条不满足，先修判定器或执行器，再看真实分数。

## 下一步

本版仍是 **L1**。L2 真实基线需要 A/B 的 M2 双链路接通后才能跑：

- `--predictor jsonl`：A/B 把预测落盘，C 离线跑分（推荐，可复现）
- `--predictor http`：直接打接口（字段名以 `contracts.py` 冻结版为准，尚未验证）
