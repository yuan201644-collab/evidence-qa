"""生成独立的规模测试 schema 元数据 `infra/schema_meta_scale.json`。

为什么单独一份、而不放回 `infra/schema_meta.json`：
    2026-10-17 的 `a111f5d` 把三张 `scale.sales_scale_*` 从默认 schema 里移除了，
    理由写在那次提交里——它们是合成销售表、自述「不代表真实业务数据」，
    留在默认 schema 会给 Schema Linking 引入无关干扰项。这个判断成立：
    规模和干扰项不该同时进默认上下文。

    但移除的代价是 `eval/cases/scale_structured_v01/` 那 30 道规模题
    一道都跑不了（题面写死了 `scale.sales_scale_1000` 这类表名）。
    赛题原文（`docs/reference/赛题八_题面原文.txt:359-361`）要求
    「在至少两种不同数据规模下的实测或预估响应性能对比」，这项属系统效率 5 分。

    解法：照政采题已经确立的模式（`schema_meta_gov.json` 独立一份，
    跑题时用 `SCHEMA_META_PATH` 指向它），给规模题也做一份独立的
    `schema_meta_scale.json`。默认 schema 保持干净，规模题在自己的上下文里跑。

内容来源：
    scale 三张表的定义逐字取自 `a111f5d~1:infra/schema_meta.json`
    （即被移除前的版本），保证与 `infra/scripts/seed_scale_sales.py`
    真正灌出来的列结构一致——不重新手写，避免与灌库脚本漂移。
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_META = ROOT / "infra" / "schema_meta.json"
OUT = ROOT / "infra" / "schema_meta_scale.json"
SCALES = ("sales_scale_1000", "sales_scale_10000", "sales_scale_100000")


def read_scale_tables() -> list[dict]:
    """从 `a111f5d~1` 取回三张 scale 表的定义。"""
    raw = subprocess.run(
        ["git", "show", "a111f5d~1:infra/schema_meta.json"],
        cwd=str(ROOT),
        capture_output=True,
        check=True,
    ).stdout
    meta = json.loads(raw.decode("utf-8"))
    tables = meta.get("tables") or []
    picked = [t for t in tables if str(t.get("table")) in SCALES]
    missing = [name for name in SCALES if not any(str(t.get("table")) == name for t in picked)]
    if missing:
        raise SystemExit("历史版本里缺少这些表定义：" + "、".join(missing))
    return sorted(picked, key=lambda t: str(t.get("table")))


def main() -> int:
    default_meta = json.loads(DEFAULT_META.read_text(encoding="utf-8"))
    scale_tables = read_scale_tables()

    meta = {
        "schema_version": default_meta.get("schema_version", "v1"),
        "description": (
            "规模效率实验专用 schema：只含 scale.sales_scale_{1000,10000,100000} "
            "三张同构合成销售表。与 infra/schema_meta.json 分开，"
            "是为了不让合成表给默认业务链路的 Schema Linking 引入无关干扰项；"
            "跑规模题时以 SCHEMA_META_PATH 指向本文件。"
        ),
        "database": "chinook（scale schema 与 Chinook 11 表同库、独立 schema）",
        "tables": scale_tables,
    }
    OUT.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("已写出 " + str(OUT))
    print("  表数: " + str(len(scale_tables)))
    for table in scale_tables:
        print("    " + str(table["table"]) + "  " + str(table["row_count"]) + " 行  "
              + str(len(table["columns"])) + " 列")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
