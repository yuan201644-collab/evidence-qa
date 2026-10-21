# 项目总体 · 1019 更新快照（参考用，未接入运行）

来源：`项目与赛题基础\参考文件\项目总体\`（2026-10-19 之后的工作区快照）。

## 结论先行：只有 `orchestrator.py` 是新的，而且不能直接接进来

| 文件 | 与 `drill_20261019` 包内同路径 | 处理 |
|---|---|---|
| `contracts.py` | **逐字节相同**（sha256 `2268e3af…`） | 仅留档，运行时用 `app/contracts.py` |
| `fact_review.py` | **逐字节相同**（sha256 `282b1439…`） | 仅留档，运行时用 `app/fact_review.py` |
| `pyproject.toml` | **逐字节相同**（sha256 `eee1a9dc…`） | 仅留档，根目录用同一份 |
| `uv.lock` | **逐字节相同**（sha256 `2769a00a…`） | 仅留档 |
| `orchestrator.py` | **不同**：107,726 B / 2,027 行 vs 包内 94,832 B / 1,844 行（sha256 `e61145a9…` vs `9ec4697a…`） | **未接入**，见下 |

## 为什么 `orchestrator.py` 没接进去

新版第 536 行调用：

```python
pending_origin = self.session_memory.pending_prompt(session_id)
```

而本项目当前的 `app/memory.py` 里 `SessionMemory` **没有** `pending_prompt` 这个方法。实测接入后第一个 `/api/ask` 直接 500：

```
AttributeError: 'SessionMemory' object has no attribute 'pending_prompt'
  app/orchestrator.py:536 in _answer_serialized
  app/orchestrator.py:506 in answer
  app/main.py:584 in ask
```

全量比对后，新版对 `session_memory` 的调用只多出 `pending_prompt` 这一个成员（`B-only` 为空），说明它就是 10-19 之后在另一份工作区里继续迭代的产物，配套的 `memory.py` 没有一起流出来。

因此本目录的约定是：

- `app/orchestrator.py` 保持 10-19 交付包的 `9ec4697a…` 版本 —— 与包内 `memory.py` / `contracts.py` / `fact_review.py` 自洽，`/health`、登录、`/api/ask` 全链路可跑。
- 本快照里的 `orchestrator.py` 只作为「下一版要合什么」的参照。要真正启用，需要先在 `app/memory.py` 的 `SessionMemory` 上补齐 `pending_prompt(session_id)`（返回该会话待答的澄清原问，无则 `None`），再整体替换并回归。

## 顺带说明：两份正式报告用的是哪一版

`docs/初赛报告初稿_20261012_v0.1/设计报告/设计报告.docx`（5,241,218 B）与 `技术实现说明书/技术实现说明书.docx`（4,123,604 B）取自同一份 1019 快照，**比包内 `_v0.3` 过程稿更新**，已作为最终稿放进报告目录；`_v0.1` / `_v0.3` 过程稿未纳入。
