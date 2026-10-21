"""pgvector_store.py - 生产级向量库（PostgreSQL + pgvector）

bge-m3 嵌入（indexer.BgeEmbedder）产出的 1024 维稠密向量存这里，做语义检索。
不依赖 Docker：本机装好 PostgreSQL 并启用 pgvector 扩展即可（原生安装或容器都行）。

启用步骤：
    1) uv add psycopg[binary] pgvector
    2) 起一个带 pgvector 扩展的 PostgreSQL（原生安装或 Docker 均可）
    3) 在 index_chunks 里传 dsn 即可自动切换到本存储

为什么表里要存 chunk 的全部字段（含 text / section_path / char 偏移）：
    search 需要直接把 Chunk 还原出来交给上层构造 DocRef。只存一个下标再回查
    进程内列表是行不通的——「建索引一次、查询在另一个进程」是生产常态，那个
    进程的列表是空的（实测会 IndexError）。所以这里以数据库为唯一事实来源，
    search 不依赖任何进程内缓存。

注意（呼应原则③ SQL 方言适配预留）：
    嵌入维度 dim 必须和 embedder 输出一致（bge-m3 = 1024）。建表 DDL 用 VECTOR(dim)。
    section_path 存 JSON 数组字符串而不是用分隔符拼接：round-trip 必须无损，
    否则章节标题里恰好含该分隔符时会被切错。将来换达梦 / 人大金仓可改 DDL 与方言。
"""

import json
import logging

from app.rag.schemas import Chunk

_log = logging.getLogger(__name__)

# 连接不可用（服务器重启 / 网络中断 / 连接被管理员掐断）时抛出的异常类型。
# 单独抽出来是为了让「断连后重建连接」这条路径只捕获真正的连接故障 ——
# SQL 语法错误、约束冲突之类的业务异常同样会走 except，但那些重连也没用，
# 反而会把一个明确的报错变成一次无谓的重试。
_UNAVAILABLE_ERRORS = ("OperationalError", "InterfaceError")

_DDL_TEMPLATE = (
    "CREATE EXTENSION IF NOT EXISTS vector;"
    "CREATE TABLE IF NOT EXISTS chunk_vectors ("
    "  id SERIAL PRIMARY KEY,"
    "  doc_id TEXT NOT NULL,"
    "  chunk_idx INTEGER NOT NULL,"
    "  page INTEGER,"
    "  section_path TEXT,"
    "  char_start INTEGER,"
    "  char_end INTEGER,"
    "  bboxes TEXT,"
    "  page_width REAL,"
    "  page_height REAL,"
    "  doc_title TEXT,"
    "  extraction_method TEXT,"
    "  text TEXT,"
    "  embedding vector(%d),"
    "  UNIQUE (doc_id, chunk_idx)"
    ");"
)

# 幂等升级：CREATE TABLE IF NOT EXISTS 对「已存在的表」不会补列。
# 老库（缺 page_width / page_height / doc_title 三列）必须靠
# ALTER ... ADD COLUMN IF NOT EXISTS 追加，否则 DDL 改了但表结构没变，
# 插入会直接报「column does not exist」。每次 init_schema 都跑一遍，幂等。
_ALTER_SQL = (
    "ALTER TABLE chunk_vectors "
    "ADD COLUMN IF NOT EXISTS page_width REAL, "
    "ADD COLUMN IF NOT EXISTS page_height REAL, "
    "ADD COLUMN IF NOT EXISTS doc_title TEXT, "
    "ADD COLUMN IF NOT EXISTS extraction_method TEXT;"
)

# 文档级元数据表：判断「库里的索引和磁盘上的文档是否匹配」的唯一依据。
# fingerprint 是内容级 sha256（含索引格式版本号，见 corpus.content_fingerprint）——
# 不用文件字节 hash（PDF 元数据时间戳会让它每次都变），也不用 mtime。
# chunk_count 存下来是为了双保险：指纹一致但行数对不上（表被截断 / 上次写到一半崩了）
# 时不能跳过，否则会得到一个「以为有索引、其实查不到」的空壳。
_DDL_META = (
    "CREATE TABLE IF NOT EXISTS doc_meta ("
    "  doc_id TEXT PRIMARY KEY,"
    "  fingerprint TEXT NOT NULL,"
    "  chunk_count INTEGER NOT NULL,"
    "  indexed_at TIMESTAMPTZ NOT NULL DEFAULT now()"
    ");"
)

