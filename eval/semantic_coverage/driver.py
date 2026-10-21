"""S2-H 语义覆盖采集：把 SC 场景的真实请求/响应原样落盘，供 scorer 独立打分。

与 eval/multiturn_runner.py 的分工：那个是「多轮链路保持」证据工具（自带判分）；
本脚本**只采集、不打分**——判据（rubric.jsonl）与采集（本脚本）严格分离，
scorer.py 独立读取原始响应打分，防止采集方按判据挑最好的一次（母方案 :563-571）。

行为硬要求（S2 计划书 §10.2 / 母方案 :563-571）：
  1. --cases 只读题面（questions.jsonl），不读判据、Gold 或历史答案；
  2. 输出目录已有结果时拒绝覆盖；复跑另建运行号；
  3. 凭据走环境变量（--username-env/--password-env），不进命令行与日志；
  4. 每个场景独立会话；多轮场景保持本场景会话；
  5. 完整保存原始响应、异常、延迟与失败次数，不挑最好一次；
  6. 超时先登记未完成状态，不自动重放原 POST；
  7. 保存提交号、源码差异哈希、数据库指纹；采集进程环境不冒称服务实际配置——
     服务进程的闸门档位由采集方通过 /health 与显式 --gate-mode 声明留痕；
  8. 日志不保存密码、会话 cookie、令牌或含凭据连接串。

用法：
    python -m eval.semantic_coverage.driver --base http://127.0.0.1:8000 \
        --username-env VERIMIND_EVAL_USERNAME --password-env VERIMIND_EVAL_PASSWORD \
        --out-dir eval/reports/semantic_coverage/<运行号>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval.multiturn_runner import authenticated_client  # 复用会话与 Origin 处理（§10.2 :3）

CASES_DIR = ROOT / "eval" / "semantic_coverage" / "development_v0.1"
QUESTIONS_PATH = CASES_DIR / "questions.jsonl"
CONFIG_PATH = CASES_DIR / "collection_config.json"

RESULT_FILES = ("raw_responses.jsonl", "case_results.jsonl", "runtime_manifest.json")


def load_questions(only: str | None = None) -> list[dict[str, Any]]:
    """只读题面。读到的文件里不允许出现 rubric 字段（防串台）。"""
    if not QUESTIONS_PATH.is_file():
        raise SystemExit(f"题面文件不存在：{QUESTIONS_PATH}")
    cases: list[dict[str, Any]] = []
    for lineno, line in enumerate(QUESTIONS_PATH.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        item = json.loads(text)
        if "rubric" in item or "expect" in json.dumps(item):
            raise SystemExit(f"{QUESTIONS_PATH.name}:{lineno} 题面文件不允许携带判据字段")
        cases.append(item)
    if only:
        cases = [c for c in cases if c.get("id") == only]
        if not cases:
            raise SystemExit(f"没有找到场景 {only}")
    return cases


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_snapshot() -> dict[str, Any]:
    """提交号与源码差异哈希。不读工作区里任何业务内容进记录。"""
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout.strip()

    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    dirty_sha = hashlib.sha256(status.encode("utf-8")).hexdigest() if status else ""
    return {
        "commit": head,
        "worktree_dirty": bool(status),
        "dirty_status_sha256": dirty_sha,
        "dirty_status_lines": len(status.splitlines()),
    }


def database_fingerprints(config: dict[str, Any]) -> dict[str, Any]:
    """采集隔离样本库与主库指纹。失败如实登记，不阻塞采集。"""
    result: dict[str, Any] = {}
    try:
        from eval.mainline.fingerprint_postgres import fingerprint_database

        dsn_env = os.environ.get("SEMANTIC_COVERAGE_DSN", "")
        if dsn_env:
            result["semantic_coverage_fixture"] = fingerprint_database(dsn_env, scope="database")
        chinook_dsn = os.environ.get("DATABASE_URL", "")
        if chinook_dsn:
            result["chinook"] = fingerprint_database(chinook_dsn, scope="business")
    except Exception as exc:  # noqa: BLE001 —— 指纹失败如实留痕，不让采集整包崩
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def service_probe(base: str, timeout: float) -> dict[str, Any]:
    """/health 与闸门档位探测。档位以服务进程 /health 返回为准——
    采集进程自己的环境变量不代表服务实际配置（§10.2 :7）。"""
    probe: dict[str, Any] = {}
    try:
        with urlrequest.urlopen(f"{base.rstrip('/')}/health", timeout=timeout) as resp:
            probe["health"] = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        probe["health_error"] = f"{type(exc).__name__}: {exc}"
    # 服务端不暴露闸门档位时显式缺省，绝不用采集进程环境冒称。
    probe["gate_mode"] = os.environ.get("SEMANTIC_COVERAGE_REPORTED_GATE", "unreported")
    return probe


def ask_once(
    base: str,
    question: str,
    session_id: str,
    timeout: float,
    opener: urlrequest.OpenerDirector | None,
    origin: str | None,
) -> dict[str, Any]:
    """单次请求，完整登记。参照 multiturn_runner.ask 的单轮失败语义。"""
    record: dict[str, Any] = {
        "question": question,
        "session_id": session_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    payload = json.dumps({"question": question, "session_id": session_id}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        # 协商核验卡字段（app/main.py:347 _requested_fact_review）：不带此头
        # 响应不含 fact_review，SC-05/08/12 的 sql_requirement_coverage 判据
        # 就没有数据来源。首轮采集（run1）漏了此头，核验卡字段未验证。
        "x-askdata-fact-review": "2",
    }
    if origin:
        headers["Origin"] = origin
    request = urlrequest.Request(
        f"{base.rstrip('/')}/api/ask", data=payload, headers=headers, method="POST"
    )
    started = time.perf_counter()
    try:
        context = (
            opener.open(request, timeout=timeout)
            if opener is not None
            else urlrequest.urlopen(request, timeout=timeout)
        )
        with context as response:
            record["status"] = response.status
            record["body"] = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        record["status"] = exc.code
        record["body"] = {"error": exc.read().decode("utf-8", "replace")}
    except urlerror.URLError as exc:
        record["status"] = 0
        record["error"] = f"无法连接 {base}：{exc.reason}"
    except TimeoutError:
        # 超时只登记未完成，不自动重放（§10.2 :6）。
        record["status"] = 0
        record["error"] = f"请求超时（{timeout:g} 秒未返回）；登记为未完成，未自动重放"
    except Exception as exc:  # noqa: BLE001
        record["status"] = 0
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return record


def run_scenario(
    case: dict[str, Any], args: argparse.Namespace,
    opener: urlrequest.OpenerDirector | None, origin: str | None,
) -> dict[str, Any]:
    """一个场景一组请求。多轮场景（turns>1）共用本场景会话（§10.2 :4）。"""
    case_id = str(case.get("id") or "?")
    session_id = f"{args.session_prefix}-{case_id}"
    turns_out = [
        ask_once(args.base, str(turn.get("question") or ""), session_id,
                 args.timeout, opener, origin)
        for turn in (case.get("turns") or [])
    ]
    attempts = sum(1 for t in turns_out if t.get("status") == 200)
    return {
        "id": case_id,
        "dataset": case.get("dataset") or "chinook",
        "session_id": session_id,
        "turns": turns_out,
        "completed_turns": attempts,
        "total_turns": len(turns_out),
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("SEMANTIC_COVERAGE_BASE", "http://127.0.0.1:8000"))
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--username-env", default="VERIMIND_EVAL_USERNAME")
    parser.add_argument("--password-env", default="VERIMIND_EVAL_PASSWORD")
    parser.add_argument("--only", default=None, help="只跑一个场景（冒烟用）")
    parser.add_argument("--skip-auth", action="store_true", help="服务未开登录门时跳过登录")
    parser.add_argument("--session-prefix", default="semcov")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    if out_dir.exists() and any((out_dir / name).exists() for name in RESULT_FILES):
        existing = ", ".join(name for name in RESULT_FILES if (out_dir / name).exists())
        raise SystemExit(
            f"输出目录 {out_dir} 已有结果（{existing}）。"
            "拒绝覆盖：请另建运行号目录（§10.2 :2）。"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    cases = load_questions(args.only)
    opener: urlrequest.OpenerDirector | None = None
    origin: str | None = None
    if not args.skip_auth:
        username = os.environ.get(args.username_env, "")
        password = os.environ.get(args.password_env, "")
        if not username or not password:
            raise SystemExit(
                f"需要测试账号：设置 {args.username_env} / {args.password_env} 环境变量"
                "（凭据不进命令行与日志，§10.2 :3）。"
            )
        opener, origin = authenticated_client(args.base, username, password, args.timeout)

    results = [run_scenario(case, args, opener, origin) for case in cases]

    # ── 落盘：原始响应逐行（不挑最好，失败/超时全保留）──────────────
    raw_path = out_dir / "raw_responses.jsonl"
    with raw_path.open("w", encoding="utf-8") as handle:
        for scenario in results:
            handle.write(json.dumps(scenario, ensure_ascii=False) + "\n")

    # ── 采集侧索引（不含任何判分；判分是 scorer.py 的活）────────────
    index_path = out_dir / "case_results.jsonl"
    with index_path.open("w", encoding="utf-8") as handle:
        for scenario in results:
            handle.write(json.dumps({
                "id": scenario["id"],
                "completed_turns": scenario["completed_turns"],
                "total_turns": scenario["total_turns"],
                "statuses": [t.get("status") for t in scenario["turns"]],
            }, ensure_ascii=False) + "\n")

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base": args.base,
        "timeout_seconds": args.timeout,
        "questions_sha256": sha256_of(QUESTIONS_PATH),
        "git": git_snapshot(),
        "database_fingerprints": database_fingerprints(
            json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        ),
        "service": service_probe(args.base, args.timeout),
        "credentials_note": "凭据经环境变量传入，未写入本记录",
    }
    (out_dir / "runtime_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    done = sum(1 for s in results if s["completed_turns"] == s["total_turns"])
    print(f"采集完成：{done}/{len(results)} 场景全部轮次成功，输出 {out_dir}")
    return 0 if done == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
