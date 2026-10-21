"""FastAPI 应用入口。

启动方式：
    uvicorn app.main:app --reload

路由：
    GET  /health                     探活
    GET  /api/models                 可选模型清单（只返回公开信息与配置状态）
    POST /api/ask                    提问（请求 / 响应形状由 app/contracts.py 定死）
    POST /api/ask/stream             提问并推送实际处理阶段（最终结构不变）
    GET  /api/eval/suites            评测套件清单（面板用）
    POST /api/eval/run               开一次评测，返回 SSE 进度流

评测接口是**演示通道**：判分口径复用 eval/runner.py 与 eval/metrics.py，
不在这里另立标准。正式报告仍由 `python eval/runner.py` 产出。

编排器在启动时装配一次，进程内复用。图谱后端由环境变量 `GRAPH_BACKEND`
决定（`mock` / `kag`）；文档检索后端由 `DOC_SEARCH_BACKEND` 决定
（`rag` / `mock`），切后端不需要改接口。
"""
from pathlib import Path
import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import time
from collections import defaultdict
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# 必须在 app.auth_store 之前：AuthStore 在 import 期就实例化，账号库路径
# 取决于 VERIMIND_AUTH_DB_PATH，而该键写在 .env 里。加载晚一步，进程就会
# 静默落到 %LOCALAPPDATA% 的默认空库上 —— 表现是"账号明明建过却登不上"。
from app.rag.config import load_env_file

load_env_file()

from app.auth_store import AuthError, AuthStore, AuthUser, SESSION_TTL_SECONDS
from app.contracts import (
    AskRequest,
    AskResponse,
    DatabaseSchemaResponse,
    DatabaseStatusResponse,
    ResultSetPage,
    UnstructuredDatabaseResponse,
    UnstructuredDatabaseStatusResponse,
)
from app.database_api import (
    build_database_schema,
    build_database_status,
    build_unstructured_database,
    build_unstructured_database_status,
)
from app.eval_api import EvalRunner, SUITES, progress_events, suite_catalog, tasks_evidence
from app.model_profiles import (
    get_default_profile_id,
    get_profile,
    model_profile_scope,
    public_profile_catalog,
)
from app.model_config_store import (
    ModelConfigError,
    editable_profile_catalog,
    save_profile_config,
)
from app.orchestrator import build_default_orchestrator

app = FastAPI(title="AskData Agent", version="0.2.0")
logger = logging.getLogger(__name__)
_auth_store = AuthStore()