_SELECT_SQL = (
    "SELECT doc_id, page, section_path, char_start, char_end, bboxes, text, "
    "page_width, page_height, doc_title, extraction_method, "
    "embedding <-> %s::vector AS dist "
    "FROM chunk_vectors ORDER BY dist LIMIT %s"
)

# 多文档并存时只检索当前文档，避免查 A 串出 B 的结果
_SELECT_SQL_BY_DOC = (
    "SELECT doc_id, page, section_path, char_start, char_end, bboxes, text, "
    "page_width, page_height, doc_title, extraction_method, "
    "embedding <-> %s::vector AS dist "
    "FROM chunk_vectors WHERE doc_id = %s ORDER BY dist LIMIT %s"
)


class PgVectorStore:
    """基于 PostgreSQL + pgvector 的向量库。"""

    def __init__(self, dsn, dim, *, read_only=False):
        self.dsn = dsn
        self.dim = dim
        self.read_only = read_only
        self._conn = self._connect()

    def _connect(self):
        """建一条新连接（只读连接在第一次 SQL 前就要设好只读）。"""
        import psycopg  # 延迟 import，未装不会在加载期报错

        conn = psycopg.connect(self.dsn)
        if self.read_only:
            # 在第一次 SQL 前设为只读；数据库本身也会拒绝本事务中的任何写语句。
            conn.read_only = True
        return conn

    def _is_connection_lost(self, exc):
        """这个异常是不是「连接不可用」，而不是 SQL 本身有问题。

        判据取异常类名而不是 `isinstance`：psycopg 是延迟 import 的，
        在模块顶层引用它的异常类型会把「没装 psycopg」也变成加载期报错。
        """
        return type(exc).__name__ in _UNAVAILABLE_ERRORS

    def _reconnect(self):
        """丢弃坏连接、重建一条。

        本 store 的连接是**长活**的：进程启动时建一条，之后所有检索都复用它。
        这在正常情况下是好事（省去每次握手），但数据库一旦重启（Docker 重启、
        pg_ctl restart、容器被 OOM 杀掉），这条连接就永久失效了 —— 旧实现不会
        察觉也不会重建，于是**之后每一个文档问题都失败**，直到人工重启后端。
        表现是 `/api/ask` 一律 500、堆栈止于 `psycopg.OperationalError:
        the connection is lost`，而数据库本身完全健康（新连接一查就通）。

        重试次数刻意很小：这里只处理「连接对象过期」，不处理「数据库还没起来」——
        后者应该快速失败并让上层如实报「数据库不可用」，而不是在请求线程里干等。
        """
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - 坏连接关不掉是常态，不该掩盖下面的重建
            pass
        self._conn = self._connect()
        _log.warning("向量库连接已失效，已重建（数据库可能重启过）")

    def _run(self, operation):
        """执行一次数据库操作；连接已失效时重建连接并重试一次。

        operation 接收一个连接、返回结果。只有「连接不可用」才会重试 ——
        SQL 语法错误、约束冲突等业务异常原样抛出，把它们变成重试毫无意义。
        """
        try:
            return operation(self._conn)
        except Exception as exc:  # noqa: BLE001 - 只对连接类故障重试，其余原样抛
            if not self._is_connection_lost(exc):
                raise
            self._reconnect()
            return operation(self._conn)

    def init_schema(self):
        """建表 / 补列。

        **只读连接绝不能调用本方法**：DDL 会被只读事务拒绝或回滚，而 `ALTER TABLE`
        拿到的 ACCESS EXCLUSIVE 锁要等事务结束才释放。只读连接活得很久（检索期间一直
        复用），锁就被一直抱着 —— 表现为第一个文档问题成功、之后每一个都卡在
        `ALTER TABLE` 上无限期等待，且不会自愈。

        这里显式拦一道，把「静默死锁」变成「立刻报错」，避免以后又有人在只读路径上调它。
        """
        if self.read_only:
            raise RuntimeError(
                "只读向量库连接不能执行建表 / 补列；请在可写连接上初始化结构"
            )
        ddl = _DDL_TEMPLATE % self.dim

        def _init(conn):
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute(_ALTER_SQL)   # 老库补列（幂等，表已是最新则无操作）
                cur.execute(_DDL_META)    # 文档元数据表（幂等）
            conn.commit()

        self._run(_init)

    def add(self, chunk, vector, idx=None):
        """写入一条向量。

        idx 只是留档（方便排查这条对应第几个 chunk），不参与检索时的回查；
        签名与 MemoryVectorStore.add 保持一致，index_chunks 才能统一调用。

        不在此处 commit：重复索引靠 ON CONFLICT (doc_id, chunk_idx) DO UPDATE
        覆盖旧行（同文档内容改了重新索引即更新），提交由 index_chunks 批量收尾，
        避免逐条 commit 的开销（见 commit()）。
        """
        def _insert(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO chunk_vectors "
                    "(doc_id, chunk_idx, page, section_path, char_start, char_end, bboxes, "
                    " page_width, page_height, doc_title, extraction_method, text, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (doc_id, chunk_idx) DO UPDATE SET "
                    "page = EXCLUDED.page, "
                    "section_path = EXCLUDED.section_path, "
                    "char_start = EXCLUDED.char_start, "
                    "char_end = EXCLUDED.char_end, "
                    "bboxes = EXCLUDED.bboxes, "
                    "page_width = EXCLUDED.page_width, "
                    "page_height = EXCLUDED.page_height, "
                    "doc_title = EXCLUDED.doc_title, "
                    "extraction_method = EXCLUDED.extraction_method, "
                    "text = EXCLUDED.text, "
                    "embedding = EXCLUDED.embedding",
                    (
                        chunk.doc_id,
                        idx,
                        chunk.page,
                        json.dumps(chunk.section_path, ensure_ascii=False),
                        chunk.char_start,
                        chunk.char_end,
                        json.dumps(chunk.bboxes, ensure_ascii=False),
                        chunk.page_width,
                        chunk.page_height,
                        chunk.doc_title,
                        chunk.extraction_method,
                        chunk.text,
                        vector,
                    ),
                )

        self._run(_insert)

    def commit(self):
        """批量提交。index_chunks 在全部 add 完成后统一调用一次。"""
        self._conn.commit()

    # ── 按文档增删（corpus.py 的增量索引 / 清孤儿用）────────────────────
    # 这几个方法都属于「文档级」操作，与上面「单条切片」的 add 分开，
    # 免得 index_chunks 的单文档路径被卷进来。

    def delete_by_doc(self, doc_id):
        """删掉某份文档的全部切片，返回删除行数。

        重新索引前必须先调用：ON CONFLICT (doc_id, chunk_idx) 只能覆盖「已存在」
        的下标，文档改短后（93 片 → 80 片）旧的 80~92 行既不会被更新也不会被删，
        会永远留在库里被检索到。先删净再写入才是根治。
        """
        def _delete(conn):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM chunk_vectors WHERE doc_id = %s", (doc_id,))
                return cur.rowcount

        return self._run(_delete)

    def delete_docs(self, doc_ids):
        """批量删掉若干文档的切片与元数据（清孤儿用），返回删除的切片行数。"""
        ids = list(doc_ids)
        if len(ids) == 0:
            return 0

        def _delete(conn):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM chunk_vectors WHERE doc_id = ANY(%s::text[])", (ids,))
                deleted = cur.rowcount
                cur.execute("DELETE FROM doc_meta WHERE doc_id = ANY(%s::text[])", (ids,))
            return deleted

        return self._run(_delete)

    def _quiet_commit(self, conn):
        """收尾只读事务；连接已断时静默跳过。

        连接断掉的情况下 commit 自己也会抛，而且抛的是「连接丢失」——
        把上层真正想报的原因盖掉。事务已经随连接一起没了，没有任何锁需要释放，
        所以直接跳过，交给 _run 重建连接后重试。
        """
        try:
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            if not self._is_connection_lost(exc):
                raise

    def load_meta(self):
        """返回 {doc_id: (fingerprint, chunk_count)}。"""

        def _load(conn):
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT doc_id, fingerprint, chunk_count FROM doc_meta")
                    return cur.fetchall()
            finally:
                # 结束只读事务：PostgreSQL 的读事务也会拿 AccessShareLock，
                # 只要本连接还开着就一直不放。留一个 idle-in-transaction 会挡住
                # 后来的 `ALTER TABLE`（需要 AccessExclusiveLock）—— 表现为后续
                # 进程全部卡死在建索引上，且不会自愈。见 count_chunks_by_doc 同款处理。
                self._quiet_commit(conn)

        rows = self._run(_load)
        meta = {}
        for doc_id, fingerprint, chunk_count in rows:
            meta[doc_id] = (fingerprint, chunk_count)
        return meta

    def count_chunks_by_doc(self):
        """返回 {doc_id: 实际切片行数}，用来交叉验证指纹（见 _DDL_META 注释）。"""

        def _count(conn):
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT doc_id, count(*) FROM chunk_vectors GROUP BY doc_id")
                    return cur.fetchall()
            finally:
                self._quiet_commit(conn)

        rows = self._run(_count)
        counts = {}
        for doc_id, n in rows:
            counts[doc_id] = n
        return counts

    def save_meta(self, doc_id, fingerprint, chunk_count):
        """写入 / 更新某文档的元数据（幂等）。"""

        def _save(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO doc_meta (doc_id, fingerprint, chunk_count, indexed_at) "
                    "VALUES (%s, %s, %s, now()) "
                    "ON CONFLICT (doc_id) DO UPDATE SET "
                    "fingerprint = EXCLUDED.fingerprint, "
                    "chunk_count = EXCLUDED.chunk_count, "
                    "indexed_at = EXCLUDED.indexed_at",
                    (doc_id, fingerprint, chunk_count),
                )

        self._run(_save)

    def search(self, query_vec, top_k=5, doc_id=None):
        """返回 [(Chunk, 相似度)]，相似度越大越相关（与 MemoryVectorStore 同尺度）。

        doc_id 非空时只检索该文档的切片（多文档并存时避免串出别的文档）；
        为空则全表检索（向后兼容单文档 / 测试场景）。

        pgvector 的 <-> 返回欧氏距离；因为 bge-m3 入库时已归一化，
        所以 cos = 1 - dist^2 / 2，换算成余弦相似度，让两个后端分数可直接比较。
        """
        if doc_id is None:
            sql = _SELECT_SQL
            params = (query_vec, top_k)
        else:
            sql = _SELECT_SQL_BY_DOC
            params = (query_vec, doc_id, top_k)

        def _search(conn):
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
            except Exception as exc:
                # PostgreSQL leaves a transaction aborted after a statement error.
                # This store keeps one connection for its lifetime, so clear that
                # failed read transaction before a later retrieval reuses it.
                #
                # 连接已断时 rollback 自己也会抛，并且把真正的原因盖掉 ——
                # 现象就是堆栈停在 `psycopg.OperationalError: the connection is
                # lost`，看不出是「连接早就断了」。这里跳过，交给 _run 重连重试。
                if not self._is_connection_lost(exc):
                    conn.rollback()
                raise
            # 检索是只读的，但读事务同样持有 AccessShareLock；不结束就会一直压着
            # chunk_vectors，挡住后续进程建索引时的 `ALTER TABLE`
            # （需要 AccessExclusiveLock，会无限期等待）。查完立即收尾。
            #
            # 注意 commit 必须在 try 之外：它自己失败时，except 里的 rollback
            # 会让同一个错误被记两次，也会把「提交失败」误报成「语句失败」。
            conn.commit()
            return rows

        rows = self._run(_search)
        results = []
        for (doc_id, page, section_path, char_start, char_end, bboxes, text,
             page_width, page_height, doc_title, extraction_method, dist) in rows:
            chunk = Chunk(
                doc_id=doc_id,
                text=text,
                page=page,
                section_path=json.loads(section_path),
                char_start=char_start,
                char_end=char_end,
                bboxes=json.loads(bboxes) if bboxes else [],
                page_width=page_width,
                page_height=page_height,
                doc_title=doc_title,
                extraction_method=extraction_method,
            )
            results.append((chunk, 1.0 - (dist * dist) / 2.0))
        return results
