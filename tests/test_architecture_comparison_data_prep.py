import sys
from pathlib import Path

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "eval" / "cross_source_heldout" / "tools"
sys.path.insert(0, str(TOOLS))

from prepare_architecture_comparison_data import (  # noqa: E402
    _ordered_table_metadata,
    _validate_identifier,
    _validate_schema_ddl,
    _validate_target_schemas,
    _reader_usage_schemas,
    _schema_ddl_for_execution,
)


def test_data_prep_loads_foreign_key_parents_before_dependent_tables():
    input_rows = [{"table": name} for name in (
        "work_order_events", "contract_equipment", "work_orders", "contracts", "equipment",
    )]
    assert [row["table"] for row in _ordered_table_metadata(input_rows)] == [
        "equipment", "contracts", "work_orders", "contract_equipment", "work_order_events",
    ]
    with pytest.raises(RuntimeError, match="外键装载顺序"):
        _ordered_table_metadata(input_rows[:-1])


def test_data_prep_accepts_only_expected_isolated_schema_and_tables():
    ddl = """
    CREATE SCHEMA demo_v02;
    CREATE TABLE demo_v02.equipment (equipment_id TEXT PRIMARY KEY);
    CREATE TABLE demo_v02.contracts (contract_id TEXT PRIMARY KEY);
    """
    _validate_schema_ddl(ddl, "demo_v02", {"equipment", "contracts"})


def test_data_prep_accepts_if_not_exists_for_an_explicit_fresh_schema():
    ddl = """
    CREATE SCHEMA IF NOT EXISTS demo_v02;
    CREATE TABLE demo_v02.equipment (equipment_id TEXT PRIMARY KEY);
    """
    _validate_schema_ddl(ddl, "demo_v02", {"equipment"})
    executable = _schema_ddl_for_execution(ddl, "demo_v02")
    assert "CREATE SCHEMA demo_v02;" in executable
    assert "IF NOT EXISTS" not in executable


def test_data_prep_accepts_if_not_exists_on_tables_in_a_fresh_schema():
    ddl = """
    CREATE SCHEMA IF NOT EXISTS demo_v03;
    CREATE TABLE IF NOT EXISTS demo_v03.equipment (equipment_id TEXT PRIMARY KEY);
    """
    _validate_schema_ddl(ddl, "demo_v03", {"equipment"})
    executable = _schema_ddl_for_execution(ddl, "demo_v03")
    assert "CREATE SCHEMA demo_v03;" in executable
    assert "CREATE SCHEMA IF NOT EXISTS" not in executable


def test_data_prep_rejects_quoted_schema_identifiers_with_if_not_exists():
    ddl = '''
    CREATE SCHEMA IF NOT EXISTS "demo_v03";
    CREATE TABLE IF NOT EXISTS "demo_v03".equipment (equipment_id TEXT PRIMARY KEY);
    '''
    with pytest.raises(RuntimeError, match="schema.sql"):
        _validate_schema_ddl(ddl, "demo_v03", {"equipment"})


def test_data_prep_counts_explicitly_quoted_expected_table_names():
    ddl = """
    CREATE SCHEMA demo_v02;
    CREATE TABLE demo_v02."equipment" (equipment_id TEXT PRIMARY KEY);
    """
    _validate_schema_ddl(ddl, "demo_v02", {"equipment"})


@pytest.mark.parametrize("ddl", [
    "CREATE SCHEMA demo_v02; DROP SCHEMA public CASCADE;",
    "CREATE SCHEMA other_schema; CREATE TABLE other_schema.equipment (id TEXT);",
    "CREATE SCHEMA demo_v02; CREATE TABLE demo_v02.unexpected (id TEXT);",
    "CREATE SCHEMA demo_v02; CREATE TABLE demo_v02.equipment (id TEXT); CREATE TABLE other_schema.extra (id TEXT);",
    "CREATE SCHEMA demo_v02; CREATE SCHEMA other_schema; CREATE TABLE demo_v02.equipment (id TEXT);",
])
def test_data_prep_rejects_destructive_or_mismatched_schema(ddl):
    with pytest.raises(RuntimeError):
        _validate_schema_ddl(ddl, "demo_v02", {"equipment"})


@pytest.mark.parametrize("identifier", ["MixedCase", "schema;drop", "bad-name", "a" * 64])
def test_data_prep_rejects_unsafe_postgres_identifiers(identifier):
    with pytest.raises(ValueError):
        _validate_identifier(identifier, "schema")


def test_preparation_tool_is_frozen_to_one_isolated_vector_schema():
    # Prevent a pgvector table from being silently created in the business schema
    # while the report and readonly grants point at a different vector schema.
    with pytest.raises(ValueError, match="同一新隔离 schema"):
        _validate_target_schemas("business_schema", "vector_schema")


def test_readonly_role_has_usage_on_separate_pgvector_extension_schema():
    assert _reader_usage_schemas(
        "fresh_cs12_v01", "fresh_cs12_v01", "cross_source_demo"
    ) == ("fresh_cs12_v01", "cross_source_demo")
