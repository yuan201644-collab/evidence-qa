"""toc.py - 章节目录识别

有三条路，按准确度从高到低：
1. 内嵌目录：PDF 自带的书签（阅读器左侧那棵树），最准，直接读。
2. 编号容错：目录丢失、且标题与正文**同字号**时，靠「编号写法」认标题。
   语料里的实际写法：`第二章　质保条款`、`3　核心技术`、`3.3　实时链路`、`一、環境參數記錄`。
3. 字号容错：编号也认不出时，靠「字比正文大」反推标题。
你的语料（data/corpus/raw/ 下的 PDF）多数走第二条路。

运行方式（任意目录均可，路径自动锚定项目根）：
    python -m app.rag.ingest.toc
    python -m app.rag.ingest.toc data/corpus/raw/某文件.pdf
"""

import os
import re

import pymupdf

from app.rag.ingest.loader import _default_pdf_paths, _needs_plain_text, extract_blocks, extract_lines
from app.rag.schemas import SectionNode

# 编号型标题的识别规则。为什么需要它：
#   这批语料的章节标题与正文**同字号**（实测 KB-001 全文 10.5，只有文档大标题 15.0），
#   靠"字号比正文大"一条都认不出来，于是目录树只剩文档标题一个节点，
#   所有切片的 section_path 全是文档名，引用卡片读不出在哪一章。
# 规则按「更具体的先匹配」排序，正则的第 1 组是编号、第 2 组是标题正文。
_HEADING_PATTERNS = (
    (re.compile(r"^(第[一二三四五六七八九十百零〇\d]+章)\s*[\u3000 ]+(.+)$"), 1),
    (re.compile(r"^(第[一二三四五六七八九十百零〇\d]+节)\s*[\u3000 ]+(.+)$"), 2),
    # 两级以上编号（3.3 实时链路）必须排在单级编号（3 核心技术）前面，否则会被截成 "3"
    (re.compile(r"^(\d+(?:\.\d+)+)\s*[\u3000 ]+(.+)$"), 2),
    (re.compile(r"^(\d+)\s*[\u3000 ]+(.+)$"), 1),
    (re.compile(r"^([一二三四五六七八九十]+)\s*[、，,.]\s*(.+)$"), 1),
)

# 标题正文最长多少字，以及标题里不该出现的句读。
# 这两条一起用来挡掉"1.1　服务对象：本合同覆盖甲方……"这类条款句子：
# 它们同样以编号开头，但后面跟的是整段正文，会带句号、分号、冒号。
# 语料里的真标题都很短且无句读（服务范围 / 3.3 实时链路 / 三、异常与处置）。
_MAX_HEADING_TITLE_LEN = 20
_SENTENCE_PUNCT = "。；;：:，,？！"
_ENUMERATED_LABEL = re.compile(
    r"^([一二三四五六七八九十百零〇]+)\s*[、，,.]\s*([^：:]{1,20}?)\s*[：:]"
)

# 「目录混乱版」样本的标题行前面带一个字号标记（实测形如 "[2.5] 第二章　质保条款"），
# 那是生成这批对照样本时把字号写进了正文。不剥掉的话编号就不在行首，一条都认不出。
_SIZE_MARKER = re.compile(r"^\[\d+(?:\.\d+)?\]\s*")

# Markdown 标题前缀（`# 1　接口概览` / `## 2.1 请求体`）。语料里的 .md 用它标层级，
# 剥掉之后才能交给统一的编号规则判断。`**加粗**` 一并去掉，否则标题会带星号。
_MD_PREFIX = re.compile(r"^#{1,6}\s*")

# 标题里的各种空白。中文排版常用**全角空格**分隔编号与标题（`第二章　数据分级`），
# 而契约示例与评测用例写的块锚点是半角（`第二章 质保条款`）。
_WHITESPACE = re.compile(r"[\s\u3000]+")


