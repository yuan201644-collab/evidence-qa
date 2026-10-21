# -*- coding: utf-8 -*-
"""生成 T3 版本化修订清单（零覆盖；不触碰原清单与任何 freeze / 原始回答）。

判定依据：T3 采集与 T5 采集同在本机 2026-10-13 傍晚同一复核会话内
（复核报告 `C_复核_20261013_重跑T345.md` 第二节实测窗口 18:45-20:20 GMT+8，
报告显式列明 8000 端口为 T3、Chinook；T3 采集 freeze run_id 落在该窗口内）。
故 T3 与 T5 共享同一"采集当时实际库"指纹。
"""
import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(r"C:\Users\ASUS\WorkBuddy\askdata-agent")
DEV = ROOT / "eval" / "mainline" / "development_v0.1"
SRC = DEV / "t3_run3_data_manifest.json"
OUT = DEV / "t3_run3_data_manifest.rev1.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    if not SRC.is_file():
        print("缺少原清单：", SRC)
        return 1
    if OUT.exists():
        print("已存在修订清单，拒绝覆盖：", OUT)
        return 1

    original = json.loads(SRC.read_text(encoding="utf-8"))
    original_sha = sha256_file(SRC)

    measured = {
        "base_table_count": 14,
        "total_base_table_rows": 126607,
        "content_sha256": (
            "0fe6c67e5e29550a561955e22b1105db8c78298026077c4b153516ac074a506b"
        ),
    }

    revision = dict(original)
    revision.update(measured)

    schema_files = {
        "schema_meta_sha256": "infra/schema_meta.json",
        "descriptions_sha256": "infra/schema_meta.descriptions.zh.json",
        "aliases_sha256": "infra/schema_meta.aliases.zh.json",
    }
    refreshed = {}
    for key, rel in schema_files.items():
        if key in revision:
            path = ROOT / rel
            if path.is_file():
                revision[key] = sha256_file(path)
                refreshed[key] = revision[key]

    revision["_revision"] = {
        "label": "运行数据绑定修订后的开发回归评分",
        "revision_of": "t3_run3_data_manifest.json",
        "revision_of_sha256": original_sha,
        "reason": (
            "原冻结清单声明 18 表 / 15,769 行（2026-10-11 dddc236 快照），"
            "与 10-13 采集所用库（14 表 / 126,607 行，含 3 张 scale 表）本就不一致；"
            "采集阶段 driver 只登记清单文件哈希、未校验内容。"
        ),
        "binding_basis": (
            "eval/reports/c-line-1013/mainline/C_复核_20261013_重跑T345.md 第二节"
            "（2026-10-13 18:45-20:20 GMT+8 实测；报告显式列明 8000 端口为 T3 / Chinook，"
            "T3 采集 freeze run_id 20261013T105015Z、20261013T110817Z 均落在该窗口内）。"
            "该报告随 feat-archive-c-run-1013 于 2026-10-13 22:15:01 +0800 提交（0b030c8）。"
        ),
        "shared_fingerprint_with": "t5_v02_data_manifest.rev1.json",
        "t3_scoring_note": (
            "T3 审计脚本 eval/mainline/audit_t3_runtime_replay.py 不读取本清单、"
            "无指纹闸门，本修订不影响任何 T3 评分结果，仅为登记口径修正。"
        ),
        "collection_date": "2026-10-13",
        "regenerated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "generated_by": "agent_temp/c_review_20261014/prepare_t3_rev1.py",
    }

    OUT.write_text(
        json.dumps(revision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("原清单 sha256 :", original_sha)
    print("新清单:", OUT)
    print("新清单 sha256 :", sha256_file(OUT))
    print("schema 哈希刷新:", json.dumps(refreshed, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
