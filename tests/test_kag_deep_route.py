from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from app.contracts import DocRef, ToolResult, TraceStep
from app.evidence_supplement import (
    CandidateEvidence,
    CorpusScope,
    EvidenceLocator,
    RouteExecution,
    ScopedDocument,
    SupplementDiagnostics,
    SupplementRequest,
    SupplementResult,
)
from app.kag_supplement import (
    KagProcessSupplementer,
    _candidate_content_for_matching,
    _source_span,
)
from app.rag.schemas import Candidate, Chunk
from app.tools.doc_search import RagDocSearchTool


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ref(doc_id: str, text: str, start: int, end: int) -> DocRef:
    return DocRef(
        doc_id=doc_id,
        doc_title=doc_id,
        page=1,
        section_path="测试章节",
        snippet=text,
        char_start=start,
        char_end=end,
        bboxes=[],
        page_width=None,
        page_height=None,
        extraction_method="plain",
    )


class _FakeReranker:
    def rerank(self, _question, documents):
        return [(index, 1.0 - index / 10) for index in range(len(documents))]


class _FakeRag:
    def __init__(self) -> None:
        self.bm25 = object()
        self.store = object()
        self.embedder = object()
        self.k = 60
        self.retrieve_top_k = 8
        self.rerank_top_k = 5
        self.reranker = _FakeReranker()
        self.rag_chunk = Chunk(
            doc_id="doc-1", text="RAG 支持片段", page=1, char_start=0, char_end=7,
            section_path=["测试章节"], doc_title="材料一", extraction_method="plain",
        )
        self.kag_chunk = Chunk(
            doc_id="doc-1", text="KAG 补充片段", page=1, char_start=10, char_end=17,
            section_path=["测试章节"], doc_title="材料一", extraction_method="plain",
        )
        self.corpus_index = SimpleNamespace(chunks=[self.rag_chunk, self.kag_chunk], parse_info={})
        self.generated_require_chain: bool | None = None
        self.generated_hits = []

    def answer(self, question, doc_id=None, known_facts=None, answer_question=None):
        # 签名必须与真实 RagAnswerTool.answer 对齐：doc_search.run 会传 doc_id
        # （题面用书名号点名文档时锁定检索范围，见 app/rag/retrieval/named_doc.py）。
        self.answer_doc_id = doc_id
        return ToolResult(data=f"普通回答：{question}", trace=[], refs=[])

    def generate(self, _question, context_hits, *, require_chain=False, **_kwargs):
        self.generated_require_chain = require_chain
        self.generated_hits = list(context_hits)
        refs = [ref for _candidate, ref in context_hits]
        return {"text": "项目模型生成的回答[1]", "final_refs": refs[:1], "gen_ms": 3.0, "context_num": len(refs)}

    def generate_step(self, _question, generated, *, step_id):
        return TraceStep(
            step_id=step_id,
            tool="llm_generate",
            input={"context_num": generated["context_num"]},
            output={"answer_len": len(generated["text"])},
            elapsed_ms=generated["gen_ms"],
            hit=generated["final_refs"],
        )


class _FakeHybridRetriever:
    hits = []

    def __init__(self, *_args, **_kwargs):
        pass

    def search(self, _question, **_kwargs):
        return list(self.hits)


class _FakeSupplementer:
    def __init__(self, result: SupplementResult, kag_hits: list[tuple[Candidate, DocRef]]) -> None:
        self.result = result
        self.kag_hits = kag_hits

    def prepare_request(self, _question, _rag_hits, _chunks):
        return object(), {"doc-1": "RAG 支持片段___KAG 补充片段"}

    def supplement(self, _request):
        return self.result

    def validate_and_convert(self, *_args):
        return self.kag_hits


def _kag_result(status: str = "completed", candidates: int = 1) -> SupplementResult:
    route_status = "completed" if status in {"completed", "partial"} else status
    return SupplementResult(
        request_id="test-request",
        status=status,
        elapsed_ms=12,
        reason_code=None if status == "completed" else "test_status",
        executed_routes=[RouteExecution(
            route="hybrid", status=route_status, elapsed_ms=12, candidate_count=candidates
        )],
        diagnostics=SupplementDiagnostics(
            mapped_candidate_count=candidates,
            unmapped_candidate_count=0,
            rejected_candidate_count=0,
        ),
    )


def test_standard_mode_does_not_call_kag() -> None:
    rag = _FakeRag()
    tool = RagDocSearchTool(rag_tool=rag, kag_supplementer=object())
    result = tool.run({"question": "普通问题"})
    assert result.data["text"] == "普通回答：普通问题"


def test_deep_mode_reranks_rag_and_kag_together(monkeypatch) -> None:
    rag = _FakeRag()
    rag_ref = _ref("doc-1", "RAG 支持片段", 0, 7)
    rag_hit = (Candidate(chunk=rag.rag_chunk, score=0.8, method="rag"), rag_ref)
    kag_ref = _ref("doc-1", "KAG 补充片段", 10, 17)
    kag_hit = (Candidate(chunk=rag.kag_chunk, score=0.0, method="kag_hybrid"), kag_ref)
    _FakeHybridRetriever.hits = [rag_hit]
    monkeypatch.setattr("app.rag.retrieval.fusion.HybridRetriever", _FakeHybridRetriever)
    supplementer = _FakeSupplementer(_kag_result(), [kag_hit])
    tool = RagDocSearchTool(rag_tool=rag, kag_supplementer=supplementer)

    result = tool.run({"question": "复杂问题", "retrieval_mode": "deep"})

    assert rag.generated_require_chain is True
    assert [ref.doc_id for _candidate, ref in rag.generated_hits] == ["doc-1", "doc-1"]
    assert [step.tool for step in result.trace] == [
        "hybrid_retrieve", "kag_supplement", "joint_rerank", "llm_generate"
    ]
    assert result.trace[1].output["status"] == "completed"
    assert result.trace[1].hit == [kag_ref]
    assert result.refs == [rag_ref]
    assert "未取得 KAG" not in result.data["text"]


