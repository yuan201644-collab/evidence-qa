from app.nl2sql.pipeline import build_sql_refs
from app.nl2sql.schema import ColumnMeta, SchemaCatalog, TableMeta
from eval.cross_source_runner import _required_sql_refs_match


def _catalog(table_name, columns, primary_key):
    return SchemaCatalog([
        TableMeta(
            name=table_name,
            primary_key=tuple(primary_key),
            columns=tuple(ColumnMeta(name, "text") for name in columns),
        )
    ])


def test_build_sql_refs_bind_all_projected_real_columns_and_values():
    catalog = _catalog(
        "work_orders",
        ["work_order_id", "equipment_id", "opened_at"],
        ["work_order_id"],
    )
    rows = [{
        "WORK_ORDER_ID": "WO-TEST-1",
        "equipment_id": "DEV-TEST-1",
        "opened_at": "2032-06-29T08:55:00+08:00",
    }]

    refs = build_sql_refs(
        ["work_orders"], catalog, rows,
        ["WORK_ORDER_ID", "equipment_id", "opened_at", "generated_alias"],
        sql="SELECT work_order_id AS WORK_ORDER_ID, equipment_id, opened_at FROM work_orders",
    )

    assert len(refs) == 1
    ref = refs[0].model_dump(mode="json")
    assert ref["column"] == "work_order_id/equipment_id/opened_at"
    assert ref["row_pk"] == "work_order_id=WO-TEST-1"
    assert ref["snippet"].endswith(
        '[work_order_id="WO-TEST-1";equipment_id="DEV-TEST-1";'
        'opened_at="2032-06-29T08:55:00+08:00"]'
    )
    requirements = [{
        "table": "work_orders",
        "row_pk": "work_order_id=WO-TEST-1",
        "required_columns": ["work_order_id", "equipment_id", "opened_at"],
        "required_values": {
            "work_order_id": "WO-TEST-1",
            "equipment_id": "DEV-TEST-1",
            "opened_at": "2032-06-29T08:55:00+08:00",
        },
    }]
    assert _required_sql_refs_match([ref], requirements)


def test_build_sql_refs_handles_case_insensitive_composite_primary_keys():
    catalog = _catalog(
        "work_order_events",
        ["work_order_id", "event_type", "event_at"],
        ["work_order_id", "event_type"],
    )
    rows = [{
        "Work_Order_Id": "WO-TEST-2",
        "EVENT_TYPE": "accepted",
        "Event_At": "2032-06-29T09:20:00+08:00",
    }]

    refs = build_sql_refs(
        ["work_order_events"], catalog, rows,
        ["work_order_id", "event_type", "event_at"],
        sql="SELECT work_order_id, event_type, event_at FROM work_order_events",
    )

    assert refs[0].row_pk == "work_order_id=WO-TEST-2&event_type=accepted"
    assert refs[0].column == "work_order_id/event_type/event_at"


def test_build_sql_refs_does_not_claim_a_partial_composite_key():
    catalog = _catalog(
        "work_order_events",
        ["work_order_id", "event_type", "event_at"],
        ["work_order_id", "event_type"],
    )

    refs = build_sql_refs(
        ["work_order_events"], catalog,
        [{"work_order_id": "WO-TEST-3", "event_at": "2032-06-29T09:20:00+08:00"}],
        ["work_order_id", "event_at"],
        sql="SELECT work_order_id, event_at FROM work_order_events",
    )

    assert len(refs) == 1
    assert refs[0].row_pk is None
    assert refs[0].column == "work_order_id/event_at"


def test_build_sql_refs_does_not_claim_ambiguous_join_column_ownership():
    catalog = SchemaCatalog([
        TableMeta(
            name="work_orders",
            primary_key=("work_order_id",),
            columns=(
                ColumnMeta("work_order_id", "text"),
                ColumnMeta("opened_at", "text"),
            ),
        ),
        TableMeta(
            name="work_order_events",
            primary_key=("work_order_id", "event_type"),
            columns=(
                ColumnMeta("work_order_id", "text"),
                ColumnMeta("event_type", "text"),
            ),
        ),
    ])

    refs = build_sql_refs(
        ["work_orders", "work_order_events"], catalog,
        [{
            "work_order_id": "WO-TEST-4",
            "opened_at": "2032-06-29T08:55:00+08:00",
            "event_type": "accepted",
        }],
        ["work_order_id", "opened_at", "event_type"],
        sql=("SELECT e.event_type AS work_order_id, w.opened_at, e.event_type "
             "FROM work_orders AS w JOIN work_order_events AS e "
             "ON e.work_order_id = w.work_order_id"),
    )

    assert [ref.table for ref in refs] == ["work_orders", "work_order_events"]
    assert all(ref.row_pk is None for ref in refs)
    assert all(ref.column is None for ref in refs)


