"""进程内采集 RAG 真实预测，产出可直接喂给 `eval/runner.py --predictor jsonl` 的文件。

为什么有这个脚本（路线选择，2026-10-16 定）
--------------------------------------------
`eval/runner.py` 的 `HttpPredictor` 打不通真实 `/api/ask`，三处对不上：

| 项 | 真实 `/api/ask`（app/main.py:571） | `HttpPredictor`（eval/runner.py:393） |
|---|---|---|
| 鉴权 | 无（cookie 会话，全站要登录） | 假设可直接 POST |
| 请求体 | `AskRequest`（question） | `{question, database}` / `{question, top_k}` |
| 响应 | 契约 `AskResponse`（refs/trace） | 假设 `{sql, answer, retrieved}` |

用户拍板走**路二**：绕开服务，进程内直接调编排层。
好处是不动服务、不动账号、不动任何现有配置；代价是验证的是
「编排层 + 检索层 + 生成层」，**不含 HTTP 外壳那一层**。

口径诚实性（重要）
------------------
输出**只填 `answer` 和 `refs`，不填 `retrieved`**。原因见 eval/runner.py:380-383：

    ⚠️ `refs` 只填进 `answer_refs` 即可，**不要再在 `retrieved` 里复制一份**：
      `run_rag` 走的是 `pred.refs_for_retrieval()`（`retrieved or answer_refs`），
      复制一份会让 `retrieval_is_approximated` 判成 False —— 明明是近似值，
      报告里却显示成「真召回」，等于把不可信的分数伪装成可信的。

我们手上只有答案的引用，没有检索阶段的全量命中，所以必须让它保持「近似」状态。

用法
----
    # 单轮文档问答（21 题）
    D:\\python\\python.exe -X utf8 eval\\collect_rag_predictions.py --suite rag

    # 多跳（20 题，走 rag_multihop 后端）
    D:\\python\\python.exe -X utf8 eval\\collect_rag_predictions.py --suite multihop

    # 两个都跑
    D:\\python\\python.exe -X utf8 eval\\collect_rag_predictions.py --suite all

产出落在 `eval/reports/rag_real_predictions_<suite>_<UTC时间戳>.jsonl`，
同时打印一份可读的采集摘要（成功/失败/耗时）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES_DIR = ROOT / "eval" / "cases"
REPORTS_DIR = ROOT / "eval" / "reports"

#: 每个套件对应的 case 文件与文档检索后端。
#: 单轮用默认 `rag`；多跳必须换成 `rag_multihop`，否则多跳题会被单轮链路答掉。
SUITE_SPEC = {
    "rag": {"cases": "rag.jsonl", "backend": "rag", "label": "单轮文档问答"},
    "multihop": {"cases": "rag_multihop.jsonl", "backend": "rag_multihop", "label": "多跳检索"},
    # 政采文档单源：14 题，全挂在政府采购法规与公告语料上（2026-10-18 建）。
    # 走单轮后端 —— 这些题每题只问一份文档，不需要多跳分解。
    # ⚠ 语料不在 data/corpus/ 下，必须另设 RAG_CORPUS_DIR 与
    # RAG_CORPUS_MANIFEST_PATH（见 --help 与 README）；否则会去检索 chinook 那 15 份。
    "gov_rag": {
        "cases": "gov_procurement/rag.jsonl",
        "backend": "rag",
        "label": "政采文档单源",
    },
}


def _load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(json.loads(line))
    return cases


def _ref_to_payload(ref) -> dict:
    """把契约 `Ref` 转成 runner 判定器认的形状。

    `eval/runner.py:303-313` 记着一次历史缺陷：oracle 回放的是 `doc`/`anchor`
    形状，与判定器取值字段**同形**，于是字段名改错也全绿 —— 自证式全部通过。
    所以这里老老实实按契约字段名写：`kind`/`doc_id`/`section_path`/`snippet`。
    """
    if hasattr(ref, "model_dump"):
        data = ref.model_dump(mode="json")
    elif isinstance(ref, dict):
        data = dict(ref)
    else:  # pragma: no cover - 契约外的形状，宁可显式失败
        raise TypeError(f"无法序列化的引用类型：{type(ref)!r}")

    # 判定器优先读 `kind`；契约里 doc 引用的判别字段是 `kind`。
    # `doc_id` 在契约里可能叫 `doc_id`，也见过叫 `document_id` 的历史写法，两个都带上不影响。
    payload = {
        "kind": data.get("kind") or "doc",
        "doc_id": data.get("doc_id") or data.get("document_id") or "",
        "section_path": data.get("section_path") or data.get("anchor"),
        "snippet": data.get("snippet") or "",
    }
    # 页码/偏移等定位信息能带就带，判定器不用也无害。
    for extra in ("page", "page_no", "char_start", "char_end", "title", "score"):
        if data.get(extra) is not None:
            payload[extra] = data[extra]
    return payload


def collect(suite: str, *, limit: int | None, verbose: bool) -> Path:
    spec = SUITE_SPEC[suite]
    cases_path = CASES_DIR / spec["cases"]
    if not cases_path.exists():
        raise SystemExit(f"题集不存在：{cases_path}")

    cases = _load_cases(cases_path)
    if limit is not None:
        cases = cases[:limit]
    if not cases:
        raise SystemExit(f"{cases_path.name} 为空")

    # 导入放在函数内：让 `--help` 不必触发整个 app 包初始化。
    from app.orchestrator import build_default_orchestrator
    from app.rag.config import loaded_dotenv_keys

    keys = loaded_dotenv_keys()
    print(f"[env] 从 .env 加载了 {len(keys)} 个键" + (f"：{'、'.join(keys)}" if keys else "（未找到 .env 或键已被占用）"))
    print(f"[env] LLM_API_KEY={'已设置' if os.environ.get('LLM_API_KEY') else '**未设置**'}"
          f"  PG_DSN={'已设置' if os.environ.get('PG_DSN') else '**未设置**'}")

    # 多跳复用同一个编排器入口，只换文档检索后端（见 build_default_orchestrator 的 docstring）。
    # `graph_backend=disabled`：本次只测 SQL+RAG 主链，不让图谱 mock 工具参与，
    # 否则 mock 图谱会给出假结果污染判定。
    orchestrator = build_default_orchestrator(
        doc_search_backend=spec["backend"],
        graph_backend="disabled",
    )
    print(f"[编排] doc_search_backend={spec['backend']}  graph_backend=disabled")
    print(f"[题集] {cases_path.name}  共 {len(cases)} 题  （{spec['label']}）")
    print("-" * 78)

    rows: list[dict] = []
    ok = failed = 0
    t_all = time.perf_counter()

    for i, case in enumerate(cases, 1):
        cid = case["id"]
        question = case["question"]
        # 每题独立会话：本次只测单轮问答，会话串了会让上一轮的条件继承到下一题。
        session_id = f"collect-{suite}-{cid}"
        t0 = time.perf_counter()
        try:
            resp = orchestrator.answer(question, session_id=session_id, retrieval_mode="standard")
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            refs = [_ref_to_payload(r) for r in (resp.refs or [])]
            answer = resp.answer or ""
            row = {
                "id": cid,
                "answer": answer,
                "refs": refs,
                # 显式留空：见模块 docstring「口径诚实性」。有 refs 就不是真召回全量。
                "retrieved": [],
                "latency_ms": elapsed_ms,
            }
            ok += 1
            status = f"OK  refs={len(refs):<2} ans={len(answer):>4}字 {elapsed_ms:>6}ms"
        except Exception as exc:  # noqa: BLE001 - 采集脚本要的是「哪题炸了」，不是崩掉整轮
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            row = {
                "id": cid,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": elapsed_ms,
            }
            failed += 1
            status = f"**FAIL** {type(exc).__name__}: {str(exc)[:90]}"
            if verbose:
                traceback.print_exc()
        rows.append(row)
        print(f"  [{i:>2}/{len(cases)}] {cid:<12} {status}")
        if verbose and "answer" in row:
            print(f"           Q: {question[:70]}")
            print(f"           A: {row['answer'][:70]}")

    total_s = time.perf_counter() - t_all

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = REPORTS_DIR / f"rag_real_predictions_{suite}_{stamp}.jsonl"
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print("-" * 78)
    print(f"[完成] 成功 {ok} / 失败 {failed} / 共 {len(rows)} 题，总耗时 {total_s:.1f}s")
    print(f"[产出] {out}")
    print()
    print("下一步（判分）：")
    # 政采文档单源与 chinook 单轮共用 runner 的 rag 判据（gold_refs/answer_points 同形），
    # 所以 runner_suite 仍是 rag；区别只在预测来自哪套语料。
    runner_suite = "multihop" if suite == "multihop" else "rag"
    print(f"  D:\\python\\python.exe -X utf8 eval\\runner.py --suite {runner_suite} "
          f"--predictor jsonl --predictions \"{out}\"")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="进程内采集 RAG 真实预测（路线二）")
    ap.add_argument(
        "--suite",
        choices=["rag", "multihop", "gov_rag", "all"],
        default="rag",
        help=(
            "rag/multihop 用 data/corpus 那 15 份；gov_rag 用政采语料，"
            "须先设 RAG_CORPUS_DIR 与 RAG_CORPUS_MANIFEST_PATH；"
            "all 只含 chinook 的两套（政采语料与它不共用索引，不能混跑）"
        ),
    )
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 题，用于冒烟")
    ap.add_argument("--verbose", action="store_true", help="打印问题与答案摘要")
    args = ap.parse_args()

    suites = ["rag", "multihop"] if args.suite == "all" else [args.suite]
    for suite in suites:
        collect(suite, limit=args.limit, verbose=args.verbose)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
