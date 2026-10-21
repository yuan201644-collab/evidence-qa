from pathlib import Path

from app.clarifier import SemanticClarifier
from app.nl2sql.join_planner import JoinPlanner
from app.nl2sql.linker import SchemaLinker
from app.nl2sql.normalizer import AliasTable, load_alias_table, normalize
from app.nl2sql.planning_models import LinkCandidate, LinkResult
from app.nl2sql.schema import ColumnMeta, ForeignKeyMeta, SchemaCatalog, TableMeta


def _table(name, columns, *, description="", foreign_keys=(), primary_key=()):
    return TableMeta(
        name=name,
        schema="sales",
        description=description,
        columns=tuple(ColumnMeta(column, "text") for column in columns),
        foreign_keys=tuple(foreign_keys),
        primary_key=tuple(primary_key),
    )


def test_column_hit_promotes_existing_low_scoring_table_candidate() -> None:
    catalog = SchemaCatalog([
        _table("purchase_order", ("id", "total_amount"), description="采购订单"),
    ])
    aliases = AliasTable(
        version="test",
        updated_at="test",
        tables={"sales.purchase_order": ["采购"]},
        columns={"public.purchase_order.total_amount": ["采购金额"]},
    )

    result = SchemaLinker(aliases=aliases, descriptions_path="missing-test-descriptions.json").link(
        "采购金额", catalog
    )

    candidate = next(item for item in result.selected_tables if item.table == "purchase_order")
    assert candidate.score_parts.get("owner_of_column", 0) > 0
    assert candidate.score > 0.5


def test_generic_column_owner_does_not_force_an_unrelated_join() -> None:
    catalog = SchemaCatalog([
        _table("customer", ("id", "name")),
        _table("artist", ("id", "name")),
    ])
    customer = LinkCandidate("table", "sales", "customer", None, 0.9,
                             {"exact_or_alias": 0.9}, ("customer",))
    artist_name = LinkCandidate("table", "sales", "artist", None, 0.8,
                                {"owner_of_column": 1.0}, ("name",))
    linked = LinkResult(selected_tables=(customer, artist_name))

    plan = JoinPlanner().plan(linked, catalog)

    assert plan.tables == ("sales.customer",)
    assert not plan.warnings


def test_foreign_key_intermediate_table_is_included_in_path() -> None:
    customer = _table(
        "customer", ("id",), primary_key=("id",),
        foreign_keys=(ForeignKeyMeta(("address_id",), "address", ("id",)),),
    )
    address = _table("address", ("id",), primary_key=("id",))
    order = _table(
        "sales_order", ("id",), primary_key=("id",),
        foreign_keys=(ForeignKeyMeta(("customer_id",), "customer", ("id",)),),
    )
    catalog = SchemaCatalog([customer, address, order])
    linked = LinkResult(selected_tables=(
        LinkCandidate("table", "sales", "sales_order", None, 0.9),
        LinkCandidate("table", "sales", "address", None, 0.9),
    ))

    plan = JoinPlanner().plan(linked, catalog)

    assert plan.tables == ("sales.sales_order", "sales.customer", "sales.address")
    assert len(plan.edges) == 2


def test_intent_object_and_measure_select_a_declared_join_path() -> None:
    customer = _table("customer", ("id", "name"), description="客户主数据", primary_key=("id",))
    invoice = TableMeta(
        name="invoice",
        schema="sales",
        description="发票头表，记录每次结账的金额合计。",
        columns=(
            ColumnMeta("id", "integer", nullable=False, is_primary_key=True),
            ColumnMeta("customer_id", "integer", description="下单客户外键。"),
            ColumnMeta("total", "numeric", description="发票金额合计。"),
        ),
        primary_key=("id",),
        foreign_keys=(ForeignKeyMeta(("customer_id",), "customer", ("id",)),),
    )
    catalog = SchemaCatalog([customer, invoice])
    linked = SchemaLinker(
        aliases=AliasTable(version="test", updated_at="test"),
        descriptions_path="missing-test-descriptions.json",
    ).link("按客户汇总消费金额", catalog, intent={"objects": ["customer"], "measures": ["金额"]})
    plan = JoinPlanner().plan(linked, catalog, target_grain="customer")

    assert {item.table for item in linked.selected_tables} >= {"customer", "invoice"}
    assert set(plan.tables) == {"sales.customer", "sales.invoice"}
    assert len(plan.edges) == 1


