"""KAG 抽取层改进中的纯逻辑（不依赖 KAG，可离线单测）

为什么单独拆一个文件
--------------------
R1（三元组结构修复）与 R2（实体名归一）的**判定逻辑**是纯函数，
不需要 KAG、不需要大模型、不需要图数据库就能验证。把它们从
`kag_improve.py` 里拆出来，是为了让评审在一台没装 KAG 的机器上
也能跑 `python test_kag_normalize.py` 看到修复前后的差异，
而不是只能相信报告里的数字。

依赖：仅标准库。
许可：Apache-2.0。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Tuple

# ---------------------------------------------------------------------------
# 与 KAG 0.8.0 `kag/common/utils.py:196 processing_phrases` 完全相同的字符类：
# 保留 ASCII 字母数字、CJK 基本区（\u4e00-\u9fa5）与半角空格，其余替换为空格。
# 保持一致是为了让「我们比上游宽松」这件事只发生在空白上，不引入别的语义变化。
# ---------------------------------------------------------------------------
UPSTREAM_CHARCLASS = re.compile(r"[^A-Za-z0-9\u4e00-\u9fa5 ]")
_WHITESPACE = re.compile(r"\s+")

# 上游用来兜底「无法归类」的边类型取值（不同版本大小写不一）。
OTHER_TYPE_ALIASES = frozenset(
    {"OTHER_TYPE", "other_type", "Other", "OTHER", "UNKNOWN", ""}
)


def upstream_text(value: Any) -> str:
    """上游 `processing_phrases` 的等效实现（保留内部空格）。

    用于对照：证明我们与上游的差别只在空白处理上。
    """
    return UPSTREAM_CHARCLASS.sub(" ", str(value or "").lower()).strip()


def grounding_text(value: Any) -> str:
    """实体比对用的归一化文本：上游字符类规则 + 去空白（R2）。

    对只差空格/换行的同一实体，归一化后相等；对真正不同的实体不引入新匹配。
    """
    text = str(value or "").lower()
    return _WHITESPACE.sub("", UPSTREAM_CHARCLASS.sub(" ", text))


def unpack_triples(triples: Any) -> List[List[Any]]:
    """把抽取器返回的三元组布局归一成 `list[list]`（R1）。

    上游两条路径的返回布局不同：
      - 同步 `_invoke`  ：`(triples_list,)` —— 外层长度 1 的元组（第 521 行尾逗号）
      - 异步 `_ainvoke` ：`triples_list`    —— 已经是 list[list]

    判定规则刻意保守：只有「外层恰好 1 个元素 + 该元素是 list/tuple +
    它的首个元素也是 list/tuple」时才拆一层。真正的单条三元组
    `[["A", "rel", "B"]]` 首个元素是字符串，不会被误拆。
    """
    if triples is None:
        return []
    if not isinstance(triples, (list, tuple)):
        return []
    if (
        len(triples) == 1
        and isinstance(triples[0], (list, tuple))
        and len(triples[0]) > 0
        and isinstance(triples[0][0], (list, tuple))
    ):
        return [list(t) for t in triples[0]]
    return [list(t) for t in triples if isinstance(t, (list, tuple))]


def count_usable_edges(triples: Any) -> int:
    """模拟上游 `assemble_sub_graph_with_triples` 的过滤，数出「真正能被接纳」的边数。

    上游第 344-346 行：
        for tri in triples:
            if tri is None or len(tri) != 3:
                continue

    传入修复前的布局（`(triples_list,)`）会得到 0（或极少数巧合），
    传入修复后的布局会得到真实边数。这个函数就是「修复生效」的可执行证据。
    """
    usable = 0
    if not isinstance(triples, (list, tuple)):
        return 0
    for tri in triples:
        if tri is None:
            continue
        try:
            if len(tri) != 3:
                continue
        except TypeError:
            continue
        # 上游随后会取 tri[0]/tri[1]/tri[2]；这里只判断长度，足够区分两种布局。
        usable += 1
    return usable


def build_graph_metrics(edges: Iterable[Tuple[str, str, str]]) -> Dict[str, Any]:
    """从「(主语, 关系类型, 宾语)」边集合算出图谱质量指标（R3）。

    参数
    ----
    edges : 可迭代的 `(subject, edge_type, object)`。

    返回
    ----
    `total_edges` / `semantic_edges` / `other_edges` / `semantic_edge_ratio` /
    `other_edge_ratio` / `edge_type_distribution` / `definition`。

    注意
    ----
    `semantic_edge_ratio` 只衡量「边是否被赋予了有意义的关系类型」，
    不衡量关系语义是否正确。后者只能人工抽样核对。
    """
    counter: Counter = Counter()
    total = 0
    for _subject, edge_type, _obj in edges:
        total += 1
        counter[str(edge_type or "")] += 1

    other = sum(n for t, n in counter.items() if t in OTHER_TYPE_ALIASES)
    semantic = total - other
    return {
        "total_edges": total,
        "semantic_edges": semantic,
        "other_edges": other,
        "semantic_edge_ratio": round(semantic / total, 4) if total else 0.0,
        "other_edge_ratio": round(other / total, 4) if total else 0.0,
        "edge_type_distribution": dict(counter.most_common()),
        "definition": (
            "semantic_edge_ratio = 不含 OTHER_TYPE 等兜底类型的边 / 全部边。"
            "只反映关系类型是否被赋上，不反映关系语义是否正确。"
        ),
    }
