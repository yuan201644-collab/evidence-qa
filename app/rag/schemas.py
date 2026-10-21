"""schemas.py - RAG 链路全部数据结构定义（B 侧）

为什么需要这个文件：
    链路里每个模块都要把数据传给下一个模块。如果大家都用散装的
    dict（字典）传，字段名拼错、类型不对只能在运行时才炸。
    这里用 pydantic v2 把每个结构「锁死」，实例化时自动校验，
    错一个字段当场就报错，把接口不一致消灭在边界。

哪些是 B 独占、哪些是共享：
    Block / SectionNode / Chunk / Candidate 是 B 内部数据，B 自己定。
    Ref / TraceStep / AskResponse 等共享结构已由 A 在 app.contracts.py
    冻结，统一从那里 import（见 retriever.py / indexer.py），本文件不再重复定义。
"""

from typing import Literal

from pydantic import BaseModel, Field


class Block(BaseModel):
    """loader 的输出：一份语料里的一段文本（**行级**粒度）。

    对应 loader.extract_blocks 返回的每个字典。
    粒度说明（M3 起）：一条 Block 是一个版面行，或若干被硬断开的行合并后的结果，
    也可能是还原出来的一行表格记录 / 一整张 Markdown 表格。
    PDF 里的硬断行会跨 PyMuPDF 的块出现，所以粒度必须落到行，块级拼不回来
    （见 app/rag/ingest/normalize.py）。chunker 之后会按章节与字数上限把相邻
    Block 合并成切片，检索粒度不受这里变细的影响。

    page        这一块在第几页（从 1 开始）
    bbox        块的矩形坐标 [x0, y0, x1, y1]。PDF / DOCX 是真版面坐标；
                纯文本（md / txt）没有版面，这里放**行号伪坐标**
                （x0 = x1 = 0，y0 = y1 = 行号），只用来比较同页内的先后顺序，
                好让 chunker 算对章节归属，**不能用来做高亮**（见 loader docstring）。
    text        块的文字
    size        块的主字号（toc 用它判断标题）
    page_width  所在页的原始宽度（PDF point 单位，PyMuPDF page.rect.width）
    page_height 所在页的原始高度（PDF point 单位，PyMuPDF page.rect.height）
                —— 前端要把 bbox（point 单位、原点左上）换算成渲染像素，
                必须知道页面原始尺寸；同一 PDF 不同页尺寸可能不同，
                所以是「跟着页走」而不是文档级统一值。
    extraction_method
                这段文本的**提取方式**：ocr / text_layer / plain（与
                app/contracts.DocRef.extraction_method 同义同值）。
                由 loader 在**按页分派**时确定，所以同一页内取值唯一；
                它是「这段字是怎么弄出来的」，不是文档来源（来源看 doc_id），
                两者别混。未标记时留 None。
    """

    page: int
    bbox: list
    text: str
    size: float
    page_width: float | None = None
    page_height: float | None = None
    extraction_method: Literal["ocr", "text_layer", "plain"] | None = None


class SectionNode(BaseModel):
    """toc 的输出：章节树里的一个节点。

    对应 toc.build_tree 返回的每个节点。
    level  层级，1 是最大标题，2 是次级，依次往下
    title  这一节的标题文字
    page   落在第几页
    bbox   标题块在页面上的矩形坐标 [x0, y0, x1, y1]。
           默认全 0 向后兼容：y0=0 意味着「总在块之前」，
           等价于旧的纯页级匹配行为（内嵌书签路径无 bbox 时即退化为此）。
           字号容错路径会填真实 bbox，用于页内按阅读顺序（y 从上到下）比较。
           纯文本（md / txt）来源填**行号伪坐标**（y0 = y1 = 行号），
           理由与 Block.bbox 相同。
    """

    level: int
    title: str
    page: int
    # 用 default_factory 而不是可变的默认值 []：可读性上更符合惯例。
    # （pydantic v2 对可变默认会深拷贝、各实例互不影响，行为上本就安全，
    # 这里只是把写法规范化，避免读代码的人误以为会共享同一个 list。）
    bbox: list = Field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])


class Chunk(BaseModel):
    """chunker 的输出：一段可检索的切片。

    和 Block 的区别：Chunk 已经带上了章节归属（section_path），
    且由若干相邻文本块合并而成，是检索和引用的最小单位。
    doc_id       来自哪份文档（C 给语料时要约定）
    text         切片文字（由组成块用换行拼接）
    page         切片首块所在页（合并要求同页，故切片不会跨页）
    section_path 章节路径，如 ["第三章", "3.2 训练"]；在第一个标题之前的内容
                （封面、目录页）确无章节归属，如实留空列表，**不要用占位串顶替**
                —— 占位串会被当成真章节名显示到引用卡片上，下游也分不清
                「这一章真叫这个名字」和「不知道是哪一章」。
    char_start   在「全文按块阅读顺序拼接（块间换行）」中的起始字符位置。
                这是虚拟偏移，仅用于全文上下文定位 / 排序 / 去重，
                **不是 PDF 像素级定位，不能用来做精确高亮**。
    char_end     同上，结束字符位置
    bboxes      组成该切片的每个文本块在页面上的矩形坐标列表，
                每个元素是 (x0, y0, x1, y1) 四元组，按阅读顺序排列，
                且全部位于同一页（page 字段）。这是 PDF 精确高亮的
                原生几何定位依据：引用卡片要画高亮框时，用
                page + page_width/page_height + bboxes 即可在 PDF 上框出原文，
                而不是依赖 char_start/char_end（后者是虚拟偏移，见上）。
                收窄成 4 元组是为了让 pydantic 在边界上就校验「每个框恰好
                4 个数」，而不是任由 list[list] 混进长度不对的坐标。
                纯文本（md / txt）来源没有版面几何，这里**如实为空列表**：
                loader 给它的 bbox 是行号伪坐标，chunker 会过滤掉（见 flush），
                填充假框只会让前端在 PDF 上画一个错位的零面积小方块。
    page_width  所在页原始宽度（PDF point 单位），配 bboxes 供前端换算渲染像素
    page_height 所在页原始高度（PDF point 单位），同上
    doc_title   文档显示名，供引用卡片展示（doc_id 是内部编号，不可读）
    """

    doc_id: str
    text: str
    page: int
    section_path: list[str] = Field(default_factory=list)  # 章节路径；标题之前的内容为空列表
    char_start: int
    char_end: int
    bboxes: list[tuple[float, float, float, float]] = Field(default_factory=list)  # 组成块的 bbox（PDF 原生几何定位，用于高亮）
    page_width: float | None = None   # 所在页原始宽（point），配 bboxes 给前端换算像素
    page_height: float | None = None  # 所在页原始高（point），同上
    doc_title: str | None = None      # 文档显示名（引用卡片展示用）
    extraction_method: Literal["ocr", "text_layer", "plain"] | None = None  # 提取方式，由组成块带上来（合并要求同页，故取值唯一）


class Candidate(BaseModel):
    """retriever 的输出：一个被检索命中的候选切片，附打分。

    chunk   命中的那个 Chunk
    score   综合得分（混合检索 + 重排后）
    method  命中来源，如 "bm25" / "vector" / "rerank"，方便排查
    """

    chunk: Chunk
    score: float
    method: str


