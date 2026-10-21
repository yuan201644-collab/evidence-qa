"""从 技术实现说明书_正文.md 生成本报告的 Word 初稿。使用 Codex 文档运行时的 Python。"""
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


def make_doc(kind: str) -> Document:
    doc = Document()
    sec = doc.sections[0]
    # 2026-10-15 版式规范化：A4 纸（中文报告标准），页边距上下 2.54cm、左右 2.5cm，
    # 页眉 1.5cm、页脚 1.75cm。原为 Letter 8.5x11in + 0.78/0.72/0.72/0.7in。
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    # 2026-10-18 压页：与设计报告同一组页边距（左右 2.2cm、上 2.4cm、下 2.2cm）。
    # 原 2.5/2.54 组合在 19 张表、多张图的正文下多出一页，跨过 30 页上限。
    sec.left_margin, sec.right_margin = Cm(2.2), Cm(2.2)
    sec.top_margin, sec.bottom_margin = Cm(2.4), Cm(2.2)
    sec.header_distance, sec.footer_distance = Cm(1.5), Cm(1.75)
    # 正文小四号宋体（12pt）、行距固定值 20 磅、段前后 0 磅，
    # 段落靠首行缩进 2 字符区分——多校学位/技术报告规范的一致写法。
    # 原为 11.5pt（非标准字号）+ 1.15 倍行距 + 段后 7pt（西式段间距）。
    set_font(doc.styles["Normal"], "宋体", 12)
    pf = doc.styles["Normal"].paragraph_format
    # 2026-10-18 二轮压页：表 9／表 10／表 16 等登记表填实后由 30 页涨到 33 页，超出 30 页上限。
    # 正文行距由固定 20 磅改为 18 磅（小四 12pt 仍是 1.5 倍行距，视觉上仍是正常中文版式），
    # 表内上下留白 60→45 twips。两处都是排版参数，不改任何内容与数字。
    pf.line_spacing = Pt(17.5)
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
    doc.styles["Caption"].paragraph_format.space_after = Pt(6)
    hp = sec.header.paragraphs[0]
    hp.text = "知证 VeriMind  多模态精准问数问答智能体    " + kind
    for r in hp.runs:
        r.font.name = "宋体"
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(0,0,0)
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for text in ["第 ", None, " 页"]:
        if text is not None:
            fp.add_run(text).font.size = Pt(9)
        else:
            field = OxmlElement("w:fldSimple")
            field.set(qn("w:instr"), "PAGE")
            fp._p.append(field)
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
    # 版心宽度 = A4 21cm − 左右各 2.5cm = 16.0cm。原按 Letter 版心 7.0in 写死，
    # 换 A4 后若不改，表格会比版心宽约 0.7in 而戳进两侧页边距。
    for i,c in enumerate(t.columns):
        c.width = Cm(16.0 * widths[i])
    props=t._tbl.tblPr
    borders=OxmlElement("w:tblBorders")
    for name in ["top","left","bottom","right","insideH","insideV"]:
        b=OxmlElement("w:"+name)
        b.set(qn("w:val"),"single"); b.set(qn("w:sz"),"4"); b.set(qn("w:color"),"D9D9D9")
        borders.append(b)
    props.append(borders)
    for index,row in enumerate(rows):
        cells=t.add_row().cells
        trpr=t.rows[-1]._tr.get_or_add_trPr()
        cant=OxmlElement("w:cantSplit"); trpr.append(cant)
        if index==0:
            repeated=OxmlElement("w:tblHeader"); trpr.append(repeated)
        for j,c in enumerate(cells):
            c.width=Cm(16.0*widths[j])
            c.vertical_alignment=WD_CELL_VERTICAL_ALIGNMENT.CENTER
            c.text=row[j] if j<len(row) else ""
            tcpr=c._tc.get_or_add_tcPr()
            margin=OxmlElement("w:tcMar")
            for side,value in [("top",30),("bottom",30),("left",110),("right",110)]:
                el=OxmlElement("w:"+side);el.set(qn("w:w"),str(value));el.set(qn("w:type"),"dxa");margin.append(el)
            tcpr.append(margin)
            sh=OxmlElement("w:shd");sh.set(qn("w:fill"),"E8F0F6" if index==0 else ("F8FAFC" if index%2==0 else "FFFFFF"));tcpr.append(sh)
            for p in c.paragraphs:
                # 2026-10-18 压页：表内行距 12.5->11.5 磅，配合上下留白 95->60 twips。
                p.paragraph_format.line_spacing=Pt(10.5)
                p.paragraph_format.space_after=Pt(1)
                p.paragraph_format.space_before=Pt(1)
                p.paragraph_format.keep_with_next = index < len(rows) - 1
                if index==0 or (j>0 and len(p.text)<12):
                    p.alignment=WD_ALIGN_PARAGRAPH.CENTER
                for run in p.runs:
                    run.font.name="宋体";run.font.size=Pt(9);run.font.bold=index==0
                    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"),"宋体")


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


