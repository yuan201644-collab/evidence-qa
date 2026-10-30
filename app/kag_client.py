# -*- coding: utf-8 -*-
"""KAG 服务端协议客户端（纯传输层，不含任何业务判断）。

只做三件事：登录拿 Cookie、建会话、发一次提问并把 SSE 流解析成结构化结果。
协议细节全部来自服务器跑批脚本的实测实现（`build/run4arm.py`），
逐条对应，不自行发挥：

  POST /v1/accounts/login                     {account, password=sha256(pwd+"OPENSPG")} → Set-Cookie
  POST /public/v1/reasoner/session/create     {appId, name} → {"result":{"id":...}}
  POST /v1/chat/completions                   {prompt:[{type,content}], session_id,
                                               app_id, thinking_enabled, search_enabled} → text/event-stream

三个容易踩的点，都在这里处理掉：

1. **SSE 必须跨 chunk 行缓冲**：一帧 `data: {...}` 可能被 TCP 切成两半，
   逐 chunk 解码会抛 JSON 异常并静默丢帧；流尾最后一行常常**没有换行符**，
   不 flush 就会丢掉含最终答案的那一帧（跑批阶段踩过两次，且字节数看不出异常）。
2. **块名带引号**：`resultNodes[].name` 的实际值是 `"\\"AH24_v2_split_4\\""`，
   不 strip 就永远匹配不上文档前缀。
3. **块正文在 `properties.content`**：实测 93/93 个节点都带，
   所以证据片段不必回查图数据库，直接从响应里取。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# 块/文档节点命名：<DOC>_v2（根） / <DOC>_v2_split_<N>（块）
CHUNK_RE = re.compile(r"^(.+?)_v2(?:_split_(\d+))?$")

# 技术失败特征：KAG 兜底异常文案 / 服务端 300s 掐断。命中即视为"没跑成"。
TECH_MARKS = ("抱歉，处理查询", "[TIMEOUT]")


@dataclass
class KagChunk:
    """一个检索块。`content` 是块正文，直接来自 SSE，不回查图库。"""

    name: str
    doc: str
    content: str = ""
    score: float = 0.0


def _decode_chunk_content(value) -> str:
    """把 `resultNodes[].properties.content` 解码成纯正文。

    服务端下发的这个字段是**被序列化过一次的 JSON 字符串**：正文里的换行
    变成字面 `\\n`（反斜杠 + n 两个字符），整串还带首尾双引号。不解码的话
    界面上会显示成 `"SD16_v2\\n《2016年…》项目清单如下。`——引号多余、
    换行不生效，看起来像渲染 bug。这里只做解码，不改动任何字符内容。
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    stripped = text.strip()
    if stripped[:1] == '"' and stripped[-1:] == '"':
        try:
            decoded = json.loads(stripped)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, str):
            return decoded.strip()
    # 没有外层引号但仍有字面转义的情况（部分节点如此），退化为替换。
    if "\\n" in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    return text.strip()


@dataclass
class KagResult:
    answer: str = ""
    chunks: list[KagChunk] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    think: str = ""
    frames: int = 0
    elapsed: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.answer) and not self.error and not any(
            m in self.answer for m in TECH_MARKS)

    def chunks_of(self, doc: str, limit: int = 12) -> list[KagChunk]:
        """按文档前缀取块，保持服务端返回顺序（顺序本身就是相关性序）。"""
        pat = "%s_v2" % doc
        got = [c for c in self.chunks if c.name.startswith(pat + "_split")
               or c.name == pat]
        return got[:limit]


