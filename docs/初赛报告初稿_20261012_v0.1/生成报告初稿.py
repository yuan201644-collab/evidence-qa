"""初赛报告生成入口。

两份报告已各自独立成目录，每个目录都能单独拷贝、单独重建：

- 设计报告\生成设计报告.py
- 技术实现说明书\生成技术实现说明书.py

原本一体式的生成逻辑（front() 前置页、markdown() 正文解析、figures() 插图、
table()/paragraph() 版式）已随两份报告拆分到上述两个目录。
同目录的 Word导出PDF.ps1 仍可一次导出两份 PDF。
"""
from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGETS = ("设计报告/生成设计报告.py", "技术实现说明书/生成技术实现说明书.py")


def main() -> None:
    for relative in TARGETS:
        script = ROOT / relative
        if not script.is_file():
            print(f"[跳过] 未找到 {script}")
            continue
        print(f"=== {relative} ===")
        runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
