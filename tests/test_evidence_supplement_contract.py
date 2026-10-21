from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from app.evidence_supplement import (
    CandidateEvidence,
    CorpusScope,
    EvidenceLocator,
    RelationEdge,
    RelationSource,
    RouteExecution,
    ScopedDocument,
    SupplementRequest,
    SupplementResult,
    validate_supplement_result,
)


CANONICAL_TEXT = "合同金额上限为931.02元。"
ORIGINAL_SHA256 = "a" * 64
TEXT_SHA256 = hashlib.sha256(CANONICAL_TEXT.encode("utf-8")).hexdigest()


def _candidate(
    *,
    candidate_id: str = "cand-1",
    document_id: str = "doc-1",
    original_sha256: str = ORIGINAL_SHA256,
    text_sha256: str = TEXT_SHA256,
    quote: str = CANONICAL_TEXT,
    start: int | None = 0,
    end: int | None = len(CANONICAL_TEXT),
    relation_path: list[RelationEdge] | None = None,
) -> CandidateEvidence:
    return CandidateEvidence(
        candidate_id=candidate_id,
        source_system="test-adapter",
        retrieval_route="hybrid",
        index_chunk_id="chunk-1",
        document_id=document_id,
        original_file_sha256=original_sha256,
        normalized_text_sha256=text_sha256,
        original_locator=EvidenceLocator(coordinate_type="pdf_page", page=1),
        canonical_char_start=start,
        canonical_char_end=end,
        page=1,
        section_path=["预算说明"],
        quote=quote,
        context="",
        score=0.98,
        score_source="test-only score",
        relation_path=relation_path or [],
    )


def _request(
    *,
    rag_candidates: list[CandidateEvidence] | None = None,
    max_candidates: int = 4,
    max_context_chars: int = 1000,
) -> SupplementRequest:
    return SupplementRequest(
        request_id="req-1",
        question="合同金额上限是多少？",
        original_question="这个项目的合同金额上限是多少？",
        fact_targets=[
            {"fact_id": "amount", "target": "合同金额上限", "current_gap": "RAG 片段未包含金额"}
        ],
        corpus_scope=CorpusScope(
            corpus_id="procurement-corpus",
            version="v1",
            documents=[
                ScopedDocument(
                    document_id="doc-1",
                    original_file_sha256=ORIGINAL_SHA256,
                    normalized_text_sha256=TEXT_SHA256,
                )
            ],
        ),
        rag_candidates=rag_candidates or [],
        remaining_budget_ms=10_000,
        max_candidates=max_candidates,
        max_context_chars=max_context_chars,
    )


def _result(*candidates: CandidateEvidence) -> SupplementResult:
    return SupplementResult(
        request_id="req-1",
        status="completed",
        candidates=list(candidates),
        executed_routes=[
            RouteExecution(route="hybrid", status="completed", elapsed_ms=25, candidate_count=len(candidates))
        ],
        elapsed_ms=30,
    )


def test_valid_candidate_matches_scope_and_canonical_text_slice():
    request = _request()
    candidate = _candidate()

    validate_supplement_result(
        request,
        _result(candidate),
        normalized_texts={"doc-1": CANONICAL_TEXT},
    )


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        (_candidate(document_id="other-doc"), "不在 corpus_scope"),
        (_candidate(original_sha256="b" * 64), "原文件哈希"),
        (_candidate(text_sha256="c" * 64), "规范文本版本"),
        (_candidate(quote="错误引文"), "坐标不匹配"),
    ],
)
def test_candidate_with_wrong_identity_version_or_quote_is_rejected(candidate, message):
    request = _request()
    with pytest.raises(ValueError, match=message):
        validate_supplement_result(
            request,
            _result(candidate),
            normalized_texts={"doc-1": CANONICAL_TEXT},
        )


def test_result_must_match_request_id_and_combined_candidate_budget():
    request = _request(
        rag_candidates=[_candidate(candidate_id="rag-1")],
        max_candidates=1,
    )
    with pytest.raises(ValueError, match="request_id"):
        validate_supplement_result(
            request,
            _result(_candidate()).model_copy(update={"request_id": "other-request"}),
        )
    with pytest.raises(ValueError, match="总数超过 max_candidates"):
        validate_supplement_result(request, _result(_candidate()))


def test_combined_context_budget_includes_existing_rag_candidates():
    request = _request(
        rag_candidates=[_candidate(candidate_id="rag-1")],
        max_context_chars=len(CANONICAL_TEXT) * 2 - 1,
    )
    with pytest.raises(ValueError, match="总量超过 max_context_chars"):
        validate_supplement_result(request, _result(_candidate()))


def test_unmapped_candidate_is_retained_but_not_marked_citable():
    candidate = _candidate(start=None, end=None)
    assert candidate.has_canonical_location is False
    validate_supplement_result(_request(), _result(candidate))


def test_graph_relation_without_original_quote_is_not_evidence():
    edge = RelationEdge(subject="预算", predicate="最高限额", object="931.02 元")
    assert edge.has_original_source_quote is False
    validate_supplement_result(_request(), _result(_candidate(relation_path=[edge])))


def test_graph_relation_original_quote_is_checked_against_scope_and_text():
    edge = RelationEdge(
        subject="预算",
        predicate="最高限额",
        object="931.02 元",
        source=RelationSource(
            document_id="doc-1",
            original_file_sha256=ORIGINAL_SHA256,
            normalized_text_sha256=TEXT_SHA256,
            locator=EvidenceLocator(coordinate_type="pdf_page", page=1),
            canonical_char_start=0,
            canonical_char_end=len(CANONICAL_TEXT),
            quote=CANONICAL_TEXT,
        ),
    )
    candidate = _candidate(relation_path=[edge])
    assert edge.has_original_source_quote is True
    validate_supplement_result(
        _request(),
        _result(candidate),
        normalized_texts={"doc-1": CANONICAL_TEXT},
    )

    wrong_edge = edge.model_copy(update={
        "source": edge.source.model_copy(update={"quote": "931.02元"})
    })
    with pytest.raises(ValueError, match="关系边引文与规范文本坐标不匹配"):
        validate_supplement_result(
            _request(),
            _result(_candidate(relation_path=[wrong_edge])),
            normalized_texts={"doc-1": CANONICAL_TEXT},
        )


def test_strict_contract_rejects_unknown_answer_field_and_invalid_offsets():
    payload = _result(_candidate()).model_dump()
    payload["answer"] = "不能从检索适配器返回业务答案"
    with pytest.raises(ValidationError):
        SupplementResult.model_validate(payload)
    with pytest.raises(ValidationError):
        EvidenceLocator(coordinate_type="char", start=8, end=4)
