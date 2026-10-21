"""loader.py - 语料文本抽取（第一周）

作用：把一份语料拆成很多「文本块」。
每个块记录四样东西：它在第几页、它的坐标框、它的文字、它的字号。
字号是后面 toc 判断「哪几行是标题」的关键依据。

支持四种格式：
    .pdf            版面抽取（PyMuPDF），带真实 bbox 与字号，可做几何高亮
    .docx           也走 PyMuPDF。实测 KB-005.docx 的正文（word/document.xml）
                    是 516 字，PyMuPDF 抽出 543 字，与源文一致；还能读到内嵌
                    目录书签。**因此不引入 python-docx** —— 抽取质量够用，
                    不为一份语料加一个依赖（项目当前网络环境下装包成本不低）。
    .md / .txt      纯文本按行读。**不能用 PyMuPDF** —— 实测 KB-010.md 走
                    PyMuPDF 只抽出 13 字（正文基本读不出来），纯文本读是 671 字。
    .pdf 扫描页     没有文本层，走 ocr.py 的离线 OCR（逐页判定，见下方"页级分派"）。
                    实测 KB-007 / KB-008 文本层 0 字符，不 OCR 就是 0 切片、
                    检索永远命中不到。

非 PDF 没有版面信息：page 一律 1，page_width/height 为 None。
bbox 借来放**行序号**：y0 = y1 = 该行在文件里的行号（从 1 起算），x0 = x1 = 0。
    为什么借 bbox 而不新增字段：chunker 判断「这段文字属于哪一章」靠的就是
    bbox[1] 的先后（见 _is_section_before_block）。纯文本若把 bbox 一律填 0，
    每个章节都会被判成「在第一个块之前」，一次全压进栈，栈顶只剩最后一章。
    实测 KB-010.md 的全部切片章节路径都变成 ['5 版本变更']，引用卡片指错章节。
    填行号后同页内按行号比大小，章节归属即正确。
    这个框仍是零面积的，**不是版面坐标，不能用来高亮**，chunker 会把它过滤掉。
后果是引用卡片给不出几何高亮（契约允许这些字段为空），只能定位到文档级与章节级。
"""

import os

import pymupdf  # 新版官方写法。旧写法 import fitz 已被官方标记弃用

from app.rag.ingest import normalize, ocr
from app.rag.schemas import Block

#: 进索引的语料后缀。加新格式时改这里 + extract_blocks 里的分派。
SUPPORTED_EXTS = (".pdf", ".docx", ".md", ".txt")

#: 走纯文本读的后缀（其余交给 PyMuPDF）
_PLAIN_TEXT_EXTS = (".md", ".txt")

#: 纯文本来源的统一字号。纯文本没有字号信息，给同一个值等于让 toc 的
#: 「字号容错」在这类文档上自然失效（全同字号认不出标题），避免误判。
_PLAIN_TEXT_SIZE = 12.0


def is_pdf(path):
    """是不是 PDF。toc 用它决定走不走「字号容错」那条路（纯文本没有字号）。"""
    return str(path).lower().endswith(".pdf")


def _needs_plain_text(path):
    return str(path).lower().endswith(_PLAIN_TEXT_EXTS)


def project_root():
    """从当前脚本位置反推项目根目录。

    loader.py 在 app/rag/ingest/ 下，往上走四级就是项目根：
        app/rag/ingest/loader.py -> app/rag/ingest -> app/rag -> app -> 项目根
    默认语料路径也基于它拼接（具体文件名由 _default_pdf_paths 发现，
    不再写死单份 1.pdf），不受你从哪个目录运行脚本的影响。
    """
    here = os.path.abspath(__file__)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))
    return root


def _scan_corpus_dir(directory):
    """扫一个目录（不递归），返回 (语料路径列表, 被跳过的文件名列表)。

    ignored 只装「看着像语料、后缀却不在 SUPPORTED_EXTS 里」的文件（隐藏文件与
    .gitkeep 不算）。列出来是为了让「文件放进去了却没进检索池」当场可见，
    不必靠人肉发现。
    """
    paths = []
    ignored = []
    if not os.path.isdir(directory):
        return paths, ignored
    for name in sorted(os.listdir(directory)):
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        if name.lower().endswith(SUPPORTED_EXTS):
            paths.append(full)
        elif not name.startswith(".") and name != ".gitkeep":
            ignored.append(name)
    return paths, ignored


