"""阶段 3：把政采语料索引进 askdata_gov 库的向量表。

设计要点（2026-10-17）：
    表名固定（chunk_vectors / doc_meta），隔离完全靠**数据库**——
    本脚本读 manifest 里的 local_path，但写入目标由 RAG_DSN 或 --dsn 决定，
    绝不去动 chinook 库里那套基础语料索引。

    为什么不走 app/rag/corpus.py 的 index_corpus()：
    那个函数按目录扫文件，而我们的语料分在两处（laws_regulation/*.htm.txt 与
    detail_v3/*.txt），且**文件名不是可读标题**（t20140103_4650784.htm.txt）。
    标题在 manifest 里，需要写进 doc_meta 的 doc_title 供前端溯源展示。
    因此这里直接复用 PgVectorStore + 分块 + bge-m3，逐份写入，写完后自己
    维护 doc_meta（格式与 corpus.py 一致：doc_id / fingerprint / chunk_count）。

    fingerprint 口径与 corpus.content_fingerprint 保持一致的含义：内容 sha256，
    含索引格式版本号。这样将来若改用 index_corpus() 续接，指纹能对上。

用法：
    python infra/scripts/seed_gov_vectors.py --dry-run     # 只报计划，不动库
    python infra/scripts/seed_gov_vectors.py               # 全量写入
    python infra/scripts/seed_gov_vectors.py --limit 5     # 先试 5 份
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

WS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WS))

MANIFEST = WS / "agent_temp" / "pb" / "probe_ccgp" / "vectorize_manifest.json"
DEFAULT_DSN = "postgresql://ict:ict_dev_only_change_me@127.0.0.1:5432/askdata_gov"

# 索引格式版本：与 app/rag/corpus.py 的同一常量口径保持一致。
# 分块规则或向量模型变了，就要升这个号，否则旧指纹会被误判成"没变、可跳过"。
#
# v2（2026-10-17）：法规切块增加「剥元数据头 + section_path 只挂章」两处改动。
#   改之前 section_path 全是「【颁布单位】」或「第87号」，前端溯源无意义；
#   手工 TRUNCATE chunk_vectors 重跑时还发现 doc_meta 没清、全部被跳过 ——
#   正是这条注释警告过的那种不一致。升版后指纹自然变化，重跑即重建。
INDEX_FORMAT_VERSION = "v2"


def content_fingerprint(text: str, title: str, doc_type: str = "") -> str:
    """内容指纹：把切块方式（doc_type）也算进去。

    只哈希正文会让「换了切块规则但正文没变」被判成"没变、可跳过"，
    库里就会留着旧切法的切片而 doc_meta 说一切正常 —— 这种不一致最难查。
    """
    digest = hashlib.sha256()
    digest.update(INDEX_FORMAT_VERSION.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(doc_type.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(title.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()


def chunk_regulation(text: str, *, max_chars: int = 900) -> list[tuple[list[str], str]]:
    """法条按「条」切，并把所属「章」记进 section_path。

    为什么不能沿用公告的空行切块：法规正文每一条之间正好隔一个空行，
    按空行切会把一条完整的法条拆成"条号"与"条文"两片，
    实测规则层平均只有 119 字/片 —— 检索时召回「第四条」却缺上文，
    生成环节就只能靠猜。法条是天然的语义单元，**一条一片**才对。

    返回 [(section_path, chunk_text)]。
    """
    import re

    lines = [line.strip() for line in text.splitlines()]

    # 先剥掉法规页首部的元数据块，再切条。
    #
    # 清洗后的法规正文长这样：
    #     【颁布单位】 / 3 / 【文 号】 / 财政部令第87号 / （空行）/ 【颁布日期】 / 2017-07-11 / ...
    # 标签行与它的值行是分开的两行。只在循环里 skip 标签行，值行（"3"、"财政部令第87号"）
    # 仍会落进 title_line，于是全部法条的 section_path 都显示成元数据值。
    # 正确的做法是整块剥掉：连续出现「标签行 + 可选值行 + 空行」直到遇到正文。
    cut = 0
    while cut < len(lines):
        candidate = lines[cut]
        if not candidate:
            cut += 1
            continue
        if not re.match(r"^【[^】]*】$", candidate):
            break
        # 标签行之后、下一个标签行或第一个非空非标签行之前，最多吞一条值行
        cut += 1
        if cut < len(lines) and lines[cut] and not re.match(r"^【[^】]*】$", lines[cut]):
            cut += 1
    lines = lines[cut:]

    # 元数据块之后还常残留两行纯编号（部令编号在前言前又重复一遍），
    # 如「财政部令第87号」「第87号」。切条循环里的 `len(line) <= 40` 会把它们
    # 当成前言标题，section_path 就变成「第87号」。这里一次性剥掉。
    while lines and (not lines[0] or re.match(r"^(?:财政部令)?第\s*[\d.]+号$", lines[0])):
        lines.pop(0)

    chapter = ""          # 当前章，如「第二章 采购方式」
    title_line = ""       # 标题行（第一条之前的内容归到「前言」）
    pieces: list[tuple[list[str], str]] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        body = "\n".join(buffer).strip()
        buffer.clear()
        if len(body) < 20:
            return
        # section 直接给「章」就够了。标题行（title_line）只在前言段有用，
        # 挂到每一条上就是噪音 —— 前端溯源显示「第一章 总则 / 前言 / 第一条」
        # 里那个「前言」纯属干扰。有章时只用章，无章的前言段才落「前言」。
        if chapter:
            section = [chapter]
        else:
            section = [title_line or "前言"]        # 过长的条（个别附则/罚则列表）再按 max_chars 切，但保留章条上下文
        if len(body) <= max_chars:
            pieces.append((section, body))
            return
        cursor = 0
        while cursor < len(body):
            pieces.append((section, body[cursor: cursor + max_chars]))
            cursor += max_chars

    for line in lines:
        if not line:
            continue
        if re.match(r"^第[一二三四五六七八九十百]+章", line):
            flush()
            chapter = line
            continue
        if re.match(r"^第[一二三四五六七八九十百]+条", line):
            flush()
            buffer.append(line)
            continue
        if re.match(r"^附\s*则", line) or re.match(r"^第[一二三四五六七八九十百]+节", line):
            flush()
            chapter = line
            continue
        if not chapter and not buffer and len(line) <= 40:
            # 第一条之前、长度像标题的行，作为前言标题。
            #
            # 必须排除【颁布单位】这类元数据标签：清洗后的法规正文第一行就是
            # 「【颁布单位】」，初版把它当标题，导致全部 784 个法条切片的
            # section_path 都显示成「【颁布单位】」，前端溯源完全失去意义。
            if re.match(r"^【[^】]*】$", line):
                continue
            title_line = line
            continue
        buffer.append(line)
    flush()

    if not pieces:
        # 兜底：不是标准法条格式的页面（通知类），退回按空行切
        return [([], piece) for piece in chunk_text(text, max_chars=max_chars)]
    return pieces


def chunk_announcement(text: str, *, max_chars: int = 700) -> list[tuple[list[str], str]]:
    """公告按空行分段，标题作为 section_path。"""
    return [([], piece) for piece in chunk_text(text, max_chars=max_chars)]


def chunk_text(text: str, *, max_chars: int = 700) -> list[str]:
    """按空行优先、句子次之切块。

    政采公告的正文是纯文本，段落之间有空行。
    先按空行分段，再把过长的段按句号/分号继续切，保证每块不超过 max_chars。
    """
    import re

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for para in paragraphs:
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        sentences = re.split(r"(?<=[。；！？])", para)
        buffer = ""
        for sentence in sentences:
            if len(buffer) + len(sentence) <= max_chars:
                buffer += sentence
            else:
                if buffer:
                    pieces.append(buffer)
                buffer = sentence if len(sentence) <= max_chars else sentence[:max_chars]
        if buffer:
            pieces.append(buffer)

    chunks: list[str] = []
    for piece in pieces:
        if chunks and len(piece) < 40:
            # 太短的片段（目录行、页码）粘到上一块，避免产生无信息切片
            chunks[-1] = chunks[-1] + "\n" + piece
        else:
            chunks.append(piece)
    return chunks or ([text[:max_chars]] if text.strip() else [])


def main() -> None:
    parser = argparse.ArgumentParser(description="把政采语料写入 askdata_gov 的向量表")
    parser.add_argument("--dsn", default=os.environ.get("PGVECTOR_DSN") or DEFAULT_DSN)
    parser.add_argument("--dry-run", action="store_true", help="只报计划，不写库")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 份（试跑用）")
    parser.add_argument("--group", choices=["regulation", "announcement", "all"], default="all")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    docs: list[dict] = []
    if args.group in ("regulation", "all"):
        docs.extend(manifest["regulation"])
    if args.group in ("announcement", "all"):
        docs.extend(manifest["announcement"])
    if args.limit:
        # 试跑时两组各取一部分，保证两类都覆盖
        reg = [d for d in docs if d["doc_type"] == "regulation"][: max(1, args.limit // 3)]
        ann = [d for d in docs if d["doc_type"] == "announcement"][: args.limit - len(reg)]
        docs = reg + ann

    print(f"待索引 {len(docs)} 份 → {args.dsn.rsplit('/', 1)[-1]}")
    if args.dry_run:
        total_chars = 0
        for doc in docs:
            path = WS / doc["local_path"]
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            total_chars += len(text)
        print(f"正文字符合计 {total_chars:,}")
        print("dry-run，未写库")
        return

    from app.rag.retrieval.indexer import BgeEmbedder
    from app.rag.retrieval.pgvector_store import PgVectorStore
    from app.rag.schemas import Chunk

    # 延迟导入：bge-m3 加载要十几秒，dry-run 不该付这个代价。
    # 用 indexer.BgeEmbedder（生产实现），接口是 embed_many(texts) -> list[list[float]]；
    # 注意不是 embed(单个) 批量循环，那样会逐条付推理开销。
    embedder = BgeEmbedder()
    store = PgVectorStore(args.dsn, embedder.dim)
    store.init_schema()  # 幂等：建表 + ALTER ADD COLUMN IF NOT EXISTS

    # 已索引的指纹一次性读进来（load_meta 返回 {doc_id: (fingerprint, chunk_count)}），
    # 不要每份文档查一次库。
    meta = store.load_meta()

    indexed, skipped, failed = 0, 0, []
    total_chunks = 0
    started = time.perf_counter()

    # ---------- 第一遍：全部读完、切块、过指纹，攒成一个待编码清单 ----------
    #
    # 为什么分两遍：初版是「每份文档各调一次 embed_many」，3 份要 92 秒，
    # 224 份预计两小时以上 —— 时间全花在每批只喂 3~40 条文本、
    # GPU/MPS 每次都要重新填充流水线上。把 224 份的切片合并成一个大清单后
    # 一次性交给 embed_many（内部 batch_size=32 自动分批），
    # 同样多的切片只要几分钟。
    pending: list[tuple[dict, str, str, list[tuple[list[str], str]], str]] = []
    for doc in docs:
        doc_id = doc["doc_id"]
        path = WS / doc["local_path"]
        if not path.is_file():
            failed.append((doc_id, "本地文件不存在"))
            continue
        text = path.read_text(encoding="utf-8").strip()
        if len(text) < 50:
            failed.append((doc_id, f"正文过短（{len(text)} 字）"))
            continue

        title = doc.get("title") or doc_id
        # 指纹要把切块方式算进去：改了切块规则但正文没变时，
        # 单看正文 sha256 会误判"没变、可跳过"，库里留下旧切法的切片。
        fingerprint = content_fingerprint(text, title, doc["doc_type"])

        # 指纹没变就跳过 —— 与 corpus.py 的增量口径一致
        existing = meta.get(doc_id)
        if existing and existing[0] == fingerprint:
            skipped += 1
            continue

        # 法规与公告用不同切块器：法条按「条」切，公告按空行切（见各函数 docstring）
        if doc["doc_type"] == "regulation":
            pieces = chunk_regulation(text)
        else:
            pieces = chunk_announcement(text)
        if not pieces:
            failed.append((doc_id, "切不出切片"))
            continue
        pending.append((doc, doc_id, title, pieces, fingerprint))

    total_to_encode = sum(len(item[3]) for item in pending)
    print(f"待编码 {len(pending)} 份 / {total_to_encode} 片（跳过 {skipped} 份未变）")

    # ---------- 第二遍：一次性编码（内部分批），再逐份写库 ----------
    all_texts: list[str] = []
    for _doc, _doc_id, title, pieces, _fp in pending:
        for section, body in pieces:
            # 把章节路径拼进待编码文本，让"哪一章哪一条"参与向量语义
            prefix = " / ".join(section) if section else ""
            all_texts.append((title + ("｜" + prefix if prefix else "") + "\n" + body))

    vectors: list[list[float]] = []
    if all_texts:
        # batch_size 取小值：本机是 8GB 笔记本显卡，bge-m3 单条约 2.2GB，
        # 一次喂 32 条会额外吃掉 1GB 以上激活内存，实测在与其他程序共存时
        # 会 `torch.OutOfMemoryError: Tried to allocate 932.00 MiB`。
        # 16 条一批慢不了多少（瓶颈在写库与分块），但显存峰值减半。
        #
        # 再兜一层：真 OOM 就退到 CPU 重跑，宁可慢，不要留下"索引到一半"的库。
        try:
            vectors = embedder.embed_many(all_texts, batch_size=16)
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower():
                raise
            # 先释放已占着的显存再换 CPU，否则旧模型仍挂在 GPU 上白占 2GB
            print(f"  [!] GPU 显存不足，改用 CPU 重新编码（{len(all_texts)} 片，会慢几分钟）")
            del embedder
            import gc as _gc

            _gc.collect()
            try:
                import torch as _torch

                _torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001 - 没有 CUDA 时本就不需要清理
                pass
            os.environ["CUDA_VISIBLE_DEVICES"] = ""
            embedder = BgeEmbedder()
            vectors = embedder.embed_many(all_texts, batch_size=8)
    cursor = 0

    for position, (doc, doc_id, title, pieces, fingerprint) in enumerate(pending, 1):
        chunk_vectors = vectors[cursor: cursor + len(pieces)]
        cursor += len(pieces)

        # 重索引：先清掉这份文档的旧切片，避免旧切法留下的孤儿行
        store.delete_by_doc(doc_id)
        for chunk_idx, ((section, piece), vector) in enumerate(zip(pieces, chunk_vectors)):
            store.add(
                Chunk(
                    doc_id=doc_id,
                    text=piece,
                    # page 必须 ≥ 1：app/contracts.py 的 DocRef.page 带 ge=1 约束，
                    # 检索时 app/rag/retrieval/indexer.py:204 会用它构造 DocRef，
                    # 填 0 会让校验抛 ValidationError 把整条检索链路打崩
                    # （2026-10-17 实测：政采 224 份文档全库 page=0，跨源题一查文档就崩）。
                    # 公告与法规都是从 HTML 页面洗出的纯文本、没有分页概念，
                    # 统一记第 1 页，而不是留空或填 0。
                    page=1,
                    # section_path 是 list[str]：法条是 [章, 条/前言]，公告是 [标题]
                    section_path=section or [title],
                    char_start=0,
                    char_end=len(piece),
                    bboxes=[],
                    page_width=None,
                    page_height=None,
                    doc_title=title,
                    # 公告正文是从 HTML 页面洗出来的纯文本，法规同理，
                    # 两者都不是 OCR 也不是 PDF 文本层，归到 'plain'
                    extraction_method="plain",
                ),
                vector,
                idx=chunk_idx,
            )
        store.save_meta(doc_id, fingerprint, len(pieces))
        meta[doc_id] = (fingerprint, len(pieces))
        indexed += 1
        total_chunks += len(pieces)

        if position % 40 == 0 or position == len(pending):
            elapsed = time.perf_counter() - started
            print(f"  [{position}/{len(pending)}] 已写 {indexed} 份 / {total_chunks} 片，"
                  f"失败 {len(failed)}，用时 {elapsed:.0f}s")

    store.commit()

    elapsed = time.perf_counter() - started
    print()
    print("=== 索引结果 ===")
    print(f"  写入      {indexed} 份")
    print(f"  跳过（未变）{skipped} 份")
    print(f"  失败      {len(failed)} 份")
    print(f"  切片合计  {total_chunks}")
    print(f"  用时      {elapsed:.1f}s")
    for doc_id, reason in failed[:10]:
        print(f"    ✗ {doc_id}: {reason}")

    counts = store.count_chunks_by_doc()
    print(f"  库内文档 {len(counts)} 份 / 切片 {sum(counts.values())} 片")


if __name__ == "__main__":
    main()
