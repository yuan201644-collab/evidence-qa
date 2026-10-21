"""`kag_normalize.py` 的自测：不需要 KAG、不需要大模型、不需要图数据库。

直接运行：
    python test_kag_normalize.py

它验证三件事，每件都对应报告里的一个数字口径：

1. R1 修复前后，上游 `assemble_sub_graph_with_triples` 能接纳的边数
   —— 修复前是 0（整批丢边），修复后是真实条数。
   并复现「某段文本恰好抽出 3 条三元组时会产生 1 条错误边」这个观察。
2. R2 的归一化确实只放宽了空白，没有放宽别的字符。
3. R3 的指标算式与报告里的 152/455 = 33.41% 对得上。

依赖：仅标准库。
"""

from __future__ import annotations

import sys

from kag_normalize import (
    build_graph_metrics,
    count_usable_edges,
    grounding_text,
    unpack_triples,
    upstream_text,
)

_failures: list[str] = []


def check(name: str, actual, expected) -> None:
    ok = actual == expected
    print(("  PASS  " if ok else "  FAIL  ") + name)
    if not ok:
        print(f"        expected={expected!r}  actual={actual!r}")
        _failures.append(name)


def main() -> int:
    # ------------------------------------------------------------------
    print("[1] R1 三元组结构：修复前后能被上游接纳的边数")
    # 一段文本抽出 5 条三元组 —— 上游同步路径的实际返回值是 (list,)
    triples_list = [
        ["智能计算设备", "质保期", "36 个月"],
        ["GPU 加速卡", "质保期", "24 个月"],
        ["采购人", "为", "某研究院"],
        ["合同", "签订于", "2026-03-12"],
        ["维保服务", "覆盖", "全国 31 省"],
    ]
    buggy = (triples_list,)  # 上游第 521 行尾逗号造成的布局
    fixed = unpack_triples(buggy)

    check("上游布局下可接纳边数（丢边）", count_usable_edges(buggy), 0)
    check("归一化后布局合法", isinstance(fixed, list) and all(isinstance(t, list) for t in fixed), True)
    check("归一化后可接纳边数", count_usable_edges(fixed), 5)
    check("归一化不改变三元组内容", fixed, triples_list)

    # 恰好 3 条时，上游会把「3 条」误当成「1 条的三元组」，产生 1 条错误边
    three = triples_list[:3]
    check("恰好 3 条时产生的错误边数", count_usable_edges((three,)), 1)
    check("同样 3 条修复后的边数", count_usable_edges(unpack_triples((three,))), 3)

    # 保守性：正常的单条三元组不能被误拆
    check("单条三元组不被误拆", unpack_triples([["A", "rel", "B"]]), [["A", "rel", "B"]])
    check("空输入", unpack_triples(None), [])

    # ------------------------------------------------------------------
    print("[2] R2 实体名归一：只放宽空白")
    split = "智能计算\n设备"          # PDF 折行把词切开
    joined = "智能计算设备"            # 实体列表里的写法
    check("上游口径判定为不同", upstream_text(split) == upstream_text(joined), False)
    check("我们的口径判定为相同", grounding_text(split) == grounding_text(joined), True)

    # 不放宽其它字符：标点/大小写仍按上游字符类处理
    check("标点仍被替换", grounding_text("A（B）") == grounding_text("A  B "), True)
    check("大小写仍被折叠", grounding_text("ABC") == grounding_text("abc"), True)
    # 真正不同的实体不能相等
    check("不同实体不相等", grounding_text("GPU 加速卡") == grounding_text("GPU 加速器"), False)

    # ------------------------------------------------------------------
    print("[3] R3 指标算式：与报告的 152/455 对得上")
    edges = [("s", "保期", "o")] * 152 + [("s", "OTHER_TYPE", "o")] * (455 - 152)
    m = build_graph_metrics(edges)
    check("总边数", m["total_edges"], 455)
    check("语义关系边数", m["semantic_edges"], 152)
    check("语义关系边占比", m["semantic_edge_ratio"], 0.3341)
    check("兜底类型边占比", m["other_edge_ratio"], 0.6659)

    # ------------------------------------------------------------------
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} 项 —— {', '.join(_failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
