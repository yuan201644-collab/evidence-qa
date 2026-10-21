"""ocr.py - 扫描件（无文本层页面）的离线 OCR 通道

为什么需要这一层：
    KB-007 / KB-008 是整页位图（实测文本层 0 字符），PyMuPDF 抽不出任何文字块，
    于是这两份文档在索引里是 0 切片，检索永远命中不到。
    语料里这样的样本必须有，因为「低质量数据（模糊扫描件、非标准格式）下的稳定性」
    是评分项，题面也把 OCR 明确写进了工具清单。

坐标换算（本模块最容易写错、也最必须验的一处）：
    渲染出来的位图是**像素**坐标，PDF 页面是 **point** 坐标（1/72 英寸）。
    必须按「页面原始尺寸 ÷ 位图尺寸」逐轴换算：
        595 / 1240 = 0.479839（x）    842 / 1755 = 0.479772（y）
    两个方向不保证相同，所以分轴算，不能取一个系数。
    少乘这一步的后果是把 1240 像素宽的页面当 1240 point 宽，高亮框横向偏大 2.08 倍。
    实测：像素框 [117,104,474,137] -> [56.1, 49.9, 227.4, 65.7] point，
    左边距约 2cm，与页面版式相符。

四条设计取舍：
    1. **懒加载 + 缺依赖静默降级**。没装 rapidocr_onnxruntime 时 is_available() 为假，
       链路照常跑（扫描件仍无切片），由 corpus 的 summary 点名报出来，
       不在 import 期抛异常 —— 一个可选能力不该让整条 RAG 链路起不来。
    2. **页级缓存**。实测单页 OCR 7 到 10 秒（150 DPI，与机器负载相关），
       100 页扫描件一次全量要 700 秒以上，每次索引重跑不可接受。
       缓存键取 (文件内容摘要, 页号, DPI)，文件内容一改即失效。
    3. **进程内单例引擎**。模型加载约 0.5 秒，且权重随 wheel 内置（无需联网下模型）。
    4. **不给字号**。OCR 给不出可靠字号，而 toc 的字号容错会把"字比正文大"的行
       当成标题。这里一律填 0.0，永远不可能大于正文字号，等价于让那条规则自然失效，
       章节只靠编号识别（与纯文本来源的处理同源，但比纯文本更严格：
       纯文本给统一常量，混合文档里那个常量仍可能大于正文字号）。
"""

import hashlib
import json
import os
import threading
import time

import numpy as np
import pymupdf

from app.rag.config import env_int
from app.rag.schemas import Block

#: 开关（0 = 关闭 OCR）。沿用 app/rag/config.py 的 env_int；开关类允许 0 所以 minimum=0。
_ENV_ENABLED = "OCR_ENABLED"
_DEFAULT_ENABLED = 1

#: 渲染 DPI。150 是实测的默认值：100 DPI 快约 25%（6.9 秒 vs 8.7 秒/页），
#: 但 KB-007 会少识别一行（8 行 vs 9 行），关键编号仍全中。
#: 初赛的评测要点是编号与数字，宁可慢一点也要全，所以默认 150。
_ENV_DPI = "OCR_RENDER_DPI"
_DEFAULT_DPI = 150

#: 页级判据：一页文本层的字符数少于这个值，就当作"这页没有文本层"。
#: 取 20 是经验值（正常页动辄几百字，扫描页是 0），中间值极少出现。
_ENV_MIN_CHARS = "OCR_MIN_TEXT_CHARS"
_DEFAULT_MIN_CHARS = 20

#: 页级缓存目录。默认放 agent_temp/（协作规范：中间产物统一放这里，已 gitignore）。
_ENV_CACHE_DIR = "OCR_CACHE_DIR"
_DEFAULT_CACHE_DIR = os.path.join("agent_temp", "ocr_cache")

#: 模块名，用于可用性探测。用 importlib 的 find_spec 探测，不真的 import，
#: 避免在没装的机器上把 cv2 / onnxruntime 的导入错误抛到调用方。
_PACKAGE = "rapidocr_onnxruntime"

#: OCR 不给字号，一律填 0.0。为什么是 0 而不是像纯文本那样给个常量：
#: 混合文档（有的页有文本层、有的页是扫描件）里，常量若大于正文字号，
#: toc 的字号容错会把 OCR 行当成"字比正文大"的标题。0.0 永远不可能大于正文字号，
#: 等价于让那条规则自然失效，章节只靠编号识别（理由见模块 docstring 第 4 条）。
_OCR_FONT_SIZE = 0.0

_ENGINE = None
_ENGINE_LOCK = threading.Lock()
_AVAILABLE = None
#: 进程内统计，供 corpus 打印与效率章节引用。
_STATS = {"pages_ocr": 0, "pages_cached": 0, "pages_skipped_no_engine": 0, "seconds": 0.0}
#: 文件内容摘要的进程内备忘：(路径, 大小, mtime_ns) -> 摘要。同一份文件只哈希一次。
_FILE_KEY_MEMO = {}


