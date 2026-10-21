"""normalize.py - 版面文本规范化：硬断行合并、表格行还原、真表格转 Markdown

为什么需要这一层（三个都是实测出来的具体缺陷，不是假想）：

1. **硬断行**。PDF 生成器在版面放不下时会把一个词硬切成两行，PyMuPDF 于是抽出两个
   文本块/行：
       "...|　合格　|　相对湿"      +      "度　|　30%~70%..."
       "...|　不高于 6"             +      "5dB　|　58dB..."
       "序列号 SN-8823917，已生"     +      "成更换工单 GD-20260910-07。"
       "验收日期: 2026 年 8 月 12 "  +      "日"
   不合并的后果：BM25 与向量都匹配不上「相对湿度」这种被切开的词，检索直接漏。
   实测 KB-014 / KB-011 各有多处，体检工具报的是 8 处候选。

2. **表格行还原**。这批语料的「表格」没有框线（`find_tables` 全库 0 个对象），
   它是用 `　|　` 分隔的纯文本行。行被硬断之后（见上），整张表变成几行长文本，
   键值对串在一起。这里按「列数」把它还原成一行一条记录，并把表头名回填到每行：
       检查项: 正压值 | 标准范围: 不小于 5Pa | 实测值: 8Pa | 结论: 合格
   这样每一行都是自解释的，检索命中一行就等于拿到一条完整记录。
   列数**靠推断**，推不出来就原样保留（宁可不解，也不要切错）。

3. **真表格兜底**。语料里现在没有框线表格，但换成别的文档就会遇到。
   命中 find_tables 时转成 Markdown 表格（一行一条记录，模型读起来最稳），
   并把该区域内的原始行剔除，避免同一段内容进索引两遍。

三条都是纯函数，不依赖 PyMuPDF 的对象（真表格那条除外），便于单测。

**已知未覆盖**：跨页的硬断行（上一页最后一行被切开、下半截在下一页开头）。
合并只在页内做（调用方按页传行进来）。现有语料只有一份两页文档，
收益不足以支撑把页间顺序判断引进来；真遇到再补，不要先猜。
"""

import re

# 句末标点：出现在接缝两侧说明这里是**真实换行**，不该合并。
# 注意不含逗号、顿号、冒号 —— 那些是句中标点，后面接着的行是同一句的续行。
_SENTENCE_END = "。！？；!?;…"
# 收尾类符号：它们结束一个语义单元，也当作真实换行
_CLOSING = "）」』】》〉”’"

# 表头/单元格的「像标签」判据：不含数字与单位符号、不太长。
# 用途是推断列数时判断「每行的首列是不是一个名称」（见 infer_columns）。
_CELL_DIGIT = re.compile(r"[0-9０-９]")
_UNIT_MARKS = "℃°%~＝=×/+"
_MAX_LABEL_LEN = 8

#: 编号样式：字母 + 连字符 + 数字，可重复多段（CU‐A3‐001 / ST‐A3‐011 / SN‐8823917）。
#: 连字符要认三种写法：语料里用的是 U+2010「‐」，也见过 ASCII「-」与「–」。
#: ⚠️ 必须允许重复的「连字符 + 字母数字」段：只允许一段的写法匹配不上 CU‐A3‐001
#: 这种两段编号，实测会让设备表的列推断直接失败（首列既不像名称也不像编号）。
_ID_LIKE = re.compile(
    r"^[A-Za-z]{1,6}[\-\u2010\u2011\u2013][A-Za-z0-9]+"
    r"(?:[\-\u2010\u2011\u2013][A-Za-z0-9]+)*$"
)

#: 合并硬断行时允许的字号差。同一次硬断行不会改字号；标题转正文一定会改，
#: 所以「字号差太大就当成真实换行」能把「文档大标题 + 紧随其后的字段行」挡住。
_SIZE_TOLERANCE = 0.6


def _is_cjk(ch):
    return "\u4e00" <= ch <= "\u9fff"


