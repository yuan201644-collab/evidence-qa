# -*- coding: utf-8 -*-
"""RAG 环境（预检）脚本 —— 回答「这台机器能不能跑真语料评测」。

为什么需要它：多跳检索的对照实验要在**能跑通向量通道**的机器上做
（本机 A 缺 jieba / sentence-transformers / psycopg / pgvector，跑不了真语料）。
与其问「你的机器行不行」，不如给一个脚本，跑一下就知道缺什么、怎么装。

用法（在项目根目录）：
    python eval/preflight_rag_env.py            # 只查本地，不联网
    python eval/preflight_rag_env.py --net      # 额外探测 LLM API / HF 镜像 / PG 连接

判定口径（与 pyproject.toml 一致，勿凭印象改）：
    fastapi==0.141.1  uvicorn==0.53.0  pydantic==2.13.5  pymupdf==1.28.2
    jieba>=0.42.1  sentence-transformers>=3.3.0  psycopg[binary]>=3.2.0  pgvector>=0.3.0

输出：每项一行 ✅ / ❌ / ⚠️，最后给「缺什么、怎么装」的清单。
"""

import argparse
import glob
import importlib
import os
import sys
from pathlib import Path

# 让 app.* 可以被导入（与本目录其他脚本一致：把仓库根塞进 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (导入名, 显示名, 是否必须, 版本要求说明)
DEPS = [
    ("fastapi", "FastAPI", True, "==0.141.1"),
    ("uvicorn", "uvicorn", True, "==0.53.0"),
    ("pydantic", "pydantic", True, "==2.13.5"),
    ("fitz", "PyMuPDF", True, "==1.28.2（import 名是 fitz）"),
    ("jieba", "jieba 分词", True, ">=0.42.1（0.42.1 无 wheel，需 setuptools<80 构建）"),
    ("sentence_transformers", "sentence-transformers", True, ">=3.3.0（bge 重排靠它）"),
    ("psycopg", "psycopg3", True, ">=3.2.0（需 [binary] extra）"),
    ("pgvector", "pgvector", True, ">=0.3.0"),
]

ENV_VARS = [
    ("LLM_API_KEY", True, "大模型 API Key（题面：建议订购国产 Token）"),
    ("LLM_API_BASE", False, "API 地址（如 https://api.deepseek.com/v1；旧名 LLM_BASE_URL 也认）"),
    ("LLM_MODEL", False, "模型名（默认 gpt-4o-mini，应替换为实际国产模型）"),
    ("PG_DSN", True, "PostgreSQL 连接串（pgvector 存储要用）"),
]

PROBLEMS = []
OKS = 0


def ok(msg):
    global OKS
    OKS += 1
    print("  ✅ " + msg)


def bad(msg):
    PROBLEMS.append(msg)
    print("  ❌ " + msg)


def warn(msg):
    print("  ⚠️  " + msg)


def section(title):
    print("\n── " + title + " " + "─" * max(1, 60 - len(title)))


def check_python():
    section("1. Python 版本")
    v = sys.version_info
    if (v.major, v.minor) >= (3, 11):
        ok("Python %d.%d.%d（题面建议 Python；pyproject 用 uv 管理）" % (v.major, v.minor, v.micro))
    else:
        bad("Python %d.%d 过低（建议 3.11+）" % (v.major, v.minor))


def check_deps():
    section("2. 第三方依赖（与 pyproject.toml 对齐）")
    for mod, name, required, ver in DEPS:
        try:
            m = importlib.import_module(mod)
            version = getattr(m, "__version__", None)
            extra = ("（版本 %s）" % version) if version else ""
            ok("%s 已安装 %s｜要求 %s" % (name, extra, ver))
        except Exception as e:  # noqa: BLE001
            bad("%s 缺失或导入失败（%s: %s）｜要求 %s" % (name, type(e).__name__, e, ver))


def check_corpus():
    section("3. 语料（data/corpus/raw/）")
    files = sorted(glob.glob("data/corpus/raw/*"))
    real = [f for f in files if not f.endswith(".gitkeep")]
    if len(real) >= 10:
        ok("共 %d 份语料（题面要求 ≥10 份）" % len(real))
    else:
        bad("只有 %d 份语料，题面要求 ≥10 份" % len(real))
    # 顺带提示扫描件：多跳评测用不到，但 OCR 是另一条线的硬前提
    try:
        import fitz

        scanned = []
        for f in real:
            if f.lower().endswith(".pdf"):
                try:
                    d = fitz.open(f)
                    if not any(p.get_text().strip() for p in d):
                        scanned.append(os.path.basename(f))
                    d.close()
                except Exception:  # noqa: BLE001
                    pass
        if scanned:
            warn("以下 %d 份 PDF 无文本层（扫描件），本评测用不到；但它们是 OCR 线的硬前提：%s"
                 % (len(scanned), "、".join(scanned)))
    except Exception:  # noqa: BLE001
        pass  # PyMuPDF 缺失已在第 2 节报过


