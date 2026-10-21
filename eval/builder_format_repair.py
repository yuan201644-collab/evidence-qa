"""仅修复JSON字符串结束后多余的分号，不改变知识文字或语义。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import builder_construct as builder


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.run_dir.resolve(strict=True)
    target = root / "draft_knowledge.json"
    if target.exists():
        raise FileExistsError("不覆盖已有草稿")
    source = root / "draft_raw_response.txt"
    text = source.read_text(encoding="utf-8").strip()
    edits = []
    for _ in range(5):
        try:
            parsed = json.loads(text)
            break
        except json.JSONDecodeError as error:
            if error.msg != "Expecting ',' delimiter" or text[error.pos:error.pos + 1] != ";" or text[:error.pos].rstrip()[-1:] != '"':
                raise
            edits.append({"position": error.pos, "removed_character": ";", "reason": "JSON字符串结束后的非法分号；无内容字符改写"})
            text = text[:error.pos] + text[error.pos + 1:]
    else:
        raise RuntimeError("格式修复超过预算")
    builder.check_entries(parsed)
    calls = builder.core.read(root / "draft_calls.json")
    parsed.update(version="draft-v0.1", model=calls["calls"][0].get("model", builder.MeasuredClient().model))
    builder.write_json(target, parsed)
    builder.write_json(root / "draft_format_repair.json", {"source_sha256": builder.sha256(source), "edits": edits,
                       "semantic_text_rewritten": False, "repair_source_sha256": builder.sha256(Path(__file__)),
                       "original_response_preserved": True})
    print("格式修复完成，条目：", len(parsed["entries"]), "非法分号：", len(edits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
