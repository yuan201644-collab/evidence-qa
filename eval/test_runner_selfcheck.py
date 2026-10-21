"""`runner.py` 里 oracle 自证闸门的回归测试。

跑法（不需要 pytest）：
    python eval/test_runner_selfcheck.py

为什么单独测这一个函数（2026-09-30）：
    oracle 自证检查原先写成 `if "rag" in payload:`，**只覆盖主 RAG 套件**。
    B 在 M6 新增的 `--suite multihop`，其 payload 键是 `multihop` 而不是 `rag`，
    于是自证检查对它**整个跳过** —— 多跳 oracle 单题通过只有 80%（4/20 因
    `forbidden_entities` 与要点撞子串而误判），却照样报「自证通过」、退出码 0。
    这个缺口让 4 条自相矛盾的用例藏了下来，`--suite all` 也发现不了
    （`all` 刻意不含 multihop）。

    `_rag_selfcheck()` 把「每个 RAG 类套件都要接线」收敛成一处可见调用；
    本文件把它的行为与接线状态一起钉住 —— 以后新增套件漏接线，这里会红。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runner import _rag_selfcheck, resolve_dialect  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"[{name}] 期望 {want!r}，实际 {got!r}")
        print(f"  FAIL  {name}: 期望 {want!r}，实际 {got!r}")
    else:
        print(f"  ok    {name}")


def check_true(name: str, got) -> None:
    check(name, bool(got), True)


def _overall(hit: float = 1.0, faith: float = 1.0, passed: float = 1.0) -> dict:
    return {"overall": {"hit_rate": hit, "faithfulness": faith, "pass_rate": passed}}


def test_full_marks_pass() -> None:
    print("满分不报警")
    check("三项全 100% -> 空", _rag_selfcheck({"rag": _overall()}, "rag", "RAG"), [])


def test_each_metric_can_trip() -> None:
    print("三项各自能触发")
    check("命中率掉分 -> 1 条", len(_rag_selfcheck({"rag": _overall(hit=0.8)}, "rag", "RAG")), 1)
    check("忠实度掉分 -> 1 条", len(_rag_selfcheck({"rag": _overall(faith=0.9)}, "rag", "RAG")), 1)
    check("单题通过掉分 -> 1 条", len(_rag_selfcheck({"rag": _overall(passed=0.8)}, "rag", "RAG")), 1)
    check(
        "三项全掉 -> 3 条",
        len(_rag_selfcheck({"rag": _overall(0.8, 0.9, 0.7)}, "rag", "RAG")),
        3,
    )


def test_multihop_actually_wired() -> None:
    """关键回归：多跳套件掉分必须报警 —— 漏过正是这次 bug 藏住的原因。"""
    print("多跳套件必须被覆盖")
    payload = {"multihop": _overall(faith=0.9375, passed=0.8)}
    out = _rag_selfcheck(payload, "multihop", "多跳")
    check("多跳掉分 -> 报警 2 条", len(out), 2)
    check_true("文案带「多跳」标签", all("多跳" in s for s in out))
    # 光有函数不够，main 里必须真的接线；否则等于没修
    src = (Path(__file__).resolve().parent / "runner.py").read_text(encoding="utf-8")
    check_true(
        'main 已接线 _rag_selfcheck(payload, "multihop", "多跳")',
        '_rag_selfcheck(payload, "multihop"' in src,
    )
    check_true(
        'main 已接线 _rag_selfcheck(payload, "rag", "RAG")',
        '_rag_selfcheck(payload, "rag"' in src,
    )


def test_missing_key_is_noop() -> None:
    print("套件缺席时不报警、不抛")
    check("payload 无该键 -> 空", _rag_selfcheck({}, "multihop", "多跳"), [])
    check("payload 为空 -> 空", _rag_selfcheck({}, "rag", "RAG"), [])


def test_dialect_resolution() -> None:
    """回落必须是「可见」的：显式给 --dialect、或给了 DSN，都不该走回落路径。

    背景（2026-10-04，C 线）：`main` 原先写 `args.dialect or ("postgres" if dsn else "sqlite")`，
    没有 `DATABASE_URL` 时静默换成内存 SQLite、退出码仍为 0 —— 判分口径被换掉却无任何提示。
    现由 `resolve_dialect()` 判定并返回来源，调用方据此打告警；本测试把它钉住。
    """
    print("方言回落判定")
    check("显式 sqlite -> 尊重选择", resolve_dialect("sqlite", None), ("sqlite", "explicit"))
    check("显式 postgres -> 尊重选择", resolve_dialect("postgres", None), ("postgres", "explicit"))
    check("有 DSN 无 --dialect -> postgres", resolve_dialect(None, "postgresql://u@h/db"), ("postgres", "env_dsn"))
    check("无 DSN 无 --dialect -> 回落", resolve_dialect(None, None), ("sqlite", "auto_fallback"))
    # 光有函数不够，main 里必须真的对回落打了告警；否则等于没修
    src = (Path(__file__).resolve().parent / "runner.py").read_text(encoding="utf-8")
    check_true(
        "main 对 auto_fallback 打印告警",
        'dialect_source == "auto_fallback"' in src and "file=sys.stderr" in src,
    )
    check_true("告警文案常量存在", "AUTO_FALLBACK_WARNING" in src)


def test_column_types_actually_wired() -> None:
    """列语义标注必须真的传到判定器 —— 只读进报告、不传参，等于没接线。

    背景（2026-10-04，C 线，口径 2.3）：用例侧新增 `column_types` 标注金额列，
    判定器 `compare_resultsets` 加了同名参数。若 runner 里只把该字段写进报告
    （`extra`）却忘了传给判定器，金额容差**不会生效、且看不出来**：
    报告里那一列照常有值，只是判定actually用的仍是 1e-6 相对容差。
    与 `_rag_selfcheck` 漏接线是同一类静默失效，故同样用源码断言钉住。
    """
    print("列语义标注的接线（源码级）")
    import re

    src = (Path(__file__).resolve().parent / "runner.py").read_text(encoding="utf-8")
    n_compare = 0
    missing: list[int] = []
    for m in re.finditer(r"compare_resultsets\(", src):
        block = src[m.start() : m.start() + 600]
        # 只认「拿真实结果比较」的分支：dry-run 那段是 pred_error=pred.error，不去比较结果
        if "pred_error=pred_err" not in block:
            continue
        n_compare += 1
        if 'column_types=case.get("column_types")' not in block:
            missing.append(m.start())
    check("真实结果比较分支数（nl2sql / robust）", n_compare, 2)
    check("每处都传了 column_types", missing, [])


def main() -> int:
    test_full_marks_pass()
    test_each_metric_can_trip()
    test_multihop_actually_wired()
    test_missing_key_is_noop()
    test_dialect_resolution()
    test_column_types_actually_wired()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项失败：")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
