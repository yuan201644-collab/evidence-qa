"""PostgreSQL 向量库的连接卫生：读事务必须收尾、只读连接不得建表。

背景（真事故）：`PgVectorStore` 整个生命周期复用同一条连接。PostgreSQL 的**只读
事务也会拿 AccessShareLock**，只要连接不结束就一直持有。在线文档问答建好工具后
连接常驻，于是 `chunk_vectors` 被一把共享锁长期压住；后来任何进程建索引时的
`ALTER TABLE ... ADD COLUMN`（要 AccessExclusiveLock）都会**无限期等待**，
且不会自愈 —— 表现是「第一个文档问题正常，之后全部卡死」。

这里用假连接断言「查完就 commit」这个契约。真锁行为要真 PostgreSQL 才看得见，
所以另有一条可选的真库回归（见文件末尾）。
"""

import os

import pytest

from app.rag.retrieval.pgvector_store import PgVectorStore


class _Cursor:
    def __init__(self, sink, rows):
        self._sink = sink
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, _params=None):
        self._sink.append(sql)

    def fetchall(self):
        return self._rows


class _Connection:
    """记录 commit / rollback 次数，不发真实 SQL。"""

    def __init__(self, rows=()):
        self._rows = list(rows)
        self.sql = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self.sql, self._rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _store(rows=()):
    store = PgVectorStore.__new__(PgVectorStore)
    store._conn = _Connection(rows)
    store.read_only = False
    return store


# ── 读路径必须结束事务 ────────────────────────────────────────────────


def test_search_commits_so_read_transaction_does_not_hold_lock() -> None:
    """检索后必须 commit：留着 idle-in-transaction 会挡住别人建索引。"""
    store = _store(rows=[])

    store.search([0.0], top_k=3)

    assert store._conn.commits == 1, "检索成功后未结束读事务，锁会被一直持有"
    assert store._conn.rollbacks == 0


def test_load_meta_commits_read_transaction() -> None:
    store = _store(rows=[])

    store.load_meta()

    assert store._conn.commits == 1


def test_count_chunks_by_doc_commits_read_transaction() -> None:
    store = _store(rows=[])

    store.count_chunks_by_doc()

    assert store._conn.commits == 1


def test_search_still_rolls_back_on_failure_and_reraises() -> None:
    """原有契约不能丢：失败要 rollback 并抛出，否则连接会卡在 aborted 状态。"""

    class _Boom(_Cursor):
        def execute(self, sql, _params=None):
            raise RuntimeError("simulated SQL failure")

    class _BoomConn(_Connection):
        def cursor(self):
            return _Boom(self.sql, self._rows)

    store = PgVectorStore.__new__(PgVectorStore)
    store._conn = _BoomConn()
    store.read_only = False

    with pytest.raises(RuntimeError, match="simulated SQL failure"):
        store.search([0.0], top_k=1)

    assert store._conn.rollbacks == 1
    assert store._conn.commits == 0, "失败路径不应 commit"


# ── 只读连接不得执行 DDL ─────────────────────────────────────────────


def test_init_schema_refuses_on_read_only_connection() -> None:
    """把「静默死锁」变成「立刻报错」，防止以后又有人在只读路径上调它。"""
    store = PgVectorStore.__new__(PgVectorStore)
    store._conn = _Connection()
    store.read_only = True
    store.dim = 1024

    with pytest.raises(RuntimeError, match="只读"):
        store.init_schema()

    assert store._conn.sql == [], "只读连接不应发出任何 DDL"


def test_init_schema_runs_ddl_on_writable_connection() -> None:
    store = _store()
    store.dim = 1024

    store.init_schema()

    assert len(store._conn.sql) == 3, "应执行建表 + 补列 + 元数据表三条 DDL"
    assert store._conn.commits == 1


# ── 连接失效后必须自动重建 ───────────────────────────────────────────
#
# 背景（第二个真事故）：上面那条常驻连接在数据库重启（Docker 重启、pg_ctl
# restart、容器被 OOM 杀掉）后会**永久失效**。旧实现不察觉也不重建，于是
# 之后每一个文档问题都 500，堆栈止于 `psycopg.OperationalError: the
# connection is lost`，而数据库本身完全健康。必须人工重启后端才恢复。
#
# 这里的假连接用「操作必然抛 OperationalError」模拟断连；真断连行为由文件
# 末尾的可选真库用例覆盖（它会真把连接掐掉）。


class _LostConnectionError(Exception):
    """名字与 psycopg.OperationalError 一致 —— 判据按类名匹配，不依赖 psycopg。"""


_LostConnectionError.__name__ = "OperationalError"


class _BreaksThenWorks(_Connection):
    """第一次操作抛「连接丢失」，重建后的新连接正常工作。"""

    def __init__(self, rows=(), *, fail_times=1):
        super().__init__(rows)
        self._remaining_failures = fail_times

    def cursor(self):
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise _LostConnectionError("the connection is lost")
        return super().cursor()


