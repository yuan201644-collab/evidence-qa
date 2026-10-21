# -*- coding: utf-8 -*-
"""RAG 解析层（loader / toc / chunker）的回归测试。

为什么必须有这一组：
    纯文本来源（md / txt）没有版面坐标，早期把 bbox 一律填 0，导致
    chunker 判断章节归属时「每个章节都在第一个块之前」恒成立，
    所有章节被一次压进栈，栈顶只剩最后一章 —— 实测 KB-010.md 的**全部**
    切片章节路径都变成 ['5 版本变更']，引用卡片指错章节。
    这类故障不抛异常、不影响「答案看起来对不对」，只在引用卡片和
    评测的块锚点上静默错位，所以必须有断言守住。

    修复方式是把行号写进 bbox 的 y（见 loader 的 _plain_text_lines），
    前提是「空行也占一个行号」，且 toc 与 loader 共用同一套行号。
    这两条约定一旦被谁改掉，本组测试就会红。

跑法：
    D:/python/python.exe app/test_rag_ingest.py
    .venv/Scripts/python.exe app/test_rag_ingest.py

覆盖：
    ① 纯文本行号：逐行编号、空行占号、y0 == y1 == 行号且 x 为 0
    ② 块的伪坐标是「文件里的绝对行号」（空行被跳过但行号不回收）
    ③ Markdown 的多级标题各自归属正确章节（本组测试的核心）
    ④ 纯文本切片的 bboxes 为空（不把伪坐标当版面框交给前端）
    ⑤ 真实语料 KB-010.md 的章节路径完整且分散（缺陷现场）
    ⑥ PDF 切片的 bboxes 非空（退化框过滤没误伤有版面坐标的来源）
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag.ingest.chunker import build_chunks                    # noqa: E402
from app.rag.ingest.loader import (                                # noqa: E402
    _blocks_from_plain_text,
    _plain_text_lines,
    extract_blocks,
)
from app.rag.schemas import Block, SectionNode                      # noqa: E402
from app.rag.ingest.toc import (                                    # noqa: E402
    _match_heading,
    _normalize_title,
    build_tree,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = PROJECT_ROOT / "data" / "corpus" / "raw"

PASSED = 0


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


#: 造一份最小 Markdown，形状对齐真实语料 KB-010.md：
#: 首行是文档标题（无编号，不该产生章节）、章节之间空行隔开、每章一句正文。
#: 行号（1 起算）：
#:   1  # 文档标题
#:   2  （空）
#:   3  # 1　第一章标题
#:   4  第一章正文甲。
#:   5  （空）
#:   6  # 2　第二章标题
#:   7  第二章正文乙。
#:   8  （空）
#:   9  # 3　第三章标题
#:  10  第三章正文丙。
SAMPLE_MD = "\n".join([
    "# 文档标题",
    "",
    "# 1\u3000第一章标题",
    "第一章正文甲。",
    "",
    "# 2\u3000第二章标题",
    "第二章正文乙。",
    "",
    "# 3\u3000第三章标题",
    "第三章正文丙。",
])


class _TempMd:
    """把 SAMPLE_MD 落到临时文件，用完自动清理。"""

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "sample.md")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(SAMPLE_MD)
        return self.path

    def __exit__(self, *exc):
        self._dir.cleanup()
        return False


def _corpus_path(prefix):
    """按 KB 编号前缀找语料文件，找不到就报错（语料随仓库跟踪，不该缺）。"""
    for name in sorted(os.listdir(CORPUS_DIR)):
        if name.startswith(prefix):
            return str(CORPUS_DIR / name)
    raise AssertionError("语料缺失：" + prefix + " 不在 " + str(CORPUS_DIR))


# ── ① 纯文本行号 ──────────────────────────────────────────────────────
def t_plain_text_line_numbering():
    with _TempMd() as path:
        lines = _plain_text_lines(path)
        raw = open(path, encoding="utf-8").read().splitlines()
        assert len(lines) == len(raw), \
            f"行数不一致：_plain_text_lines {len(lines)} 行 vs 文件 {len(raw)} 行"
        for index in range(len(lines)):
            text, page, bbox = lines[index]
            line_no = index + 1
            assert text == raw[index], f"第 {line_no} 行文字错位：{text!r}"
            assert page == 1, f"纯文本页码应为 1，实际 {page}"
            assert bbox[0] == 0.0 and bbox[2] == 0.0, \
                f"第 {line_no} 行 x 应为 0（零面积），实际 {bbox}"
            assert bbox[1] == float(line_no) and bbox[3] == float(line_no), \
                f"第 {line_no} 行 y 应为行号 {line_no}，实际 y0={bbox[1]} y1={bbox[3]}"
        # 第 2 行是空行，它也必须占住 2 号
        assert lines[1][0].strip() == "", "造样本的第 2 行本该是空行"
        assert lines[1][2][1] == 2.0, "空行没有占住行号"


# ── ② 块的伪坐标是文件里的绝对行号 ─────────────────────────────────────
def t_blocks_keep_absolute_line_number():
    with _TempMd() as path:
        source = open(path, encoding="utf-8").read().splitlines()
        line_of = {}
        for index in range(len(source)):
            text = source[index].strip()
            if text:
                line_of[text] = index + 1

        blocks = _blocks_from_plain_text(path)
        assert len(blocks) == len(line_of), \
            f"块数应等于非空行数 {len(line_of)}，实际 {len(blocks)}"
        for block in blocks:
            expect = line_of[block.text]
            assert block.bbox[1] == float(expect), (
                f"块 {block.text!r} 的伪坐标应为绝对行号 {expect}，"
                f"实际 {block.bbox[1]}（空行被回收行号时就会这样）"
            )
        # 第一章正文落在第 4 行，前面跨过一个空行，行号必须是 4 而不是 3
        body = [b for b in blocks if b.text == "第一章正文甲。"][0]
        assert body.bbox[1] == 4.0, f"跨空行后的块行号应为 4，实际 {body.bbox[1]}"


# ── ③ 多级标题各自归属正确章节（核心回归） ────────────────────────────
def t_markdown_sections_split_by_heading():
    with _TempMd() as path:
        chunks = build_chunks(
            _blocks_from_plain_text(path), build_tree(path), "sample"
        )
        assert len(chunks) > 0, "样本切不出任何切片"

        def section_of(keyword):
            for c in chunks:
                if keyword in c.text:
                    return c.section_path
            raise AssertionError(f"没有切片包含 {keyword!r}")

        assert section_of("# 文档标题") == [], \
            "文档大标题没有编号，不该产生章节，其内容应归到空路径"
        assert section_of("第一章正文甲。") == ["1 第一章标题"], \
            f"第一章正文归属错误：{section_of('第一章正文甲。')}"
        assert section_of("第二章正文乙。") == ["2 第二章标题"], \
            f"第二章正文归属错误：{section_of('第二章正文乙。')}"
        assert section_of("第三章正文丙。") == ["3 第三章标题"], \
            f"第三章正文归属错误：{section_of('第三章正文丙。')}"

        # 关键判据：不能所有切片都堆在最后一章（这正是修复前的故障形态）
        last = ["3 第三章标题"]
        assert not all(c.section_path == last for c in chunks), \
            "全部切片都落在最后一章，章节栈被一次压满了"


# ── ④ 纯文本切片的 bboxes 为空 ────────────────────────────────────────
def t_plain_text_chunks_have_no_bboxes():
    with _TempMd() as path:
        chunks = build_chunks(
            _blocks_from_plain_text(path), build_tree(path), "sample"
        )
        for c in chunks:
            assert c.bboxes == [], (
                "纯文本没有版面坐标，bboxes 应为空；"
                f"实际收到 {c.bboxes}（会把行号伪坐标当高亮框交给前端）"
            )
            assert c.page_width is None and c.page_height is None


# ── ⑤ 真实语料 KB-010.md（缺陷现场） ──────────────────────────────────
def t_real_corpus_kb010_sections():
    path = _corpus_path("KB-010")
    chunks = build_chunks(extract_blocks(path), build_tree(path), "KB-010")
    assert len(chunks) > 0, "KB-010 切不出切片"

    expected = {"1 接口概览", "2 发起问数请求", "3 错误码", "4 限流与重试", "5 版本变更"}
    seen = set()
    for c in chunks:
        if c.section_path:
            seen.add(c.section_path[-1])
    missing = expected - seen
    assert not missing, f"这些章节没有切片归属：{sorted(missing)}"
    assert len(seen) >= 2, f"章节过于集中，只有一个章节有切片：{seen}"
    assert not all(c.section_path and c.section_path[-1] == "5 版本变更" for c in chunks), \
        "回归：全部切片又落到了 ['5 版本变更']"


# ── ⑥ PDF 的 bboxes 不受影响 ──────────────────────────────────────────
def t_pdf_chunks_keep_bboxes():
    path = _corpus_path("KB-001")
    chunks = build_chunks(extract_blocks(path), build_tree(path), "KB-001")
    assert len(chunks) > 0, "KB-001 切不出切片"
    for c in chunks:
        assert len(c.bboxes) > 0, (
            f"PDF 切片应当带版面 bbox，实际为空（页码 {c.page}）：{c.text[:30]!r}"
        )
        for x0, y0, x1, y1 in c.bboxes:
            assert x1 > x0 and y1 > y0, f"PDF 的 bbox 不该是零面积：{(x0, y0, x1, y1)}"


# ── ⑦ 章节标题的空白归一 ──────────────────────────────────────────────
def t_normalize_title_collapses_whitespace():
    """全角空格、连续空白、首尾空白都要压成单个半角空格。

    这是引用卡片与评测块锚点能否对上写法的基础：契约示例与用例都是半角写法
    （`第二章 质保条款`），原文却常用全角空格（`第二章　质保条款`）。
    """
    assert _normalize_title("第二章\u3000数据分级") == "第二章 数据分级", "全角空格没归一"
    assert _normalize_title("第二章   质保条款") == "第二章 质保条款", "连续空格没压成一个"
    assert _normalize_title("\u3000 第二章 质保条款 \u3000") == "第二章 质保条款", "首尾空白没剥掉"
    assert _normalize_title("3\xa0错误码") == "3 错误码", "不换行空格（NBSP）没归一"
    assert _normalize_title("无空白") == "无空白", "无空白的标题被改动"


# ── ⑧ 内嵌书签路径同样要归一（KB-005 是唯一带书签的语料）────────────
def t_bookmark_titles_match_eval_anchor():
    path = _corpus_path("KB-005")
    titles = [s.title for s in build_tree(path)]
    assert len(titles) > 0, "KB-005 应当有内嵌书签，实际一个章节都没建出来"
    for t in titles:
        assert "\u3000" not in t, f"章节标题残留全角空格：{t!r}"
    # 用例手写的锚点是半角写法，解析产出必须与它字面一致，否则块锚点判定必失配
    assert "第二章 数据分级" in titles, f"标题写法与用例锚点不一致：{titles}"


# ── ⑨ 乱序正文条款按编号归属章节 ─────────────────────────────────────
def t_numbered_clauses_follow_section_number_not_visual_order():
    """目录顺序被打乱时，正文条款编号仍应定位到对应章节。"""
    blocks = [
        Block(page=1, bbox=[10, 10, 100, 20], text="1 人员出入管理", size=12),
        Block(page=1, bbox=[10, 25, 300, 35], text="5.2 发生火警时，按预案处置。", size=12),
        Block(page=1, bbox=[10, 40, 100, 50], text="2 温湿度管理", size=12),
        Block(page=1, bbox=[10, 55, 300, 65], text="1.2 禁止携带易燃物品进入机房。", size=12),
        Block(page=1, bbox=[10, 70, 100, 80], text="5 应急处置", size=12),
    ]
    sections = [
        SectionNode(level=1, title="1 人员出入管理", page=1, bbox=[10, 10, 100, 20]),
        SectionNode(level=1, title="2 温湿度管理", page=1, bbox=[10, 40, 100, 50]),
        SectionNode(level=1, title="5 应急处置", page=1, bbox=[10, 70, 100, 80]),
    ]
    chunks = build_chunks(blocks, sections, "KB-015")

    def section_of(keyword):
        for chunk in chunks:
            if keyword in chunk.text:
                return chunk.section_path
        raise AssertionError(f"没有切片包含 {keyword!r}")

    assert section_of("5.2 发生火警") == ["5 应急处置"], (
        f"5.2 条款应归到第 5 章，实际为 {section_of('5.2 发生火警')}"
    )
    assert section_of("1.2 禁止携带") == ["1 人员出入管理"], (
        f"1.2 条款应归到第 1 章，实际为 {section_of('1.2 禁止携带')}"
    )


# ── ⑩ OCR 枚举条款标题提取 ───────────────────────────────────────────
def t_chinese_numbered_ocr_clause_labels_become_sections():
    """扫描件常见的「一、字段名：内容」应保留可读章节锚点。"""
    examples = (
        ("一、投标保证金：人民币8万元，须于投标截止时间前缴纳。", "一 投标保证金"),
        ("二、交货期：合同签订之日起60日历天内完成到货与安装。", "二 交货期"),
        ("三、质保期：验收合格之日起3年。", "三 质保期"),
        ("四、付款方式：到货验收后支付30%。", "四 付款方式"),
        ("五、技术参数要求：显存不低于80GB。", "五 技术参数要求"),
        ("六、投标截止时间：2026年6月30日9:30。", "六 投标截止时间"),
    )
    for line, expected_title in examples:
        level, title = _match_heading(line)
        assert level == 1 and title == expected_title, (
            f"枚举条款标题识别错误：{line!r} -> {(level, title)!r}，"
            f"预期 (1, {expected_title!r})"
        )


if __name__ == "__main__":
    tests = [
        ("① 纯文本行号：空行占号、y 即行号", t_plain_text_line_numbering),
        ("② 块的伪坐标是绝对行号", t_blocks_keep_absolute_line_number),
        ("③ 多级标题各自归属正确章节", t_markdown_sections_split_by_heading),
        ("④ 纯文本切片 bboxes 为空", t_plain_text_chunks_have_no_bboxes),
        ("⑤ 真实语料 KB-010.md 章节完整", t_real_corpus_kb010_sections),
        ("⑥ PDF 切片仍带版面 bbox", t_pdf_chunks_keep_bboxes),
        ("⑦ 标题空白归一（全角/连续/首尾）", t_normalize_title_collapses_whitespace),
        ("⑧ 内嵌书签标题与用例锚点写法一致", t_bookmark_titles_match_eval_anchor),
        ("⑨ 乱序条款按编号归属章节", t_numbered_clauses_follow_section_number_not_visual_order),
        ("⑩ OCR 枚举条款字段名形成章节锚点", t_chinese_numbered_ocr_clause_labels_become_sections),
    ]
    for name, fn in tests:
        check(name, fn)
    print(f"\nALL PASSED ({PASSED}/{len(tests)})")
