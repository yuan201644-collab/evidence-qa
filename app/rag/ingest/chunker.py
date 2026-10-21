"""chunker.py - 把文本块和章节树合并成可检索切片（Chunk）

为什么需要这一步：
    loader 给的是「散块」，每块只知道自己第几页、字号多大。
    toc 给的是「章节树」，知道文档分了几章几节。
    但两者谁也没说「这块文字属于哪一章哪一节」。
    chunker 干的就是这个缝合：给每个文本块补上它的章节归属，
    同时记下它在全文里的字符位置，方便后面引用高亮。

输入：
    blocks   loader.extract_blocks 的输出，list[Block]
    sections toc.build_tree 的输出，list[SectionNode]
    doc_id   这份文档的编号，由编排层 / C 给语料时约定

输出：
    list[Chunk]，每个 Chunk 带 section_path（章节路径）和 char 偏移

关于「章节路径」怎么算：
    顺着文档往下读，遇到一个标题就把它压进栈，遇到更小的标题就
    先弹掉同级或更深的，再压。栈里从底到顶就是当前文字的章节路径。
    比如读到「第三章」再读到「3.2 训练」，栈里就是
    ["第三章", "3.2 训练"]，后面这段正文就归到这里。

关于 char_start / char_end：
    把全文按块阅读顺序拼起来（块之间用换行隔开），每个切片记录自己
    在拼接串里的起止位置。这是「虚拟偏移」——只用于全文上下文定位 /
    排序 / 去重，**不是 PDF 像素级定位**，不能用来做精确高亮
    （高亮请用 Chunk.bboxes）。

关于切片（合并）与重叠窗口：
    早期版本「一个 Block 一个 Chunk」，会导致大量超短碎片（1.pdf 实测
    615 个切片里 192 个不超过 10 字），上下文被切碎。现在改为：把
    相邻「同页 + 同章节 + 未超长」的块合并成一个 Chunk，吸收碎片、保住
    上下文。每个 Chunk 同时收集组成块的 bbox 列表（bboxes），作为 PDF
    精确高亮的原生几何定位；纯文本来源（md / txt）没有版面坐标，
    其 bboxes 如实留空（理由见 flush 里对退化框的过滤）。
    重叠窗口作为后续可选项，暂不实现，避免
    过早堆复杂逻辑；当前合并已满足检索粒度需求。
"""

import os
import re

from app.rag.schemas import Chunk, SectionNode

from app.rag.ingest.loader import extract_blocks, _default_pdf_paths
from app.rag.ingest.toc import build_tree


class _Seg:
    """chunker 内部的轻量片段，不参与「Block 来自 loader」的契约。

    build_chunks 的合并流水线只关心三样东西：文字、页码、bbox。
    把流水线换成 _Seg，而不是拿 chunker 自己 new 出来的 Block 顶替，
    避免「Block 一定来自 loader 解析」这条约定被虚拟对象打破。
    _Seg 同时兼容 _by_page_y 的 key（读 .page 与 .bbox[1]）。
    """

    __slots__ = ("text", "page", "bbox", "extraction_method")

    def __init__(self, text, page, bbox, extraction_method=None):
        self.text = text
        self.page = page
        self.bbox = bbox
        # 提取方式只跟着走、不参与合并判定（合并判据仍是同页 + 同章节 + 字数）
        self.extraction_method = extraction_method


def _by_page_y(item):
    """给 sorted 当 key 用：按 (页码, y0) 升序排。

    y0 是块/标题在页面上的纵坐标，同一页内从上到下递增，
    即阅读顺序。只按页码排会让同一页内的顺序不确定，
    既会错配章节归属，也会打乱 char 偏移的阅读顺序。
    """
    return (item.page, item.bbox[1])


def _is_section_before_block(section, block):
    """判断 section 是否在 block 之前（含同一页内按 y 坐标比较）。

    纯按页码排时，同页里「上一章结尾的块」和「下一章开头的标题」
    会被错误归属到同一章；改成 (page, y0) 后，同一页内只有
    y0 不晚于块的标题才推进栈，页内归属正确。
    """
    if section.page < block.page:
        return True
    if section.page > block.page:
        return False
    # 同一页：按 y0 比（y 从上到下是阅读顺序）
    section_y = section.bbox[1]
    block_y = block.bbox[1]
    return section_y <= block_y


def _update_stack(stack, node):
    """把一个章节节点压进栈，保持栈里是从根到当前节的层级链。

    规则：先弹掉栈顶所有 level 大于等于当前节点的（同级或更深的旧节点），
    再把当前节点压进去。这样栈底永远是最上面的章节，栈顶是最近的细节点。
    """
    while len(stack) > 0 and stack[-1].level >= node.level:
        stack.pop()
    stack.append(node)


