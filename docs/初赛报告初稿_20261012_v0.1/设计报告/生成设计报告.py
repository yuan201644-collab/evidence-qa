"""从 设计报告_正文.md 生成本报告的 Word 初稿。使用 Codex 文档运行时的 Python。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FIG = ROOT / "figures"
INK = "#20374a"
BLUE = "#3976a7"
TEAL = "#2f7e86"
PURPLE = "#7b6296"
FONT_FILE = Path("C:/Windows/Fonts/msyh.ttc")


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_FILE), size)


def centered(draw: ImageDraw.ImageDraw, text: str, xy: tuple[int, int], size: int = 30, color: str = INK) -> None:
    draw.text(xy, text, font=font(size), fill=color, anchor="mm")


def box(draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int], title: str, lines: list[str], color: str = BLUE) -> None:
    x, y, w, h = bounds
    draw.rounded_rectangle((x, y, x + w, y + h), radius=12, fill="#f4f8fb", outline=color, width=2)
    centered(draw, title, (x + w // 2, y + 32), 29, color)
    for i, text in enumerate(lines):
        centered(draw, text, (x + w // 2, y + 74 + i * 33), 24)


def arrow(draw: ImageDraw.ImageDraw, points: list[tuple[int, int]], color: str = BLUE) -> None:
    draw.line(points, fill=color, width=4, joint="curve")
    x, y = points[-1]
    ax, ay = points[-2]
    if x > ax:
        tip = [(x, y), (x - 12, y - 7), (x - 12, y + 7)]
    elif x < ax:
        tip = [(x, y), (x + 12, y - 7), (x + 12, y + 7)]
    elif y > ay:
        tip = [(x, y), (x - 7, y - 12), (x + 7, y - 12)]
    else:
        tip = [(x, y), (x - 7, y + 12), (x + 7, y + 12)]
    draw.polygon(tip, fill=color)


def figures() -> None:
    FIG.mkdir(exist_ok=True)
    im = Image.new("RGB", (1200, 530), "white")
    d = ImageDraw.Draw(im)
    centered(d, "系统分层与主要职责", (600, 35), 35)
    layers = [
        ("交互层", "Vue 工作台　问答记录　SQL 与原文证据查看", BLUE),
        ("编排层", "问题理解　澄清与会话　路由　专用事实计划", TEAL),
        ("工具层", "SQL 查询　文档检索　多跳　业务核验", BLUE),
        ("数据层", "PostgreSQL　结构目录　原文　章节切片　索引", TEAL),
        ("模型层", "国产生成接口　本地 bge 向量与重排　离线文字识别", PURPLE),
    ]
    for i, (name, body, color) in enumerate(layers):
        y = 75 + i * 79
        d.rounded_rectangle((25, y, 1175, y + 66), radius=8, fill="#f3f7fa", outline="#d0dae2", width=2)
        d.rounded_rectangle((25, y, 210, y + 66), radius=8, fill=color)
        centered(d, name, (118, y + 33), 30, "white")
        centered(d, body, (691, y + 33), 27)
    centered(d, "分层表示职责归属　在线执行顺序见处理流程", (600, 501), 24, "#647584")
    im.save(FIG / "五层架构.png")

    im = Image.new("RGB", (1200, 390), "white")
    d = ImageDraw.Draw(im)
    centered(d, "在线问题处理", (600, 33), 35)
    nodes = [
        ("用户输入", ["自然语言问题", "已有会话条件"]),
        ("要素与语义", ["标准化与检查", "缺条件时澄清"]),
        ("路由与计划", ["选择工具路径", "维护事实依赖"]),
        ("工具执行", ["SQL 或 RAG", "显式跨源核验"]),
        ("回答与来源", ["结果及限定条件", "引用与实际轨迹"]),
    ]
    for i, (title, lines) in enumerate(nodes):
        x = 15 + 239 * i
        box(d, (x, 100, 214, 137), title, lines)
        if i < 4:
            arrow(d, [(x + 217, 168), (x + 236, 168)])
    arrow(d, [(360, 240), (360, 288), (120, 288), (120, 240)], "#ad7c45")
    centered(d, "补充条件后继续原问题", (249, 325), 26, "#ad7c45")
    centered(d, "单源问题走短路径　KAG 真实增强尚未接入默认链路", (800, 320), 24, "#647584")
    im.save(FIG / "在线流程.png")

    im = Image.new("RGB", (1200, 565), "white")
    d = ImageDraw.Draw(im)
    centered(d, "当前工单合同事实核验", (600, 35), 35)
    top = [
        ("对象事实", ["工单与设备编号", "日期与设备客户"]),
        ("桥接关系", ["受控映射与唯一性", "客户 状态 双有效期"]),
        ("事件事实", ["关键事件与时区", "时间顺序及耗时"]),
    ]
    bottom = [
        ("结论与证据", ["事实与来源关联", "完整或限定回答"]),
        ("口径与比较", ["指标 单位 适用范围", "确定性计算与判断"]),
        ("文档规则", ["定向检索合同原文", "范围 时限 特别约定"]),
    ]
    for i in range(3):
        x = 30 + i * 400
        box(d, (x, 98, 340, 140), top[i][0], top[i][1], PURPLE)
        box(d, (x, 328, 340, 140), bottom[i][0], bottom[i][1], PURPLE)
        if i < 2:
            arrow(d, [(x + 343, 168), (x + 397, 168)], PURPLE)
            arrow(d, [(x + 397, 398), (x + 343, 398)], PURPLE)
    arrow(d, [(1000, 242), (1000, 324)], PURPLE)
    centered(d, "前置不足：跳过依赖任务，保留已确认事实并说明阻断依据", (600, 510), 27, PURPLE)
    centered(d, "显式启用的专用场景　复用真实 SQL 和文档工具", (600, 545), 24, "#647584")
    im.save(FIG / "跨源核验.png")

    im = Image.new("RGB", (1200, 415), "white")
    d = ImageDraw.Draw(im)
    centered(d, "CS18 v0.6 严格通过结果", (600, 37), 35)
    centered(d, "判据为回答正确且证据充分　每个流程 64 题", (600, 83), 25, "#647584")
    for i, (name, n, color) in enumerate([( "新事实核验", 62, TEAL), ("匹配参考", 7, BLUE)]):
        y = 137 + i * 99
        d.text((20, y + 27), name, font=font(30), fill=INK, anchor="lm")
        d.rounded_rectangle((235, y, 1005, y + 55), radius=8, fill="#eaf0f4")
        d.rounded_rectangle((235, y, 235 + 770 * n / 64, y + 55), radius=8, fill=color)
        d.text((1030, y + 27), f"{n}/64", font=font(31), fill=color, anchor="lm")
    centered(d, "合成数据上的描述性结果　不等于项目整体准确率", (600, 355), 25, "#647584")
    centered(d, "候选顺序随机化、晚停及协议优先级限制随原报告披露", (600, 393), 23, "#647584")
    im.save(FIG / "跨源结果.png")

    im = Image.new("RGB", (1200, 340), "white")
    d = ImageDraw.Draw(im)
    centered(d, "离线运行与核验", (600, 35), 35)
    nodes = [("题集与判据",["问题参考分离","区分回归与留出"]),("冻结与预检",["代码数据配置","真实资源检查"]),("真实运行",["保留全部回答","错误耗时轨迹"]),("评分与复核",["原文与结果核对","盲审分歧裁决"]),("受控汇总",["封存与配对分析","新版本再回归"])]
    for i, (title, lines) in enumerate(nodes):
        x = 15 + i * 239
        box(d, (x, 96, 214, 140), title, lines, BLUE)
        if i < 4:
            arrow(d, [(x + 217, 166), (x + 236, 166)])
    centered(d, "标准答案仅进入评分端　不进入在线作答与检索索引", (600, 290), 27, TEAL)
    im.save(FIG / "评测流程.png")


def set_font(style, name: str, size: float, bold: bool = False) -> None:
    style.font.name = name
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = RGBColor(0, 0, 0)
    # 2026-10-18：主题字体优先级高于显式字体名。模板里的 Heading/Title 带着
    # w:asciiTheme="majorHAnsi" / w:eastAsiaTheme="majorEastAsia"，
    # 只写 w:ascii 与 w:eastAsia 时 Word/WPS 仍按主题字体渲染（等线 Light），
    # 标题在 Word 里显示成另一套字。四个主题属性必须删掉，显式字体名才生效。
    rfonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), name)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if rfonts.get(qn(attr)) is not None:
            del rfonts.attrib[qn(attr)]


def _setup_section(sec) -> None:
    """A4 + 中文报告页边距。新分区的属性不会自动继承，必须显式设置。"""
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    sec.left_margin, sec.right_margin = Cm(2.2), Cm(2.2)
    sec.top_margin, sec.bottom_margin = Cm(2.4), Cm(2.2)
    sec.header_distance, sec.footer_distance = Cm(1.5), Cm(1.75)


# OOXML CT_SectPr 的元素顺序：<w:pgNumType> 必须排在 <w:cols> 之前，
# 追加到 sectPr 末尾会被 Word 判为无效而忽略（实测正文首页仍显示「第 1 页」）。
_PGNUM_SUCCESSORS = (
    "w:cols", "w:formProt", "w:vAlign", "w:noEndnote", "w:titlePg",
    "w:textDirection", "w:bidi", "w:rtlGutter", "w:docGrid",
    "w:printerSettings", "w:sectPrChange",
)


def _footer(sec, text: str | None, start: int | None = None) -> None:
    """写页脚。text=None 表示该分区不显示页脚；text="PAGE" 用页码域。

    start 给定时，续接页码：把 <w:pgNumType w:start="N"/> 写进分区属性，
    使 PAGE 域从 N 起算——正文首页接着预览两页编号，不走回头路。
    """
    sec.footer.is_linked_to_previous = False
    fp = sec.footer.paragraphs[0]
    for r in list(fp.runs):
        r._element.getparent().remove(r._element)
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if text is None:
        return
    # add_section 克隆上一分区，pgNumType 会被一起带过来，导致每个分区都从 1 起算。
    # 只有显式给了 start 的分区才保留旧起始，其余先摘掉再决定是否重写。
    for old in sec._sectPr.xpath("./w:pgNumType"):
        sec._sectPr.remove(old)
    if start is not None:
        el = OxmlElement("w:pgNumType")
        el.set(qn("w:start"), str(start))
        sec._sectPr.insert_element_before(el, *_PGNUM_SUCCESSORS)
    if text == "PAGE":
        for piece in ["第 ", None, " 页"]:
            if piece is None:
                fld = OxmlElement("w:fldSimple")
                fld.set(qn("w:instr"), "PAGE")
                fp._p.append(fld)
            else:
                fp.add_run(piece).font.size = Pt(9)
        return
    run = fp.add_run(text)
    run.font.name = "宋体"
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0, 0, 0)


def _new_page_section(doc: Document, footer_text: str | None, start: int | None = None):
    """以分节符（新页）开启下一段，并立即给定该段的页脚。"""
    from docx.enum.section import WD_SECTION

    sec = doc.add_section(WD_SECTION.NEW_PAGE)
    _setup_section(sec)
    _footer(sec, footer_text, start)
    return sec


def make_doc(kind: str) -> Document:
    doc = Document()
    sec = doc.sections[0]
    _setup_section(sec)
    # 2026-10-15 版式规范化：A4 纸（中文报告标准），页边距上下 2.54cm、左右 2.5cm，
    # 页眉 1.5cm、页脚 1.75cm。原为 Letter 8.5x11in + 0.78/0.72/0.72/0.7in。
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    sec.left_margin, sec.right_margin = Cm(2.2), Cm(2.2)
    sec.top_margin, sec.bottom_margin = Cm(2.4), Cm(2.2)
    sec.header_distance, sec.footer_distance = Cm(1.5), Cm(1.75)
    # 正文小四号宋体（12pt）、行距固定值 15.5 磅、段前后 0 磅，
    # 段落靠首行缩进 2 字符区分——多校学位/技术报告规范的一致写法。
    # 原为 11.5pt（非标准字号）+ 1.15 倍行距 + 段后 7pt（西式段间距）。
    # 2026-10-18 压页：行距由 17 磅收到 15.5 磅（约等于 Word 单倍行距 15.6 磅），
    # 段前距由 8 磅收到 4 磅——设计报告硬上限 20 页，而 10-17 增图与 6.4 后已到 23 页。
    set_font(doc.styles["Normal"], "宋体", 12)
    pf = doc.styles["Normal"].paragraph_format
    pf.line_spacing = Pt(14)
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.widow_control = True
    # 标题黑体：主标题二号 22pt、一级三号 16pt、二级四号 14pt、三级小四 12pt。
    # 行距单独设为单倍：正文是固定值 20 磅，标题若继承会被裁切（字号大于行距）。
    for name, sz in [("Title", 22), ("Subtitle", 16), ("Heading 1", 16), ("Heading 2", 14), ("Heading 3", 12)]:
        set_font(doc.styles[name], "黑体", sz, True)
        fmt = doc.styles[name].paragraph_format
        fmt.space_before = Pt(12 if name != "Title" else 0)
        fmt.space_after = Pt(6)
        fmt.line_spacing = 1.0
        fmt.keep_with_next = True
    set_font(doc.styles["Caption"], "宋体", 9)
    doc.styles["Caption"].paragraph_format.line_spacing = 1.0
    doc.styles["Caption"].paragraph_format.space_after = Pt(4)
    hp = sec.header.paragraphs[0]
    hp.text = "知证 VeriMind  多模态精准问数问答智能体    " + kind
    for r in hp.runs:
        r.font.name = "宋体"
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(0,0,0)
    # 封面不显示页码：中文报告惯例，且交付要求前两页为亮点预览页，
    # 封面若带「第 1 页」会与预览页的「正文第 1 页」冲突。
    _footer(sec, None)
    # 打开即更新域：目录与页码由 PAGE/TOC 域生成，若不带缓存结果，
    # 接收方在 Word / WPS 里看到的会是空目录。写 updateFields 让打开时自动刷新。
    _settings = doc.settings.element
    if not _settings.xpath("./w:updateFields"):
        _uf = OxmlElement("w:updateFields")
        _uf.set(qn("w:val"), "true")
        _settings.append(_uf)
    doc.core_properties.title = "知证 VeriMind 多模态数据驱动的可解释精准问数问答智能体 " + kind
    doc.core_properties.subject = "赛题八 校内初赛报告"
    doc.core_properties.author = "项目团队"
    doc.core_properties.version = "v0.3"
    return doc


def paragraph(doc: Document, text: str, indent: bool = True) -> None:
    p = doc.add_paragraph()
    # 行内粗体：把 **...** 交替渲染为粗体。正文只使用这一种行内标记，
    # 故不做通用 Markdown 解析；不处理时星号会原样排进 Word。
    for i, seg in enumerate(text.split("**")):
        if not seg:
            continue
        run = p.add_run(seg)
        if i % 2 == 1:
            run.bold = True
    if p._p.getprevious() is not None and p._p.getprevious().tag == qn("w:tbl"):
        p.paragraph_format.space_before = Pt(8)
    if indent:
        p.paragraph_format.first_line_indent = Pt(24)  # 2 字符 @ 小四 12pt


NAVY = "1F3C88"        # 表头深蓝，也是表框上下粗线的颜色
NAVY_TEXT = "1F3C88"
HEAD_FILL = "1F3C88"
FIRST_FILL = "E8EFFA"    # 首列浅蓝
ZEBRA_FILL = "F4F6FA"    # 隔行浅灰
RULE_LINE = "C9D3E4"     # 横向细线


def _tbl_shade(cell, fill: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    for old in tcpr.findall(qn("w:shd")):
        tcpr.remove(old)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcpr.append(shd)


def _tbl_cell_border(cell, edge: str, size: int, color: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    borders = tcpr.find(qn("w:tcBorders"))
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        tcpr.append(borders)
    el = borders.find(qn("w:" + edge))
    if el is None:
        el = OxmlElement("w:" + edge)
        borders.append(el)
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(size))
    el.set(qn("w:space"), "0")
    el.set(qn("w:color"), color)


_NUMERIC = re.compile(r"^[\d\s,.:;%+\-–—/()（）\u4e2a\u4ef6\u6761\u9879]*\d[\d\s,.:;%+\-–—/()（）\u4e2a\u4ef6\u6761\u9879]*$")


def table(doc: Document, rows: list[list[str]]) -> None:
    cols = len(rows[0])
    t = doc.add_table(rows=0, cols=cols)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    widths = {
        2:[.35,.65], 3:[.24,.36,.40], 4:[.22,.32,.27,.19],
        5:[.29,.16,.16,.16,.23], 6:[.28,.11,.11,.11,.14,.25],
        7:[.27,.15,.12,.12,.12,.12,.10]
    }.get(cols, [1/cols]*cols)
    for i, c in enumerate(t.columns):
        c.width = Cm(16.0 * widths[i])
    # 2026-10-18：参照参赛版式改为「深蓝表头 + 首列浅蓝 + 隔行浅灰 + 去竖线」。
    # 只留横向细线，表头、表尾各一条深蓝粗线；竖线全部置 none，避免 16cm 宽表变密栅格。
    props = t._tbl.tblPr
    for old in props.findall(qn("w:tblBorders")):
        props.remove(old)
    borders = OxmlElement("w:tblBorders")
    for name, size, color in [
        ("top", 12, NAVY), ("bottom", 12, NAVY),
        ("left", 0, RULE_LINE), ("right", 0, RULE_LINE),
        ("insideH", 4, RULE_LINE), ("insideV", 0, RULE_LINE),
    ]:
        b = OxmlElement("w:" + name)
        if size == 0:
            b.set(qn("w:val"), "none")
            b.set(qn("w:sz"), "0")
            b.set(qn("w:space"), "0")
        else:
            b.set(qn("w:val"), "single")
            b.set(qn("w:sz"), str(size))
            b.set(qn("w:space"), "0")
            b.set(qn("w:color"), color)
        borders.append(b)
    props.append(borders)
    for index, row in enumerate(rows):
        cells = t.add_row().cells
        trpr = t.rows[-1]._tr.get_or_add_trPr()
        trpr.append(OxmlElement("w:cantSplit"))
        if index == 0:
            trpr.append(OxmlElement("w:tblHeader"))
        for j, c in enumerate(cells):
            c.width = Cm(16.0 * widths[j])
            c.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            c.text = row[j] if j < len(row) else ""
            tcpr = c._tc.get_or_add_tcPr()
            margin = OxmlElement("w:tcMar")
            for side, value in [("top", 50), ("bottom", 50), ("left", 100), ("right", 100)]:
                el = OxmlElement("w:" + side)
                el.set(qn("w:w"), str(value))
                el.set(qn("w:type"), "dxa")
                margin.append(el)
            tcpr.append(margin)
            if index == 0:
                _tbl_shade(c, HEAD_FILL)
                _tbl_cell_border(c, "bottom", 12, NAVY)
            elif j == 0:
                _tbl_shade(c, FIRST_FILL)
            else:
                _tbl_shade(c, ZEBRA_FILL if index % 2 == 0 else "FFFFFF")
            for p in c.paragraphs:
                p.paragraph_format.line_spacing = Pt(11)
                p.paragraph_format.space_after = Pt(1)
                p.paragraph_format.space_before = Pt(1)
                p.paragraph_format.keep_with_next = index < len(rows) - 1
                text = p.text.strip()
                numeric = bool(text) and bool(_NUMERIC.match(text))
                if index == 0 or (j > 0 and (numeric or len(text) < 12)):
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in p.runs:
                    run.font.name = "宋体"
                    run.font.size = Pt(8.5)
                    run.font.bold = index == 0 or j == 0 or numeric
                    if index == 0:
                        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                    elif j == 0:
                        run.font.color.rgb = RGBColor(0x1F, 0x3C, 0x88)
                    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")

def placeholder(width: int = 1200, height: int = 430) -> Path:
    """为尚未绘制的插图生成中性占位图。

    占位图不写回被引用的路径，避免后续被误当作正式插图。
    """
    dest = FIG / "_未绘制的占位图.png"
    if not dest.is_file():
        FIG.mkdir(exist_ok=True)
        im = Image.new("RGB", (width, height), "#f2f4f6")
        d = ImageDraw.Draw(im)
        d.rectangle([1, 1, width - 2, height - 2], outline="#c3ccd4", width=3)
        d.text((width // 2 - 62, height // 2 - 20), "插图待绘制", font=font(30), fill="#7c8a96")
        im.save(dest)
    return dest


def picture(doc: Document, filename: Path, caption: str = "", width_cm: float = 16.0) -> None:
    if not Path(filename).is_file():
        print(f"[警告] 缺少插图 {filename}，本次以占位图代替；正式插图登记见 图表编号清单.md")
        filename = placeholder()
    p=doc.add_paragraph()
    p.alignment=WD_ALIGN_PARAGRAPH.CENTER
    # 正文采用固定 20 磅行距；图片段若继承该行距，Word 会把行内图裁成一条窄带。
    # 图片段改为单倍自动行距，让段落按图高展开。
    p.paragraph_format.line_spacing=1.0
    p.paragraph_format.keep_with_next=True
    p.add_run().add_picture(str(filename),width=Cm(width_cm))
    # 为图片补充可访问说明。
    for dp in p._p.iter(qn("wp:docPr")):
        dp.set("descr",caption or filename.stem)
    if caption:
        cp=doc.add_paragraph(caption,style="Caption")
        cp.alignment=WD_ALIGN_PARAGRAPH.CENTER


def front(doc: Document, kind: str) -> None:
    p=doc.add_paragraph("第三届中国电子杯高校 ICT 产教融合创新大赛")
    p.alignment=WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before=Pt(52)
    p.paragraph_format.space_after=Pt(15)
    p=doc.add_paragraph("赛题八",style="Subtitle");p.alignment=WD_ALIGN_PARAGRAPH.CENTER
    p=doc.add_paragraph("多模态数据驱动的可解释\n精准问数问答智能体",style="Title")
    p.alignment=WD_ALIGN_PARAGRAPH.CENTER;p.paragraph_format.space_before=Pt(39);p.paragraph_format.space_after=Pt(20)
    p=doc.add_paragraph(kind,style="Subtitle");p.alignment=WD_ALIGN_PARAGRAPH.CENTER
    p=doc.add_paragraph("知证 VeriMind",style="Subtitle");p.alignment=WD_ALIGN_PARAGRAPH.CENTER;p.paragraph_format.space_before=Pt(15)
    p=doc.add_paragraph("事实—证据核验架构（FEVA）",style="Subtitle");p.alignment=WD_ALIGN_PARAGRAPH.CENTER;p.paragraph_format.space_before=Pt(5)
    for text in ["参赛单位  南京邮电大学",
                 "指导教师  徐康",
                 "队员      袁湘熙、胡雨航、祖启源"]:
        p=doc.add_paragraph(text);p.alignment=WD_ALIGN_PARAGRAPH.CENTER;p.paragraph_format.space_before=Pt(8)
    p=doc.add_paragraph("v0.3　2026 年 10 月 18 日");p.alignment=WD_ALIGN_PARAGRAPH.CENTER;p.paragraph_format.space_before=Pt(33)
    _new_page_section(doc, None)
    p=doc.add_paragraph("目录",style="Title");p.alignment=WD_ALIGN_PARAGRAPH.CENTER
    for r in p.runs:r.font.size=Pt(16)  # 「目录」二字三号黑体居中，取正文一级标题字号
    p=doc.add_paragraph()
    fld=OxmlElement("w:fldSimple");fld.set(qn("w:instr"),'TOC \\o "1-2" \\h \\z \\u')
    p._p.append(fld)
    # 目录列到二级：中级任务（3.3）、政采测试（6.4）、KAG（4.3）这些评分相关小节
    # 必须能在目录里被找到，只列一级等于把它们藏起来。
    # 2026-10-18 夜（二次）：原把「项目核心亮点预览」与「核心能力与验证概览」拆成两个
    # 独立分组，概览页只有一张表加一段，整页大半空白。合为一节后内容自然续排，第二页
    # 由亮点描述与能力表填实；封面与目录保持原样，封面仍独占第一页。
    # 2026-10-18 夜（三次）：亮点页语气收紧为正面陈述。限制与未收口项留在第 6、7 章，
    # 此处只讲做成了什么、凭什么能核对——同一个数字在两处的语气不同，口径仍以正文章节为准。
    _new_page_section(doc, "PAGE", start=1)
    doc.add_heading("项目核心亮点预览",level=1)
    paragraph(doc,"知证 VeriMind 把数据库问数与文档问答收进同一个自然语言入口。用户不必先判断该查库还是查资料——系统先解析问题里哪些对象要落到业务记录、哪些要在资料里找依据，再决定走哪条路径；复合问题中跨路径的部分由事实—证据核验架构（FEVA）承接，把事实依赖、跨源关系与来源证据显式登记下来。路径判断本身也留有记录，事后可以回看它为什么这样走。")
    paragraph(doc,"统一工具与来源结构。SQL 工具与文档工具以同一套结果结构交接：前端能直接看到本次实际执行的查询语句、命中的表与字段、检索到的文档片段与完整调用过程，答案里的每条事实都能回指到具体来源。可解释性由此建立在确定性记录之上，而不是模型的自我描述——同样的问法重跑，链路依然可核。")
    paragraph(doc,"多模态数据融合处理链路。一条问题可能同时涉及结构化记录、公告或合同正文，以及从扫描件识别出的表格与条款。三类输入在进入推理前统一成同一种证据形态：数据库侧是可回指的查询结果行，文档侧是带页码与章节路径的切片，识别侧是带像素坐标换算的原文框；三条来源共用一套引用编号与一份证据账本。因此同一份文件里的表格数字与库里的一条记录，可以放进同一张核验表逐项比对——跨源核验做的正是这件事。")
    paragraph(doc,"FEVA 由五件可分别交付的组件构成。事实流（FactFlow）登记复合问题内部的事实依赖与求解顺序；跨源证桥（SourceBridge）判断两侧是否指向同一个业务对象；口径对齐（MetricAlign）确保指标定义与统计范围一致；证据账本（EvidenceLedger）保存每条事实的状态、证据编号与来源引用；有界求证（BoundedVerify）给出收敛条件，让求证有终点。五件拆开，每一件都能单独验证。")
    paragraph(doc,"可解释性落在三处看得见的地方：调用链逐步骤呈现工具、入参与出参；证据面板给出实际 SQL、命中字段与原文页码章节；失败路径同样清楚——工具报错不会被包装成答案，界面给的是明确的未完成提示与重试入口。用户与评委看到的，都是系统真正做过的事。")
    paragraph(doc,"连续的中级能力链。任务 1、3、4、5、7 分别是问题理解、澄清追问、表字段定位、多表关联与文档目录组织。它们不是并列的功能点，而是同一条链上的连续环节：前一环的输出正是后一环的输入，任一环失效都会在端到端结果上显露，因此效果只能由同版专项测试逐项核查。")
    picture(doc,FIG/"系统总体架构_20261017.jpg","系统总体架构",width_cm=14.5)
    table(doc,[["能力","当前实现范围","验证状态与出处"],["数据库问数","标准化 定位 关联 校验 只读执行","分项结果见表 2、效率见表 7"],["文档问答","解析 切片 混合召回 重排 生成 引用","分项结果见表 2、忠实度见技术实现说明书第 7 章"],["多轮与可解释性","澄清恢复 会话状态 工具轨迹 原文查看","单端口实例五场景见表 4"],["跨源机制","显式工单—合同事实依赖与核验","受控对照见表 6"],["政采业务库","11 张表合计 15,418 行，五套 77 题","覆盖与留档见表 8"],["KAG 增强","可选检索已接入 默认关闭","扩展路径，烟测见表 9"]])
    paragraph(doc,"跨源核验是这套架构里唯一能与「单源直接回答」正面比较的部分。在 64 道合成跨源题上，新流程严格通过 62 道，匹配参考范围的旧流程 7 道，配对差 85.94 个百分点；更关键的是过度拒答由 41 道降为 0 道——把「不确定」错当「不适用」的退化，在这套机制下没有出现。")
    paragraph(doc,"真实业务数据上改用可逐条复核的政务采购公告库。字段抽取按「表头行数等于数据行数等于列数」校验，规则原文由脚本按锚点截取而不是手抄，题面按「先跑标准 SQL 拿真答案、再据此写题面」的方式命题——每一步都留痕，任何一个分数都能回到原始公告与原始 SQL。结构化问数、文档单源问答、跨源融合、鲁棒性与多轮五套共 77 题、20 轮多轮全部跑通并留档，逐套记录随交付包提供。")
    paragraph(doc,"规模效率在万行级销售数据上做了三档对照，每档 10 个查询：严格通过 27 题、语义通过 30 题，三档中位耗时集中在 3.3 至 3.6 秒。数据行数增长两个数量级而耗时基本持平，说明瓶颈在意图理解与生成，不在数据规模。")
    paragraph(doc,"上述能力都有同版记录可核对：分项结果见表 2、中级任务见表 3，跨源对照见表 6，效率见表 7，政务采购库覆盖见表 8，界面五个场景见表 4、最终验收见表 5。方法与适用范围在后续各章按项展开。")
    _new_page_section(doc, "PAGE")


def markdown(doc: Document, source: Path) -> None:
    lines=source.read_text(encoding="utf-8").splitlines()
    i=0;first=True;code=False;in_references=False;first_h1=True
    while i<len(lines):
        line=lines[i].strip()
        if not line:
            i+=1;continue
        if line.startswith("```"):
            code=not code;i+=1;continue
        if code:
            p=doc.add_paragraph(line)
            p.paragraph_format.space_after=Pt(1)
            p.paragraph_format.line_spacing=Pt(12.5)  # 与表内一致：小五 9pt + 固定行距
            for r in p.runs:r.font.name="Consolas";r.font.size=Pt(9)
            i+=1;continue
        if line.startswith("# "):
            # 2026-10-15 页数预算：一级标题不再强制另起一页。
            # 原实现对每个 H1 设 page_break_before，7 处章分页各浪费约半页，
            # 实测 24 页 -> 21 页（见 页数预算核算_20261015.md）。
            # 章节靠 Heading 1 自身的字号与段前距区分，不再另调间距：
            # 实测段前距 15pt 时 21 页，18pt 及以上即涨到 22 页。
            first=False
            in_references = line[2:].strip() == "参考资料"
            if in_references:
                # 2026-10-18：15 张表插入后溢出到 21 页，溢出的正是参考资料后半段。
                # 参考资料属条目式短行，改双栏后高度约减半，可收回 20 页。
                from docx.enum.section import WD_SECTION
                ref_section = doc.add_section(WD_SECTION.CONTINUOUS)
                cols = ref_section._sectPr.xpath("./w:cols")
                if cols:
                    cols[0].set(qn("w:num"), "2")
                    cols[0].set(qn("w:space"), "284")
                # add_section 克隆上一分区的 sectPr，会带上正文的 pgNumType start=3。
                # 参考资料是正文的续页，必须继续编号，故摘掉继承项。
                for inherited in ref_section._sectPr.xpath("./w:pgNumType"):
                    ref_section._sectPr.remove(inherited)
            h=doc.add_heading(line[2:],level=1)
            # 中文报告规范：每章另起一页。首章由正文分节符保证，不再重复分页。
            if not first_h1:
                h.paragraph_format.page_break_before=True
            first_h1=False
            if in_references:
                for run in h.runs:run.font.size=Pt(14)
            i+=1;continue
        if line.startswith("## "):
            h=doc.add_heading(line[3:],level=2)
            if in_references:
                h.paragraph_format.space_before=Pt(4)
                h.paragraph_format.space_after=Pt(2)
                for run in h.runs:run.font.size=Pt(10.5)
            i+=1;continue
        if line.startswith("| "):
            rows=[]
            while i<len(lines) and lines[i].strip().startswith("|"):
                cells=[c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?",c or " ") for c in cells):rows.append(cells)
                i+=1
            table(doc,rows);continue
        m=re.match(r"!\[(.*?)\]\((.*?)\)",line)
        if m:
            caption=""
            j=i+1
            while j<len(lines) and not lines[j].strip():j+=1
            if j<len(lines) and lines[j].startswith("图 "):
                caption=lines[j];i=j
            image_path=ROOT/m.group(2)
            compact_figures={
                "系统总体架构_20261017.jpg",
                "在线问答处理流程_KAG默认关闭_v0.1.png",
                "FEVA核验运行方式_20261017.jpg",
            }
            image_width_cm=13.0 if image_path.name in compact_figures else 15.0
            picture(doc,image_path,caption or m.group(1),width_cm=image_width_cm);i+=1;continue
        if re.match(r"^表 \d+ ",line):
            # 表题与图注用同一种 Caption 样式并居中。
            # 不特判时，表题会走下面的 paragraph() 被排成带首行缩进的正文段落，
            # 与图注的大小、对齐都不一致（2026-10-15 体检发现）。
            cap=doc.add_paragraph(line,style="Caption")
            cap.alignment=WD_ALIGN_PARAGRAPH.CENTER
            i+=1;continue
        paragraph(doc,line,not line.startswith("["))
        if in_references:
            ref_paragraph=doc.paragraphs[-1]
            ref_paragraph.paragraph_format.line_spacing=Pt(11)
            ref_paragraph.paragraph_format.space_after=Pt(1)
            for run in ref_paragraph.runs:run.font.size=Pt(9)
        i+=1


def main() -> None:
    figures()
    manifest={"date":"2026-10-18","version":"v0.3","status":"candidate_with_registered_measurements","documents":[]}
    for kind in ["设计报告"]:
        doc=make_doc(kind)
        front(doc,kind)
        source=ROOT/(kind+"_正文.md")
        markdown(doc,source)
        dest=ROOT/(kind+"_v0.3.docx")
        doc.save(dest)
        manifest["documents"].append({"kind":kind,"source":source.name,"source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),"docx":dest.name})
        print(str(dest))
    (ROOT/"生成清单.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    main()