def test_two_character_overlap_does_not_select_unrelated_tables() -> None:
    catalog = SchemaCatalog([
        _table("customer", ("id",), description="客户主数据"),
        _table("invoice", ("id",), description="发票表"),
    ])
    linked = SchemaLinker(
        aliases=AliasTable(version="test", updated_at="test"),
        descriptions_path="missing-test-descriptions.json",
    ).link("Chinook 数据库中一共有多少位客户？", catalog)

    assert all(item.table != "invoice" for item in linked.selected_tables)


def test_sidecar_column_description_and_measure_select_invoice_total(tmp_path) -> None:
    descriptions = tmp_path / "descriptions.json"
    descriptions.write_text(
        '{"database":"chinook","contract_version":"1.1.1",'
        '"tables":{"invoice":"发票头表，total 按销售额统计"},'
        '"columns":{"invoice.total":"销售额及营收统计口径"}}',
        encoding="utf-8",
    )
    catalog = SchemaCatalog(
        [_table("invoice", ("invoice_id", "total"))],
        database="chinook",
        contract_version="1.1.1",
    )

    linked = SchemaLinker(
        aliases=AliasTable(version="test", updated_at="test"),
        descriptions_path=descriptions,
    ).link("销售额", catalog, intent={"measures": ["销售额"]})
    plan = JoinPlanner().plan(linked, catalog)

    invoice = next(item for item in linked.selected_tables if item.table == "invoice")
    total = next(item for item in linked.selected_columns if item.column == "total")
    assert invoice.score_parts["intent_measure_description"] == 0.72
    assert total.score_parts["intent_measure_description"] == 0.78
    assert plan.tables == ("sales.invoice",)


def test_default_chinook_sidecar_is_loaded_without_environment_override() -> None:
    catalog = SchemaCatalog(
        [_table("invoice", ("total",))],
        database="chinook",
        contract_version="1.1.1",
    )
    linked = SchemaLinker(
        aliases=AliasTable(version="test", updated_at="test"),
        descriptions_path=None,
    ).link("按销售额计算", catalog, intent={"measures": ["销售额"]})

    invoice = next(item for item in linked.selected_tables if item.table == "invoice")
    total = next(item for item in linked.selected_columns if item.column == "total")
    assert invoice.score_parts["intent_measure_description"] == 0.72
    assert total.score_parts["intent_measure_description"] == 0.78


