# 上游 Issue 正文（可直接粘贴到 OpenSPG/KAG）

> 用法：把下面 `=== 正文开始 ===` 与 `=== 正文结束 ===` 之间的内容整体复制到
> <https://github.com/OpenSPG/KAG/issues/new> ，标题用第一行。
> 中文对照见本文件末尾「中文备查」，仅供报告附录引用，不要粘进 Issue。

---

=== 正文开始 ===

标题：

```
[Bug] schema_free_extractor: trailing comma in sync _invoke() wraps triples in a 1-tuple and silently drops every extracted edge
```

正文：

### Search before asking

- [x] I had searched in the [issues](https://github.com/OpenSPG/KAG/issues?q=is%3Aissue) and found no *open* similar issue. Related but closed: #583 (see "Prior art" below).

### Operating system information

Windows 11, Python 3.10, KAG 0.8.0 (`KAG_VERSION` = `0.8.0`). The defect is pure Python and platform-independent.

### What happened

In `SchemaFreeExtractor._invoke()` (the **synchronous** path) the triple list returned by
`triples_extraction()` is wrapped in a 1-element tuple because of a trailing comma:

```python
# kag/builder/component/extractor/schema_free_extractor.py:515  (master, blob 6890adc2)
triples = (self.triples_extraction(passage, filtered_entities),)
```

Downstream, `assemble_sub_graph_with_triples()` iterates that value as if it were a list of triples:

```python
# kag/builder/component/extractor/schema_free_extractor.py:340-341
for tri in triples:
    if tri is None or len(tri) != 3:
        continue
```

So the loop body runs **exactly once**, with `tri` bound to the whole list of triples instead of one
triple. Two outcomes, both wrong:

| LLM returns | `len(tri)` | result |
|---|---|---|
| N ≠ 3 triples | N | `continue` → **all N edges silently dropped**, no error, no log |
| exactly 3 triples | 3 | guard is bypassed → **1 bogus edge** whose subject/object are `str()` of the Python list literal, e.g. `'spark    released by    apache foundation' --spark____supports____kafka--> 'kafka    maintained by    apache foundation'` |

The async path does **not** have the comma:

```python
# kag/builder/component/extractor/schema_free_extractor.py:548
triples, std_entities = await asyncio.gather(
    self.atriples_extraction(passage, filtered_entities),
    self.anamed_entity_standardization(passage, filtered_entities),
)
```

and `assemble_sub_graph()` is annotated `triples: List[list]`. Since the default
`unstructured_builder_chain` (`kag/builder/default_chain.py`) drives the **sync** `invoke()`, the
default builder pipeline takes the broken path.

### How to reproduce

No LLM, no OpenSPG service, no KAG runtime needed — the loop logic is self-contained:

```python
import re

def processing_phrases(phrase):                      # kag/common/utils.py:196, verbatim
    phrase = str(phrase)
    return re.sub("[^A-Za-z0-9\u4e00-\u9fa5 ]", " ", phrase.lower()).strip()

def to_camel_case(phrase):                           # kag/common/utils.py:201, verbatim
    return processing_phrases(phrase).replace(" ", "_")

OTHER = "Others"

class SG:
    def __init__(self): self.edges = []
    def add_node(self, *a, **k): pass
    def add_edge(self, s_id, s_label, p, o_id, o_label): self.edges.append((s_id, p, o_id))

def cat_name(ents, name):
    for e in ents:
        if processing_phrases(e["name"]) == processing_phrases(name):
            return e["category"], e["name"]
    return None, None

def assemble(sg, ents, triples):                     # body of
    for tri in triples:                              # assemble_sub_graph_with_triples
        if tri is None or len(tri) != 3:
            continue
        s_cat, s_name = cat_name(ents, tri[0])
        tri[0] = processing_phrases(tri[0])
        if tri[0] == "": continue
        if s_cat is None:
            s_cat, s_name = OTHER, tri[0]
            sg.add_node(s_name, s_name, s_cat)
        o_cat, o_name = cat_name(ents, tri[2])
        if o_name == "": continue
        if o_cat is None:
            o_name, o_cat = processing_phrases(tri[2]), OTHER
            sg.add_node(o_name, o_name, o_cat)
        et = to_camel_case(tri[1])
        if et: sg.add_edge(s_name, s_cat, et, o_name, o_cat)
    return sg

LLM = [["Spark", "released by", "Apache Foundation"],
       ["Spark", "supports", "Kafka"],
       ["Kafka", "maintained by", "Apache Foundation"],
       ["Spark", "is a", "processing engine"],
       ["Kafka", "is a", "message queue"]]

for label, wrapped in (("as in _invoke() :515", True), ("as in _ainvoke() :548", False)):
    data = [list(t) for t in LLM]
    sg = assemble(SG(), [], (data,) if wrapped else data)
    print(f"{label:26s} -> {len(sg.edges)} edges")
```

Output:

```
as in _invoke() :515       -> 0 edges
as in _ainvoke() :548      -> 5 edges
```

### Expected behaviour

Both paths build the same subgraph from the same triple list — i.e. 5 edges above.

### Actual behaviour

The sync path builds 0 edges (or, for exactly 3 triples, 1 garbage edge).

### Impact we measured

On a 15-document / 103-chunk procurement corpus, changing **only** this unwrapping (a subclass that
expands a 1-tuple, upstream source untouched) moved the semantic-relation edge ratio of the built
graph from **0.57 % (152/455) to 33.41 %**, and dropped the NER-side `Others` share from
**41.1 % to 2.4 %**. Note the side effect: with the fix the graph also gains more `Others` *nodes*
(1.9 % → 44.1 %) because triples now really contribute fallback nodes — i.e. the graph becomes
argument-like instead of entity-only. Before the fix, KAG's graph index effectively contained
entities without relations.

### Prior art

#583 reports the same root cause ("triples has been wrongly set as a tuple, it should be
List[list]") and links this very function. It was **closed unmerged** on 2025-06-27
(`merged_at: null`). Its current diff contains only a `README.md` badge-link change
(`changed_files: 1`, `additions: 1`, `deletions: 1`), so the extractor change never appeared in a
reviewable diff. I cannot tell from the outside whether the fix was rejected on the merits or lost
when the fork diverged — hence this Issue rather than a comment on the closed PR.

Still present on master as of blob `6890adc2` (21202 bytes, 557 lines); the last commit touching
this file is #593 (2025-06-19).

### Suggested minimal fix

```diff
-        triples = (self.triples_extraction(passage, filtered_entities),)
+        triples = self.triples_extraction(passage, filtered_entities)
```

`_invoke` already assumes `self.triples_extraction()` returns a list (the async sibling awaits the
same function and passes the value straight through), so this is a one-line, behaviour-restoring
change. If a defensive shape check is preferred, `raw = ...; triples = raw if isinstance(raw, list)
else [raw]` also works and additionally tolerates a provider returning a single triple, but I would
keep it at the plain unwrap so the two paths stay symmetric.

I am happy to send this as a PR (with the offline regression test above as a `pytest` case).

### Are you willing to submit PR?

- [x] Yes I am willing to submit a PR!

=== 正文结束 ===

---

## 中文备查（供技术报告附录引用，不要粘进 Issue）

**标题**：schema_free_extractor 同步分支尾逗号把三元组包成单元素元组，导致抽出的关系边被整批静默丢弃

**问题**：`_invoke()` 第 515 行 `triples = (self.triples_extraction(...),)` 的尾逗号，使下游
`for tri in triples` 只循环一次、`tri` 变成「全部三元组」这个 list 本身。于是：LLM 返回 N ≠ 3 条时，
`len(tri) != 3` 命中 `continue`，**N 条边全部静默丢弃**；恰好 3 条时绕过保护，生成 **1 条主客体都是
Python 列表字面量**的假边。异步分支 `_ainvoke()` 第 548 行用 `asyncio.gather` 直接取 list，没有这个
问题；`assemble_sub_graph(triples: List[list])` 的类型标注也说明 list 才是预期形态。默认建图链
`unstructured_builder_chain` 走的是同步分支，所以默认流程必然触发。

**复现**：40 行纯逻辑脚本，不需要 LLM、OpenSPG 服务或 KAG 运行时。同一份 5 条三元组输入，同步形态
产出 0 条边，异步形态产出 5 条边。

**实测影响**（15 文档 / 103 块政务采购语料，只改这一处解包）：语义关系边占比 0.57 %（152/455）
→ 33.41 %；NER 口径 Others 41.1 % → 2.4 %。副作用：整图口径 Others 节点 1.9 % → 44.1 %，因为三元组
真正开始贡献兜底节点。

**与 #583 的关系**：#583 报的是同一个根因，2025-06-27 关闭且未合并（`merged_at: null`）；其当前
diff 只剩 README 徽章链接改动（`changed_files: 1`），抽取器的那行改动从未出现在可评审的 diff 里。
master 至今（blob `6890adc2`）仍有该缺陷，该文件最后一次被触及是 #593（2025-06-19）。
