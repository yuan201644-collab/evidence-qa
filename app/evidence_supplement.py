"""Internal contract for optional retrieval candidates from a supplementer.

This module deliberately contains no KAG/OpenSPG client and is not wired into
the public ask endpoint. It defines the boundary the mainline can validate
before any supplemental candidate is fused with project RAG evidence.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


SupplementStatus = Literal[
    "completed",
    "partial",
    "unavailable",
    "failed",
    "timed_out",
    "not_applicable",
]
RouteName = Literal["vector", "relationship", "hybrid"]
RouteStatus = Literal[
    "completed",
    "partial",
    "unavailable",
    "failed",
    "timed_out",
    "not_run",
]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScopedDocument(StrictModel):
    """A document version that the current request is allowed to search."""

    document_id: str = Field(min_length=1)
    original_file_sha256: str = Field(pattern=_SHA256_RE.pattern)
    normalized_text_sha256: str = Field(pattern=_SHA256_RE.pattern)


class CorpusScope(StrictModel):
    """Authenticated, versioned document scope supplied by the mainline."""

    corpus_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    documents: list[ScopedDocument] = Field(min_length=1)

    @model_validator(mode="after")
    def document_ids_are_unique(self) -> "CorpusScope":
        ids = [document.document_id for document in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("corpus_scope 中 document_id 必须唯一")
        return self


class EvidenceLocator(StrictModel):
    """Coordinates in the source system; canonical offsets are separate."""

    coordinate_type: Literal[
        "char",
        "page_char",
        "pdf_page",
        "pdf_bbox",
        "chunk_offset",
        "unknown",
    ]
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    page: int | None = Field(default=None, ge=1)
    section_path: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def coordinate_range_is_consistent(self) -> "EvidenceLocator":
        if (self.start is None) != (self.end is None):
            raise ValueError("原始坐标 start/end 必须同时提供或同时省略")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("原始坐标区间必须是非空的左闭右开区间")
        if self.coordinate_type == "pdf_page" and self.page is None:
            raise ValueError("pdf_page 坐标必须提供 page")
        return self


class RelationSource(StrictModel):
    """Optional original-text support for one graph relation edge."""

    document_id: str | None = None
    original_file_sha256: str | None = Field(default=None, pattern=_SHA256_RE.pattern)
    normalized_text_sha256: str | None = Field(default=None, pattern=_SHA256_RE.pattern)
    locator: EvidenceLocator | None = None
    canonical_char_start: int | None = Field(default=None, ge=0)
    canonical_char_end: int | None = Field(default=None, ge=0)
    quote: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def source_fields_are_all_or_none(self) -> "RelationSource":
        identity_and_quote = (
            self.document_id,
            self.original_file_sha256,
            self.normalized_text_sha256,
            self.locator,
            self.quote,
        )
        if all(value is None for value in identity_and_quote):
            return self
        if any(value is None for value in identity_and_quote):
            raise ValueError("关系边原文证据必须同时含来源身份、坐标和引文")
        if (self.canonical_char_start is None) != (self.canonical_char_end is None):
            raise ValueError("关系边规范文本 canonical_char_start/end 必须同时提供或同时省略")
        if (
            self.canonical_char_start is not None
            and self.canonical_char_end is not None
            and self.canonical_char_end <= self.canonical_char_start
        ):
            raise ValueError("关系边规范文本区间必须非空")
        return self


class RelationEdge(StrictModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    source: RelationSource = Field(default_factory=RelationSource)

    @property
    def has_original_source_quote(self) -> bool:
        """Whether the adapter supplied a source pointer and quote, not whether it supports the edge."""
        return self.source.document_id is not None


class CandidateEvidence(StrictModel):
    """One retrieved passage with stable source identity and two coordinate systems."""

    candidate_id: str = Field(min_length=1)
    source_system: str = Field(min_length=1)
    retrieval_route: Literal["project_rag", "vector", "relationship", "hybrid"]
    index_chunk_id: str | None = None
    document_id: str = Field(min_length=1)
    original_file_sha256: str = Field(pattern=_SHA256_RE.pattern)
    normalized_text_sha256: str = Field(pattern=_SHA256_RE.pattern)
    original_locator: EvidenceLocator
    canonical_char_start: int | None = Field(default=None, ge=0)
    canonical_char_end: int | None = Field(default=None, ge=0)
    page: int | None = Field(default=None, ge=1)
    section_path: list[str] = Field(default_factory=list)
    quote: str = Field(min_length=1)
    context: str = ""
    score: float | None = Field(default=None, allow_inf_nan=False)
    score_source: str | None = None
    relation_path: list[RelationEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def canonical_offsets_are_consistent(self) -> "CandidateEvidence":
        if (self.canonical_char_start is None) != (self.canonical_char_end is None):
            raise ValueError("规范文本 canonical_char_start/end 必须同时提供或同时省略")
        if (
            self.canonical_char_start is not None
            and self.canonical_char_end is not None
            and self.canonical_char_end <= self.canonical_char_start
        ):
            raise ValueError("规范文本区间必须是非空的左闭右开区间")
        return self

    @property
    def has_canonical_location(self) -> bool:
        return self.canonical_char_start is not None


class FactTarget(StrictModel):
    fact_id: str = Field(min_length=1)
    target: str = Field(min_length=1)
    current_gap: str = Field(min_length=1)


class EntityConstraint(StrictModel):
    entity_type: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    label: str | None = None


class SupplementRequest(StrictModel):
    """Request to retrieve extra evidence; contains no answer key or credentials."""

    request_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    original_question: str | None = None
    fact_targets: list[FactTarget] = Field(default_factory=list)
    corpus_scope: CorpusScope
    entity_constraints: list[EntityConstraint] = Field(default_factory=list)
    rag_candidates: list[CandidateEvidence] = Field(default_factory=list)
    remaining_budget_ms: int = Field(ge=1)
    max_candidates: int = Field(ge=1, le=100)
    max_context_chars: int = Field(ge=1, le=200_000)

    @model_validator(mode="after")
    def rag_candidates_fit_scope_and_budget(self) -> "SupplementRequest":
        if len(self.rag_candidates) > self.max_candidates:
            raise ValueError("已有 RAG 候选数超过 max_candidates")
        context_chars = sum(len(item.quote) + len(item.context) for item in self.rag_candidates)
        if context_chars > self.max_context_chars:
            raise ValueError("已有 RAG 候选文本超过 max_context_chars")
        for candidate in self.rag_candidates:
            validate_candidate_scope(candidate, self.corpus_scope)
        return self


class RouteExecution(StrictModel):
    route: RouteName
    status: RouteStatus
    elapsed_ms: float = Field(ge=0, allow_inf_nan=False)
    candidate_count: int = Field(ge=0)


class SupplementDiagnostics(StrictModel):
    mapped_candidate_count: int = Field(ge=0)
    unmapped_candidate_count: int = Field(ge=0)
    rejected_candidate_count: int = Field(ge=0)
    stage_elapsed_ms: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def stage_durations_are_non_negative(self) -> "SupplementDiagnostics":
        if any(value < 0 or value == float("inf") or value != value for value in self.stage_elapsed_ms.values()):
            raise ValueError("阶段耗时不能为负数")
        return self


class SupplementResult(StrictModel):
    """Retrieval result only; it must never contain a generated business answer."""

    request_id: str = Field(min_length=1)
    status: SupplementStatus
    candidates: list[CandidateEvidence] = Field(default_factory=list)
    executed_routes: list[RouteExecution] = Field(default_factory=list)
    elapsed_ms: float = Field(ge=0, allow_inf_nan=False)
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    diagnostics: SupplementDiagnostics | None = None

    @model_validator(mode="after")
    def status_matches_recorded_routes(self) -> "SupplementResult":
        if self.status == "completed" and not any(
            route.status == "completed" for route in self.executed_routes
        ):
            raise ValueError("completed 结果必须记录至少一条 completed 实际路线")
        if self.status == "not_applicable" and (self.candidates or self.executed_routes):
            raise ValueError("not_applicable 结果不能包含候选或已执行路线")
        return self


class EvidenceSupplementer(Protocol):
    """Internal adapter contract. Implementations are supplied by the KAG track."""

    def supplement(self, request: SupplementRequest) -> SupplementResult:
        """Return original-text candidates within the request's scope and budget."""


