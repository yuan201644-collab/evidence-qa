"""S2-F F4/F5：继承条件进生成提示词 + 核验卡继承来源标注。

## F4 为什么生成器也要看要求清单

清单此前只喂给校验器：校验器能发现「继承来的『巴西』没写进 SQL」，
但生成器根本不知道有这条要求——修复重试时它没有依据可改，只会再猜
一次。把要求前置到生成期（`【用户要求（必须落实）】`块），才是
"让模型第一次就写对"；校验器退回兜底位。

## F5 为什么继承条要在核验卡标注来源

用户看到「巴西」未落实被标黄时，需要知道这个条件**不是本轮新提的**、
而是上一轮对话延续下来的——否则继承机制本身会被当成系统凭空加条件
（与 SC-33 凭空过滤混淆，方向正好相反）。
"""

from __future__ import annotations

from app.condition_archive import merge_condition_archive
from app.fact_review import _requirement_coverage_check
from app.nl2sql.generator import build_prompts, requirements_prompt
from app.nl2sql.normalizer import normalize
from app.nl2sql.planning_models import QueryRequirements
from app.query_requirements import REQUIREMENTS_VERSION, extract_requirements


class _TinyCatalog:
    """build_prompts 只调 render_ddl；不需要真库结构。"""

    def render_ddl(self, table_names=None):
        return "CREATE TABLE invoice (billing_country TEXT, total NUMERIC);"


def _inherited_brazil() -> QueryRequirements:
    """构造「上一轮：只看巴西的呢？」留下的条件档案 → 继承清单。"""
    first = extract_requirements("只看巴西的呢？", normalize("只看巴西的呢？"))
    archive = merge_condition_archive(
        None, first, question="只看巴西的呢？", is_followup=False,
    )
    return QueryRequirements(
        version=REQUIREMENTS_VERSION,
        original="",
        normalized_question="",
        requirements=archive.active_requirements,
    )


def _turn2_with_inheritance() -> QueryRequirements:
    """接续轮「那发票有多少张？」：本轮对象 invoice + 继承巴西。"""
    return extract_requirements(
        "那发票有多少张？", normalize("那发票有多少张？"),
        inherited=_inherited_brazil(),
    )


# ── F4：生成提示词 ──────────────────────────────────────────────────


def test_inherited_value_filter_appears_in_generation_prompt():
    """继承条（source=session）必须出现在【用户要求】块里，且标注继承来源。"""
    system, user = build_prompts("那发票有多少张？", _TinyCatalog(), requirements=_turn2_with_inheritance())
    assert "【用户要求（必须落实）】" in user
    assert "上一轮继承" in user, "继承条必须显式标注，否则模型当成用户本轮新话"
    assert "Brazil" in user, "取值的规范形式要给出，模型才知道过滤值写什么"


def test_requirements_block_lists_literal_turn_items_too():
    """本轮自己提的要求同样进块——继承与本轮条目一视同仁。"""
    system, user = build_prompts("那发票有多少张？", _TinyCatalog(), requirements=_turn2_with_inheritance())
    assert "业务对象：发票" in user
    assert "取值过滤：巴西" in user


def test_no_requirements_produces_no_block():
    """无清单时不占提示词——不造空块，避免模型把空要求误读成约束。"""
    system, user = build_prompts("平台上有多少客户？", _TinyCatalog())
    assert "【用户要求（必须落实）】" not in user


def test_unresolved_requirements_are_excluded():
    """未解析条目（还不算完整要求）不进生成提示——避免模型对着「它」猜。"""
    reqs = extract_requirements("它的质保期是多久？")
    text = requirements_prompt(reqs)
    for item in reqs.requirements:
        if not item.actionable:
            assert item.surface not in text or text == ""


# ── F5：核验卡继承来源标注 ──────────────────────────────────────────


class _Run:
    def __init__(self, data):
        self.result = type("_TraceCarrier", (), {"data": data, "trace": []})()


def test_inherited_check_detail_marks_session_origin():
    """继承条未落实时，detail 必须带「来自上一轮对话的延续」。"""
    coverage = {
        "status": "unknown",
        "checks": [{
            "requirement_id": "session:value_filter:巴西",
            "kind": "value_filter", "surface": "巴西", "outcome": "unknown",
            "reason_code": "value_without_evidence",
            "message": "问题限定了取值（巴西），但 SQL 没有可证实的对应过滤。",
            "sql_scope": "", "inherited": True,
        }],
    }
    check = _requirement_coverage_check([_Run({"requirement_coverage": coverage})], None)
    assert check is not None and check.result == "unknown"
    assert check.detail is not None
    assert "上一轮对话的延续" in check.detail


def test_non_inherited_check_detail_has_no_session_note():
    """本轮自己的条件未落实时**不**标继承——错标会混淆责任归属。"""
    coverage = {
        "status": "repair",
        "checks": [{
            "requirement_id": "value_filter:0-2",
            "kind": "value_filter", "surface": "巴西", "outcome": "repair",
            "reason_code": "value_without_evidence",
            "message": "问题限定了取值（巴西），但相关表没有对应列。",
            "sql_scope": "", "inherited": False,
        }],
    }
    check = _requirement_coverage_check([_Run({"requirement_coverage": coverage})], None)
    assert check is not None and check.result == "failed"
    assert check.detail is not None
    assert "上一轮对话的延续" not in check.detail


def test_missing_coverage_returns_none():
    """闸门 off / 没跑核对 → 不加检查项（造 not_run 会混淆「没核对」与「没执行」）。"""
    assert _requirement_coverage_check([_Run({})], None) is None
