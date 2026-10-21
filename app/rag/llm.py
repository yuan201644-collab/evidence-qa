"""llm.py - 生成答案用的 LLM 客户端（OpenAI 兼容协议）

赛题八题面建议“大模型订购国产 Token”，本项目用国产 LLM API（如通义千问 /
DeepSeek），它们都兼容 OpenAI 的 /v1/chat/completions 协议。这里用一个最小客户端
直接发 HTTP 请求（标准库 urllib），不依赖官方 SDK，零新增依赖。

环境变量（变量名以 .env.example 为准）：
    LLM_API_KEY          必填；没有就报错（明确提示去配置）
    LLM_API_BASE         兼容端点，默认 https://api.openai.com/v1（国产模型换成对应地址）
                         ⚠️ 过渡期兼容：也接受旧名 LLM_BASE_URL，两个都没设才回落默认值
    LLM_MODEL            模型名，默认 gpt-4o-mini（按实际国产模型替换，如 qwen-plus）
    LLM_TIMEOUT_SECONDS  单次请求超时秒数，默认 60
    LLM_MAX_RETRIES      失败重试次数，默认 2（总尝试次数 = 重试次数 + 1）

    max_tokens 不读环境变量（.env.example 未声明该变量），需要时由调用方显式传入。

稳定性：网络抖动是常态，_call 失败会按 1s、2s…… 退避重试。
        但只重试"重试有意义"的错误：网络类错误、5xx、408/429；
        4xx（鉴权失败、参数错、路径错）重试多少次结果都一样，直接放弃，
        免得白等退避还把一个"配置错了"伪装成"网络不稳"。

空输出：HTTP 200 但正文为空、或被 max_tokens 截断（finish_reason=length）时，
        抛 LLMError 而不是返回空串。本模型（DeepSeek-V4.1-Flash）带思维链，
        reasoning_tokens 计入 max_tokens，给小了就会"想一半就断"（详见 _call 内注释）。

可测试性：设 LLM_MOCK=1 时不发真实请求，返回一个把上下文拼起来的占位答案，
方便在没有 key 的环境里把“检索 -> 重排 -> 生成”整条链路跑通、写测试。
"""

import json
import os
import time
import urllib.error
import urllib.request

from app.rag.config import env_int

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"
_DEFAULT_TIMEOUT = 60
_DEFAULT_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 1.0  # 第 n 次重试前等 n 秒

_ENV_API_KEY = "LLM_API_KEY"
_ENV_BASE_URL = "LLM_API_BASE"
_ENV_BASE_URL_LEGACY = "LLM_BASE_URL"  # 本模块早期用的名字，过渡期一并接受
_ENV_MODEL = "LLM_MODEL"
_ENV_TIMEOUT = "LLM_TIMEOUT_SECONDS"
_ENV_MAX_RETRIES = "LLM_MAX_RETRIES"

# 4xx 里明确属于"稍后再试"的两个：408 请求超时、429 限流。
# 其余 4xx 是请求本身有问题（鉴权/参数/路径），重试不会有不同结果。
_RETRYABLE_4XX = (408, 429)


def _is_retryable(exc):
    """判断异常是否值得重试：网络类错误与 5xx/408/429 可重试，其余 4xx 放弃。"""
    if isinstance(exc, urllib.error.HTTPError):
        if 500 <= exc.code < 600:
            return True
        return exc.code in _RETRYABLE_4XX
    # URLError（含连接失败/超时）、OSError、TimeoutError 都是瞬时故障，值得重试。
    # 注意 HTTPError 是 URLError 的子类，必须先在上面判掉，否则 4xx 也会被重试。
    return isinstance(exc, (urllib.error.URLError, OSError, TimeoutError))


def _error_label(exc):
    """给错误起个便于排查的短名：HTTP 错误带上状态码。"""
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP " + str(exc.code)
    return type(exc).__name__


def _resolve_base_url():
    """优先读 .env.example 声明的 LLM_API_BASE，回退旧名，最后回退默认端点。

    不做"静默回落默认值"的直接读法：base_url 拿不到时如果悄悄指向
    api.openai.com，报出来的会是一个迷惑的鉴权错误，而不是"配置没读到"。
    """
    value = os.environ.get(_ENV_BASE_URL)
    if value:
        return value
    value = os.environ.get(_ENV_BASE_URL_LEGACY)
    if value:
        return value
    return _DEFAULT_BASE_URL


class LLMError(Exception):
    """LLM 调用或配置出错。"""


