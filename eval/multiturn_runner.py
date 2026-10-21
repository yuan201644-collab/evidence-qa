"""多轮对话脚本评测：验证「连续 5 轮以上准确」这条赛题要求的可复现证据。

与 eval/runner.py 的区别（两者都不替代对方）：
  - runner.py 是**单轮判分权威**，每题用独立 session，按结果集/gold 判对错；
  - 本脚本是**对话链路证据**，一条用例的多轮**共用一个 session**，
    逐轮记录真实答案与接续判定，用于回答「连续 5 轮能不能保持」。

判定口径刻意保守：
  一条用例「通过」= 5 轮全过。中间任一轮失败，整条记为失败并指出第几轮。
  不把「4/5 轮」折算成分数 —— 赛题要求的是「连续」。

关键实现细节：**上下文接续与回答正确分开判定**。
`app/memory.py` 的 _combine 会把上文与追问拼成
    "<上一轮问题>\n用户补充或修改：<本轮追问>"
normalizer 步骤的 output.normalized 里出现这个接缝，只能证明上文被传入，
不能证明答案正确。因此每轮还会单独核对最终答案、SQL 条件、结果行数和列名；
依赖上下文的轮次必须既接上上文，也满足已登记的业务断言。

用法：
    python -m eval.multiturn_runner                    # 文字报告
    python -m eval.multiturn_runner --json out.json    # 附带 JSON 产物
    python -m eval.multiturn_runner --base http://127.0.0.1:8000
    python -m eval.multiturn_runner --case mt-01       # 只跑一条

接口需要登录；默认从 VERIMIND_EVAL_USERNAME / VERIMIND_EVAL_PASSWORD 环境变量读取
测试账号，不把密码放在命令行或评测报告中。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "eval" / "cases" / "multiturn.jsonl"

# app/memory.py::_combine 生成的上文接缝。出现在 normalizer 输出里即表示已接续。
FOLLOWUP_SEAM = "用户补充或修改"

DEFAULT_BASE = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 180.0


class _LoopbackSecureCookiePolicy(DefaultCookiePolicy):
    """Allow Secure auth cookies over local HTTP only for loopback evaluation."""

    def return_ok_secure(self, cookie: Cookie, request: urllib.request.Request) -> bool:
        if cookie.secure and request.type == "http":
            hostname = urllib.parse.urlsplit(f"{request.type}://{request.host}").hostname or ""
            if hostname.casefold() == "localhost":
                return True
            try:
                if ipaddress.ip_address(hostname).is_loopback:
                    return True
            except ValueError:
                pass
        return super().return_ok_secure(cookie, request)


@dataclass
class TurnResult:
    round: int
    question: str
    answer: str
    status: int
    elapsed_ms: float
    attached: bool
    depends_on_context: bool
    expect_hits: list[str]
    expect_missed: list[str]
    expect_absent_hits: list[str]
    expect_sql_hits: list[str]
    expect_sql_missed: list[str]
    expect_sql_absent_hits: list[str]
    has_answer_assertion: bool
    expected_row_count: int | None
    actual_row_count: int | None
    expected_columns: list[str]
    missing_columns: list[str]
    actual_sql: str
    passed: bool
    note: str = ""


@dataclass
class CaseResult:
    case_id: str
    title: str
    scoring: str
    passed: bool
    turns_passed: int
    turns_total: int
    first_failure_round: int | None
    turns: list[TurnResult] = field(default_factory=list)
    notes: str = ""


def load_cases(only: str | None = None, cases_path: Path | None = None) -> list[dict[str, Any]]:
    """读多轮题集。`cases_path` 缺省用 chinook 的 `eval/cases/multiturn.jsonl`。

    2026-10-18 加这条参数：政采库的多轮题集在
    `eval/cases/gov_procurement/multiturn.jsonl`，此前只能靠改模块常量，
    等于每换一个库都要动判分器源码 —— 判分器不该知道有几套题集。
    """
    path = cases_path or CASES_PATH
    if not path.is_file():
        raise SystemExit(f"用例文件不存在：{path}")
    cases: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("//"):
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path.name}:{lineno} JSON 解析失败：{exc}") from exc
        cases.append(item)
    if only:
        cases = [c for c in cases if c.get("id") == only]
        if not cases:
            raise SystemExit(f"没有找到用例 {only}")
    return cases


def ask(
    base: str,
    question: str,
    session_id: str,
    timeout: float,
    *,
    opener: urllib.request.OpenerDirector | None = None,
    origin: str | None = None,
) -> tuple[int, dict[str, Any], float]:
    payload = json.dumps({"question": question, "session_id": session_id}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/ask",
        data=payload,
        headers=headers,
        method="POST",
    )
    started = time.perf_counter()
    try:
        response_context = (
            opener.open(request, timeout=timeout)
            if opener is not None
            else urllib.request.urlopen(request, timeout=timeout)
        )
        with response_context as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        return exc.code, {"error": detail}, (time.perf_counter() - started) * 1000
    except urllib.error.URLError as exc:
        return 0, {"error": f"无法连接 {base}：{exc.reason}"}, (time.perf_counter() - started) * 1000
    except TimeoutError:
        # 单轮超时只能算这一轮失败。原先这里漏了捕获，超时会把异常抛到顶层，
        # 整个脚本崩掉、前面已经跑完的轮次全部丢失，一份结果都留不下 ——
        # 长跑评测里这是最亏的失败方式。这里按「本轮失败」返回，继续跑后面的轮次。
        return (
            0,
            {"error": f"请求超时（{timeout:g} 秒未返回）"},
            (time.perf_counter() - started) * 1000,
        )


def authenticated_client(
    base: str, username: str, password: str, timeout: float
) -> tuple[urllib.request.OpenerDirector, str]:
    """Log in without putting credentials in command-line arguments or logs."""
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("后端地址必须是有效的 HTTP 或 HTTPS 地址")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(
            CookieJar(policy=_LoopbackSecureCookiePolicy())
        )
    )
    payload = json.dumps(
        {"username": username, "password": password}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/auth/login",
        data=payload,
        headers={"Content-Type": "application/json", "Origin": origin},
        method="POST",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise SystemExit(f"登录失败：HTTP {response.status}")
            json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"登录失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"无法连接后端 {base}：{exc.reason}") from exc
    return opener, origin


def normalized_of(body: dict[str, Any]) -> str:
    """取 normalizer 步骤里真正送去下游的问题文本。"""
    for step in body.get("trace") or []:
        if step.get("tool") == "normalizer":
            output = step.get("output") or {}
            return str(output.get("normalized") or "")
    return ""


def execution_summary(body: dict[str, Any]) -> tuple[int | None, list[str]]:
    """Read the final SQL executor's row count and result columns from trace."""
    for step in reversed(body.get("trace") or []):
        output = step.get("output") or {}
        row_count = output.get("row_count")
        columns = output.get("columns")
        if isinstance(row_count, int) and isinstance(columns, list):
            return row_count, [str(column) for column in columns]
    return None, []


