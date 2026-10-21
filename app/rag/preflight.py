"""preflight.py - 启动前检查与"清晰报错"

为什么需要这个模块：
    这条链路上"卡住"和"死了"在终端里长得一模一样——加载 2GB 权重、正在下载
    2GB 权重、进程被 OOM 杀掉，三者都是**没有任何输出**。已经为此付过代价：
    一次直接运行 reranker 没走到 .env 加载路径，于是回退成 HF 仓库名并静默
    开始下载 2.2GB，看起来就是"运行到一半无提示退出"（详见 config.py 的说明）。

    所以启动时把下面几件事明确报出来，让"慢"和"错"可以区分：

        1. 模型权重在哪（本地目录 / HF 仓库名）；
        2. 本地目录存不存在；仓库名模式下缓存是否齐全、能不能联网下载；
        3. HF_ENDPOINT / HF_HUB_OFFLINE 当前生效值；
        4. 向量后端是 memory 还是 pgvector；
        5. 语料数 / 切片数 / 向量数是否一致。

    原则：**检查与报错只做加法，不改链路行为**。缺权重时立刻抛
    ModelUnavailableError 并给出修法，而不是回落到去下几百 MB 到几 GB。
"""

import os
from pathlib import Path

# HF 缓存的默认位置。只用来"给出更准的错误信息"，不参与加载决策——
# 真正的加载仍由 sentence_transformers / huggingface_hub 负责。
_HF_HUB_CACHE_ENV = "HF_HUB_CACHE"
_HF_HOME_ENV = "HF_HOME"


class ModelUnavailableError(RuntimeError):
    """模型权重不可用（路径写错、未缓存且无法下载等）。"""


def _huggingface_cache_dir():
    """推测 HF 缓存目录，仅用于把错误信息写得更具体。"""
    cache = os.environ.get(_HF_HUB_CACHE_ENV)
    if cache:
        return os.path.join(cache, "hub")
    hf_home = os.environ.get(_HF_HOME_ENV)
    if hf_home:
        return os.path.join(hf_home, "hub")
    return os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")


def is_local_path(value):
    """判断模型名是本地路径还是 HF 仓库名。

    约定：出现路径分隔符、盘符、或以 . / ~ 开头即视为本地路径。
    仓库名是 `org/name` 这种形式（含斜杠但更短且不含盘符），所以不能只看斜杠。
    """
    if not value:
        return False
    if value.startswith(".") or value.startswith("~"):
        return True
    if ":\\" in value or ":/" in value or value.startswith("/") or value.startswith("\\"):
        return True
    return False


def cached_repo_dir(model_name):
    """给出 HF 仓库名对应的本地缓存目录（推测值）。"""
    return os.path.join(_huggingface_cache_dir(), "models--" + model_name.replace("/", "--"))


def check_model(model_name, env_name):
    """检查一个模型是否可用，返回问题描述列表（空列表 = 看起来没问题）。

    只做"能确定的事情"：本地路径存不存在；仓库名在离线模式下缓存是否齐全。
    真正的加载校验留给 ensure_model_ready() 里的 try/except——
    这里不 import sentence_transformers，避免为了检查而先加载几十秒。
    """
    problems = []
    if not model_name:
        problems.append(env_name + " 为空，无法确定用哪个模型")
        return problems

    if is_local_path(model_name):
        if not os.path.isdir(model_name):
            problems.append(
                env_name + " 指向本地目录但不存在：" + model_name
            )
        else:
            # 本地目录至少要能找到配置或权重之一，否则多半是路径指到了上一层
            names = os.listdir(model_name)
            if not any(n.endswith((".json", ".safetensors", ".bin", ".model")) for n in names):
                problems.append(
                    env_name + " 指向的目录里没有模型文件：" + model_name
                    + "（应包含 config.json / *.safetensors 等）"
                )
        return problems

    # 仓库名模式：离线且缓存不齐时直接给结论（否则会表现为长时间无输出）
    offline = os.environ.get("HF_HUB_OFFLINE", "")
    if offline in ("1", "true", "True") and not os.path.isdir(cached_repo_dir(model_name)):
        problems.append(
            env_name + " 用的是 HF 仓库名 " + model_name + "，但设置了 HF_HUB_OFFLINE="
            + offline + " 且本地没有缓存：" + cached_repo_dir(model_name)
        )
    return problems


