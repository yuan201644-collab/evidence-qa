"""真实检索/网络/隔离 PostgreSQL 边界检查；不用标准答案回放或模拟连接器。

默认仅执行真实 BM25 和本机 TCP 故障检查。数据库锁与只读拒写检查需要显式
--expected-system-identifier；该编号应从 askdata-cross-source-eval-db 容器读取。
只对已有合成评测库施加临时事务锁，结束后回滚，不新增或修改业务记录。
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import importlib.metadata
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.tools.cross_source_demo import CrossSourceDemoTool, _DocumentEvidenceUnavailable, _RunBudget
from eval.cross_source_reference_baseline import StageBReferenceBaseline
from eval.cross_source_runner import ask

FIXTURES = ROOT / "eval" / "cross_source" / "boundary"
WORK_ORDER = "WO-2026-0101"
QUESTION = f"工单 {WORK_ORDER} 是否在合同修复时限内完成？"


class BoundaryAssertion(AssertionError):
    def __init__(self, message: str, detail: dict[str, Any] | None) -> None:
        super().__init__(message)
        self.detail = detail or {}


def require(condition: bool, message: str, detail: dict[str, Any] | None = None) -> None:
    if not condition:
        raise BoundaryAssertion(message, detail)


@contextmanager
def configured_environment(values: dict[str, str]) -> Iterator[None]:
    """仅临时设置真实工具配置，不替换工具函数或依赖结果。"""
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def query_observations(tool: CrossSourceDemoTool) -> list[dict[str, Any]]:
    retriever = tool._ensure_doc_retriever()
    queries = (
        "HT-EVAL-001 服务级别 首次响应 现场到达 修复完成 受理 服务恢复时限",
        "HT-EVAL-001 修复完成 服务恢复 受理时刻 修复时限",
    )
    return [{"query": query, "candidates": [
        {"rank": rank, "score": candidate.score, "ref": ref.model_dump()}
        for rank, (candidate, ref) in enumerate(retriever.retrieve(query, top_k=8, doc_id="HT-EVAL-001"), 1)
    ]} for query in queries]


def retrieval_retry() -> dict[str, Any]:
    docs = FIXTURES / "retry_documents"
    with configured_environment({"CROSS_SOURCE_DEMO_DOCS_DIR": str(docs), "CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT": "2"}):
        a = StageBReferenceBaseline(dsn="unused", docs_dir=docs)._retrieve_once("HT-EVAL-001")
        tool = CrossSourceDemoTool(dsn="unused")
        ref, limits, _, attempts = tool._retrieve_contract_limits("HT-EVAL-001", _RunBudget(5000))
        require(a[0] is None and a[1] is None, "参考首查必须未取得时限")
        require(a[2]["status"] == "required_metric_not_found", "参考基线应记录单次检索未命中")
        require([item["status"] for item in attempts] == ["required_metric_not_found", "required_metric_found"], "真实补查未按预期恢复")
        require(limits["service_restored"] == ("hours", 8), "条款数值错误")
        return {"scope": "真实解析、切片、BM25；本项不连接数据库", "attempts": attempts,
                "ref": ref.model_dump(), "limits": limits, "retrieval_observations": query_observations(tool)}


def retrieval_missing() -> dict[str, Any]:
    with configured_environment({"CROSS_SOURCE_DEMO_DOCS_DIR": str(FIXTURES / "missing_documents"), "CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT": "2"}):
        try:
            CrossSourceDemoTool(dsn="unused")._retrieve_contract_limits("HT-EVAL-001", _RunBudget(5000))
        except _DocumentEvidenceUnavailable as exc:
            require(len(exc.attempts) == 2, "未按次数停止")
            return {"attempts": exc.attempts}
    raise AssertionError("无时限文档不应返回时限")


def network_fault(*, stall: bool) -> dict[str, Any]:
    # 真实 OS TCP 端点：释放端口并确认 OS 拒绝；或监听但不响应 PG 协议。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        if stall:
            probe.listen(1)
        port = probe.getsockname()[1]
        os_error = None
        if not stall:
            probe.close()
            with socket.socket() as confirm:
                confirm.settimeout(3)
                os_error = confirm.connect_ex(("127.0.0.1", port))
            require(os_error in (errno.ECONNREFUSED, 10061), "OS 未确认拒绝连接，不能把此端点标为拒绝连接")
        dsn = make_conninfo(host="127.0.0.1", port=port, dbname="cross_source_demo", user="network_probe")
        budget_ms = 1000 if stall else 5000
        with configured_environment({"CROSS_SOURCE_DEMO_TIME_BUDGET_MS": str(budget_ms)}):
            started = time.perf_counter()
            result = CrossSourceDemoTool(dsn=dsn).run({"question": QUESTION})
            elapsed_ms = (time.perf_counter() - started) * 1000
    # Windows 驱动可把 OS 拒绝连接延迟到 connect_timeout 才返回；不规定它必须快速失败。
    # remaining_ms() 向下取整，剩余不足 1 ms 已视为耗尽。
    expected = "budget_exhausted" if elapsed_ms >= budget_ms - 1 else "failed"
    detail = {"fault": "TCP 接通但不响应 PostgreSQL 协议" if stall else "释放临时端口后 OS 确认拒绝连接",
              "os_connect_error": os_error,
              "configured_budget_ms": budget_ms, "observed_elapsed_ms": elapsed_ms,
              "result": result.model_dump()}
    require(result.trace[0].output["status"] == expected, "网络错误被错误分类", detail)
    require(result.refs == [] and len(result.trace) == 1, "网络故障不能冒充查询零结果或继续文档检索", detail)
    return detail


def fixture_snapshot(conn: psycopg.Connection) -> dict[str, Any]:
    result = {}
    for table, ordering in (
        ("equipment", "equipment_id"), ("contracts", "contract_id"),
        ("contract_equipment", "contract_id,equipment_id"),
        ("work_orders", "work_order_id"), ("work_order_events", "work_order_id,event_type"),
    ):
        rows = conn.execute(f"SELECT * FROM cross_source_demo.{table} ORDER BY {ordering}").fetchall()
        result[table] = {"count": len(rows), "sha256": hashlib.sha256(json.dumps(rows, default=str, ensure_ascii=False).encode()).hexdigest()}
    return result


def validate_isolated_database(dsn: str, expected_id: str) -> dict[str, Any]:
    options = conninfo_to_dict(dsn)
    require(options.get("host") == "127.0.0.1", "故障检查仅允许本机回环地址")
    require(options.get("port") != "55532", "拒绝对日常演示端口施加故障")
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        database, system_id = conn.execute("SELECT current_database(), system_identifier::text FROM pg_control_system()").fetchone()
        require(database == "cross_source_demo" and system_id == expected_id, "隔离库实例标识不匹配")
        orders = [row[0] for row in conn.execute("SELECT work_order_id FROM cross_source_demo.work_orders ORDER BY work_order_id").fetchall()]
        require(orders == [f"WO-2026-{i:04d}" for i in range(101, 108)], "不是指定的七题合成评测库，拒绝施加锁")
        return fixture_snapshot(conn)


def read_only_rejection(dsn: str) -> dict[str, Any]:
    # 诊断 SQL 不进入在线工具；WHERE false 且只读事务双重保护，不写入记录。
    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        require(conn.execute("SHOW transaction_read_only").fetchone()[0] == "on", "不是只读事务")
        try:
            conn.execute("UPDATE cross_source_demo.work_orders SET summary=summary WHERE false")
        except psycopg.errors.ReadOnlySqlTransaction as exc:
            state = exc.sqlstate
            conn.rollback()
            require(state == "25006", "不是数据库只读拒写")
            return {"sqlstate": state, "error_type": type(exc).__name__, "scope": "隔离数据库安全诊断，不属于在线执行 SQL"}
        conn.rollback()
    raise AssertionError("只读事务未拒绝 UPDATE")


def authentication_rejection(dsn: str) -> dict[str, Any]:
    options = conninfo_to_dict(dsn)
    options["password"] = "intentionally-invalid-isolated-probe-password"
    with configured_environment({"CROSS_SOURCE_DEMO_TIME_BUDGET_MS": "5000"}):
        result = CrossSourceDemoTool(dsn=make_conninfo(**options)).run({"question": QUESTION})
    detail = {"fault": "只对隔离库提交一次错误密码；不更改账号或密码", "result": result.model_dump()}
    require(result.trace[0].output["status"] == "failed", "认证拒绝被错误分类", detail)
    require(result.trace[0].output["error_type"] == "OperationalError", "未得到真实数据库连接错误", detail)
    require(result.refs == [] and len(result.trace) == 1, "认证错误冒充空查询或继续检索", detail)
    return detail


def locked_query_timeout(dsn: str) -> dict[str, Any]:
    with psycopg.connect(dsn, connect_timeout=5) as lock_conn:
        lock_conn.execute("SET LOCAL lock_timeout='1s'")
        lock_conn.execute("LOCK TABLE cross_source_demo.work_orders IN ACCESS EXCLUSIVE MODE")
        try:
            with configured_environment({"CROSS_SOURCE_DEMO_TIME_BUDGET_MS": "1000"}):
                result = CrossSourceDemoTool(dsn=dsn).run({"question": QUESTION})
            require(result.trace[0].output["status"] == "budget_exhausted", "真实锁等待未进入预算停止")
            require(result.trace[0].output["error_type"] == "QueryCanceled", "不是 PostgreSQL 查询取消")
            require(result.refs == [] and len(result.trace) == 1, "超时后错误地继续检索或引用")
            return {"fault": "隔离库事务临时独占 work_orders 表；在线 SELECT 等待锁后由 statement_timeout 取消",
                    "result": result.model_dump()}
        finally:
            lock_conn.rollback()


@contextmanager
def api_server(dsn: str, docs: Path, enabled: bool = True) -> Iterator[str]:
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    env = {**os.environ, "CROSS_SOURCE_DEMO_DSN": dsn, "CROSS_SOURCE_DEMO_DOCS_DIR": str(docs),
           "CROSS_SOURCE_DEMO_ENABLED": "1" if enabled else "0", "CROSS_SOURCE_DEMO_TIME_BUDGET_MS": "5000",
           "CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT": "2", "DOC_SEARCH_BACKEND": "mock", "GRAPH_BACKEND": "mock"}
    process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                               cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    endpoint = f"http://127.0.0.1:{port}"
    try:
        deadline = time.perf_counter() + 10
        while time.perf_counter() < deadline:
            require(process.poll() is None, "辅助 API 进程启动失败")
            try:
                with urllib.request.urlopen(endpoint + "/health", timeout=0.3) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise TimeoutError("辅助 API 未在 10 秒内就绪")
        yield endpoint
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def http_retrieval_boundary(dsn: str, *, recover: bool) -> dict[str, Any]:
    docs = FIXTURES / ("retry_documents" if recover else "missing_documents")
    with api_server(dsn, docs) as endpoint:
        response, elapsed = ask(endpoint, "actual-retry" if recover else "actual-missing", QUESTION, 10)
    sql_steps = [step for step in response["trace"] if step["tool"] == "cross_source_sql"]
    doc = next(step for step in response["trace"] if step["tool"] == "cross_source_doc_retrieval")
    alignment = response["trace"][-1]["output"]
    require(len(sql_steps) == 1 and len(doc["output"]["attempts"]) == 2, "补查次数或局部性错误")
    require(alignment["status"] == ("repair_within_limit" if recover else "insufficient_document_evidence"), "在线补查结论错误")
    require(bool([ref for ref in response["refs"] if ref["kind"] == "doc"]) == recover, "文档引用被伪造或缺失")
    if recover:
        metric = next(item for item in alignment["metric_checks"] if item["metric"] == "service_restored")
        require(metric["elapsed_seconds"] == 14400 and metric["limit"] == 8, "在线补查数值错误")
    canonical = lambda ref: json.dumps(ref, sort_keys=True, ensure_ascii=False)
    require({canonical(ref) for ref in response["refs"]} == {canonical(ref) for step in response["trace"] for ref in step["hit"]}, "在线引用与记录不一致")
    return {"http_elapsed_ms": elapsed, "response": response, "model_calls": 0,
            "scope": "真实 FastAPI→固定参数 SQL→真实 BM25；其他默认工具明确使用模拟实现且本题未调用"}


def http_switch_disabled(dsn: str) -> dict[str, Any]:
    with api_server(dsn, FIXTURES / "retry_documents", enabled=False) as endpoint:
        response, elapsed = ask(endpoint, "disabled", QUESTION, 10)
    require(not any(step["tool"].startswith("cross_source_") for step in response["trace"]), "关闭开关仍调用演示工具")
    return {"http_elapsed_ms": elapsed, "response": response, "scope": "只验证关闭演示开关的路由兼容性；默认工具为模拟实现，不计业务答案正确率"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-system-identifier", help="从隔离评测容器读取的 PostgreSQL 实例标识；提供才运行数据库故障项")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "eval" / "reports" / "cross_source")
    args = parser.parse_args()
    cases = [("real_bm25_retry", retrieval_retry), ("real_bm25_missing_limit", retrieval_missing),
             ("real_tcp_connection_refused", lambda: network_fault(stall=False)),
             ("real_tcp_connection_timeout", lambda: network_fault(stall=True))]
    dsn = os.environ.get("CROSS_SOURCE_DEMO_DSN", "")
    before = None
    if args.expected_system_identifier:
        before = validate_isolated_database(dsn, args.expected_system_identifier)
        cases += [("postgres_read_only_rejection", lambda: read_only_rejection(dsn)),
                  ("postgres_authentication_rejection", lambda: authentication_rejection(dsn)),
                  ("postgres_statement_timeout", lambda: locked_query_timeout(dsn)),
                  ("http_actual_targeted_retry", lambda: http_retrieval_boundary(dsn, recover=True)),
                  ("http_actual_missing_limit", lambda: http_retrieval_boundary(dsn, recover=False)),
                  ("http_disabled_demo_switch", lambda: http_switch_disabled(dsn))]
    sources = [Path(__file__), ROOT / "app" / "tools" / "cross_source_demo.py", ROOT / "eval" / "cross_source_reference_baseline.py",
               ROOT / "eval" / "cross_source_verify.ps1", ROOT / "eval" / "cross_source_runner.py",
               ROOT / "app" / "contracts.py", ROOT / "app" / "orchestrator.py", ROOT / "app" / "main.py",
               ROOT / "app" / "rag" / "schemas.py", ROOT / "app" / "rag" / "tokenizer.py",
               ROOT / "app" / "rag" / "retrieval" / "retriever.py",
               *(ROOT / "app" / "rag" / "ingest" / name for name in ("loader.py", "toc.py", "chunker.py")),
               *(ROOT / "tests").glob("test_cross_source_*.py"), *FIXTURES.rglob("*.md")]
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    results = []
    for name, action in cases:
        started = time.perf_counter()
        try:
            detail = action()
            row = {"case_id": name, "passed": True, **detail}
        except Exception as exc:
            row = {"case_id": name, "passed": False, "error_type": type(exc).__name__,
                   "error": str(exc) if isinstance(exc, AssertionError) else "异常信息不自动导出，避免泄露连接凭证"}
            if isinstance(exc, BoundaryAssertion):
                row["observed_detail"] = exc.detail
        row["test_elapsed_ms"] = (time.perf_counter() - started) * 1000
        results.append(row)
        print(f"{name}: {'通过' if row['passed'] else '失败'}", flush=True)
    after = validate_isolated_database(dsn, args.expected_system_identifier) if before is not None else None
    unchanged = hashes == {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=5, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=5, check=True).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = "unknown", None
    report = {"created_at_utc": stamp, "status": "constructed_boundary_regression_not_unseen_eval",
              "warning": "合成、针对性工程检查，不证明自然场景召回率、泛化、模型能力或赛事成绩。连接预算可能受驱动超时粒度影响，观察耗时照实记录。",
              "postgres_system_identifier": args.expected_system_identifier, "sha256": hashes,
              "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                          "git_commit": commit, "git_dirty": dirty,
                          "dependencies": {name: importlib.metadata.version(name) for name in ("psycopg", "fastapi", "uvicorn", "pydantic", "sqlglot", "jieba")}},
              "source_snapshot_unchanged": unchanged, "database_snapshot_before": before,
              "database_snapshot_after": after, "database_unchanged": before == after,
              "pass_count": sum(row["passed"] for row in results), "case_count": len(results), "results": results}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f"cross-source-boundary-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"报告：{path}")
    return 0 if all(row["passed"] for row in results) and before == after and unchanged else 1


if __name__ == "__main__":
    sys.exit(main())
