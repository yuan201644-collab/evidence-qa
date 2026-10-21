"""capability_runtime 的单元测试。不连库、不调模型、无 IO。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.nl2sql.capability_runtime import (
    DEFAULT_DESCRIPTIONS_PATH,
    RuntimeCapability,
    load_capability,
    load_descriptions,
)
from app.nl2sql.schema import SchemaCatalog

#: 工程根：tests/test_capability_runtime.py -> tests -> 工程根
REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_META = REPO_ROOT / "infra" / "schema_meta.json"


def _capability() -> RuntimeCapability:
    catalog = SchemaCatalog.from_file(REAL_META)
    return load_capability(catalog)


# ── 真实 Chinook 元数据上的判定 ─────────────────────────────────────────
def test_customer_has_no_time_column():
    """SC-05 的核心依据：customer 表没有任何时间列。

    `2023年有多少客户？` 因此必须被拦（repair），而不是丢条件算全量。
    """
    cap = _capability()
    assert cap.time_columns("customer") == ()
    assert cap.entity_grain("customer") == "one_row_per_customer_id"


def test_invoice_time_column_is_invoice_date():
    cap = _capability()
    assert cap.time_columns("invoice") == ("invoice_date",)
    # 「按月份/季度汇总时需先截断到月」这条说明必须被能力层读到，
    # 否则无法解释为什么月份统计要先 DATE_TRUNC。
    assert "截断" in cap.business_meaning("invoice", "invoice_date")


def test_employee_history_is_not_supported():
    """hire_date 只说明何时入职，不能证明现在还在职。

    这是 capability 层最容易写错的一处：把 hire_date 当时间列是对的，
    当历史依据是错的。
    """
    cap = _capability()
    assert cap.supports_history("employee") is False
    assert "hire_date" in cap.time_columns("employee")
    assert "birth_date" in cap.time_columns("employee")


def test_invoice_total_is_the_revenue_measure():
    """§5.4「按发票头统计销售额」的判据来自 total 的说明原文。"""
    cap = _capability()
    meaning = cap.business_meaning("invoice", "total")
    assert "发票头" in meaning and "销售额" in meaning
    assert "total" in cap.amount_columns("invoice")


def test_billing_country_and_customer_country_are_distinguishable():
    """静态能力目录做不到的那件事（计划 §3.2 论证 1）：两列都能当国家维度，
    但说明原文明确写了「而非客户所在国家」。"""
    cap = _capability()
    billing = cap.business_meaning("invoice", "billing_country")
    customer = cap.business_meaning("customer", "country")
    assert "而非客户所在国家" in billing
    assert "客户所在国家" in customer
    assert "billing_country" in cap.category_columns("invoice")
    assert "country" in cap.category_columns("customer")


def test_postal_code_is_not_a_category_column():
    """邮编不是可分组维度。宁可漏，不可把邮编当国家。"""
    cap = _capability()
    assert "postal_code" not in cap.category_columns("customer")
    assert "billing_postal_code" not in cap.category_columns("invoice")


def test_association_table_has_no_independent_grain():
    """playlist_track 是关联表：对它做「有多少个」会把关联行数当实体数。"""
    cap = _capability()
    assert cap.entity_grain("playlist_track") == "association"
    assert cap.primary_key("playlist_track") == ("playlist_id", "track_id")


def test_normalized_values_come_from_metadata():
    cap = _capability()
    values = cap.normalized_values("customer", "country")
    assert "Argentina" in values and len(values) >= 5
    assert cap.normalized_values("customer", "no_such_column") == ()
    assert cap.normalized_values("no_such_table", "country") == ()


def test_unknown_table_degrades_to_empty():
    """表不存在时全部返回空/unknown，绝不抛异常也绝不返回「有」。"""
    cap = _capability()
    assert cap.primary_key("no_such_table") == ()
    assert cap.entity_grain("no_such_table") == "unknown"
    assert cap.time_columns("no_such_table") == ()
    assert cap.category_columns("no_such_table") == ()
    assert cap.supports_history("no_such_table") is False
    assert cap.business_meaning("no_such_table", "x") == ""


def test_snapshot_is_observational():
    cap = _capability()
    snap = cap.snapshot("customer")
    assert snap["exists"] is True
    assert snap["time_columns"] == []
    assert snap["primary_key"] == ["customer_id"]
    assert snap["column_descriptions"]["country"] == "客户所在国家。销售额按国家汇总时用本字段。"


# ── 第二套 schema：AdventureWorks（证明「不建静态能力目录」是对的）───────
AW_META = REPO_ROOT / "infra" / "schema_meta.adventureworks.json"


def _aw_capability() -> RuntimeCapability:
    if not AW_META.is_file():
        pytest.skip("AdventureWorks 元数据不在本 checkout")
    return load_capability(SchemaCatalog.from_file(AW_META))


def test_aw_sibling_descriptions_are_picked_up():
    """catalog 的 source_path 在 infra/ 下时，必须自动找到兄弟说明文件。

    这条是本模块能替代静态目录的前提：不靠硬编码路径，跟着 catalog 走。
    """
    cap = _aw_capability()
    assert len(cap.descriptions) > 50
    assert cap.descriptions.get("salesorderheader.orderdate")


def test_aw_time_column_is_orderdate():
    cap = _aw_capability()
    assert cap.time_columns("SalesOrderHeader") == ("orderdate",)
    assert "按订单日期筛选" in cap.business_meaning("SalesOrderHeader", "OrderDate")


def test_aw_customer_has_no_time_column_either():
    """与 chinook 一致的结论：客户主表同样没有时间依据。"""
    cap = _aw_capability()
    assert cap.time_columns("Customer") == ()


def test_aw_revenue_measure_is_on_the_detail_table():
    """销售额口径两套 schema 不同：

    - chinook：`invoice.total`（发票头）
    - AdventureWorks：`SalesOrderDetail.LineTotal`（明细行）

    静态目录要把两张表都标对才能覆盖，运行时读说明原文天然分得开。

    ⚠ `LineTotal` 的列说明是「行级 LineTotal 数值」，**不含金额词**，
    所以 `amount_columns` 判不出它 —— 这是刻意的保守（宁可漏、不可猜）。
    真正的口径判据在表级说明「统计销售额时应使用 LineTotal 列」。
    """
    cap = _aw_capability()
    detail = cap.table_meaning("SalesOrderDetail")
    assert "LineTotal" in detail
    # 「统计销售数量时应使用该表」在表级说明里
    assert "统计销售数量时应使用该表" in detail
    # ⚠ LineTotal 的列级说明是「行级 LineTotal 数值」，不含金额词，
    # 所以 amount_columns 判不出它 —— 这是刻意的保守（宁可漏、不可猜）。
    assert "linetotal" not in cap.amount_columns("SalesOrderDetail")
    assert cap.business_meaning("SalesOrderDetail", "LineTotal") == "销售订单明细的行级 LineTotal 数值。"


def test_aw_association_tables_have_no_independent_grain():
    cap = _aw_capability()
    assert cap.entity_grain("SalesOrderDetail") == "association"
    assert cap.entity_grain("EmployeeDepartmentHistory") == "association"
    assert cap.entity_grain("Customer") == "one_row_per_CustomerID"


def test_aw_validity_pair_is_detected():
    """BillOfMaterials 的 StartDate/EndDate 是有效期成对出现的列。"""
    cap = _aw_capability()
    assert set(cap.time_columns("BillOfMaterials")) == {"startdate", "enddate"}
    assert "为空表示未登记结束日期" in cap.business_meaning("BillOfMaterials", "EndDate")


def test_aw_ship_address_differs_from_customer_address():
    """「该地址不同于客户注册地址」—— 与 chinook 的账单国家/客户国家同型的
    口径区分，同样只能从说明原文读出来。"""
    cap = _aw_capability()
    assert "不同于客户注册地址" in cap.business_meaning("SalesOrderHeader", "ShipToAddressID")


def test_aw_history_is_supported_only_by_history_tables():
    """Employee 本身不含任职区间；EmployeeDepartmentHistory 才有。

    能力层如实报告，不替调用方选表。
    """
    cap = _aw_capability()
    assert cap.supports_history("Employee") is False
    assert set(cap.time_columns("EmployeeDepartmentHistory")) == {"startdate", "enddate"}


def test_value_meaning_reports_absence_honestly():
    """元数据没登记取值语义 → 空串，即「没有依据」，不等于通过。"""
    cap = _capability()
    assert cap.value_meaning("customer", "country") == ""
    assert cap.value_meaning("no_such_table", "x") == ""


# ── 缺失说明 → unknown 而非 pass（母方案 §5.2 硬纪律）───────────────────
def test_missing_descriptions_yield_empty_everything():
    catalog = SchemaCatalog.from_file(REAL_META)
    cap = RuntimeCapability(catalog, {})
    for table in catalog.table_names():
        assert cap.time_columns(table) == ()
        assert cap.category_columns(table) == ()
        assert cap.amount_columns(table) == ()
        assert cap.column_descriptions(table) == {}
        assert cap.table_meaning(table) == catalog.get_table(table).description


def test_load_descriptions_handles_both_shapes():
    """columns 段既可能是 dict 也可能是 list（导出脚本两种都产）。"""
    dict_shape = load_descriptions(REAL_META.parent / "schema_meta.descriptions.zh.json")
    assert dict_shape.get("customer.country") == "客户所在国家。销售额按国家汇总时用本字段。"
    assert dict_shape.get("invoice.total") == "发票金额合计，也是按发票头统计销售额/营收时采用的口径。"
    assert load_descriptions(Path("/nonexistent/file.json")) == {}
    assert load_descriptions(None) == {}


def test_manufactured_catalog_without_descriptions_is_all_unknown():
    """手搓小目录（无说明）时，任何能力判定都不能声称支持。"""
    catalog = SchemaCatalog.from_dict({
        "database": "probe",
        "tables": [{
            "table": "thing",
            "description": "东西表",
            "primary_key": ["id"],
            "columns": [
                {"name": "id", "type": "int", "is_primary_key": True},
                {"name": "created_at", "type": "timestamp"},
            ],
        }],
    })
    cap = load_capability(catalog, descriptions_path=Path("/nonexistent"))
    # created_at 列名像时间列，但**没有说明就不能判** —— 这是本层最重要的纪律。
    assert cap.time_columns("thing") == ()
    assert cap.snapshot("thing")["time_columns"] == []
    assert cap.entity_grain("thing") == "one_row_per_id"


def test_default_descriptions_path_points_into_repo():
    assert DEFAULT_DESCRIPTIONS_PATH.is_file()


# ── schema.table 与裸表名必须等价（真实链路上的误报根因）─────────────────
#
# 背景：`schema_linker` / `join_plan` 给的表名是 `public.invoice`
# （qualified_name），而 catalog 与说明文件登记的是裸 `invoice`。
# 早期能力层两处都按原样拼键，于是 `time_columns("public.invoice")`
# 返回空元组 → `_check_time` 的第一层判据判出
# `time_condition_unsupported`（"相关业务表没有可用的时间依据"），
# 而真实 SQL 里 `WHERE invoice_date >= '2023-01-01'` 写得完全正确。
# 一次字符串不匹配会让闸门在**所有带 join_plan 的链路**上系统性误报，
# 故这组断言必须同时覆盖两种写法 —— 只测裸表名是守不住该回归的。
def test_schema_qualified_table_name_is_equivalent_to_bare():
    cap = _capability()
    for method in (
        cap.time_columns,
        cap.category_columns,
        cap.amount_columns,
        cap.quantity_columns,
        cap.primary_key,
        cap.entity_grain,
        cap.supports_history,
    ):
        bare = method("invoice")
        qualified = method("public.invoice")
        assert bare == qualified, f"{method.__name__} 对两种写法给出了不同结果"
    # 非空才有说服力：两边都为 () 时上面的相等是假通过。
    assert cap.time_columns("public.invoice") == ("invoice_date",)


def test_schema_qualified_column_description_is_found():
    """列说明的键是 `表.列`，用带 schema 的表名拼会变成三段、永远查不到。"""
    cap = _capability()
    assert cap.business_meaning("invoice", "invoice_date") == \
        cap.business_meaning("public.invoice", "invoice_date")
    assert "截断" in cap.business_meaning("public.invoice", "invoice_date")


def test_multilevel_schema_prefix_is_stripped():
    """`db.schema.table` 也要能回退到裸表名（只取最后一段）。"""
    cap = _capability()
    assert cap.time_columns("chinook.public.invoice") == ("invoice_date",)


def test_unknown_qualified_name_still_yields_empty():
    """归一化不能变成"什么都认"，不存在的表仍须返回空元组。"""
    cap = _capability()
    assert cap.time_columns("public.no_such_table") == ()
    assert cap.entity_grain("public.no_such_table") == "unknown"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