def discover_corpus_files():
    """发现语料文件，返回 (路径列表, 被跳过的文件名列表)。

    默认去 项目根/data/corpus/raw/ 下找所有支持格式（见 SUPPORTED_EXTS），
    文件名形如 KB-xxx.pdf/.docx/.md/.txt；该目录不存在或一份都没扫到时，
    兜底扫 项目根/data/corpus/ 直接目录（不含子目录）。

    三点约定：
    1. **完全动态**：不写死文件名、不写死数量。以后往目录里丢新语料、删掉旧的、
       换一整批，都自动反映到索引里（配合 corpus.py 的变化检测与清孤儿）。
    2. **被跳过的文件要列出来**：后缀不在支持范围的文件会被整份跳过，不列出来
       就是悬案（列出来才看得到）。
    3. 非 PDF（docx/md/txt）没有版面信息，引用卡片给不出几何高亮，
       定位到文档级与章节级为止。
    """
    root = project_root()
    raw_dir = os.path.join(root, "data", "corpus", "raw")
    paths, ignored = _scan_corpus_dir(raw_dir)
    if paths:
        return paths, ignored
    return _scan_corpus_dir(os.path.join(root, "data", "corpus"))


def _default_pdf_paths():
    """只返回路径列表（保留旧调用点），细节见 discover_corpus_files。"""
    return discover_corpus_files()[0]


def extract_blocks(path) -> list[Block]:
    """抽取一份语料的所有文本块，按扩展名分派（理由见模块 docstring）。

    .md / .txt  走纯文本按行切块（page=1、bbox 是行号伪坐标、无字号）
    .pdf/.docx  走 PyMuPDF 的版面抽取（带真实 bbox、字号、页尺寸）
                PDF 里没有文本层的页交给 ocr.py（逐页判定，见 _blocks_via_pymupdf）
    """
    if _needs_plain_text(path):
        return _blocks_from_plain_text(path)
    return _blocks_via_pymupdf(path)


def _plain_text_lines(path):
    """读纯文本并按行返回 (行文本, 页码, bbox)。

    行号从 1 起算，**空行也占一个行号**（不跳过）。
    为什么不跳过空行：extract_lines 与 _blocks_from_plain_text 必须用同一套行号，
    否则 toc 认出来的标题行号与块的伪坐标对不上，章节归属又会错位。
    """
    out = []
    lines = _read_plain_text(path).splitlines()
    for index in range(len(lines)):
        line_no = index + 1
        # y0 = y1 = 行号：零面积，仅作「同页内的先后」比较用，不是版面坐标
        out.append((lines[index], 1, [0.0, float(line_no), 0.0, float(line_no)]))
    return out


def _blocks_from_plain_text(path) -> list[Block]:
    """纯文本（.md / .txt）按行切成块。

    为什么按「行」而不是按「段」：toc 的编号识别是逐行做的（`3.3　实时链路`
    这种标题独占一行），按段切会把标题与它前面的正文粘在一起，章节就认不出来。
    chunker 之后会按字数上限把相邻块合并回去，所以行粒度不会让切片变碎。

    bbox 里放行号而不是 0：理由见模块 docstring（否则章节栈会被一次压满）。
    """
    blocks = []
    for line, page, bbox in _plain_text_lines(path):
        line = line.strip()
        if line == "":
            continue
        blocks.append(Block(
            page=page,
            bbox=bbox,
            text=line,
            size=_PLAIN_TEXT_SIZE,
            page_width=None,
            page_height=None,
            # 纯文本来源（md / txt）：没有版面，也没有 OCR 参与
            extraction_method="plain",
        ))
    return blocks


