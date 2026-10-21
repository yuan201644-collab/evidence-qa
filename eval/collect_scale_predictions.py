"""按三档规模题集走 FastAPI `/api/ask` 采集，产出可直接交给
`eval/cases/scale_structured_v01/score_scale_run.py` 的运行目录。

为什么单独写、不复用 `eval/mainline/driver.py`：
    driver.py 面向会话级主线评测（一个案例多轮共用 session、带 rubric 防泄露等
    一整套机制）。规模题只有 30 题单轮，只关心「真实端到端耗时」与「SQL 是否正确」，
    用不上那套。这里保持最小，但**严格对齐 score_scale_run.py 的输入契约**：
      <run-dir>/raw_answers.jsonl  每条 {case_id, http_status, elapsed_ms, response, error}
      <run-dir>/freeze.json        capture_status / answer_key_read_by_driver /
                                   case_file_sha256 / data_manifest / service_runtime_manifest

答案隔离（`answer_key_read_by_driver` 必须是 False）：
    本脚本只读 `questions.jsonl`，不读 `references.jsonl`，也不读任何 rubric。
    判分器事后自己读参考答案并在只读事务里复算——这是它设计上的隔离点，
    这里如实标记 False。

时延口径：
    elapsed_ms 是从发出 HTTP 请求到收到完整响应体的墙钟时间，含 FastAPI 框架、
    鉴权中间件与 JSON 序列化；不含客户端解析。首题触发模型/索引首次加载会偏高，
    故 `--warmup` 先跑一题预热且不计入正式样本。
"""

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PACK_DIR = ROOT / "eval" / "cases" / "scale_structured_v01"
COOKIE_FILE = ROOT / "agent_temp" / "pb" / "scale_session.json"
BASE = "http://127.0.0.1:8000"
REPORTS = ROOT / "eval" / "reports"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def ask(client: httpx.Client, question: str, session_id: str, timeout: float) -> tuple[dict, int, float, dict | None]:
    """返回 (响应体, http_status, elapsed_ms, 错误信息)。"""
    payload = {"question": question, "session_id": session_id}
    started = time.perf_counter()
    try:
        resp = client.post(BASE + "/api/ask", json=payload, timeout=timeout)
        elapsed_ms = (time.perf_counter() - started) * 1000
        status = resp.status_code
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"_raw": resp.text[:2000]}
        if status != 200:
            return body, status, elapsed_ms, {"type": "HttpStatus", "detail": str(body)[:300]}
        return body, status, elapsed_ms, None
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {}, 0, elapsed_ms, {"type": type(exc).__name__, "detail": str(exc)[:300]}


def main() -> int:
    parser = argparse.ArgumentParser(description="三档规模题集在线采集")
    parser.add_argument("--out-dir", type=Path, default=None, help="默认落到 eval/reports/scale_run_<时间戳>")
    parser.add_argument("--warmup", action="store_true", help="先跑一题预热，不计入正式样本")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    questions_path = PACK_DIR / "questions.jsonl"
    cases = read_jsonl(questions_path)
    if args.limit:
        cases = cases[: args.limit]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_dir = args.out_dir or (REPORTS / f"scale_run_{stamp}")
    run_dir.mkdir(parents=True, exist_ok=True)

    cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    rows: list[dict] = []

    with httpx.Client(cookies=cookies) as client:
        if args.warmup:
            _, _, warm_ms, _ = ask(client, "sales_scale_1000 表里共有多少行？", "scale-warmup", args.timeout)
            print("预热 " + str(round(warm_ms)) + " ms（不计入样本）")

        started_at = datetime.now(timezone.utc)
        for index, case in enumerate(cases, 1):
            case_id = str(case["id"])
            body, status, elapsed_ms, error = ask(
                client, str(case["question"]), f"scale-{case_id}", args.timeout
            )
            rows.append({
                "case_id": case_id,
                "http_status": status,
                "elapsed_ms": round(elapsed_ms, 2),
                "response": body,
                "error": error,
            })
            flag = "ERR" if error else "OK "
            print(f"[{index}/{len(cases)}] {case_id} {flag} {round(elapsed_ms)}ms"
                  + ("  " + str(error)[:80] if error else ""))

    raw_path = run_dir / "raw_answers.jsonl"
    raw_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    data_manifest = PACK_DIR / "data_manifest.json"
    runtime_manifest = PACK_DIR / "runtime_manifest.json"
    freeze = {
        "schema": "structured_scale_run_freeze_v1",
        "run_id": run_dir.name,
        "capture_status": "completed",
        "answer_key_read_by_driver": False,
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "case_count": len(rows),
        "case_file_sha256": sha256_file(questions_path),
        "data_manifest": {"path": str(data_manifest.relative_to(ROOT)), "sha256": sha256_file(data_manifest)},
        "service_runtime_manifest": {
            "path": str(runtime_manifest.relative_to(ROOT)),
            "sha256": sha256_file(runtime_manifest),
        },
        "raw_answers_sha256": sha256_file(raw_path),
        "notes": "本冻结在读取参考答案之前完成；参考答案只由 score_scale_run.py 在只读事务里事后读取。",
    }
    (run_dir / "freeze.json").write_text(json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    ok = sum(1 for r in rows if r["http_status"] == 200 and not r["error"])
    print("")
    print("运行目录: " + str(run_dir))
    print("成功响应: " + str(ok) + " / " + str(len(rows)))
    print("下一步: D:\\python\\python.exe -X utf8 eval\\cases\\scale_structured_v01\\score_scale_run.py --run-dir \"" + str(run_dir) + "\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
