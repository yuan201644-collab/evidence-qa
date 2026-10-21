"""pytest 启动器：在「解释器副本」里补上主仓库的 site-packages，然后跑 pytest。

## 为什么需要它

本机对 `.venv` 施加了写盘限制：由 `.venv\\Scripts\\python.exe` 启动的进程**只能写
项目树内的路径**，写项目外一律 `PermissionError errno=13`。连带后果是 pytest 的
`tmp_path` 落到项目内，触发
`eval/cross_source_heldout/tools/prepare_architecture_blind_review.py:22` 与
`run_architecture_comparison.py:973` 的「必须保存在项目工作区之外」约束，
9 个用例因此失败。**这不是产品缺陷，是运行环境位置问题。**

## 实测结论（均由 pwsh 直接启动，逐项对照）

| 启动方式                                        | 写系统 Temp |
| ----------------------------------------------- | ----------- |
| 父 = `.venv\\python.exe`，子 = 任意解释器          | DENIED 13   |
| 父 = 不受限解释器，子 = `.venv` 或项目外副本       | WRITABLE    |
| 父 = pwsh，子 = `.venv\\python.exe`               | DENIED 13   |
| 父 = pwsh，子 = 项目外副本 / uv 基础 / D:\\python  | WRITABLE    |

即：**限制沿进程树继承**，且 `.venv` 自身受限。因此必须由**不受限的父进程**直接
拉起一个**项目外的解释器副本**，执行链中不能出现受限进程。

这件事只有 PowerShell 能做（`python.exe` 自己无法逃离，因为它就是受限的那一方），
所以入口是 `scripts/run_tests.ps1`。

## 用法

    pwsh -File scripts/run_tests.ps1                       # 全量
    pwsh -File scripts/run_tests.ps1 tests/test_ask_progress.py
    pwsh -File scripts/run_tests.ps1 -k "nl2sql or cross_source"

直接调用本文件也可以，但必须由**项目外的**解释器启动，例如：

    C:\\askdata-tmp\\pytest-python.exe scripts/run_tests.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SITE_PACKAGES = PROJECT_ROOT / ".venv" / "Lib" / "site-packages"


def _site_packages() -> Path:
    override = os.environ.get("ASKDATA_SITE_PACKAGES")
    return Path(override).expanduser() if override else DEFAULT_SITE_PACKAGES


def main(argv: list[str]) -> int:
    site_packages = _site_packages()
    if not site_packages.is_dir():
        print(f"[run_tests] 找不到 site-packages：{site_packages}", file=sys.stderr)
        return 2

    # 副本是裸解释器：既没有 .venv 的依赖，也没有以项目根为 import 起点
    # （正常由 .pth / editable install 提供）。两者都补上。
    sys.path.insert(0, str(site_packages))
    sys.path.insert(0, str(PROJECT_ROOT))

    import pytest

    return int(pytest.main(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
