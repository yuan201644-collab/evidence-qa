# -*- coding: utf-8 -*-
"""在基于 d03c3d9 的独立临时工作副本中，重跑 T5 评分（数据绑定 rev1）。

- 只读主仓库原件；所有改动/产物都落在临时副本 WT 内。
- 不改题目、参考答案、评分器、原始归档；不重跑模型（沿用 10-13 原始回答）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

MAIN = Path(r"C:\Users\ASUS\WorkBuddy\askdata-agent")
WT = Path(r"C:\Users\ASUS\WorkBuddy\_c_tmp_t5rev1_d03c3d9")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    for raw in (MAIN / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["PYTHONPATH"] = str(WT)  # 让 app/ 取自 d03c3d9 基线
    return env


def main() -> int:
    assert (WT / "eval/mainline/score_t5_development.py").is_file(), "副本缺少评分器"
    wt_head = subprocess.run(
        ["git", "-C", str(WT), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    print("worktree HEAD =", wt_head)

    # 1) rev1 清单放入副本
    src_rev1 = MAIN / "eval/mainline/development_v0.1/t5_v02_data_manifest.rev1.json"
    dst_rev1 = WT / "eval/mainline/development_v0.1/t5_v02_data_manifest.rev1.json"
    shutil.copy2(src_rev1, dst_rev1)
    rev1_sha = sha256_file(dst_rev1)
    print("rev1 manifest sha256 =", rev1_sha)

    # 2) 判据（handoff 参考答案）放入副本，保持自包含
    src_crit = MAIN / "agent_temp/handoff_C_20261014/t4_t5_criteria"
    dst_crit = WT / "agent_temp/handoff_C_20261014/t4_t5_criteria"
    dst_crit.parent.mkdir(parents=True, exist_ok=True)
    if dst_crit.exists():
        shutil.rmtree(dst_crit)
    shutil.copytree(src_crit, dst_crit)

    # 3) 评分副本：原始回答来自副本内归档原件（PR#61 已并入 d03c3d9）
    run_dir = WT / "agent_temp/c_review_20261014/t5-throttled-rev1"
    run_dir.mkdir(parents=True, exist_ok=True)
    arch_dir = WT / "eval/reports/c-line-1013/mainline/C-review-1013/t5-throttled"
    src_raw = arch_dir / "raw_answers.jsonl"
    assert src_raw.is_file(), f"副本内无归档原始回答：{src_raw}"
    shutil.copy2(src_raw, run_dir / "raw_answers.jsonl")

    # 4) 冻结记录绑定到 rev1（仅改 data_manifest 指向）
    freeze = json.loads((arch_dir / "freeze.json").read_text(encoding="utf-8"))
    old = dict(freeze.get("data_manifest") or {})
    freeze["data_manifest"] = {"file": "t5_v02_data_manifest.rev1.json", "sha256": rev1_sha}
    (run_dir / "freeze.json").write_text(
        json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print("freeze binding:", old, "->", freeze["data_manifest"])
    print("raw_answers sha256 =", sha256_file(run_dir / "raw_answers.jsonl"))
    print("freeze sha256      =", sha256_file(run_dir / "freeze.json"))

    # 5) 运行评分器（cwd=副本）
    cmd = [
        str(MAIN / ".venv" / "Scripts" / "python.exe"),
        "eval/mainline/score_t5_development.py",
        "--run-dir", "agent_temp/c_review_20261014/t5-throttled-rev1",
        "--questions", "eval/mainline/development_v0.1/t5_v02_questions.jsonl",
        "--reference", "agent_temp/handoff_C_20261014/t4_t5_criteria/t5_v02_reference_cases.jsonl",
    ]
    log_dir = MAIN / "agent_temp/c_review_20261014/logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        cmd, cwd=str(WT), env=load_env(), capture_output=True, text=True, encoding="utf-8"
    )
    body = (res.stdout or "") + (res.stderr or "")
    (log_dir / "t5_rev1_worktree_score.log").write_text(
        f"# cwd={WT}\n# HEAD={wt_head}\n# rev1_manifest_sha256={rev1_sha}\n" + body,
        encoding="utf-8",
    )
    print(body)
    print("exit_code =", res.returncode)
    return res.returncode


if __name__ == "__main__":
    raise SystemExit(main())
