"""打包初赛源码交付 ZIP（release/verimind-src-<ver>.zip）。

设计要点
--------
1) **以 git HEAD 快照为底**，不用 `git add -A` 之后的整个工作区：工作区里有
   `agent_temp`（数百 MB 中间产物）、`.venv`、`web/node_modules`、修订副本的
   `.qa_render*` 渲染页，全打进去会让包膨胀十倍且混入过程文件。
2) **显式补必需的非跟踪产物**：前端已构建的 `web/dist`（单端口跑界面要用）、
   政采库快照 `data/gov/askdata_gov_structured.sql`、报告插图 `figures/`。
   这些要么是构建产物、要么体积较大，团队约定不进 git，但交付包必须有。
3) **写 MANIFEST.json**：逐文件 sha256 + git 提交号，供评委核对完整性。
4) 打包后打印体积构成，方便判断该不该进一步裁剪。

用法
----
    python scripts/package_release.py --dry-run      # 只统计，不写 zip
    python scripts/package_release.py                 # 生成 release/verimind-src-<日期>.zip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 以 HEAD 快照为底，再补这些非跟踪路径（相对仓库根）
EXTRA_PATHS = [
    "web/dist",
    "data/gov/askdata_gov_structured.sql",
    "docs/初赛报告初稿_20261012_v0.1/设计报告/figures",
    "docs/初赛报告初稿_20261012_v0.1/技术实现说明书/figures",
]
# 打进包里时放到根目录的重命名映射
RENAME_TO_ROOT = {
    "docs/交付包目录说明_20261018.md": "目录说明.md",
}
# 一律排除
PRUNE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".venv", "node_modules",
                   "agent_temp", ".mypy_cache", ".ruff_cache"}
PRUNE_PATH_PREFIXES = (
    "docs/初赛报告初稿_20261012_v0.1_修订副本",
    "docs/学习笔记",
    "docs/项目基本文件",
    "docs/知产维权",
)


def sh(cmd: list[str], cwd: Path = ROOT) -> str:
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def head_commit() -> str:
    try:
        return sh(["git", "rev-parse", "HEAD"])
    except Exception:
        return "unknown"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def prune(stage: Path) -> list[str]:
    """删掉不该进包的目录与路径前缀，返回被删的相对路径。"""
    removed: list[str] = []
    for path in sorted(stage.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and path.name in PRUNE_DIR_NAMES:
            removed.append(str(path.relative_to(stage)))
            shutil.rmtree(path, ignore_errors=True)
    for prefix in PRUNE_PATH_PREFIXES:
        target = stage / prefix
        if target.is_dir():
            removed.append(prefix)
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            removed.append(prefix)
            target.unlink()
    return removed


# 打包前必须从交付物里抹掉的凭据：
# 1) .env 里真实配置的模型密钥（模板里的占位值不算）；
# 2) 文档里出现过的本机演示账号口令。
# 数据库口令 ict_dev_only_change_me 是模板默认值、已公开在 .env.example，不在此列。
SECRET_ENV_KEYS = ("LLM_API_KEY", "DEEPSEEK_API_KEY", "GLM4_API_KEY")
KNOWN_SECRETS = ()  # 具体口令一律用下面的正则兜，避免大小写变体漏网
# 本机评测账号口令形如 <已移除：请按 .env.example 自行配置>，文档里出现过大小写不同的写法，
# 因此按形状匹配整段，而不是写死某一个字面量。
SECRET_PATTERNS = (
    re.compile(r"Eval-[A-Za-z0-9]{6,}-[A-Za-z0-9_]{6,}"),
    re.compile(r"(?i)(VERIMIND_EVAL_PASSWORD\s*[=:]\s*)([\"']?)[^\s\"']+\2"),
)
REDACTION = "<已移除：请按 .env.example 自行配置>"
TEXT_SUFFIXES = {".md", ".txt", ".json", ".py", ".ps1", ".cmd", ".yml", ".yaml",
                 ".toml", ".example", ".cfg", ".ini", ".sql"}


def load_secrets() -> list[str]:
    secrets = set(KNOWN_SECRETS)
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if key.strip() in SECRET_ENV_KEYS and value:
                secrets.add(value)
    # 长的先替换，避免短串先命中把长串切碎
    return sorted(secrets, key=len, reverse=True)


def sanitize(stage: Path) -> int:
    literals = load_secrets()
    replaced = 0
    for path in stage.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != "目录说明.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        original = text
        for secret in literals:
            if secret and secret in text:
                replaced += text.count(secret)
                text = text.replace(secret, REDACTION)
        for index, pattern in enumerate(SECRET_PATTERNS):
            hits = pattern.findall(text)
            if not hits:
                continue
            replaced += len(hits)
            if index == 1:  # 保留「变量名 =」，只抹值
                text = pattern.sub(lambda m: (m.group(1) or "") + REDACTION, text)
            else:
                text = pattern.sub(REDACTION, text)
        if text != original:
            path.write_text(text, encoding="utf-8")
            print(f"  [脱敏] {path.relative_to(stage).as_posix()}")
    return replaced


def stage_tree(stage: Path) -> None:
    stage.mkdir(parents=True, exist_ok=True)
    snapshot = stage.parent / "_head_snapshot.zip"
    subprocess.run(["git", "archive", "--format=zip", "-o", str(snapshot), "HEAD"],
                   cwd=ROOT, check=True)
    with zipfile.ZipFile(snapshot) as zf:
        zf.extractall(stage)
    snapshot.unlink()

    for rel in EXTRA_PATHS:
        src = ROOT / rel
        if not src.exists():
            print(f"  [跳过] 非跟踪路径不存在：{rel}")
            continue
        dst = stage / rel
        if src.is_dir():
            # 已存在的文件不动（HEAD 里可能的同名内容优先），只补缺的
            for item in sorted(src.rglob("*")):
                if not item.is_file():
                    continue
                target = dst / item.relative_to(src)
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    for src_rel, dst_name in RENAME_TO_ROOT.items():
        src = ROOT / src_rel
        if src.is_file():
            shutil.copy2(src, stage / dst_name)
        else:
            print(f"  [警告] 根说明文件缺失：{src_rel}")


def build_manifest(stage: Path) -> dict:
    files = []
    for path in sorted(stage.rglob("*")):
        if not path.is_file():
            continue
        files.append({
            "path": path.relative_to(stage).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        })
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_commit": head_commit(),
        "file_count": len(files),
        "total_bytes": sum(f["bytes"] for f in files),
        "files": files,
    }


def report(stage: Path) -> None:
    total = 0
    per_dir: dict[str, list[int]] = {}
    biggest: list[tuple[int, str]] = []
    for path in stage.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        top = path.relative_to(stage).parts[0]
        per_dir.setdefault(top, [0, 0])
        per_dir[top][0] += 1
        per_dir[top][1] += size
        biggest.append((size, path.relative_to(stage).as_posix()))
    print(f"\n未压缩合计 {total / 1048576:.1f} MB / {sum(v[0] for v in per_dir.values())} 个文件")
    print(f"{'顶层目录':<32}{'文件数':>8}{'体积':>12}")
    for name, (count, size) in sorted(per_dir.items(), key=lambda kv: -kv[1][1]):
        print(f"{name:<32}{count:>8}{size / 1048576:>10.1f} MB")
    print("\n最大的 12 个文件：")
    for size, name in sorted(biggest, reverse=True)[:12]:
        print(f"  {size / 1048576:>7.2f} MB  {name}")


def make_zip(stage: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(stage).as_posix())
    print(f"\nZIP 已生成：{dest}  {dest.stat().st_size / 1048576:.1f} MB")


def main() -> int:
    parser = argparse.ArgumentParser(description="打包初赛源码交付 ZIP")
    parser.add_argument("--version", default=datetime.now().strftime("%Y%m%d"),
                        help="版本后缀，默认今天日期")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写 zip")
    parser.add_argument("--keep-stage", action="store_true", help="保留暂存目录便于检查")
    args = parser.parse_args()

    stage = ROOT / "agent_temp" / "release_stage" / f"verimind-src-{args.version}"
    if stage.exists():
        shutil.rmtree(stage)
    print(f"暂存目录：{stage.relative_to(ROOT)}")
    stage_tree(stage)
    replaced = sanitize(stage)
    if replaced:
        print(f"  已脱敏 {replaced} 处凭据")
    removed = prune(stage)
    if removed:
        print(f"  已排除 {len(removed)} 项：{', '.join(removed[:6])}"
              f"{' …' if len(removed) > 6 else ''}")
    manifest = build_manifest(stage)
    (stage / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report(stage)
    if args.dry_run:
        print("\n[dry-run] 未生成 zip")
    else:
        make_zip(stage, ROOT / "release" / f"verimind-src-{args.version}.zip")
    if not args.keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
