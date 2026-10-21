# -*- coding: utf-8 -*-
"""扫描件 OCR 通道（app/rag/ingest/ocr.py）的回归测试。

为什么用假引擎而不是真跑 RapidOCR：
    本组要守的全是**结构性质** —— 坐标有没有换算、缓存有没有生效、
    依赖缺失时会不会静默炸链路、loader 有没有把扫描页交给 OCR。
    这些性质在真引擎上反而看不出来（真引擎只会给你一段"看起来对"的文字），
    而且真跑一页要 7 到 10 秒，需求方（CI / 没装依赖的机器）也装不了。

跑法：
    D:/python/python.exe app/test_rag_ocr.py
    .venv/Scripts/python.exe app/test_rag_ocr.py

覆盖：
    ① 像素框 -> point 框：逐轴换算，不是只乘一个系数
    ② 换算结果与"自己按 page.rect / 位图尺寸算一遍"的期望值一致（硬性验收项）
    ③ 文件内容摘要：同内容稳定，内容变则变
    ④ 页级判据：有文本层的页不 OCR，扫描页要 OCR
    ⑤ 依赖缺失时优雅降级：返回空、不抛异常、统计里能看见
    ⑥ 开关关闭时同样降级
    ⑦ 缓存命中不重复跑引擎，且两次结果逐字段一致
    ⑧ loader 分派：扫描件走 OCR、文本层文档完全不触发 OCR
    ⑨ corpus 摘要能说清 OCR 为什么没生效
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf                                                    # noqa: E402

from app.contracts import DocRef, TraceStep                       # noqa: E402
from app.rag.answer import RagAnswerTool, _parse_provenance_step  # noqa: E402
from app.rag.corpus import CorpusIndex, _document_parse_info      # noqa: E402
from app.rag.ingest.chunker import build_chunks                   # noqa: E402
from app.rag.ingest import loader, ocr                            # noqa: E402
from app.rag.schemas import Block                                # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = PROJECT_ROOT / "data" / "corpus" / "raw"

PASSED = 0

#: 假引擎返回的像素框（照 KB-007 实测的首行框写，页面 595x842、位图 1240x1755）
FAKE_PIXEL_BOX = [[117.0, 104.0], [474.0, 104.0], [474.0, 137.0], [117.0, 137.0]]
FAKE_TEXT = "服务器巡检记录 (扫描件)"
FAKE_SCORE = 0.9664
TEST_DPI = 150


def check(name: str, fn) -> None:
    global PASSED
    try:
        fn()
    except AssertionError as e:
        print(f"[FAIL] {name}\n       {e}")
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {name}\n        {type(e).__name__}: {e}")
        raise
    PASSED += 1
    print(f"[ ok ] {name}")


def _corpus_path(prefix):
    for name in sorted(os.listdir(CORPUS_DIR)):
        if name.startswith(prefix):
            return str(CORPUS_DIR / name)
    raise AssertionError("语料缺失：" + prefix)


class _Env:
    """临时改环境变量，退出时还原（测试之间不互相污染）。"""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


class FakeEngine:
    """假 OCR 引擎：记录被调用次数，返回固定的一行结果。"""

    def __init__(self, lines=None):
        self.calls = 0
        self.lines = lines if lines is not None else [
            [FAKE_PIXEL_BOX, FAKE_TEXT, FAKE_SCORE]
        ]

    def __call__(self, image):
        self.calls += 1
        return list(self.lines), [0.01]


class _FakeEnginePatch:
    """把 ocr 模块的引擎与可用性替换掉，退出时还原。

    为什么要连 is_available 一起换：find_spec 的结果在进程内被缓存，
    测试机器没装 rapidocr 时它恒为 False，只换引擎不足以让链路走到 OCR。
    """

    def __init__(self, engine):
        self.engine = engine
        self.saved_engine = None
        self.saved_available = None

    def __enter__(self):
        self.saved_engine = ocr._ENGINE
        self.saved_available = ocr._AVAILABLE
        ocr._ENGINE = self.engine
        ocr._AVAILABLE = True
        return self

    def __exit__(self, *exc):
        ocr._ENGINE = self.saved_engine
        ocr._AVAILABLE = self.saved_available
        return False


# ── ① 逐轴换算 ────────────────────────────────────────────────────────
def t_point_bbox_scales_each_axis():
    """x 用横向系数、y 用纵向系数，两个方向不共享一个数。"""
    box = [[100.0, 200.0], [300.0, 200.0], [300.0, 260.0], [100.0, 260.0]]
    out = ocr._to_point_bbox(box, 0.5, 0.25)
    assert out == [50.0, 50.0, 150.0, 65.0], f"逐轴换算结果不对：{out}"
    # 若两个方向共用一个系数（常见写法），x 会算成 300*0.25=75
    assert out[2] == 150.0, "x 方向没有用横向系数"


# ── ② 与独立算一遍的期望值一致 ────────────────────────────────────────
def t_point_bbox_matches_independent_math():
    """自己渲染同一页、自己算一遍期望值，再和模块产出的框比。

    这是 M2 的硬性验收项：坐标必须缩放回 PDF point。
    测试不复用模块的换算函数，而是拿 page.rect 与位图尺寸重算，
    所以「忘了乘系数」这种缺陷一定会被抓到。
    """
    path = _corpus_path("KB-007")
    doc = pymupdf.open(path)
    page = doc[0]
    pix = page.get_pixmap(dpi=TEST_DPI)
    scale_x = page.rect.width / pix.width
    scale_y = page.rect.height / pix.height
    expected = [
        round(min(p[0] for p in FAKE_PIXEL_BOX) * scale_x, 2),
        round(min(p[1] for p in FAKE_PIXEL_BOX) * scale_y, 2),
        round(max(p[0] for p in FAKE_PIXEL_BOX) * scale_x, 2),
        round(max(p[1] for p in FAKE_PIXEL_BOX) * scale_y, 2),
    ]
    page_width, page_height = page.rect.width, page.rect.height
    doc.close()

    # 期望值与原始像素值必须不同，否则这条测试没有区分度
    assert abs(expected[2] - max(p[0] for p in FAKE_PIXEL_BOX)) > 100, \
        "期望值没体现出缩放，测试本身失效"

    with tempfile.TemporaryDirectory() as tmp:
        with _Env(OCR_ENABLED="1", OCR_RENDER_DPI=str(TEST_DPI), OCR_CACHE_DIR=tmp):
            engine = FakeEngine()
            ocr.reset_stats()
            with _FakeEnginePatch(engine):
                doc = pymupdf.open(path)
                blocks = ocr.ocr_page(path, doc[0], 1)
                doc.close()

    assert len(blocks) == 1, f"应产出 1 个块，实际 {len(blocks)}"
    block = blocks[0]
    assert block.bbox == expected, f"换算后的框 {block.bbox} != 期望 {expected}"
    x0, y0, x1, y1 = block.bbox
    assert 0 <= x0 < x1 <= page_width, f"横向超出页面：{block.bbox} vs 宽 {page_width}"
    assert 0 <= y0 < y1 <= page_height, f"纵向超出页面：{block.bbox} vs 高 {page_height}"
    assert block.page == 1 and block.text == FAKE_TEXT
    assert block.size == 0.0, "OCR 块的字号必须是 0（理由见 ocr 模块 docstring）"
    assert block.page_width == page_width and block.page_height == page_height, \
        "块必须带页面原始尺寸，否则前端换算不出渲染像素"


# ── ③ 文件内容摘要 ────────────────────────────────────────────────────
def t_file_key_stable_and_sensitive():
    path = _corpus_path("KB-007")
    first = ocr.file_key(path)
    assert first == ocr.file_key(path), "同一文件两次摘要不同"
    # 换一个路径指向同一内容：摘要应一致（缓存键跟着内容走，不跟路径走）
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "renamed.pdf")
        shutil.copyfile(path, copy)
        assert ocr.file_key(copy) == first, "同内容不同路径的摘要应一致"
        # 内容改一个字节：摘要必须变，否则会命中过期缓存
        with open(copy, "ab") as f:
            f.write(b"x")
        assert ocr.file_key(copy) != first, "内容变了摘要却没变"


# ── ④ 页级判据 ────────────────────────────────────────────────────────
def t_page_level_dispatch_judgement():
    text_doc = pymupdf.open(_corpus_path("KB-001"))
    assert ocr.page_needs_ocr(text_doc[0]) is False, \
        "有文本层的页不该被判成需要 OCR"
    text_doc.close()

    scan_doc = pymupdf.open(_corpus_path("KB-007"))
    assert ocr.page_needs_ocr(scan_doc[0]) is True, "扫描页应被判成需要 OCR"
    scan_doc.close()


# ── ⑤ 依赖缺失时降级 ──────────────────────────────────────────────────
def t_degrades_without_engine():
    path = _corpus_path("KB-007")
    with tempfile.TemporaryDirectory() as tmp:
        with _Env(OCR_ENABLED="1", OCR_CACHE_DIR=tmp):
            saved = ocr._AVAILABLE
            ocr._AVAILABLE = False
            try:
                ocr.reset_stats()
                doc = pymupdf.open(path)
                blocks = ocr.ocr_page(path, doc[0], 1)
                doc.close()
                stats = ocr.stats()
            finally:
                ocr._AVAILABLE = saved
    assert blocks == [], "依赖缺失时应返回空列表而不是抛异常"
    assert stats["pages_skipped_no_engine"] == 1, \
        f"应记一条跳过，实际统计 {stats}"
    assert stats["available"] is False


# ── ⑥ 开关关闭时降级 ──────────────────────────────────────────────────
def t_degrades_when_disabled():
    path = _corpus_path("KB-007")
    with tempfile.TemporaryDirectory() as tmp:
        with _Env(OCR_ENABLED="0", OCR_CACHE_DIR=tmp):
            engine = FakeEngine()
            ocr.reset_stats()
            with _FakeEnginePatch(engine):
                doc = pymupdf.open(path)
                blocks = ocr.ocr_page(path, doc[0], 1)
                doc.close()
    assert blocks == [], "开关关闭时应返回空"
    assert engine.calls == 0, "开关关闭时不该调用引擎"
    assert ocr.stats()["pages_skipped_no_engine"] >= 1


# ── ⑦ 缓存命中不重复跑引擎 ────────────────────────────────────────────
def t_cache_avoids_second_engine_call():
    path = _corpus_path("KB-007")
    with tempfile.TemporaryDirectory() as tmp:
        with _Env(OCR_ENABLED="1", OCR_RENDER_DPI=str(TEST_DPI), OCR_CACHE_DIR=tmp):
            ocr.reset_stats()
            with _FakeEnginePatch(FakeEngine()) as patch:
                doc = pymupdf.open(path)
                first = ocr.ocr_page(path, doc[0], 1)
                second = ocr.ocr_page(path, doc[0], 1)
                doc.close()
                calls = patch.engine.calls
            stats = ocr.stats()

    assert calls == 1, f"第二次应命中缓存，引擎实际被调 {calls} 次"
    assert len(first) == len(second) == 1
    assert [b.bbox for b in first] == [b.bbox for b in second], "缓存前后结果不一致"
    assert [b.text for b in first] == [b.text for b in second]
    assert stats["pages_cached"] == 1, f"应记一次缓存命中，实际 {stats}"


# ── ⑧ loader 分派 ─────────────────────────────────────────────────────
def t_loader_dispatches_scan_to_ocr():
    scan = _corpus_path("KB-007")
    text_path = _corpus_path("KB-001")
    with tempfile.TemporaryDirectory() as tmp:
        with _Env(OCR_ENABLED="1", OCR_RENDER_DPI=str(TEST_DPI), OCR_CACHE_DIR=tmp):
            # 扫描件：OCR 可用时应产出块
            engine = FakeEngine()
            with _FakeEnginePatch(engine):
                blocks = loader.extract_blocks(scan)
            assert len(blocks) > 0, "扫描件走 OCR 后应产出文本块"
            assert blocks[0].text == FAKE_TEXT
            assert blocks[0].extraction_method == "ocr"
            assert engine.calls == 1, "同一页只该 OCR 一次"

            # 有文本层的文档：一次都不该触发 OCR
            engine2 = FakeEngine()
            with _FakeEnginePatch(engine2):
                text_blocks = loader.extract_blocks(text_path)
                ocr_lines = loader.extract_lines(text_path)
            assert len(text_blocks) > 0
            assert {block.extraction_method for block in text_blocks} == {"text_layer"}
            assert engine2.calls == 0, \
                "有文本层的文档不该触发 OCR（否则会白花 7 秒/页）"
            assert len(ocr_lines) > 0

            # 同一份扫描件，extract_blocks 与 extract_lines 共用缓存：
            # 前者已 OCR 过，后者不该再调引擎
            engine3 = FakeEngine()
            with _FakeEnginePatch(engine3):
                lines = loader.extract_lines(scan)
            assert len(lines) > 0, "toc 用的行也得能拿到 OCR 结果"
            assert engine3.calls == 0, \
                "extract_lines 应命中 extract_blocks 写下的缓存，不重复 OCR"
            assert lines[0][0] == FAKE_TEXT and lines[0][1] == 1


# ── ⑨ corpus 摘要 ─────────────────────────────────────────────────────
def t_corpus_summary_explains_ocr():
    class _Dummy:
        pass

    def make(ocr_info, empty=()):
        idx = CorpusIndex(bm25=None, store=None, embedder=None, chunks=[])
        idx.ocr = ocr_info
        idx.empty = list(empty)
        return idx.summary()

    text = make({"enabled": False})
    assert "OCR 已关闭" in text, text

    text = make({"enabled": True, "available": False}, empty=["KB-007_x"])
    assert "未装 rapidocr" in text, text
    assert "装上后才会进检索池" in text, text

    text = make({"enabled": True, "available": True, "pages_ocr": 2,
                 "pages_cached": 0, "seconds": 18.0})
    assert "识别 2 页" in text and "9.0 秒/页" in text, text

    text = make({"enabled": True, "available": True, "pages_ocr": 0,
                 "pages_cached": 2, "seconds": 0.0})
    assert "缓存命中 2 次" in text, text

    # 本轮没有扫描页时不该冒出一句无意义的 0 页
    text = make({"enabled": True, "available": True, "pages_ocr": 0,
                 "pages_cached": 0, "seconds": 0.0})
    assert "没有需要处理的扫描页" in text, text


# ── ⑩ 提取方式 ───────────────────────────────────────────────────────
def t_chunks_carry_extraction_method():
    """同一页同一来源的块合并后，切片要带上这个来源标记。"""
    blocks = [
        Block(page=1, bbox=[0, 0, 10, 10], text="第一行", size=12.0,
              extraction_method="text_layer"),
        Block(page=1, bbox=[0, 11, 10, 20], text="第二行", size=12.0,
              extraction_method="text_layer"),
    ]
    chunks = build_chunks(blocks, [], "same")
    assert len(chunks) == 1, [c.text for c in chunks]
    assert chunks[0].extraction_method == "text_layer"


def t_page_boundary_keeps_sources_apart():
    """跨页不合并，来源因此天然唯一。

    loader 按整页分派（整页走 OCR 或整页走文本层），chunker 又要求同页才合并，
    所以一个切片不会混两种来源。将来若放宽同页合并，这条会先红。
    """
    blocks = [
        Block(page=1, bbox=[0, 0, 10, 10], text="文本层那页", size=12.0,
              extraction_method="text_layer"),
        Block(page=2, bbox=[0, 0, 10, 10], text="扫描件那页", size=0.0,
              extraction_method="ocr"),
    ]
    chunks = build_chunks(blocks, [], "mixed")
    assert [c.extraction_method for c in chunks] == ["text_layer", "ocr"], \
        [(c.text, c.extraction_method) for c in chunks]


def t_loader_labels_plain_and_docx_without_ocr():
    plain_blocks = loader.extract_blocks(_corpus_path("KB-010"))
    docx_blocks = loader.extract_blocks(_corpus_path("KB-005"))
    assert plain_blocks and {block.extraction_method for block in plain_blocks} == {"plain"}
    assert docx_blocks and {block.extraction_method for block in docx_blocks} == {"text_layer"}


def t_parse_info_records_ocr_without_reprocessing():
    scan = _corpus_path("KB-007")
    blocks = [Block(page=1, bbox=[0, 0, 10, 10], text="OCR 行", size=0.0,
                    extraction_method="ocr")]
    info = _document_parse_info(
        scan,
        blocks,
        {"pages_ocr": 1, "pages_cached": 2, "seconds": 3.5},
        {"pages_ocr": 2, "pages_cached": 3, "seconds": 7.75},
    )
    assert info["extraction_methods"] == ["ocr"]
    assert info["block_count"] == info["ocr_block_count"] == 1
    assert info["ocr"]["pages_ocr"] == 1
    assert info["ocr"]["cache_hits"] == 1
    assert info["ocr"]["elapsed_ms"] == 4250.0


def t_parse_provenance_trace_only_includes_retrieved_documents():
    class _Index:
        parse_info = {
            "scan": {
                "extraction_methods": ["ocr"],
                "block_count": 9,
                "ocr_block_count": 9,
                "ocr": {"pages_ocr": 1, "cache_hits": 1,
                        "elapsed_ms": 3500.0, "average_score": 0.9664},
            },
            "plain": {"extraction_methods": ["plain"], "block_count": 6, "ocr_block_count": 0},
        }

    refs = [
        DocRef(doc_id="scan", snippet="扫描件片段"),
        DocRef(doc_id="scan", snippet="同一文档第二段"),
        DocRef(doc_id="missing", snippet="未知文档"),
    ]
    step = _parse_provenance_step(_Index(), refs, step_id=1)
    assert step is not None
    assert step.tool == "parse_provenance" and step.elapsed_ms == 0.0
    assert step.input == {"doc_ids": ["scan"]}
    assert step.output["recorded_at"] == "index_build"
    assert step.output["documents"] == [{"doc_id": "scan", **_Index.parse_info["scan"]}]


def t_answer_prepends_parse_provenance_without_changing_refs():
    class _Index:
        parse_info = {
            "scan": {"extraction_methods": ["ocr"], "block_count": 9, "ocr_block_count": 9}
        }

    ref = DocRef(doc_id="scan", snippet="扫描件片段", extraction_method="ocr")
    tool = object.__new__(RagAnswerTool)
    tool.corpus_index = _Index()
    tool.retrieve_and_rank = lambda question, doc_id: {"retrieve_refs": [ref], "context_hits": []}
    tool.retrieve_steps = lambda result, step_id, doc_id: [
        TraceStep(step_id=step_id, tool="hybrid_retrieve", elapsed_ms=1.0, hit=[ref]),
        TraceStep(step_id=step_id + 1, tool="rerank", elapsed_ms=1.0, hit=[ref]),
    ]
    # 签名要与 `RagAnswerTool.answer` 实际传入的实参对齐：2026-10-17 起
    # answer() 会额外传 known_facts（跨源题把数据库已算出的事实交给文档侧），
    # 假件不收这个关键字就会 TypeError。
    tool.generate = lambda question, context_hits, known_facts=None: {
        "text": "答案", "final_refs": [ref], "context_num": 0,
    }
    tool.generate_step = lambda question, generated, step_id: TraceStep(
        step_id=step_id, tool="llm_generate", elapsed_ms=1.0, hit=[ref]
    )

    result = RagAnswerTool.answer(tool, "问题")
    assert [step.step_id for step in result.trace] == [1, 2, 3, 4]
    assert [step.tool for step in result.trace] == [
        "parse_provenance", "hybrid_retrieve", "rerank", "llm_generate",
    ]
    assert result.refs == [ref], "解析事实只能补 trace，不能改答案引用"


if __name__ == "__main__":
    tests = [
        ("① 像素框逐轴换算", t_point_bbox_scales_each_axis),
        ("② 换算与独立算的期望值一致", t_point_bbox_matches_independent_math),
        ("③ 文件内容摘要稳定且敏感", t_file_key_stable_and_sensitive),
        ("④ 页级判据", t_page_level_dispatch_judgement),
        ("⑤ 依赖缺失时降级", t_degrades_without_engine),
        ("⑥ 开关关闭时降级", t_degrades_when_disabled),
        ("⑦ 缓存命中不重复跑引擎", t_cache_avoids_second_engine_call),
        ("⑧ loader 分派扫描件与文本层", t_loader_dispatches_scan_to_ocr),
        ("⑨ corpus 摘要说清 OCR 状态", t_corpus_summary_explains_ocr),
        ("⑩ 切片带上来源标记", t_chunks_carry_extraction_method),
        ("⑪ 跨页不合并所以来源唯一", t_page_boundary_keeps_sources_apart),
        ("⑫ loader 标注纯文本与 DOCX 文本层", t_loader_labels_plain_and_docx_without_ocr),
        ("⑬ 解析事实记录 OCR 块与耗时", t_parse_info_records_ocr_without_reprocessing),
        ("⑭ trace 只展示命中文档的解析事实", t_parse_provenance_trace_only_includes_retrieved_documents),
        ("⑮ answer 追加解析事实且保持引用", t_answer_prepends_parse_provenance_without_changing_refs),
    ]
    for name, fn in tests:
        check(name, fn)
    print(f"\nALL PASSED ({PASSED}/{len(tests)})")