# 合并阈值：单个切片累积文字超过这个长度就强制切分（按块边界切，不切断行）。
#
# 取值依据（对现有 12 份语料实测，见下方数据）：调大到 480 之后就不再起约束作用，
# 是「同页 + 同章节」这条合并规则本身把切片切碎的，不是这个上限。
#   cap   切片数  平均字数  最长  最短
#   240     68     93.6     239    8
#   360     65     98.0     313    8
#   480     63    101.2     419    8
#   720     63    101.2     419    8   ← 已饱和
# 保留这个上限是为了防别的语料出现超长章节时切片过大（BM25 / 向量会把长文本
# 的相关性打薄），480 对现有语料等于不上限，对更长的章节仍能兜住。
MAX_CHUNK_CHARS = 480

# A numbered body clause can be visibly displaced from its logical section in a
# document whose extracted page order is scrambled (for example, KB-015). Keep
# section heading numbers and body clause numbers separate: a heading is a
# short label, while a body clause usually continues with sentence punctuation.
_NUMERIC_SECTION = re.compile(r"^(\d+(?:\.\d+)*)\s+")
_NUMERIC_CLAUSE = re.compile(r"^\s*(\d+(?:\.\d+)+)\s+")


def _numeric_section_paths(sections: list[SectionNode]) -> dict[str, list[str]]:
    """Build unambiguous paths for numbered headings such as ``5`` / ``5.2``."""
    by_number: dict[str, list[SectionNode]] = {}
    for section in sections:
        match = _NUMERIC_SECTION.match(section.title)
        if match:
            by_number.setdefault(match.group(1), []).append(section)

    paths: dict[str, list[str]] = {}

    def resolve(number: str, visiting: frozenset[str] = frozenset()) -> list[str] | None:
        if number in paths:
            return paths[number]
        matches = by_number.get(number, [])
        if len(matches) != 1 or number in visiting:
            return None

        section = matches[0]
        parent = number.rsplit(".", 1)[0] if "." in number else None
        if parent and parent in by_number:
            parent_path = resolve(parent, visiting | {number})
            if parent_path is None:
                return None
            path = parent_path + [section.title]
        else:
            path = [section.title]
        paths[number] = path
        return path

    for number in by_number:
        resolve(number)
    return paths


def _numbered_clause_path(text: str, section_paths: dict[str, list[str]]) -> list[str] | None:
    """Resolve a numbered body clause to its deepest known numbered section."""
    match = _NUMERIC_CLAUSE.match(text)
    if match is None:
        return None
    parts = match.group(1).split(".")
    for depth in range(len(parts), 0, -1):
        path = section_paths.get(".".join(parts[:depth]))
        if path is not None:
            return path
    return None


def _split_long_block(block, max_chars):
    """超长单块预切分：一个 Block 的文字超过 max_chars 时，

    按字符数切成若干不超过 max_chars 的子片段。子片段继承原块的 page 与
    bbox（我们没有字符级 bbox，整块共用一个几何框，高亮精度够用），
    保持原块内部的阅读顺序。返回的是内部 _Seg，**不创建虚拟 Block**，
    以保住「Block 一定来自 loader 解析」这条契约。这样合并后任何
    切片都不会超过 max_chars。
    """
    text = block.text
    if len(text) <= max_chars:
        return [_Seg(text, block.page, block.bbox,
                     getattr(block, "extraction_method", None))]
    segs = []
    start = 0
    while start < len(text):
        segs.append(_Seg(text[start:start + max_chars], block.page, list(block.bbox),
                         getattr(block, "extraction_method", None)))
        start = start + max_chars
    return segs


def _expand_blocks(blocks, max_chars):
    """把 blocks 里所有超长块展开成切分后的 _Seg 列表（保留顺序）。"""
    out = []
    for b in blocks:
        out.extend(_split_long_block(b, max_chars))
    return out


def _title_from_doc_id(doc_id):
    """从文档编号推出可读的显示名（doc_title 的兜底策略）。

    C 的语料命名规范是「KB-001_中文标题」，而 doc_id 直接取「文件名去扩展名」，
    于是变成 "KB-001_智能计算设备年度维保服务合同" 这种带编号前缀的形式。
    契约里写了 doc_id 是内部标识、不可直接展示，所以拿它当显示名是不合格的。

    这里剥掉编号前缀，退回干净的中文标题。不符合该命名规范的（比如 "1"）
    原样返回，不做猜测——宁可显示得朴素，也不要瞎猜一个名字。
    """
    m = re.match(r"^[A-Za-z]{2}-\d+_(.+)$", doc_id)
    if m:
        return m.group(1)
    return doc_id


