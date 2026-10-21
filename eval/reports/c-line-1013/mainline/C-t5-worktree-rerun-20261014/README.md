# T5 评分 —— 在基于 d03c3d9 的独立临时工作副本中重跑（数据绑定 rev1）

- 日期：2026-10-14
- 范围：**已曝光开发回归**，非未见题/泛化证据，非正式比赛成绩
- 结果标签：**运行数据绑定修订后的开发回归评分**
- 独立工作副本：`C:\Users\ASUS\WorkBuddy\_c_tmp_t5rev1_d03c3d9`（`git worktree`，HEAD = `d03c3d900efd13875107108eb642c44450fb2d6e`）
- 主仓库提交（发起本次操作时）：`c72890d249c80333e916000bddf87b4fd357e544`
- 所用基线提交：`d03c3d900efd13875107108eb642c44450fb2d6e`（A 指定）

## 结果
- **19/20**（strict 19，accuracy 0.95，error_rate 0.0）
- easy 2/2、medium 7/7、hard 10/11；single_table 5/5、multi_table 14/15
- 唯一未过：`sem-12`（标准 27 行 vs 预测 51 行）
- **数据库指纹校验：通过**（`database_fingerprint_verified = True`，`database_label_verified = chinook`）
- 冻结运行：`20261013T105820Z-effe3f4d`；原始回答 sha256 `cb168b98a3f8e62c0aa57e123277506b62f1b00448562bb53b9f1ea3707cc06a`；绑定后 freeze sha256 `773c3ed052836e8b4cfa4232cf74f00b359e2664eeb2524408d1e2a737577c53`

## 与上一轮的一致性
本轮（独立副本、Python 3.11、cwd=副本）产出的评分 JSON 与上一轮（工作区直跑）**除 `generated_at` 外逐字段完全相同** → 结果可复现。

## 做法（零覆盖）
- 副本内复制**归档原始回答**（`eval/reports/c-line-1013/mainline/C-review-1013/t5-throttled/raw_answers.jsonl`）。
- 副本内 `freeze.json` **仅改** `data_manifest` 指向 `t5_v02_data_manifest.rev1.json`（sha256 `b08765bc…`）。
- rev1 清单置于副本 `eval/mainline/development_v0.1/`；题包/参考答案/评分器**未改**（直接取自 `d03c3d9` 基线）。
- 未重跑模型（沿用 10-13 原始回答）。
- 主仓库既有归档、原清单、原始 freeze **全部未动**。