def check_env():
    section("4. 环境变量")
    for name, required, desc in ENV_VARS:
        val = os.environ.get(name)
        if val:
            shown = val if name != "LLM_API_KEY" else val[:6] + "…" + val[-4:]
            ok("%s 已设置 = %s　（%s）" % (name, shown, desc))
        elif required:
            bad("%s 未设置　（%s）" % (name, desc))
        else:
            warn("%s 未设置（可选）　（%s）" % (name, desc))


def check_net(args):
    section("5. 网络探测（--net）")
    # 5.1 大模型 API：不真调用（花钱），只看域名通不通
    base = os.environ.get("LLM_API_BASE") or os.environ.get("LLM_BASE_URL") \
        or "https://api.openai.com/v1"
    host = base.split("//")[-1].split("/")[0]
    try:
        import socket

        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ok("LLM API 域名可解析：%s" % host)
    except Exception as e:  # noqa: BLE001
        bad("LLM API 域名解析失败 %s（%s）——现场/本机若无外网，须换本地模型" % (host, e))
    # 5.2 HF 镜像：bge 模型下载走这里
    try:
        import urllib.request

        urllib.request.urlopen("https://hf-mirror.com", timeout=8)
        ok("HF 镜像（hf-mirror.com）可达 —— bge 向量/重排模型下载走这里")
    except Exception as e:  # noqa: BLE001
        bad("HF 镜像不可达（%s）——首次跑 RAG 下载 bge 模型会失败；"
            "可设 HF_ENDPOINT=https://hf-mirror.com 或提前手动放权重" % e)
    # 5.3 PostgreSQL + pgvector
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        bad("PG_DSN 未设置，无法测数据库")
        return
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=6) as conn:
            ok("PostgreSQL 连接成功")
            with conn.cursor() as cur:
                cur.execute("SELECT extname FROM pg_extension WHERE extname='vector'")
                row = cur.fetchone()
                if row:
                    ok("pgvector 扩展已启用")
                else:
                    bad("pgvector 扩展未启用 —— 需在该库执行 CREATE EXTENSION vector;")
    except Exception as e:  # noqa: BLE001
        bad("PostgreSQL 连接失败（%s: %s）" % (type(e).__name__, e))


def check_import_chain():
    section("6. 链路级导入（比单包导入更能暴露问题）")
    for mod in ("app.rag.answer", "app.rag.multihop", "app.rag.retrieval.fusion"):
        try:
            importlib.import_module(mod)
            ok("%s 可导入" % mod)
        except Exception as e:  # noqa: BLE001
            bad("%s 导入失败（%s: %s）" % (mod, type(e).__name__, e))


def main():
    ap = argparse.ArgumentParser(description="RAG 环境预检")
    ap.add_argument("--net", action="store_true", help="额外做网络探测（LLM API/HF 镜像/PG）")
    args = ap.parse_args()

    print("RAG 环境预检 —— 回答「这台机器能不能跑真语料评测」")
    check_python()
    check_deps()
    check_corpus()
    check_env()
    if args.net:
        check_net(args)
    check_import_chain()

    print("\n" + "=" * 68)
    if not PROBLEMS:
        print("✅ 没有发现阻碍项：这台机器可以跑真语料评测。")
        print("   建议先跑一遍确认：DOC_SEARCH_BACKEND=rag uv run python -m eval.runner --suite rag")
        return 0
    print("❌ 有 %d 项阻碍，装好后再跑一次本脚本：" % len(PROBLEMS))
    for p in PROBLEMS:
        print("  - " + p)
    print("\n常见安装方式（项目用 uv 管理）:")
    print("  uv sync                                   # 按 pyproject 装齐全部依赖")
    print("  uv add \"psycopg[binary]\" pgvector          # 缺数据库驱动时")
    print("  export PG_DSN=postgresql://user:pass@host:5432/db")
    print("  export LLM_API_KEY=...  LLM_API_BASE=https://api.deepseek.com/v1  LLM_MODEL=deepseek-chat")
    return 1


if __name__ == "__main__":
    sys.exit(main())
