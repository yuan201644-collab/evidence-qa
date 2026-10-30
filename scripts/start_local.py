#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本机演示启动器 —— 不需要 KAG 服务器、不加载本地模型。

用途：拿到仓库的人（评审、同学、未来的自己）在**一台干净的电脑**上，
只装 5 个 Python 包就能把界面跑起来，并验证核心能力中的一部分：

    可验证：登录、界面、冻结题库（31 题）、逐题事实表重算（拆题 → 判定 → 数值）
            整个过程不调模型、不联网、不需要图谱
    不可验证：KAG 问答链路、四臂对照实验 —— 这两项依赖实验服务器与商业模型 API，
            报告里已写明其环境，本脚本不假装能跑

为什么要有这个脚本，而不是沿用 start.cmd / start-kag.cmd：
    start.cmd     是老项目入口，会拉 PostgreSQL 容器并加载 6.5 GB 本地模型；
    start-kag.cmd 要求先有 KAG 服务地址（.env.local 里的 KAG_BASE）。
两者在评审机器上都跑不起来，所以这里单独给一条「最小可运行路径」。

用法：
    python scripts/start_local.py              # 默认 127.0.0.1:8000
    python scripts/start_local.py --port 8100
    python scripts/start_local.py --no-browser

账号：脚本会自动建一个演示管理员（已存在则重置密码），账号密码在启动时打印。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEMO_USER = "demo"
DEMO_PASSWORD = "Demo2026Local!"

# 导入路径所需的第三方包：包名 -> 用于报错的 pip 名
REQUIRED = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("pydantic", "pydantic"),
    ("sqlglot", "sqlglot"),
    ("psycopg", "psycopg[binary]"),
]


def _missing() -> list[str]:
    import importlib.util

    out = []
    for mod, pipname in REQUIRED:
        if importlib.util.find_spec(mod) is None:
            out.append(pipname)
    return out


def _env_defaults() -> dict[str, str]:
    """给一组本机可用的默认值；已显式设置的环境变量一律不覆盖。"""
    auth_db = ROOT / "data" / "local_auth.db"
    auth_db.parent.mkdir(parents=True, exist_ok=True)
    defaults = {
        # 账号库放本地文件，不碰生产库
        "VERIMIND_AUTH_DB_PATH": str(auth_db),
        # 本机是 http，Cookie 不能带 Secure，否则浏览器不回传、登录后立刻掉线
        "VERIMIND_AUTH_COOKIE_SECURE": "0",
    }
    for k, v in defaults.items():
        os.environ.setdefault(k, v)
    return {k: os.environ[k] for k in defaults}


def _seed_account() -> tuple[bool, str]:
    """建演示账号；已存在则重置密码。返回 (是否成功, 说明)。"""
    env = dict(os.environ)
    env["_DEMO_PW"] = DEMO_PASSWORD

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "app.auth_cli", *args],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
        )

    r = run("create-admin", "--username", DEMO_USER, "--password-env", "_DEMO_PW")
    if r.returncode == 0:
        return True, "已创建演示账号"
    # 已存在 → 重置密码，保证文档里写的口令一定可用
    r2 = run("reset-admin-password", "--username", DEMO_USER, "--password-env", "_DEMO_PW")
    if r2.returncode == 0:
        return True, "演示账号已存在，密码已重置"
    detail = ""
    for text in (r.stderr, r2.stderr):
        for line in (text or "").strip().splitlines():
            if line.strip():
                detail = line.strip()
    return False, detail or "创建账号失败（详见 python -m app.auth_cli create-admin）"


def main() -> int:
    # 被重定向到文件时 Python 默认整块缓冲，提示语会迟迟不出现；
    # 这个脚本的输出本身就是给人看的引导，改行缓冲。
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
        sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="循证问答 · 本机演示启动器")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    missing = _missing()
    if missing:
        print("缺少依赖：%s" % " ".join(missing))
        print("请先执行（在仓库根目录）：")
        print("    python -m pip install -r requirements-local.txt")
        print("或只装这一组：")
        print("    python -m pip install %s" % " ".join(missing))
        return 2

    envs = _env_defaults()

    dist = ROOT / "web" / "dist" / "index.html"
    if not dist.exists():
        print("[提示] 未找到 web/dist，界面不会显示，只有 API 可用。")
        print("       如需界面：cd web && npm ci && npm run build")
        print("       （仓库已随附构建产物，正常 clone 应该有；缺失说明被裁剪过）")

    ok, msg = _seed_account()
    print("[账号] %s" % msg)
    if ok:
        print("       用户名 %s   密码 %s" % (DEMO_USER, DEMO_PASSWORD))
    else:
        print("       登录可能不可用：%s" % msg)

    kag = os.environ.get("KAG_BASE") or os.environ.get("KAG_SERVER")
    if kag:
        print("[问答] 检测到 KAG_BASE，问答链路将直连 %s" % kag)
    else:
        print("[问答] 未设置 KAG_BASE —— 问答链路不可用（正常，需要实验服务器）")
        print("       可演示的部分：界面 + 冻结题库 + 逐题事实表重算（不调模型、不联网）")

    url = "http://%s:%d" % (args.host, args.port)
    print("")
    print("  打开 %s" % url)
    print("  账号库：%s" % envs["VERIMIND_AUTH_DB_PATH"])
    print("  停止：Ctrl+C")
    print("")

    if not args.no_browser:
        import threading
        import webbrowser
        threading.Timer(2.5, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="info", app_dir=str(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
