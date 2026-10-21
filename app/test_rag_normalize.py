# -*- coding: utf-8 -*-
"""版面文本规范化（app/rag/ingest/normalize.py）的回归测试。

为什么值得单独一组测试：
    这里三条规则都是**启发式**——什么时候该把两行拼起来、一张文本化表格有几列。
    启发式最怕"改了别处、悄悄改了行为"，所以每条规则都要有正向与反向用例：
    既要证明该合并的合并了，也要证明**不该合并的没合并**（后者更容易出错，
    而且出错的方式是把不相干的行粘成一句，检索直接给错内容）。

跑法：
    .venv/Scripts/python.exe app/test_rag_normalize.py

覆盖：
    ① 硬断行判据：正向（CJK / 数字 / 跨块）
    ② 硬断行判据：反向（句末标点、标题、表格与正文混接、分隔符开头）
    ③ 接缝拼接：CJK 不加空格，拉丁字母之间加空格
    ④ merge_lines：跨行合并、bbox 取并集、字号差太大不合并
    ⑤ 列数推断：三张真实表的单元格序列（4 / 4 / 3 列）
    ⑥ 列数推断：反例必须返回 None（推不出来就不猜）
    ⑦ 表格行还原：表头回填成「列名: 值」，bbox 取贡献行的并集
    ⑧ 真表格转 Markdown（合成一张带框线的表格 PDF，这是 find_tables 的验收对象）
    ⑨ 真实语料：KB-014 环境表 4 行、KB-011 得分表 8 行、KB-014 的「相对湿度」不再被切断
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf                                                    # noqa: E402

from app.rag.ingest import normalize                              # noqa: E402
from app.rag.ingest.loader import extract_blocks, extract_lines    # noqa: E402

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


def _corpus_path(prefix):
    for name in sorted(os.listdir(CORPUS_DIR)):
        if name.startswith(prefix):
            return str(CORPUS_DIR / name)
    raise AssertionError("语料缺失：" + prefix)


def _heading(text):
    """测试里用的标题判定，直接复用生产口径。"""
    from app.rag.ingest.loader import _heading_predicate

    return _heading_predicate()(text)


# 三张真实表的单元格序列（从语料实测抄下来，见 KB-014 / KB-011）
CELLS_ENV = ["检查项", "标准范围", "实测值", "结论",
             "温度", "18℃~27℃", "22.6℃", "合格",
             "相对湿度", "30%~70%", "45%", "合格",
             "正压值", "不小于 5Pa", "8Pa", "合格",
             "噪声", "不高于 65dB", "58dB", "合格"]
CELLS_DEVICE = ["设备编号", "设备类型", "运行状态", "告警数",
                "CU\u2010A3\u2010001", "GPU 服务器", "正常", "0",
                "CU\u2010A3\u2010002", "GPU 服务器", "正常", "0",
                "ST\u2010A3\u2010011", "分布式存储节点", "正常", "1",
                "NW\u2010A3\u2010005", "核心交换机", "正常", "0"]
CELLS_SCORE = ["验收项", "权重", "实得分",
               "功能完整性", "20%", "19",
               "性能达标率", "20%", "17",
               "接口规范性", "15%", "15",
               "文档完整性", "15%", "13",
               "数据准确性", "10%", "10",
               "安全合规性", "10%", "9",
               "可维护性", "5%", "4",
               "培训完成度", "5%", "5"]


# ── ① 硬断行判据（正向） ──────────────────────────────────────────────
def t_continuation_positive():
    cases = [
        ("|　合格　|　相对湿", "度　|　30%~70%　|　45%", "中文词被硬切"),
        ("　噪声　|　不高于 6", "5dB　|　58dB　|　合格", "数字被硬切"),
        ("3.1　ST-A3-011 存储节点…序列号 SN-8823917，已生", "成更换工单 GD-20260910-07。", "中文词组被硬切"),
        ("验收日期: 2026 年 8 月 12 ", "日", "日期被硬切"),
        ("可维护性 | 5% | 4　|　培", "训完成度 | 5% | 5", "表格单元格被硬切"),
    ]
    for prev, nxt, why in cases:
        assert normalize.looks_like_continuation(prev, nxt, is_heading=_heading), (
            "该合并却没合并（%s）：%r + %r" % (why, prev, nxt))


# ── ② 硬断行判据（反向） ──────────────────────────────────────────────
def t_continuation_negative():
    cases = [
        ("3.2　其余设备无异常。", "四、下次巡检安排", "prev 以句号结尾"),
        ("本次验收覆盖需求分析…不含后续运维服务。", "2　验收项与结论", "prev 以句号结尾"),
        ("一、环境参数记录", "　检查项　|　标准范围", "prev 是标题"),
        ("1.2　服务内容：故障响应、备件更换", "第一章　服务范围", "next 是标题"),
        ("验收项共 8 项，各项权重与得分如下表。", "验收项: 功能完整性 | 权重: 20%", "表格与正文之间"),
        ("验收项: 培训完成度 | 权重: 5% | 实得分: 5", "加权总分 = 19×0.2 + … = 92.15 分。", "表格与正文之间"),
        ("甲方信息如下", "| 名称 | 地址 |", "next 以分隔符开头"),
        ("", "随便什么", "prev 为空"),
    ]
    for prev, nxt, why in cases:
        assert not normalize.looks_like_continuation(prev, nxt, is_heading=_heading), (
            "不该合并却合并了（%s）：%r + %r" % (why, prev, nxt))


# ── ③ 接缝拼接 ────────────────────────────────────────────────────────
def t_join_seam():
    assert normalize.join_seam("相对湿", "度") == "相对湿度", "中文之间不该加空格"
    assert normalize.join_seam("不高于 6", "5dB") == "不高于 65dB", "数字之间不该加空格"
    assert normalize.join_seam("共 8", "项") == "共 8项", "数字接中文不加空格"
    assert normalize.join_seam("hello", "world") == "hello world", "拉丁词之间要加空格"
    assert normalize.join_seam("模型", "GPT-4") == "模型GPT-4", "中文接拉丁不加空格（保持原样）"


# ── ④ merge_lines ─────────────────────────────────────────────────────
def t_merge_lines():
    lines = [
        ("|　合格　|　相对湿", [40.0, 100.0, 200.0, 110.0], 10.5),
        ("度　|　30%~70%", [40.0, 112.0, 200.0, 122.0], 10.5),
        ("下一条完全独立。", [40.0, 140.0, 200.0, 150.0], 10.5),
    ]
    merged = normalize.merge_lines(lines, is_heading=_heading)
    assert len(merged) == 2, f"应合并成 2 行，实际 {len(merged)}：{[m[0] for m in merged]}"
    text, bbox, size = merged[0]
    assert text == "|　合格　|　相对湿度　|　30%~70%", f"合并后的文本不对：{text!r}"
    assert bbox == [40.0, 100.0, 200.0, 122.0], f"bbox 应是并集：{bbox}"
    assert size == 10.5

    # 字号差太大不合并（标题转正文）
    lines = [
        ("机房运维巡视检查记录表", [56.0, 49.8, 221.0, 65.5], 15.0),
        ("机房名称: A3 机房", [56.0, 71.5, 340.7, 82.4], 10.5),
    ]
    merged = normalize.merge_lines(lines, is_heading=_heading)
    assert len(merged) == 2, f"字号不同不该合并，实际 {len(merged)}"


# ── ⑤ 列数推断（真实表） ──────────────────────────────────────────────
def t_infer_columns_real_tables():
    assert normalize.infer_columns(CELLS_ENV) == 4, "环境记录表应为 4 列"
    assert normalize.infer_columns(CELLS_DEVICE) == 4, "设备状态表应为 4 列（首列是编号）"
    assert normalize.infer_columns(CELLS_SCORE) == 3, "验收得分表应为 3 列"


# ── ⑥ 列数推断（反例） ────────────────────────────────────────────────
def t_infer_columns_rejects():
    assert normalize.infer_columns([]) is None
    assert normalize.infer_columns(["只有一个"]) is None
    # 单元格太少
    assert normalize.infer_columns(["甲", "乙", "丙"]) is None
    # 首列不像名称（散文里出现的竖线）
    assert normalize.infer_columns(["3.1", "ST-A3-011 存储节点存在 1 条告警"]) is None
    # 表头里出现值（22.6℃ 不该出现在表头）
    assert normalize.infer_columns(["检查项", "22.6℃", "相对湿度", "45%"]) is None
    # 总数除不尽任何候选列数
    assert normalize.infer_columns(["甲", "乙", "丙", "丁", "戊"]) is None
    # 每种切法都出现「首列不是名称」或「列内取值不同类」
    assert normalize.infer_columns(["名称一", "10", "名称二", "20", "30", "名称三"]) is None, \
        "没有一种切法能让各列同类，应当拒绝"


# ── ⑦ 表格行还原 ──────────────────────────────────────────────────────
def t_reconstruct_rows():
    lines = [
        ("前面一段正文。", [40.0, 90.0, 500.0, 100.0], 10.5),
        ("　检查项　|　标准范围　|　实测值　|　结论　|　温度　|　18℃~27℃　|　22.6℃　|　合格",
         [40.0, 111.0, 530.0, 122.0], 10.5),
        ("相对湿度　|　30%~70%　|　45%　|　合格",
         [40.0, 127.0, 530.0, 138.0], 10.5),
    ]
    out = normalize.reconstruct_table_rows(lines)
    assert out is not None, "应当能还原出表格行"
    assert len(out) == 3, f"应是 正文 + 2 行表，实际 {len(out)}"
    assert out[0][0] == "前面一段正文。", "非表格行要原样保留"
    assert out[1][0] == "检查项: 温度 | 标准范围: 18℃~27℃ | 实测值: 22.6℃ | 结论: 合格", out[1][0]
    assert out[2][0] == "检查项: 相对湿度 | 标准范围: 30%~70% | 实测值: 45% | 结论: 合格", out[2][0]
    # bbox 取贡献行的并集：第二行数据只来自第二行文本
    assert out[2][1] == [40.0, 127.0, 530.0, 138.0], out[2][1]

    # 推不出列数时原样返回 None，调用方保留原文
    no_table = [("普通一行", [0.0, 0.0, 10.0, 10.0], 10.5)]
    assert normalize.reconstruct_table_rows(no_table) is None


# ── ⑧ 真表格转 Markdown ───────────────────────────────────────────────
def t_markdown_table_from_pdf():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    x0, y0, cw, rh = 56.0, 100.0, 160.0, 24.0
    for i in range(4):
        page.draw_line(pymupdf.Point(x0 + i * cw, y0), pymupdf.Point(x0 + i * cw, y0 + 3 * rh))
    for j in range(4):
        page.draw_line(pymupdf.Point(x0, y0 + j * rh), pymupdf.Point(x0 + 3 * cw, y0 + j * rh))
    cells = [["验收项", "权重", "实得分"], ["功能完整性", "20%", "19"], ["性能达标率", "20%", "17"]]
    for r in range(3):
        for c in range(3):
            page.insert_text(pymupdf.Point(x0 + c * cw + 8, y0 + r * rh + 16),
                             cells[r][c], fontname="china-s", fontsize=10)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "table.pdf")
        doc.save(path)
        doc.close()
        blocks = extract_blocks(path)

    texts = [b.text for b in blocks]
    markdown = [t for t in texts if t.startswith("|")]
    assert len(markdown) == 1, f"应产出一个 Markdown 表格块，实际 {len(markdown)} 个：{texts}"
    table = markdown[0]
    assert "| 验收项 | 权重 | 实得分 |" in table, table
    assert "| 功能完整性 | 20% | 19 |" in table, table
    assert "| --- | --- | --- |" in table, "Markdown 表头分隔行缺失"
    # 表格区域内的原始行必须被剔除，否则同一内容进索引两遍
    assert not any("功能完整性" in t and not t.startswith("|") for t in texts), \
        f"表格内容出现了两种形态：{texts}"


# ── ⑨ 真实语料 ────────────────────────────────────────────────────────
def t_real_corpus_tables():
    kb014 = _corpus_path("KB-014")
    blocks = extract_blocks(kb014)
    texts = [b.text for b in blocks]

    env_rows = [t for t in texts if t.startswith("检查项:")]
    assert len(env_rows) == 4, f"环境参数表应还原出 4 行，实际 {len(env_rows)}：{env_rows}"
    assert any("实测值: 45%" in t for t in env_rows), "相对湿度那一行的实测值应是 45%"

    device_rows = [t for t in texts if t.startswith("设备编号:")]
    assert len(device_rows) == 4, f"设备状态表应还原出 4 行，实际 {len(device_rows)}"
    assert any("ST\u2010A3\u2010011" in t and "告警数: 1" in t for t in device_rows), \
        "ST-A3-011 那行的告警数应是 1"

    # 硬断行必须已经拼回来：整份文档里不该再出现被切断的「相对湿」
    merged_lines = [text for text, _page, _bbox in extract_lines(kb014)]
    assert not any(text.rstrip().endswith("相对湿") for text in merged_lines), \
        "「相对湿度」仍被切断"
    assert any("相对湿度" in text for text in texts), "「相对湿度」没有拼回成完整词"

    kb011 = _corpus_path("KB-011")
    score_rows = [b.text for b in extract_blocks(kb011) if b.text.startswith("验收项:")]
    assert len(score_rows) == 8, f"验收得分表应还原出 8 行，实际 {len(score_rows)}"
    assert any("验收项: 功能完整性 | 权重: 20% | 实得分: 19" == t for t in score_rows), score_rows


if __name__ == "__main__":
    tests = [
        ("① 硬断行判据（正向）", t_continuation_positive),
        ("② 硬断行判据（反向）", t_continuation_negative),
        ("③ 接缝拼接", t_join_seam),
        ("④ merge_lines 合并与字号守卫", t_merge_lines),
        ("⑤ 列数推断（三张真实表）", t_infer_columns_real_tables),
        ("⑥ 列数推断（反例返回 None）", t_infer_columns_rejects),
        ("⑦ 表格行还原与表头回填", t_reconstruct_rows),
        ("⑧ 真表格转 Markdown（合成 PDF）", t_markdown_table_from_pdf),
        ("⑨ 真实语料表格还原", t_real_corpus_tables),
    ]
    for name, fn in tests:
        check(name, fn)
    print(f"\nALL PASSED ({PASSED}/{len(tests)})")
