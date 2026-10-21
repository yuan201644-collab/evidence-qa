#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规模语料复现校验 —— 核对 10 / 100 / 500 页三档产物的可复现性。

## 它回答什么问题

`docs/RAG规模效率实测_20261003.md` 的效率结论引用了三档规模 PDF 的 SHA-256。
别人（或评审）拿到仓库之后，要确认「这三份 PDF 没被换过、报告里的哈希对得上」——
过去只能人眼逐位比对，本脚本把这件事变成一条命令。

## 两种哈希的分工（关键，别混用）

| 字段 | 是什么 | 什么时候稳定 |
|---|---|---|
| `file_sha256_local` | 生成出的 PDF **字节**哈希 | **对仓库里现有的这份文件稳定**（git 保证字节不变）；但**重跑生成器不稳定** —— PDF 写入器（PyMuPDF）会在文件里留 `/ID`、对象编号等非确定成分，**实测同机重跑都得到不同哈希**（大小、页数相同，哈希不同）。 |
| `content_sha256` | **章节数据**的内容指纹 | **重跑生成器也稳定**，跨机器、跨 PyMuPDF 版本。 |

→ 要核对「手上这份文件是不是报告里测的那一份」：比 `file_sha256_local`（本脚本 ② 组）。
→ 要核对「重跑生成器能不能得到同一份文档」：比 `content_sha256`（本脚本 ③ 组）。

两者对应两种正当的复现方式，**不矛盾**：
- **复现方式 A（推荐，最省事）**：`git clone` 后对 `data/corpus/scale/*.pdf` 直接算字节哈希 →
  与报告/清单的 `file_sha256_local` 比对。只要仓库里的文件没被改，必然一致。
- **复现方式 B（从源头重造）**：跑 `build_scale_corpus.py` 重新生成 → 用 `content_sha256` 比对，
  证明「同一份 sections 得到同一份文档」。字节哈希此时不可比（上表已说明原因）。

## 用法

    python data/corpus/tools/verify_scale_corpus.py
    python data/corpus/tools/verify_scale_corpus.py --json data/corpus/reports/scale_verify.json

退出码：0 全通过；1 有失败项；2 前置条件缺失（清单或目录不存在）。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Windows 控制台默认不是 UTF-8，不改会让中文/✓ 报 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 —— 改不了就算了，无非是花屏
        pass

_HERE = Path(__file__).resolve().parent
CORPUS_DIR = _HERE.parent          # data/corpus
SCALE_DIR = CORPUS_DIR / "scale"
MANIFEST_NAME = "manifest_scale.json"

#: 题面 L509 举例的三档目标页数
EXPECTED_TARGETS = [10, 100, 500]


