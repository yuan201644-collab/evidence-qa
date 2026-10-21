from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# eval/experiments/ 被 .gitignore 排除（实验中间产物，仅存在于运行机）。
# run_task4_de_ablation_v03 内部还会 import 同目录的 evont_metrics / evont_next_runner，
# 所以只探测文件存在不够 —— 直接尝试真实 import，失败就模块级 skip。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from eval.experiments.evont_next.run_task4_de_ablation_v03 import make_arm_prompts, sanitized_base_url
except ModuleNotFoundError:  # pragma: no cover - CI 环境
    pytest.skip("eval/experiments/evont_next 不在本 checkout 中（CI 环境）", allow_module_level=True)


def test_d_e_prompts_change_only_registered_factors() -> None:
    raw = {"database": "test", "dialect": "postgres", "tables": [
        {"schema": "public", "table": "Orders", "description": "orders",
         "columns": [{"name": "OrderID", "type": "integer"}, {"name": "Total", "type": "numeric"}],
         "foreign_keys": []},
        {"schema": "public", "table": "Unrelated", "description": "other data",
         "columns": [{"name": "Noise", "type": "text"}], "foreign_keys": []},
    ]}
    selected = ["public.Orders"]
    knowledge = '[{"id":"K1","tables":["public.Orders"]}]'
    source_c = {
        "id": "q1", "visible_schema_tables": selected,
        "calls": [{}, {"system": "same system", "user": json.dumps({
            "question": "orders total", "schema": "old schema", "business_knowledge": knowledge,
        })}],
    }

    prompts = make_arm_prompts(source_c, raw)
    d = json.loads(prompts["D"]["user"])
    e = json.loads(prompts["E"]["user"])

    assert prompts["D"]["system"] == prompts["E"]["system"] == "same system"
    assert d["question"] == e["question"] == "orders total"
    assert d["business_knowledge"] == knowledge
    assert e["business_knowledge"] == ""
    assert '"Unrelated"' in d["schema"]
    assert '"Unrelated"' not in e["schema"]
    assert '"Orders"' in e["schema"]

    source_c["calls"][-1]["user"] = json.dumps({
        "question": "orders total", "schema": "old schema", "business_knowledge": "[]",
    })
    empty_prompts = make_arm_prompts(source_c, raw)
    empty_d = json.loads(empty_prompts["D"]["user"])
    empty_e = json.loads(empty_prompts["E"]["user"])
    assert empty_d["business_knowledge"] == "[]"
    assert empty_e["business_knowledge"] == "[]"


def test_base_url_freeze_does_not_capture_credentials_or_query() -> None:
    assert sanitized_base_url("https://user:secret@example.test/v1?api_key=secret#frag") == \
        "https://example.test/v1"
