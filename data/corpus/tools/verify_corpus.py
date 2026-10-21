#!/usr/bin/env python3
"""校验知识库语料是否满足任务书的硬要求。

对应两处要求：
  1. 赛题提交要求「知识库文档样本（不少于 10 份，涵盖不同类型文档格式）」
  2. C 任务书验收标准「≥10 份样本到位且分类标注（含脏目录样本与扫描件）」

为什么要有这个脚本：这些指标过去只写在 data/corpus/README.md 的正文里，
是「人看过一眼觉得对」。但份数、脏样本数、扫描件数、哈希一致性这些
都是**确定性**的，能一键复跑就不该靠人眼。评委或者队友怀疑数量时，
跑一条命令出结论，比翻文档吵赢更省事。

用法：
    python data/corpus/tools/verify_corpus.py
    python data/corpus/tools/verify_corpus.py --json data/corpus/reports/last.json

退出码：0 全通过；1 有失败项；2 前置条件缺失（清单或目录不存在）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parents[1]
MANIFEST = CORPUS_DIR / "manifest.json"
RAW_DIR = CORPUS_DIR / "raw"

# ---- 硬要求常量。改这里就是改标准，不要散落在检查逻辑里 ----
MIN_DOCS = 10           # 不少于 10 份
MIN_FORMATS = 2         # 涵盖不同类型文档格式
MIN_DIRTY_OUTLINE = 2   # ≥2 份目录层级混乱的
MIN_SCANNED = 1         # 1-2 份扫描件
MAX_SCANNED = 2

DIRTY_MARK = "目录混乱"
SCANNED_MARK = "扫描件"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append(Check(name, bool(passed), detail))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="校验 data/corpus 是否满足任务书硬要求")
    ap.add_argument("--json", type=Path, default=None, help="把报告写成 JSON 便于存档")
    args = ap.parse_args()

    if not MANIFEST.exists():
        print(f"找不到清单：{MANIFEST}")
        return 2
    if not RAW_DIR.is_dir():
        print(f"找不到语料目录：{RAW_DIR}")
        print("提示：raw/ 是生成产物，先跑 python data/corpus/tools/build_corpus.py")
        return 2

    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"清单不是合法 JSON：{e}")
        return 2

    docs = manifest.get("documents") or []
    r = Report()

    # ---- 1. 份数 ----
    r.add(
        f"文档份数 ≥ {MIN_DOCS}",
        len(docs) >= MIN_DOCS,
        f"期望 ≥{MIN_DOCS}，实测 {len(docs)}",
    )
    r.add(
        "清单 count 字段与 documents 条数一致",
        manifest.get("count") == len(docs),
        f"count={manifest.get('count')}，documents={len(docs)}",
    )

    # ---- 2. 格式分布 ----
    formats = sorted({d.get("format") for d in docs if d.get("format")})
    r.add(
        f"文档格式种类 ≥ {MIN_FORMATS}",
        len(formats) >= MIN_FORMATS,
        f"实测 {len(formats)} 种：{formats}",
    )

    # ---- 3. 脏目录样本 ----
    dirty = [d["id"] for d in docs if DIRTY_MARK in (d.get("file") or "")]
    r.add(
        f"目录混乱样本 ≥ {MIN_DIRTY_OUTLINE}",
        len(dirty) >= MIN_DIRTY_OUTLINE,
        f"实测 {len(dirty)} 份：{dirty}",
    )
    r.add(
        "脏目录样本的 outline_ok 标为 false",
        all(not d.get("outline_ok") for d in docs if d["id"] in dirty),
        "标记与文件特征自洽"
        if all(not d.get("outline_ok") for d in docs if d["id"] in dirty)
        else "有脏目录样本被标成 outline_ok=true",
    )

    # ---- 4. 扫描件 ----
    scanned = [d["id"] for d in docs if SCANNED_MARK in (d.get("file") or "")]
    sc_ok = MIN_SCANNED <= len(scanned) <= MAX_SCANNED
    r.add(
        f"扫描件 {MIN_SCANNED}–{MAX_SCANNED} 份",
        sc_ok,
        f"实测 {len(scanned)} 份：{scanned}",
    )
    no_text = [d["id"] for d in docs if not d.get("has_text_layer")]
    r.add(
        "扫描件与 has_text_layer=false 完全对应",
        sorted(scanned) == sorted(no_text),
        f"扫描件 {sorted(scanned)}；无文本层 {sorted(no_text)}",
    )

    # ---- 5. 分类标注 ----
    unlabeled = [d["id"] for d in docs if not (d.get("category") or "").strip()]
    r.add(
        "每份都有分类标注",
        not unlabeled,
        "全部有 category" if not unlabeled else f"缺分类：{unlabeled}",
    )
    cats = sorted({d.get("category") for d in docs if d.get("category")})
    r.add("分类个数 ≥ 2", len(cats) >= 2, f"实测 {len(cats)} 类：{cats}")

    # ---- 6. ID 唯一且连续 ----
    ids = [d.get("id") for d in docs]
    r.add("ID 唯一", len(set(ids)) == len(ids), f"{len(set(ids))}/{len(ids)} 个唯一")

    # ---- 7. 清单与磁盘一致 ----
    on_disk = {p.name for p in RAW_DIR.iterdir() if p.is_file() and p.name != ".gitkeep"}
    listed = {d.get("file") for d in docs}
    only_list = sorted(listed - on_disk)
    only_disk = sorted(on_disk - listed)
    r.add(
        "磁盘文件与清单一一对应",
        not only_list and not only_disk,
        "完全对应"
        if not only_list and not only_disk
        else f"清单有磁盘无：{only_list}；磁盘有清单无：{only_disk}",
    )

    # ---- 8. 逐份字节数与哈希 ----
    size_bad, hash_bad, missing = [], [], []
    for d in docs:
        p = RAW_DIR / (d.get("file") or "")
        if not p.exists():
            missing.append(d.get("id"))
            continue
        if d.get("bytes") is not None and p.stat().st_size != d["bytes"]:
            size_bad.append(f"{d['id']}(清单{d['bytes']}/实际{p.stat().st_size})")
        exp = d.get("file_sha256_local")
        if exp and sha256_file(p) != exp:
            hash_bad.append(d["id"])
    r.add("逐份字节数与清单一致", not size_bad, "全部一致" if not size_bad else str(size_bad))
    r.add(
        "逐份字节哈希与清单一致",
        not hash_bad and not missing,
        "全部一致"
        if not hash_bad and not missing
        else f"哈希不符：{hash_bad}；文件缺失：{missing}",
    )
    r.add(
        "每份都有内容指纹 content_sha256",
        all(d.get("content_sha256") for d in docs),
        "全部有"
        if all(d.get("content_sha256") for d in docs)
        else "有文档缺内容指纹，跨机器复现时无法判定内容是否被改",
    )

    # ---- 9. 每份都有说明 ----
    no_notes = [d["id"] for d in docs if not (d.get("notes") or "").strip()]
    r.add("每份都有 notes 说明", not no_notes, "全部有" if not no_notes else f"缺说明：{no_notes}")

    # ---- 输出 ----
    width = 74
    print("=" * width)
    print("  data/corpus 硬要求校验")
    print("=" * width)
    for c in r.checks:
        print(("PASS " if c.passed else "FAIL ") + c.name)
        print("       " + c.detail)
    print("-" * width)
    total_bytes = sum((RAW_DIR / d["file"]).stat().st_size for d in docs if (RAW_DIR / d["file"]).exists())
    print(f"语料总体积：{total_bytes:,} 字节（{len(docs)} 份）")
    print(f"通过 {len(r.checks) - len(r.failed)} 项，失败 {len(r.failed)} 项")
    print("-" * width)
    if r.failed:
        print("校验未通过：")
        for c in r.failed:
            print(f"  - {c.name}：{c.detail}")
    else:
        print("校验通过：语料满足任务书硬要求。")

    r.facts = {
        "documents": len(docs),
        "formats": formats,
        "categories": cats,
        "dirty_outline": dirty,
        "scanned": scanned,
        "total_bytes": total_bytes,
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {"passed": not r.failed, "facts": r.facts, "checks": [asdict(c) for c in r.checks]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"报告已写入：{args.json}")

    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
