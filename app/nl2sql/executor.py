"""executor.py - 真正连库执行那条**已通过校验**的 SQL。

**它在链路里的位置**：generator 产出 → validator 放行 → 才轮到本模块执行。
本模块**不做 SQL 校验**（那是 validator 的活），但会再加两道数据库侧的安全带，
因为"代码里判过只读"和"数据库真的拒绝写"是两件事，后者才是最后防线。

**三道防线的作用点**（缺一不可）：

    | 防线 | 位置 | 挡什么 |
    |---|---|---|
    | sqlglot 静态校验（只放明 SELECT） | validator.py | 99% 的写操作，且给出可回灌模型的错误信息 |
    | 连接设为只读（`read_only=True`） | 本模块 | 静态校验漏掉的写操作（数据库自己拒绝） |
    | 语句超时（`statement_timeout`） | 本模块 | 一条笛卡尔积把库拖死 |

**三个工程决定**：

    ① **每次执行开一条新连接、用完就关**。不做连接池。
       理由：本链路是"一次问答执行一次查询"的形态，连接复用的收益很小；
       而池化会带来"连接失效/事务残留"这类难查的问题（上一次没提交干净，
       下一次莫名其妙报错）。等真的做高并发（前端多会话并发问数）再引入池化，
       届时也应当由 `app/` 层统一提供，而不是本模块自己藏一个池。
    ② **结果值在这里做 JSON 化归一**。`Decimal`、`datetime`、`UUID` 这些类型
       不能直接被 FastAPI 序列化成 JSON（会报序列化错误）。放在执行层归一，
       trace、前端、评测脚本拿到的是同一份可序列化数据，不用各自兜一遍。
    ③ **报错不抛异常，收进 `ExecutionResult.error`**。查询写错（列不存在、
       除零、超时）是**正常业务分支**，要能回灌给模型做修复重试；
       让它冒到 HTTP 层只会变成一个 500，用户什么也看不到。
    ④ **同时带出 `sqlstate`**（数据库协议里的标准五字符错误码，如 42703 = 字段不存在）。
       上层靠它判"这个错值不值得回灌给模型重写"（见 pipeline.is_retryable_execution_error）：
       按错误码判比按错误文案做包含匹配可靠 —— 文案会随 PostgreSQL 版本和语言变，码不会。
"""

from __future__ import annotations

import datetime
import json
import time
from uuid import uuid4
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from app.nl2sql.config import env_int, env_str, get_dsn


@dataclass
class ExecutionResult:
    """一次查询执行的结果。

    Attributes:
        rows: 结果行，每行是「列名 -> 值」的字典。值已做 JSON 化归一。
        columns: 结果列名（按 SELECT 里的顺序）。空结果集**也有列名**，别拿它判成败。
        row_count: 行数（= len(rows)）。
        elapsed_ms: 执行耗时（毫秒，含连接建立）。
        error: 失败原因（一句话人话）。None 表示这条查询执行成功了。
        sqlstate: 数据库返回的标准错误码；成功时与连接层故障时都是 None。
    """

    rows: list[dict] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    elapsed_ms: float = 0.0
    error: str | None = None
    sqlstate: str | None = None
    result_truncated: bool = False


