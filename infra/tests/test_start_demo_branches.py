#!/usr/bin/env python3
"""start_demo.py 失败分支的桩测试。

为什么用桩而不是真的把 Docker 卸了、把 daemon 停了：
失败分支恰恰是**只有出问题时才会走到**的代码，用真环境测它的成本极高
（要卸载重装 Docker、要停 Docker Desktop），结果是没人测，等评委机器上第一次走这条路径才发现。
桩测试把「判定逻辑」与「环境状态」解耦，几毫秒跑完，可以每次都跑。

实测踩过的坑（别再用这个思路）：想通过清空 PATH 来模拟「没装 Docker」是行不通的——
Windows 上解释器自身就起不来了，脚本一行都不会执行，会得到「无输出、退出码 0」的假象，
看起来像脚本静默通过了。所以必须打桩，不能改环境。

用法：
    python infra/tests/test_start_demo_branches.py

退出码：0 全部通过；1 有分支不符合预期。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
START_DEMO = ROOT / "start_demo.py"


def load_module():
    spec = importlib.util.spec_from_file_location("start_demo", START_DEMO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cp(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out, err)


def drive(sd, label: str, *, which, run_impl, argv, expect_rc: int, expect_text: str) -> bool:
    """把 main() 跑一遍，断言它给出预期的退出码与可读的失败原因。"""
    sd.shutil.which = which
    sd.run = run_impl
    sys.argv = argv
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sd.main()
    out = buf.getvalue()
    hit = expect_text in out
    good = rc == expect_rc and hit
    print(f"[{'PASS' if good else 'FAIL'}] {label}")
    print(f"        退出码={rc}（期望 {expect_rc}）  提示命中={hit}（找「{expect_text}」）")
    for line in out.strip().splitlines()[-3:]:
        print("        | " + line)
    return good


def main() -> int:
    if not START_DEMO.exists():
        print(f"找不到 {START_DEMO}")
        return 2

    sd = load_module()
    results: list[bool] = []

    # 分支 1：机器上没装 Docker —— 必须给出安装指引，而不是丢一个 FileNotFoundError
    results.append(drive(
        sd,
        "没装 Docker：提示未安装并退出 2",
        which=lambda name: None,
        run_impl=lambda *a, **k: cp(0),
        argv=["start_demo.py"],
        expect_rc=2,
        expect_text="没有找到 docker 命令",
    ))

    # 分支 2：装了 Docker Desktop 但没启动 —— 这是最常见的一种，提示必须可操作
    results.append(drive(
        sd,
        "daemon 没在跑：提示先启动 Docker Desktop 并退出 2",
        which=lambda name: "docker",
        run_impl=lambda cmd, **k: cp(1, err="error during connect: docker daemon is not running")
        if cmd[:2] == ["docker", "info"]
        else cp(0),
        argv=["start_demo.py"],
        expect_rc=2,
        expect_text="daemon 没在跑",
    ))

    # 分支 3：镜像缺失且没加 --offline —— 必须点出两条出路（联网加速 / 离线包）
    def run_missing_image(cmd, **k):
        if cmd[:2] == ["docker", "info"]:
            return cp(0)
        if cmd[:3] == ["docker", "compose", "version"]:
            return cp(0, out="Docker Compose version v2.29.0")
        if cmd[:3] == ["docker", "image", "inspect"]:
            return cp(1, err="No such image")
        return cp(0)

    results.append(drive(
        sd,
        "镜像缺失且未加 --offline：给出两条出路并退出 2",
        which=lambda name: "docker",
        run_impl=run_missing_image,
        argv=["start_demo.py"],
        expect_rc=2,
        expect_text="镜像缺失",
    ))

    print()
    print(f"桩测试结果：{sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
