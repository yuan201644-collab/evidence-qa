# -*- coding: utf-8 -*-
"""步 3：在基于 d03c3d9 的独立副本上，以 APP_TIME_ANCHOR=2024-06-15 起服务并带节流重跑 T3。

一个脚本内完成：起服务 → 健康检查 → 节流采集 → 停服务。产物落主仓库新目录（不覆盖）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

MAIN = Path(r"C:\Users\ASUS\WorkBuddy\askdata-agent")
WT = Path(r"C:\Users\ASUS\WorkBuddy\_c_tmp_t3anchor_d03c3d9")
OUT = MAIN / "eval/reports/c-line-1013/mainline/C-t3-anchor-20261014/t3-anchor"
LOGDIR = MAIN / "agent_temp/c_review_20261014/logs"
BASE = "http://127.0.0.1:8000"
ANCHOR = "2024-06-15"
INTERVAL = "12"


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
    env["APP_TIME_ANCHOR"] = ANCHOR          # 本次重跑的关键：固定时间锚点
    env["PYTHONPATH"] = str(WT)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


def health_ok(timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        return r.status == 200 and body.get("status") == "ok"
    except Exception:
        return False


def main() -> int:
    LOGDIR.mkdir(parents=True, exist_ok=True)
    env = load_env()

    # 1) 副本自包含化
    shutil.copy2(MAIN / ".env", WT / ".env")
    (WT / "agent_temp").mkdir(parents=True, exist_ok=True)
    shutil.copy2(MAIN / "agent_temp/throttled_capture.py", WT / "agent_temp/throttled_capture.py")
    shutil.copy2(
        MAIN / "eval/mainline/development_v0.1/t3_run3_data_manifest.rev1.json",
        WT / "eval/mainline/development_v0.1/t3_run3_data_manifest.rev1.json",
    )
    shutil.copy2(MAIN / "agent_temp/c_review_20261014/t3_anchor_runtime_manifest.json",
                 WT / "agent_temp/t3_anchor_runtime_manifest.json")

    if OUT.exists() and any(OUT.iterdir()):
        print("输出目录已有内容，拒绝覆盖：", OUT)
        return 3

    svc_log = (LOGDIR / "svc_t3_anchor_8000.log").open("w", encoding="utf-8")
    print("启动服务（APP_TIME_ANCHOR=%s, cwd=%s）..." % (ANCHOR, WT), flush=True)
    proc = subprocess.Popen(
        [str(MAIN / ".venv/Scripts/python.exe"), "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(WT), env=env, stdout=svc_log, stderr=subprocess.STDOUT,
    )

    try:
        deadline = time.time() + 300
        ok = False
        while time.time() < deadline:
            if proc.poll() is not None:
                print("服务进程已退出，code =", proc.returncode)
                break
            if health_ok():
                ok = True
                break
            time.sleep(3)
        if not ok:
            print("健康检查未通过，停止。")
            return 4
        print("健康检查通过：", json.dumps(json.loads(
            urllib.request.urlopen(BASE + "/health", timeout=5).read().decode()), ensure_ascii=False), flush=True)

        cmd = [
            str(MAIN / ".venv/Scripts/python.exe"), str(WT / "agent_temp/throttled_capture.py"),
            BASE,
            str(WT / "eval/mainline/development_v0.1/t3_v02_questions.jsonl"),
            str(OUT),
            str(WT / "eval/mainline/development_v0.1/t3_run3_data_manifest.rev1.json"),
            str(WT / "agent_temp/t3_anchor_runtime_manifest.json"),
            INTERVAL,
        ]
        print("开始采集（节流间隔 %ss）..." % INTERVAL, flush=True)
        cap = subprocess.run(cmd, cwd=str(WT), env=env, capture_output=True, text=True, encoding="utf-8")
        (LOGDIR / "t3_anchor_capture.log").write_text(
            (cap.stdout or "") + (cap.stderr or "") + f"\nexit_code = {cap.returncode}\n", encoding="utf-8")
        print((cap.stdout or "")[-2000:], flush=True)
        print((cap.stderr or "")[-2000:], flush=True)
        print("capture exit_code =", cap.returncode, flush=True)
        return cap.returncode
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        svc_log.close()
        print("服务已停止。", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