def _read_plain_text(path):
    """读纯文本。用 utf-8-sig：有 BOM 时吃掉 BOM，没 BOM 时等同 utf-8。

    不用它的话，带 BOM 的文件首行会多一个不可见字符，标题识别会漏掉第一行。
    """
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def extract_lines(path) -> list:
    """按阅读顺序返回每一行，元素为 (行文本, 页码, bbox)。

    统一了 PDF / DOCX / MD / TXT 四种来源，供 toc 做编号识别 ——
    toc 因此不必知道自己处理的是哪种格式。纯文本来源的页码固定 1，
    bbox 的 y 是行号（见 _plain_text_lines），不是版面坐标。

    PDF 逐页分派：整页没有文本层就交给 OCR（见 ocr.py），
    与 extract_blocks 用同一份页级缓存，所以两边各调一次也只 OCR 一遍。
    """
    if _needs_plain_text(path):
        return _plain_text_lines(path)

    lines = []
    doc = pymupdf.open(path)
    ocr_pages = is_pdf(path)
    is_heading = _heading_predicate()
    for page_index in range(doc.page_count):
        page = doc[page_index]
        page_no = page_index + 1
        if ocr_pages and ocr.page_needs_ocr(page):
            ocr_text = ocr.ocr_lines(path, page, page_no)
            if ocr_text:
                lines.extend(ocr_text)
                continue
        # 与 extract_blocks 同口径：先合并硬断行，再交给 toc 认标题。
        # 不合并的话，被硬切开的标题与正文都会认不出来。
        for text, bbox, _size in normalize.merge_lines(
            _page_raw_lines(page), is_heading=is_heading
        ):
            lines.append((text, page_no, bbox))
    doc.close()
    return lines


def _heading_predicate():
    """返回一个「这行是不是标题」的判定函数，供 normalize.merge_lines 用。

    之所以在函数里延迟导入 toc：toc 在模块级 import 本文件（loader），
    本文件若在模块级 import toc 就形成环形导入。用到时才导入即可断开。
    """
    from app.rag.ingest.toc import _match_heading

    def is_heading(text):
        level, _title = _match_heading(text)
        return level != 0

    return is_heading


def _page_raw_lines(page) -> list:
    """一页的原始行：[(文本, bbox, 字号)]，按 PyMuPDF 给出的阅读顺序。

    用**行级** bbox 而不是块级：合并硬断行、判断章节归属都要求行级精度，
    而且 PyMuPDF 的一个块里常常混着"表格片段 + 小标题"（实测 KB-014 的块 4），
    块级粒度没法把它们分开。

    字号取该行里字符数最多的那一档（沿用旧版「取块主字号」的口径，只是降到行级）。
    行内各 span 用空格拼接：空串拼接会让跨 span 的英文粘成 "helloworld"。
    """
    out = []
    for block in page.get_text("dict").get("blocks", []):
        lines = block.get("lines")
        if lines is None:
            continue          # 图片块没有 lines
        for line in lines:
            parts = []
            size_chars = {}
            for span in line.get("spans", []):
                text = span.get("text", "")
                if text.strip() == "":
                    continue
                parts.append(text)
                size = round(span["size"], 1)     # 一位小数，避免浮点误差
                size_chars[size] = size_chars.get(size, 0) + len(text)
            text = " ".join(parts).strip()
            if text == "" or len(size_chars) == 0:
                continue
            main_size = 0
            max_chars = -1
            for size in size_chars:
                if size_chars[size] > max_chars:
                    max_chars = size_chars[size]
                    main_size = size
            bbox = [float(v) for v in line.get("bbox", (0.0, 0.0, 0.0, 0.0))]
            out.append((text, bbox, main_size))
    return out


def _layout_tables(page, page_no, page_width, page_height):
    """找框线表格并写成 Markdown 块。返回 (表格区域 bbox 列表, Block 列表)。

    find_tables 在异常版面上会抛错（例如没有文本层的页、残缺的图形对象），
    一律当作「没有表格」处理，不让一个可选能力把整份文档的抽取搞挂。
    """
    boxes = []
    blocks = []
    try:
        tables = list(page.find_tables().tables)
    except Exception:
        return boxes, blocks

    for table in tables:
        try:
            rows = table.extract()
            box = [float(v) for v in table.bbox]
        except Exception:
            continue
        markdown = normalize.table_to_markdown(rows)
        if markdown == "":
            continue
        boxes.append(box)
        # 表格没有字号可言，填 0.0（与 OCR 块同一口径：不让它被字号容错当成标题）
        blocks.append(Block(
            page=page_no, bbox=box, text=markdown, size=0.0,
            page_width=page_width, page_height=page_height,
            # 框线表格来自 PDF 自带文本层（find_tables 依赖文本层对象），非 OCR
            extraction_method="text_layer",
        ))
    return boxes, blocks