def build_chunks(blocks, sections, doc_id, doc_title=None, max_chars=MAX_CHUNK_CHARS) -> list[Chunk]:
    """把 blocks 和 sections 合并成带章节归属、带 bbox 的 Chunk 列表。

    两遍处理：
        第零步：超长单块先按 max_chars 预切分（见 _split_long_block），
                保证后续任何切片都不会超过 max_chars。
        第一遍：给每个块算章节归属 section_path（逻辑不变，沿用 y 坐标比较）。
        第二遍：把相邻「同页 + 同章节 + 累积未超长」的块合并成一个 Chunk，
                同时收集组成块的 bbox 列表，作为 PDF 精确高亮的几何定位。

    参数：
        blocks   list[Block]，建议已按页码升序（这里会再排一次，防乱序）
        sections list[SectionNode]，建议已按出现顺序（这里会再排一次）
        doc_id   str，文档编号
        doc_title str | None，文档显示名。None 时自动从 doc_id 剥掉
                 KB 编号前缀（见 _title_from_doc_id），而不是直接用 doc_id。
                 引用卡片要展示人类可读名，doc_id 是内部编号不可直接显示；
                 上层若有更合适的显示名（如 manifest 里的）请显式传进来。
        max_chars 单个切片累积文字上限，超过即切分

    返回：
        list[Chunk]，按阅读顺序排列
    """
    if doc_title is None:
        doc_title = _title_from_doc_id(doc_id)
    # 页 -> (页宽, 页高)：Chunk 要带上所在页的原始尺寸（前端换算渲染像素用）。
    # 合并要求同页，所以一个 Chunk 只需一份尺寸，从该页任一 block 取即可。
    page_sizes = {}
    for b in blocks:
        page_sizes[b.page] = (b.page_width, b.page_height)
    # 先把超长块展开成 _Seg，再排序；切分产生的子片段同 page 同 y0，排序后仍相邻
    segs = sorted(_expand_blocks(blocks, max_chars), key=_by_page_y)
    sections_sorted = sorted(sections, key=_by_page_y)

    # ---- 第一遍：逐片段算章节归属 ----
    seg_paths = []      # 与 segs 一一对应，每项是该片段的章节路径
    stack = []          # 当前章节栈，元素是 SectionNode
    j = 0               # sections 指针
    numbered_section_paths = _numeric_section_paths(sections)
    for seg in segs:
        # 把「在当片段之前（含同页 y0 不晚于片段）」的章节都推进栈里
        while j < len(sections_sorted) and _is_section_before_block(sections_sorted[j], seg):
            _update_stack(stack, sections_sorted[j])
            j = j + 1

        # 栈为空说明这片段在第一个标题之前（封面、目录页等）——它确实没有章节归属，
        # 用空路径如实表达。
        # 不能用"该block在第一个标题之前"这类占位串：它是非空字符串，会被当成
        # 真实章节名一路带到引用卡片，也会让评测的块锚点对不上（下游 DocRef.section_path
        # 为 "" 时，评测按「仅文档级命中」判定；顶着占位串反而连这条路都走不通）。
        current_path = [node.title for node in stack]
        clause_path = _numbered_clause_path(seg.text, numbered_section_paths)
        if clause_path is not None and clause_path != current_path:
            seg_paths.append(clause_path)
        else:
            seg_paths.append(current_path)

    # ---- 第二遍：合并相邻同页同章节片段 ----
    chunks = []
    offset = 0          # 全文拼接偏移，记录每个切片在拼接串里的起点

    cur_segs = []       # 当前正在累积的片段（_Seg）
    cur_path = None     # 当前累积片段的章节路径
    cur_page = None     # 当前累积片段的页（合并要求同页）
    cur_len = 0         # 当前累积文字真实长度（含片段间换行），即 join 后长度

    def flush():
        """把当前累积的片段封口成一个 Chunk。"""
        nonlocal cur_segs, cur_path, cur_page, cur_len, offset  #声明要修改外层变量
        if len(cur_segs) == 0:
            return
        text = "\n".join(seg.text for seg in cur_segs)  #用换行拼片段
        start = offset
        end = offset + len(text)
        # 组成块的 bbox 收成 4 元组（Chunk.bboxes 已收窄为 tuple[float,float,float,float]），
        # 原生几何定位，供前端在 PDF 上画高亮框。
        #
        # 只收「有面积」的框：纯文本来源（md / txt）的 bbox 是「行号伪坐标」
        # （x0 == x1 == 0，y0 == y1 == 行号，见 loader），它只用来比较先后顺序，
        # 不是版面坐标。收进来会让前端在 PDF 上画一个零面积的错位小方块。
        # 这类切片本就拿不到版面几何，如实留空。
        bboxes = []
        for seg in cur_segs:
            x0, y0, x1, y1 = seg.bbox
            if x1 > x0 and y1 > y0:
                bboxes.append((x0, y0, x1, y1))
        page_w, page_h = page_sizes.get(cur_page, (None, None))
        # 提取方式：合并要求「同页」，而来源是 loader 按页判定的，故正常情况下
        # 本切片内取值唯一。这里仍做一次一致性检查——万一将来放宽同页合并，
        # 混合来源会被自动降级为 None（不确定就不猜），而不是随便挑一个。
        methods = {seg.extraction_method for seg in cur_segs}
        extraction_method = methods.pop() if len(methods) == 1 else None
        chunks.append(Chunk(
            doc_id=doc_id,
            text=text,
            page=cur_page,
            section_path=cur_path,
            char_start=start,
            char_end=end,
            bboxes=bboxes,
            page_width=page_w,
            page_height=page_h,
            doc_title=doc_title,
            extraction_method=extraction_method,
        ))
        # 切片之间用换行隔开，下一切片起点要跳过这个换行符
        offset = end + 1

    for idx in range(len(segs)):
        seg = segs[idx]
        p = seg_paths[idx]
        if len(cur_segs) == 0:
            # 开始一个新的累积
            cur_segs.append(seg)
            cur_path = p
            cur_page = seg.page
            cur_len = len(seg.text)
        else:
            same_section = (p == cur_path)
            same_page = (seg.page == cur_page)
            would_overflow = (cur_len + 1 + len(seg.text)) > max_chars  #加上这个片段是否超 240 字
            # 章节变了、跨页了、或累积将超长 —— 先封口，再让本片段开启新切片
            if (not same_section) or (not same_page) or would_overflow:
                flush()
                cur_segs = [seg]
                cur_path = p
                cur_page = seg.page
                cur_len = len(seg.text)
            else:
                cur_segs.append(seg)
                cur_len = cur_len + 1 + len(seg.text)

    flush()   #剩下最后的封口
    return chunks