def test_deep_mode_reports_fallback_when_kag_unavailable(monkeypatch) -> None:
    rag = _FakeRag()
    rag_ref = _ref("doc-1", "RAG 支持片段", 0, 7)
    _FakeHybridRetriever.hits = [(Candidate(chunk=rag.rag_chunk, score=0.8, method="rag"), rag_ref)]
    monkeypatch.setattr("app.rag.retrieval.fusion.HybridRetriever", _FakeHybridRetriever)
    tool = RagDocSearchTool(
        rag_tool=rag,
        kag_supplementer=_FakeSupplementer(_kag_result("unavailable", 0), []),
    )

    result = tool.run({"question": "复杂问题", "retrieval_mode": "deep"})

    assert result.trace[1].output["status"] == "unavailable"
    assert len(rag.generated_hits) == 1
    assert "回答仅依据项目 RAG 检索证据" in result.data["text"]


def test_kag_candidates_map_from_content_when_title_is_opaque(tmp_path: Path, monkeypatch) -> None:
    text = "设备巡检周期为30天。"
    source = tmp_path / "source.txt"
    source.write_text(text, encoding="utf-8")
    workspace = tmp_path / "runtime"
    (workspace / "corpus").mkdir(parents=True)
    (workspace / "canonical").mkdir()
    (workspace / "runtime").mkdir()
    (workspace / "canonical" / "source.txt").write_text(text, encoding="utf-8")
    (workspace / "corpus" / "source__p0001.txt").write_text(text, encoding="utf-8")
    original_hash = _sha("original source")
    text_hash = _sha(text)
    manifest = {
        "schema_version": "askdata-kag-corpus-v2",
        "source_root": str(tmp_path),
        "document_count": 1,
        "index_unit_count": 1,
        "documents": [{
            "document_id": "source",
            "canonical_file": "source.txt",
            "source_file": "source.txt",
            "original_file_sha256": original_hash,
            "normalized_text_sha256": text_hash,
            "chars": len(text),
            "chunks": [{
                "start": 0, "end": len(text), "page": 1,
                "section_path": ["运行规程"], "bboxes": [],
                "page_width": None, "page_height": None, "extraction_method": "plain",
            }],
            "chunk_count": 1,
            "ocr_chunk_count": 0,
            "pages": [{
                "page": 1,
                "unit_file": "source__p0001.txt",
                "canonical_char_start": 0,
                "canonical_char_end": len(text),
                "normalized_unit_text_sha256": text_hash,
                "chars": len(text),
                "chunk_ranges": [],
            }],
        }],
    }
    (workspace / "corpus_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    supplementer = KagProcessSupplementer(workspace, python_executable=Path("python"))
    monkeypatch.setattr(supplementer, "readiness", lambda: (True, None))
    import subprocess

    stdout = json.dumps({
        "request_id": "req-1",
        "candidate_only_requested": True,
        "candidates": [{
            "title": "file_deadbeef",
            "content": "file_deadbeef\n" + text,
            "chunk_id": "chunk-1",
        }],
    }, ensure_ascii=False)
    captured = {}

    def fake_subprocess_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess([], 0, stdout, "")

    monkeypatch.setattr(
        "app.kag_supplement.subprocess.run",
        fake_subprocess_run,
    )
    request = SupplementRequest(
        request_id="req-1",
        question="设备巡检周期是多少？",
        corpus_scope=CorpusScope(
            corpus_id="test-corpus",
            version="askdata-kag-corpus-v2",
            documents=[ScopedDocument(
                document_id="source",
                original_file_sha256=original_hash,
                normalized_text_sha256=text_hash,
            )],
        ),
        remaining_budget_ms=5000,
        max_candidates=8,
        max_context_chars=18000,
    )

    result = supplementer.supplement(request)

    assert result.status == "completed"
    assert len(result.candidates) == 1
    assert result.candidates[0].document_id == "source"
    assert result.candidates[0].quote == text
    assert (result.candidates[0].canonical_char_start, result.candidates[0].canonical_char_end) == (0, len(text))
    assert Path(captured["env"]["TEMP"]).is_dir()
    assert captured["env"]["TEMP"] == captured["env"]["TMP"]


def test_kag_quoted_page_candidate_is_unwrapped_before_exact_mapping() -> None:
    title = "KB-001__p0001"
    source_text = "第一章 服务范围\n一级故障：30分钟内响应，4小时内到场。"
    serialized_content = json.dumps(f"{title}\n{source_text}", ensure_ascii=False)

    cleaned = _candidate_content_for_matching(title, serialized_content)

    assert cleaned == source_text
    assert _source_span(source_text, cleaned) == (0, len(source_text))


def test_kag_candidate_layout_whitespace_maps_to_canonical_source_span() -> None:
    source_text = "合同编号 A-17。外接设备不在覆盖范围内。"
    kag_excerpt = "合同编号 A-17。\n外接设备不在覆盖范围内。"

    span = _source_span(source_text, kag_excerpt)

    assert span == (0, len(source_text))
    assert source_text[span[0]:span[1]] == source_text