def enabled():
    """OCR 总开关。默认开。"""
    return env_int(_ENV_ENABLED, _DEFAULT_ENABLED, minimum=0) != 0


def is_available():
    """rapidocr_onnxruntime 是否可用。结果缓存，探测本身很轻。"""
    global _AVAILABLE
    if _AVAILABLE is None:
        import importlib.util

        _AVAILABLE = importlib.util.find_spec(_PACKAGE) is not None
    return _AVAILABLE


def unavailable_reason():
    """不可用时给人看的原因；可用时返回 None。"""
    if not enabled():
        return "OCR_ENABLED=0（开关关闭）"
    if not is_available():
        return "未安装 " + _PACKAGE + "（离线 OCR 依赖）"
    return None


def stats():
    """本进程的 OCR 统计快照。"""
    snapshot = dict(_STATS)
    snapshot["available"] = is_available()
    snapshot["enabled"] = enabled()
    return snapshot


def config_snapshot():
    """OCR 的配置与可用性快照，不含运行计数。

    给基线工具（eval/tools/parse_health.py）用：M2 之后**切片数取决于 OCR 能不能跑**
    （扫描件在没装依赖时是 0 切片、装了就有切片），所以两份基线要可比，
    就必须把「当时的 OCR 配置与可用性」一起记下来。这里只读配置，不触发任何 OCR。
    """
    return {
        "enabled": enabled(),
        "available": is_available(),
        "reason": unavailable_reason(),
        "dpi": env_int(_ENV_DPI, _DEFAULT_DPI, minimum=72),
        "min_text_chars": env_int(_ENV_MIN_CHARS, _DEFAULT_MIN_CHARS, minimum=0),
        "cache_dir": _cache_dir(),
    }


def reset_stats():
    """清空统计（测试与重复测量用）。"""
    for key in _STATS:
        _STATS[key] = 0 if key != "seconds" else 0.0


def _get_engine():
    """懒加载 RapidOCR 引擎，进程内单例。

    加锁是因为 /api/ask 走线程池：两个请求同时首次触发索引会各建一个引擎，
    每个都占一份 onnxruntime 会话与模型内存。
    """
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                from rapidocr_onnxruntime import RapidOCR

                _ENGINE = RapidOCR()
    return _ENGINE


def file_key(path):
    """文件内容摘要，做缓存键用。

    为什么用内容摘要而不是 mtime：复制、解压、git checkout 都会改 mtime，
    改名或换一份同名文件却可能保持 mtime。内容摘要既不误判也不漏判。
    代价是每次索引重算一遍（语料单份 40 到 80 KB，实测可忽略）。
    """
    stat = os.stat(path)
    memo_key = (os.path.abspath(path), stat.st_size, stat.st_mtime_ns)
    hit = _FILE_KEY_MEMO.get(memo_key)
    if hit is not None:
        return hit

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    digest = h.hexdigest()
    _FILE_KEY_MEMO[memo_key] = digest
    return digest


def page_needs_ocr(page):
    """这一页是否需要走 OCR：文本层字符数低于阈值。

    只判字符数，不看有没有图片。理由：一页可能是"文字 + 一张插图"的正常版面，
    那种页文本层是好的，不该整页交给 OCR 去覆盖（OCR 反而会丢字）。
    """
    return len(page.get_text("text").strip()) < env_int(
        _ENV_MIN_CHARS, _DEFAULT_MIN_CHARS, minimum=0
    )


def _cache_dir():
    raw = os.environ.get(_ENV_CACHE_DIR) or _DEFAULT_CACHE_DIR
    if not os.path.isabs(raw):
        # app/rag/ingest/ocr.py -> app/rag/ingest -> app/rag -> app -> 项目根
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
        raw = os.path.join(root, raw)
    return raw


def _cache_path(key, page_no, dpi):
    return os.path.join(_cache_dir(), "%s_p%d_d%d.json" % (key[:16], page_no, dpi))


def _read_cache(path):
    """读缓存。读不到或内容坏了都当成"没有缓存"，不抛错。"""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or "lines" not in payload:
        return None
    return payload


