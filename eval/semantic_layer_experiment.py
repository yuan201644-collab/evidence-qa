"""静态业务规则的真实模型配对实验；正式问答链路保持不变。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from app.nl2sql.config import env_int, load_env_file
from app.nl2sql.executor import ExecutionResult, SqlExecutor
from app.nl2sql.pipeline import Nl2SqlPipeline
from app.nl2sql.schema import SchemaCatalog
from app.nl2sql.validator import validate_sql
from app.rag.llm import LLMError, OpenAILikeClient, _is_retryable
from metrics import compare_resultsets, gold_sql_has_order_by
from runner import SqliteExecutor, git_revision, load_cases

ASSETS = ROOT / "eval" / "semantic_layer"


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def result_rows(result: ExecutionResult) -> list[tuple[Any, ...]]:
    return [tuple(row.get(c) for c in result.columns) for row in result.rows]


class CheckedExecutor:
    """所有实验查询复用静态校验，SQLite 导入后也禁止写入。"""

    def __init__(self, dialect: str, catalog: SchemaCatalog) -> None:
        self.catalog = catalog
        self.dialect = dialect
        self.inner = SqliteExecutor() if dialect == "sqlite" else SqlExecutor(readonly=True)
        if dialect == "sqlite":
            self.inner.conn.execute("PRAGMA query_only=ON")

    def execute(self, sql: str) -> ExecutionResult:
        validation = validate_sql(sql, self.catalog, dialect="postgres", auto_limit=0)
        if not validation.ok:
            return ExecutionResult(error=validation.error)
        if self.dialect == "postgres":
            return self.inner.execute(validation.sql)
        started = time.perf_counter()
        try:
            converted = self.inner.sqlglot.transpile(validation.sql, read="postgres", write="sqlite")[0]
            cur = self.inner.conn.execute(converted)
            columns = [c[0] for c in cur.description or []]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            return ExecutionResult(rows=rows, columns=columns, row_count=len(rows),
                                   elapsed_ms=(time.perf_counter() - started) * 1000)
        except Exception as exc:
            return ExecutionResult(error=f"SQLite 执行或转换失败：{type(exc).__name__}: {exc}")

    def close(self) -> None:
        if self.dialect == "sqlite":
            self.inner.conn.close()


class MeasuredClient(OpenAILikeClient):
    """生产客户端同样的请求配置，额外记录用量；不存模型思维链。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls: list[dict[str, Any]] = []

    def _call(self, system: str, user: str) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.0,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key},
        )
        record: dict[str, Any] = {"system": system, "user": user, "network_attempts": 0}
        self.calls.append(record)
        started = time.perf_counter()
        try:
            for attempt in range(self.max_retries + 1):
                record["network_attempts"] += 1
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        obj = json.loads(response.read().decode("utf-8"))
                    break
                except Exception as exc:
                    label = f"HTTP {exc.code}" if hasattr(exc, "code") else type(exc).__name__
                    if not _is_retryable(exc) or attempt == self.max_retries:
                        record["error"] = label
                        raise LLMError(f"模型请求失败：{label}") from exc
                    time.sleep(attempt + 1)
            choice = obj["choices"][0]
            record["usage"] = obj.get("usage")
            record["finish_reason"] = choice.get("finish_reason")
            content = choice["message"].get("content")
            if not isinstance(content, str) or not content.strip():
                raise LLMError("模型未返回非空回答正文")
            return content
        except (KeyError, IndexError, ValueError) as exc:
            record["error"] = "response_parse_failed"
            raise LLMError("模型响应格式无法解析") from exc
        finally:
            record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)


def database_fingerprint(executor: CheckedExecutor, catalog: SchemaCatalog) -> dict[str, Any]:
    tables = {}
    for name in catalog.table_names():
        table = catalog.get_table(name)
        # 标识符仅来自已有目录，不使用用户输入。
        columns = ", ".join('"' + c.name.replace('"', '""') + '"' for c in table.columns)
        query = 'SELECT ' + columns + ' FROM "' + name.replace('"', '""') + '" ORDER BY ' + columns
        result = executor.execute(query)
        if result.error:
            raise RuntimeError(f"数据摘要读取失败：{name}")
        encoded = json.dumps(result_rows(result), ensure_ascii=False, default=str).encode("utf-8")
        tables[name] = {"rows": result.row_count, "sha256": hashlib.sha256(encoded).hexdigest()}
    expected = {"customer": 59, "invoice": 412, "invoice_line": 2240, "track": 3503}
    if any(tables[name]["rows"] != count for name, count in expected.items()):
        raise RuntimeError("Chinook 核心表行数不符合现有基线")
    return tables