def picture(doc: Document, filename: Path, caption: str = "") -> None:
    if not Path(filename).is_file():
        print(f"[警告] 缺少插图 {filename}，本次以占位图代替；正式插图登记见 图表编号清单.md")
        filename = placeholder()
    p=doc.add_paragraph()
    p.alignment=WD_ALIGN_PARAGRAPH.CENTER
    # 正文使用固定行距；图片段若继承固定值，Word 会把行内图裁成窄带。
    # 改为自动行距，使段落按图片高度展开（与设计报告生成器一致）。
    p.paragraph_format.line_spacing=1.0
    p.paragraph_format.keep_with_next=True
    p.add_run().add_picture(str(filename),width=Cm(16.0))
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
    doc.add_page_break()
    p=doc.add_paragraph("目录",style="Title");p.alignment=WD_ALIGN_PARAGRAPH.CENTER
    for r in p.runs:r.font.size=Pt(16)  # 「目录」二字三号黑体居中，取正文一级标题字号
    p=doc.add_paragraph()
    fld=OxmlElement("w:fldSimple");fld.set(qn("w:instr"),'TOC \\o "1-2" \\h \\z \\u')
    p._p.append(fld)
    doc.add_page_break()
    # 2026-10-15 决定：技术实现说明书不设「项目核心亮点预览」两页。
    # 依据：题面原文只对设计报告明写「封面或目录后的第 1-2 页必须为项目核心亮点预览页」，
    # 对技术实现说明书仅说「格式要求同上」，按不占用页数解释。
    # 原两页预览（项目核心亮点预览 / 核心能力与验证概览）已从此处移除，说明书 27 -> 25 页。
    # 设计报告仍保留这两页，见 设计报告/生成设计报告.py 的同名函数。


def markdown(doc: Document, source: Path) -> None:
    lines=source.read_text(encoding="utf-8").splitlines()
    i=0;first=True;code=False
    while i<len(lines):
        line=lines[i].strip()
        if not line:
            i+=1;continue
        if line.startswith("```"):
            code=not code;i+=1;continue
        if code:
            p=doc.add_paragraph(line)
            p.paragraph_format.space_after=Pt(2)
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
            h=doc.add_heading(line[2:],level=1)
            i+=1;continue
        if line.startswith("## "):
            doc.add_heading(line[3:],level=2);i+=1;continue
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
            picture(doc,ROOT/m.group(2),caption or m.group(1));i+=1;continue
        if re.match(r"^表 \d+ ",line):
            # 表题与图注用同一种 Caption 样式并居中。
            # 不特判时，表题会走下面的 paragraph() 被排成带首行缩进的正文段落，
            # 与图注的大小、对齐都不一致（2026-10-15 体检发现）。
            cap=doc.add_paragraph(line,style="Caption")
            cap.alignment=WD_ALIGN_PARAGRAPH.CENTER
            i+=1;continue
        paragraph(doc,line,not line.startswith("["))
        i+=1


def main() -> None:
    figures()
    manifest={"date":"2026-10-18","version":"v0.3","status":"draft_with_unfilled_final_measurements","documents":[]}
    for kind in ["技术实现说明书"]:
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
