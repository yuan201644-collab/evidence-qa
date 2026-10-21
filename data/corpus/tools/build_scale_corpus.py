#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规模语料生成器 —— 为「效率 5 分」的 10 / 100 / 500 页规模实测造语料。

## 它解决什么问题

题面原文（`docs/reference/赛题八_题面原文.txt` L507–509）要求：

> ②多规模性能对比：至少在 3 种不同数据规模下（例如结构化数据：1 千行、1 万行、
>   10 万行；**非结构化文档：10 页、100 页、500 页**）的实测或仿真性能数据，
>   展示响应时间随数据量增长的变化趋势。

而现有 15 份语料实测**每份只有 1 页**（12 份 PDF 合计 12 页），离 500 页差两个数量级。
本脚本以现有语料为种子，按目标页数**循环扩写**，产出可跑真实检索链路的三档规模语料。

## 硬约束（改这个脚本前必读）

1. **绝不改动 `raw/` 下现有 15 份语料** —— `eval/cases/rag.jsonl` 的 15 条
   `gold_refs` 锚点（`doc` + `anchor` + `snippet`）全靠它们，动一个字基线就全废。
   本脚本只**新增**文件，输出到 `data/corpus/scale/` 独立目录。
2. **`manifest.json` 不动** —— 它是 `build_corpus.py` 的产物，重跑会被覆盖。
   规模语料的清单单独写 `data/corpus/scale/manifest_scale.json`。
3. **页码范围与 `sections` 的关系靠"填充到目标页数"而非"猜每页字数"** ——
   渲染后回读真实页数，不够就继续补，直到达到或超过目标页数。
4. 语言与格式与种子语料保持一致（中文、A4、同样字体），否则 OCR / 检索的表现不可比。

## 扩写规则

见 `expand_doc()` 的 docstring —— 核心是「保留种子首章原文 + 用业务语料池循环填充」，
这样**任何页数规模下首章内容与元数据都不变**，便于横向对比。

## 用法

    python data/corpus/tools/build_scale_corpus.py            # 生成 10/100/500 三档
    python data/corpus/tools/build_scale_corpus.py --pages 10 # 只生成 10 页档
    python data/corpus/tools/build_scale_corpus.py --verify   # 只核对已有产物的页数
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

# 复用种子语料的渲染器与语料定义，保证字体、页边距、章节风格完全一致。
# 用 importlib 按文件路径加载：本脚本与被导入文件同在 tools/ 下，直接 import 也能工作，
# 但按路径加载能避免"从别处执行脚本时 sys.path 不含 tools/"这个常见坑。
import importlib.util

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[2]  # data/corpus/tools -> 仓库根

_spec = importlib.util.spec_from_file_location("build_corpus", _HERE / "build_corpus.py")
if _spec is None or _spec.loader is None:  # pragma: no cover —— 只会在文件被删时触发
    raise SystemExit(f"找不到种子语料生成器：{_HERE / 'build_corpus.py'}")
_seed_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_seed_mod)

#: 三档目标页数，直接来自题面 L509 的举例
TARGET_PAGES = (10, 100, 500)

DEFAULT_OUT = ROOT / "data" / "corpus" / "scale"

#: 种子文档（用它的 category / 章节风格，以及被写入 manifest 的元数据）。
#: 选 KB-003（技术文档类）：章节结构最深（有 3 级），扩写后目录仍自然。
SEED_ID = "KB-003"