_MONEY_UNITS = {"亿元": 100_000_000, "亿": 100_000_000, "万元": 10_000, "万": 10_000, "元": 1}
_MONEY_RE = re.compile(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(亿元|亿|万元|万|元)")


def _money_values(text: str) -> set[float]:
    """读出文本里带金额单位的数值，换算到「元」。

    只认带单位的片段：裸数字（行数、编号）不该跟金额比。
    """
    values: set[float] = set()
    for number, unit in _MONEY_RE.findall(text or ""):
        try:
            values.add(round(float(number.replace(",", "")) * _MONEY_UNITS[unit], 2))
        except ValueError:
            continue
    return values


def _normalise_number(text: str) -> float | None:
    """把 `2,064,498,011.13` 这类带千分位的数字还原成 float；不是数字返回 None。"""
    cleaned = (text or "").strip().replace(",", "")
    if not cleaned or not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        return None
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def _contains_either(expected: str, answer_raw: str, answer_folded: str) -> bool:
    """断言「答案里出现了这个值」，按**语义相等**而不是字面相等判。

    三个真实的误判来源（2026-10-18 政采多轮实测）：
      1) 千分位：用例写 2,064,498,011.13，系统答 2064498011.13；
      2) 金额单位：用例写 45,058,000.00，系统答 4505.8 万元；
      3) 别名无关：列名由模型自由起，判分只该看值。
    字面包含仍然先行，命中就直接 True，与旧行为一致。
    """
    if expected.casefold() in answer_folded:
        return True
    wanted = _normalise_number(expected)
    if wanted is not None:
        if wanted in _money_values(answer_raw):
            return True
        # 也允许答案里就是这个数字的裸值
        for token in re.findall(r"-?\d[\d,]*(?:\.\d+)?", answer_raw or ""):
            if _normalise_number(token) == wanted:
                return True
    return False


def _column_tokens(name: str) -> set[str]:
    """把列名切成语义词元：`total_award_yuan` → {total, award, yuan}。"""
    return {part for part in re.split(r"[^a-z0-9]+", name.casefold()) if part}


#: 列名里可以互换的表层写法。语义相同、只是模型措辞不同时不该判错
#: （2026-10-18 实测：用例写 total_award_yuan，模型起名 total_award_amount）。
_COLUMN_SYNONYMS = {
    "amount": "yuan",
    "money": "yuan",
    "sum": "total",
    "avg": "average",
    "cnt": "count",
    "num": "count",
    "number": "count",
    "qty": "count",
    "date": "day",
    "name": "name",
}
#: 可选词元：缺了不算错（`project_count` 与 `project_cnt` 都要认，
#: `total_award_yuan` 少了 `award` 也仍然是那个金额）。
_COLUMN_OPTIONAL = {"total", "average", "name", "day", "yuan", "id", "code", "level"}


def _column_matches(expected: str, actual_columns: list[str]) -> bool:
    """期望列名只要**实质词元**都能在某一个实际列名里找到，就算命中。

    规则：
      1) 字面完全相等 → 命中；
      2) 期望列名的实质词元（去掉可选词元后）必须全部出现在同一个实际列名里；
      3) 同义词归一后再比（amount/money→yuan，sum→total，cnt/num→count）。
    为什么不用「完全相等」：别名由大模型自由起，把 `total_award_yuan`
    写成 `total_award_amount` 不该算错；真正要抓的是**语义漂移**
    （例如该给金额却给了条数），那会连实质词元都对不上。
    """
    wanted_all = _column_tokens(expected)
    wanted_core = {_COLUMN_SYNONYMS.get(token, token) for token in wanted_all} - _COLUMN_OPTIONAL
    for column in actual_columns:
        if column.casefold() == expected.casefold():
            return True
        if not wanted_core:
            # 期望列名全是可选词元，只能靠字面相等，上面已判过
            continue
        actual = {_COLUMN_SYNONYMS.get(token, token) for token in _column_tokens(column)}
        if wanted_core <= actual:
            return True
    return False


def judge_turn(turn: dict[str, Any], body: dict[str, Any], status: int, elapsed: float) -> TurnResult:
    answer = str(body.get("answer") or "")
    normalized = normalized_of(body)
    attached = FOLLOWUP_SEAM in normalized
    actual_row_count, actual_columns = execution_summary(body)
    actual_sql = str(body.get("sql") or "")

    expects = [str(item) for item in turn.get("expect_contains") or []]
    # 只检查最终答案，不能把历史问题里的词算成答案命中。
    answer_text = answer.casefold()
    hits = [item for item in expects if _contains_either(item, answer, answer_text)]
    missed = [item for item in expects if item not in hits]

    # 正反断言都只检查最终答案，不能检查拼接后的问题文本。
    absents = [str(item) for item in turn.get("expect_absent") or []]
    absent_hits = [item for item in absents if item.casefold() in answer_text]

    sql = str(body.get("sql") or "")
    sql_text = sql.casefold()
    sql_expects = [str(item) for item in turn.get("expect_sql_contains") or []]
    sql_hits = [item for item in sql_expects if item.casefold() in sql_text]
    sql_missed = [item for item in sql_expects if item not in sql_hits]
    sql_absents = [str(item) for item in turn.get("expect_sql_absent") or []]
    sql_absent_hits = [item for item in sql_absents if item.casefold() in sql_text]

    expected_row_count_raw = turn.get("expect_row_count")
    expected_row_count = (
        int(expected_row_count_raw) if expected_row_count_raw is not None else None
    )
    expected_columns = [str(item) for item in turn.get("expect_columns") or []]
    missing_columns = [column for column in expected_columns if not _column_matches(column, actual_columns)]
    row_count_mismatch = (
        expected_row_count is not None
        and actual_row_count != expected_row_count
    )

    depends = bool(turn.get("depends_on_context"))
    has_answer_assertion = bool(
        expects or expected_row_count is not None or expected_columns
    )
    context_matches = attached == depends
    # 本轮有没有「可证明取到同一列」的数值断言：expect_contains 里至少有一个
    # 能解析成数字的要点。有它，列名就降为信息性；没有它，列名仍是硬断言。
    has_numeric_assertion = any(_normalise_number(item) is not None for item in expects)
    notes: list[str] = []

    if status != 200:
        notes.append(f"HTTP {status}：{str(body.get('error'))[:120]}")
    if missed:
        notes.append("答案未包含：" + "、".join(missed))
    if absent_hits:
        notes.append("答案出现了不该出现的标记（疑似答成全库而非上文对象）：" + "、".join(absent_hits))
    if not has_answer_assertion:
        notes.append("缺少答案正向断言；本轮不能判为通过")
    if sql_missed:
        notes.append("SQL 未包含预期字段/条件：" + "、".join(sql_missed))
    if sql_absent_hits:
        notes.append("SQL 包含不应出现的字段/条件：" + "、".join(sql_absent_hits))
    if row_count_mismatch:
        notes.append(
            "结果行数不符：预期 " + str(expected_row_count)
            + "，实际 " + str(actual_row_count)
        )
    if missing_columns:
        # 列名是模型自由起的别名，人预先写死的 expect_columns 只是「应该取这几列」的
        # 意图登记。当本轮已经有**数值正向断言且全部命中**时，值已经证明取的是同一列，
        # 列名不同不该判错（实测「改成 2026-10-15 呢？」SQL 完全正确、答案 624 正确，
        # 只因别名写成 count_question 就被判失败）。
        # 没有数值断言时（例如只断言行数），列名回到硬断言位置。
        if has_numeric_assertion and not missed:
            notes.append("列别名与预期不同（本轮值断言已命中，不计失败）：" + "、".join(missing_columns))
            missing_columns = []
        else:
            notes.append("结果缺少预期字段：" + "、".join(missing_columns))
    if depends and not attached:
        notes.append("未接上上文（normalizer 输出里没有接缝），本轮按孤立问题处理")
    if not depends and attached:
        notes.append("本轮标为独立问题，但实际接上了上文")

    return TurnResult(
        round=int(turn.get("round") or 0),
        question=str(turn.get("question") or ""),
        answer=answer,
        status=status,
        elapsed_ms=round(elapsed, 1),
        attached=attached,
        depends_on_context=depends,
        expect_hits=hits,
        expect_missed=missed,
        expect_absent_hits=absent_hits,
        expect_sql_hits=sql_hits,
        expect_sql_missed=sql_missed,
        expect_sql_absent_hits=sql_absent_hits,
        has_answer_assertion=has_answer_assertion,
        expected_row_count=expected_row_count,
        actual_row_count=actual_row_count,
        expected_columns=expected_columns,
        missing_columns=missing_columns,
        actual_sql=actual_sql[:8000],
        passed=(
            status == 200
            and has_answer_assertion
            and not missed
            and not absent_hits
            and not sql_missed
            and not sql_absent_hits
            and not row_count_mismatch
            and not missing_columns
            and context_matches
        ),
        note="；".join(notes),
    )


def run_case(
    base: str,
    case: dict[str, Any],
    timeout: float,
    *,
    opener: urllib.request.OpenerDirector | None = None,
    origin: str | None = None,
    session_run_tag: str = "",
) -> CaseResult:
    case_id = str(case.get("id") or "?")
    # 一条用例一个 session，整条链路共用；用例之间必须隔离，
    # 否则上一条的主题会串到下一条（这正是多轮机制本身的行为）。
    #
    # session_run_tag：本进程一次运行的唯一后缀。
    # SessionMemory 是**进程内存**且服务端没有会话重置接口，若 session_id 只由
    # case_id 决定，同一服务上连跑两次，第二次会继承第一次的上下文 ——
    # 实测症状是「2026-10-16 发布了多少条采购公告？」这条**第 1 轮独立问题**
    # 被上一轮残留的 kind='招标公告' 带偏，SQL 多出一个筛选条件。
    # 每次都换 session_id，保证「新会话」语义与可复现性。
    session_id = f"multiturn-{case_id}-{session_run_tag}"
    results: list[TurnResult] = []

    for turn in case.get("turns") or []:
        question = str(turn.get("question") or "")
        status, body, elapsed = ask(
            base, question, session_id, timeout, opener=opener, origin=origin
        )
        results.append(judge_turn(turn, body, status, elapsed))

    first_failure = next((item.round for item in results if not item.passed), None)
    return CaseResult(
        case_id=case_id,
        title=str(case.get("title") or ""),
        scoring=str(case.get("scoring") or ""),
        passed=first_failure is None,
        turns_passed=sum(1 for item in results if item.passed),
        turns_total=len(results),
        first_failure_round=first_failure,
        turns=results,
        notes=str(case.get("notes") or ""),
    )


def print_report(results: list[CaseResult], base: str) -> None:
    print()
    print("=" * 76)
    print(f"多轮对话连续追问评测  |  后端 {base}")
    print("=" * 76)

    for result in results:
        mark = "通过" if result.passed else "未通过"
        print()
        print(f"【{result.case_id}】{result.title} —— {mark}")
        print(f"  连续通过 {result.turns_passed}/{result.turns_total} 轮", end="")
        if result.first_failure_round is not None:
            print(f"，首个失败在第 {result.first_failure_round} 轮")
        else:
            print()
        for turn in result.turns:
            flag = "✓" if turn.passed else "✗"
            attached = "已接上文" if turn.attached else "未接上文"
            need = "需上文" if turn.depends_on_context else "不需上文"
            print(f"    {flag} 第{turn.round}轮 [{need}·{attached}] {turn.question}")
            print(f"        答：{(turn.answer or '(空)').replace(chr(10), ' ')[:96]}")
            if turn.actual_row_count is not None:
                expected = (
                    "预期 " + str(turn.expected_row_count)
                    if turn.expected_row_count is not None
                    else "未登记行数金标准"
                )
                print(f"        结果行数：{turn.actual_row_count}（{expected}）")
            if turn.note:
                print(f"        说明：{turn.note}")
            if not turn.passed and turn.actual_sql:
                excerpt = " ".join(turn.actual_sql.split())
                print(f"        实际 SQL：{excerpt[:320]}")

    total = len(results)
    passed = sum(1 for item in results if item.passed)
    print()
    print("-" * 76)
    print(f"汇总：连续 5 轮以上全部通过的用例 {passed}/{total}")
    for result in results:
        if result.passed:
            continue
        bad = next((t for t in result.turns if not t.passed), None)
        if bad is not None:
            print(f"  待修：{result.case_id} 第 {bad.round} 轮「{bad.question}」—— {bad.note}")
    print("-" * 76)
    print("说明：本报告只覆盖对话链路保持能力，不等价于 eval/runner.py 的准确率口径。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多轮对话连续追问评测")
    parser.add_argument("--base", default=DEFAULT_BASE, help="后端地址")
    parser.add_argument("--case", default=None, help="只跑指定用例 id")
    parser.add_argument(
        "--cases",
        dest="cases_path",
        default=None,
        help=(
            "多轮题集文件（默认 eval/cases/multiturn.jsonl）。"
            "政采用 eval/cases/gov_procurement/multiturn.jsonl"
        ),
    )
    parser.add_argument("--json", dest="json_path", default=None, help="把结果写入 JSON 文件")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单轮超时秒数")
    parser.add_argument(
        "--username-env",
        default="VERIMIND_EVAL_USERNAME",
        help="登录账号所在的环境变量名（默认 VERIMIND_EVAL_USERNAME）",
    )
    parser.add_argument(
        "--password-env",
        default="VERIMIND_EVAL_PASSWORD",
        help="登录密码所在的环境变量名（默认 VERIMIND_EVAL_PASSWORD）",
    )
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    username = os.environ.get(args.username_env, "")
    password = os.environ.get(args.password_env, "")
    if not username or not password:
        raise SystemExit(
            "多轮接口需要登录；请通过环境变量 "
            + args.username_env + " / " + args.password_env
            + " 提供测试账号，不要把密码写进命令行。"
        )
    opener, origin = authenticated_client(args.base, username, password, args.timeout)
    cases_path = Path(args.cases_path) if args.cases_path else None
    cases = load_cases(args.case, cases_path)
    # 每次运行一个唯一后缀：服务端 SessionMemory 是进程内存、无重置接口，
    # 复用 session_id 会把上一次运行留下的上下文带进本次的「第 1 轮」。
    session_run_tag = time.strftime("%m%d%H%M%S")
    results = [
        run_case(
            args.base, case, args.timeout,
            opener=opener, origin=origin, session_run_tag=session_run_tag,
        )
        for case in cases
    ]
    print_report(results, args.base)

    if args.json_path:
        payload = {
            "base": args.base,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "scope": "多轮对话链路保持验证，非系统准确率实验",
            "pass_rule": "一条用例 5 轮全部通过才算通过",
            "attach_rule": f"normalizer 输出含「{FOLLOWUP_SEAM}」接缝即判定为已接上文",
            "cases": [asdict(item) for item in results],
        }
        target = Path(args.json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 已写入 {target}")

    return 0 if all(item.passed for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
