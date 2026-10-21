"""metrics.py 的单元测试。

跑法（不需要 pytest）：
    python eval/test_metrics.py

为什么这批测试必须存在：
    评测集的判定器是全队唯一客观标尺。判定器有假阳性（错的判成对）→ 分数虚高，
    初赛评委复跑就对不上；有假阴性（对的判成错）→ 明明做对了却拿低分。
    两者都不能靠"看着像"来保证，只能靠固定用例锁死。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import (  # noqa: E402
    Tally,
    accuracy_band,
    as_cell,
    cell_eq,
    compare_resultsets,
    gold_sql_has_order_by,
)

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"[{name}] 期望 {want!r}，实际 {got!r}")
        print(f"  FAIL  {name}: 期望 {want!r}，实际 {got!r}")
    else:
        print(f"  ok    {name}")


# --------------------------------------------------------------------------------------
# 单元格层
# --------------------------------------------------------------------------------------
def test_cell() -> None:
    print("cell_eq / as_cell")
    check("数值字符串归一为数值", as_cell("12.50").kind, "num")
    check("纯文本保持文本", as_cell("Rock").kind, "text")
    check("None 归一为 null", as_cell(None).kind, "null")
    check("布尔不落进数值分支", as_cell(True).kind, "bool")

    check("1.0 与 1 相等", cell_eq(as_cell(1.0), as_cell(1)), True)
    check("0.1+0.2 与 0.3 相等（容差）", cell_eq(as_cell(0.1 + 0.2), as_cell(0.3)), True)
    check("数值与数值字符串跨类型相等", cell_eq(as_cell(12.5), as_cell("12.50")), True)
    check("文本大小写敏感", cell_eq(as_cell("Rock"), as_cell("rock")), False)
    check("文本两侧空白被去掉", cell_eq(as_cell(" Rock "), as_cell("Rock")), True)
    check("null 只等于 null", cell_eq(as_cell(None), as_cell(0)), False)
    check("null 等于 null", cell_eq(as_cell(None), as_cell(None)), True)


# --------------------------------------------------------------------------------------
# 结果集层 —— 四个口径各一条
# --------------------------------------------------------------------------------------
def test_row_order_insensitive() -> None:
    print("行序无关（默认口径：标准 SQL 不含 ORDER BY）")
    v = compare_resultsets([[1, "a"], [2, "b"]], [["2", "b"], ["1", "a"]])
    check("行序打乱仍 pass", v.kind, "pass")


def test_row_order_sensitive() -> None:
    """口径 L88：标准 SQL 含 ORDER BY 时，行的顺序本身就是答案的一部分。

    这条是 2026-09-20 补的口径缺口：此前 `compare_resultsets` 一律按多重集比较，
    `nl2sql.jsonl` 里含 ORDER BY 的用例，排序写错也照样判过 —— 假阳性、虚高。
    """
    print("行序敏感（order_matters=True）")
    gold = [[1], [2], [3]]
    check(
        "顺序错乱 + 内容相同 → 必须判 fail",
        compare_resultsets(gold, [[3], [2], [1]], order_matters=True).kind,
        "fail",
    )
    check(
        "顺序错乱的原因要写清是「内容对、顺序错」",
        "顺序不同" in compare_resultsets(gold, [[3], [2], [1]], order_matters=True).reason,
        True,
    )
    check("顺序正确 → pass", compare_resultsets(gold, [[1], [2], [3]], order_matters=True).kind, "pass")
    check(
        "内容也不同 → fail（不能和「只是顺序错」混为一谈）",
        compare_resultsets(gold, [[1], [2], [9]], order_matters=True).kind,
        "fail",
    )
    # 回归保护：默认仍然是行序无关。默认值若被改动，所有无序用例都会开始误判。
    check("默认 order_matters=False 时顺序错乱仍 pass", compare_resultsets(gold, [[3], [2], [1]]).kind, "pass")


def _regex_fallback(fn, sql: str):
    """强制走 `gold_sql_has_order_by` 的正则降级分支。

    `sys.modules['sqlglot'] = None` 会让 `import sqlglot` 抛 ImportError，
    于是函数落进 except 分支 —— 本机没装 sqlglot 时的那条路，也必须是对的。
    """
    saved = sys.modules.get("sqlglot", "!absent")
    sys.modules["sqlglot"] = None
    try:
        return fn(sql)
    finally:
        if saved == "!absent":
            sys.modules.pop("sqlglot", None)
        else:
            sys.modules["sqlglot"] = saved


def test_order_by_detection() -> None:
    """`ORDER BY` 的顶层判定 —— 只要是「顶层」，两种实现路径都必须一致。"""
    print("gold_sql_has_order_by：只看顶层")
    cases = [
        ("SELECT name FROM genre ORDER BY name;", True),
        ("SELECT count(*) AS c FROM customer;", False),
        # 窗口函数里的 ORDER BY 不决定结果集顺序，不能算
        ("SELECT row_number() OVER (ORDER BY x) FROM t;", False),
        # 子查询里的 ORDER BY 同理，外层仍是无序的
        ("SELECT * FROM (SELECT a FROM t ORDER BY a) s;", False),
        ("SELECT a FROM t ORDER BY a LIMIT 5;", True),
        ("SELECT a FROM t GROUP BY a ORDER BY count(*) DESC;", True),
    ]
    for sql, want in cases:
        check(f"AST 路径：{sql[:44]}", gold_sql_has_order_by(sql), want)
    for sql, want in cases:
        check(f"正则降级：{sql[:44]}", _regex_fallback(gold_sql_has_order_by, sql), want)


def test_col_order_insensitive_is_loose() -> None:
    print("列序无关 —— 必须落在 pass_loose，不能是 pass")
    v = compare_resultsets([[1, 2]], [[2, 1]])
    check("列序不同判 pass_loose（不是 pass）", v.kind, "pass_loose")
    check("pass_loose 记录列排列", v.column_permutation, (1, 0))
    check("pass_loose 仍算通过", v.ok, True)


def test_float_tolerance() -> None:
    print("浮点容差")
    check("小数尾差在容差内", compare_resultsets([[12.34]], [[12.3400000001]]).kind, "pass")
    check("真实差异仍判错", compare_resultsets([[12.34]], [[12.44]]).kind, "fail")
    check("大额相对容差", compare_resultsets([[1_000_000.0]], [[1_000_000.5]]).kind, "pass")


def test_money_column_tolerance() -> None:
    """口径 2.3：金额差异 1 分钱以内算对（列语义标注 kind="money"）。

    为什么要有这批：默认的 abs/rel 容差是 1e-6，对金额**过严** ——
    144.54 与 144.545 只差半分钱（两处四舍五入位置不同所致），却会被判错。
    这类假阴性会让做对的系统掉分，且看起来像模型算错。
    """
    print("金额列容差（money 走绝对 1 分钱）")
    # 单元格层
    check("money: 差半分钱算对", cell_eq(as_cell(144.54), as_cell(144.545), True, "money"), True)
    check("money: 恰好 1 分算对（含端点）", cell_eq(as_cell(144.54), as_cell(144.55), True, "money"), True)
    check("money: 差 6 分算错", cell_eq(as_cell(144.54), as_cell(144.60), True, "money"), False)
    check("money: 大额差 1 分算对", cell_eq(as_cell(1_000_000.00), as_cell(1_000_000.01), True, "money"), True)
    check(
        "money: 大额差 1 元算错（不做相对放宽——1e-6 相对在大额上会放到 1 元）",
        cell_eq(as_cell(1_000_000.00), as_cell(1_000_001.00), True, "money"),
        False,
    )
    check("money 列的空值仍按 null 判等", cell_eq(as_cell(None), as_cell(None), True, "money"), True)
    check("money 列 null 与 0 判不等", cell_eq(as_cell(None), as_cell(0), True, "money"), False)

    # 「未标注」必须保持历史行为：同样半分钱差异在默认口径下仍判错。
    # 这条是回归保护——若 kind 被误当作默认值，所有数值列都会悄悄放宽。
    check("未标 money：半分钱差异仍判错（默认 1e-6 口径不变）", cell_eq(as_cell(144.54), as_cell(144.545)), False)

    # 结果集层：整列语义生效
    gold = [["Rock", 100.00], ["Jazz", 200.00]]
    pred = [["Rock", 100.005], ["Jazz", 200.005]]
    check("结果集：未标 money -> 判错", compare_resultsets(gold, pred).kind, "fail")
    check(
        "结果集：标 money -> 判对",
        compare_resultsets(gold, pred, column_types=["text", "money"]).kind,
        "pass",
    )

    # 列排列时语义必须**跟着重排**，否则容差张冠李戴。
    # 这条能区分「重排」与「不重排」：不重排时金额列会落到 text 语义上 -> 判 fail。
    g = [[100.00, "Rock"]]
    p = [["Rock", 100.005]]
    check(
        "列序不同 + money 差半分钱 -> pass_loose（列语义跟随列排列）",
        compare_resultsets(g, p, column_types=["money", "text"]).kind,
        "pass_loose",
    )

    # 标注长度与列数不符 -> 视为无效标注、退化为默认口径（判定器不得因此崩）
    check(
        "column_types 长度不符 -> 退化为默认口径（判错，且不抛异常）",
        compare_resultsets(gold, pred, column_types=["money"]).kind,
        "fail",
    )


def test_empty_and_error_separated() -> None:
    print("空结果与报错分开统计")
    v = compare_resultsets([], [])
    check("两侧皆空判 pass", v.kind, "pass")
    check("两侧皆空被标记为无区分度", v.degenerate, True)
    check("标准空 / 预测非空 判错", compare_resultsets([], [[1]]).kind, "fail")

    v2 = compare_resultsets([], [], pred_error="syntax error at or near FROM")
    check("预测报错优先，不被空集掩盖", v2.kind, "error")
    v3 = compare_resultsets([[1]], [[1]], gold_error="column does not exist")
    check("标准 SQL 报错单列为 error", v3.kind, "error")


def test_shape_mismatch() -> None:
    print("形状不一致")
    check("列数不同判错", compare_resultsets([[1, 2]], [[1]]).kind, "fail")
    check("行数不同判错", compare_resultsets([[1], [2]], [[1]]).kind, "fail")
    check("行数相同但值不同判错", compare_resultsets([[1], [2]], [[1], [3]]).kind, "fail")


def test_real_world_shapes() -> None:
    print("真实形态")
    gold = [["Rock", 1297], ["Latin", 579], ["Metal", 374]]
    check(
        "多行多列乱序 + 数值字符串混合",
        compare_resultsets(gold, [[374, "Metal"], ["1297", "Rock"], [579, "Latin"]]).kind,
        "pass_loose",
    )
    check(
        "列序一致、仅行序打乱 → pass",
        compare_resultsets(gold, [["Latin", 579], ["Metal", 374], ["Rock", 1297]]).kind,
        "pass",
    )
    # 复合主键表的联接结果：大量相同行，多重集语义必须正确
    dup_gold = [[1, 2], [1, 2], [1, 3]]
    check("重复行按出现次数算", compare_resultsets(dup_gold, [[1, 3], [1, 2], [1, 2]]).kind, "pass")
    check("重复行少一次即判错", compare_resultsets(dup_gold, [[1, 2], [1, 3], [1, 3]]).kind, "fail")


# --------------------------------------------------------------------------------------
# 档位与汇总
# --------------------------------------------------------------------------------------
def test_bands() -> None:
    print("70/80/90 档位（初赛评分表原文）")
    check("69% 不得分", accuracy_band(0.69)["ge_70"]["score"], 0)
    check("70% 得 6 分", accuracy_band(0.70)["ge_70"]["score"], 6)
    check("80% 得 8 分", accuracy_band(0.80)["ge_80"]["score"], 8)
    check("90% 得 10 分", accuracy_band(0.90)["ge_90"]["score"], 10)
    check("95% 同时满足三档，取最高档 10", (accuracy_band(0.95)["ge_90"]["score"], accuracy_band(0.95)["ge_80"]["score"]), (10, 8))


def test_tally() -> None:
    print("汇总口径")
    t = Tally()
    t.add("a", compare_resultsets([[1]], [[1]]))                       # pass
    t.add("b", compare_resultsets([[1, 2]], [[2, 1]]))                 # pass_loose
    t.add("c", compare_resultsets([[1]], [[2]]))                       # fail
    t.add("d", compare_resultsets([[1]], [], pred_error="boom"))       # error
    s = t.summary()
    check("总数", s["total"], 4)
    check("主口径通过数（含 pass_loose）", s["passed"], 2)
    check("严格通过数（不含 pass_loose）", s["passed_strict"], 1)
    check("pass_loose 单独计数", s["passed_loose_only"], 1)
    check("报错单独计数", s["errored"], 1)
    check("主口径准确率 2/4", s["accuracy"], 0.5)
    check("严格口径准确率 1/4", s["strict_accuracy"], 0.25)
    check("报错计入分母", s["error_rate"], 0.25)
    check("50% 未达 70% 档", s["bands"]["ge_70"]["score"], 0)


def test_git_revision() -> None:
    """跑分 JSON 必须带代码版本（评测口径 L238）。

    这条不属于「判定器」本身，但属于评测链路的硬要求：
    分数不能对应到某次具体代码状态时，「上周 92% 这周 87%」就无法归因。
    放在这里是因为它是跑分前唯一会自动执行的检查入口。
    """
    print("跑分带代码版本（口径 L238）")
    import tempfile

    import runner

    sha, dirty = runner.git_revision()
    check("返回 (str, bool)", (isinstance(sha, str), isinstance(dirty, bool)), (True, True))
    check(
        "SHA 是短哈希或 unknown",
        sha == "unknown" or (len(sha) >= 7 and all(c in "0123456789abcdef" for c in sha)),
        True,
    )

    original = runner.ROOT
    try:
        runner.ROOT = Path(tempfile.mkdtemp())  # 非 git 仓库目录
        check("取不到 git 时降级为 unknown", runner.git_revision(), ("unknown", False))
    finally:
        runner.ROOT = original


def main() -> int:
    for fn in (
        test_cell,
        test_row_order_insensitive,
        test_row_order_sensitive,
        test_order_by_detection,
        test_col_order_insensitive_is_loose,
        test_float_tolerance,
        test_money_column_tolerance,
        test_empty_and_error_separated,
        test_shape_mismatch,
        test_real_world_shapes,
        test_bands,
        test_tally,
        test_git_revision,
    ):
        fn()
        print()

    print("=" * 60)
    if FAILURES:
        print(f"未通过 {len(FAILURES)} 项：")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("全部通过。判定器口径与初始设计一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