def ensure_model_ready(model_name, env_name, kind):
    """加载权重前调用：不可用就抛 ModelUnavailableError，并给出可执行的修法。

    kind 只用于错误信息措辞（如"嵌入模型" / "重排模型"）。
    """
    problems = check_model(model_name, env_name)
    if not problems:
        return

    lines = ["%s不可用：" % kind] + ["  - " + p for p in problems]
    if is_local_path(model_name) or not model_name:
        lines.append("修法：把 " + env_name + " 指向已下载好的权重目录，")
        lines.append("      例如 " + env_name + "=D:/askdata-agent/models/bge-reranker-v2-m3")
    else:
        lines.append("修法二选一：")
        lines.append("      ① 允许联网下载：设 HF_HUB_OFFLINE=0 并按需设 HF_ENDPOINT（如 https://hf-mirror.com）")
        lines.append("      ② 用本地权重：把 " + env_name + " 指向已下载好的目录")
    raise ModelUnavailableError("\n".join(lines))


def announce(model_name, kind):
    """加载权重前的提示行。

    存在的意义就是"不许长时间无输出"：让人知道当前卡在哪一步、大概要等多久。
    """
    where = "本地目录 " + model_name if is_local_path(model_name) else "HF 仓库 " + model_name
    print("[" + kind + "] 正在加载 " + where + "（首次加载约 10~30 秒，请稍候）...", flush=True)


def env_lines():
    """环境摘要：HF 相关变量与向量后端。返回可直接打印的字符串列表。"""
    endpoint = os.environ.get("HF_ENDPOINT") or "(未设，用默认 huggingface.co)"
    offline = os.environ.get("HF_HUB_OFFLINE") or "(未设，允许联网下载)"
    dsn = os.environ.get("PG_DSN")
    return [
        "  HF_ENDPOINT     = " + endpoint,
        "  HF_HUB_OFFLINE  = " + offline,
        "  向量后端         = " + ("pgvector（PG_DSN 已配置）" if dsn
                                   else "memory（PG_DSN 未配置，进程退出即丢）"),
    ]


def configure_hf_offline(model_name):
    """权重已在本地缓存时，强制 HF 库走离线路径，返回是否已置为离线。

    为什么需要：huggingface_hub 即便命中缓存，也会先向 huggingface.co 发一批
    `HEAD .../resolve/main/xxx.json` 去核对 revision，失败就按 1/2/4/8/8 秒退避
    重试 5 轮，每个文件各来一遍。本机装了 TLS 拦截类加速器（如 Watt Toolkit /
    Steam++，证书 CN=SteamTools Certificate）时，这些请求全部
    CERTIFICATE_VERIFY_FAILED —— 证书在 Windows 信任库里，但 Python 的 certifi
    不认，于是每次加载模型都白等几分钟（实测一次 /api/ask 被拖过 300 秒超时）。

    这里只做一件保守的事：**确认本地确实有完整缓存**，再置 HF_HUB_OFFLINE=1。
    缓存不完整时保持原状，让正常的下载路径继续可用，绝不会把"能下"变成"下不了"。
    """
    if is_local_path(model_name) or not model_name:
        return False
    if str(os.environ.get("HF_HUB_OFFLINE", "")).lower() in ("1", "true", "yes"):
        return True

    repo_dir = cached_repo_dir(model_name)
    # 只看"有没有至少一个快照"还不够：半途下载中断也会留下快照目录。
    # 要求快照里有 config.json，且不存在 huggingface_hub 的未完成标记。
    snapshots = os.path.join(repo_dir, "snapshots")
    if not os.path.isdir(snapshots):
        return False
    complete = False
    for entry in os.listdir(snapshots):
        snap = os.path.join(snapshots, entry)
        if os.path.isfile(os.path.join(snap, "config.json")):
            # incomplete 文件存在 = 这次下载没收尾，宁可让它联网补齐
            if not any(n.endswith(".incomplete") for n in os.listdir(snap)):
                complete = True
                break
    if not complete:
        return False

    os.environ["HF_HUB_OFFLINE"] = "1"
    # transformers 有一份自己的开关，两边都置才彻底不联网
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    return True