def _normalize_title(text):
    """把标题里的空白统一成单个半角空格，并去掉首尾空白。

    为什么必须统一：章节标题有两个来源（PDF/DOCX 的内嵌书签、按编号识别），
    原文什么写法都有。不过同一套归一的话，同一份文档换一条解析路径就会产出
    不同写法的 section_path，引用卡片和评测的块锚点都会对不上。
    实测 KB-005（DOCX，唯一带内嵌书签的语料）书签原文是 `第二章　数据分级`
    （全角空格），而用例里手写的锚点是 `第二章 数据分级`，字面不等导致
    rag-e-02 明明检索命中却被判未命中。
    """
    return _WHITESPACE.sub(" ", str(text)).strip()


def project_root():
    """从当前脚本位置反推项目根目录。

    toc.py 在 app/rag/ingest/ 下，往上走四级就是项目根：
        app/rag/ingest/toc.py -> app/rag/ingest -> app/rag -> app -> 项目根
    默认语料路径也基于它拼接（具体文件名由 _default_pdf_paths 发现，
    不再写死单份 1.pdf），不受你从哪个目录运行脚本的影响。
    """
    here = os.path.abspath(__file__)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))
    return root


def build_tree(pdf_path) -> list[SectionNode]:
    """统一入口。PDF/DOCX 走三条路，纯文本只走一条。

    - PDF / DOCX：内嵌书签 → 按编号认 → 字号容错
    - MD / TXT  ：只走「按编号认」。它们没有内嵌书签，也没有字号
                  （loader 给全篇同一个默认字号），走字号容错只会误判。

    返回格式（各条路输出一致，后面的 chunker 不用区分）：
        [SectionNode(level=1, title="第一章 服务范围", page=1), ...]
    """
    if _needs_plain_text(pdf_path):
        return build_tree_by_numbering(pdf_path)

    doc = pymupdf.open(pdf_path)
    raw_toc = doc.get_toc()   # 读内嵌书签，每项 (层级, 标题, 页码)
    doc.close()

    tree = []
    if len(raw_toc) > 0:
        for item in raw_toc:
            level = item[0]
            # 书签标题同样要过空白归一（见 _normalize_title）：书签原文的全角空格
            # 会让 section_path 与按编号识别那条路径的写法不一致。
            title = _normalize_title(item[1])
            page = item[2]
            # 内嵌书签路径没有 bbox，留默认 [0,0,0,0]，退化为页级匹配（选项 A）
            node = SectionNode(level=level, title=title, page=page)
            tree.append(node)
        return tree

    # 能走到这里，说明这份 PDF 没有内嵌目录。先按编号认标题：
    # 它比字号可靠（不依赖排版差异），产出的标题也更接近评测里的块锚点写法。
    tree = build_tree_by_numbering(pdf_path)
    if len(tree) > 0:
        return tree

    return build_tree_by_fontsize(pdf_path)


def _match_heading(text):
    """把一行文字判成标题，返回 (level, 规范化标题) 或 (0, None)。

    规范化：编号与标题之间统一成一个半角空格。原文用的是全角空格或顿号
    （`第二章　质保条款`、`一、環境參數記錄`），统一之后引用卡片好读，
    也能和评测用例里手写的块锚点（`第二章 质保条款`）对上写法。

    入口先剥两类前缀：目录混乱版的字号标记 `[2.5]`，以及 Markdown 的 `#`。
    """
    line = _SIZE_MARKER.sub("", text.strip())
    line = _MD_PREFIX.sub("", line).replace("**", "").strip()
    if line == "":
        return 0, None

    # OCR 扫描件常把「一、字段名：值」作为一整行，既是独立可检索条目，
    # 又没有单独的标题行。把冒号前短标签作为章节锚点，原始整行仍留在正文切片。
    label_match = _ENUMERATED_LABEL.match(line)
    if label_match is not None:
        number = label_match.group(1)
        label = label_match.group(2).strip()
        if label and not any(p in label for p in _SENTENCE_PUNCT):
            return 1, _normalize_title(number + " " + label)

    for pattern, level in _HEADING_PATTERNS:
        m = pattern.match(line)
        if m is None:
            continue
        number = m.group(1).strip()
        title = m.group(2).strip()
        if title == "" or len(title) > _MAX_HEADING_TITLE_LEN:
            continue
        # 真标题不带句读；带句号/分号/冒号的通常是"编号 + 整句话"的条款正文
        if any(p in title for p in _SENTENCE_PUNCT):
            continue
        return level, _normalize_title(number + " " + title)
    return 0, None