def _write_cache(path, payload):
    """写缓存。写不进去（磁盘满、无权限）只提示，不影响本次返回。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError:
        return False
    return True


def _to_point_bbox(pixel_points, scale_x, scale_y):
    """把 OCR 的四个像素角点收成一个 point 矩形 [x0, y0, x1, y1]。

    OCR 给的 box 是四个角点（左上、右上、右下、左下），可能有轻微倾斜。
    取 min/max 得到外接矩形，再逐轴乘换算系数（理由见模块 docstring）。
    """
    xs = [p[0] for p in pixel_points]
    ys = [p[1] for p in pixel_points]
    return [
        round(min(xs) * scale_x, 2),
        round(min(ys) * scale_y, 2),
        round(max(xs) * scale_x, 2),
        round(max(ys) * scale_y, 2),
    ]


def ocr_page(path, page, page_no, dpi=None):
    """对一页做 OCR，返回 list[Block]，带换算回 point 的 bbox。

    返回空列表的三种情况，都不抛异常：
      1. 总开关关闭，或依赖未安装（记进 pages_skipped_no_engine）
      2. 这一页其实有文本层（调用方不该调它，这里兜一层防误用）
      3. OCR 引擎返回 0 行（空白页）

    缓存命中时直接读 JSON 重建 Block，不再跑引擎。
    """
    if not enabled() or not is_available():
        _STATS["pages_skipped_no_engine"] += 1
        return []
    if not page_needs_ocr(page):
        return []

    use_dpi = dpi if dpi is not None else env_int(_ENV_DPI, _DEFAULT_DPI, minimum=72)
    cache_file = _cache_path(file_key(path), page_no, use_dpi)

    payload = _read_cache(cache_file)
    if payload is not None:
        _STATS["pages_cached"] += 1
    else:
        payload = _run_engine(page, page_no, use_dpi)
        _write_cache(cache_file, payload)

    page_width = payload.get("page_width")
    page_height = payload.get("page_height")
    blocks = []
    for line in payload["lines"]:
        text = (line.get("text") or "").strip()
        if text == "":
            continue
        bbox = line.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        blocks.append(Block(
            page=page_no,
            bbox=list(bbox),
            text=text,
            size=_OCR_FONT_SIZE,
            page_width=page_width,
            page_height=page_height,
            # 本页没有文本层、由离线 OCR 识别得到（调用方按页分派到这里）
            extraction_method="ocr",
        ))
    return blocks


def _run_engine(page, page_no, dpi):
    """真正跑一次 OCR，返回可落盘的 payload。

    输入用 numpy 数组而不是 PNG 字节：实测同一页 9.3 秒 vs 15.7 秒，
    PNG 那条路要多一次编码 + 解码。像素内容与抽内嵌图片一致（同一份渲染图），
    识别结果逐行相同。
    """
    pix = page.get_pixmap(dpi=dpi)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)

    # 逐轴换算系数：不能只算一个（实测 x 0.479839 / y 0.479772）
    scale_x = page.rect.width / pix.width
    scale_y = page.rect.height / pix.height

    t0 = time.time()
    result, _elapse = _get_engine()(arr)
    _STATS["pages_ocr"] += 1
    _STATS["seconds"] += time.time() - t0

    lines = []
    for item in (result or []):
        box, text, score = item[0], item[1], item[2]
        text = (text or "").strip()
        if text == "":
            continue
        lines.append({
            "text": text,
            "score": round(float(score), 4),
            "bbox": _to_point_bbox(box, scale_x, scale_y),
        })

    return {
        "page": page_no,
        "dpi": dpi,
        "pix": [pix.width, pix.height],
        "page_width": page.rect.width,
        "page_height": page.rect.height,
        "scale": [round(scale_x, 6), round(scale_y, 6)],
        "lines": lines,
    }


def ocr_lines(path, page, page_no, dpi=None):
    """与 ocr_page 同源，返回 toc 需要的形式：(文本, 页码, bbox)。

    走同一份缓存，所以 toc 与 loader 各自调用也只 OCR 一次。
    """
    return [
        (block.text, block.page, list(block.bbox))
        for block in ocr_page(path, page, page_no, dpi=dpi)
    ]


def average_score(path, page, page_no, dpi=None):
    """这一页 OCR 的平均置信度（诊断用，没有缓存时返回 None 而不触发 OCR）。"""
    use_dpi = dpi if dpi is not None else env_int(_ENV_DPI, _DEFAULT_DPI, minimum=72)
    payload = _read_cache(_cache_path(file_key(path), page_no, use_dpi))
    if not payload or not payload.get("lines"):
        return None
    scores = [line.get("score", 0.0) for line in payload["lines"]]
    return round(sum(scores) / len(scores), 4)


if __name__ == "__main__":
    import sys

    from app.rag.ingest.loader import discover_corpus_files

    print("开关:", enabled(), "| 依赖可用:", is_available(), "| 原因:", unavailable_reason())
    print("渲染 DPI:", env_int(_ENV_DPI, _DEFAULT_DPI, minimum=72),
          "| 页级字符阈值:", env_int(_ENV_MIN_CHARS, _DEFAULT_MIN_CHARS, minimum=0))
    print("缓存目录:", _cache_dir())

    paths = sys.argv[1:] or discover_corpus_files()[0]
    for path in paths:
        if not str(path).lower().endswith(".pdf"):
            continue
        doc = pymupdf.open(path)
        for page_no, page in enumerate(doc, start=1):
            if not page_needs_ocr(page):
                continue
            t0 = time.time()
            blocks = ocr_page(path, page, page_no)
            print("%s 第 %d 页：%d 块，耗时 %.2f 秒"
                  % (os.path.basename(path), page_no, len(blocks), time.time() - t0))
            for block in blocks[:3]:
                print("   bbox=%s  字号 %.1f  %s" % (block.bbox, block.size, block.text[:40]))
        doc.close()
    print("统计:", stats())