def _blocks_via_pymupdf(pdf_path) -> list[Block]:
    """从 PDF / DOCX 抽取所有文本块，每块带主字号（PyMuPDF 版面抽取）。

    返回格式（每个元素是一个 Block 对象，字段见 schemas.Block）：
        Block(page=1, bbox=[x0, y0, x1, y1], text="文字", size=9.5,
              page_width=595.0, page_height=842.0)
        ...
    size 说明：一个块里可能混着好几种字号，
    这里取「字符数最多的那种」当作这个块的主字号。
    page_width / page_height 是所在页的原始尺寸（PDF point 单位），
    供前端把 bbox 换算成渲染像素；按页取，不做文档级统一。

    页级分派（扫描件走 OCR）：
        逐页看文本层。整页字符数低于阈值（默认 20）就整页交给 ocr.ocr_page，
        不与该页的文本层混用。混用的风险是同一段文字进两次（OCR 一份、文本层一份），
        检索时重复命中、生成时上下文被同一内容占满。
        只对 PDF 分派：DOCX 没有位图可识别，它的"页"是排版产物。
        OCR 关闭或依赖未装时 ocr_page 返回空，这一页退回文本层逻辑，
        不把那零星几个字也丢掉，同时记一条 pages_skipped_no_engine 供 corpus 报出来。

    粒度与规范化（normalize.py）：
        本函数产出的 Block 是**行级**的，不是 PyMuPDF 的块级 ——
        硬断行（「相对湿」+「度」）会跨块出现，只有行级才拼得回来；
        文本化表格也要按行还成一条条记录。
        三条规范化：合并硬断行 → 表格行还原（表头回填）→ 真表格转 Markdown。
    """
    doc = pymupdf.open(pdf_path)
    blocks = []
    ocr_pages = is_pdf(pdf_path)
    is_heading = _heading_predicate()

    for page_no, page in enumerate(doc, start=1):
        if ocr_pages and ocr.page_needs_ocr(page):
            ocr_blocks = ocr.ocr_page(pdf_path, page, page_no)
            if len(ocr_blocks) > 0:
                blocks.extend(ocr_blocks)
                continue
        # 该页原始尺寸（point 单位）。同一 PDF 不同页尺寸可能不同
        # （A4/A3 混排是合法的），所以跟 page 走，不能全文档取一个值。
        page_width = page.rect.width
        page_height = page.rect.height
        raw_lines = _page_raw_lines(page)

        # 真表格兜底：命中框线表格就写成 Markdown，并把落在表格区域里的原始行剔除，
        # 否则同一段内容会以「表格文本」和「扁平行」两种形态各进一次索引。
        table_boxes, table_blocks = _layout_tables(page, page_no, page_width, page_height)
        if len(table_boxes) > 0:
            raw_lines = [
                item for item in raw_lines
                if not any(normalize.bbox_contains(box, item[1]) for box in table_boxes)
            ]
            blocks.extend(table_blocks)

        merged = normalize.merge_lines(raw_lines, is_heading=is_heading)
        rows = normalize.reconstruct_table_rows(merged)
        for text, bbox, size in (rows if rows is not None else merged):
            blocks.append(Block(
                page=page_no,
                bbox=list(bbox),
                text=text,
                size=size,
                page_width=page_width,
                page_height=page_height,
                # 走到这里说明本页有文本层（没文本层的页已在上方交给 OCR 并 continue）
                extraction_method="text_layer",
            ))

    doc.close()
    return blocks


if __name__ == "__main__":
    import sys

    # 用法：python -m app.rag.ingest.loader 某个文件
    # 不传参数时，默认去 项目根/data/corpus/raw/ 下找所有支持的语料
    if len(sys.argv) > 1:
        paths = [sys.argv[1]]
        skipped = []
    else:
        paths, skipped = discover_corpus_files()

    if len(paths) == 0:
        print("找不到语料：默认去 data/corpus/raw/ 下找 "
              + " / ".join(SUPPORTED_EXTS) + "，请把语料放到那里，或运行时传入路径")
    else:
        for path in paths:
            if not os.path.exists(path):
                print("找不到文件:", path)
                continue
            result = extract_blocks(path)
            print("文件:", path)
            print("共抽取", len(result), "个文本块")
            if len(result) > 0:
                print("第一块页码:", result[0].page)
                print("第一块字号:", result[0].size)
                print("第一块文本预览:", result[0].text[:80])

    if len(skipped) > 0:
        print()
        print("被跳过的文件 " + str(len(skipped)) + " 个（后缀不在 "
              + " / ".join(SUPPORTED_EXTS) + " 里，未进索引）：")
        for name in skipped:
            print("   -", name)