def _load(name: str, path: Path):
    """按文件路径加载同目录模块，避免「从别处执行时 sys.path 不含 tools/」的坑。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise SystemExit(f"找不到模块文件：{path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    ap = argparse.ArgumentParser(description="校验 10/100/500 页规模语料的可复现性")
    ap.add_argument("--dir", type=Path, default=SCALE_DIR, help=f"规模语料目录（默认 {SCALE_DIR}）")
    ap.add_argument("--json", type=Path, default=None, help="把报告写成 JSON 便于存档")
    ap.add_argument(
        "--skip-fingerprint",
        action="store_true",
        help="跳过内容指纹重算（只查文件与字节哈希，更快）",
    )
    args = ap.parse_args()

    out_dir: Path = args.dir
    manifest_path = out_dir / MANIFEST_NAME
    if not manifest_path.exists():
        print(f"找不到规模语料清单：{manifest_path}")
        print("提示：先跑 python data/corpus/tools/build_scale_corpus.py 生成三档产物。")
        return 2
    if not out_dir.is_dir():
        print(f"找不到目录：{out_dir}")
        return 2

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"清单不是合法 JSON：{e}")
        return 2

    docs = data.get("documents") or []
    r = Report()

    # ---- 1. 覆盖三档 ----
    targets = sorted(d.get("target_pages") for d in docs if d.get("target_pages") is not None)
    r.add(
        f"恰好覆盖 {EXPECTED_TARGETS} 三档",
        targets == EXPECTED_TARGETS,
        f"实测 target_pages = {targets}",
    )
    r.add("清单 count 与 documents 条数一致", data.get("count") == len(docs),
          f"count={data.get('count')}，documents={len(docs)}")

    # ---- 2. 清单字段完整 ----
    need = ("id", "file", "target_pages", "actual_pages", "bytes",
            "content_sha256", "file_sha256_local")
    incomplete = [d.get("id") or d.get("file") for d in docs
                  if any(d.get(k) in (None, "") for k in need)]
    r.add("每档清单字段完整", not incomplete,
          "字段齐全" if not incomplete else f"缺字段：{incomplete}")

    # ---- 3. 磁盘与清单一一对应 ----
    on_disk = {p.name for p in out_dir.glob("*.pdf")}
    listed = {d.get("file") for d in docs}
    only_list = sorted(listed - on_disk)
    only_disk = sorted(on_disk - listed)
    r.add("磁盘 PDF 与清单一一对应", not only_list and not only_disk,
          "完全对应" if not only_list and not only_disk
          else f"清单有磁盘无：{only_list}；磁盘有清单无：{only_disk}")

    # ---- 4/5/6. 逐档：页数 / 字节数 / 字节哈希 ----
    _seed = _load("build_corpus", _HERE / "build_corpus.py")
    _scale = _load("build_scale_corpus", _HERE / "build_scale_corpus.py")

    seed = next((d for d in _seed.DOCS_ALL if d.get("id") == _scale.SEED_ID), None)
    if seed is None:
        print(f"找不到种子文档 {_scale.SEED_ID}，无法重算内容指纹。")
        return 2

    missing, page_bad, page_mismatch, size_bad, hash_bad, fp_bad = [], [], [], [], [], []
    rows: list[tuple[str, int, int, str, str]] = []

    for d in docs:
        fname = d.get("file") or ""
        p = out_dir / fname
        if not p.exists():
            missing.append(d.get("id") or fname)
            continue

        actual = _scale.count_pdf_pages(p)
        if d.get("actual_pages") is not None and actual != d["actual_pages"]:
            page_mismatch.append(f"{fname}(清单{d['actual_pages']}/实际{actual})")
        if d.get("target_pages") is not None and actual < d["target_pages"]:
            page_bad.append(f"{fname}(实际{actual} < 目标{d['target_pages']})")

        if d.get("bytes") is not None and p.stat().st_size != d["bytes"]:
            size_bad.append(f"{fname}(清单{d['bytes']}/实际{p.stat().st_size})")

        disk_hash = sha256_file(p)
        if d.get("file_sha256_local") and disk_hash != d["file_sha256_local"]:
            hash_bad.append(fname)

        fp_ok = "—"
        if not args.skip_fingerprint and d.get("content_sha256"):
            doc = _scale.expand_doc(seed, d["target_pages"], scale=d["target_pages"],
                                    seed_rng=20261001 + d["target_pages"])
            recomputed = _seed.content_fingerprint(doc["sections"])
            fp_ok = "✓" if recomputed == d["content_sha256"] else "✗"
            if recomputed != d["content_sha256"]:
                fp_bad.append(fname)

        rows.append((fname, d.get("target_pages", 0), actual,
                     disk_hash[:12] + "…", fp_ok))

    r.add("每档 PDF 都在磁盘上", not missing, "全部存在" if not missing else f"缺失：{missing}")
    r.add("每档实际页数 ≥ 目标页数", not page_bad,
          "全部达标" if not page_bad else str(page_bad))
    r.add("每档实际页数与清单一致", not page_mismatch,
          "全部一致" if not page_mismatch else str(page_mismatch))
    r.add("逐档字节数与清单一致", not size_bad, "全部一致" if not size_bad else str(size_bad))
    r.add("逐档字节哈希 file_sha256_local 与清单一致", not hash_bad and not missing,
          "全部一致" if not hash_bad and not missing
          else f"不符：{hash_bad}；缺失：{missing}")

    if args.skip_fingerprint:
        r.add("（已跳过）重算内容指纹 content_sha256", True, "本次用 --skip-fingerprint")
    else:
        r.add("重算内容指纹 content_sha256 与清单一致（跨机复现依据）", not fp_bad and not missing,
              "全部一致（重跑生成器也会得到同一份文档）"
              if not fp_bad and not missing
              else f"不符：{fp_bad}；缺失：{missing}")

    # ---- 输出 ----
    width = 78
    print("=" * width)
    print("  规模语料（10 / 100 / 500 页）复现校验")
    print(f"  清单：{manifest_path}")
    print("=" * width)
    print(f"{'文件':<34}{'目标':>5}{'实际':>5}  {'字节哈希':<15}{'内容指纹':<9}")
    print("-" * width)
    for fname, tgt, actual, dhash, fp_ok in rows:
        print(f"{fname:<34}{tgt:>5}{actual:>5}  {dhash:<15}{fp_ok:<9}")
    print("-" * width)
    for c in r.checks:
        print(("PASS " if c.passed else "FAIL ") + c.name)
        print("       " + c.detail)
    print("-" * width)
    print(f"通过 {len(r.checks) - len(r.failed)} 项，失败 {len(r.failed)} 项")
    if r.failed:
        print("校验未通过：")
        for c in r.failed:
            print(f"  - {c.name}：{c.detail}")
        print()
        print("提示：字节哈希对不上，先确认文件是否被改动（git status / git diff）。")
        print("      内容指纹对不上，说明生成器或种子语料变了，需要复核改动来源。")
    else:
        print("校验通过：三档规模语料可复现，报告中的 SHA-256 与仓库文件一致。")

    r.facts = {
        "targets": targets,
        "rows": [{"file": f, "target_pages": t, "actual_pages": a,
                  "file_sha256_local_prefix": h, "content_fingerprint_ok": fp}
                 for f, t, a, h, fp in rows],
        "mode": "skip-fingerprint" if args.skip_fingerprint else "full",
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"passed": not r.failed, "facts": r.facts,
                        "checks": [asdict(c) for c in r.checks]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"报告已写入：{args.json}")

    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