class OpenAILikeClient:
    """OpenAI 兼容端点的最小聊天客户端。"""

    def __init__(self, api_key=None, base_url=None, model=None,
                 timeout=None, max_retries=None, max_tokens=None):
        self.api_key = api_key if api_key is not None else os.environ.get(_ENV_API_KEY)
        self.base_url = base_url if base_url is not None else _resolve_base_url()
        self.model = model if model is not None else (os.environ.get(_ENV_MODEL) or _DEFAULT_MODEL)
        # 读法：显式传参优先，其次环境变量，最后回退到与 .env.example 同值的默认值。
        # 用 `or` 而不是 os.environ.get(name, default)：第二参只在**键缺失**时生效，
        # 键存在但值是空串会返回空串，于是 ".env 里写了 LLM_MODEL= 却没填" 会让
        # 请求体带上空模型名。base_url 同理（_resolve_base_url 已用 if value: 判空）；
        # api_key 留空则由 chat() 的 `if not self.api_key` 报出清晰错误。
        if timeout is None:
            timeout = env_int(_ENV_TIMEOUT, _DEFAULT_TIMEOUT)
        if max_retries is None:
            # 重试次数允许为 0（= 不重试），故 minimum=0
            max_retries = env_int(_ENV_MAX_RETRIES, _DEFAULT_MAX_RETRIES, minimum=0)
        self.timeout = timeout
        self.max_retries = max_retries
        # 默认 None = 请求体不带 max_tokens，交由端点自行决定上限
        self.max_tokens = max_tokens

    def chat(self, system, user):
        """返回模型生成的回答文本（str）。

        system / user 分别是系统提示与用户提示。模拟模式下返回占位答案。
        """
        if os.environ.get("LLM_MOCK") == "1":
            return self._mock(user)
        # A request-scoped profile overrides only provider/model credentials;
        # timeout, retry and token limits remain those of this client instance.
        from app.model_profiles import get_active_profile

        profile = get_active_profile()
        api_key = profile.api_key if profile else self.api_key
        if not api_key:
            raise LLMError("未配置所选模型的服务端 API 密钥；测试可设 LLM_MOCK=1")
        if profile:
            return self._call(
                system,
                user,
                api_key=profile.api_key,
                base_url=profile.base_url,
                model=profile.model,
            )
        return self._call(system, user)

    def _call(self, system, user, *, api_key=None, base_url=None, model=None):
        selected_key = self.api_key if api_key is None else api_key
        selected_base = self.base_url if base_url is None else base_url
        selected_model = self.model if model is None else model
        url = selected_base.rstrip("/") + "/chat/completions"
        payload = {
            "model": selected_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        # ensure_ascii=False：中文不转 \uXXXX，请求体体积约减半，国产端点尤其明显
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # Request 只是数据容器，urlopen 不改它，重试时可以复用同一个
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + selected_key)

        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8")
                break
            except Exception as exc:
                if not _is_retryable(exc):
                    # 鉴权失败、参数错、路径错这类，重试多少次都一样，立刻放弃
                    raise LLMError(
                        "调用 LLM 失败（" + _error_label(exc) + "，该错误重试无意义）: " + str(exc)
                    )
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
        else:
            raise LLMError(
                "调用 LLM 失败（已尝试 "
                + str(self.max_retries + 1)
                + " 次，末次 "
                + _error_label(last_exc)
                + "）: "
                + str(last_exc)
            )

        try:
            obj = json.loads(body)
            choices = obj["choices"]
            message = choices[0]["message"]
            content = message.get("content") or ""
            finish_reason = choices[0].get("finish_reason")
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError("LLM 返回解析失败: " + str(exc) + " body=" + body[:500])

        # 空正文必须报错，不能把空串当答案返回。
        # 起因（2026-10-03 实测火山方舟 DeepSeek-V4.1-Flash）：该模型带思维链，
        # reasoning_tokens 计入 max_tokens，给小了会「想一半就断」——此时 HTTP 200、
        # 无任何异常、content 为空串。若不拦，调用方会把空串当成「模型答了但什么都没说」
        # 继续往下走，属于静默失败（比报错更难排查）。
        if not content.strip():
            raise LLMError(
                "LLM 返回空内容（finish_reason="
                + str(finish_reason)
                + "）；若为 length 说明思维链吃满了 max_tokens，请调大 NL2SQL_MAX_TOKENS"
            )
        # 被 max_tokens 截断的正文同样是废答案（SQL 不完整 / 引用被切掉），一并报错。
        if finish_reason == "length":
            raise LLMError(
                "LLM 输出被 max_tokens 截断（finish_reason=length，正文长度 "
                + str(len(content))
                + "）；思维链计入 max_tokens，请调大 NL2SQL_MAX_TOKENS"
            )
        return content

    def _mock(self, user):
        # 占位实现：从 user 里抽取 [n] 开头的上下文行，拼成一句“依据上下文”的回答。
        # 仅用于无 key 环境下跑通链路与测试，不是真实生成。
        lines = user.splitlines()
        ctx = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and "]" in stripped:
                ctx.append(stripped)
        if len(ctx) == 0:
            return "（mock）未能从上下文中找到可依据的内容。"
        return "（mock 答案）依据以下片段作答：\n" + "\n".join(ctx)
