"""NL2SQL / RAG 评测的判定口径实现（纯函数，无 IO，无数据库）。

口径来自项目组已冻结的决定（见 项目概况 §四）：**执行结果集一致，不用 SQL 文本匹配**。
本文件把这句话变成可执行、可单测的代码。

五条细则（每条都对应一个测试用例，见 test_metrics.py）：
  1. 行序：**仅当 `gold_sql` 含 `ORDER BY` 时**才比对顺序；不含则忽略
     （`docs/评测口径_v0.1.md` L88）。本模块是纯函数，不自己解析 SQL——
     由调用方用 `gold_sql_has_order_by()` 判定后，通过 `order_matters=` 传进来。
  2. 列序无关：SELECT 的列顺序不同不算错；但列数必须一致。
     为不让「列序无关」掩盖假阳性，实际返回 pass / pass_loose 两档：
     只有列序差异 → pass_loose，报告里单独计数，可人工复核。
  3. 浮点容差：数值列按 abs/rel 双向容差比较，避免 DECIMAL 与 float 的表示差异判错。
     被显式标注为金额的列（`column_types` 里的 "money"）改走**绝对 1 分钱**（口径 2.3）——
     金额有最小货币单位，相对容差在大额上会放得过多，反而错。
  4. 空结果与报错分开统计：空 vs 空是 pass；报错是 error，单独算错误率，不混进准确率分母的语义里。
  5. 规模上限：结果集很大时退化为精确匹配，不跑 O(n^2) 容差匹配（防止跑分脚本卡死）。
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Literal, Sequence

# ---- 判定阈值（写死为常量，不要散落在调用处）----
ABS_TOL = Decimal("1e-6")
REL_TOL = Decimal("1e-6")
#: 金额列的绝对容差（口径 2.3：金额差异 1 分钱以内算对）。
#:
#: 为什么是**绝对** 0.01 而不是相对容差：
#:   金额有法定最小单位（分）。相对容差在金额上反而错 —— 144.54 的 1e-6 相对容差
#:   只有 0.00014，比一分钱严得多，覆盖不了「标准 SQL 与预测 SQL 在表达式里
#:   四舍五入位置不同」产生的分位级差异（如 ROUND(a+b,2) 与 ROUND(a,2)+ROUND(b,2)）。
#:
#: 只有该列被显式标注为金额（`column_types` 里取 "money"）时才启用；
#: 未标注的列一律走上面的 abs/rel 容差，行为与历史一致。
MONEY_ABS_TOL = Decimal("0.01")

#: 结果列的语义标注取值（用例 `column_types` 字段）。**只覆盖判定语义，不改变值比较以外的东西**：
#:   "money"  —— 金额列，按绝对 1 分钱比较（口径 2.3）
#:   "number" —— 普通数值列，按 abs/rel 双向容差（默认行为）
#:   "text"   —— 文本列，要求完全相等
#: 注意：这是**结果列**的语义（题目级），与源字段是不是金额（库级，见 `infra/schema_meta`）
#: 是两件事 —— `SELECT sum(unit_price*quantity) AS revenue` 的结果列不对应任何单一源字段。
COLUMN_KINDS = ("money", "number", "text")

# 超过这两个规模就退化为精确匹配 / 不做列排列尝试
MAX_ROWS_FOR_TOLERANCE = 2000
MAX_ROWS_FOR_PERMUTATION = 200
MAX_COLS_FOR_PERMUTATION = 6
NULL_TOKEN = "\x00NULL"

PassKind = Literal["pass", "pass_loose", "fail", "error"]


# --------------------------------------------------------------------------------------
# 单元格归一与比较
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Cell:
    kind: Literal["null", "num", "bool", "text"]
    value: Any

    def key(self) -> tuple:
        if self.kind == "num":
            # 归一化数值的字符串形式：去掉多余的 0，便于做精确多重集匹配的快速路径
            return ("num", format(self.value.normalize(), "f"))
        return (self.kind, self.value)


def as_cell(v: Any) -> Cell:
    if v is None:
        return Cell("null", None)
    if isinstance(v, bool):
        return Cell("bool", v)
    if isinstance(v, (int, float, Decimal)):
        return Cell("num", Decimal(str(v)))
    s = str(v)
    # 驱动可能把数值列回成字符串；先试数值，失败再当文本
    try:
        return Cell("num", Decimal(s.strip()))
    except (InvalidOperation, ValueError):
        return Cell("text", s.strip())


def cell_eq(a: Cell, b: Cell, tol: bool = True, kind: str | None = None) -> bool:
    """单元格相等判定。

    `kind` 是该列的语义标注（`COLUMN_KINDS` 之一，来自用例的 `column_types`）：
      · "money" —— 走绝对 `MONEY_ABS_TOL`（1 分钱），**不走相对容差**（见常量注释）
      · 其余 / None —— 走历史口径的 abs + rel 双向容差
    `tol=False`（精确模式）时忽略 `kind`，一律按归一化字符串精确比对。
    """
    if a.kind == "null" or b.kind == "null":
        return a.kind == b.kind
    if a.kind == "num" and b.kind == "num":
        if not tol:
            return a.key() == b.key()
        diff = abs(a.value - b.value)
        if kind == "money":
            # 金额：绝对 1 分钱。这里**不叠加**相对容差 —— 大额金额上相对容差会放得过多
            # （100 万 × 1e-6 = 1 元），那不是「一分钱以内」。
            return diff <= MONEY_ABS_TOL
        if diff <= ABS_TOL:
            return True
        scale = max(abs(a.value), abs(b.value))
        return scale > 0 and (diff / scale) <= REL_TOL
    if a.kind == "bool" and b.kind == "bool":
        return a.value == b.value
    if a.kind == "text" and b.kind == "text":
        return a.value == b.value
    # 类型不同（数值 vs 文本）已在 as_cell 阶段尽量归一，到这说明确实不同
    return False


def row_eq(a: Sequence[Cell], b: Sequence[Cell], tol: bool = True, kinds: Sequence[str | None] | None = None) -> bool:
    """行相等判定。`kinds` 按列给出语义标注（长度应等于列数），缺席时退化为无语义。"""
    if len(a) != len(b):
        return False
    if kinds is None:
        return all(cell_eq(x, y, tol) for x, y in zip(a, b))
    return all(
        cell_eq(x, y, tol, kinds[i] if i < len(kinds) else None)
        for i, (x, y) in enumerate(zip(a, b))
    )


def _strip_nested_parens(text: str) -> str:
    """去掉括号及其内部内容（支持嵌套），用于「只看顶层」的近似判定。"""
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def gold_sql_has_order_by(sql: str) -> bool:
    """判断标准 SQL **顶层**是否含 `ORDER BY` —— 决定行序要不要参与判定。

    口径（`docs/评测口径_v0.1.md` L88）：
    「**行的顺序** ｜ **只有在 `gold_sql` 含 `ORDER BY` 时才比对**；不含则忽略 ｜
      没有排序要求时，数据库返回顺序本来就不确定」

    优先走 sqlglot 的 AST（项目技术约束：SQL 校验与方言解耦用 sqlglot）；
    未安装或解析失败时退到正则 —— **评测不允许因为解析器缺失就跑不出分**。

    只看**顶层**的 ORDER BY，两类都不能算：
      · 窗口函数里的 `OVER (ORDER BY x)` —— 它不决定结果集顺序
      · 子查询里的 `ORDER BY` —— 外层仍是无序的
    AST 路径用 `tree.args.get("order")`（只读 Select 自己的 order 参数，不是 `find`）；
    正则降级路径先去掉括号内容再匹配，等价地达到「只看顶层」。
    """
    text = sql or ""
    try:
        import sqlglot

        tree = sqlglot.parse_one(text, read="postgres")
        return tree is not None and tree.args.get("order") is not None
    except Exception:  # noqa: BLE001 —— 解析器缺失/方言不支持都不该让评测崩
        return bool(re.search(r"\border\s+by\b", _strip_nested_parens(text), re.I))


# --------------------------------------------------------------------------------------
# 结果集比较
# --------------------------------------------------------------------------------------
@dataclass
class Verdict:
    kind: PassKind
    reason: str = ""
    gold_rows: int = 0
    pred_rows: int | None = None
    column_permutation: tuple[int, ...] | None = None
    degenerate: bool = False
    """标准结果为空集时为 True。它仍按 pass/fail 的正常语义判定（空=空即 pass），
    但报告里必须单独列出——一个恒为空集的用例对准确率没有任何区分度
    （对应任务书风险清单第 1 条：用例要有区分度）。"""

    @property
    def ok(self) -> bool:
        return self.kind in ("pass", "pass_loose")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "gold_rows": self.gold_rows,
            "pred_rows": self.pred_rows,
            "column_permutation": list(self.column_permutation) if self.column_permutation else None,
            "degenerate": self.degenerate,
        }


def _multiset_eq(
    gold: list[tuple[Cell, ...]],
    pred: list[tuple[Cell, ...]],
    tol: bool,
    kinds: Sequence[str | None] | None = None,
) -> bool:
    """行序无关的多重集比较。先走 O(n) 精确路径，不匹配再退 O(n^2) 带容差匹配。"""
    if len(gold) != len(pred):
        return False
    if not gold:
        return True

    if sorted(map(lambda r: tuple(c.key() for c in r), gold)) == sorted(map(lambda r: tuple(c.key() for c in r), pred)):
        return True

    if not tol or len(gold) > MAX_ROWS_FOR_TOLERANCE:
        return False

    used = [False] * len(pred)
    for g in gold:
        hit = -1
        for i, p in enumerate(pred):
            if not used[i] and row_eq(g, p, tol=True, kinds=kinds):
                hit = i
                break
        if hit < 0:
            return False
        used[hit] = True
    return True


def _ordered_eq(
    gold: list[tuple[Cell, ...]],
    pred: list[tuple[Cell, ...]],
    tol: bool,
    kinds: Sequence[str | None] | None = None,
) -> bool:
    """行序敏感的比较：逐位置相等（列序仍要求一致）。"""
    if len(gold) != len(pred):
        return False
    return all(row_eq(g, p, tol=tol, kinds=kinds) for g, p in zip(gold, pred))


def compare_resultsets(
    gold_rows: Iterable[Sequence[Any]],
    pred_rows: Iterable[Sequence[Any]],
    *,
    gold_error: str | None = None,
    pred_error: str | None = None,
    order_matters: bool = False,
    column_types: Sequence[str] | None = None,
) -> Verdict:
    """核心判定。gold_error / pred_error 分别表示该侧执行报错。

    `order_matters`：行序是否参与判定。调用方按 `gold_sql` 是否含 `ORDER BY` 传入
    （见 `gold_sql_has_order_by()`）。默认 `False` = 行序无关，与历史行为一致；
    为 `True` 时列排列（pass_loose）不再尝试——列序变了行序判定就失去意义。

    `column_types`：**按列**给出结果列语义（取自用例的 `column_types` 字段，取值见
    `COLUMN_KINDS`）。长度必须等于结果列数，否则视为无效标注、退化为无列语义
    （用例层由 `validate_cases.py` 拦长度不符，这里再兜一道，保证判定器不会因此崩）。
    只有 "money" 会改变容差行为（绝对 1 分钱，口径 2.3）。
    """
    g_src = [list(r) for r in gold_rows]
    gold = [tuple(as_cell(v) for v in r) for r in g_src]

    # 报错优先于结果比较：SQL 报错不能用「两边都是空」蒙过去
    if pred_error:
        return Verdict("error", f"预测 SQL 执行报错：{pred_error}", gold_rows=len(gold), pred_rows=None)
    if gold_error:
        return Verdict("error", f"标准 SQL 执行报错（用例本身有问题，先修用例）：{gold_error}", gold_rows=len(gold))

    p_src = [list(r) for r in pred_rows]
    pred = [tuple(as_cell(v) for v in r) for r in p_src]

    if not gold:
        if not pred:
            return Verdict(
                "pass",
                "两侧均为空集。注意：该用例无区分度，应替换（任务书风险清单第 1 条）",
                gold_rows=0,
                pred_rows=0,
                degenerate=True,
            )
        return Verdict("fail", f"标准结果为空，预测返回 {len(pred)} 行", gold_rows=0, pred_rows=len(pred))

    n_cols_gold = len(gold[0])
    n_cols_pred = len(pred[0]) if pred else 0
    if pred and n_cols_pred != n_cols_gold:
        return Verdict(
            "fail",
            f"列数不同：标准 {n_cols_gold} 列，预测 {n_cols_pred} 列",
            gold_rows=len(gold),
            pred_rows=len(pred),
        )
    if len(pred) != len(gold):
        return Verdict(
            "fail",
            f"行数不同：标准 {len(gold)} 行，预测 {len(pred)} 行",
            gold_rows=len(gold),
            pred_rows=len(pred),
        )

    # 列语义标注：长度不符即视为无效（用例错误应被 validate_cases 提前拦下，这里只做兜底）
    kinds: list[str | None] | None = None
    if column_types:
        if len(column_types) == n_cols_gold:
            kinds = list(column_types)
        else:
            kinds = None

    # ---- 行序敏感分支：gold_sql 含 ORDER BY 时，顺序本身就是答案的一部分 ----
    if order_matters:
        if _ordered_eq(gold, pred, tol=True, kinds=kinds):
            return Verdict(
                "pass",
                "列序一致 + 行序按 ORDER BY 比对一致 + 数值容差内一致",
                gold_rows=len(gold),
                pred_rows=len(pred),
            )
        if _multiset_eq(gold, pred, tol=True, kinds=kinds):
            # 内容对、顺序错 —— 含 ORDER BY 时这就是错；但要能一眼定位是哪一种错
            return Verdict(
                "fail",
                "行序敏感（gold_sql 含 ORDER BY）但顺序不一致：结果集内容相同、顺序不同",
                gold_rows=len(gold),
                pred_rows=len(pred),
            )
        return Verdict(
            "fail",
            "行序敏感（gold_sql 含 ORDER BY）且结果集内容也不一致",
            gold_rows=len(gold),
            pred_rows=len(pred),
        )

    # 严格：列序敏感
    if _multiset_eq(gold, pred, tol=True, kinds=kinds):
        return Verdict("pass", "列序一致 + 行序无关 + 数值容差内一致", gold_rows=len(gold), pred_rows=len(pred))

    # 宽松：允许列顺序不同
    if n_cols_gold <= MAX_COLS_FOR_PERMUTATION and len(gold) <= MAX_ROWS_FOR_PERMUTATION:
        for perm in itertools.permutations(range(n_cols_gold)):
            if perm == tuple(range(n_cols_gold)):
                continue
            permuted = [tuple(r[i] for i in perm) for r in gold]
            # 列被重排后，列语义标注必须**跟着重排**——否则容差张冠李戴
            # （把金额列的内容当文本列、把文本列当金额列去比）。
            permuted_kinds = [kinds[i] for i in perm] if kinds else None
            if _multiset_eq(permuted, pred, tol=True, kinds=permuted_kinds):
                return Verdict(
                    "pass_loose",
                    "仅列顺序不同，按列序无关口径判为通过（pass_loose，需人工确认不是巧合）",
                    gold_rows=len(gold),
                    pred_rows=len(pred),
                    column_permutation=perm,
                )
    else:
        return Verdict(
            "fail",
            "与标准结果不一致；结果集过大已跳过列排列尝试，只按列序敏感口径判定",
            gold_rows=len(gold),
            pred_rows=len(pred),
        )

    # 形状一致但值不同：给出第一处差异，让跑分报告能直接定位，而不是只报一个"不一致"
    gold_keys = sorted(tuple(c.key() for c in r) for r in gold)
    pred_keys = sorted(tuple(c.key() for c in r) for r in pred)
    detail = ""
    for a, b in zip(gold_keys, pred_keys):
        if a != b:
            detail = f"；首处差异 标准={list(a)[:4]} 预测={list(b)[:4]}"
            break
    return Verdict(
        "fail",
        f"结果集数值不一致（形状相同，{len(gold)} 行 × {n_cols_gold} 列）{detail}",
        gold_rows=len(gold),
        pred_rows=len(pred),
    )


# --------------------------------------------------------------------------------------
# 指标聚合与档位换算
# --------------------------------------------------------------------------------------
# 初赛评分表原文档位（性能指标 1 / 2）：≥70% 得 6 分，≥80% 得 8 分，≥90% 得 10 分
ACCURACY_BANDS = ((0.90, 10), (0.80, 8), (0.70, 6))


def accuracy_band(rate: float, bands: Sequence[tuple[float, int]] = ACCURACY_BANDS) -> dict:
    """把准确率换成评分表上的档位。三档全算出来，不是只报一个总数。"""
    result = {"rate": round(rate, 4)}
    for threshold, score in bands:
        key = f"ge_{int(threshold * 100)}"
        result[key] = {"score": score if rate >= threshold else 0, "reached": rate >= threshold}
    return result


@dataclass
class Tally:
    total: int = 0
    passed: int = 0
    passed_strict: int = 0
    passed_loose: int = 0
    failed: int = 0
    errored: int = 0
    cases: list[dict] = field(default_factory=list)

    def add(self, case_id: str, verdict: Verdict, extra: dict | None = None) -> None:
        self.total += 1
        if verdict.kind == "pass":
            self.passed += 1
            self.passed_strict += 1
        elif verdict.kind == "pass_loose":
            self.passed += 1  # 主口径把 pass_loose 记为通过
            self.passed_loose += 1
        elif verdict.kind == "error":
            self.errored += 1
        else:
            self.failed += 1
        row = {"id": case_id, **verdict.to_dict()}
        if extra:
            row.update(extra)
        self.cases.append(row)

    @property
    def accuracy(self) -> float:
        """主口径：通过（含 pass_loose） / 全部用例。报错计入分母，不剔除。"""
        return self.passed / self.total if self.total else 0.0

    @property
    def strict_accuracy(self) -> float:
        """严格口径：只有 pass 才计通过。与主口径的差就是 pass_loose 的数量，用于暴露假阳性。"""
        return self.passed_strict / self.total if self.total else 0.0

    def summary(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "passed_strict": self.passed_strict,
            "passed_loose_only": self.passed_loose,
            "failed": self.failed,
            "errored": self.errored,
            "accuracy": round(self.accuracy, 4),
            "strict_accuracy": round(self.strict_accuracy, 4),
            "error_rate": round(self.errored / self.total, 4) if self.total else 0.0,
            "bands": accuracy_band(self.accuracy),
        }


def macro_average(tallies: dict[str, Tally]) -> float:
    """多子集等权平均。用于「单表子集」与「多表子集」各自算完再汇总，避免大子集吞掉小子集。"""
    if not tallies:
        return 0.0
    return sum(t.accuracy for t in tallies.values()) / len(tallies)
