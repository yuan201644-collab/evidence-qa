import json

from app import routing_signals
from app.nl2sql import normalizer


def test_normalizer_and_router_use_the_same_explicit_runtime_metadata(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    docs_dir = data_dir / "documents"
    docs_dir.mkdir(parents=True)
    schema_path = data_dir / "schema_meta.json"
    descriptions_path = data_dir / "schema_meta.descriptions.zh.json"
    schema_path.write_text(json.dumps({
        "tables": [{"table": "thermofleet_units", "columns": [
            {"name": "thermofleet_id", "description": "温控转运设备编号"}
        ], "description": "温控转运设备主数据"}],
    }, ensure_ascii=False), encoding="utf-8")
    descriptions_path.write_text(json.dumps({
        "tables": {"thermofleet_units": "温控转运舱设备表"},
        "columns": {"thermofleet_units.thermofleet_id": "设备唯一编号"},
    }, ensure_ascii=False), encoding="utf-8")
    (docs_dir / "ZebraColdChainManual.md").write_text("运行手册", encoding="utf-8")
    manifest_path = data_dir / "documents_manifest.json"
    manifest_path.write_text(json.dumps({"documents": [{
        "id": "ZebraColdChainManual", "file": "ZebraColdChainManual.md",
        "title": "温控运输手册", "category": "冷链维保规范",
    }]}, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("SCHEMA_META_PATH", str(schema_path))
    monkeypatch.setenv("SCHEMA_META_DESCRIPTIONS_PATH", str(descriptions_path))
    monkeypatch.setenv("RAG_CORPUS_DIR", str(docs_dir))
    monkeypatch.setenv("RAG_CORPUS_MANIFEST_PATH", str(manifest_path))

    vocab = normalizer.load_vocab()
    assert vocab.tables == ["thermofleet_units"]
    assert vocab.doc_titles == {"温控运输手册": "ZebraColdChainManual"}
    aliases = normalizer.load_alias_table()
    assert aliases.version == "runtime-no-matching-aliases"
    assert not aliases.tables and not aliases.columns and not aliases.values

    routing_signals.reload_signals()
    terms = routing_signals.schema_terms()
    assert "thermofleet" in terms
    assert "冷链" in routing_signals.corpus_terms()
    routing_signals.reload_signals()


def test_normalizer_accepts_list_shaped_table_descriptions(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    schema_path = data_dir / "schema_meta.json"
    descriptions_path = data_dir / "schema_meta.descriptions.zh.json"
    schema_path.write_text(json.dumps({
        "tables": [{"table": "treatment_pumps", "columns": []}],
    }, ensure_ascii=False), encoding="utf-8")
    descriptions_path.write_text(json.dumps({
        "tables": [
            {"table": "treatment_pumps", "description": "污水处理泵站主数据表"},
            {"table": "ignored_no_description", "description": None},
            {"table": " ", "description": "不应创建别名"},
            "malformed row",
        ],
        "columns": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("SCHEMA_META_PATH", str(schema_path))
    monkeypatch.setenv("SCHEMA_META_DESCRIPTIONS_PATH", str(descriptions_path))
    monkeypatch.delenv("RAG_CORPUS_DIR", raising=False)
    monkeypatch.delenv("RAG_CORPUS_MANIFEST_PATH", raising=False)

    vocab = normalizer.load_vocab()

    assert vocab.table_desc_alias == {"污水处理泵站主数据": "treatment_pumps"}
    routing_signals.reload_signals()
    assert "泵站" in routing_signals.schema_terms()
    routing_signals.reload_signals()