#: 数据库不可用时给用户看的说明。原始异常是 psycopg 的
#: `OperationalError: the connection is lost`（英文、且完全不提示下一步做什么），
#: 直接冒到界面上是一行 500 红字。这里翻成人话，并点明最常见的成因 ——
#: 本项目数据库跑在 Docker 里，机器内存吃紧时容器被杀是常态。
_DB_UNAVAILABLE_MESSAGE = (
    "数据库暂时不可用，本次问答未完成。"
    "请确认数据库服务已启动（Docker Desktop 中的 ict-db 容器），然后重试。"
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """把「数据库连不上」这类环境故障翻译成可读提示。

    连接类故障（连接丢失 / 拒绝 / 超时）是**环境问题**而不是代码缺陷：
    数据库重启、容器被 OOM 杀掉、Docker 没起来都会触发，堆栈对用户毫无意义。
    其余异常仍按 500 处理，但同样不把原始堆栈丢给前端。

    ⚠️ `HTTPException` / `RequestValidationError` 必须原样交回框架处理。
    Starlette 按异常类的 MRO 逐级向上找最近的处理器，而 `Exception` 是所有
    异常的基类 —— 这里若不显式放行，401「请先登录」、403 来源校验失败、
    404 结果集过期这些**已经写好的**响应会被一并压成 500，登录门与全部
    业务错误提示同时失效。这是本处理器第一版真实踩到的坑（4 个既有接口
    测试因此由全绿转红）。
    """
    if isinstance(exc, (HTTPException, RequestValidationError)):
        raise exc
    if _is_database_unavailable(exc):
        logger.warning("数据库不可用，已返回友好提示：%s: %s", type(exc).__name__, exc)
        return JSONResponse(status_code=503, content={"detail": _DB_UNAVAILABLE_MESSAGE})
    logger.exception("未处理异常：%s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "服务内部错误，本次请求未完成。"})


def _is_database_unavailable(exc: BaseException) -> bool:
    """判断异常链里是否有连接类故障。

    沿 __cause__ / __context__ 往下找：修复后的向量库会重连一次，只有真的连不上
    才抛出来；此时最外层可能是包装过的异常，真正的原因是链里的 OperationalError。

    按**类名**匹配而不是 isinstance：psycopg 装了才 import 得到，而这个
    handler 必须在任何情况下都能跑（包括依赖缺失的环境）。
    """
    names = {
        "OperationalError",       # 连接丢失 / 拒绝 / 服务器关闭
        "InterfaceError",         # 连接对象已不可用
        "ConnectionException",    # psycopg 各连接类异常的共同基类
    }
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in names:
            return True
        current = current.__cause__ or current.__context__
    return False

_login_failures: dict[str, list[float]] = defaultdict(list)
_AUTH_COOKIE_NAME = "verimind_session"

#: 进程级单例。装配成本低，但保持单一实例便于后续接会话状态。
_orchestrator = build_default_orchestrator()

#: 评测运行器。单进程内同一时刻只允许一个评测在跑（见 app/eval_api.py 的说明）。
_eval_runner = EvalRunner()


def _request_user(request: Request) -> AuthUser:
    user = getattr(request.state, "auth_user", None)
    if not isinstance(user, AuthUser):
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _require_admin(request: Request) -> AuthUser:
    user = _request_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="此功能仅限管理员使用")
    return user


def _scoped_session_id(user: AuthUser, client_session_id: str) -> str:
    digest = hashlib.sha256(client_session_id.encode("utf-8")).hexdigest()
    return f"{user.id}:{digest}"


@app.middleware("http")
async def require_authenticated_api(request: Request, call_next):
    """默认保护 API 与前端构建资源；匿名者只收到无项目代码的登录页。"""
    path = request.url.path
    # 登录接口本身必须匿名可达：第一个 if 只覆盖 "/api/" 开头的其它路径，
    # 若这里不放行，请求会掉进下面的 elif 白名单并被判成「未登录」，形成死循环。
    if path == "/api/auth/login":
        return await call_next(request)
    if path.startswith("/api/"):
        user = _auth_store.resolve_session(request.cookies.get(_AUTH_COOKIE_NAME))
        if user is None:
            return JSONResponse(status_code=401, content={"detail": "请先登录"})
        request.state.auth_user = user
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            try:
                _verify_same_origin(request)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    elif path not in {"/", "/health", "/brand/verimind-mark.svg", "/api/auth/login"}:
        if _auth_store.resolve_session(request.cookies.get(_AUTH_COOKIE_NAME)) is None:
            return JSONResponse(status_code=401, content={"detail": "请先登录"})
    return await call_next(request)


def _verify_same_origin(request: Request) -> None:
    """Cookie 鉴权接口对写请求校验 Origin，避免跨站伪造请求。

    浏览器在**同源** POST 时按规范不带 `Origin` 头（只有跨源请求才带，Chrome 对
    Fetch 同源写请求也省略），因此「缺 Origin」不能直接当跨站拒绝——否则同源
    表单/前端请求会被误杀。这里改为：有 Origin 就逐字比对 Host；没有 Origin 就
    退回 `Sec-Fetch-Site`（浏览器强制携带、页面脚本无法伪造）判断是否同站。
    两者都缺失才拒绝，此时可能是非浏览器客户端，保守按可疑处理。
    """
    origin = request.headers.get("origin")
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    expected_host = forwarded_host or request.headers.get("host", "")
    if origin:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.netloc.casefold() != expected_host.casefold()
        ):
            raise HTTPException(status_code=403, detail="请求来源校验失败")
        return
    # 同源请求不带 Origin：用 Sec-Fetch-Site 兜底。same-origin/none 放行，
    # cross-site 明确拒绝，缺失（老浏览器或非浏览器客户端）按同源处理以保持
    # 对 CLI/测试客户端的兼容——真正的跨站攻击一定来自浏览器，会带上该头。
    fetch_site = request.headers.get("sec-fetch-site", "").strip().casefold()
    if fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="请求来源校验失败")


class AuthLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=48)
    password: str = Field(min_length=1, max_length=256)


class AuthUserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=48)
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "user"] = "user"


class AuthUserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool
    role: Literal["admin", "user"]


class AuthPasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=12, max_length=256)


class AuthPasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


def _auth_cookie_secure() -> bool:
    return os.environ.get("VERIMIND_AUTH_COOKIE_SECURE", "true").strip().lower() not in {"0", "false", "no"}


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        _AUTH_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_auth_cookie_secure(),
        samesite="lax",
        path="/",
    )


@app.post("/api/auth/login")
def auth_login(payload: AuthLoginRequest, request: Request, response: Response) -> dict[str, object]:
    _verify_same_origin(request)
    client_ip = request.client.host if request.client else "unknown"
    throttle_key = f"{client_ip}:{payload.username.strip().casefold()}"
    now = time.monotonic()
    attempts = [stamp for stamp in _login_failures[throttle_key] if now - stamp < 15 * 60]
    _login_failures[throttle_key] = attempts
    if len(attempts) >= 8:
        raise HTTPException(status_code=429, detail="登录尝试过多，请 15 分钟后再试")
    user = _auth_store.authenticate(payload.username, payload.password)
    if user is None:
        attempts.append(now)
        _login_failures[throttle_key] = attempts
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    _login_failures.pop(throttle_key, None)
    _set_auth_cookie(response, _auth_store.create_session(user.id))
    return {"user": user.public_dict()}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, object]:
    return {"user": _request_user(request).public_dict()}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response) -> dict[str, bool]:
    _verify_same_origin(request)
    _auth_store.revoke_session(request.cookies.get(_AUTH_COOKIE_NAME))
    response.delete_cookie(
        _AUTH_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=_auth_cookie_secure(),
        samesite="lax",
    )
    return {"ok": True}