def looks_like_continuation(prev_text, next_text, is_heading=None):
    """判断 next 是不是 prev 被硬断下来的续行。

    判据（全部满足才合并）：
      两侧都非空；
      prev 的最后一行不是标题（标题是自成一行的语义单元，后面不该接东西）；
      next 的第一行不是标题（否则会把小标题粘进上一段）；
      prev 不以句末标点或收尾括号结尾，next 不以句末标点开头；
      next 不以分隔符开头（以 `|` 开头的行是另一条表格记录）；
      两侧「是不是表格行」一致。表格的硬断行只会发生在表格内部，
      表格与正文之间是真实边界。实测少了这条，KB-011 表格最后一行的
      「实得分: 5」会把紧随其后的「加权总分 = 19×0.2 + …」粘进来。

    is_heading 由调用方注入（生产环境传 toc 的编号识别）：
    这样本模块不反向依赖 toc，避免重演 loader → toc → loader 那样的环形导入。
    """
    if is_heading is None:
        is_heading = lambda text: False  # noqa: E731

    prev_text = (prev_text or "").rstrip()
    next_text = (next_text or "").lstrip()
    if prev_text == "" or next_text == "":
        return False
    if is_heading(prev_text) or is_heading(next_text):
        return False
    if prev_text[-1] in _SENTENCE_END or prev_text[-1] in _CLOSING:
        return False
    if next_text[0] in _SENTENCE_END or next_text[0] in _CLOSING:
        return False
    if next_text[0] == "|":
        return False
    # 表格与正文之间是真实边界：硬断行只会发生在同一类内容内部。
    # 判据用「有没有分隔符」而不是 is_table_line（后者要求至少 3 个单元格）：
    # 被硬断开的表格片段本身可能只剩 1 个单元格（「|　合格　|　相对湿」就是 2 个），
    # 用 is_table_line 判会把该合并的那两行挡掉（实测踩过）。
    # 反面案例：「…| 实得分: 5」+「加权总分 = …」必须挡住，否则正文被粘进表尾。
    if _has_pipe(prev_text) != _has_pipe(next_text):
        return False
    return True


def _has_pipe(text):
    """这一行是不是表格类文本（含半角或全角竖线）。"""
    return ("|" in text) or ("｜" in text)


def join_seam(prev_text, next_text):
    """按接缝两侧的字符类型决定拼接方式。

    中日韩之间、数字与中日韩之间不加空格（「相对湿」+「度」=「相对湿度」，
    「不高于 6」+「5dB」=「不高于 65dB」）；涉及拉丁字母时补一个空格，
    否则会把两个英文词粘成一个（「hello」+「world」）。
    """
    prev_text = (prev_text or "").rstrip()
    next_text = (next_text or "").lstrip()
    if prev_text == "" or next_text == "":
        return prev_text + next_text
    left = prev_text[-1]
    right = next_text[0]
    if _is_cjk(left) or _is_cjk(right) or left.isdigit() or right.isdigit():
        return prev_text + next_text
    return prev_text + " " + next_text


def should_merge(prev_line, next_line, is_heading=None):
    """两行（都是 (文本, bbox, 字号)）是否应当合并 —— 这是**唯一**的合并判据。

    为什么单独抽出来：loader 用它合并、体检工具用它统计"有多少处会被合并"。
    两边各写一份判据迟早漂移，到时候体检表报的数字就与生产行为对不上。

    两个条件：
      ① 接缝像硬断行（见 looks_like_continuation）；
      ② 两行字号相同（差不超过 _SIZE_TOLERANCE）。同一次硬断行不会改字号；
         字号一变往往是"标题转正文"这类真实换行。实测它能挡住
         「机房运维巡视检查记录表」+「机房名称: A3 机房…」被粘成一行。
    """
    if abs(float(prev_line[2]) - float(next_line[2])) > _SIZE_TOLERANCE:
        return False
    return looks_like_continuation(prev_line[0], next_line[0], is_heading=is_heading)


def merge_lines(lines, is_heading=None):
    """把硬断行合并掉。lines 是 [(文本, bbox, 字号)]，返回同样形状的列表。

    合并后的元素：文本是接缝拼好的，bbox 是所覆盖原始行的并集，
    字号取覆盖行里较大的那一档（与 loader 原本「取主字号」的口径一致）。

    ⚠️ 只合并**相邻且同页**的行（调用方按页传进来，页内天然相邻）。
    判据见 should_merge。
    """
    merged = []
    for line in lines:
        if merged and should_merge(merged[-1], line, is_heading=is_heading):
            prev_text, prev_bbox, prev_size = merged[-1]
            merged[-1] = (
                join_seam(prev_text, line[0]),
                _union_bbox(prev_bbox, line[1]),
                max(prev_size, line[2]),
            )
            continue
        merged.append((line[0], list(line[1]), line[2]))
    return merged


