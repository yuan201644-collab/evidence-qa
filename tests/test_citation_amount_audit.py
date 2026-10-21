from __future__ import annotations

from app.contracts import DocRef
from app.rag.answer import RagAnswerTool, _audit_cited_currency_amounts
from app.rag.schemas import Candidate, Chunk


class _FixedAnswerLLM:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    def chat(self, _system_prompt: str, _user_prompt: str) -> str:
        return self.answer


def _context(text: str, doc_id: str = "AO01"):
    chunk = Chunk(
        doc_id=doc_id,
        text=text,
        page=1,
        char_start=0,
        char_end=len(text),
        doc_title=doc_id,
    )
    ref = DocRef(
        doc_id=doc_id,
        doc_title=doc_id,
        snippet=text,
        page=1,
        char_start=0,
        char_end=len(text),
        bboxes=[],
        extraction_method="plain",
    )
    return Candidate(chunk=chunk, score=1.0, method="test"), ref


def test_stage22_ao_q08_wrong_low_price_citation_is_flagged() -> None:
    # Stage22 RGV AO-Q08 repeat 3: the amount 931.02 万元/年 was attached
    # to an AO01 low-price review passage. The correct budget excerpt was in
    # candidate context but absent from the answer's cited sources.
    answer = "年度预算金额为931.02万元/年，且超过预算的投标无效[2]。"
    cited_contexts = [
        "中标金额：928.0000000 万元/年。",
        "异常低价评审：评审委员会应对明显低价报价进行审查。",
    ]

    audit = _audit_cited_currency_amounts(answer, cited_contexts)

    assert audit["status"] == "warning"
    assert audit["unmatched"] == [{
        "amount": "931.02万元/年",
        "citation_indices": [2],
        "matched_citation_indices": [],
        "status": "unmatched",
    }]


def test_stage22_ao_q08_correct_budget_citation_matches() -> None:
    answer = "年度金额上限为 931.02 万元/年[1]。"
    cited_contexts = ["项目预算金额：931.02 万元/年。"]

    audit = _audit_cited_currency_amounts(answer, cited_contexts)

    assert audit["status"] == "checked"
    assert audit["unmatched"] == []
    assert audit["checked_count"] == 1


def test_currency_amount_equivalence_and_non_applicable_claims() -> None:
    equivalent = _audit_cited_currency_amounts(
        "项目预算为 931.02 万元/年[1]。",
        ["项目预算为 9,310,200 元/年。"],
    )
    no_amount = _audit_cited_currency_amounts("项目合同已签订[1]。", ["合同已签订。"])

    assert equivalent["status"] == "checked"
    assert equivalent["unmatched"] == []
    assert no_amount["status"] == "not_applicable"


def test_generation_exposes_and_warns_about_unmatched_amount_citation() -> None:
    amount_text = (
        "年度预算金额为931.02万元/年，且超过预算的投标无效[2]。"
        "复述年度上限为931.02万元/年[2]。"
    )
    winner_text = "中标金额：928.0000000 万元/年。"
    wrong_source_text = "异常低价评审：评审委员会应对明显低价报价进行审查。"
    correct_source_text = "项目预算金额：931.02 万元/年。"
    rag = RagAnswerTool(
        bm25=object(),
        store=object(),
        embedder=object(),
        llm=_FixedAnswerLLM(amount_text),
        reranker=object(),
    )

    # The correct budget excerpt is present in generation context as [3],
    # but the answer cites [2], the low-price review excerpt. The audit must
    # inspect final cited evidence, not merely search every available chunk.
    generated = rag.generate("年度金额上限是多少？", [
        _context(winner_text, "AO02"),
        _context(wrong_source_text, "AO01"),
        _context(correct_source_text, "AO01"),
    ])
    trace = rag.generate_step("年度金额上限是多少？", generated, step_id=1)

    assert "引用核验提示" in generated["text"]
    warning_text = generated["text"].split("引用核验提示：", 1)[1]
    assert warning_text.count("931.02万元/年") == 1
    assert generated["citation_amount_audit"]["status"] == "warning"
    assert trace.output["citation_amount_audit"]["unmatched"][0]["citation_indices"] == [1]
    assert generated["final_refs"][0].snippet == wrong_source_text
