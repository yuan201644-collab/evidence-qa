from app.nl2sql.schema import ColumnMeta, SchemaCatalog, TableMeta
from app.nl2sql.validator import validate_sql


def _catalog() -> SchemaCatalog:
    return SchemaCatalog([
        TableMeta(
            name="SalesOrderHeader",
            schema="Sales",
            columns=(
                ColumnMeta("SalesOrderID", "integer", nullable=False),
                ColumnMeta("TotalDue", "numeric", nullable=False),
            ),
        )
    ])


def test_unquoted_adventureworks_identifiers_fail_before_execution() -> None:
    result = validate_sql(
        "SELECT SalesOrderID, TotalDue FROM SalesOrderHeader ORDER BY TotalDue DESC LIMIT 5",
        _catalog(),
        auto_limit=0,
    )

    assert not result.ok
    assert "双引号" in result.error


def test_exact_quoted_adventureworks_identifiers_pass_validation() -> None:
    result = validate_sql(
        'SELECT "SalesOrderID", "TotalDue" FROM "Sales"."SalesOrderHeader" '
        'ORDER BY "TotalDue" DESC LIMIT 5',
        _catalog(),
        auto_limit=0,
    )

    assert result.ok, result.error
    assert result.tables == ["sales.salesorderheader"]