#: 扩写用的业务语料池 —— 与种子语料的行业背景一致（中电云 / 数据中心 / 数据中台），
#: 避免出现与知识库主题无关的内容导致检索语义漂移。
FILLER_SECTIONS: list[tuple[int, str]] = [
    (1, "运维管理体系"),
    (0, "为保障平台长期稳定运行，建立覆盖监控、告警、变更、应急四个环节的闭环管理机制。各环节均须留存操作记录，记录保留期不少于 180 天。"),
    (2, "监控指标"),
    (0, "核心监控指标包括：节点存活率、查询平均时延、写入吞吐、任务积压量、存储使用率五项。任一指标连续 5 分钟越过阈值即触发告警。"),
    (0, "告警按影响面分为三级：影响业务连续性为一级，需 15 分钟内响应；影响部分功能为二级，需 1 小时内响应；仅影响观测为三级，次工作日处理。"),
    (2, "变更管理"),
    (0, "所有生产环境变更须走审批流程，变更窗口默认安排在工作日 22:00 至次日 2:00。变更前须完成影响面评估与回滚预案，变更后须观察 30 分钟方可结束窗口。"),
    (0, "紧急变更可先执行后补审批，但须在两个工作日内补齐全部记录，并由值班负责人签字确认。"),
    (2, "应急预案"),
    (0, "针对单节点故障、机架断电、存储集群不可用、元数据服务异常四类场景，分别编制处置预案。预案每半年演练一次，演练结果纳入季度考核。"),
    (1, "数据治理流程"),
    (0, "数据按来源划分为内部生产数据、外部接入数据与公开数据三类。三类数据的采集、存储与对外提供均须符合数据分级分类管理规范的要求。"),
    (2, "元数据管理"),
    (0, "统一元数据服务维护表、字段、血缘、任务四类对象。新建表须先登记元数据，未登记的表不允许接入数据服务层。"),
    (0, "血缘关系由任务调度自动采集，人工修正须注明原因。血缘缺失超过 24 小时的表将被标记为待整改。"),
    (2, "数据质量"),
    (0, "数据质量从完整性、唯一性、一致性、及时性、准确性五个维度评估。每日凌晨执行质量巡检任务，异常结果推送至数据责任人。"),
    (0, "质量得分低于阈值的表将暂停对外服务，待整改完成并通过复检后方可恢复。"),
    (1, "接口与集成"),
    (0, "平台对外提供 REST 风格接口，统一使用 JSON 作为数据交换格式。所有接口须在网关登记，未登记的接口不允许对外暴露。"),
    (0, "接口按调用方配额限流，默认每分钟 600 次。超限请求返回 429 状态码，调用方须实现退避重试。"),
    (1, "性能与容量规划"),
    (0, "容量规划以近 6 个月的增长曲线为基准，预留不低于 30% 的余量。存储扩容须提前 30 天发起，计算扩容须提前 15 天发起。"),
    (0, "性能基线每季度重测一次，测试用例固定，环境固定，确保不同季度的数据可横向比较。"),
    (1, "安全与合规"),
    (0, "平台访问遵循最小权限原则，账号权限每季度复核一次。离职人员的账号须在离职当日回收。"),
    (0, "涉及个人信息的数据字段须加密存储，脱敏后方可用于非生产环境。审计日志不可删除，保留期不少于一年。"),
]

#: 页面尺寸与行高从种子模块取，避免两处硬编码不一致
LINE_H = _seed_mod.LINE_H
BODY_SIZE = _seed_mod.BODY_SIZE
MARGIN = _seed_mod.MARGIN
PAGE_H = _seed_mod.PAGE_H


def count_pdf_pages(path: Path) -> int:
    """读 PDF 真实页数。

    优先用 PyMuPDF（准确）；不可用时退回正则数 `/Type /Page` 对象
    （对 PyMuPDF 生成的 PDF 有效，实测与 PyMuPDF 结果一致）。
    """
    try:
        import pymupdf  # type: ignore

        with pymupdf.open(str(path)) as doc:
            return doc.page_count
    except Exception:  # noqa: BLE001 —— 没有 PyMuPDF 时走正则兜底
        import re

        blob = path.read_bytes()
        return len(re.findall(rb"/Type\s*/Page[^s]", blob))


