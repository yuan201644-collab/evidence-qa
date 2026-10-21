from app.nl2sql.schema import ColumnMeta, SchemaCatalog, TableMeta
from app.nl2sql.validator import validate_sql


def _catalog() -> SchemaCatalog:
    return SchemaCatalog(
        [
            TableMeta(
                name="artist",
                schema="public",
                columns=(ColumnMeta("artist_id", "integer", nullable=False),),
            ),
            TableMeta(
                name="sales_scale_1000",
                schema="scale",
                columns=(ColumnMeta("sale_id", "bigint", nullable=False),),
            ),
        ],
        database="chinook",
        dialect="postgres",
    )


def test_unique_non_public_table_is_qualified_from_schema_catalog() -> None:
    result = validate_sql(
        "SELECT COUNT(*) AS total_rows FROM sales_scale_1000",
        _catalog(),
        auto_limit=0,
    )

    assert result.ok, result.error
    assert result.sql == "SELECT COUNT(*) AS total_rows FROM scale.sales_scale_1000"
    assert result.tables == ["scale.sales_scale_1000"]
    assert any("scale.sales_scale_1000" in note for note in result.notes)


def test_public_table_sql_remains_unchanged() -> None:
    statement = "SELECT artist_id FROM artist"
    result = validate_sql(statement, _catalog(), auto_limit=0)

    assert result.ok, result.error
    assert result.sql == statement
    assert result.tables == ["artist"]
    assert result.notes == []


def test_ambiguous_unqualified_table_is_rejected_not_guessed() -> None:
    catalog = SchemaCatalog(
        [
            TableMeta(name="items", schema="public", columns=(ColumnMeta("id", "integer"),)),
            TableMeta(name="items", schema="scale", columns=(ColumnMeta("id", "integer"),)),
        ],
        database="test",
        dialect="postgres",
    )
    result = validate_sql("SELECT id FROM items", catalog, auto_limit=0)

    assert not result.ok
    assert "不存在的表" in result.error


def test_explicit_schema_is_preserved() -> None:
    statement = "SELECT sale_id FROM scale.sales_scale_1000"
    result = validate_sql(statement, _catalog(), auto_limit=0)

    assert result.ok, result.error
    assert result.sql == statement
    assert result.tables == ["scale.sales_scale_1000"]