def check_cases(cases: list[dict[str, Any]], executor: CheckedExecutor) -> tuple[dict[str, ExecutionResult], list[dict[str, Any]]]:
    gold: dict[str, ExecutionResult] = {}
    checks = []
    for case in cases:
        cid = case["id"]
        if cid in gold:
            raise ValueError(f"重复用例编号：{cid}")
        first = executor.execute(case["gold_sql"])
        second = executor.execute(case["gold_sql"])
        if first.error or second.error or not first.rows:
            raise RuntimeError(f"标准查询失败或为空：{cid} {first.error or second.error or 'empty'}")
        ordered = gold_sql_has_order_by(case["gold_sql"])
        verdict = compare_resultsets(result_rows(first), result_rows(second), order_matters=ordered)
        if not verdict.ok:
            raise RuntimeError(f"标准查询两次结果不一致：{cid}")
        check = {"id": cid, "gold_selfcheck": verdict.to_dict(), "gold_row_count": first.row_count}
        if case.get("mutant_sql"):
            mutant = executor.execute(case["mutant_sql"])
            if mutant.error:
                raise RuntimeError(f"变异查询本身执行失败：{cid} {mutant.error}")
            comparison = compare_resultsets(result_rows(first), result_rows(mutant), order_matters=ordered)
            check["mutant_distinguishable"] = not comparison.ok
            check["mutant_verdict"] = comparison.to_dict()
        checks.append(check)
        gold[cid] = first
    return gold, checks


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [r for r in records if r["usage_complete"]]
    latencies = sorted(r["latency_ms"] for r in records)
    total = len(records)
    usage = {key: sum(r["usage"][key] for r in complete)
             for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")}
    return {
        "passed": sum(r["verdict"]["kind"] in ("pass", "pass_loose") for r in records),
        "strict_passed": sum(r["verdict"]["kind"] == "pass" for r in records),
        "total": total, "usage_samples": len(complete), "usage_totals": usage,
        "total_tokens_per_question": usage["total_tokens"] / len(complete) if complete else None,
        "latency_median_ms": statistics.median(latencies) if latencies else None,
        "latency_p95_ms": latencies[min(len(latencies) - 1, math.ceil(len(latencies) * .95) - 1)] if latencies else None,
        "mean_generation_attempts": statistics.mean(r["attempts"] for r in records) if total else None,
    }


def render_report(report: dict[str, Any]) -> str:
    meta = report["meta"]
    lines = ["# 静态业务语义层配对实验结果 v0.1", "", "更新日期：2026-10-02。", "",
             f"执行方言：{meta['execution_dialect']}；生成方言：PostgreSQL；模型：{meta['model']}；每组每题{meta['repeats']}轮。",
             "当前系统保留既有语义说明；实验组仅增加冻结的静态规则。正式业务代码未修改。", "",
             "| 题集 | 组别 | 执行通过 | 严格通过 | 平均总词元/题 | 耗时中位数毫秒 | 生成次数均值 |",
             "|---|---|---|---|---|---|---|"]
    suite_names = {"regression": "原有回归集", "heldout": "本轮冻结验证集"}
    arm_names = {"baseline": "当前系统", "static_rules": "增加静态规则"}
    for suite, groups in report["summary"].items():
        for arm, score in groups.items():
            tokens = score["total_tokens_per_question"]
            lines.append(f"| {suite_names[suite]} | {arm_names[arm]} | {score['passed']}/{score['total']} | {score['strict_passed']}/{score['total']} | "
                         f"{round(tokens, 1) if tokens is not None else '未返回'} | {score['latency_median_ms']} | {score['mean_generation_attempts']:.2f} |")
    lines += ["", "## 配对变化", "", "| 题集 | 轮次 | 失败转成功 | 成功转失败 | 两组都失败 |", "|---|---|---|---|---|"]
    details = []
    for pair in report["pairs"]:
        lines.append(f"| {suite_names[pair['suite']]} | {pair['repeat']} | {len(pair['wins'])} | {len(pair['regressions'])} | {len(pair['both_failed'])} |")
        if pair["wins"] or pair["regressions"] or pair["both_failed"]:
            details.append(f"- {suite_names[pair['suite']]}第{pair['repeat']}轮：改善 {'、'.join(pair['wins']) or '无'}；退步 {'、'.join(pair['regressions']) or '无'}；两组都失败 {'、'.join(pair['both_failed']) or '无'}。")
    lines += ["", *details]
    weak = [c["id"] for c in report["checks"] if c.get("mutant_distinguishable") is False]
    lines += ["", "## 证据边界", "",
              f"- 对所列错误口径无区分度的验证题：{'、'.join(weak) or '无'}。这些题仍计执行匹配，但不能证明识别了该业务语义。",
              f"- 运行前后相关源码和数据摘要一致：{report['integrity_ok']}。",
              "- 新题和规则由同一执行者整理，未经独立盲审；仅支持探索结论。",
              "- 重复调用不等于新增独立题目。词元用量来自接口，未将缓存折算为费用；延迟受网络和缓存影响。",
              "- SQLite 执行结果不能替代真实 PostgreSQL 成绩。" if meta["execution_dialect"] == "sqlite" else "- 使用真实 PostgreSQL 只读执行。",
              "- 本轮未测文档回答忠实度与检索命中率；未验证任务4、工具化或自演化。", ""]
    return "\n".join(lines)


def run_experiment(args: argparse.Namespace) -> Path:
    rules_path = ASSETS / "rules_v0.1.json"
    heldout_path = ASSETS / "heldout_v0.1.jsonl"
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    hint = "【已核实的业务口径；仅在与用户问题相符时使用】\n" + "\n".join(
        f"[{rule['id']}] {rule['text']}" for rule in rules["rules"])
    regression = [{**case, "suite": "regression"} for case in load_cases(ROOT / "eval/cases/nl2sql.jsonl")]
    heldout = [{**case, "suite": "heldout"} for case in load_cases(heldout_path)]
    cases = regression + heldout
    catalog = SchemaCatalog.load()
    source_paths = [Path(__file__), rules_path, heldout_path, ROOT / "eval/cases/nl2sql.jsonl",
                    ROOT / "infra/schema_meta.descriptions.zh.json", ROOT / "eval/metrics.py", ROOT / "eval/runner.py",
                    ROOT / "eval/tools/local_check.py", ROOT / "app/contracts.py", ROOT / "app/rag/llm.py", ROOT / "app/rag/config.py"]
    source_paths += list((ROOT / "app/nl2sql").glob("*.py"))
    source_paths.append(Path(catalog.source_path))
    fingerprints = {str(p.relative_to(ROOT)): sha256(p) for p in source_paths}
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "eval/reports" / f"run-semantic-{timestamp}"
    output.mkdir(parents=True, exist_ok=False)
    executor = CheckedExecutor(args.dialect, catalog)
    try:
        data_before = database_fingerprint(executor, catalog)
        gold, checks = check_cases(cases, executor)
        write_json(output / "case_checks.json", checks)
        if args.check_only:
            print(f"用例自检 {len(checks)}/{len(cases)} 通过；有区分度新题 "
                  f"{sum(c.get('mutant_distinguishable') is True for c in checks)}/{len(heldout)}；{output}", flush=True)
            return output
        if os.getenv("LLM_MOCK") == "1":
            raise ValueError("真实对照实验不允许 LLM_MOCK=1")
        if not all(os.getenv(key) for key in ("LLM_API_KEY", "LLM_API_BASE", "LLM_MODEL")):
            raise ValueError("真实实验需明确配置模型、接口地址与密钥；不回落默认海外模型")
        client = MeasuredClient(max_tokens=env_int("NL2SQL_MAX_TOKENS", 16000))
        pipeline = Nl2SqlPipeline(client, catalog=catalog, executor=executor,
                                  max_attempts=env_int("NL2SQL_MAX_ATTEMPTS", 3), auto_limit=1000)
        commit, dirty = git_revision()
        meta = {"generated_at": datetime.now(timezone.utc).isoformat(), "git_commit": commit, "git_dirty": dirty,
                "execution_dialect": args.dialect, "generation_dialect": "postgres", "repeats": args.repeats,
                "model": client.model, "temperature": 0, "max_tokens": client.max_tokens,
                "timeout_seconds": client.timeout, "http_retries": client.max_retries,
                "max_generation_attempts": pipeline.max_attempts, "time_anchor": "2024-06-15"}
        write_json(output / "freeze.json", {"meta": meta, "source_sha256": fingerprints, "data": data_before})
        for path in source_paths:
            destination = output / "source_snapshot" / path.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())
            if sha256(destination) != fingerprints[str(path.relative_to(ROOT))]:
                raise RuntimeError("源文件在冻结时发生变化，停止实验")
        write_json(output / "rules_snapshot.json", rules)
        (output / "cases_snapshot.jsonl").write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n", encoding="utf-8")
        (output / "catalog_prompt.txt").write_text(catalog.render_ddl(), encoding="utf-8")
        records = []
        print(f"冻结完成：{output}；{len(regression)}道回归题＋{len(heldout)}道新题，每题每组{args.repeats}轮", flush=True)
        for repeat in range(1, args.repeats + 1):
            for index, case in enumerate(cases):
                arms = ["baseline", "static_rules"] if (index + repeat) % 2 else ["static_rules", "baseline"]
                for arm in arms:
                    client.calls = []
                    started = time.perf_counter()
                    outcome = pipeline.run(case["question"], hint=hint if arm == "static_rules" else "", time_anchor="2024-06-15")
                    latency = round((time.perf_counter() - started) * 1000, 1)
                    predicted = [tuple(row.get(c) for c in outcome.columns) for row in outcome.rows]
                    verdict = compare_resultsets(result_rows(gold[case["id"]]), predicted,
                                                 pred_error=outcome.error,
                                                 order_matters=gold_sql_has_order_by(case["gold_sql"]))
                    usage: dict[str, int] = {key: 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")}
                    complete = bool(client.calls)
                    for call in client.calls:
                        values = call.get("usage") or {}
                        complete &= all(isinstance(values.get(key), int) for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                            usage[key] += values.get(key, 0) or 0
                        usage["cached_tokens"] += (values.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                    record = {"id": case["id"], "suite": case["suite"], "arm": arm, "repeat": repeat,
                              "sql": outcome.sql, "error": outcome.error, "attempts": outcome.attempts,
                              "latency_ms": latency, "verdict": verdict.to_dict(), "usage": usage,
                              "usage_complete": complete, "trace": [step.model_dump(mode="json") for step in outcome.trace],
                              "calls": list(client.calls)}
                    records.append(record)
                    with (output / "records.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    prediction_path = output / f"predictions-{case['suite']}-{arm}-r{repeat}.jsonl"
                    with prediction_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({k: record[k] for k in ("id", "sql", "error", "latency_ms")}, ensure_ascii=False) + "\n")
                    print(f"轮{repeat} {case['id']} {arm}: {verdict.kind} {latency:.0f}ms 生成{outcome.attempts}次", flush=True)
                    if client.calls and any(call.get("error") for call in client.calls):
                        raise RuntimeError("出现模型请求错误，停止本轮；保留已完成记录，不把环境故障当语义差异")
        pairs = []
        for suite in ("regression", "heldout"):
            for repeat in range(1, args.repeats + 1):
                pair: dict[str, Any] = {"suite": suite, "repeat": repeat, "wins": [], "regressions": [], "both_failed": []}
                for case in (c for c in cases if c["suite"] == suite):
                    matched = {r["arm"]: r["verdict"]["kind"] in ("pass", "pass_loose") for r in records
                               if r["id"] == case["id"] and r["repeat"] == repeat}
                    if matched["static_rules"] and not matched["baseline"]:
                        pair["wins"].append(case["id"])
                    elif matched["baseline"] and not matched["static_rules"]:
                        pair["regressions"].append(case["id"])
                    elif not matched["baseline"] and not matched["static_rules"]:
                        pair["both_failed"].append(case["id"])
                pairs.append(pair)
        report = {"meta": meta, "checks": checks, "pairs": pairs,
                  "summary": {suite: {arm: summarize([r for r in records if r["suite"] == suite and r["arm"] == arm])
                                      for arm in ("baseline", "static_rules")}
                              for suite in ("regression", "heldout")},
                  "discriminating_heldout_summary": {},
                  "integrity_ok": fingerprints == {str(p.relative_to(ROOT)): sha256(p) for p in source_paths}
                                  and data_before == database_fingerprint(executor, catalog)}
        ids = {c["id"] for c in checks if c.get("mutant_distinguishable") is True}
        report["discriminating_heldout_summary"] = {arm: summarize([r for r in records if r["id"] in ids and r["arm"] == arm])
                                                  for arm in ("baseline", "static_rules")}
        write_json(output / "summary.json", report)
        (output / "report.md").write_text(render_report(report), encoding="utf-8")
        print("完成：" + str(output / "report.md"), flush=True)
        return output
    finally:
        executor.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="静态业务规则配对实验")
    parser.add_argument("--dialect", choices=("postgres", "sqlite"), default="postgres")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats 必须至少为1")
    load_env_file()
    try:
        run_experiment(args)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"实验停止：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
