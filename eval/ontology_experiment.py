"""EvoOntology思想的有限机制实验：知识、按需访问、反馈修改分别对照。"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import statistics
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
import psycopg
import sqlglot
from sqlglot import exp
from app.nl2sql.config import load_env_file
from app.nl2sql.executor import ExecutionResult, SqlExecutor
from app.nl2sql.generator import build_prompts, extract_sql
from app.nl2sql.normalizer import normalize
from app.nl2sql.schema import SchemaCatalog
from app.nl2sql.validator import validate_sql
from metrics import compare_resultsets, gold_sql_has_order_by
from runner import JsonlPredictor, run_nl2sql
from semantic_layer_experiment import MeasuredClient, result_rows, sha256, write_json

ASSETS = ROOT / "eval" / "ontology"
ARMS = ["baseline", "full_knowledge", "retrieval", "evolved"]
PASS = {"pass", "pass_loose"}
ANCHOR = datetime(2024, 6, 15)


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def aw_dsn() -> str:
    """只在内存构造连接串；不落盘、不输出密码。"""
    return psycopg.conninfo.make_conninfo(host="127.0.0.1", port=5433, dbname="postgres",
                                        user="postgres", password=os.getenv("AW_POSTGRES_PASSWORD", "postgres"))


class PromptCatalog(SchemaCatalog):
    """共同的多模式提示适配；不改生产目录和校验器。"""

    def __init__(self, raw: dict[str, Any], selected: set[str] | None = None) -> None:
        full = SchemaCatalog.from_dict(raw)
        super().__init__(full.tables.values(), database=full.database, dialect=full.dialect)
        self.raw = raw
        self.selected = selected

    def render_ddl(self, **kwargs: Any) -> str:
        lines = [f"数据库{self.database}；双引号标识符大小写敏感；使用模式限定名。"]
        for t in self.raw["tables"]:
            key = t["schema"] + "." + t["table"]
            if self.selected is not None and key not in self.selected:
                continue
            lines.append(f'表 "{t["schema"]}"."{t["table"]}"：{t.get("description", "")}')
            for c in t["columns"]:
                lines.append(f'  "{c["name"]}" {c["type"]}' + (" 主键" if c.get("is_primary_key") else "")
                             + (" — " + c["description"] if c.get("description") else ""))
            for fk in t.get("foreign_keys", []):
                ref = fk["references"]
                lines.append("  外键 " + ",".join(fk["columns"]) + " → "
                             + ref.get("schema", "public") + "." + ref["table"] + "(" + ",".join(ref["columns"]) + ")")
        return "\n".join(lines)


class GuardedExecutor:
    """语法树安全闸门＋真实目录＋模式检查＋数据库只读事务。"""

    def __init__(self, raw: dict[str, Any], dsn: str | None) -> None:
        self.catalog = SchemaCatalog.from_dict(raw)
        self.tables = {t["table"]: t["schema"] for t in raw["tables"]}
        self.inner = SqlExecutor(dsn=dsn, readonly=True, timeout_seconds=30)

    def execute(self, sql: str) -> ExecutionResult:
        v = validate_sql(sql, self.catalog, auto_limit=0)
        if not v.ok:
            return ExecutionResult(error=v.error)
        root = sqlglot.parse_one(v.sql, read="postgres")
        ctes = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
        for t in root.find_all(exp.Table):
            if t.name.lower() in ctes:
                continue
            schema = self.tables.get(t.name)
            if schema is None:
                return ExecutionResult(error="表名大小写不匹配：" + t.name)
            if schema != "public" and t.db != schema:
                return ExecutionResult(error=f'该表需要正确的模式限定名："{schema}"."{t.name}"')
            if t.db and t.db != schema:
                return ExecutionResult(error="不存在的模式与表组合：" + t.db + "." + t.name)
        return self.inner.execute(v.sql)


class TupleExecutor:
    def __init__(self, inner: GuardedExecutor) -> None:
        self.inner = inner

    def execute(self, sql: str) -> tuple[list[tuple[Any, ...]], str | None]:
        result = self.inner.execute(sql)
        return result_rows(result), result.error


def table_keys(sql: str, raw: dict[str, Any]) -> set[str]:
    try:
        root = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return set()
    names = {t["table"].lower(): t["schema"] + "." + t["table"] for t in raw["tables"]}
    ctes = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    return {names[t.name.lower()] for t in root.find_all(exp.Table)
            if t.name.lower() not in ctes and t.name.lower() in names}


def fingerprint(raw: dict[str, Any], dsn: str | None) -> dict[str, Any]:
    """真实表/视图内容摘要；不记录行级数据，不跳过非空值。"""
    out: dict[str, Any] = {}
    from app.nl2sql.config import get_dsn
    with psycopg.connect(dsn or get_dsn(), connect_timeout=10) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('statement_timeout','60000',false)")
            for t in raw["tables"]:
                ident = psycopg.sql.Identifier(t["schema"], t["table"])
                cur.execute(psycopg.sql.SQL("SELECT count(*), md5(string_agg(h,',' ORDER BY h)) FROM (SELECT md5(to_jsonb(x)::text) h FROM {} x) q").format(ident))
                out[t["schema"] + "." + t["table"]] = list(cur.fetchone())
    return out


def browse(query: str, entries: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    """轻量确定性检索；无新增向量模型，不宣称论文同款检索实现。"""
    ranked = []
    q = query.lower()
    for e in entries:
        terms = e["aliases"] + [e["title"], e["id"]] + e["tables"]
        score = sum(min(len(t), 8) for t in set(terms) if t.lower() in q)
        if score:
            ranked.append((score, e["id"], e))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in ranked[:limit]]


def connect_tables(selected: set[str], raw: dict[str, Any]) -> set[str]:
    """仅以真实外键补齐已选表之间的短路径，不根据标准查询选表。"""
    graph: dict[str, set[str]] = {}
    for t in raw["tables"]:
        key = t["schema"] + "." + t["table"]
        graph.setdefault(key, set())
        for fk in t.get("foreign_keys", []):
            ref = fk["references"]
            other = ref.get("schema", "public") + "." + ref["table"]
            graph[key].add(other)
            graph.setdefault(other, set()).add(key)
    result = set(selected)
    nodes = sorted(selected)
    for index, start in enumerate(nodes):
        for target in nodes[index + 1:]:
            queue = deque([(start, [start])])
            seen = {start}
            while queue:
                node, path = queue.popleft()
                if node == target:
                    result.update(path)
                    break
                if len(path) >= 4:
                    continue
                for nxt in sorted(graph.get(node, set())):
                    if nxt not in seen:
                        seen.add(nxt)
                        queue.append((nxt, path + [nxt]))
    return result


def json_call(client: MeasuredClient, system: str, user: str) -> dict[str, Any]:
    text = client.chat(system, user).strip()
    # 格式错误走有记录的降级，不增加隐性重试。
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("模型输出须为JSON对象")
    return result


def solve(case: dict[str, Any], arm: str, repeat: int, raw: dict[str, Any],
          entries: list[dict[str, Any]], executor: GuardedExecutor,
          gold: ExecutionResult, phase: str) -> dict[str, Any]:
    client = MeasuredClient(max_tokens=16000, timeout=60, max_retries=2)
    started = time.perf_counter()
    norm = normalize(case["question"], time_anchor=ANCHOR)
    q = norm.normalized
    trace: list[dict[str, Any]] = [{"tool": "normalizer", "input": case["question"], "output": norm.model_dump(mode="json")}]
    all_tables = {t["schema"] + "." + t["table"] for t in raw["tables"]}
    selected = set(all_tables)
    hint = ""
    fallback = None
    if arm == "full_knowledge":
        hint = json.dumps(entries, ensure_ascii=False)
    elif arm in ("retrieval", "evolved", "candidate"):
        manifest = {"tools": {"browse": "按业务词查语义条目，最多4个", "resolve": "取得命中条目的映射、约束、证据和字段"},
                    "table_index": sorted(all_tables)}
        try:
            plan = json_call(client, "你是数据库知识检索规划器。只输出JSON：{\"query\":\"检索词，可补充业务同义词\",\"tables\":[\"模式.表\"]}。表最多6个，必须在目录中。不要写SQL。", json.dumps({"question": q, "manifest": manifest}, ensure_ascii=False))
            query = str(plan.get("query", q))[:500]
            planned = plan.get("tables", [])
            if not isinstance(planned, list) or len(planned) > 6:
                raise ValueError("检索规划候选表格式或数量错误")
            chosen = set(planned) & all_tables
            hits = browse(query + " " + q, entries)
            trace.append({"tool": "browse", "input": {"query": query + " " + q, "limit": 4},
                          "output": [{"id": h["id"], "title": h["title"]} for h in hits]})
            for h in hits:
                chosen.update(h["tables"])
            selected = connect_tables(chosen, raw)
            if not selected:
                raise ValueError("没有检索到有效表")
            hint = json.dumps(hits, ensure_ascii=False)
            trace.append({"tool": "resolve", "input": {"ids": [h["id"] for h in hits]},
                          "output": {"entries": hits, "tables": sorted(selected)}})
        except Exception as exc:
            # 网络错误不能冒充语义差异；上层发现calls.error后停止。
            fallback = type(exc).__name__
            selected = set(all_tables)
            trace.append({"tool": "retrieval_fallback", "reason": fallback, "mode": "full_schema"})
    catalog = PromptCatalog(raw, selected)
    hint += "\n标识符使用双引号，表名带模式；注意题目要求的列数与顺序。"
    sql = ""
    error = None
    output = ExecutionResult(error="未执行")
    for attempt in range(1, 4):
        system, user = build_prompts(q, catalog, hint=hint, previous_error=error,
                                    previous_sql=sql, time_anchor="2024-06-15")
        try:
            sql = extract_sql(client.chat(system, user))
        except Exception as exc:
            error = "模型请求失败：" + type(exc).__name__
            if client.calls:
                client.calls[-1]["error"] = type(exc).__name__
            break
        output = executor.execute(sql)
        trace.append({"tool": "validate_execute", "input": {"sql": sql},
                      "output": {"error": output.error, "columns": output.columns, "rows": output.rows},
                      "elapsed_ms": output.elapsed_ms})
        error = output.error
        if not error:
            break
    elapsed = (time.perf_counter() - started) * 1000
    verdict = compare_resultsets(result_rows(gold), result_rows(output), pred_error=error,
                                 order_matters=gold_sql_has_order_by(case["gold_sql"]))
    expected = table_keys(case["gold_sql"], raw)
    usage = {k: sum((c.get("usage") or {}).get(k, 0) or 0 for c in client.calls)
             for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    usage["cached_tokens"] = sum(((c.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0 for c in client.calls)
    return {"id": case["id"], "question": case["question"], "phase": phase,
            "arm": arm, "repeat": repeat, "topic": case.get("topic"), "sql": sql,
            "error": error, "attempts": attempt, "verdict": verdict.to_dict(),
            "selected_tables": sorted(selected), "table_recall": len(expected & selected) / len(expected) if expected else None,
            "latency_ms": round(elapsed, 1), "usage": usage, "usage_complete": all(all(isinstance((c.get("usage") or {}).get(k), int) for k in ("prompt_tokens", "completion_tokens", "total_tokens")) for c in client.calls),
            "retrieval_fallback": fallback, "trace": trace, "calls": client.calls,
            "executed_outside_retrieval": sorted(table_keys(sql, raw) - selected)}


def check_cases(cases: list[dict[str, Any]], executor: GuardedExecutor) -> tuple[dict[str, ExecutionResult], list[dict[str, Any]]]:
    results = {}
    checks = []
    for c in cases:
        first, second = executor.execute(c["gold_sql"]), executor.execute(c["gold_sql"])
        if first.error or second.error or not first.rows or result_rows(first) != result_rows(second):
            raise RuntimeError(f"标准查询无法稳定判定：{c['id']} {first.error or second.error}")
        item = {"id": c["id"], "rows": first.row_count}
        if c.get("mutant_sql"):
            wrong = executor.execute(c["mutant_sql"])
            if wrong.error:
                raise RuntimeError(f"错误口径示例本身无法执行：{c['id']} {wrong.error}")
            item["mutant_distinguishable"] = not compare_resultsets(result_rows(first), result_rows(wrong), pred_error=wrong.error, order_matters=gold_sql_has_order_by(c["gold_sql"])).ok
        results[c["id"]] = first
        checks.append(item)
    return results, checks


def run_batch(cases: list[dict[str, Any]], arms: list[str], raw: dict[str, Any],
              variants: dict[str, list[dict[str, Any]]], output: Path, phase: str,
              executor: GuardedExecutor, repeats: int = 2) -> list[dict[str, Any]]:
    gold, checks = check_cases(cases, executor)
    write_json(output / (phase + "_case_checks.json"), checks)
    records: list[dict[str, Any]] = []
    for repeat in range(1, repeats + 1):
        def one(index: int, case: dict[str, Any]) -> list[dict[str, Any]]:
            order = list(arms) if (index + repeat) % 2 else list(reversed(arms))
            return [solve(case, arm, repeat, raw, variants[arm], executor, gold[case["id"]], phase) for arm in order]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(one, index, c) for index, c in enumerate(cases)]
            for future in as_completed(futures):
                for record in future.result():
                    records.append(record)
                    with (output / "records.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    print(f"{phase} r{repeat} {record['id']} {record['arm']} {record['verdict']['kind']} 表{len(record['selected_tables'])} 请求{len(record['calls'])}", flush=True)
                    if any(call.get("error") for call in record["calls"]):
                        raise RuntimeError("模型接口故障，停止本阶段并保留记录；不当作知识能力失败")
    crosscheck = {}
    for arm in arms:
        for repeat in range(1, repeats + 1):
            rows = [r for r in records if r["arm"] == arm and r["repeat"] == repeat]
            path = output / f"predictions-{phase}-{arm}-r{repeat}.jsonl"
            path.write_text("\n".join(json.dumps({k: r[k] for k in ("id", "sql", "error", "latency_ms")}, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
            score = run_nl2sql(cases, TupleExecutor(executor), JsonlPredictor(path))
            passed = sum(r["verdict"]["kind"] in PASS for r in rows)
            if score["overall"]["passed"] != passed:
                raise RuntimeError("现有runner重新计分不一致")
            crosscheck[f"{arm}-r{repeat}"] = score
    write_json(output / (phase + "_runner_crosscheck.json"), crosscheck)
    return records


def security_check(raw: dict[str, Any], executor: GuardedExecutor) -> None:
    """有意义的安全和适配检查；阻断查询不会进入数据库。"""
    for query in ['DELETE FROM "Sales"."SalesOrderHeader"', 'SELECT 1; SELECT 2',
                  'SELECT * INTO "Sales"."new_table" FROM "Sales"."SalesOrderHeader"',
                  'SELECT count(*) FROM "Purchasing"."SalesOrderHeader"']:
        if not executor.execute(query).error:
            raise RuntimeError("安全自检未拦截不合规查询")
    for query in ['SELECT count(*) FROM "Sales"."SalesOrderHeader"',
                  'SELECT sum("OrderQty") FROM "Sales"."SalesOrderDetail"']:
        if executor.execute(query).error:
            raise RuntimeError("有效查询被误拒绝")


def source_paths() -> list[Path]:
    return [Path(__file__), ASSETS / "build_benchmark.py", ASSETS / "cases_v0.1.json", ASSETS / "knowledge_v0.1.json",
            ASSETS / "clarify_identifiers.py", ASSETS / "cases_v0.2.json",
            ROOT / "docs/EvoOntology机制验证实验设计_v0.2.md", ROOT / "eval/semantic_layer_experiment.py",
            ROOT / "eval/metrics.py", ROOT / "eval/runner.py", ROOT / "app/rag/llm.py", ROOT / "app/rag/config.py",
            ROOT / "infra/schema_meta.adventureworks.json", ROOT / "infra/schema_meta.json",
            ROOT / "infra/schema_meta.aliases.zh.json", ROOT / "infra/schema_meta.descriptions.zh.json",
            ROOT / "eval/cases/nl2sql.jsonl", ROOT / "eval/semantic_layer/rules_v0.1.json"] + list((ROOT / "app/nl2sql").glob("*.py"))


def initialize() -> Path:
    output = ROOT / "eval/reports" / ("run-ontology-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir(parents=True, exist_ok=False)
    raw = read(ROOT / "infra/schema_meta.adventureworks.json")
    executor = GuardedExecutor(raw, aw_dsn())
    security_check(raw, executor)
    _, checks = check_cases(read(ASSETS / "cases_v0.2.json")["cases"], executor)
    write_json(output / "initial_case_checks.json", checks)
    # 只读元数据/事实探查；作为语义条目的证据，不含标准答案。
    evidence = {"metadata": raw, "probes": []}
    probes = ['SELECT count(*) AS n, count(*) FILTER(WHERE "EndDate" IS NULL) AS active_rows FROM "HumanResources"."EmployeeDepartmentHistory"',
              'SELECT count(*) AS n,count(*) FILTER(WHERE abs("LineTotal"-("UnitPrice"*"OrderQty"*(1-"UnitPriceDiscount")))>0.01) AS formula_mismatches FROM "Sales"."SalesOrderDetail"']
    for query in probes:
        result = executor.execute(query)
        if result.error:
            raise RuntimeError("初始证据探查失败")
        evidence["probes"].append({"sql": query, "rows": result.rows})
    write_json(output / "seed_evidence.json", evidence)
    paths = source_paths()
    hashes = {str(p.relative_to(ROOT)): sha256(p) for p in paths}
    client = MeasuredClient(max_tokens=16000, timeout=60, max_retries=2)
    write_json(output / "freeze.json", {"timestamp": datetime.now(timezone.utc).isoformat(), "source_sha256": hashes,
               "data_aw": fingerprint(raw, aw_dsn()), "model": client.model,
               "temperature": 0, "repeats": 2, "max_workers": 2,
               "limits": {"sql_attempts": 3, "planner_calls": 1, "patches": 1, "timeout": 60, "http_retries": 2}})
    for p in paths:
        dest = output / "source_snapshot" / p.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(p.read_bytes())
        if sha256(dest) != hashes[str(p.relative_to(ROOT))]:
            raise RuntimeError("冻结时文件改变")
    print("冻结完成：" + str(output), flush=True)
    return output


def verify_sources(output: Path) -> None:
    frozen = read(output / "freeze.json")
    for name, digest in frozen["source_sha256"].items():
        if sha256(ROOT / name) != digest:
            raise RuntimeError("冻结后源文件改变：" + name)


def propose(output: Path, records: list[dict[str, Any]], entries: list[dict[str, Any]]) -> None:
    failures = [r for r in records if r["verdict"]["kind"] not in PASS]
    if not failures:
        write_json(output / "patch_decision.json", {"accepted": False, "reason": "开发集没有失败，不强行生成修改", "candidate_exists": False})
        return
    trajectories = [{k: r[k] for k in ("id", "question", "sql", "error", "trace")} for r in failures]
    for t in trajectories:
        t["failed"] = True
    # 输出仅确定性失败标签，没有gold SQL或结果。读取开发轨迹，不传整个题集。
    client = MeasuredClient(max_tokens=16000, timeout=60, max_retries=2)
    system = "你维护数据库语义知识。依据开发题失败记录，提出对一个现有条目的有限修改。禁止返回答案、标准SQL、题号或完整题目；不能改变关联表。只输出JSON：{id,aliases:[最多12个中文别名],text:业务说明,attribution:失败归因,evidence_refs:[metadata:模式.表或probe:0/1]}。必须保留正确旧说明；有证据才修正，不猜行业惯例。说明最多1200字。"
    try:
        patch = json_call(client, system, json.dumps({"entries": entries, "failed_development_trajectories": trajectories,
                                                      "source_evidence": read(output / "seed_evidence.json")}, ensure_ascii=False))
        ids = {e["id"] for e in entries}
        if patch.get("id") not in ids or not isinstance(patch.get("aliases"), list) or not 1 <= len(patch["aliases"]) <= 12:
            raise ValueError("修改对象或别名格式不合规")
        if not all(isinstance(a, str) and 2 <= len(a) <= 30 for a in patch["aliases"]):
            raise ValueError("别名长度不合规")
        if not isinstance(patch.get("text"), str) or not 1 <= len(patch["text"]) <= 1200:
            raise ValueError("业务说明格式不合规")
        original = next(e for e in entries if e["id"] == patch["id"])
        allowed = {"metadata:" + t for t in original["tables"]} | {"probe:0", "probe:1"}
        if not patch.get("evidence_refs") or not set(patch["evidence_refs"]) <= allowed:
            raise ValueError("证据引用无效")
        if any(t["id"] in patch["text"] or t["question"] in patch["text"] for t in trajectories):
            raise ValueError("修改包含题号或整题")
        candidate = copy.deepcopy(entries)
        entry = next(e for e in candidate if e["id"] == patch["id"])
        entry["aliases"] = list(dict.fromkeys(entry["aliases"] + patch["aliases"]))
        entry["text"] = patch["text"]
        entry["evidence"] = list(dict.fromkeys(entry["evidence"] + patch["evidence_refs"]))
        write_json(output / "candidate.json", {"parent_version": "0.1", "version": "0.2-candidate", "entries": candidate, "patch": patch})
        write_json(output / "patch_decision.json", {"accepted": False, "reason": "等待证据复核与配对验证", "candidate_exists": True})
    except Exception as exc:
        write_json(output / "patch_decision.json", {"accepted": False, "reason": "候选生成或格式失败：" + type(exc).__name__, "candidate_exists": False})
    write_json(output / "evolver_calls.json", client.calls)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, Any] = {}
    for phase in sorted({r["phase"] for r in records}):
        by[phase] = {}
        for arm in ARMS + ["candidate"]:
            rows = [r for r in records if r["phase"] == phase and r["arm"] == arm]
            if not rows:
                continue
            per_id: dict[str, list[bool]] = {}
            for r in rows:
                per_id.setdefault(r["id"], []).append(r["verdict"]["kind"] in PASS)
            latency = sorted(r["latency_ms"] for r in rows)
            by[phase][arm] = {"passed": sum(r["verdict"]["kind"] in PASS for r in rows), "total": len(rows),
                "both_rounds_passed_cases": sum(len(v) == 2 and all(v) for v in per_id.values()), "unique_cases": len(per_id),
                "by_round": {str(rep): {"passed": sum(r["verdict"]["kind"] in PASS for r in rows if r["repeat"] == rep), "total": sum(r["repeat"] == rep for r in rows)} for rep in (1, 2)},
                "mean_tokens": round(statistics.mean(r["usage"]["total_tokens"] for r in rows), 1),
                "total_tokens": sum(r["usage"]["total_tokens"] for r in rows),
                "p50_ms": statistics.median(latency), "p95_ms": latency[min(len(latency)-1, int(len(latency)*0.95))],
                "mean_selected_tables": round(statistics.mean(len(r["selected_tables"]) for r in rows), 2),
                "mean_table_recall": round(statistics.mean(r["table_recall"] for r in rows if r["table_recall"] is not None), 4),
                "model_requests": sum(len(r["calls"]) for r in rows), "retrieval_fallbacks": sum(bool(r["retrieval_fallback"]) for r in rows)}
    return by


def load_records(output: Path) -> list[dict[str, Any]]:
    path = output / "records.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("check", "develop", "validate", "evaluate"), required=True)
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    load_env_file()
    if os.getenv("LLM_MOCK") == "1" or not all(os.getenv(k) for k in ("LLM_API_KEY", "LLM_API_BASE", "LLM_MODEL")):
        raise RuntimeError("需明确配置真实模型，不回退其他模型")
    output = args.run_dir.resolve() if args.run_dir else initialize()
    verify_sources(output)
    raw = read(ROOT / "infra/schema_meta.adventureworks.json")
    executor = GuardedExecutor(raw, aw_dsn())
    entries = read(ASSETS / "knowledge_v0.1.json")["entries"]
    cases = read(ASSETS / "cases_v0.2.json")["cases"]
    existing = load_records(output)
    stage_phases = {"evaluate": {"test", "regression"}}.get(args.stage, {args.stage})
    if any(r["phase"] in stage_phases for r in existing):
        raise RuntimeError("已有阶段记录不覆盖；需新建完整运行")
    if args.stage == "check":
        print("用例、安全、真实数据自检完成。" + str(output))
        return 0
    if args.stage == "develop":
        records = run_batch([c for c in cases if c["split"] == "dev"], ["retrieval"], raw, {"retrieval": entries}, output, "develop", executor)
        propose(output, records, entries)
    elif args.stage == "validate":
        decision = read(output / "patch_decision.json")
        audit_path = output / "candidate_evidence_review.json"
        if not decision.get("candidate_exists"):
            print("没有有效候选；保留父版本。", flush=True)
        elif not audit_path.exists() or not read(audit_path).get("grounded"):
            decision.update(accepted=False, reason="证据复核未通过，保留父版本")
            write_json(output / "patch_decision.json", decision)
        else:
            candidate = read(output / "candidate.json")["entries"]
            records = run_batch([c for c in cases if c["split"] == "validation"], ["retrieval", "candidate"], raw, {"retrieval": entries, "candidate": candidate}, output, "validate", executor)
            checks = []
            for repeat in (1, 2):
                old = {r["id"]: r["verdict"]["kind"] in PASS for r in records if r["arm"] == "retrieval" and r["repeat"] == repeat}
                new = {r["id"]: r["verdict"]["kind"] in PASS for r in records if r["arm"] == "candidate" and r["repeat"] == repeat}
                checks.append({"repeat": repeat, "wins": [k for k in old if new[k] and not old[k]], "regressions": [k for k in old if old[k] and not new[k]]})
            accept = all(c["wins"] and not c["regressions"] for c in checks)
            decision.update(accepted=accept, reason="两轮均有改善且无退步" if accept else "未达到两轮均改善且无退步的门槛", paired_checks=checks)
            write_json(output / "patch_decision.json", decision)
            if accept:
                write_json(output / "accepted_knowledge.json", {"version": "0.2", "entries": candidate})
    elif args.stage == "evaluate":
        decision = read(output / "patch_decision.json")
        if decision.get("reason") == "等待证据复核与配对验证":
            raise RuntimeError("尚未完成验证，禁止打开最终测试")
        arms = ARMS if decision["accepted"] else ARMS[:-1]
        variants = {arm: entries for arm in arms}
        if decision["accepted"]:
            variants["evolved"] = read(output / "accepted_knowledge.json")["entries"]
        write_json(output / "test_version_lock.json", {"arms": arms, "knowledge_sha256": {a: hashlib.sha256(json.dumps(v, ensure_ascii=False, sort_keys=True).encode()).hexdigest() for a, v in variants.items()}, "patch_decision": decision})
        run_batch([c for c in cases if c["split"] == "test"], arms, raw, variants, output, "test", executor)
        # Chinook回归：使用现有该库规则；AW修改不迁移到不同业务库。
        reg_raw = read(ROOT / "infra/schema_meta.json")
        reg_cases = [json.loads(line) for line in (ROOT / "eval/cases/nl2sql.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        reg_terms = {"sales_grain": ["销售额", "销售", "金额", "收入", "发票"],
                     "actual_price": ["价格", "单价", "成交"], "country_scope": ["国家", "地区"],
                     "count_grain": ["人数", "数量", "订单", "多少", "购买"],
                     "include_zero": ["未购买", "未售出", "没有", "包含"],
                     "duration_units": ["时长", "分钟", "播放", "秒"],
                     "playlist_scope": ["播放列表", "收录"], "employee_sales": ["员工", "客服", "销售代表"],
                     "time_filter": ["去年", "今年", "日期", "入职", "2023"]}
        reg_entries = [{"id": r["id"], "title": r["text"].split("。")[0], "aliases": reg_terms[r["id"]], "text": r["text"], "tables": list(dict.fromkeys("public." + s.split(".")[0] for s in r["sources"])), "evidence": [r["source_file"]]} for r in read(ROOT / "eval/semantic_layer/rules_v0.1.json")["rules"]]
        reg_before = fingerprint(reg_raw, None)
        run_batch(reg_cases, ["baseline", "retrieval"], reg_raw, {"baseline": reg_entries, "retrieval": reg_entries}, output, "regression", GuardedExecutor(reg_raw, None))
        write_json(output / "regression_integrity.json", {"unchanged": reg_before == fingerprint(reg_raw, None)})
        verify_sources(output)
        integrity = read(output / "freeze.json")["data_aw"] == fingerprint(raw, aw_dsn())
        if not integrity:
            raise RuntimeError("数据在实验期间改变，结果不能用于比较")
        records = load_records(output)
        write_json(output / "summary.json", {"summary": summarize(records), "integrity_ok": integrity, "patch_decision": decision, "rag_faithfulness": None, "rag_retrieval_hit_rate": None})
    print("阶段完成：" + args.stage + " " + str(output), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print("实验停止：" + str(exc), file=sys.stderr)
        raise SystemExit(1)
