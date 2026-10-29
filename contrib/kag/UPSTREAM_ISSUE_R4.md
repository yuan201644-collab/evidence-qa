# 上游 Issue 正文：R4 知识单元解析（已提交 #759）

> **状态：已提交。** <https://github.com/OpenSPG/KAG/issues/759>
> 提交时间 2026-10-29 08:21（UTC+8），提交账号 `yuan201644-collab`，当前状态 `open`。
> 与 `UPSTREAM_ISSUE.md`（R1，已提交为
> [#758](https://github.com/OpenSPG/KAG/issues/758)）相互独立。
>
> 用法：本文件保留 Issue 全文以便复现与引用；如需重新提交，把下面
> `=== 正文开始 ===` 与 `=== 正文结束 ===` 之间的内容整体复制到
> <https://github.com/OpenSPG/KAG/issues/new> ，标题用第一行。
> 中文对照在末尾「中文备查」，仅供报告附录引用，不要粘进 Issue。
>
> 核实基线：KAG 0.8.0 源码包（`KAG_VERSION` 文件内容 `0.8.0`，源码目录
> `OpenSPG-KAG-fdab15b`）。本文所有行号均在该源码上逐行核对。
> **已核对当前 master（2026-10-29）：master 的 sha 就是 `fdab15b`，与本项目基线是
> 同一个 commit，上游一个提交都没往前走，`util.py:10` 仍无 `strict` 参数，缺陷仍在原位。**

---

=== 正文开始 ===

标题：

```
[Bug] knowledge_unit parsing aborts the whole document on a single raw newline inside a JSON string value
```

正文：

### Search before asking

- [x] I had searched in the [issues](https://github.com/OpenSPG/KAG/issues?q=is%3Aissue) and found no similar issue.

### Operating system information

Ubuntu 22.04 (also reproduced on Windows 11), Python 3.10, KAG 0.8.0.
The defect is pure Python + stdlib `json` and platform-independent.

### What happened

Building an unstructured knowledge base (`混合索引` / hybrid index) through the
KAG web UI, the build stalls and no graph is produced. The log repeats every
few seconds:

```
kag/builder/prompt/default/knowledge_unit.py:250  parse_response
  -> kag/builder/prompt/default/util.py:51
     raise ValueError("the output KnowUnit str is invalid: " + respond)
json.decoder.JSONDecodeError: Invalid control character at: line 80 column 10 (char 3640)
```

The LLM put a **raw newline** inside a JSON string value. `json.loads()` defaults
to `strict=True`, which rejects any character in `0x00`–`0x1F` that appears inside a
string literal. The document is then dropped entirely — one bad character kills the
whole extraction round, and the build never converges.

This is not a rare tail case: our corpus is Chinese government / university
announcements, where a table row or an address very often contains a hard line
break, and the extractor is explicitly told to preserve the original text.

### Why the existing 5-layer fallback does not help

`load_knowIE_data()` (`util.py:8-54`) already retries five times. Reading the code,
each layer does one of:

```
:10      json.loads(respond)
:14-18   slice from first '{' to last '}', then respond.replace("\\'", "'")
:21-29   rebuild braces, then fix quotes inside `"Content": "..."` via regex
:32-42   strip ```json fence and `output:` prefix
:49      append a trailing '}' and parse again
:51      raise ValueError(...)
```

**None of the five layers touches control characters inside string values.**
They only re-arrange braces, quotes, fences and the trailing brace.
Searching the whole `kag/` tree of 0.8.0 for `strict=` yields **zero** hits,
and for `control char` / `0x1f` / `unescape_control` also **zero** hits.
So all five layers fail identically, and `:51` is reached every time.

Two secondary issues in the same function (worth a separate look, not required
for this bug):

```
:14-16   left_pos / right_pos are computed on `respond` but used to slice
         `extract_ret_str`, which is `respond` after `modify_knowledge_unit()`
         (`:160-167`, rewrites `"知识点1名称": "xxx",` into `"xxx":`).
         The two strings differ in length, so the slice offsets can be wrong.
:23      the repair regex (?<="Content": ")(.*?)(?=",\n    "Knowledge Type")
         only matches English keys. With the Chinese prompt the keys are
         内容 / 知识类型, so this layer never matches anything.
```

### How to reproduce

No model, no OpenSPG, no network — paste this into a Python 3.10 REPL:

```python
import json

respond = '''{
  "some unit": {
    "内容": "first line
second line",
    "知识类型": "policy"
  }
}'''

json.loads(respond)                    # JSONDecodeError: Invalid control character
json.loads(respond, strict=False)      # works, and keeps the "\n"
```

To see that the real upstream function fails on exactly this input, load
`util.py` directly (it only imports `json`, `re`, `logging`, so it is importable
standalone):

```python
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    "kag_util", "kag/builder/prompt/default/util.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["kag_util"] = mod
spec.loader.exec_module(mod)

mod.load_knowIE_data(respond)   # ValueError: the output KnowUnit str is invalid: ...
```

### Expected behaviour

A raw newline (or tab) inside a JSON string value should be tolerated, the same
way the fallback already tolerates a missing trailing brace. The content should
be preserved verbatim, newline included.

### Actual behaviour

`ValueError` after five failed attempts; the knowledge unit for that document is
lost, and a build over such a corpus can fail 13 times in 7 minutes on a single
document.

### Suggested minimal fix

Pass `strict=False` to the five `json.loads()` calls inside `load_knowIE_data`:

```diff
-        extract_ret = json.loads(respond)
+        extract_ret = json.loads(respond, strict=False)
```

`strict=False` is narrow by definition: it only allows control characters inside
string literals. Missing braces, trailing commas and invalid escapes still raise —
I added assertions for exactly that, because a fix that swallows every parse error
would be worse than the bug.

A full patch is attached as `upstream_patch_knowie_parse.diff`: 5 lines changed,
fallback structure and error message untouched.

If you consider `strict=False` too permissive, an alternative is to apply it only
to the first layer (`:10`) and leave the other four strict — the common case is a
clean JSON payload with one stray newline, which layer 1 would then catch.
Happy to adjust whichever way you prefer.

### Are you willing to submit PR?

Yes, if the direction is acceptable. I can also add the repro above as a pytest
case under the existing test layout.

=== 正文结束 ===

---

## 中文备查（供技术报告附录引用，不要粘进 Issue）

### 现象

建「非结构化 + 混合索引」知识库时构建卡住不出图，日志 7 分钟内重复 13 次：

```
kag/builder/prompt/default/util.py:51
  ValueError: the output KnowUnit str is invalid: ...
json.decoder.JSONDecodeError: Invalid control character at: line 80 column 10 (char 3640)
```

### 根因

模型在 JSON 字符串值里写了裸换行符（`0x0A`）。`json.loads` 默认 `strict=True`，
字符串内部的 `0x00`–`0x1F` 一律拒绝。

上游 `util.py:8-54` 已有 5 层重试，逐层核对后确认：**没有任何一层处理控制字符**，
它们只动大括号、引号、围栏和结尾补 `}`。整棵 `kag/` 源码树里 `strict=` 命中 0 次。

### 修复

`json.loads(..., strict=False)`，5 处。只放行字符串内控制字符，其余语法照旧校验。

### 我们做了什么验证

- 29 项离线断言（`test_knowie_parse.py`），含「修复后其余语法错误仍报错」7 项反向断言；
- 设 `KAG_UTIL_PY` 指向真实上游 `util.py` 后额外 8 项，直接在**上游原函数**上
  验证「修复前抛错 / 打补丁后解析成功 / 正常 JSON 结果不变」；
- 补丁用 `git diff` 从真实文件生成，不是手写。

### 与本项目 R1 的关系

| | R1 | R4 |
| --- | --- | --- |
| 文件 | `schema_free_extractor.py:521` | `util.py:10/18/29/42/49` |
| 后果 | 边全部丢失，图只剩孤立实体 | 整篇文档建图中断 |
| 是否改语义 | 否（把错的数据摆正） | 是（放行原先拒绝的输入） |
| 复现成本 | 需装 KAG | **不需要 KAG**，单文件即可 |
