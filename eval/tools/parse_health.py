#!/usr/bin/env python
"""语料解析体检：把「每份语料到底能不能被解析出可检索内容」变成一张可比的表。

为什么需要这个工具（M0 的定位）：
    在动解析器之前，先要有基线。否则「加了 OCR 之后有没有变好」只能靠感觉。
    本工具只读，不改链路、不加载模型、不连数据库，跑一次约 1 至 2 秒。

它回答四个问题（每个都对应一个已知缺口）：
    1) 这份文档有没有文本层？没有就必须走 OCR，否则它在检索里永远命中不到。
    2) 页面里有没有表格对象（PyMuPDF find_tables）？没有的话，「表格结构还原」
       就没有验收对象，只该做行级切片与折行合并。
    3) 有没有中文折行截断（`相对湿` + `度`）？这会让 BM25 与向量都匹配不上完整词。
    4) 切出了几个切片、章节归属是不是合理的？0 切片等于检索必失；
       章节路径全落在最后一章，说明章节栈被一次压满（M1 修的就是这个）。

它不做的事：不给分数、不做判定结论。判定留给改代码前后两次输出的对照。

用法：
    python -m eval.tools.parse_health
    python -m eval.tools.parse_health --out agent_temp/parse_health_before.json
    python -m eval.tools.parse_health data/corpus/raw/KB-010_问数服务API接口说明.md
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import hashlib
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from app.rag.corpus import _INDEX_VERSION  # noqa: E402
from app.rag.ingest import normalize, ocr  # noqa: E402
from app.rag.ingest.chunker import build_chunks  # noqa: E402
from app.rag.ingest.loader import (  # noqa: E402
    _heading_predicate,
    _page_raw_lines,
    _needs_plain_text,
    discover_corpus_files,
    extract_blocks,
)
from app.rag.ingest.toc import build_tree  # noqa: E402

#: 本工具的输出格式版本。改了字段含义、判定阈值或统计口径就 +1 ——
#: 两份基线能否对比，先看这个号是否相同。
#
#: 变更记录：
#:   1  初版（meta + documents）
#:   2  meta 增加解析文件的 SHA-256 与汇总指纹，每份文档增加文件内容摘要
#:   3  meta 增加 git_repo 与 detected_git_root；git 仓库根与 ROOT 不一致时
#:      拒绝采用外层仓库的 HEAD／分支／脏标记（这三项的字段名与含义未变）
#:   4  解析文件清单加入 ocr.py；meta 增加 ocr 段（开关／依赖可用性／DPI／阈值）。
#:      M2 之后切片数取决于 OCR 能不能跑，不记下来两份基线就不可比
#:   5  硬断行候选改用 normalize 的共享判据（loader 合并用的是同一条）；
#:      解析文件清单加入 normalize.py
_TOOL_VERSION = "5"

#: 解析链路的源文件。逐份记 SHA-256 的原因：
#: 只记 HEAD 与 dirty 文件名，无法区分「同一 HEAD、同一批脏文件、内容却不同」
#: 的两次运行 —— 两次都 dirty，但 loader 已经在中间被改过，两份基线仍会被当成可比。
_TRACKED_PARSE_FILES = (
    "eval/tools/parse_health.py",
    "app/rag/ingest/loader.py",
    "app/rag/ingest/toc.py",
    "app/rag/ingest/chunker.py",
    "app/rag/ingest/ocr.py",
    "app/rag/ingest/normalize.py",
    "app/rag/schemas.py",
    "app/rag/corpus.py",
)

#: 判定「这一页没有文本层」的字数阈值。取 20 是经验值：
#: 正常渲染页动辄几百字，扫描页是 0。中间值极少出现，阈值不敏感。
_SCANNED_CHAR_THRESHOLD = 20


def _page_facts(path):
    """逐页取体检指标。纯文本没有页，返回空列表。"""
    if _needs_plain_text(path):
        return []
    facts = []
    doc = pymupdf.open(path)
    for page_no, page in enumerate(doc, start=1):
        text = page.get_text("text").strip()
        images = page.get_images(full=True)
        max_px = 0
        for img in images:
            info = doc.extract_image(img[0])
            max_px = max(max_px, max(info.get("width", 0), info.get("height", 0)))
        try:
            tables = len(page.find_tables().tables)
        except Exception:
            tables = -1  # 取不到就如实标 -1，不当作 0
        facts.append({
            "page": page_no,
            "chars": len(text),
            "images": len(images),
            "max_image_px": max_px,
            "tables": tables,
        })
    doc.close()
    return facts


def _broken_line_examples(path, limit=3):
    """合并前的硬断行候选，返回 (总数, 示例列表)。

    判据直接用 normalize.looks_like_continuation（loader 合并硬断行时用的是同一条），
    所以这里的数字就是「loader 将会合并多少处」，不会与生产行为漂移。
    在**原始行**（未合并）上统计，这样它能反映合并的力度；
    合并后仍有残留的另见 broken_line_pairs_residual。
    """
    is_heading = _heading_predicate()
    total = 0
    examples = []
    doc = pymupdf.open(path)
    for page in doc:
        items = _page_raw_lines(page)
        for i in range(len(items) - 1):
            if not normalize.should_merge(items[i], items[i + 1], is_heading=is_heading):
                continue
            total += 1
            if len(examples) < limit:
                examples.append(items[i][0].rstrip()[-10:] + " ⟂ " + items[i + 1][0].strip()[:10])
    doc.close()
    return total, examples


def inspect_one(path):
    """体检一份语料，返回一个纯 dict（可直接进 JSON）。"""
    doc_id = os.path.splitext(os.path.basename(path))[0]
    pages = _page_facts(path)
    text_chars = sum(p["chars"] for p in pages)
    image_count = sum(p["images"] for p in pages)
    table_count = sum(p["tables"] for p in pages if p["tables"] > 0)
    broken_total, broken_examples = _broken_line_examples(path)

    blocks = extract_blocks(path)
    sections = build_tree(path)
    chunks = build_chunks(blocks, sections, doc_id)

    degenerate = 0
    for c in chunks:
        for x0, y0, x1, y1 in c.bboxes:
            if x1 <= x0 or y1 <= y0:
                degenerate += 1
                break

    last_section = sections[-1].title if sections else ""
    all_in_last = (
        len(chunks) > 0
        and len(sections) > 1
        and all(c.section_path and c.section_path[-1] == last_section for c in chunks)
    )

    plain = _needs_plain_text(path)
    needs_ocr = (not plain) and text_chars < _SCANNED_CHAR_THRESHOLD * max(len(pages), 1)

    return {
        "doc_id": doc_id,
        "file": os.path.basename(path),
        "ext": os.path.splitext(path)[1].lower(),
        # 语料内容摘要。解析器指纹相同、这一项也相同，才能断定两份基线看的是同一批输入。
        "file_sha256": _sha256_file(path),
        "pages": len(pages),
        "text_layer_chars": text_chars,
        "image_count": image_count,
        "table_objects": table_count,
        "needs_ocr": needs_ocr,
        "broken_line_pairs": broken_total,
        "broken_line_examples": broken_examples,
        "blocks": len(blocks),
        "sections": len(sections),
        "chunks": len(chunks),
        "chunks_with_degenerate_bbox": degenerate,
        "empty_retrieval": len(chunks) == 0,
        "all_chunks_in_last_section": all_in_last,
        "first_chunk_section": chunks[0].section_path if chunks else None,
        "first_chunk_page": chunks[0].page if chunks else None,
    }


def _same_path(a, b):
    """判断两个路径是否指向同一个目录。Windows 下大小写不敏感，统一 normcase 再比。"""
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _git_info():
    """取当前 git 状态，供基线可比性使用。取不到就如实标 unknown。

    为什么必须记进基线 JSON：解析层代码一改，同样的语料会算出不同的
    切片数、不同的章节路径。只留统计数字的话，事后没人能证明两份 JSON
    是「同一份代码跑出来的」，M2/M3 的前后对比就失去依据。

    ⚠️ 必须先校验仓库根与 ROOT 是否同一个目录。git 会向上层目录找仓库，
    所以当本工具处在「属于另一个仓库的子目录」里时（例如把某次提交
    `git archive` 解到别人的仓库下面再运行），`git rev-parse` 报出来的是
    **外层仓库**的 HEAD 与脏文件列表。那样会拼出「外层仓库的 HEAD 配本目录
    代码指纹」这种自相矛盾的元数据，正是本工具要防的误标（实测踩过一次：
    输出写成 HEAD 9323788 配 parser 5804622，而后者属于修复前的代码）。
    此时一律标 `repo = "mismatch"`，拒绝采用外层的 HEAD／分支／脏标记，
    只把外层仓库根记进 detected_git_root 供诊断。
    """
    def _run(args):
        try:
            out = subprocess.run(
                args, cwd=str(ROOT), capture_output=True, text=True, timeout=15,
            )
        except Exception:
            return None
        if out.returncode != 0:
            return None
        return out.stdout

    def _unknown(repo, detected=None):
        return {
            "repo": repo,
            "head": "unknown",
            "branch": "unknown",
            "dirty": None,
            "dirty_files": [],
            "detected_git_root": detected,
        }

    toplevel = _run(["git", "rev-parse", "--show-toplevel"])
    if toplevel is None:
        # 没装 git，或当前目录不在任何仓库里
        return _unknown("unknown")

    detected = Path(toplevel.strip())
    if not _same_path(detected, ROOT):
        return _unknown("mismatch", str(detected))

    head = _run(["git", "rev-parse", "HEAD"])
    if head is None:
        return _unknown("broken", str(detected))
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    status = _run(["git", "status", "--porcelain"])
    files = []
    if status is not None:
        for line in status.splitlines():
            if line.strip():
                files.append(line[3:].strip())
    return {
        "repo": "ok",
        "head": head.strip(),
        "branch": (branch or "").strip() or "unknown",
        "dirty": bool(files) if status is not None else None,
        "dirty_files": files,
        "detected_git_root": str(detected),
    }


def _sha256_file(path, block_size=1 << 20):
    """算一个文件的 SHA-256（十六进制）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(block_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _parser_digests():
    """解析链路各源文件的当前 SHA-256。文件缺失就如实标 missing。"""
    out = {}
    for rel in _TRACKED_PARSE_FILES:
        full = ROOT / rel
        out[rel] = _sha256_file(full) if full.is_file() else "missing"
    return out


def _fingerprint(mapping):
    """把「文件 -> 摘要」压成一个指纹，方便人眼比对两份基线是否同一份代码。

    键按字典序排，用 \\x00 分隔，避免 ("ab","c") 与 ("a","bc") 撞到同一个值。
    """
    h = hashlib.sha256()
    for key in sorted(mapping):
        h.update(key.encode("utf-8"))
        h.update(b"\x00")
        h.update(str(mapping[key]).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def collect_meta():
    """采集这次基线是在什么代码状态下生成的。"""
    git = _git_info()
    digests = _parser_digests()
    return {
        "tool": "eval/tools/parse_health.py",
        "tool_version": _TOOL_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        # git_repo = ok / mismatch / unknown / broken。
        # 只有 ok 时下面三项才可信；mismatch 说明 git 报的是外层仓库的状态。
        "git_repo": git["repo"],
        "git_head": git["head"],
        "git_branch": git["branch"],
        "git_dirty": git["dirty"],
        "git_dirty_files": git["dirty_files"],
        "detected_git_root": git["detected_git_root"],
        "parser_files_sha256": digests,
        "parser_fingerprint": _fingerprint(digests)[:16],
        "index_version": _INDEX_VERSION,
        # OCR 配置与可用性（M2 起切片数受它影响，见 _TOOL_VERSION 变更记录）
        "ocr": ocr.config_snapshot(),
        "threshold_scanned_chars": _SCANNED_CHAR_THRESHOLD,
    }


def build_payload(rows):
    """组装落盘的基线 JSON。meta 与数据分开，方便人先看 meta 判断可比性。

    可比性判据（五条全部满足才算可比）：
      git_repo == "ok"              git 元数据确实属于本目录，不是外层仓库的
      parser_fingerprint            解析器代码一致
      index_version                 入库结构版本一致
      meta.ocr 的 enabled/available  OCR 能不能跑一致（扫描件的切片数取决于它）
      documents[].file_sha256       逐份语料内容一致
    """
    return {"meta": collect_meta(), "documents": rows}


def _meta_summary_line(meta):
    """一行摘要，打在终端上。

    repo 不是 ok 时，HEAD／分支／脏标记一律不可信，所以打印时要显眼地说明，
    不能让人误把外层仓库的 HEAD 当成这份基线的代码版本。
    """
    repo = meta.get("git_repo")
    if repo == "mismatch":
        head_text = "HEAD n/a（repo=mismatch，git 指向 " + str(meta["detected_git_root"]) + "）"
    elif repo == "ok":
        dirty = meta["git_dirty"]
        if dirty is None:
            dirty_text = "dirty=unknown"
        elif dirty:
            dirty_text = "dirty(" + str(len(meta["git_dirty_files"])) + " 个文件)"
        else:
            dirty_text = "clean"
        head_text = "HEAD " + str(meta["git_head"])[:7] + " " + dirty_text
    else:
        # unknown（无 git）／broken（有 git 但取不到 HEAD）
        head_text = "HEAD n/a（repo=" + str(repo) + "）"
    return head_text + (
        " | parser " + str(meta["parser_fingerprint"])
    ) + " | _INDEX_VERSION=" + str(
        meta["index_version"]
    ) + " | tool v" + str(meta["tool_version"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="语料解析体检（只读）")
    parser.add_argument("paths", nargs="*", help="要体检的语料，缺省则动态扫 data/corpus/raw/")
    parser.add_argument("--out", default=None, help="把完整结果写成 JSON 到这里")
    args = parser.parse_args(argv)

    if args.paths:
        paths = list(args.paths)
    else:
        paths, ignored = discover_corpus_files()
        if ignored:
            print("被跳过的文件（后缀不在支持范围）:", "、".join(ignored))
    if not paths:
        print("没找到语料。默认扫 data/corpus/raw/，或运行时传入路径")
        return 1

    rows = []
    for path in paths:
        if not os.path.exists(path):
            print("找不到文件:", path)
            continue
        rows.append(inspect_one(path))

    header = (
        "文档".ljust(12) + "页".rjust(3) + "文本层字".rjust(9)
        + "图".rjust(4) + "表格".rjust(5) + "OCR".rjust(5)
        + "折断".rjust(5) + "块".rjust(5) + "章".rjust(4) + "切片".rjust(5)
        + "退化框".rjust(7) + " 章节异常"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        flag = ""
        if r["empty_retrieval"]:
            flag = " 0切片!必失"
        elif r["all_chunks_in_last_section"]:
            flag = " 切片全落最后一章"
        print(
            r["doc_id"][:12].ljust(12)
            + str(r["pages"]).rjust(3)
            + str(r["text_layer_chars"]).rjust(9)
            + str(r["image_count"]).rjust(4)
            + str(r["table_objects"]).rjust(5)
            + ("是" if r["needs_ocr"] else "否").rjust(5)
            + str(r["broken_line_pairs"]).rjust(5)
            + str(r["blocks"]).rjust(5)
            + str(r["sections"]).rjust(4)
            + str(r["chunks"]).rjust(5)
            + str(r["chunks_with_degenerate_bbox"]).rjust(7)
            + flag
        )

    print()
    print("汇总")
    print("  文档", len(rows), "｜总页数", sum(r["pages"] for r in rows))
    print("  需要 OCR：", ", ".join(r["doc_id"] for r in rows if r["needs_ocr"]) or "无")
    print("  0 切片（检索必失）：", ", ".join(r["doc_id"] for r in rows if r["empty_retrieval"]) or "无")
    print("  表格对象：", sum(r["table_objects"] for r in rows), "个")
    print("  硬断行（loader 会拼回来的接缝）：",
          sum(r["broken_line_pairs"] for r in rows), "处")
    for r in rows:
        if r["broken_line_examples"]:
            print("    ", r["doc_id"], " → ", " ; ".join(r["broken_line_examples"]))
    print("  生成状态：", _meta_summary_line(collect_meta()))
    ocr_info = ocr.config_snapshot()
    if ocr_info["available"] and ocr_info["enabled"]:
        print("  OCR：可用（%d DPI，页级阈值 %d 字符）"
              % (ocr_info["dpi"], ocr_info["min_text_chars"]))
    else:
        print("  OCR：不可用 ->", ocr_info["reason"],
              "｜下方标 0 切片的扫描件在装上依赖后才会进检索池")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = build_payload(rows)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写出:", out_path, "｜", _meta_summary_line(payload["meta"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