@app.post("/api/auth/password")
def auth_change_password(
    payload: AuthPasswordChangeRequest, request: Request, response: Response
) -> dict[str, object]:
    _verify_same_origin(request)
    user = _request_user(request)
    try:
        token = _auth_store.change_password(user.id, payload.current_password, payload.new_password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_auth_cookie(response, token)
    return {"ok": True}


@app.get("/api/admin/users")
def admin_list_users(request: Request) -> dict[str, object]:
    _require_admin(request)
    return {"users": [user.public_dict() for user in _auth_store.list_users()]}


@app.post("/api/admin/users")
def admin_create_user(payload: AuthUserCreateRequest, request: Request) -> dict[str, object]:
    _verify_same_origin(request)
    _require_admin(request)
    try:
        user = _auth_store.create_user(payload.username, payload.password, payload.role)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": user.public_dict()}


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: str, payload: AuthUserUpdateRequest, request: Request
) -> dict[str, object]:
    _verify_same_origin(request)
    actor = _require_admin(request)
    try:
        user = _auth_store.set_user(
            user_id, active=payload.active, role=payload.role, actor_id=actor.id
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": user.public_dict()}


@app.post("/api/admin/users/{user_id}/password")
def admin_reset_password(
    user_id: str, payload: AuthPasswordResetRequest, request: Request
) -> dict[str, bool]:
    _verify_same_origin(request)
    actor = _require_admin(request)
    if user_id == actor.id:
        raise HTTPException(status_code=400, detail="请使用账号面板中的修改密码功能")
    try:
        _auth_store.reset_password(user_id, payload.password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/capabilities/cross-source-demo")
def cross_source_demo_capability(request: Request) -> dict[str, object]:
    """Report whether the isolated synthetic work-order/contract route is usable."""
    _request_user(request)
    tool = _orchestrator.registry.get("cross_source_demo")
    if tool is None:
        return {
            "status": "disabled",
            "ready": False,
            "reason_code": "demo_route_disabled",
        }

    readiness = getattr(tool, "readiness", None)
    if not callable(readiness):
        return {
            "status": "unavailable",
            "ready": False,
            "reason_code": "readiness_probe_unavailable",
        }
    try:
        ready, reason_code = readiness()
    except Exception:  # noqa: BLE001 - never disclose local paths, DSNs, or DB errors
        ready, reason_code = False, "demo_readiness_check_failed"
    return {
        "status": "ready" if ready else "unavailable",
        "ready": bool(ready),
        "reason_code": reason_code,
    }


@app.get("/api/kag/status")
def kag_status(request: Request) -> dict[str, object]:
    """Return safe readiness for the user-selected optional KAG deep route."""
    _request_user(request)
    try:
        from app.kag_supplement import KagProcessSupplementer

        supplementer = KagProcessSupplementer.from_env()
        if supplementer is None:
            return {"ready": False, "status": "not_configured", "reason_code": "runtime_not_configured"}
        ready, reason = supplementer.readiness()
        return {
            "ready": ready,
            "status": "ready" if ready else "unavailable",
            "reason_code": reason,
        }
    except Exception:  # noqa: BLE001 - never expose local paths or credentials
        return {"ready": False, "status": "unavailable", "reason_code": "configuration_invalid"}


@app.get("/api/database/status", response_model=DatabaseStatusResponse)
def database_status(request: Request) -> DatabaseStatusResponse:
    """Return live database connectivity and safe schema-catalog identity."""
    try:
        status = build_database_status()
        if _request_user(request).role == "admin":
            return status
        return DatabaseStatusResponse(
            status="connected" if status.status in {"connected", "catalog_mismatch"} else "unavailable",
            database=None,
            catalog_database=None,
            label="结构化数据库",
            dialect="unknown",
            table_count=0,
            checked_at=status.checked_at,
        )
    except Exception:  # noqa: BLE001 - do not expose filesystem paths or secrets
        raise HTTPException(status_code=503, detail="数据库结构目录暂不可用")


@app.get("/api/database/schema", response_model=DatabaseSchemaResponse)
def database_schema(request: Request) -> DatabaseSchemaResponse:
    """Return the active read-only table and column catalog."""
    _require_admin(request)
    try:
        return build_database_schema()
    except Exception:  # noqa: BLE001 - do not expose filesystem paths or secrets
        raise HTTPException(status_code=503, detail="数据库结构目录暂不可用")


@app.get("/api/database/unstructured", response_model=UnstructuredDatabaseResponse)
def unstructured_database(request: Request) -> UnstructuredDatabaseResponse:
    """Return live RAG-store status, safe vector-table schema, and corpus inventory."""
    _require_admin(request)
    try:
        return build_unstructured_database(_orchestrator)
    except Exception:  # noqa: BLE001 - do not expose local paths or database details
        raise HTTPException(status_code=503, detail="文档向量库信息暂不可用")


@app.get(
    "/api/database/unstructured/status",
    response_model=UnstructuredDatabaseStatusResponse,
)
def unstructured_database_status(request: Request) -> UnstructuredDatabaseStatusResponse:
    """Return only the connection status of the active document search store."""
    try:
        status = build_unstructured_database_status(_orchestrator)
        if _request_user(request).role == "admin":
            return status
        return UnstructuredDatabaseStatusResponse(
            status="connected" if status.status == "connected" else "unavailable",
            backend="mock",
            label="文档数据库",
            checked_at=status.checked_at,
        )
    except Exception:  # noqa: BLE001 - keep local paths and configuration private
        raise HTTPException(status_code=503, detail="文档向量库状态暂不可用")


@app.get("/api/models")
def model_catalog() -> dict:
    """Return safe model labels and configuration state; never return credentials."""
    return {
        "default_model_id": get_default_profile_id(),
        "models": public_profile_catalog(),
    }


class ModelProfileConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=160)
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)


@app.get("/api/model-config")
def model_config_catalog(request: Request) -> dict[str, object]:
    """Return editable model settings and key-presence flags, never credentials."""
    _require_admin(request)
    return {"profiles": editable_profile_catalog()}


@app.put("/api/model-config/{profile_id}")
async def update_model_config(profile_id: str, request: Request) -> dict[str, object]:
    """Persist a supported model profile; validation errors never echo API keys."""
    _verify_same_origin(request)
    _require_admin(request)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="模型配置请求格式错误") from exc
    try:
        payload = ModelProfileConfigUpdate.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="模型配置格式不正确；密钥内容不会回显") from exc
    try:
        saved = save_profile_config(
            profile_id,
            model=payload.model,
            base_url=payload.base_url,
            api_key=payload.api_key,
        )
    except ModelConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"profile": saved}


def _requested_model_profile(request: Request) -> str | None:
    profile_id = request.headers.get("x-model-profile")
    if not profile_id:
        return None
    profile = get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=400, detail="不支持所选模型")
    if not profile.configured:
        raise HTTPException(status_code=400, detail="所选模型尚未在后端配置凭据")
    return profile_id