def _log(msg: str) -> None:
    """把进度同时写到 stderr（实时可见）与 $TEMP 下的日志文件。

    为什么要单独做：这个渲染过程在 500 页档要跑十几分钟，而本机的
    stdout 会被完整缓冲到进程结束才吐出来 —— 一旦进程被杀，日志就全丢了
    （实测踩过：SIGTERM 后日志为空，只剩半成品 PDF，无法判断进度）。
    写文件 + 显式 flush 能让「跑到哪一步」在被打断后仍然可查。
    """
    import os

    line = f"[scale] {msg}"
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        log_path = Path(os.environ.get("TEMP", ".")) / "scale-build-progress.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def expand_doc(seed: dict, target_pages: int, *, scale: int, seed_rng: int) -> dict:
    """把单页种子文档扩写成约 `target_pages` 页的文档。

    规则（顺序很重要）：

    1. **完整保留种子的 `sections`**（首章原文）—— 这样任何规模档位下，
       种子章节的位置、锚点、内容都一致，检索结果可横向对比；
    2. 追加一个"扩展章节"，标题标明规模档位，便于人工核对；
    3. 用 `FILLER_SECTIONS` **循环**填充，每轮打乱段落顺序（固定随机种子，可复现），
       直到渲染页数达到 `target_pages`（用**预估**控制循环，渲染后回读真实页数校验）；
    4. 章节编号随着轮次递增重排，避免出现两个"运维管理体系"而显得像复制粘贴失误。

    为什么用"预估 + 回读"而不是"精确算页"：
       精确算页要复刻 PyMuPDF 的字形度量与折行逻辑，等于把渲染器抄一遍；
       而回读真实页数是**确定性**的，且慢一点无所谓（脚本不是热路径）。
    """
    rng = random.Random(seed_rng)
    sections: list[tuple[int, str]] = list(seed["sections"])

    # 第 2 条：扩展章节标题。用中文括号标注档位，人工打开 PDF 一眼能认。
    sections.append((1, f"扩展章节（规模档位：{target_pages} 页）"))
    sections.append(
        (
            0,
            f"本部分用于将知识库文档样本扩充至约 {target_pages} 页，以支撑多规模性能对比测试。"
            f"以下内容与正文章节同源同风格，用于验证系统在文档规模增长时的响应时间变化趋势。",
        )
    )

    # 3 条：循环填充。每轮把填充池整体追加一次并打乱顺序。
    # 用「行数 × 平均字符数」粗估已覆盖的页数，够目标就跳出，避免无限循环。
    # 估算系数取自实测：A4 + MARGIN=56 + LINE_H=16 + BODY_SIZE=10.5 时，
    # 每页约 42 行、折行后每行约 32 个中文字 → 约 1350 字/页。
    # 实测校准（10 页档）：用 38 字/行估出的量渲染出 15 页，偏保守 → 收窄到 32。
    # 该系数只为控制循环次数，**最终页数以 count_pdf_pages 回读为准**，
    # 不足时下面的补轮逻辑会兜住，所以估偏一点不影响正确性。
    chars_per_page = 42 * 32  # 42 行 × 每行约 32 个中文字 ≈ 1350 字/页
    round_no = 0
    while True:
        round_no += 1
        batch = list(FILLER_SECTIONS)
        if round_no > 1:
            rng.shuffle(batch)
        for level, text in batch:
            if level == 1:
                # 重排章号，避免重复章名看起来像误操作
                sections.append((level, f"第 {round_no + 1} 部分　{text}"))
            else:
                sections.append((level, text))

        total_chars = sum(len(t) for _, t in sections)
        if total_chars / chars_per_page >= target_pages:
            break
        # 防御：极端参数下不至于把内存吃穿（500 页档实测约 12 轮）
        if round_no > 400:  # pragma: no cover
            break

    doc = dict(seed)
    doc["sections"] = sections
    doc["id"] = f"{seed['id']}-S{scale}"
    doc["file"] = f"{seed['id']}_{scale}页规模版.pdf"
    doc["notes"] = (
        f"规模语料：由 {seed['id']} 扩写至约 {target_pages} 页，用于效率 5 分（题面 L507–509）"
        f"的多规模性能对比实测。种子章节原文保留，扩展部分为同源风格填充。"
    )
    doc["_target_pages"] = target_pages
    doc["_scale"] = scale
    return doc