def _union_bbox(a, b):
    """两个 bbox 的并集。用于合并后仍然给得出一个能画的框。"""
    return [
        min(a[0], b[0]),
        min(a[1], b[1]),
        max(a[2], b[2]),
        max(a[3], b[3]),
    ]


# ── 表格行还原 ────────────────────────────────────────────────────────

def split_pipe_row(text):
    """按 `|`（半角与全角）切单元格，去掉两侧的空白与全角空格。"""
    parts = re.split(r"[|｜]", text)
    return [part.replace("\u3000", " ").strip() for part in parts]


def is_table_line(text, min_cells=3):
    """是不是一行文本化的表格：至少 min_cells 个单元格且分隔符够多。"""
    if "|" not in text and "｜" not in text:
        return False
    cells = split_pipe_row(text)
    non_empty = [c for c in cells if c != ""]
    return len(non_empty) >= min_cells


def is_label_like(cell):
    """单元格是否「像一个名称」而不是一个值。

    名称：检查项 / 温度 / 功能完整性 / 合格（不含数字与单位符号，且不太长）。
    值：22.6℃ / 20% / 不小于 5Pa / 3.84TB。
    """
    if cell == "" or len(cell) > _MAX_LABEL_LEN:
        return False
    if _CELL_DIGIT.search(cell):
        return False
    return not any(mark in cell for mark in _UNIT_MARKS)


def is_id_like(cell):
    """单元格是否像一个编号（CU‐A3‐001 / ST‐A3‐011）。"""
    return bool(_ID_LIKE.match(cell))


def is_name_like(cell):
    """首列的合格形态：名称或编号都算（设备表的首列就是编号）。"""
    return is_label_like(cell) or is_id_like(cell)


def cell_class(cell):
    """单元格的粗分类，用于检查「同一列的值是不是同一类」。

    类别顺序有意义：先判编号（它同时含字母与数字），再判数值（含数字），
    然后是纯名称，最后是其它。顺序错了会把 CU‐A3‐001 归成数值。
    """
    if cell == "":
        return "empty"
    if is_id_like(cell):
        return "id"
    if _CELL_DIGIT.search(cell) or any(mark in cell for mark in _UNIT_MARKS):
        return "number"
    if len(cell) <= _MAX_LABEL_LEN:
        return "label"
    return "other"


def _columns_consistent(data_rows):
    """数据行的每一列取值是否同类（**表头行不参与**）。

    为什么需要这条：只靠「首列像名称」约束不住列数。
    实测设备表在 k=2 时首列分别是「设备编号 / 运行状态 / CU‐A3‐001 / 正常…」，
    全都像名称，会被误判；但把列切开看，第 2 列混着「设备类型」「告警数」「GPU 服务器」，
    类别不一致，于是 k=2 被否掉、k=4 胜出。

    ⚠️ 必须排除表头行：表头是名称、数据行是数值，混在一起算的话每一列都"不同类"，
    三个真实表格会全部推断失败（实测踩过：20 / 20 / 27 个单元格全部返回 None）。
    """
    if not data_rows:
        return False
    ncols = len(data_rows[0])
    for col in range(ncols):
        classes = set()
        for row in data_rows:
            classes.add(cell_class(row[col]))
        classes.discard("empty")
        if len(classes) > 1:
            return False
    return True