class SqlExecutor:
    """业务库的只读执行器。

    只做一件事：把一条**已经校验过**的 SQL 丢给 PostgreSQL，取回结果行。
    """

    def __init__(
        self,
        dsn=None,
        *,
        timeout_seconds=None,
        readonly=None,
        connect_timeout_seconds=10,
    ):
        """
        Args:
            dsn: 连接串。None 时按 DATABASE_URL / POSTGRES_* 推导（见 config.get_dsn）。
            timeout_seconds: 语句超时秒数。None 时读 `SQL_TIMEOUT_SECONDS`（默认 30）。
            readonly: 是否把连接设为只读。None 时读 `SQL_READONLY_ONLY`（默认 true）。
                **关掉它只为排查问题**（比如临时验证一条 DDL），不要写进 .env 长期用。
            connect_timeout_seconds: 建连超时。库没起时不至于让请求挂到天荒地老。
        """
        self.dsn = dsn
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None
            else env_int("SQL_TIMEOUT_SECONDS", 30)
        )
        self.readonly = (
            readonly if readonly is not None
            else env_str("SQL_READONLY_ONLY", "true").strip().lower()
            in ("1", "true", "yes", "on")
        )
        self.connect_timeout_seconds = connect_timeout_seconds

    # ── 对外主入口 ─────────────────────────────────────────────────────
    def execute(self, sql, *, max_result_rows=None, max_result_bytes=None):
        """执行一条只读 SQL。**不抛异常**。"""
        text = (sql or "").strip()
        if not text:
            return ExecutionResult(error="SQL 为空，未执行")

        t0 = time.time()
        conn = None
        try:
            psycopg, dict_row = _require_psycopg()
        except RuntimeError as exc:
            return ExecutionResult(elapsed_ms=_ms(t0), error=str(exc))

        try:
            conn = psycopg.connect(
                self.dsn or get_dsn(),
                connect_timeout=self.connect_timeout_seconds,
                # 自动提交关掉：psycopg 默认开事务，只读连接跑完回滚即可，
                # 不会留下任何需要清理的状态。
                autocommit=False,
            )
            if self.readonly:
                # ⚠️ 必须在**任何语句之前**设置：psycopg 在下一次事务开始时才应用它，
                #    先跑了语句再设，这条语句就已经在读写模式下执行了。
                conn.read_only = True

            # 语句超时。用 set_config 而不是 `SET statement_timeout = %s`：
            # SET 不接受占位符参数，拼接又会被 SQL 注入的口子带进来。
            #
            # row_factory=dict_row：让 psycopg 直接把每行给成「列名 -> 值」的字典。
            # 不设它的话拿到的是普通元组，还得自己拿 cur.description 去 zip —— 
            # 那种写法在结果里有同名列时（JOIN 未起别名）会静默错位。
            with conn.cursor() as settings_cursor:
                settings_cursor.execute(
                    "SELECT set_config('statement_timeout', %s, false)",
                    (str(int(self.timeout_seconds * 1000)),),
                )
            if max_result_rows is not None or max_result_bytes is not None:
                # A named cursor fetches in batches from PostgreSQL instead of
                # materializing the entire server result in the client first.
                cursor_context = conn.cursor(
                    name="bounded_" + uuid4().hex,
                    row_factory=dict_row,
                )
            else:
                cursor_context = conn.cursor(row_factory=dict_row)
            with cursor_context as cur:
                cur.execute(text)
                columns = [d.name for d in (cur.description or [])]
                result_truncated = False
                if not columns:
                    raw_rows = []
                elif max_result_rows is None and max_result_bytes is None:
                    raw_rows = cur.fetchall()
                else:
                    raw_rows = []
                    total_bytes = 0
                    while True:
                        batch = cur.fetchmany(25)
                        if not batch:
                            break
                        stop = False
                        for row in batch:
                            if max_result_rows is not None and len(raw_rows) >= max_result_rows:
                                result_truncated = True
                                stop = True
                                break
                            normalized = _normalize_row(dict(row))
                            row_size = len(
                                json.dumps(
                                    normalized,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    default=str,
                                ).encode("utf-8")
                            )
                            if max_result_bytes is not None and total_bytes + row_size > max_result_bytes:
                                result_truncated = True
                                stop = True
                                break
                            raw_rows.append(normalized)
                            total_bytes += row_size
                        if stop:
                            break
            conn.rollback()
        except Exception as exc:  # noqa: BLE001 —— 查询报错是业务分支，不是程序崩溃
            return ExecutionResult(
                elapsed_ms=_ms(t0),
                error="SQL 执行失败：" + _short(exc),
                sqlstate=_sqlstate_of(exc),
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 —— 关连接失败不该覆盖上面的结果
                    pass

        rows = (
            raw_rows
            if max_result_rows is not None or max_result_bytes is not None
            else [_normalize_row(dict(r)) for r in raw_rows]
        )
        return ExecutionResult(
            rows=rows,
            columns=columns,
            row_count=len(rows),
            elapsed_ms=_ms(t0),
            result_truncated=result_truncated,
        )

    def ping(self):
        """探活：返回 PostgreSQL 版本号。库不可用时抛异常。

        给验收脚本与"起库之后先确认能连"的场合用 —— 与 `execute` 不同，
        这里**故意让异常冒出来**：探活失败就是配置/环境有问题，不该被静默吞掉。
        """
        psycopg, _ = _require_psycopg()
        with psycopg.connect(self.dsn or get_dsn(), connect_timeout=self.connect_timeout_seconds) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                return cur.fetchone()[0]

    def database_name(self) -> str:
        """Return the connected database name using an explicitly read-only connection."""
        psycopg, _ = _require_psycopg()
        with psycopg.connect(
            self.dsn or get_dsn(),
            connect_timeout=self.connect_timeout_seconds,
            autocommit=False,
        ) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                row = cur.fetchone()
                if not row or not row[0]:
                    raise RuntimeError("数据库未返回当前库名")
                return str(row[0])


def _require_psycopg():
    """惰性导入 psycopg。

    为什么不在文件顶部 import：本模块是 `app/tools/nl2sql.py` 的传递依赖，
    而工具注册发生在**服务启动时**。若把 psycopg 放顶部，一个"没装驱动"的环境
    会在启动阶段直接崩掉整条服务，而不是在真正要执行查询时报一句清楚的错。
    """
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise RuntimeError(
            "未安装 psycopg（PostgreSQL 驱动），无法执行 SQL。"
            "请在项目虚拟环境里安装：uv sync（或 pip install 'psycopg[binary]'）"
        ) from exc
    return psycopg, dict_row


def _normalize_row(row):
    """把结果行里的值归一成可 JSON 序列化的形态。"""
    return {key: _normalize_value(value) for key, value in row.items()}


def _normalize_value(value):
    """单个值的归一。

    `Decimal` 转 float 是**有损**的，这里明知有损仍然转：
      - 契约里 `ToolResult.data` 要过 JSON 序列化（HTTP 响应用），Decimal 过不去；
      - 精度损失只影响**展示**，评测口径里比的是数据库执行出来的结果集本身
        （且金额另允 1 分钱容差），所以不会把判分判错。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        # 二进制列（本项目业务库里没有，但别让它把整条响应打挂）
        return "0x" + bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _normalize_value(v) for k, v in value.items()}
    return str(value)


def _sqlstate_of(exc):
    """取异常上的 SQLSTATE；取不到返回 None。

    为什么不用字符串从错误文案里抠：psycopg 的异常自带 `.sqlstate`，那是数据库
    在协议里回给我们的原值。文案拼接（"column x does not exist"）随版本与语言变，
    拿它做包含匹配迟早会失效。

    **连接层故障实测没有这个属性**（连不上、握手失败、认证超时时 `sqlstate` 为 None）
    —— 而这恰恰是最不该重试的一类，所以 None 在判据里是"不重试"。
    """
    code = getattr(exc, "sqlstate", None)
    if not code:
        return None
    text = str(code).strip()
    return text or None


def _short(exc, limit=300):
    text = str(exc).strip().replace("\n", " ")
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _ms(t0):
    return round((time.time() - t0) * 1000.0, 3)


__all__ = ["ExecutionResult", "SqlExecutor"]
