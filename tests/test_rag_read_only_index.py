import pytest

from app.rag.corpus import content_fingerprint, validate_read_only_index
from app.rag.schemas import Chunk


def _chunk(doc_id="AG-ICE-7301", text="合同修复时限 6 小时"):
    return Chunk(
        doc_id=doc_id,
        text=text,
        page=1,
        char_start=0,
        char_end=len(text),
    )


def test_frozen_rag_index_accepts_exact_doc_fingerprint_and_count():
    chunks = {"AG-ICE-7301": [_chunk()]}
    meta = {"AG-ICE-7301": (content_fingerprint(chunks["AG-ICE-7301"]), 1)}

    assert validate_read_only_index(chunks, meta, {"AG-ICE-7301": 1}) == ["AG-ICE-7301"]


@pytest.mark.parametrize(
    ("meta", "row_counts"),
    [
        ({}, {}),
        ({"AG-ICE-7301": ("wrong-fingerprint", 1)}, {"AG-ICE-7301": 1}),
        ({"AG-ICE-7301": (content_fingerprint([_chunk()]), 2)}, {"AG-ICE-7301": 1}),
        ({"AG-ICE-7301": (content_fingerprint([_chunk()]), 1), "ORPHAN": ("x", 0)}, {"AG-ICE-7301": 1}),
    ],
)
def test_frozen_rag_index_fails_closed_on_drift(meta, row_counts):
    with pytest.raises(RuntimeError):
        validate_read_only_index({"AG-ICE-7301": [_chunk()]}, meta, row_counts)
