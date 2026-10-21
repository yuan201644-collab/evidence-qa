"""独立的受控反馈审计；不把故意注入错误当作自然系统收益。"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ontology_experiment as experiment
from semantic_layer_experiment import MeasuredClient, sha256, write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("develop", "validate", "evaluate"), required=True)
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    experiment.load_env_file()
    raw = experiment.read(experiment.ASSETS / "schema_profile_v0.1.json")
    executor = experiment.GuardedExecutor(raw, experiment.aw_dsn())
    original = experiment.read(experiment.ASSETS / "knowledge_v0.2.json")["entries"]
    damaged = copy.deepcopy(original)
    header = next(e for e in damaged if e["id"] == "sales_header")
    header["text"] = "销售订单正式商品金额必须把SubTotal、TaxAmt、Freight相加；单独SubTotal是未完成订单的临时金额，不用于正式商品金额统计。本镜像TotalDue未填值，应以这三个字段相加实现商品金额。采购订单与销售订单分开。按订单日期统计。"
    cases = experiment.read(experiment.ASSETS / "cases_v0.3.json")["cases"]
    sets = {"develop": ["dev-01", "dev-03"], "validate": ["validation-01", "validation-03"],
            "evaluate": ["test-01", "test-02", "test-07", "test-08", "test-20"]}
    sources = experiment.source_paths() + [Path(__file__), experiment.ROOT / "docs/EvoOntology受控反馈验证设计_v0.1.md", experiment.ROOT / "docs/EvoOntology受控反馈验证设计_v0.2.md", experiment.ROOT / "docs/EvoOntology受控反馈验证设计_v0.3.md", experiment.ROOT / "eval/ontology_route_checks.py",
              experiment.ASSETS / "cases_v0.3.json", experiment.ASSETS / "knowledge_v0.2.json", experiment.ASSETS / "schema_profile_v0.1.json", experiment.ASSETS / "data_grounding_evidence_v0.1.json"]
    if args.run_dir:
        output = args.run_dir.resolve()
        freeze = experiment.read(output / "freeze.json")
        if freeze["source_sha256"] != {str(p.relative_to(experiment.ROOT)): sha256(p) for p in sources}:
            raise RuntimeError("受控实验冻结后源码发生变化")
    else:
        output = experiment.ROOT / "eval/reports" / ("run-ontology-fault-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / "damaged_knowledge.json", {"entries": damaged, "intentional_fault": True})
        write_json(output / "freeze.json", {"source_sha256": {str(p.relative_to(experiment.ROOT)): sha256(p) for p in sources},
                   "data_aw": experiment.fingerprint(raw, experiment.aw_dsn()), "splits": sets,
                   "model": MeasuredClient(max_tokens=16000).model, "intentional_fault": True})
        for p in sources:
            target = output / "source_snapshot" / p.relative_to(experiment.ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(p.read_bytes())
        probe = 'SELECT count(*) AS n,count("SubTotal") AS subtotal_nonnull,count("TaxAmt") AS tax_nonnull,count("Freight") AS freight_nonnull,count("TotalDue") AS total_due_nonnull,count(*) FILTER(WHERE "SubTotal">0) AS positive_subtotals FROM "Sales"."SalesOrderHeader"'
        facts = executor.execute(probe)
        if facts.error:
            raise RuntimeError("受控证据探查失败")
        write_json(output / "seed_evidence.json", {"metadata": raw, "probes": [{"sql": probe, "rows": facts.rows}],
                   "official_field_definitions": {"source": "https://raw.githubusercontent.com/microsoft/sql-server-samples/master/samples/databases/adventure-works/oltp-install-script/instawdb.sql", "SubTotal": "订单商品小计，由对应明细商品金额汇总", "TaxAmt": "税额", "Freight": "运费", "TotalDue": "含税运费应付总额，SubTotal+TaxAmt+Freight"}})
        print("受控实验冻结：" + str(output), flush=True)
    prior = experiment.load_records(output)
    if any(r["phase"] == args.stage for r in prior):
        raise RuntimeError("受控阶段不重复覆盖")
    selected = [c for c in cases if c["id"] in sets[args.stage]]
    if args.stage == "develop":
        records = experiment.run_batch(selected, ["retrieval"], raw, {"retrieval": damaged}, output, "develop", executor)
        # 只用销售商品金额故障轨迹；采购作为控制，不用于改变其规则。
        failures = [r for r in records if r["id"] == "dev-01" and r["verdict"]["kind"] not in experiment.PASS]
        if not failures:
            write_json(output / "patch_decision.json", {"accepted": False, "candidate_exists": False, "reason": "注入错误未诱发失败，不能宣称修复收益"})
        else:
            client = MeasuredClient(max_tokens=16000, timeout=60, max_retries=2)
            official_ref = "official:" + experiment.read(output / "seed_evidence.json")["official_field_definitions"]["source"]
            system = '修改指定错误语义条目sales_header。只输出JSON：{"id":"sales_header","aliases":[中文别名],"text":"修正后的业务说明","attribution":"依据哪些来源事实纠正哪些错误","evidence_refs":["metadata:Sales.SalesOrderHeader","probe:0"]}。别名最多12个，每个2到30字；说明最多1200字。evidence_refs可使用metadata:Sales.SalesOrderHeader、probe:0、official:加输入中的官方来源URL。不得加入题号、整题、答案或SQL。只根据字段及探查证据，不猜行业口径。'
            trajectories = [{k: r[k] for k in ("question", "sql", "error", "trace")} for r in failures]
            try:
                response = client.chat(system, json.dumps({"entry": header, "failed": True, "trajectories": trajectories, "source_evidence": experiment.read(output / "seed_evidence.json")}, ensure_ascii=False))
                (output / "candidate_raw_response.txt").write_text(response, encoding="utf-8")
                text = response.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                patch = json.loads(text)
                write_json(output / "candidate_draft.json", patch)
                if patch.get("id") != "sales_header" or not isinstance(patch.get("aliases"), list) or not isinstance(patch.get("text"), str):
                    raise ValueError("修改格式不合法")
                if len(patch["text"]) > 1200 or len(patch["aliases"]) > 12 or any(not isinstance(a, str) or not 2 <= len(a) <= 30 for a in patch["aliases"]):
                    raise ValueError("修改超过边界")
                if not patch.get("evidence_refs") or not set(patch["evidence_refs"]) <= {"metadata:Sales.SalesOrderHeader", "probe:0", official_ref}:
                    raise ValueError("修改证据无效")
                candidate = copy.deepcopy(damaged)
                entry = next(e for e in candidate if e["id"] == "sales_header")
                entry["text"] = patch["text"]
                entry["aliases"] = list(dict.fromkeys(entry["aliases"] + patch["aliases"]))
                write_json(output / "candidate.json", {"entries": candidate, "patch": patch})
                write_json(output / "patch_decision.json", {"accepted": False, "candidate_exists": True, "reason": "等待证据审查和配对验证"})
            except Exception as exc:
                write_json(output / "patch_decision.json", {"accepted": False, "candidate_exists": False, "reason": "候选修改失败：" + type(exc).__name__ + ": " + str(exc)})
            write_json(output / "evolver_calls.json", client.calls)
    elif args.stage == "validate":
        decision = experiment.read(output / "patch_decision.json")
        review_path = output / "candidate_evidence_review.json"
        if not decision["candidate_exists"]:
            print(decision["reason"], flush=True)
        elif not review_path.exists() or not experiment.read(review_path).get("grounded"):
            decision.update(accepted=False, reason="证据审查未通过")
            write_json(output / "patch_decision.json", decision)
        else:
            candidate = experiment.read(output / "candidate.json")["entries"]
            records = experiment.run_batch(selected, ["retrieval", "candidate"], raw, {"retrieval": damaged, "candidate": candidate}, output, "validate", executor)
            checks = []
            for rep in (1, 2):
                old = {r["id"]: r["verdict"]["kind"] in experiment.PASS for r in records if r["repeat"] == rep and r["arm"] == "retrieval"}
                new = {r["id"]: r["verdict"]["kind"] in experiment.PASS for r in records if r["repeat"] == rep and r["arm"] == "candidate"}
                checks.append({"repeat": rep, "wins": [k for k in old if new[k] and not old[k]], "regressions": [k for k in old if old[k] and not new[k]]})
            decision.update(accepted=all(c["wins"] and not c["regressions"] for c in checks), paired_checks=checks, reason="配对门槛通过" if all(c["wins"] and not c["regressions"] for c in checks) else "配对门槛未通过")
            write_json(output / "patch_decision.json", decision)
    else:
        decision = experiment.read(output / "patch_decision.json")
        if not decision["accepted"]:
            print("受控修改未接受，不伪造修复测试。", flush=True)
        else:
            candidate = experiment.read(output / "candidate.json")["entries"]
            write_json(output / "test_version_lock.json", {"candidate_sha256": sha256(output / "candidate.json"), "case_ids": sets["evaluate"]})
            experiment.run_batch(selected, ["retrieval", "evolved"], raw, {"retrieval": damaged, "evolved": candidate}, output, "evaluate", executor)
        write_json(output / "summary.json", {"summary": experiment.summarize(experiment.load_records(output)), "patch_decision": decision,
                   "intentional_fault": True, "integrity_ok": experiment.read(output / "freeze.json")["data_aw"] == experiment.fingerprint(raw, experiment.aw_dsn())})
    print("受控阶段完成：" + args.stage + " " + str(output), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print("受控实验停止：" + str(exc), file=sys.stderr)
        raise SystemExit(1)