def model_lines():
    """两个模型的可用性检查结果，返回 (是否全部可用, 字符串列表)。"""
    from app.rag.retrieval.reranker import DEFAULT_MODEL as _RERANK_DEFAULT, ENV_MODEL as _RERANK_ENV
    from app.rag.retrieval.indexer import DEFAULT_EMBEDDING_MODEL as _EMBED_DEFAULT, ENV_EMBEDDING_MODEL as _EMBED_ENV

    targets = [
        ("嵌入模型", os.environ.get(_EMBED_ENV) or _EMBED_DEFAULT, _EMBED_ENV),
        ("重排模型", os.environ.get(_RERANK_ENV) or _RERANK_DEFAULT, _RERANK_ENV),
    ]
    lines = []
    ok = True
    for kind, name, env_name in targets:
        problems = check_model(name, env_name)
        where = "本地 " + name if is_local_path(name) else "仓库名 " + name
        if problems:
            ok = False
            lines.append("  [FAIL] " + kind + "（" + where + "）")
            for p in problems:
                lines.append("      - " + p)
        else:
            lines.append("  [OK] " + kind + "（" + where + "）")
    return ok, lines


def asset_lines():
    """检查路由静默降级所依赖的静态元数据是否存在。

    模型权重**不在这里查**：权重的可用性由 model_lines() 负责，而它认的是
    BGE_EMBEDDING_MODEL / BGE_RERANKER_MODEL 实际指向的位置（本地目录或 HF
    缓存）。这里原本硬编码 models/bge-m3/config.json，于是"权重在 HF 缓存里
    下好了、只是没搬进仓库 models/ 目录"这种完全正常的状态会被报成缺失，
    把 asset_lines 的返回值永久钉死成 False。资产检查因此只保留"仓库内必须
    存在的静态元数据"，它的失败才是真失败。
    """
    root = Path(__file__).resolve().parents[2]
    paths = [
        ("语料清单", root / "data" / "corpus" / "manifest.json"),
        ("结构化 schema", root / "infra" / "schema_meta.json"),
        ("schema 中文说明", root / "infra" / "schema_meta.descriptions.zh.json"),
    ]
    lines = []
    ok = True
    for label, path in paths:
        exists = path.is_file()
        if exists:
            lines.append("  [OK] " + label + "：" + str(path))
        else:
            ok = False
            lines.append("  [MISSING] " + label + "缺失：" + str(path))
    return ok, lines


def index_lines(index):
    """索引一致性：语料数 / 切片数 / 向量数。

    "向量数对不上切片数"是静默故障的典型来源——检索看着能跑，但有一批切片
    根本没进向量库（或反过来，库里留着已被删除文档的旧向量）。
    """
    lines = []
    docs = len(index.indexed) + len(index.skipped)
    lines.append("  文档 %d（本次重建 %d / 跳过 %d）" % (docs, len(index.indexed), len(index.skipped)))
    lines.append("  切片 %d" % len(index.chunks))

    store = index.store
    n_vectors = None
    vectors = getattr(store, "vectors", None)      # MemoryVectorStore
    if vectors is not None:
        n_vectors = len(vectors)
    else:                                          # PgVectorStore
        try:
            counts = store.count_chunks_by_doc()
            n_vectors = sum(counts.values())
        except Exception as exc:                   # 检查不该把链路带崩
            lines.append("  向量数：查询失败（" + type(exc).__name__ + "：" + str(exc)[:60] + "）")
            return True, lines

    ok = (n_vectors == len(index.chunks))
    lines.append("  向量 %d%s" % (n_vectors, "" if ok else "  ⚠️ 与切片数不一致"))
    if len(index.empty) > 0:
        lines.append("  ⚠️ 切不出切片的文档 %d 份：%s" % (len(index.empty), "、".join(d[:14] for d in index.empty)))
    return ok, lines


def main():
    # 先加载 .env：本模块是"启动前检查"的独立入口，直接
    # `python -m app.rag.preflight` 时没有任何别的地方会去读 .env，
    # 于是 BGE_EMBEDDING_MODEL / HF_HUB_OFFLINE / PG_DSN 全部回落默认值，
    # 检查结论与真实运行环境不符（模块开头记的那次"没走到 .env 加载路径、
    # 于是回退成 HF 仓库名并静默开始下载 2.2GB"就是同一个坑）。
    from app.nl2sql.config import load_env_file

    load_env_file()

    print("运行资产：")
    assets_ok, lines = asset_lines()
    for line in lines:
        print(line)

    print("模型权重：")
    models_ok, lines = model_lines()
    for line in lines:
        print(line)

    print("环境：")
    for line in env_lines():
        print(line)

    if not assets_ok or not models_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
