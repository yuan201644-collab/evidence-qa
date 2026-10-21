"""Isolated OpenSPG/KAG candidate retrieval for optional document deep search.

KAG runs in its pinned Python 3.10 environment and returns candidates only.
The project remains responsible for source validation, reranking, answer
generation, and final citations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.contracts import DocRef
from app.evidence_supplement import (
    CandidateEvidence,
    CorpusScope,
    EvidenceLocator,
    RouteExecution,
    ScopedDocument,
    SupplementDiagnostics,
    SupplementRequest,
    SupplementResult,
    validate_supplement_result,
)
from app.rag.schemas import Candidate, Chunk


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalized_with_offsets(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        chars.append(char)
        offsets.append(index)
    return "".join(chars), offsets


def _source_span(source: str, evidence: str) -> tuple[int, int] | None:
    """Map a uniquely matching excerpt while ignoring layout-only whitespace.

    KAG may insert line breaks where the canonical PDF text has a paragraph
    join. Every non-whitespace character must still match in order, and the
    returned interval points into the untouched canonical source.
    """
    normalized_source, source_offsets = _normalized_with_offsets(source)
    normalized_evidence, _ = _normalized_with_offsets(evidence.strip())
    if len(normalized_evidence) < 8:
        return None
    first = normalized_source.find(normalized_evidence)
    if first < 0 or normalized_source.find(normalized_evidence, first + 1) >= 0:
        return None
    last = first + len(normalized_evidence) - 1
    return source_offsets[first], source_offsets[last] + 1


def _candidate_content_for_matching(title: str, content: str) -> str:
    """Unwrap KAG's serialized page text before exact source matching.

    OpenSPG's trace can expose a page body as a JSON-quoted string with a
    repeated title line. Decode only that representation; source matching below
    still requires the resulting text to occur uniquely in the frozen source.
    """
    text = str(content or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, str):
            text = decoded

    # Some KAG trace serializers leave JSON newline escapes in the text field
    # instead of decoding them. Keep this narrow and let the exact source check
    # reject any content that does not match the indexed page.
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n")

    clean_title = str(title or "").strip().strip("\"'“”‘’")
    lines = text.splitlines()
    if lines:
        header = lines[0].strip().strip("# \t\"'“”‘’")
        if header in {clean_title, Path(clean_title).stem} or re.fullmatch(
            r"file_[0-9a-f]{8,64}", header, re.IGNORECASE
        ):
            text = "\n".join(lines[1:])
    return text.strip()


def _canonical_text(chunks: Sequence[Any]) -> str:
    ordered = sorted(chunks, key=lambda chunk: (int(chunk.char_start), int(chunk.char_end)))
    parts: list[str] = []
    cursor = 0
    for chunk in ordered:
        start, end, text = int(chunk.char_start), int(chunk.char_end), str(chunk.text)
        if start < cursor or end - start != len(text):
            raise ValueError("主线 RAG 切片偏移不连续或长度不一致")
        parts.append("\n" * (start - cursor))
        parts.append(text)
        cursor = end
    return "".join(parts)


class KagProcessSupplementer:
    """Call a local KAG candidate-only worker without importing KAG into the app."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        python_executable: Path,
        timeout_seconds: float = 60.0,
        max_candidates: int = 16,
        max_context_chars: int = 18_000,
        deepseek_api_key: str = "",
        vector_api_key: str = "",
    ) -> None:
        if timeout_seconds <= 0 or max_candidates < 1 or max_context_chars < 1:
            raise ValueError("KAG 检索预算必须为正数")
        self.runtime_dir = runtime_dir.resolve()
        self.python_executable = python_executable.resolve()
        self.timeout_seconds = float(timeout_seconds)
        self.max_candidates = min(int(max_candidates), 16)
        self.max_context_chars = min(int(max_context_chars), 200_000)
        # Capture the configured credentials before per-request model profiles
        # can temporarily change LLM_API_KEY. Values stay in memory only.
        self._deepseek_api_key = deepseek_api_key
        self._vector_api_key = vector_api_key
        self.manifest_path = self.runtime_dir / "corpus_manifest.json"
        self.identity_path = self.runtime_dir / "results" / "kag_build_identity.json"
        self.query_worker = self.runtime_dir / "runtime" / "query_worker.py"

    @classmethod
    def from_env(cls) -> "KagProcessSupplementer | None":
        from app.rag.config import load_env_file

        load_env_file()
        root = os.environ.get("KAG_RUNTIME_DIR", "").strip()
        if not root:
            return None
        try:
            timeout_seconds = float(os.environ.get("KAG_QUERY_TIMEOUT_SECONDS", "60"))
            max_candidates = int(os.environ.get("KAG_MAX_CANDIDATES", "16"))
            max_context_chars = int(os.environ.get("KAG_MAX_CONTEXT_CHARS", "18000"))
        except ValueError as exc:
            raise ValueError("KAG 超时与上下文预算配置必须是数字") from exc
        return cls(
            Path(root),
            python_executable=Path(os.environ.get("KAG_PYTHON", r"D:\kag\python310\python.exe")),
            timeout_seconds=timeout_seconds,
            max_candidates=max_candidates,
            max_context_chars=max_context_chars,
            deepseek_api_key=(
                os.environ.get("KAG_DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY", "")
            ),
            vector_api_key=os.environ.get("KAG_SILICONFLOW_API_KEY", ""),
        )

    def readiness(self) -> tuple[bool, str | None]:
        if not self.runtime_dir.is_dir() or not self.manifest_path.is_file() or not self.identity_path.is_file():
            return False, "index_not_ready"
        if not self.python_executable.is_file() or not self.query_worker.is_file():
            return False, "runtime_not_ready"
        if not self._deepseek_api_key or not self._vector_api_key:
            return False, "credentials_not_ready"
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            identity = json.loads(self.identity_path.read_text(encoding="utf-8"))
            if identity.get("status") != "SUCCESS":
                return False, "index_not_ready"
            if identity.get("source_manifest_sha256") != _sha256_file(self.manifest_path):
                return False, "index_version_mismatch"
            if int(identity.get("document_count", 0)) != int(manifest.get("document_count", -1)):
                return False, "index_version_mismatch"
            config = self.runtime_dir / "runtime" / "kag_config.yaml"
            if not config.is_file() or not identity.get("project_id"):
                return False, "runtime_not_ready"
            with socket.create_connection(("127.0.0.1", 8887), timeout=0.4):
                pass
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False, "service_unavailable"
        return True, None

    def prepare_request(
        self,
        question: str,
        rag_hits: Sequence[tuple[Any, DocRef]],
        corpus_chunks: Sequence[Any],
        *,
        request_id: str | None = None,
    ) -> tuple[SupplementRequest, dict[str, str]]:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        source_root = Path(str(manifest["source_root"]))
        canonical_dir = self.runtime_dir / "canonical"
        metadata = {str(row["document_id"]): row for row in manifest["documents"]}
        chunks_by_doc: dict[str, list[Any]] = {}
        for chunk in corpus_chunks:
            chunks_by_doc.setdefault(str(chunk.doc_id), []).append(chunk)
        if set(chunks_by_doc) != set(metadata):
            raise ValueError("当前 RAG 语料文档集合与 KAG 索引不一致")

        scoped: list[ScopedDocument] = []
        normalized_texts: dict[str, str] = {}
        for document_id, row in metadata.items():
            original_path = source_root / str(row["source_file"])
            if not original_path.is_file() or _sha256_file(original_path) != row["original_file_sha256"]:
                raise ValueError(f"原始文档版本与 KAG 索引不一致: {document_id}")
            canonical_path = canonical_dir / str(row["canonical_file"])
            if not canonical_path.is_file():
                raise ValueError(f"KAG 规范文本缺失: {document_id}")
            canonical = _canonical_text(chunks_by_doc[document_id])
            canonical_hash = _sha256(canonical.encode("utf-8"))
            if canonical_hash != row["normalized_text_sha256"]:
                raise ValueError(f"当前 RAG 规范文本与 KAG 索引不一致: {document_id}")
            if canonical_path.read_text(encoding="utf-8") != canonical:
                raise ValueError(f"KAG 规范文本与当前 RAG 切片不一致: {document_id}")
            normalized_texts[document_id] = canonical
            scoped.append(ScopedDocument(
                document_id=document_id,
                original_file_sha256=str(row["original_file_sha256"]),
                normalized_text_sha256=canonical_hash,
            ))

        rag_candidates: list[CandidateEvidence] = []
        for index, (candidate, ref) in enumerate(rag_hits):
            row = metadata.get(ref.doc_id)
            if row is None or ref.char_start is None or ref.char_end is None:
                continue
            quote = str(candidate.chunk.text)
            start, end = int(ref.char_start), int(ref.char_end)
            if end > len(normalized_texts[ref.doc_id]) or normalized_texts[ref.doc_id][start:end] != quote:
                continue
            rag_candidates.append(CandidateEvidence(
                candidate_id=f"rag:{ref.doc_id}:{start}:{end}",
                source_system="project_rag",
                retrieval_route="project_rag",
                document_id=ref.doc_id,
                original_file_sha256=str(row["original_file_sha256"]),
                normalized_text_sha256=str(row["normalized_text_sha256"]),
                original_locator=EvidenceLocator(
                    coordinate_type="char", start=start, end=end, page=ref.page,
                    section_path=[part for part in str(ref.section_path or "").split(" > ") if part],
                ),
                canonical_char_start=start,
                canonical_char_end=end,
                page=ref.page,
                section_path=[part for part in str(ref.section_path or "").split(" > ") if part],
                quote=quote,
                score=float(candidate.score),
                score_source=str(candidate.method),
            ))
        if len(rag_candidates) > self.max_candidates:
            rag_candidates = rag_candidates[:self.max_candidates]
        return SupplementRequest(
            request_id=request_id or uuid.uuid4().hex,
            question=question,
            original_question=question,
            corpus_scope=CorpusScope(
                corpus_id="askdata-main-corpus",
                version=str(manifest["schema_version"]),
                documents=scoped,
            ),
            rag_candidates=rag_candidates,
            remaining_budget_ms=int(self.timeout_seconds * 1000),
            max_candidates=max(1, self.max_candidates + len(rag_candidates)),
            max_context_chars=self.max_context_chars,
        ), normalized_texts

    def supplement(self, request: SupplementRequest) -> SupplementResult:
        ready, reason = self.readiness()
        if not ready:
            return self._empty_result(request.request_id, "unavailable", reason or "runtime_not_ready")
        started = time.perf_counter()
        remaining_seconds = min(self.timeout_seconds, request.remaining_budget_ms / 1000.0)
        env = dict(os.environ)
        env["KAG_DEEPSEEK_API_KEY"] = self._deepseek_api_key
        env["KAG_SILICONFLOW_API_KEY"] = self._vector_api_key
        env["HF_HUB_OFFLINE"] = "1"
        env["PYTHONUTF8"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # The host process may inherit TEMP/TMP paths that are absent or not
        # writable for the isolated KAG interpreter. KAG dependencies call
        # tempfile.gettempdir() during import, so pin a project-owned location.
        temp_dir = Path(__file__).resolve().parents[1] / "agent_temp" / "kag_runtime"
        try:
            temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return SupplementResult(
                request_id=request.request_id,
                status="failed",
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                reason_code="kag_temp_unavailable",
                executed_routes=[RouteExecution(route="hybrid", status="failed", elapsed_ms=0, candidate_count=0)],
                diagnostics=SupplementDiagnostics(mapped_candidate_count=0, unmapped_candidate_count=0, rejected_candidate_count=0),
            )
        env["TEMP"] = str(temp_dir)
        env["TMP"] = str(temp_dir)
        payload = {
            "request_id": request.request_id,
            "question": request.question,
            "max_candidates": self.max_candidates,
            "max_context_chars": self.max_context_chars,
        }
        try:
            process = subprocess.run(
                [str(self.python_executable), "-X", "utf8", str(self.query_worker)],
                cwd=self.runtime_dir / "runtime",
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=remaining_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            elapsed = (time.perf_counter() - started) * 1000.0
            return SupplementResult(
                request_id=request.request_id,
                status="timed_out",
                elapsed_ms=elapsed,
                reason_code="kag_query_timeout",
                executed_routes=[RouteExecution(route="hybrid", status="timed_out", elapsed_ms=elapsed, candidate_count=0)],
                diagnostics=SupplementDiagnostics(mapped_candidate_count=0, unmapped_candidate_count=0, rejected_candidate_count=0),
            )
        elapsed = (time.perf_counter() - started) * 1000.0
        if process.returncode != 0:
            return SupplementResult(
                request_id=request.request_id,
                status="failed",
                elapsed_ms=elapsed,
                reason_code="kag_process_failed",
                executed_routes=[RouteExecution(route="hybrid", status="failed", elapsed_ms=elapsed, candidate_count=0)],
                diagnostics=SupplementDiagnostics(mapped_candidate_count=0, unmapped_candidate_count=0, rejected_candidate_count=0),
            )
        try:
            raw = json.loads(process.stdout)
        except json.JSONDecodeError:
            return SupplementResult(
                request_id=request.request_id,
                status="failed",
                elapsed_ms=elapsed,
                reason_code="kag_result_invalid",
                executed_routes=[RouteExecution(route="hybrid", status="failed", elapsed_ms=elapsed, candidate_count=0)],
                diagnostics=SupplementDiagnostics(mapped_candidate_count=0, unmapped_candidate_count=0, rejected_candidate_count=0),
            )
        if raw.get("request_id") != request.request_id or raw.get("candidate_only_requested") is not True:
            return SupplementResult(
                request_id=request.request_id,
                status="failed",
                elapsed_ms=elapsed,
                reason_code="kag_candidate_contract_invalid",
                executed_routes=[RouteExecution(route="hybrid", status="failed", elapsed_ms=elapsed, candidate_count=0)],
                diagnostics=SupplementDiagnostics(mapped_candidate_count=0, unmapped_candidate_count=0, rejected_candidate_count=0),
            )

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        units: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for row in manifest["documents"]:
            for page in row["pages"]:
                units[Path(str(page["unit_file"])).stem.casefold()] = (row, page)
        allowed = {item.document_id for item in request.corpus_scope.documents}
        mapped: list[CandidateEvidence] = []
        unmapped = 0
        rejected = 0
        for index, item in enumerate(raw.get("candidates") or []):
            if not isinstance(item, Mapping):
                unmapped += 1
                continue
            title = str(item.get("title") or item.get("name") or "").strip().strip("\"'“”‘’")
            unit_key = Path(title.replace("\\", "/")).stem.casefold()
            content = _candidate_content_for_matching(title, str(item.get("content") or ""))

            preferred = units.get(unit_key)
            search_units = [preferred] if preferred is not None else list(units.values())
            matches: list[tuple[dict[str, Any], dict[str, Any], str, tuple[int, int]]] = []
            for row_candidate, page_candidate in search_units:
                unit_path = self.runtime_dir / "corpus" / str(page_candidate["unit_file"])
                unit_text = unit_path.read_text(encoding="utf-8") if unit_path.is_file() else ""
                span_candidate = _source_span(unit_text, content)
                if span_candidate is not None:
                    matches.append((row_candidate, page_candidate, unit_text, span_candidate))
            if len(matches) != 1:
                unmapped += 1
                continue
            row, page, unit_text, span = matches[0]
            document_id = str(row["document_id"])
            if document_id not in allowed:
                rejected += 1
                continue
            local_start, local_end = span
            start = int(page["canonical_char_start"]) + local_start
            end = int(page["canonical_char_start"]) + local_end
            quote = unit_text[local_start:local_end]
            overlapping = [
                chunk for chunk in row["chunks"]
                if int(chunk["page"]) == int(page["page"])
                and int(chunk["start"]) < end and int(chunk["end"]) > start
            ]
            section_paths = {tuple(chunk.get("section_path") or []) for chunk in overlapping}
            section_path = list(next(iter(section_paths))) if len(section_paths) == 1 else []
            evidence = CandidateEvidence(
                candidate_id=f"kag:{document_id}:{start}:{end}:{index}",
                source_system="openspg_kag",
                retrieval_route="hybrid",
                index_chunk_id=str(item.get("chunk_id") or "") or None,
                document_id=document_id,
                original_file_sha256=str(row["original_file_sha256"]),
                normalized_text_sha256=str(row["normalized_text_sha256"]),
                original_locator=EvidenceLocator(
                    coordinate_type="char", start=start, end=end, page=int(page["page"]), section_path=section_path,
                ),
                canonical_char_start=start,
                canonical_char_end=end,
                page=int(page["page"]),
                section_path=section_path,
                quote=quote,
                score=None,
                score_source="kag_rank_unavailable",
            )
            mapped.append(evidence)
        budget_truncated = bool(raw.get("budget_truncated"))
        status = "partial" if unmapped or rejected or budget_truncated else "completed"
        reason_code = (
            "candidate_budget_truncated" if budget_truncated
            else "candidate_mapping_partial" if status == "partial"
            else None
        )
        return SupplementResult(
            request_id=request.request_id,
            status=status,
            candidates=mapped,
            executed_routes=[RouteExecution(
                route="hybrid", status="completed", elapsed_ms=elapsed,
                candidate_count=len(raw.get("candidates") or []),
            )],
            elapsed_ms=elapsed,
            reason_code=reason_code,
            diagnostics=SupplementDiagnostics(
                mapped_candidate_count=len(mapped),
                unmapped_candidate_count=unmapped,
                rejected_candidate_count=rejected,
            ),
        )

    def validate_and_convert(
        self,
        request: SupplementRequest,
        result: SupplementResult,
        normalized_texts: Mapping[str, str],
        corpus_chunks: Sequence[Any],
    ) -> list[tuple[Candidate, DocRef]]:
        validate_supplement_result(request, result, normalized_texts=dict(normalized_texts))
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        docs = {str(row["document_id"]): row for row in manifest["documents"]}
        chunks_by_doc: dict[str, list[Any]] = {}
        for chunk in corpus_chunks:
            chunks_by_doc.setdefault(str(chunk.doc_id), []).append(chunk)
        output: list[tuple[Candidate, DocRef]] = []
        for evidence in result.candidates:
            text = normalized_texts[evidence.document_id]
            start = evidence.canonical_char_start
            end = evidence.canonical_char_end
            assert start is not None and end is not None
            if text[start:end] != evidence.quote:
                raise ValueError("KAG 候选引文与主线规范语料回切结果不一致")
            source_chunks = [
                chunk for chunk in chunks_by_doc[evidence.document_id]
                if chunk.page == evidence.page and chunk.char_start < end and chunk.char_end > start
            ]
            section_paths = {tuple(chunk.section_path) for chunk in source_chunks}
            section_path = list(next(iter(section_paths))) if len(section_paths) == 1 else []
            exact_chunk = next((
                chunk for chunk in source_chunks
                if chunk.char_start == start and chunk.char_end == end
            ), None)
            bboxes = exact_chunk.bboxes if exact_chunk is not None else []
            page_width = exact_chunk.page_width if exact_chunk is not None else None
            page_height = exact_chunk.page_height if exact_chunk is not None else None
            extraction_method = exact_chunk.extraction_method if exact_chunk is not None else None
            chunk = Chunk(
                doc_id=evidence.document_id,
                doc_title=evidence.document_id,
                text=evidence.quote,
                page=int(evidence.page or 1),
                section_path=section_path,
                char_start=start,
                char_end=end,
                bboxes=bboxes,
                page_width=page_width,
                page_height=page_height,
                extraction_method=extraction_method,
            )
            candidate = Candidate(chunk=chunk, score=float(evidence.score or 0.0), method="kag_hybrid")
            ref = DocRef(
                doc_id=evidence.document_id,
                doc_title=evidence.document_id,
                page=evidence.page,
                section_path=" > ".join(section_path) if section_path else None,
                snippet=evidence.quote,
                char_start=start,
                char_end=end,
                bboxes=bboxes,
                page_width=page_width,
                page_height=page_height,
                extraction_method=extraction_method,
            )
            output.append((candidate, ref))
        return output

    @staticmethod
    def _empty_result(request_id: str, status: str, reason: str) -> SupplementResult:
        return SupplementResult(
            request_id=request_id,
            status=status,  # type: ignore[arg-type]
            elapsed_ms=0.0,
            reason_code=reason,
            diagnostics=SupplementDiagnostics(
                mapped_candidate_count=0,
                unmapped_candidate_count=0,
                rejected_candidate_count=0,
            ),
        )


__all__ = ["KagProcessSupplementer"]