def test_build_sql_refs_does_not_treat_aggregate_alias_as_primary_key():
    catalog = _catalog("work_orders", ["work_order_id", "equipment_id"], ["work_order_id"])
    refs = build_sql_refs(
        ["work_orders"], catalog, [{"work_order_id": 12}], ["work_order_id"],
        sql="SELECT COUNT(*) AS work_order_id FROM work_orders",
    )

    assert len(refs) == 1
    assert refs[0].row_pk is None
    assert refs[0].column is None
    assert "computed-value" not in refs[0].snippet
    assert "work_order_id" not in refs[0].snippet
    assert "12" not in refs[0].snippet


def test_build_sql_refs_does_not_treat_expression_alias_as_source_column():
    catalog = _catalog("equipment", ["equipment_id", "equipment_name"], ["equipment_id"])
    refs = build_sql_refs(
        ["equipment"], catalog, [{"equipment_id": "DEV-1"}], ["equipment_id"],
        sql="SELECT UPPER(equipment_name) AS equipment_id FROM equipment",
    )

    assert refs[0].row_pk is None
    assert refs[0].column is None


def test_build_sql_refs_invalidates_a_label_shared_by_direct_and_expression_projection():
    catalog = _catalog("work_orders", ["work_order_id", "equipment_id"], ["work_order_id"])
    refs = build_sql_refs(
        ["work_orders"], catalog, [{"work_order_id": "computed-value"}], ["work_order_id"],
        sql=("SELECT work_order_id, UPPER(work_order_id) AS work_order_id "
             "FROM work_orders"),
    )

    assert refs[0].row_pk is None
    assert refs[0].column is None
    assert "computed-value" not in refs[0].snippet


def test_build_sql_refs_accepts_direct_primary_key_alias():
    catalog = _catalog("work_orders", ["work_order_id", "equipment_id"], ["work_order_id"])
    refs = build_sql_refs(
        ["work_orders"], catalog,
        [{"wo_id": "WO-TEST-5", "equipment_id": "DEV-TEST-5"}],
        ["wo_id", "equipment_id"],
        sql="SELECT work_order_id AS wo_id, equipment_id FROM work_orders",
    )

    assert refs[0].row_pk == "work_order_id=WO-TEST-5"
    assert refs[0].column == "work_order_id/equipment_id"


def test_build_sql_refs_supports_qualified_star_projection():
    catalog = _catalog("work_orders", ["work_order_id", "equipment_id"], ["work_order_id"])
    refs = build_sql_refs(
        ["work_orders"], catalog,
        [{"work_order_id": "WO-TEST-6", "equipment_id": "DEV-TEST-6"}],
        ["work_order_id", "equipment_id"],
        sql="SELECT w.* FROM work_orders AS w",
    )

    assert refs[0].row_pk == "work_order_id=WO-TEST-6"
    assert refs[0].column == "work_order_id/equipment_id"


def test_build_sql_refs_rejects_delimiter_ambiguous_primary_key_value():
    catalog = _catalog("work_orders", ["work_order_id"], ["work_order_id"])
    for value in ("WO&TEST", "WO|TEST", "", None, "X" * 61):
        refs = build_sql_refs(
            ["work_orders"], catalog, [{"work_order_id": value}], ["work_order_id"],
            sql="SELECT work_order_id FROM work_orders",
        )
        assert len(refs) == 1
        assert refs[0].row_pk is None


def test_build_sql_refs_rejects_unescaped_primary_key_field_name():
    for separator in "&|=":
        field_name = f"key{separator}part"
        catalog = _catalog("odd_table", [field_name], [field_name])
        refs = build_sql_refs(
            ["odd_table"], catalog, [{field_name: "VALUE"}], [field_name],
            sql=f'SELECT "{field_name}" FROM odd_table',
        )
        assert refs[0].row_pk is None


def test_build_sql_refs_does_not_emit_row_key_when_pk_is_beyond_bounded_preview():
    catalog = _catalog(
        "work_orders", ["c1", "c2", "c3", "c4", "c5", "c6", "work_order_id"],
        ["work_order_id"],
    )
    row = {"c1": "1", "c2": "2", "c3": "3", "c4": "4", "c5": "5",
           "c6": "6", "work_order_id": "WO-TEST-7"}
    refs = build_sql_refs(
        ["work_orders"], catalog, [row], list(row),
        sql="SELECT c1, c2, c3, c4, c5, c6, work_order_id FROM work_orders",
    )

    assert refs[0].row_pk is None
    assert refs[0].column == "c1/c2/c3/c4/c5/c6"


def test_build_sql_refs_limits_exact_value_tags_to_previewed_short_fields():
    catalog = _catalog(
        "wide_table",
        ["id", "c1", "c2", "c3", "c4", "c5", "c6", "private_note"],
        ["id"],
    )
    long_value = "x" * 100
    refs = build_sql_refs(
        ["wide_table"], catalog,
        [{"id": "ROW-1", "c1": "1", "c2": "2", "c3": "3", "c4": "4",
          "c5": "5", "c6": "6", "private_note": long_value}],
        ["id", "c1", "c2", "c3", "c4", "c5", "c6", "private_note"],
        sql=("SELECT id, c1, c2, c3, c4, c5, c6, private_note "
             "FROM wide_table"),
    )

    assert refs[0].column == "id/c1/c2/c3/c4/c5"
    assert "private_note" not in refs[0].column
    assert long_value not in refs[0].snippet
