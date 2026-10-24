# -*- coding: utf-8 -*-
"""R6 锚点抽取的离线断言（不需要 KAG、不需要 Neo4j、不需要联网）。

    python test_anchor_query.py

覆盖三类容易出错的地方：
1. 编号被截短（``2016SC01007`` 不许退化成 ``SC01007``）；
2. 年份/页码被误当锚点（会把整库 CONTAINS 进来）；
3. 命中排序把"只顺带提一句"的长文档排在名单表格之前。
"""

import sys

from anchor_query import extract_anchors, looks_like_anchor, rank_hits

CHECKS = []


def check(name, got, want):
    ok = got == want
    CHECKS.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + name)
    if not ok:
        print("        got : %r" % (got,))
        print("        want: %r" % (want,))


def main():
    print("[1] 编号抽取")
    check(
        "NYS 项目编号",
        extract_anchors("NYS212001 的项目名称、申请人、所在单位？"),
        ["NYS212001"],
    )
    check(
        "数字打头的编号不被截短",
        extract_anchors("2016SC01007 这个项目是谁主持的"),
        ["2016SC01007"],
    )
    check(
        "多段合同号整段保留",
        extract_anchors("HT-MNT-2026-001 的验收单在哪里"),
        ["HT-MNT-2026-001"],
    )
    check(
        "语料文件编号",
        extract_anchors("ATT_65041_01 里写了什么"),
        ["ATT_65041_01"],
    )
    check(
        "中文文号",
        extract_anchors("苏社〔2024〕1号 的申报要求"),
        ["苏社〔2024〕1号"],
    )
    check(
        "连写编号区间整段保留",
        extract_anchors("NYS212001-NYS212012 这一批共几项"),
        ["NYS212001-NYS212012"],
    )

    print("[2] 不该被当成锚点的东西")
    check("年份", extract_anchors("2024 年一般项目有几项"), [])
    check("页码", extract_anchors("见第 3 页第 2 段"), [])
    check("纯中文", extract_anchors("引进人才项目的申报条件"), [])
    check("四位数年份不接受", looks_like_anchor("2024"), False)
    check("短串不接受", looks_like_anchor("A12"), False)

    print("[3] 去重与上限")
    check(
        "大小写重复只留一个",
        extract_anchors("NYS212001 与 nys212001 是同一个吗"),
        ["NYS212001"],
    )
    many = " ".join("NYS2120%02d" % i for i in range(12))
    check("上限生效", len(extract_anchors(many, max_anchors=5)), 5)

    print("[4] 命中排序")
    rows = [
        {"content": "本次立项共 30 项，其中引进人才项目 13 项。"},  # 无锚点
        {"content": "……详见 NYS212001 附件……" + "无关内容" * 200},  # 命中 1，长
        {"content": "1\tNYS212001\t引进人才\t张艺\t传媒与艺术学院\n2\tNYS212014\t重点\t袁周敏\t…"},  # 命中 2，短
    ]
    ranked = rank_hits(["NYS212001", "NYS212014"], rows)
    check("无锚点的块被剔除", len(ranked), 2)
    check("命中多的排第一", ranked[0][2], ["NYS212001", "NYS212014"])
    check(
        "只命中一个的排第二",
        ranked[1][2],
        ["NYS212001"],
    )
    check("分数落在 0-1", all(0.0 <= s <= 1.0 for _r, s, _m in ranked), True)
    check("空锚点直接返回空", rank_hits([], rows), [])

    print("[5] 真实问法回归")
    q = "NYS212001 的项目名称、申请人、所在单位？"
    check("真实问法抽到 1 个锚点", extract_anchors(q), ["NYS212001"])
    q2 = "2012 年校级项目里袁周敏报的是哪一类？"
    check("无人造编号时不产出锚点", extract_anchors(q2), [])

    total = len(CHECKS)
    passed = sum(CHECKS)
    print("")
    print("%d/%d assertions passed" % (passed, total))
    if passed != total:
        print("SOME CHECKS FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
