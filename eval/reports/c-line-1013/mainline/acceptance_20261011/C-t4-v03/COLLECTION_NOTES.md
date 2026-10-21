# C-t4-v03 采集说明（T4 = Schema Linking / AdventureWorks）

## 结论：**采集未成功，评分无法进行**（双重阻塞）

## 阻塞 1：LLM 限流（环境侧）

| 运行 | 目录 | 结果 |
|---|---|---|
| 原 run | `C-t4-v03/` | 20 题 **全部 HTTP 200 但内部 LLM 429**，无一条产出 SQL |
| 拆批重跑 | `C-t4-v03b1..b4/` | 每批 5 题、批间等 90 s，仍 **19/20 = 429**、1 题超时 |

- 全部失败的报错口径一致：`调用大模型失败：调用 LLM 失败（已尝试 3 次，末次 HTTP 429）`。
- **对照探针**：等 90 s 后对小请求（`1+1=?`）连续 4 次调用**全部 200**；而 T4 的大 prompt 请求持续 429。
  → 判断为 **token 级限流**：T4 注入 AdventureWorks schema（`schema_meta.adventureworks.json` ≈ 1.2 MB），
  单次请求体量远大于 Chinook，`glm-4.5-flash` 免费档无法承载。**单纯降速无法解决**。
- 合并存档：`C-t4-v03-run2/raw_answers.jsonl`（四批拼接，20 条）。

## 阻塞 2：判据 / 参考文件缺失（A 侧材料）

执行单 4.2 指定的评分命令依赖：

```
t4_v03_rubric.jsonl          ← 不存在
t4_v03_reference_queries.jsonl ← 不存在
```

- 已核：工作区、`mainline-candidate-1010`、`main`、以及 `git log --all` 全历史 **均无**这些文件；
  它们也**未被 `.gitignore` 排除** → 属候选分支**漏提交**（T3 的 `t3_v02_rubric.jsonl` 在，T4 的缺）。
- 后果：`score_t4_development.py` 启动即 `FileNotFoundError`，**T4 无法评分**。

## 产物清单

```
C-t4-v03/raw_answers.jsonl       原 run（20 题全 429）
C-t4-v03/freeze.json
C-t4-v03b1..b4/raw_answers.jsonl 拆批重跑（批间 90 s）
C-t4-v03-run2/raw_answers.jsonl  四批合并
```

## 建议（交负责人）

1. **提升 LLM 配额 / 换通道**：T4 需要能承载 ~1.2 MB schema prompt 的模型（A 的 key / DeepSeek / 更高档 GLM）。
2. **补交 T4 判据与参考文件**：`t4_v03_rubric.jsonl` + `t4_v03_reference_queries.jsonl`（含 `t4_v03_manifest.json`）。
3. 两项齐备后重跑 4.2 的采集与评分命令即可。

> 本目录一律标注为**开发回归**；本轮因阻塞未产出任何 T4 通过/准确率数字。