def validate_candidate_scope(
    candidate: CandidateEvidence,
    scope: CorpusScope,
) -> None:
    """Reject candidate identities or versions not explicitly allowed by the request."""
    allowed = {document.document_id: document for document in scope.documents}
    document = allowed.get(candidate.document_id)
    if document is None:
        raise ValueError(f"候选来源不在 corpus_scope 内: {candidate.document_id}")
    if candidate.original_file_sha256 != document.original_file_sha256:
        raise ValueError(f"候选原文件哈希与 corpus_scope 不符: {candidate.document_id}")
    if candidate.normalized_text_sha256 != document.normalized_text_sha256:
        raise ValueError(f"候选规范文本版本与 corpus_scope 不符: {candidate.document_id}")

    for edge in candidate.relation_path:
        source = edge.source
        if source.document_id is None:
            continue
        source_document = allowed.get(source.document_id)
        if source_document is None:
            raise ValueError(f"关系边原文来源不在 corpus_scope 内: {source.document_id}")
        if source.original_file_sha256 != source_document.original_file_sha256:
            raise ValueError(f"关系边原文件哈希与 corpus_scope 不符: {source.document_id}")
        if source.normalized_text_sha256 != source_document.normalized_text_sha256:
            raise ValueError(f"关系边规范文本版本与 corpus_scope 不符: {source.document_id}")


