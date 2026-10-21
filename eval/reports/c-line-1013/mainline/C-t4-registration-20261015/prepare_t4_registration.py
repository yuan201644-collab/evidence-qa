#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T4 开发回归评分正式登记（2026-10-15，C 线）

按 A 10-15 口径：T4 的 13/20 评分同意正式登记；评分文件从临时副本目录整理到
正式报告目录，并**以可执行方式**绑定判据、参考答案、数据清单与原始响应。

本脚本只做复制与哈希自校验，不改判据、不改参考答案、不改评分口径、不重跑模型。
历史材料（agent_temp/ 临时副本、C-review-1013/ 已入库的原始响应与冻结记录）
一律保留不动；本目录为新增的「正式登记」副本。

用法：
    .venv/Scripts/python.exe eval/reports/c-line-1013/mainline/C-t4-registration-20261015/prepare_t4_registration.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
REG = ROOT / "eval/reports/c-line-1013/mainline/C-t4-registration-20261015"

# 源（全部保持不动，只读复制）
SRC_SCORE = ROOT / "agent_temp/c_review_20261014/t4-throttled/t4_development_score.json"
SRC_RUBRIC = ROOT / "agent_temp/handoff_C_20261014/t4_t5_criteria/t4_v03_rubric.jsonl"
SRC_REFERENCE = ROOT / "agent_temp/handoff_C_20261014/t4_t5_criteria/t4_v03_reference_queries.jsonl"
# 已入库的历史原始响应与冻结记录（登记目录只引用，不重复复制）
ARCHIVED_RAW = ROOT / "eval/reports/c-line-1013/mainline/C-review-1013/t4-throttled/raw_answers.jsonl"
ARCHIVED_FREEZE = ROOT / "eval/reports/c-line-1013/mainline/C-review-1013/t4-throttled/freeze.json"
# 仓库内既有材料（引用 + 校验）
IN_REPO_QUESTIONS = ROOT / "eval/mainline/development_v0.1/t4_v03_questions.jsonl"
IN_REPO_MANIFEST = ROOT / "eval/mainline/development_v0.1/t4_run3_data_manifest.json"
IN_REPO_RUNTIME = ROOT / "eval/mainline/development_v0.1/t4_v03_runtime_manifest.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_as_stored(path: Path) -> str:
    """仓库 .gitattributes 声明 `* text=auto eol=lf`。

    因此入库（clone）拿到的是 LF 形态，与本机工作副本的 CRLF 形态哈希不同。
    登记件必须同时给出两个哈希，否则换机器/换平台复核时会对不上。
    """
    raw = path.read_bytes()
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    REG.mkdir(parents=True, exist_ok=True)
    (REG / "criteria").mkdir(exist_ok=True)

    # 1) 复制评分文件与判据副本
    pairs = [
        (SRC_SCORE, REG / "t4_development_score.json"),
        (SRC_RUBRIC, REG / "criteria/t4_v03_rubric.jsonl"),
        (SRC_REFERENCE, REG / "criteria/t4_v03_reference_queries.jsonl"),
    ]
    for src, dst in pairs:
        shutil.copy2(src, dst)
        same = sha256(src) == sha256(dst)
        checks.append((f"复制一致 {rel(dst)}", same, sha256(dst)))

    score = json.loads((REG / "t4_development_score.json").read_text(encoding="utf-8"))
    freeze = json.loads(ARCHIVED_FREEZE.read_text(encoding="utf-8"))

    # 2) 评分文件自述哈希 vs 实际文件哈希
    checks.append((
        "评分文件 raw_answers_sha256 == 已入库原始响应",
        score["raw_answers_sha256"] == sha256(ARCHIVED_RAW),
        sha256(ARCHIVED_RAW),
    ))
    checks.append((
        "评分文件 freeze_sha256 == 已入库冻结记录",
        score["freeze_sha256"] == sha256(ARCHIVED_FREEZE),
        sha256(ARCHIVED_FREEZE),
    ))
    checks.append((
        "评分文件 question_pack_sha256 == 仓库题包",
        score["question_pack_sha256"] == sha256(IN_REPO_QUESTIONS),
        sha256(IN_REPO_QUESTIONS),
    ))
    checks.append((
        "评分文件 reference_queries_sha256 == 判据包参考答案",
        score["reference_queries_sha256"] == sha256(SRC_REFERENCE),
        sha256(SRC_REFERENCE),
    ))

    # 3) 冻结记录自述哈希 vs 仓库材料
    checks.append((
        "冻结记录 case_file_sha256 == 仓库题包",
        freeze["case_file_sha256"] == sha256(IN_REPO_QUESTIONS),
        sha256(IN_REPO_QUESTIONS),
    ))
    checks.append((
        "冻结记录 data_manifest.sha256 == 仓库数据清单",
        freeze["data_manifest"]["sha256"] == sha256(IN_REPO_MANIFEST),
        sha256(IN_REPO_MANIFEST),
    ))
    checks.append((
        "冻结记录 service_runtime_manifest.sha256 == 仓库运行清单",
        freeze["service_runtime_manifest"]["sha256"] == sha256(IN_REPO_RUNTIME),
        sha256(IN_REPO_RUNTIME),
    ))

    # 4) 口径标注必须存在（防止登记件丢失「已曝光开发集」定性）
    checks.append((
        "评分文件标注 run_type=已曝光开发集回归",
        score.get("run_type") == "exposed_development_regression_not_unseen_evaluation",
        str(score.get("run_type")),
    ))
    checks.append((
        "评分文件含 warning 免责说明",
        bool(score.get("warning")),
        "ok",
    ))

    lines = [
        "# T4 开发回归评分正式登记 —— SHA-256 清单",
        "# 口径：已曝光开发集回归（exposed development regression），非未见题泛化结果、非比赛成绩",
        "# 生成脚本：prepare_t4_registration.py（复制 + 哈希自校验，未改判据/未重跑模型）",
        "# 历史材料全部保留：agent_temp 临时副本、C-review-1013/ 已入库原始响应与冻结记录均未改动",
        "#",
        "# ⚠️ 行尾策略（务必读）：仓库 .gitattributes 声明 `* text=auto eol=lf`，",
        "#   因此【入库后拿到的是 LF 形态】。下面每个受本登记管辖的文件给出两个哈希：",
        "#     [LF]   = 入库形态（clone/在任意平台检出的形态），复核时用这个",
        "#     [本地] = 本机工作副本形态（本文件由 CRLF 来源复制而来）",
        "#   若在同一台机器上直接读工作区文件，会得到 [本地] 那一列。",
        "",
        "# --- 登记目录内文件（副本）---",
    ]
    for _, dst in pairs:
        lines.append(f"[LF]   {sha256_as_stored(dst)}  {rel(dst)}")
        if sha256_as_stored(dst) != sha256(dst):
            lines.append(f"[本地] {sha256(dst)}  {rel(dst)}")
    readme = REG / "C_T4评分正式登记_20261015.md"
    if readme.is_file():
        lines.append(f"[LF]   {sha256_as_stored(readme)}  {rel(readme)}")
    lines.append("")
    lines.append("# --- 绑定材料（引用仓库内既有文件，原件未动）---")
    for p, note in [
        (IN_REPO_QUESTIONS, "T4 题包 v03"),
        (IN_REPO_MANIFEST, "T4 数据清单 run3"),
        (IN_REPO_RUNTIME, "T4 运行清单 v03"),
        (ARCHIVED_RAW, "T4 原始响应（已入库存档）"),
        (ARCHIVED_FREEZE, "T4 冻结记录（已入库存档）"),
    ]:
        stored = sha256_as_stored(p)
        local = sha256(p)
        tag = "[LF]  " if stored == local else "[LF]/[本地]"
        lines.append(f"{tag} {stored}  {rel(p)}    # {note}")
    lines.append("")
    lines.append("# --- 判据来源材料（A 材料包原件，仍在 agent_temp，未改动）---")
    lines.append(f"[LF]   {sha256_as_stored(SRC_RUBRIC)}  {rel(SRC_RUBRIC)}    # T4 判据 rubric 原件")
    lines.append("")
    lines.append("# --- 自校验结果 ---")
    for name, ok, val in checks:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}  ({val[:16]}...)")
    failed = [n for n, ok, _ in checks if not ok]
    lines.append("")
    lines.append(f"# 合计 {len(checks)} 项，通过 {len(checks) - len(failed)} 项，失败 {len(failed)} 项")

    (REG / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"登记目录：{REG}")
    for name, ok, val in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n结果：{len(checks) - len(failed)}/{len(checks)} 项通过")
    print("行尾提示：清单中 [LF] 为入库形态哈希，[本地] 为本机工作副本形态哈希")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())