def test_database_specific_alias_and_description_sidecars_are_discovered(tmp_path, monkeypatch) -> None:
    schema_path = tmp_path / "schema_meta.adventureworks.json"
    schema_path.write_text(
        '{"database":"adventureworks","contract_version":"1.1.1"}',
        encoding="utf-8",
    )
    (tmp_path / "schema_meta.adventureworks.aliases.zh.json").write_text(
        '{"version":"test","updated_at":"test",'
        '"tables":{"sales.SalesReason":["销售原因"],'
        '"sales.SalesOrderHeaderSalesReason":["销售原因"]},'
        '"columns":{},"values":{},"docs":{}}',
        encoding="utf-8",
    )
    (tmp_path / "schema_meta.adventureworks.descriptions.zh.json").write_text(
        '{"database":"adventureworks","contract_version":"1.1.1",'
        '"tables":{"SalesReason":"销售原因字典。"},"columns":{}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("SCHEMA_META_PATH", str(schema_path))
    monkeypatch.delenv("SCHEMA_META_ALIASES_PATH", raising=False)
    monkeypatch.delenv("SCHEMA_META_DESCRIPTIONS_PATH", raising=False)
    aliases = load_alias_table()
    catalog = SchemaCatalog(
        [
            TableMeta("SalesReason", "sales", columns=(ColumnMeta("ReasonType", "text"),)),
            TableMeta("SalesOrderHeaderSalesReason", "sales", columns=(ColumnMeta("SalesOrderID", "integer"),)),
        ],
        database="adventureworks",
        contract_version="1.1.1",
        source_path=schema_path,
    )

    linker = SchemaLinker(aliases=aliases, descriptions_path=None)
    linked = linker.link("按销售原因类型统计不同订单数", catalog)

    assert {item.table for item in linked.selected_tables} == {
        "SalesReason", "SalesOrderHeaderSalesReason"
    }
    assert linker._descriptions(catalog)["salesreason"] == "销售原因字典。"


def test_output_column_formatting_instruction_does_not_pollute_link_candidates() -> None:
    catalog = SchemaCatalog([
        TableMeta("StateProvince", "person", description="州省行政区", columns=(
            ColumnMeta("StateProvinceID", "integer"), ColumnMeta("Name", "text"),
        )),
        TableMeta("SalesTaxRate", "sales", description="州省销售税率", columns=(
            ColumnMeta("StateProvinceID", "integer"), ColumnMeta("TaxRate", "numeric"),
        )),
        TableMeta("Employee", "hr", description="员工主数据", columns=(
            ColumnMeta("Name", "text"),
        )),
    ], database="adventureworks", contract_version="1.1.1")
    aliases = AliasTable(
        version="test", updated_at="test",
        tables={"person.StateProvince": ["州省"], "sales.SalesTaxRate": ["销售税率"]},
        columns={"StateProvince.Name": ["州省名称"], "SalesTaxRate.TaxRate": ["销售税率"]},
    )

    linked = SchemaLinker(
        aliases=aliases, descriptions_path="missing-test-descriptions.json"
    ).link(
        "按州省统计销售税率平均值。\n输出列名必须严格按以下次序使用：`state_province_id`、`name`、`avg_tax_rate`。",
        catalog,
    )

    assert not any(
        item.table == "Employee" and item.column == "Name"
        for item in linked.selected_columns
    )


def test_specific_date_alias_suppresses_weak_overlap_on_other_startdate_columns(monkeypatch) -> None:
    question = (
        "以 2013 年 6 月 30 日为截点，按部门统计当日仍有效的部门任职历史所涉及的不同人员数。"
        "任职开始日期须不晚于截点；任职终止日为空或晚于截点。"
    )
    _, linked, plan = _adventureworks_plan(monkeypatch, question)

    assert not any(item.table == "BillOfMaterials" for item in linked.selected_tables)
    assert set(plan.tables) == {
        "humanresources.department",
        "humanresources.employeedepartmenthistory",
    }
    assert not plan.warnings


def _adventureworks_plan(monkeypatch, question: str):
    root = Path(__file__).resolve().parents[1]
    schema_path = root / "infra" / "schema_meta.adventureworks.json"
    aliases_path = root / "infra" / "schema_meta.adventureworks.aliases.zh.json"
    descriptions_path = root / "infra" / "schema_meta.adventureworks.descriptions.zh.json"
    monkeypatch.setenv("SCHEMA_META_PATH", str(schema_path))
    monkeypatch.setenv("SCHEMA_META_ALIASES_PATH", str(aliases_path))
    monkeypatch.setenv("SCHEMA_META_DESCRIPTIONS_PATH", str(descriptions_path))
    catalog = SchemaCatalog.from_file(schema_path)
    normalized = normalize(question)
    decision = SemanticClarifier().decide(normalized.normalized, normalized)
    intent = vars(decision.intent)
    linked = SchemaLinker().link(normalized.normalized, catalog, intent=intent)
    plan = JoinPlanner().plan(
        linked, catalog, target_grain=decision.intent.target_grain
    )
    return normalized, linked, plan


def test_canonical_filter_values_select_their_column_owner_and_join_path(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    schema_path = root / "infra" / "schema_meta.json"
    monkeypatch.setenv("SCHEMA_META_PATH", str(schema_path))
    monkeypatch.setenv("SCHEMA_META_ALIASES_PATH", str(root / "infra" / "schema_meta.aliases.zh.json"))
    monkeypatch.setenv("SCHEMA_META_DESCRIPTIONS_PATH", str(root / "infra" / "schema_meta.descriptions.zh.json"))
    question = "买过 Rock 但从未买过 Jazz 的客户有哪些？返回客户编号，按编号升序，不重复。"

    normalized = normalize(question)
    decision = SemanticClarifier().decide(normalized.normalized, normalized)
    catalog = SchemaCatalog.from_file(schema_path)
    linked = SchemaLinker().link(
        normalized.normalized, catalog, intent=vars(decision.intent)
    )
    plan = JoinPlanner().plan(linked, catalog, target_grain="customer")

    assert any(
        item.table == "genre" and item.column == "name"
        for item in linked.selected_columns
    )
    assert set(plan.tables) == {
        "public.customer",
        "public.invoice",
        "public.invoice_line",
        "public.track",
        "public.genre",
    }
    assert not plan.ambiguous


def test_explicit_column_alias_owners_can_form_a_declared_bridge_path(monkeypatch) -> None:
    normalized, linked, plan = _adventureworks_plan(
        monkeypatch, "按销售原因类型统计关联的不同订单数。"
    )

    assert "ReasonType" in normalized.normalized
    assert "SalesOrderID" in normalized.normalized
    assert {item.qualified_name for item in linked.selected_tables
            if "explicit_column_alias" in item.score_parts} >= {
        "Sales.SalesReason", "Sales.SalesOrderHeaderSalesReason"
    }
    assert set(plan.tables) == {
        "sales.salesreason", "sales.salesorderheadersalesreason"
    }
    assert len(plan.edges) == 1
    assert not plan.ambiguous
    assert not linked.ambiguous_groups


def test_purchase_status_alias_and_measure_columns_plan_header_detail_join(monkeypatch) -> None:
    _, linked, plan = _adventureworks_plan(
        monkeypatch,
        "按采购状态统计明细行的拒收率，定义为拒收数量总和除以订购数量总和。",
    )

    assert {item.qualified_name for item in linked.selected_tables
            if item.score >= 0.5} >= {
        "Purchasing.PurchaseOrderHeader", "Purchasing.PurchaseOrderDetail"
    }
    assert set(plan.tables) == {
        "purchasing.purchaseorderheader", "purchasing.purchaseorderdetail"
    }
    assert len(plan.edges) == 1
    assert not plan.ambiguous


def test_production_identifier_does_not_create_false_product_join(monkeypatch) -> None:
    _, _, plan = _adventureworks_plan(
        monkeypatch,
        "列出 2013 年 1 月 1 日仍然有效的 BOM 关系；StartDate 含当天，EndDate 不含当天，EndDate 为空表示持续有效。",
    )

    assert plan.tables == ("production.billofmaterials",)
    assert not plan.edges
    assert not plan.ambiguous


def test_department_start_date_alias_does_not_select_bom_history(monkeypatch) -> None:
    _, _, plan = _adventureworks_plan(
        monkeypatch,
        "按部门实体统计 2013 年内开始在该部门任职的不同员工人数。员工可以在公司更早入职，也可能调入新部门；本题按部门归属开始日期统计，不按公司入职日期统计。部门以 DepartmentID 区分，即使名称相同也分别统计。",
    )

    assert set(plan.tables) == {
        "humanresources.department",
        "humanresources.employeedepartmenthistory",
        "humanresources.employee",
    }
    assert not plan.ambiguous
    assert not any("billofmaterials" in warning for warning in plan.warnings)