def build_tree_by_numbering(pdf_path) -> list[SectionNode]:
    """目录丢失时的容错之一：按编号写法认标题，不依赖字号。

    逐行扫，行首是 `第X章` / `X` / `X.Y` / `中文数字、` 且后面跟的是一小段文字，
    就判为标题。行的来源统一交给 loader.extract_lines —— 它把 PDF / DOCX / MD / TXT
    都归成 (行文本, 页码, bbox)，所以这里不必知道自己处理的是哪种格式。

    用**行级** bbox 而不是块级：标题常常和上一段的尾巴挤在同一个文本块里，
    用块级坐标会把整块错判到新章节去。纯文本来源的 bbox 放的是行号伪坐标
    （x0 = x1 = 0，y0 = y1 = 行号），与 loader 给块的伪坐标同一套编号，
    所以两边比大小是有效的。

    扫描顺序即阅读顺序，因此产出天然按出现顺序排列（目录混乱版文档也照此处理，
    它本来就没有正确的章节顺序可言）。
    """
    tree = []
    for text, page, bbox in extract_lines(pdf_path):
        level, title = _match_heading(text)
        if level == 0:
            continue
        tree.append(
            SectionNode(level=level, title=title, page=page, bbox=list(bbox))
        )
    return tree


def build_tree_by_fontsize(pdf_path, max_title_len=50) -> list[SectionNode]:
    """目录丢失时的容错：靠字号把标题找出来。

    三步：
    1. 统计全篇每个字号的总字符数，字最多的那档就是「正文字号」。
    2. 字号大于正文、且文字很短的块，判为标题。
    3. 把标题按字号从大到小分档，最大的当一级，其次二级，依次往下。

    max_title_len 是「标题最长多少字」的阈值，用来挡掉正文长句。
    这个值要按实际文档调，先给 50。
    """
    blocks = extract_blocks(pdf_path)
    tree = []
    if len(blocks) == 0:
        return tree

    # 1) 找正文字号：统计每种字号的总字符数，取最多的那档
    size_chars = {}
    for b in blocks:
        size = b.size
        if size in size_chars:
            size_chars[size] = size_chars[size] + len(b.text)
        else:
            size_chars[size] = len(b.text)

    body_size = 0
    max_chars = -1
    for size in size_chars:
        if size_chars[size] > max_chars:
            max_chars = size_chars[size]
            body_size = size

    # 2) 挑标题候选：字号比正文大，且文字不长
    candidates = []
    for b in blocks:
        if b.size > body_size and len(b.text) <= max_title_len:
            candidates.append(b)

    # 3) 收集这些标题里出现过的字号，去掉重复，从大到小排
    sizes = []
    for b in candidates:
        size = b.size
        if size not in sizes:
            sizes.append(size)
    sizes.sort(reverse=True)

    # 最大的字号排第一级，其次第二级，依次往下
    level_of_size = {}
    i = 0
    for size in sizes:
        level_of_size[size] = i + 1
        i = i + 1

    for b in candidates:
        node = SectionNode(
            level=level_of_size[b.size],
            title=b.text,
            page=b.page,
            bbox=b.bbox,
        )
        tree.append(node)

    return tree


if __name__ == "__main__":
    import sys

    # 用法：python -m app.rag.ingest.toc 某个.pdf
    # 不传参数时，默认去 项目根/data/corpus/raw/ 下找所有 PDF
    if len(sys.argv) > 1:
        paths = [sys.argv[1]]
    else:
        paths = _default_pdf_paths()

    if len(paths) == 0:
        print("找不到 PDF：默认去 data/corpus/raw/ 下找，请把语料放到那里，或运行时传入路径")
    else:
        for path in paths:
            if not os.path.exists(path):
                print("找不到文件:", path)
                continue
            tree = build_tree(path)
            print("文件:", path)
            print("共识别", len(tree), "个章节节点")
            for node in tree[:15]:  #先打印前15个章节标题
                indent = "  " * (node.level - 1)  #越深的层级前面的空格越多
                print(indent + "L" + str(node.level) + " " + node.title[:40]
                      + " -> 第" + str(node.page) + "页")