def _requested_fact_review(request: Request) -> bool:
    """Negotiate the additive response field without changing legacy JSON keys."""
    version = request.headers.get("x-askdata-fact-review")
    if version is None:
        return False
    if version != "2":
        raise HTTPException(status_code=400, detail="不支持的事实核验响应版本")
    return True


def _ask_response_payload(response: AskResponse, *, include_fact_review: bool) -> dict:
    payload = response.model_dump(mode="json")
    if not include_fact_review or response.fact_review is None:
        payload.pop("fact_review", None)
    if not include_fact_review:
        # Query-result locators are part of the v2 opt-in contract. Keep them
        # out of responses to existing clients that did not negotiate v2.
        for ref in payload.get("refs", []):
            if isinstance(ref, dict):
                ref.pop("result_id", None)
        for step in payload.get("trace", []):
            if not isinstance(step, dict):
                continue
            for ref in step.get("hit") or []:
                if isinstance(ref, dict):
                    ref.pop("result_id", None)
    return payload


@app.post("/api/ask", response_model=AskResponse)
def ask(req: AskRequest, request: Request) -> JSONResponse:
    """提问接口。

    响应里的 `refs` 由编排层从全部工具调用的命中里汇总去重，
    不手工拼装 —— 保证契约不变量（refs == 所有 trace.hit 的并集去重）。
    """
    user = _request_user(request)
    profile_id = _requested_model_profile(request)
    include_fact_review = _requested_fact_review(request)
    try:
        with model_profile_scope(profile_id):
            answer_options = {"retrieval_mode": req.retrieval_mode} if req.retrieval_mode != "standard" else {}
            response = _orchestrator.answer(
                req.question,
                _scoped_session_id(user, req.session_id),
                **answer_options,
            )
            return JSONResponse(content=_ask_response_payload(
                response, include_fact_review=include_fact_review
            ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/ask/stream")
async def ask_stream(req: AskRequest, request: Request) -> StreamingResponse:
    """交互式问答进度流；最终回答仍使用既有 AskResponse 字段。"""
    user = _request_user(request)
    profile_id = _requested_model_profile(request)
    scoped_session_id = _scoped_session_id(user, req.session_id)
    include_fact_review = _requested_fact_review(request)

    async def stream():
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[dict] = asyncio.Queue()

        def publish(event: dict) -> None:
            try:
                loop.call_soon_threadsafe(events.put_nowait, event)
            except RuntimeError:
                # 客户端断开或事件循环关闭时，进度输出不得影响执行中的问答。
                pass

        async def run_answer() -> None:
            try:
                def answer_with_profile() -> AskResponse:
                    with model_profile_scope(profile_id):
                        answer_options = {"retrieval_mode": req.retrieval_mode} if req.retrieval_mode != "standard" else {}
                        return _orchestrator.answer(
                            req.question,
                            scoped_session_id,
                            **answer_options,
                            progress_callback=publish,
                        )

                response = await asyncio.to_thread(answer_with_profile)
                publish({"type": "complete", "response": _ask_response_payload(
                    response, include_fact_review=include_fact_review
                )})
            except Exception:
                logger.exception("流式问答处理失败")
                publish({"type": "error", "message": "服务处理未完成，请检查后端状态后重试。"})
            finally:
                publish({"type": "end"})

        worker = asyncio.create_task(run_answer())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(events.get(), timeout=12.0)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keep-alive\n\n"
                    continue
                yield "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"
                if event.get("type") == "end":
                    break
        finally:
            if not worker.done():
                # 当前问答运行在同步工具链中，断开连接不安全地中断线程；
                # 它会完成原有工作，但不会再改变回答契约或对其他会话加锁。
                worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/tasks/evidence")
def tasks_evidence_route(request: Request) -> dict:
    """中级任务能力面板的数据源。

    与 `/api/eval/suites` 的区别：那个是「现在能跑什么」，这个是「已经跑出了什么」。
    中级任务的证据分散在各任务的专项报告里、口径不统一，无法现场重跑，
    所以做成静态台账接口，只读 `infra/tasks_evidence.json`。
    """
    _require_admin(request)
    return tasks_evidence()


@app.get("/api/eval/suites")
def eval_suites(request: Request) -> dict:
    """评测面板的套件清单：有哪些套件、各多少题、对应评分表哪一项。

    单独做一个只读接口，是让面板先渲染出「可跑什么、共多少题」，
    而不是点下去才知道 —— 演示时能少一次等待。

    `running` 是**后端视角**的真实状态，不是前端的。这是修「点了停止就再也
    开不了评测」的关键：停止只断开了 SSE 连接，后台线程还在跑，之前界面
    只看自己本地的 running，于是以为结束了、又能点开始，而后端一律 409。
    现在界面轮询这个字段，两边看到的是同一个事实。
    """
    _require_admin(request)
    state = _eval_runner.current()
    current = None
    if state is not None:
        current = {
            "run_id": state.run_id,
            "suite": state.suite,
            "total": state.total,
            "done": state.done,
        }
    return {"suites": suite_catalog(), "running": _eval_runner.is_busy(), "current": current}


@app.post("/api/eval/cancel")
def eval_cancel(request: Request, run_id: str | None = Query(default=None, description="要停的那一轮；不传则停当前轮")) -> dict:
    """请后台停下来。**立即返回，不等它真的停。**

    为什么不等待：线程要跑完当前这题才会退出，可能十几秒。挂在这儿等会让
    HTTP 请求一直悬着，界面也无从区分"正在停"和"卡住了"。真实状态由
    `/api/eval/suites` 的 `running` 反映，界面轮询它到变 false 为止。

    传 `run_id` 是为了防止误停：界面点停止到请求到达之间，上一轮可能刚好
    跑完、下一轮已经开始，不核对 id 就会把刚开的那轮一起停掉。
    """
    _verify_same_origin(request)
    _require_admin(request)
    ok = _eval_runner.cancel(run_id)
    return {"requested": ok, "running": _eval_runner.is_busy()}


@app.post("/api/eval/run")
def eval_run(request: Request, suite: str = Query(description="套件标识，取自 /api/eval/suites")) -> StreamingResponse:
    """开一次评测并以 SSE 推进度。

    事件类型（每帧 `data: {json}\\n\\n`）：
        `case`       单题跑完，带答案、耗时、判分理由与三个子条件明细
        `progress`   心跳，长时间单题期间保活
        `done`       汇总（总数 / 通过数 / 准确率 / 三档目标 / 分难度）
        `cancelled`  被用户停止，没有汇总
        `error`      致命错误，流随即结束

    为什么用 SSE 而不是轮询：与 `/api/ask` 的推送方式一致，前端已有解析器，
    且评测单题可能十几秒，轮询要么太密要么显示滞后。

    注意：**客户端断开这条流不会让后端停下**，要停得显式调
    `/api/eval/cancel`。断开只是不再接收事件，后台线程照跑 —— 这正是
    「点了停止就再也开不了评测」的成因。
    """
    _verify_same_origin(request)
    _require_admin(request)
    try:
        state = _eval_runner.start(suite)
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StreamingResponse(
        progress_events(state),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/results/{result_id}", response_model=ResultSetPage)
def result_page(
    result_id: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    page_size: int = Query(default=100, ge=1, le=200),
    session_id: str = Header(alias="X-Session-ID", min_length=1),
) -> ResultSetPage:
    """Read one page from a short-lived server-side result set."""
    user = _request_user(request)
    page = _orchestrator.result_set_store.page(
        result_id,
        offset=offset,
        page_size=page_size,
        session_id=_scoped_session_id(user, session_id),
    )
    if page is None:
        raise HTTPException(status_code=404, detail="结果集不存在或已过期，请重新提问")
    return page


@app.get("/api/results/{result_id}/export.csv")
def export_result_csv(
    result_id: str,
    request: Request,
    session_id: str = Header(alias="X-Session-ID", min_length=1),
) -> StreamingResponse:
    """Export only complete result sets; partial rows must not masquerade as full data."""
    user = _request_user(request)
    scoped_session_id = _scoped_session_id(user, session_id)
    first_page = _orchestrator.result_set_store.page(
        result_id, offset=0, page_size=1, session_id=scoped_session_id,
    )
    if first_page is None:
        raise HTTPException(status_code=404, detail="结果集不存在或已过期，请重新提问")
    if not first_page.complete:
        raise HTTPException(status_code=409, detail="结果集不完整，拒绝生成完整 CSV")
    payload = _orchestrator.result_set_store.csv_bytes(result_id, session_id=scoped_session_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="结果集不存在或已过期，请重新提问")
    return StreamingResponse(
        iter([payload]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="query-result.csv"'},
    )


_RAW_CORPUS_DIR = Path(__file__).resolve().parents[1] / "data" / "corpus" / "raw"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SUPPORTED_DOCUMENT_EXTS = {".pdf", ".docx", ".md", ".txt"}


def _document_path(doc_id: str) -> Path | None:
    """只按语料文件 stem 查找受支持的原文，不接受目录或扩展名输入。"""
    if not _RAW_CORPUS_DIR.is_dir():
        return None
    for candidate in _RAW_CORPUS_DIR.iterdir():
        if (
            candidate.is_file()
            and candidate.suffix.lower() in _SUPPORTED_DOCUMENT_EXTS
            and candidate.stem == doc_id
        ):
            return candidate
    return None


@app.get("/api/documents/{doc_id}/file", response_class=FileResponse)
def document_file(doc_id: str) -> FileResponse:
    """按稳定 doc_id 返回语料目录中的原始文件，只允许四种已解析格式。"""
    candidate = _document_path(doc_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    media_type = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
    }[candidate.suffix.lower()]
    disposition = "attachment" if candidate.suffix.lower() == ".docx" else "inline"
    return FileResponse(
        candidate,
        media_type=media_type,
        filename=candidate.name,
        content_disposition_type=disposition,
    )


@app.get("/api/documents/{doc_id}/preview")
def document_preview(doc_id: str) -> dict[str, str]:
    """给证据面板返回可读原文；PDF 仍由现有阅读器按页渲染。"""
    candidate = _document_path(doc_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if candidate.suffix.lower() == ".pdf":
        return {"kind": "pdf", "file_name": candidate.name}

    try:
        if candidate.suffix.lower() in {".md", ".txt"}:
            content = candidate.read_text(encoding="utf-8-sig", errors="replace")
        else:
            # DOCX 与索引构建共用同一解析器，预览内容与问答检索文本保持一致。
            from app.rag.ingest.loader import extract_blocks

            content = "\n".join(block.text for block in extract_blocks(str(candidate)))
    except Exception as exc:  # noqa: BLE001 - 单份文档解析失败不能泄露服务器路径
        logger.exception("原文预览解析失败：%s", doc_id)
        raise HTTPException(status_code=500, detail="原文解析失败") from exc
    return {"kind": "text", "file_name": candidate.name, "content": content}


def _registered_evidence_paths() -> set[str]:
    """只开放中级任务台账和评测套件明示登记的证据文件。"""
    paths: set[str] = set()
    ledger = tasks_evidence()
    for task in ledger.get("tasks", []):
        for item in task.get("evidence", []):
            relative = item.get("path")
            if isinstance(relative, str):
                paths.add(relative)
    paths.update(f"eval/cases/{suite['file']}" for suite in SUITES.values())
    return paths


@app.get("/api/evidence/file")
def evidence_file(
    request: Request,
    path: str = Query(description="证据台账登记的仓库相对路径"),
) -> Response:
    """只读打开已登记的文本证据；公开用例仅返回题目，不返回参考答案。"""
    _require_admin(request)
    if path not in _registered_evidence_paths():
        raise HTTPException(status_code=404, detail="证据不存在或未登记")
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in path:
        raise HTTPException(status_code=404, detail="证据不存在或未登记")
    try:
        resolved = (_PROJECT_ROOT / relative).resolve(strict=True)
        resolved.relative_to(_PROJECT_ROOT.resolve())
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=404, detail="证据不存在或未登记") from None
    if not resolved.is_file() or resolved.suffix.lower() not in {".md", ".txt", ".py", ".jsonl"}:
        raise HTTPException(status_code=404, detail="证据不存在或未登记")
    if resolved.stat().st_size > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="证据文件过大，无法在线预览")
    try:
        content = resolved.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise HTTPException(status_code=500, detail="证据读取失败") from exc

    if relative.parts[:2] == ("eval", "cases") and resolved.suffix.lower() == ".jsonl":
        public_cases = []
        try:
            for line in content.splitlines():
                if not line.strip():
                    continue
                case = json.loads(line)
                public_cases.append({
                    key: case[key]
                    for key in ("id", "question", "difficulty")
                    if key in case
                })
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(status_code=500, detail="评测用例文件格式错误") from exc
        return JSONResponse(
            content={
                "notice": "仅展示题目；参考 SQL、判分要点和其他标准答案字段已隐藏。",
                "cases": public_cases,
            },
            headers={"X-Content-Type-Options": "nosniff"},
        )
    return PlainTextResponse(
        content,
        headers={"X-Content-Type-Options": "nosniff"},
    )


# Docker 构建会把前端产物放在这里。源码开发时目录不存在，仍由 Vite 代理 API。
# 静态挂载位于 API 路由之后，避免将 /api/ask 当作前端资源处理。
frontend_dist = Path(__file__).resolve().parents[1] / "web" / "dist"
if frontend_dist.is_dir():
    # Windows 注册表可能将 .mjs 映射为 text/plain，浏览器会拒绝加载
    # PDF 模块工作线程；显式设置静态 JavaScript 模块的内容类型。
    mimetypes.add_type("text/javascript", ".mjs")

    @app.get("/", include_in_schema=False)
    def app_home(request: Request) -> Response:
        if _auth_store.resolve_session(request.cookies.get(_AUTH_COOKIE_NAME)) is not None:
            return FileResponse(frontend_dist / "index.html", headers={"Cache-Control": "no-store"})
        return HTMLResponse(
            content=_PUBLIC_LOGIN_HTML,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            },
        )

    @app.get("/brand/verimind-mark.svg", include_in_schema=False)
    def public_login_mark() -> Response:
        mark = frontend_dist / "brand" / "verimind-mark.svg"
        if not mark.is_file():
            raise HTTPException(status_code=404, detail="品牌图标不存在")
        return FileResponse(mark, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})

    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")


_PUBLIC_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>登录 · 知证 VeriMind</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#f5f7fa;color:#1d2b40;font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.card{width:min(420px,100%);padding:38px;border:1px solid #e4e9f0;border-radius:22px;background:#fff;box-shadow:0 18px 60px rgba(35,59,102,.12)}
.mark{display:block;width:54px;height:54px;margin:0 auto 20px}.eyebrow{margin:0 0 8px;color:#315bc7;font-size:10px;font-weight:750;letter-spacing:.18em;text-align:center}
h1{margin:0;color:#233b66;font-size:25px;text-align:center}.sub{margin:9px 0 26px;color:#637086;font-size:13px;text-align:center}
form{display:grid;gap:16px}label{display:grid;gap:7px;color:#637086;font-size:12px;font-weight:650}input{width:100%;padding:12px 13px;border:1px solid #e4e9f0;border-radius:10px;outline:0;color:#1d2b40;background:#fff;font:inherit;font-size:14px}input:focus{border-color:#8da9e4;box-shadow:0 0 0 3px #edf2ff}
button{min-height:44px;border:0;border-radius:10px;color:#fff;background:#315bc7;font:inherit;font-size:14px;font-weight:700;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.error{min-height:0;margin:0;color:#c8452f;font-size:12px}.foot{margin:22px 0 0;color:#75849a;font-size:11px;text-align:center}
@media(prefers-color-scheme:dark){body{background:#141922;color:#f2f5fa}.card{border-color:#303a4b;background:#1d2430}h1{color:#f2f5fa}.sub,label{color:#bac5d6}input{border-color:#303a4b;color:#f2f5fa;background:#151b25}.foot{color:#93a1b6}}
</style></head><body><main class="card"><img class="mark" src="/brand/verimind-mark.svg" alt="知证 VeriMind"><p class="eyebrow">SECURE WORKSPACE</p><h1>登录知证</h1><p class="sub">使用管理员分配的账号进入问答工作台。</p>
<form id="login"><label>用户名<input name="username" autocomplete="username" minlength="3" maxlength="48" required autofocus></label><label>密码<input name="password" type="password" autocomplete="current-password" maxlength="256" required></label><p id="error" class="error" role="alert"></p><button id="submit" type="submit">安全登录</button></form><p class="foot">账号由系统管理员创建 · 登录会话有效期 12 小时</p></main>
<script>const form=document.querySelector('#login'),button=document.querySelector('#submit'),error=document.querySelector('#error');form.addEventListener('submit',async event=>{event.preventDefault();button.disabled=true;button.textContent='正在验证…';error.textContent='';const data=new FormData(form);try{const response=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:data.get('username'),password:data.get('password')})});if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail||'登录失败，请重试。')}location.replace('/')}catch(reason){error.textContent=reason instanceof Error?reason.message:'无法连接登录服务。';button.disabled=false;button.textContent='安全登录'}})</script></body></html>"""