class KagClient:
    """一个进程内复用一个实例即可：Cookie 在 `login()` 后保存，供后续请求复用。"""

    def __init__(self, base: str, account: str, password: str, app_id: int,
                 timeout: int = 420):
        self.base = base.rstrip("/")
        self.account = account
        self.password = password
        self.app_id = int(app_id)
        self.timeout = timeout
        self._cookie: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 认证

    def login(self, force: bool = False) -> str:
        with self._lock:
            if self._cookie and not force:
                return self._cookie
            digest = hashlib.sha256((self.password + "OPENSPG").encode()).hexdigest()
            body = json.dumps({"account": self.account, "password": digest}).encode()
            req = urllib.request.Request(
                self.base + "/v1/accounts/login", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                pairs = []
                for raw in r.headers.get_all("Set-Cookie") or []:
                    m = re.match(r"([^=]+)=([^;]+)", raw)
                    if m:
                        pairs.append("%s=%s" % (m.group(1), m.group(2)))
                self._cookie = "; ".join(pairs) if pairs else "token=" + digest
                r.read()
            return self._cookie

    def create_session(self, name: str | None = None) -> int | None:
        cookie = self.login()
        payload = {"appId": self.app_id,
                   "name": name or "ui_%d" % int(time.time() * 1000)}
        req = urllib.request.Request(
            self.base + "/public/v1/reasoner/session/create",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Cookie": cookie})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
        m = re.search(r'"id"\s*:\s*(\d+)', raw)
        return int(m.group(1)) if m else None

    # ------------------------------------------------------------ 提问

    def ask(self, question: str, thinking: bool = True,
            on_frame=None) -> KagResult:
        """发一次提问，把 SSE 流解析成 `KagResult`。

        `on_frame(dict)` 是可选回调，每解析一帧调用一次 —— 上层用它往前端
        推进度（例如"KAG 已召回 N 个块"），不参与结果构造。
        回调抛异常会被吞掉：进度通道故障不得影响答案本身。
        """
        t0 = time.time()
        res = KagResult()
        state = {"answer": "", "sg_guard": 0}
        seen: dict[str, int] = {}

        try:
            cookie = self.login()
            sid = self.create_session()
            if sid is None:
                res.error = "session/create 未返回会话 id"
                return res

            body = json.dumps({
                "prompt": [{"type": "text", "content": question}],
                "session_id": sid,
                "app_id": self.app_id,
                "thinking_enabled": bool(thinking),
                "search_enabled": True,
            }).encode()
            req = urllib.request.Request(
                self.base + "/v1/chat/completions", data=body,
                headers={"Content-Type": "application/json", "Cookie": cookie})

            buf = b""
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._consume(line, res, state, seen, on_frame)
            if buf.strip():  # 流尾 flush：最后一行没有换行符也不能丢
                self._consume(buf, res, state, seen, on_frame)
            res.answer = state["answer"]
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            res.error = "HTTP %s %s" % (exc.code, detail)
        except Exception as exc:  # noqa: BLE001 - 网络层任何异常都要变成可读错误
            res.error = "%s: %s" % (type(exc).__name__, str(exc)[:200])

        res.elapsed = round(time.time() - t0, 1)
        return res

    # ------------------------------------------------------------ SSE 解析

    def _consume(self, line: bytes, res: KagResult, state: dict,
                 seen: dict[str, int], on_frame) -> None:
        s = line.decode("utf-8", "replace").strip()
        if not s.startswith("data:"):
            return
        s = s[5:].strip()
        if not s or s == "[DONE]":
            return
        try:
            obj = json.loads(s)
        except Exception:
            return
        res.frames += 1

        ans = obj.get("answer")
        if isinstance(ans, str) and len(ans) > len(state["answer"]):
            state["answer"] = ans

        think = obj.get("think")
        if isinstance(think, str) and len(think) > len(res.think):
            res.think = think

        for d in obj.get("documents") or []:
            nm = (d or {}).get("document_name") or (d or {}).get("name")
            if nm and nm not in res.docs:
                res.docs.append(nm)

        # 同一份子图每帧重复下发，只在元素数变大时重解析，避免几百帧重复解析几十 MB。
        sg = obj.get("subgraph")
        if isinstance(sg, list) and len(sg) > state["sg_guard"]:
            state["sg_guard"] = len(sg)
            for group in sg:
                if not isinstance(group, dict):
                    continue
                for node in group.get("resultNodes") or []:
                    if not isinstance(node, dict):
                        continue
                    raw = (node.get("name") or "").strip().strip('"')
                    m = CHUNK_RE.match(raw)
                    if not m:
                        continue
                    doc = m.group(1)
                    if doc not in res.docs:
                        res.docs.append(doc)
                    if raw in seen:
                        continue
                    props = node.get("properties") or {}
                    content = _decode_chunk_content(props.get("content"))
                    # 块正文首行是块名本身（"AH24_v2_split_4\n项目编号 ..."），
                    # 去掉它，界面上显示的是纯正文。
                    if content.startswith(raw):
                        content = content[len(raw):].lstrip("\n")
                    try:
                        score = float(props.get("score") or 0.0)
                    except (TypeError, ValueError):
                        score = 0.0
                    seen[raw] = len(res.chunks)
                    res.chunks.append(KagChunk(name=raw, doc=doc,
                                               content=content, score=score))

        if on_frame is not None:
            try:
                on_frame({"chunks": len(res.chunks), "docs": len(res.docs),
                          "answer_len": len(state["answer"])})
            except Exception:
                pass