if __name__ == "__main__":
    import sys as _sys

    # 用法：python -m app.rag.ingest.chunker 某个.pdf
    # 不传参数时，默认去 项目根/data/corpus/raw/ 下找所有 PDF
    if len(_sys.argv) > 1:
        paths = [_sys.argv[1]]
    else:
        paths = _default_pdf_paths()

    if len(paths) == 0:
        print("找不到 PDF：默认去 data/corpus/raw/ 下找，请把语料放到那里，或运行时传入路径")
    else:
        for path in paths:
            if not os.path.exists(path):
                print("找不到文件:", path)
                continue

            blocks = extract_blocks(path)
            sections = build_tree(path)
            doc_id = os.path.splitext(os.path.basename(path))[0]
            chunks = build_chunks(blocks, sections, doc_id)

            print("文件:", path, "doc_id:", doc_id)
            print("文本块数:", len(blocks))
            print("章节节点数:", len(sections))
            print("切片数(合并后):", len(chunks))
            tiny = 0
            no_bbox = 0
            for c in chunks:
                if len(c.text) <= 10:
                    tiny = tiny + 1
                if len(c.bboxes) == 0:
                    no_bbox = no_bbox + 1
            print("  -> 合并吸收碎片：合并前每块一切片理论为 %d，现 %d 个切片" % (len(blocks), len(chunks)))
            print("  -> 不超过10字碎片数:", tiny, "(合并前应约为 %d)" % sum(1 for b in blocks if len(b.text) <= 10))
            print("  -> 无 bbox 的切片数:", no_bbox,
                  "(PDF 应为 0；md/txt 没有版面坐标，应等于切片总数)")

            # 看看前几个切片分别归到了哪个章节，确认归属逻辑对不对
            shown = 0
            for c in chunks:
                if shown >= 6:
                    break
                print("---")
                print("页码:", c.page)
                print("章节路径:", " / ".join(c.section_path))
                print("字符区间:", c.char_start, "-", c.char_end)
                print("组成块数/bbox数:", len(c.bboxes))
                print("文字预览:", c.text[:50].replace("\n", "\\n"))
                shown = shown + 1