def build_one(seed: dict, target_pages: int, out_dir: Path) -> dict:
    """生成单个规模档位，返回 manifest 条目。

    页数收敛策略：**先粗估渲染一次，回读真实页数，再按"每页对应多少章节"
    一次性补齐差额**，最多两轮。早先的写法是每轮只追加一个填充批次（27 个章节）
    就重渲染一次 —— 500 页档从 383 页补到 500 页需要 120 多轮、每轮渲染十几分钟，
    等于永远跑不完（实测卡死过）。差额要按实测比例折算，不能一轮挪一小步。
    """
    scale = target_pages
    _log(f"=== 开始 {target_pages} 页档 ===")
    doc = expand_doc(seed, target_pages, scale=scale, seed_rng=20261001 + scale)
    _log(f"{target_pages} 页档：预估章节数 {len(doc['sections'])}")

    out_path = out_dir / doc["file"]
    actual = 0
    for attempt in range(1, 4):
        _log(f"{target_pages} 页档：第 {attempt} 次渲染（章节 {len(doc['sections'])}）…")
        # 先渲到临时文件，成功后再原子替换目标文件。
        # 为什么必须这样：本机踩过一次真实事故 —— 500 页档第一版收敛逻辑要跑 120 多轮，
        # 手动 kill 掉进程后，它仍在把 394 页的中间态写回目标路径，
        # 覆盖掉了新版刚生成的 509 页成品，而清单还记着 509 页 → 清单与磁盘不一致。
        # 中间态永远不落正式路径，才能杜绝这种覆盖。
        staging = out_path.with_suffix(".staging.pdf")
        _seed_mod.render_pdf(doc, staging)
        actual = count_pdf_pages(staging)
        _log(f"{target_pages} 页档：第 {attempt} 次渲染完成，实际 {actual} 页")
        if actual >= target_pages:
            staging.replace(out_path)  # 同目录 rename，原子
            break
        # 未达标：丢弃中间产物，补齐后再来
        staging.unlink(missing_ok=True)

        # 按「章节数 / 实际页数」折算每页章节数，一次性补足差额并留 8% 余量。
        # 页面装不满会导致章节-页数是次线性关系，所以这里宁可略微多补。
        sections_per_page = len(doc["sections"]) / max(actual, 1)
        deficit_pages = (target_pages - actual) * 1.08
        need_sections = max(int(deficit_pages * sections_per_page), len(FILLER_SECTIONS))
        rounds = -(-need_sections // len(FILLER_SECTIONS))  # 向上取整
        _log(
            f"{target_pages} 页档：差 {target_pages - actual} 页，"
            f"按 {sections_per_page:.1f} 章节/页 补 {rounds} 个批次"
        )
        for i in range(rounds):
            extra = list(FILLER_SECTIONS)
            random.Random(20261001 + scale * 1000 + i).shuffle(extra)
            doc["sections"].extend(extra)
    else:  # pragma: no cover —— 3 轮仍不够说明估算系数错得离谱
        _log(f"警告：{target_pages} 页档在 3 轮后仍未达标（当前 {actual} 页）")
        if not out_path.exists():
            raise SystemExit(f"{target_pages} 页档未能生成，且无既有成品可保留")

    size = out_path.stat().st_size
    _log(f"{target_pages} 页档：完成，{actual} 页 / {size:,} B")
    return {
        "id": doc["id"],
        "file": doc["file"],
        "format": "pdf",
        "kind": "pdf",
        "category": seed["category"],
        "target_pages": target_pages,
        "actual_pages": actual,
        "bytes": size,
        "sections": len(doc["sections"]),
        "derived_from": seed["id"],
        "content_sha256": _seed_mod.content_fingerprint(doc["sections"]),
        "file_sha256_local": _seed_mod.sha256(out_path),
        "notes": doc["notes"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 10/100/500 页规模语料（效率 5 分用）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录")
    ap.add_argument(
        "--pages",
        type=int,
        nargs="*",
        default=list(TARGET_PAGES),
        help=f"目标页数，可给多个（默认 {' '.join(map(str, TARGET_PAGES))}）",
    )
    ap.add_argument("--verify", action="store_true", help="只核对已有产物的真实页数，不重新生成")
    ap.add_argument(
        "--manifest-only",
        action="store_true",
        help="只按磁盘上已有的 PDF 重建清单，不重新渲染（改完别的档位后补清单用）",
    )
    args = ap.parse_args()

    out_dir: Path = args.out
    seed = next(d for d in _seed_mod.DOCS_ALL if d["id"] == SEED_ID)

    if args.verify:
        manifest_path = out_dir / "manifest_scale.json"
        if not manifest_path.exists():
            print(f"没有找到清单：{manifest_path}，先跑一次生成。", file=sys.stderr)
            return 2
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        ok = True
        for entry in data["documents"]:
            path = out_dir / entry["file"]
            if not path.exists():
                print(f"✗ {entry['file']} 文件缺失")
                ok = False
                continue
            actual = count_pdf_pages(path)
            flag = "✓" if actual >= entry["target_pages"] else "✗"
            if actual < entry["target_pages"]:
                ok = False
            print(
                f"{flag} {entry['file']:<34} 目标 {entry['target_pages']:>3} 页 / "
                f"实际 {actual:>3} 页 / {path.stat().st_size:>9,} B"
            )
        return 0 if ok else 1

    out_dir.mkdir(parents=True, exist_ok=True)

    if args.manifest_only:
        # 只重建清单：复用磁盘上已有的 PDF，不重渲染。
        # 场景：分档跑完（每次只跑一档），最后统一出一份完整清单。
        readme = out_dir / "manifest_scale.json"
        entries = []
        for pages in args.pages:
            doc = expand_doc(seed, pages, scale=pages, seed_rng=20261001 + pages)
            path = out_dir / doc["file"]
            if not path.exists():
                print(f"跳过（文件不存在）：{doc['file']}", file=sys.stderr)
                continue
            entries.append(
                {
                    "id": doc["id"],
                    "file": doc["file"],
                    "format": "pdf",
                    "kind": "pdf",
                    "category": seed["category"],
                    "target_pages": pages,
                    "actual_pages": count_pdf_pages(path),
                    "bytes": path.stat().st_size,
                    "sections": len(doc["sections"]),
                    "derived_from": seed["id"],
                    "content_sha256": _seed_mod.content_fingerprint(doc["sections"]),
                    "file_sha256_local": _seed_mod.sha256(path),
                    "notes": doc["notes"],
                }
            )
        write_manifest(readme, entries)
        for e in entries:
            print(f"{e['id']:<14}目标 {e['target_pages']:>3} / 实际 {e['actual_pages']:>4} 页 / {e['bytes']:>10,} B")
        print(f"\n清单已写出：{readme}（未重新渲染）")
        return 0

    # 清掉上一轮可能留下的日志（进程被杀时会残留半成品，日志串起来容易误读）
    import os

    log_path = Path(os.environ.get("TEMP", ".")) / "scale-build-progress.log"
    log_path.write_text("", encoding="utf-8")

    _log(f"种子文档 {SEED_ID}（{seed['file']}，{seed['category']}）")
    _log(f"输出目录 {out_dir}")

    entries = []
    for pages in args.pages:
        entry = build_one(seed, pages, out_dir)
        entries.append(entry)
        print(
            f"{entry['id']:<12}{entry['target_pages']:>4} 页档 -> 实际 {entry['actual_pages']:>4} 页  "
            f"{entry['bytes']:>10,} B  {entry['file']}",
            flush=True,
        )

    write_manifest(out_dir / "manifest_scale.json", entries)
    print(f"\n清单已写出：{out_dir / 'manifest_scale.json'}")
    print("注意：本脚本只新增 data/corpus/scale/ 下的文件，不动 raw/ 与 manifest.json。")
    return 0


def write_manifest(manifest_path: Path, entries: list[dict]) -> None:
    """写规模语料清单。抽出来是为了让「生成」与「只重建清单」两条路径共用同一格式。"""
    manifest_path.write_text(
        json.dumps(
            {
                "generator": "data/corpus/tools/build_scale_corpus.py",
                "purpose": "题面 L507–509「非结构化文档 10 页 / 100 页 / 500 页」多规模性能对比",
                "seed": SEED_ID,
                "count": len(entries),
                "hash_notes": {
                    "content_sha256": "语料内容指纹，对章节数据做哈希，跨机器与 PyMuPDF 版本稳定。",
                    "file_sha256_local": "生成文件字节哈希，受写入器版本影响，只作本地产物指纹。",
                    "actual_pages": "渲染后回读的真实 PDF 页数（用 PyMuPDF，不可用时退回正则数 /Type /Page）。",
                },
                "documents": entries,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