def validate_supplement_result(
    request: SupplementRequest,
    result: SupplementResult,
    *,
    normalized_texts: dict[str, str] | None = None,
) -> None:
    """Validate request ownership, hard budgets, scope and optional quote coordinates.

    When `normalized_texts` is supplied, the mainline verifies the SHA-256 and
    exact quote slice against its canonical corpus text. Candidates with no
    canonical offsets remain diagnostic candidates and must not be treated as
    citable original-text evidence.
    """
    if result.request_id != request.request_id:
        raise ValueError("SupplementResult.request_id 与请求不一致")
    if len(request.rag_candidates) + len(result.candidates) > request.max_candidates:
        raise ValueError("RAG 与补充候选总数超过 max_candidates")
    candidate_chars = sum(
        len(candidate.quote) + len(candidate.context)
        for candidate in [*request.rag_candidates, *result.candidates]
    )
    if candidate_chars > request.max_context_chars:
        raise ValueError("RAG 与补充候选文本总量超过 max_context_chars")

    documents = {document.document_id: document for document in request.corpus_scope.documents}
    for candidate in [*request.rag_candidates, *result.candidates]:
        validate_candidate_scope(candidate, request.corpus_scope)
        if normalized_texts is not None and candidate.has_canonical_location:
            canonical_text = normalized_texts.get(candidate.document_id)
            if canonical_text is None:
                raise ValueError(f"主线规范语料缺失: {candidate.document_id}")
            expected_sha256 = documents[candidate.document_id].normalized_text_sha256
            actual_sha256 = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError(f"主线规范语料哈希与 scope 不符: {candidate.document_id}")
            start = candidate.canonical_char_start
            end = candidate.canonical_char_end
            assert start is not None and end is not None
            if end > len(canonical_text) or canonical_text[start:end] != candidate.quote:
                raise ValueError(f"候选引文与规范文本坐标不匹配: {candidate.candidate_id}")

        for edge in candidate.relation_path:
            source = edge.source
            if source.document_id is None or normalized_texts is None:
                continue
            canonical_text = normalized_texts.get(source.document_id)
            if canonical_text is None:
                raise ValueError(f"关系边主线规范语料缺失: {source.document_id}")
            expected_sha256 = documents[source.document_id].normalized_text_sha256
            actual_sha256 = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError(f"关系边规范语料哈希与 scope 不符: {source.document_id}")
            start = source.canonical_char_start
            end = source.canonical_char_end
            if start is not None and end is not None:
                if end > len(canonical_text) or canonical_text[start:end] != source.quote:
                    raise ValueError(f"关系边引文与规范文本坐标不匹配: {candidate.candidate_id}")


__all__ = [
    "CandidateEvidence",
    "CorpusScope",
    "EntityConstraint",
    "EvidenceLocator",
    "EvidenceSupplementer",
    "FactTarget",
    "RelationEdge",
    "RelationSource",
    "RouteExecution",
    "ScopedDocument",
    "SupplementDiagnostics",
    "SupplementRequest",
    "SupplementResult",
    "validate_candidate_scope",
    "validate_supplement_result",
]