def infer_columns(cells):
    """从整串单元格里推断列数，推不出来返回 None。

    规则：取满足下面全部条件的最小 k（k >= 2 且整除总数）——
      ① 行数至少 2（要有表头 + 至少一条数据行）；
      ② 表头那一行的每个单元格都像名称（表头里不该出现值）；
      ③ 每一行的首列像名称或编号；
      ④ 每一列内部的取值同类（见 _columns_consistent）。
    为什么取「最小 k」：①②③④ 已经排掉大多数候选，剩下的里面，
    小的 k 更符合「列数远小于行数」的先验。
    ⚠️ 推断失败一律返回 None，调用方原样保留文本。解错比不解更糟：
    切错的边界会制造出不存在的行，让检索给出错误的一行记录。
    """
    total = len(cells)
    if total < 4:
        return None
    for k in range(2, total // 2 + 1):
        if total % k != 0:
            continue
        rows = [cells[i:i + k] for i in range(0, total, k)]
        if len(rows) < 2:
            continue
        if not all(is_label_like(cell) for cell in rows[0]):
            continue
        if not all(is_name_like(row[0]) for row in rows[1:]):
            continue
        if not _columns_consistent(rows[1:]):
            continue
        return k
    return None


def reconstruct_table_rows(lines, min_cells=3):
    """把连续的表格行还原成「一行一条记录」。

    lines 是 [(文本, bbox, 字号)]（已经过 merge_lines）。返回同样形状的列表；
    如果没有任何一段能还原（没有表格行、或列数推不出来），返回 None，
    让调用方原样使用输入 —— 返回等价的列表会让「到底还原了没有」变得看不出来。

    做法：把连续的表格行拼成一条长文本（行之间用 `|` 连接，保持单元格边界），
    切成单元格后按推断出的列数分组，表头名回填到每一行：
        检查项: 正压值 | 标准范围: 不小于 5Pa | 实测值: 8Pa | 结论: 合格
    每行的 bbox 取「贡献了该行单元格的那些原始行」的并集。
    """
    out = []
    reconstructed = False
    index = 0
    total = len(lines)
    while index < total:
        if not is_table_line(lines[index][0], min_cells=min_cells):
            out.append(lines[index])
            index += 1
            continue

        # 找到这一段连续的表格行
        end = index
        while end < total and is_table_line(lines[end][0], min_cells=min_cells):
            end += 1
        group = lines[index:end]

        # 单元格序列，同时记住每个单元格来自哪一行（用来算行的 bbox）
        cells = []
        cell_source = []
        for line_no in range(len(group)):
            text = group[line_no][0]
            row_cells = [c for c in split_pipe_row(text) if c != ""]
            for cell in row_cells:
                cells.append(cell)
                cell_source.append(line_no)

        ncols = infer_columns(cells)
        if ncols is None:
            # 推不出来就原样保留（宁可不解，也不要切错）
            out.extend(group)
            index = end
            continue

        header = cells[:ncols]
        body = cells[ncols:]
        body_source = cell_source[ncols:]
        for start in range(0, len(body), ncols):
            row_cells = body[start:start + ncols]
            # 尾巴不足一列说明总数与列数不匹配（正常情况下不会发生，因为 k 整除总数）
            if len(row_cells) < ncols:
                break
            pairs = []
            for col in range(ncols):
                name = header[col] if col < len(header) else ("第" + str(col + 1) + "列")
                pairs.append(name + ": " + row_cells[col])
            sources = body_source[start:start + ncols]
            bbox = list(group[sources[0]][1])
            for line_no in sources[1:]:
                bbox = _union_bbox(bbox, group[line_no][1])
            size = max(group[line_no][2] for line_no in sources)
            out.append((" | ".join(pairs), bbox, size))
            reconstructed = True

        index = end
    return out if reconstructed else None


# ── 真表格兜底 ────────────────────────────────────────────────────────

def table_to_markdown(rows):
    """把 find_tables 抽出的二维列表转成 Markdown 表格。

    转 Markdown 而不是保留原样：Markdown 表格是"一行一条记录 + 表头显式"，
    模型读起来不容易串行，人也一眼能看懂。空单元格补空串，保持列数对齐。
    """
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    lines = []
    for index in range(len(rows)):
        cells = []
        for col in range(width):
            value = rows[index][col] if col < len(rows[index]) else ""
            cells.append((value or "").replace("\n", " ").strip())
        lines.append("| " + " | ".join(cells) + " |")
        if index == 0:
            lines.append("|" + "|".join([" --- "] * width) + "|")
    return "\n".join(lines)


def bbox_contains(outer, inner):
    """inner 的中心点是否落在 outer 里。用于剔除被真表格覆盖的原始行。"""
    cx = (inner[0] + inner[2]) / 2.0
    cy = (inner[1] + inner[3]) / 2.0
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]
