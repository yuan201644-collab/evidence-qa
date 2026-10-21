from types import SimpleNamespace

from app.rag import answer


def test_rag_builder_can_use_frozen_runtime_corpus_and_read_only_dsn(monkeypatch, tmp_path):
    (tmp_path / "contract.md").write_text("test", encoding="utf-8")
    (tmp_path / "ignored.bin").write_bytes(b"x")
    monkeypatch.setenv("RAG_CORPUS_DIR", str(tmp_path))
    monkeypatch.setenv("RAG_DSN", "postgresql://user:pass@127.0.0.1/db")
    monkeypatch.setenv("RAG_INDEX_READ_ONLY", "1")

    index = SimpleNamespace(
        bm25=object(), store=object(), embedder=object(),
        summary=lambda: "frozen", chunks=[], indexed=[], skipped=[], pruned=[],
        ignored=[], empty=[], backend="pgvector", ocr={},
    )
    calls = {}

    def fake_index_corpus(**kwargs):
        calls.update(kwargs)
        return index

    class FakeTool:
        def __init__(self, bm25, store, embedder, llm, reranker=None):
            self.corpus_index = None

    monkeypatch.setattr("app.rag.corpus.index_corpus", fake_index_corpus)
    monkeypatch.setattr(answer, "RagAnswerTool", FakeTool)

    result = answer.build_rag_tool_from_corpus(llm=object(), reranker=object())

    assert calls["dsn"] == "postgresql://user:pass@127.0.0.1/db"
    assert calls["read_only"] is True
    assert calls["paths"] == [str((tmp_path / "contract.md").resolve())]
    assert result.corpus_index is index
