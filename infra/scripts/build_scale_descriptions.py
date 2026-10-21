"""为规模测试 schema 生成配套的列说明文件（`schema_meta_scale.descriptions.zh.json`）。

为什么需要：
    `app/nl2sql/capability_runtime.py:49-51` 的时间列判据刻意**只看说明文本、
    不看列名**（理由见该处注释：`birth_date` 是时间列但表示出生年份，
    靠列名会把「去年入职」与「去年出生」混为一谈）。

    而说明文件的查找顺序（`_description_path_from_catalog`）是
    「SCHEMA_META_DESCRIPTIONS_PATH > catalog 来源文件同名旁文件 > 项目默认」。
    规模题用的是 `infra/schema_meta_scale.json`，它的旁文件不存在，
    于是回落到默认的 `schema_meta.descriptions.zh.json`（Chinook 的说明），
    里面没有 `sales_scale_*` 的任何记录 ⇒ `time_columns('sales_scale_1000')` 返回空
    ⇒ 凡带「2024 年」这类时间范围的规模题都被要求核验器判成
    「相关业务表没有可用的时间依据」，3 轮重试后直接放弃作答。

    实测：30 道规模题里 12 道有 SQL、18 道被拦（其中 4 道时间题是这条链断掉）。

做法：
    把 `schema_meta_scale.json` 里每张表、每一列的 `description` 原样导出成
    系统约定的旁文件格式 `{"columns": {"表.列": "说明"}}`，让判据读到它。
    **不新增任何判据、不改词表**——说明文本本来就写在 schema 里，此前只是没被读到。
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "infra" / "schema_meta_scale.json"
OUT = SCHEMA.with_name(SCHEMA.stem + ".descriptions.zh.json")


def main() -> int:
    meta = json.loads(SCHEMA.read_text(encoding="utf-8"))
    columns: dict[str, str] = {}
    for table in meta.get("tables") or []:
        name = str(table.get("table") or "").strip()
        if not name:
            continue
        for column in table.get("columns") or []:
            column_name = str(column.get("name") or "").strip()
            description = str(column.get("description") or "").strip()
            if column_name and description:
                columns[str(name + "." + column_name).lower()] = description

    payload = {
        "schema": "schema_meta_descriptions_zh_v1",
        "source_schema": SCHEMA.name,
        "note": (
            "由 infra/scripts/build_scale_descriptions.py 从 schema_meta_scale.json "
            "的 columns[].description 原样导出，供 app/nl2sql/capability_runtime.py "
            "的时间列判据读取。判据只看说明文本、不看列名，故本文件必须存在，"
            "否则规模表会被判成「没有可用的时间依据」。"
        ),
        "columns": columns,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已写出 " + str(OUT.name) + "，共 " + str(len(columns)) + " 条列说明")
    time_like = [k for k, v in columns.items() if "日期" in v or "时间" in v]
    print("其中含时间语义的列 " + str(len(time_like)) + " 条：" + "、".join(sorted(time_like)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