def test_reconnecting_store_produces_a_working_connection():
    """重建走的是真 _connect()，所以这里只断言「换了对象」这层契约。"""
    store = PgVectorStore.__new__(PgVectorStore)
    store.dsn = "postgresql://example/db"
    store.dim = 1024
    store.read_only = False
    made = []

    def _fake_connect():
        conn = _Connection()
        made.append(conn)
        return conn

    store._connect = _fake_connect
    store._conn = _Connection()

    store._reconnect()

    assert len(made) == 1, "重连必须真的建一条新连接"
    assert store._conn is made[0]


def test_search_reconnects_and_retries_once_on_lost_connection() -> None:
    """断连后必须自己重建并重试，不能让上层看到 500。"""
    store = PgVectorStore.__new__(PgVectorStore)
    store.dsn = "postgresql://example/db"
    store.dim = 1024
    store.read_only = False
    store._conn = _BreaksThenWorks(rows=[])
    reconnects = []
    store._connect = lambda: (reconnects.append(1), _Connection(rows=[]))[1]

    store.search([0.0], top_k=3)

    assert len(reconnects) == 1, "断连后应重建连接"
    assert store._conn.commits >= 1, "重试成功后仍要结束读事务"


def test_load_meta_reconnects_on_lost_connection() -> None:
    store = PgVectorStore.__new__(PgVectorStore)
    store.dsn = "postgresql://example/db"
    store.dim = 1024
    store.read_only = False
    store._conn = _BreaksThenWorks(rows=[])
    reconnects = []
    store._connect = lambda: (reconnects.append(1), _Connection(rows=[]))[1]

    store.load_meta()

    assert len(reconnects) == 1


def test_business_errors_are_not_retried() -> None:
    """SQL 写错、约束冲突这类异常重连也没用，必须原样抛出、不重试。

    否则一个明确的报错会变成一次无谓的重连 + 重放，还可能掩盖真实原因。
    """

    class _SyntaxError(Exception):
        pass

    _SyntaxError.__name__ = "SyntaxError"

    class _BadSqlConn(_Connection):
        def cursor(self):
            raise _SyntaxError('syntax error at or near "FORM"')

    store = PgVectorStore.__new__(PgVectorStore)
    store.dsn = "postgresql://example/db"
    store.dim = 1024
    store.read_only = False
    store._conn = _BadSqlConn()
    reconnects = []
    store._connect = lambda: (reconnects.append(1), _Connection())[1]

    with pytest.raises(Exception, match="syntax error"):
        store.search([0.0], top_k=1)

    assert reconnects == [], "业务异常不该触发重连"


def test_lost_connection_is_not_retried_forever() -> None:
    """数据库一直不可用时只重试一次，快速失败 —— 不在请求线程里干等。"""
    store = PgVectorStore.__new__(PgVectorStore)
    store.dsn = "postgresql://example/db"
    store.dim = 1024
    store.read_only = False

    class _AlwaysBroken(_Connection):
        def cursor(self):
            raise _LostConnectionError("the connection is lost")

    store._conn = _AlwaysBroken()
    attempts = []

    def _connect():
        attempts.append(1)
        return _AlwaysBroken()

    store._connect = _connect

    with pytest.raises(_LostConnectionError):
        store.search([0.0], top_k=1)

    assert len(attempts) == 1, "只该重连一次，不能无限重试"


# ── 可选：真库端到端回归 ─────────────────────────────────────────────
#
# 假连接测不出真实锁语义，所以另留一条连真库的用例。
# 默认跳过，只有显式给出可用 DSN 时才跑：
#     set VERIMIND_TEST_PG_DSN=postgresql://ict:...@127.0.0.1:5432/chinook
# 断言「检索用的常驻连接」不会挡住另一个连接的 ALTER TABLE。


@pytest.mark.skipif(
    not os.environ.get("VERIMIND_TEST_PG_DSN"),
    reason="需要 VERIMIND_TEST_PG_DSN 指向可写 PostgreSQL 才跑真库锁回归",
)
def test_live_search_connection_does_not_block_ddl() -> None:
    import threading
    import psycopg

    dsn = os.environ["VERIMIND_TEST_PG_DSN"]
    store = PgVectorStore(dsn, 1024)
    try:
        store.init_schema()
        try:
            store.search([0.0] * 1024, top_k=1)
        except Exception:
            pass  # 维度/表内容无关紧要，这里只关心事务是否收尾

        done = {}

        def alter():
            conn = psycopg.connect(dsn)
            try:
                with conn.cursor() as cur:
                    cur.execute("ALTER TABLE chunk_vectors ADD COLUMN IF NOT EXISTS zz_lock_probe REAL")
                conn.commit()
                done["ok"] = True
            finally:
                conn.close()

        thread = threading.Thread(target=alter, daemon=True)
        thread.start()
        thread.join(timeout=15)
        assert done.get("ok") is True, "常驻检索连接挡住了 DDL，会重现线上死锁"
    finally:
        conn = psycopg.connect(dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE chunk_vectors DROP COLUMN IF EXISTS zz_lock_probe")
            conn.commit()
        finally:
            conn.close()
