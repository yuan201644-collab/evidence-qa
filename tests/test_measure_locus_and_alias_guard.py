"""2026-10-17 三处判据修复的回归锁。

三处都是「跑真实数据时暴露、但单测从未覆盖」的缺陷，各自对应一个可复现的最小输入：

1. `measure_locus` —— 「中标金额的平均值」被澄清器当缺对象追问。
   判据不能用词表（政采/Chinook/AdventureWorks 各套说法，穷举必漏），
   改用能力层的列说明做落点核对。
2. 输出别名豁免 —— `SELECT x AS y ... GROUP BY t.y` 里带前缀的 `t.y`
   被误当成输出别名 y 放过，一路执行到数据库才报错。
3. 纯聚合 + 多余 GROUP BY —— `COUNT(DISTINCT x) ... GROUP BY x` 返回
   每个分组一行（恒为 1），与统计总数的语义相反。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from app.nl2sql.capability_runtime import load_capability
from app.nl2sql.normalizer import normalize
from app.nl2sql.schema import SchemaCatalog
from app.nl2sql.validator import validate_sql
from app.query_requirements import extract_requirements

ROOT = Path(__file__).resolve().parents[1]
GOV_SCHEMA = ROOT / "infra" / "schema_meta_gov.json"


@pytest.fixture(scope="module")
def gov_catalog() -> SchemaCatalog:
    if not GOV_SCHEMA.is_file():
        pytest.skip("政采库结构文件不存在")
    previous = os.environ.get("SCHEMA_META_PATH")
    os.environ["SCHEMA_META_PATH"] = str(GOV_SCHEMA)
    try:
        yield SchemaCatalog.load(str(GOV_SCHEMA))
    finally:
        if previous is None:
            os.environ.pop("SCHEMA_META_PATH", None)
        else:
            os.environ["SCHEMA_META_PATH"] = previous


def _locus(question: str, capability) -> bool:
    normalized = normalize(question, time_anchor=datetime(2026, 10, 17))
    requirements = extract_requirements(
        normalized.normalized, normalized=normalized, capability=capability
    )
    return requirements.measure_locus


@pytest.mark.parametrize(
    "question",
    [
        "按采购方式分组，各自的中标金额平均值是多少",
        "各项目预算金额合计是多少",
        "中标金额是多少？",
    ],
)
def test_measure_with_explicit_modifier_is_located(gov_catalog: SchemaCatalog, question: str):
    """指标词带限定语（中标金额/预算金额）＝已点名到具体列，不该再追问对象。"""
    capability = load_capability(gov_catalog)
    assert _locus(question, capability) is True


@pytest.mark.parametrize(
    "question",
    [
        "有多少位？",
        "一共有多少？",
        "数量是多少？",
        "金额是多少？",
    ],
)
def test_bare_measure_still_needs_object(gov_catalog: SchemaCatalog, question: str):
    """裸指标词（数量/金额）单独出现时用户没说数谁的数量——必须追问。

    这条是 SC-02 那批真缺陷的护栏：放宽判据时若把它放行，
    等于把「有多少位？」这种真缺对象的问题也当正常问题处理。
    """
    capability = load_capability(gov_catalog)
    assert _locus(question, capability) is False


def test_qualified_column_shadowing_output_alias_is_rejected(gov_catalog: SchemaCatalog):
    """带表前缀的列引用不可能指代输出别名，别名同名不能成为放行理由。"""
    sql = (
        "SELECT i.unit_price_yuan AS unit_price FROM items i "
        "GROUP BY i.unit_price ORDER BY i.unit_price DESC"
    )
    result = validate_sql(sql, catalog=gov_catalog)
    assert result.ok is False
    assert "unit_price" in (result.error or "")


def test_bare_output_alias_in_order_by_is_accepted(gov_catalog: SchemaCatalog):
    """无前缀的 ORDER BY / GROUP BY 引用输出别名是合法写法，不能被新判据误杀。"""
    sql = (
        "SELECT a.award_yuan AS amount FROM awards a "
        "WHERE a.award_yuan > 0 ORDER BY amount DESC LIMIT 5"
    )
    assert validate_sql(sql, catalog=gov_catalog).ok is True

    grouped = (
        "SELECT p.procurement_way AS way, count(*) AS n FROM projects p "
        "GROUP BY way ORDER BY n DESC"
    )
    assert validate_sql(grouped, catalog=gov_catalog).ok is True


def test_scalar_aggregate_with_redundant_group_by_is_rejected(gov_catalog: SchemaCatalog):
    """SELECT 全聚合却又 GROUP BY：结果集应是 1 行却变成每分组一行。"""
    sql = (
        "SELECT count(DISTINCT project_id) AS project_count FROM projects "
        "WHERE first_announce_date >= DATE '2026-10-01' GROUP BY project_id"
    )
    result = validate_sql(sql, catalog=gov_catalog)
    assert result.ok is False
    assert "GROUP BY" in (result.error or "")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT procurement_way, count(*) AS n FROM projects GROUP BY procurement_way",
        "SELECT count(DISTINCT project_id) AS n FROM announcements",
        (
            "SELECT procurement_way, count(*) AS n FROM projects "
            "GROUP BY procurement_way HAVING count(*) > 5"
        ),
        (
            "SELECT p.procurement_way AS way, count(*) AS n FROM projects p "
            "GROUP BY way"
        ),
    ],
)
def test_legitimate_group_by_is_accepted(gov_catalog: SchemaCatalog, sql: str):
    """正常分组、全局聚合、带 HAVING 的分组、按输出别名分组都不该被拦。"""
    assert validate_sql(sql, catalog=gov_catalog).ok is True
