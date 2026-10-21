"""把政采向量库的抽样清单转成路由可用的语料清单。

为什么需要这一步：
    `app/routing_signals.corpus_terms()` 从"语料清单里的文档命名与分类"派生
    文档侧实体词，靠它判断一个问题是不是要查文档。政采的 224 份文档（24 份
    法规 + 200 份公告）已经向量化进 askdata_gov，但它们的清单是向量化脚本
    自己的格式（regulation/announcement 两个数组、法规标题是清洗残留的
    「【颁布单位】」），`corpus_terms()` 读不到。

    不补这一步的后果（实测）：政采跨源 15 题里没有任何一题能触发跨源路由，
    因为文档侧一个实体词都提取不出来，全部被当成纯问数题，只生成 SQL、
    一个条文都不查。

输出：agent_temp/pb/probe_ccgp/gov_corpus_manifest.json
    形如 {"documents": [{"file": "...", "title": "...", "category": "..."}]}
    配合环境变量 RAG_CORPUS_DIR=agent_temp/pb/probe_ccgp/cleaned_v3 与
    RAG_CORPUS_MANIFEST_PATH=<本文件> 使用。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "agent_temp" / "pb" / "probe_ccgp"

# 法规标题的正则兜底：清单里缺失真实标题时，从正文首个「第X号」行或文件名推。
_LAW_TITLE_RE = re.compile(r"《([^》]{4,40})》")


def _regulation_title(entry: dict, text: str) -> str:
    title = str(entry.get("title") or "").strip()
    # regulation_index.json 的 title 就是网页标题（含书名号），是真实法规名。
    match = _LAW_TITLE_RE.search(title)
    if match:
        return match.group(1).strip()
    if title and "【" not in title and len(title) >= 4:
        return title
    # 兜底：正文里第一个书名号短语
    match = _LAW_TITLE_RE.search(text[:2000])
    if match:
        return match.group(1).strip()
    return Path(str(entry.get("doc_id") or "")).stem


def main() -> None:
    vec = json.loads((PROBE / "vectorize_manifest.json").read_text(encoding="utf-8"))
    index = json.loads((PROBE / "regulation_index.json").read_text(encoding="utf-8"))
    by_url = {str(item.get("url", "")).strip(): item for item in index}

    documents: list[dict] = []
    for entry in vec.get("regulation", []):
        doc_id = str(entry.get("doc_id") or "")
        local = ROOT / str(entry.get("local_path") or "")
        text = local.read_text(encoding="utf-8", errors="ignore") if local.is_file() else ""
        # vectorize_manifest 没存 url，按 doc_id 回查 regulation_index 的 local_path 尾名
        matched = None
        for item in by_url.values():
            if Path(str(item.get("local_path") or "")).stem.replace(".txt", "") in doc_id:
                matched = item
                break
        title = _regulation_title(matched or entry, text)
        documents.append({
            "file": doc_id + ".txt",
            "title": title,
            "category": "政府采购法规",
        })

    for entry in vec.get("announcement", []):
        title = str(entry.get("title") or "").strip()
        kind = str(entry.get("kind") or "").strip()
        documents.append({
            "file": str(entry.get("doc_id") or "") + ".txt",
            "title": title,
            "category": "政府采购公告" + (("·" + kind) if kind else ""),
        })

    out = PROBE / "gov_corpus_manifest.json"
    out.write_text(
        json.dumps({"documents": documents}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"写出 {out}：法规 {len(vec.get('regulation', []))} 份 + 公告 {len(vec.get('announcement', []))} 份")
    for doc in documents[:6]:
        print("  ", doc["category"], "|", doc["title"][:50])


if __name__ == "__main__":
    main()
