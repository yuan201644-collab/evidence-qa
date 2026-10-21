"""ask_demo.py - 交互式问答入口（本地看效果用）

作用：
    扫全语料建**一次**索引，然后把问题送进「融合召回 -> 重排 -> 生成 -> 溯源」，
    每一步的耗时、产出、引用卡片都打出来。
    想先看"到底喂给大模型的是什么"，设 SHOW_PROMPT=1 或 LLM_MOCK=1，
    会把实际发出去的 system / user 完整打出来。

为什么不用 answer.py 的 __main__：
    那个是「一份 PDF 建一个工具」的旧写法，一次只问一句写死的问题，检索范围也只有那一份。
    现在语料是多份、检索要跨文档，走 corpus.index_corpus 建一个装全部文档的索引。

跑法：
    python -m app.ask_demo                        # 交互式提问
    python -m app.ask_demo 质保期多久 谁负责运维     # 直接问这几句
    SHOW_PROMPT=1 python -m app.ask_demo          # 额外打印喂给大模型的 prompt
    LLM_MOCK=1 python -m app.ask_demo             # 不调真模型，只看检索与拼装

后端：
    不设 PG_DSN 走内存向量库，进程退出索引即丢，适合本地看效果；
    设了 PG_DSN 走 pgvector，未变的文档会跳过 bge-m3 编码（见 corpus.py）。
"""

import os
import sys
import time


class _RecordingLLM:
    """套在真 LLM 外面，把实际发出去的 system / user 记下来。

    只看不改：记录之后原样转给内层客户端，所以链路行为与不加这层完全一致。
    """

    def __init__(self, inner):
        self.inner = inner
        self.last_system = None
        self.last_user = None

    def chat(self, system, user):
        self.last_system = system
        self.last_user = user
        return self.inner.chat(system, user)


def _show_prompt(llm, system_label):
    print()
    print("喂给大模型的 system：")
    print(llm.last_system)
    print()
    print("喂给大模型的 user（含检索到的片段）：")
    print(llm.last_user)


def _print_refs(refs):
    if len(refs) == 0:
        print("  没有引用。")
        return
    for i, ref in enumerate(refs, start=1):
        title = ref.doc_title or ref.doc_id
        where = ""
        if ref.page is not None:
            where += " 第" + str(ref.page) + "页"
        if ref.section_path:
            where += " " + ref.section_path
        print("  [" + str(i) + "] " + title + where)
        snippet = (ref.snippet or "").replace("\n", " ")
        print("      " + snippet[:80])


def _ask(tool, question):
    print()
    print("=" * 62)
    print("问题：" + question)
    t0 = time.time()
    try:
        result = tool.answer(question)
    except Exception as exc:
        print("  调用失败：" + type(exc).__name__ + ": " + str(exc))
        text = str(exc)
        if "Connection" in text or "huggingface" in text or "offline" in text.lower():
            print()
            print("  提示：模型权重可能没下全。走镜像重下：")
            print("    export HF_ENDPOINT=https://hf-mirror.com")
            print("    .venv/Scripts/python.exe -c \"from sentence_transformers import CrossEncoder; "
                  "CrossEncoder('BAAI/bge-reranker-v2-m3')\"")
        return
    print("端到端耗时 %.2f 秒" % (time.time() - t0))
    print()
    print("调用轨迹：")
    for step in result.trace:
        out = dict(step.output or {})
        # 引用修正在没生效时不必占屏；生效时单独报一行，方便核对引用可靠性
        fix = out.pop("citation_fix", None)
        print("  [%d] %-16s %9.1f ms  输出 %s" % (
            step.step_id, step.tool, step.elapsed_ms, out))
        if fix and (fix.get("mapped") or fix.get("dropped")):
            print("        引用修正：改写 %s | 删除 %s" % (
                fix.get("mapped") or "无", fix.get("dropped") or "无"))
    print()
    print("答案：")
    print(result.data)
    print()
    print("引用 %d 条：" % len(result.refs))
    _print_refs(result.refs)


def main():
    from app.rag.answer import build_rag_tool_from_corpus, OpenAILikeClient
    from app.rag.config import loaded_dotenv_keys, loaded_empty_keys
    from app.rag import preflight
    from app.rag.retrieval.reranker import BgeReranker

    keys = loaded_dotenv_keys()
    if len(keys) > 0:
        print("已从项目根的 .env 读取 " + str(len(keys)) + " 个变量：" + "、".join(keys))
    else:
        print("没从 .env 读到变量（根目录没有 .env，或者里面的键已被真实环境变量占着）")
    empties = loaded_empty_keys()
    if len(empties) > 0:
        print("⚠️ .env 里这些键写了但没填值，当前按「未设置」处理：" + "、".join(empties))

    # 启动预检：把"权重在哪、能不能下载、后端是什么"先摆出来。
    # 这个环境里"正在下载 2GB"和"正常加载中"在终端上都是没有输出，所以必须显式报。
    print("环境：")
    for line in preflight.env_lines():
        print(line)
    ok, lines = preflight.model_lines()
    print("模型权重：")
    for line in lines:
        print(line)
    if not ok:
        print()
        print("⚠️ 上面有不可用的模型。继续跑会在加载时明确报错（不会再静默回落到下载）。")

    dsn = os.environ.get("PG_DSN")
    print("索引中（扫 data/corpus/raw/，动态发现）...")
    t0 = time.time()
    client = _RecordingLLM(OpenAILikeClient())
    tool = build_rag_tool_from_corpus(dsn=dsn, llm=client, reranker=BgeReranker())
    print("索引完成，耗时 %.1f 秒" % (time.time() - t0))
    print(tool.corpus_index.summary())
    consistent, idx_lines = preflight.index_lines(tool.corpus_index)
    for line in idx_lines:
        print(line)
    if not consistent:
        print("⚠️ 向量数与切片数不一致，检索结果可能不完整。")
    print()
    inner = client.inner
    key_state = "已设置" if inner.api_key else "未设置（会报错，改设 LLM_MOCK=1 可空跑）"
    print("大模型：" + str(inner.model) + " @ " + str(inner.base_url))
    print("LLM_API_KEY：" + key_state + " | 超时 " + str(inner.timeout) + "s"
          + " | 重试 " + str(inner.max_retries) + " 次")
    print("检索参数：召回 " + str(tool.retrieve_top_k) + " 条 -> 重排留 " + str(tool.rerank_top_k) + " 条")

    show = os.environ.get("SHOW_PROMPT") == "1" or os.environ.get("LLM_MOCK") == "1"
    questions = sys.argv[1:]
    if len(questions) == 0:
        print()
        print("输入问题回车提问，直接回车或输入 exit 退出。")
        while True:
            try:
                q = input("问> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if q == "" or q in ("exit", "quit", "q"):
                break
            _ask(tool, q)
            if show:
                _show_prompt(client, "生成")
    else:
        for q in questions:
            _ask(tool, q)
            if show:
                _show_prompt(client, "生成")


if __name__ == "__main__":
    main()
