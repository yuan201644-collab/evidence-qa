"""validator.py - SQL 安全闸门：任何生成的 SQL 在执行前必须过这一关。

**它在链路里的位置**：`generator` 产出候选 SQL → `validator` 静态校验 → `executor` 才执行。
项目硬规范（AGENTS.md §六.7）写死了这条：「所有 SQL 生成结果必须经 sqlglot 校验后才允许执行」，
「禁止任何写操作 SQL（INSERT / UPDATE / DELETE / DROP / ALTER）进入执行路径」。

**为什么用 AST 而不是正则黑名单**：
    `DELETE` 可以藏成 `delete  from x`、`DEL/**/ETE`、注释与大小写变形……正则永远追不上。
    而 sqlglot 先把 SQL 解析成语法树，我们在**树**上判断"根节点是不是 SELECT"
    "有没有 DML/DDL 节点" —— 变形在解析阶段就被抹平了。
    这是项目技术框架里「sqlglot 做 SQL 校验与方言解耦」那条选型的落点。

**校验的五个动作**（顺序即优先级，任一不过就返回 error）：

    ① 解析成恰好一条语句（禁止 `;` 拼接的第二条语句 —— 注入的经典入口）
    ② 顶层必须是 SELECT（或 SELECT 的集合运算 UNION/EXCEPT/INTERSECT）
    ③ 全树扫描，出现 DML/DDL/命令节点一律拒绝（含 SELECT INTO）
    ④ 表名、字段名必须真的存在于库结构目录（SQL 写错的最后一道拦网）
    ⑤ 无 GROUP BY 时自动补 LIMIT（`SQL_AUTO_LIMIT`），给库兜底

⚠️ **⑤ 的取舍必须知道**：补 LIMIT 会让"列出全部 N 行"这类问题只回前 1000 行。
   本库最大表 3503 行，所以这个截断**在真数据上会真的发生**，不是理论风险。
   之所以仍然默认开：它是"防止一条坏 SQL 拖垮库"的安全阀，且 `notes` 里会留痕。
   要关掉就设 `SQL_AUTO_LIMIT=0`。想两全的做法是"先 EXPLAIN 估行数再决定补不补"，
   那属于后续优化，不在首版范围。

**最小规范化**：没有需要改动时，返回的是**原始 SQL 文本**。如果 PostgreSQL 目录中
某张非 `public` 表名唯一、但候选 SQL 未写 schema，则依据目录给该表补全限定名；
只有发生此项规范化或自动补 LIMIT 时才重新渲染 SQL，并在校验 notes 中记录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from app.nl2sql.schema import TableMeta

#: 全树禁止出现的节点类型。按名字取，取不到的跳过（不同 sqlglot 版本的类名会变，
#: 写死 import 会在升级时直接 ImportError，把"校验器"变成"启动失败"）。
_FORBIDDEN_NODE_NAMES = (
    "Insert",        # INSERT
    "Update",        # UPDATE
    "Delete",        # DELETE
    "Drop",          # DROP
    "Create",        # CREATE
    "Alter",         # ALTER
    "TruncateTable",  # TRUNCATE
    "Grant",         # GRANT
    "Into",          # SELECT ... INTO（会写表）
    "Merge",         # MERGE
    "Copy",          # COPY ... TO/FROM
    "Set",           # SET 会话变量
    "Command",       # 解析器不认识的裸命令（VACUUM / ANALYZE 等）
    "Transaction",   # BEGIN / START TRANSACTION
    "Commit",
    "Rollback",
    "Use",
)
_FORBIDDEN_NODES = tuple(
    getattr(exp, name) for name in _FORBIDDEN_NODE_NAMES if hasattr(exp, name)
)

#: 允许作为**顶层**的节点：SELECT 与它的集合运算。
_TOP_LEVEL_SELECTS = tuple(
    getattr(exp, name) for name in ("Select", "Union", "Intersect", "Except", "SetOperation")
    if hasattr(exp, name)
)


@dataclass
class ValidationResult:
    """校验结果。

    Attributes:
        sql: 通过校验后**应当执行**的 SQL。未做改动时就是输入原文。
        ok: 是否放行。
        error: 不放行的原因（一句人话，会回灌给模型做修复重试）。
        notes: 做了哪些非阻断性处理（如"补了 LIMIT 1000"），供 trace 留痕。
        tables: SQL 引用到的真实表名（去重、按出现顺序）。pipeline 用它造 SqlRef。
    """

    sql: str
    ok: bool
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)


def validate_sql(
    sql,
    catalog,
    *,
    dialect="postgres",
    auto_limit=1000,
    check_columns=True,
    extra_allowed_tables=(),
):
    """对一条候选 SQL 做只读与存在性校验。

    Args:
        sql: 候选 SQL 文本。
        catalog: `SchemaCatalog`，提供表/字段存在性的判据。
        dialect: sqlglot 解析方言（项目数据层预留方言适配，换达梦/人大金仓时改这里）。
        auto_limit: 无 GROUP BY 时补的行数上限；<=0 表示不补。
        check_columns: 是否校验字段存在性。关掉它的唯一合理场景是
            "库里用了目录之外的计算列"，正常留 True。
        extra_allowed_tables: 额外放行的表名（如临时视图 / 演示用的独立表）。

    Returns:
        `ValidationResult`。**不抛异常** —— 校验失败是正常业务分支（要回灌模型重试），
        不是程序错误。
    """
    text = (sql or "").strip()
    if not text:
        return ValidationResult(sql="", ok=False, error="SQL 为空")

    # ── ① 解析：必须恰好一条语句 ──────────────────────────────────────
    try:
        parsed = sqlglot.parse(text, read=dialect)
    except Exception as exc:  # noqa: BLE001 —— 解析失败是输入问题，不是程序错误
        return ValidationResult(sql=text, ok=False, error="SQL 解析失败：" + str(exc))

    statements = [node for node in parsed if node is not None]
    if len(statements) == 0:
        return ValidationResult(sql=text, ok=False, error="SQL 解析后没有任何语句")
    if len(statements) > 1:
        return ValidationResult(
            sql=text,
            ok=False,
            error="只允许一条 SQL 语句（检测到 " + str(len(statements)) + " 条）",
        )
    root = statements[0]

    # ── ② 顶层必须是 SELECT / 集合运算 ───────────────────────────────
    if not isinstance(root, _TOP_LEVEL_SELECTS):
        return ValidationResult(
            sql=text,
            ok=False,
            error="只允许 SELECT 查询，当前顶层语句是 " + type(root).__name__,
        )

    # ── ③ 全树扫描禁止节点 ───────────────────────────────────────────
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            return ValidationResult(
                sql=text,
                ok=False,
                error="SQL 中含被禁止的写操作/命令节点：" + type(node).__name__,
            )

    # ── ④ 表名 / 字段名存在性 ────────────────────────────────────────
    allowed = {t.lower() for t in extra_allowed_tables}
    cte_names = {
        cte.alias_or_name.lower() for cte in root.find_all(exp.CTE) if cte.alias_or_name
    }
    subquery_aliases = {
        sub.alias.lower() for sub in root.find_all(exp.Subquery) if sub.alias
    }

    table_nodes = list(root.find_all(exp.Table))
    qualified_tables = []
    if str(dialect).casefold() in {"postgres", "postgresql"}:
        # A unique non-public table can be resolved from the authoritative schema
        # catalog. Qualify it before execution instead of relying on search_path.
        # Ambiguous names remain untouched and are rejected by the normal table gate.
        for node in table_nodes:
            if node.db or node.args.get("catalog") or node.name.casefold() in cte_names:
                continue
            if node.name.casefold() in allowed:
                continue
            matches = _matching_tables(catalog, node)
            if len(matches) != 1 or matches[0].schema.casefold() == "public":
                continue
            schema = matches[0].schema
            node.set(
                "db",
                exp.Identifier(this=schema, quoted=_identifier_requires_quote(schema)),
            )
            qualified_tables.append(f"{schema}.{matches[0].name}")

    alias_to_table = {}
    for node in table_nodes:
        real = _qualified_name(node)
        if node.alias:
            alias_to_table[node.alias.lower()] = real

    case_error = _check_case_sensitive_identifiers(root, catalog, cte_names, dialect)
    if case_error:
        return ValidationResult(sql=text, ok=False, error=case_error)

    tables = []
    for node in table_nodes:
        name = _qualified_name(node)
        if not name:
            continue
        if name in cte_names:
            continue
        if name not in allowed and not catalog.has_table(name):
            return ValidationResult(
                sql=text,
                ok=False,
                error="SQL 引用了库结构里不存在的表：" + node.name
                + "（可用表：" + "、".join(catalog.table_names()) + "）",
            )
        if name not in tables:
            tables.append(name)

    if check_columns:
        column_error = _check_columns(root, catalog, cte_names, subquery_aliases, alias_to_table, allowed)
        if column_error:
            return ValidationResult(sql=text, ok=False, error=column_error)
        scalar_error = _check_scalar_aggregate_grouping(root)
        if scalar_error:
            return ValidationResult(sql=text, ok=False, error=scalar_error)

    # ── ④.5 多表 JOIN 下裸列歧义的确定性限定（2026-10-17 新增）────────────
    # 数据库在执行 `GROUP BY supplier_id`（suppliers 与 awards 都有 supplier_id）
    # 时会报 `AmbiguousColumn`，这占政采 30 题真实运行的 4/30 道执行报错。
    # 修法：只碰 GROUP BY / ORDER BY 子句里的裸列，SELECT/WHERE 交给数据库/重写；
    # 归属按 FROM 顺序第一张含该列的表，修复必须留痕（notes），且只在列
    # **确实存在于目录**时动手——列不存在（模型编造）时保持报错走重写，
    # 不掩盖真实错误。
    disambiguated = _disambiguate_bare_columns(root, catalog, cte_names, subquery_aliases)

    # ── ④.4 剔除「把规定当数据塞进 SELECT」的编造常量列（2026-10-17 新增）──
    # 实测起因（政采跨源 15 题，2026-10-17）：跨源问句里一半是法规问题，
    # 模型知道这半不该由 SQL 答，却只能在 SQL 里作答，于是把规定文本当字符串
    # 常量塞进投影，还带上占位符：
    #   SELECT COUNT(*) AS announcement_count,
    #          '招标公告期限应当是[具体天数]个工作日' AS regulation_info
    #   FROM announcements WHERE kind = '招标公告'
    # 后果不只是这一列没用：`build_conclusion` 会把它渲染进结论，编排层再拿
    # 结论当「已知事实」喂给文档侧，等于把一句带占位符的假数据当既定事实。
    # 实测 350 条政采 SQL 里 4 条有这个形态（1.1%），全部出现在跨源题上。
    # 处理方式**不阻断**：剔除该投影并留痕。理由是这个常量不影响数据那半的
    # 正确性，阻断会让整道题从"答对一半"退化成"什么都没答"。
    dropped_constants = _drop_regulation_constants(root)

    # ── ⑤ 补 LIMIT（仅在没有 GROUP BY 时；有 GROUP BY 的结果集已被分组收敛）──
    notes = []
    final_sql = text
    if dropped_constants:
        notes.append(
            "已剔除把规定文本当数据输出的常量列（应由文档侧回答）："
            + "、".join(dropped_constants)
        )
    if disambiguated or qualified_tables or dropped_constants:
        if disambiguated:
            notes.append(
                "多表 JOIN 下歧义裸列已按 FROM 顺序限定归属："
                + "、".join(dict.fromkeys(disambiguated))
            )
        if qualified_tables:
            unique_qualified = sorted(set(qualified_tables), key=str.casefold)
            notes.append(
                "已按结构目录为唯一非 public 表补全 schema 限定："
                + "、".join(unique_qualified)
            )
        final_sql = root.sql(dialect=dialect)
    if auto_limit and auto_limit > 0 and _top_level_group(root) is None and _top_level_limit(root) is None:
        # 纯聚合（SELECT 全是聚合函数、无 GROUP BY）本来就只返回一行，
        # 补 LIMIT 不改变任何结果，只会在 trace 里留下一条"已补 LIMIT"
        # 的噪音，容易被误读成"这是分组结果的前 N 行"。
        if _is_scalar_aggregate(root):
            notes.append("纯聚合查询只返回一行，未补 LIMIT")
        else:
            try:
                limited = root.limit(int(auto_limit))
                final_sql = limited.sql(dialect=dialect)
                notes.append("已自动补 LIMIT " + str(auto_limit) + "（SQL_AUTO_LIMIT，防止全表回传）")
            except Exception:  # noqa: BLE001 —— 补不上就不补，不因为它阻断一条本来合规的查询
                notes.append("尝试补 LIMIT 失败，已按原语句放行")

    return ValidationResult(sql=final_sql, ok=True, notes=notes, tables=tables)


def _is_scalar_aggregate(root) -> bool:
    """顶层 SELECT 是否"只输出聚合结果、且没有 GROUP BY"（必然只返回一行）。

    用来避免给标量聚合补无意义的 LIMIT。带 GROUP BY 的不算（会返回多行），
    带非聚合投影列的也不算（那是普通查询）。
    """
    if not isinstance(root, exp.Select):
        return False
    if root.args.get("group") is not None:
        return False
    projections = [node for node in root.expressions if not isinstance(node, exp.Star)]
    if not projections:
        return False
    return all(
        any(isinstance(sub, exp.AggFunc) for sub in node.find_all(exp.AggFunc))
        for node in projections
    )


_REGULATION_CONST_RE = re.compile(r"\[[^\]]{1,20}\]")
_REGULATION_WORDS = (
    "应当", "不得", "须", "规定", "工作日", "期限", "公示", "公告期限", "批准", "备案",
)


def _drop_regulation_constants(root) -> list[str]:
    """从顶层 SELECT 投影里剔除「把规定当数据输出」的字符串常量列。

    判据（三条同时满足才剔）：
        1. 顶层 SELECT（不是子查询/CTE 内部），避免误伤合法的分段统计；
        2. 投影是**纯字符串常量**，或 `CASE` 且所有分支都是字符串常量；
        3. 该常量含占位符 `[xxx]`，或长度 >= 12 且含规定类措辞。

    为什么第 3 条要长度门槛：`CASE WHEN kind = '中标公告' THEN '中标' ELSE '其他' END`
    是完全合法的分段统计，标签很短、不含规定措辞。实测政采 30 题问数题里
    这类写法很常见，必须放过。

    为什么只剔不阻断：这个常量不影响数据那半的正确性。阻断会让整道题从
    「数据答对、只多了一列废话」退化成「什么都没答」，那是对用户更差的结果。

    Returns:
        被剔除的常量文本列表（空列表表示没动）。**原地修改 `root`**。
    """
    if not isinstance(root, exp.Select):
        return []

    def _bad_constant(node) -> str | None:
        if isinstance(node, exp.Literal) and node.is_string:
            return str(node.this)
        return None

    kept = []
    dropped: list[str] = []
    for projection in root.expressions:
        target = projection.this if isinstance(projection, exp.Alias) else projection

        # 形态一：纯字符串常量
        literal = _bad_constant(target)
        if literal is not None:
            if _looks_like_regulation(literal):
                dropped.append(literal)
                continue
            kept.append(projection)
            continue

        # 形态二：CASE，且所有结果分支都是字符串常量
        if isinstance(target, exp.Case):
            branches = [target.args.get("default")]
            branches.extend(iff.args.get("true") for iff in target.args.get("ifs") or [])
            literals = [node for node in branches if isinstance(node, exp.Literal) and node.is_string]
            if literals and len(literals) == len([b for b in branches if b is not None]):
                bad = [str(node.this) for node in literals if _looks_like_regulation(str(node.this))]
                if bad:
                    dropped.extend(bad)
                    continue
        kept.append(projection)

    if dropped:
        # 至少留一列，否则 SELECT 变成空列表、SQL 直接失效
        root.set("expressions", kept or [exp.Star()])
    return dropped


def _looks_like_regulation(text: str) -> bool:
    """一段中文常量是否像「规定文本」而不是普通的短标签。"""
    candidate = (text or "").strip()
    if not candidate:
        return False
    if _REGULATION_CONST_RE.search(candidate):
        return True
    if len(candidate) < 12:
        return False
    return any(word in candidate for word in _REGULATION_WORDS)


def _check_scalar_aggregate_grouping(root):
    """SELECT 全是聚合函数却又 GROUP BY 时，报"结果本应一行却会被拆成多行"。
    只判**顶层、无 HAVING、SELECT 全为聚合**这一种能确定的情况：
    `SELECT COUNT(DISTINCT project_id) ... GROUP BY project_id` 会返回每个项目
    一行的计数（处处为 1），与"一共有多少个不同项目"的语义完全相反。
    这种语句数据库照收，只在执行后才能看出行数不对，是真实运行里反复出现的
    静默错答（政采 gov-m-18：标准 1 行、预测 2659 行）。

    存在的 HAVING 时 GROUP BY 是必要的（先分组再筛选），一律放过。
    """
    if not isinstance(root, exp.Select):
        return None
    if root.args.get("having") is not None:
        return None
    group = root.args.get("group")
    if group is None or not group.expressions:
        return None
    projections = [node for node in root.expressions if not isinstance(node, exp.Star)]
    if not projections:
        return None
    if not all(any(isinstance(sub, exp.AggFunc) for sub in node.find_all(exp.AggFunc))
               for node in projections):
        return None
    grouped = "、".join(
        node.sql(dialect="postgres") for node in group.expressions
    )
    return (
        "SELECT 只输出聚合结果，却又按 " + grouped + " 分组，会返回每个分组一行"
        "（每个计数恒为 1），与统计总数的语义相反；去掉 GROUP BY 才是全局聚合。"
    )


def _check_columns(root, catalog, cte_names, subquery_aliases, alias_to_table, allowed):
    """字段存在性检查。通过返回 None，不通过返回一句错误说明。

    只做**能确定判断**的检查，判不准的一律放过 —— 宁漏不误杀：
    校验器误杀一条正确 SQL 的代价（改不动、答不出）比漏放一条错 SQL 更大，
    因为错的 SQL 到执行阶段还有数据库自己会报错。

    放过的情况：
      - 字段名是查询自己定义的输出别名（`... AS revenue` 后在 ORDER BY 里用 `revenue`）
      - 限定名指向 CTE 或子查询（派生表，列集合由它自己决定）
      - 限定名不在别名表里，也不在目录里（真正的问题交给"表不存在"那条报）
    """
    output_aliases = {
        alias.alias.lower() for alias in root.find_all(exp.Alias) if alias.alias
    }
    # ORDER BY / GROUP BY 里不带表前缀、且与输出别名同名的列，是引用 SELECT 的
    # 输出别名（`... AS revenue` 之后 `ORDER BY revenue`），必须放过。
    # 但**带表前缀的**（`ORDER BY i.unit_price`）永远不可能指代输出别名——
    # SQL 里 `i.x` 就是 items 表的列 x。原实现只看名字不看前缀，导致
    # `SELECT i.unit_price_yuan AS unit_price ... GROUP BY i.unit_price`
    # 这种「别名当列名用」的错被当场放过，一路执行到数据库才报 UndefinedColumn。
    order_scoped_aliases = {
        (node.name or "").lower()
        for clause in root.find_all(exp.Order, exp.Group)
        for node in clause.find_all(exp.Column)
        if not (node.table or "")
    }
    opaque = {n.lower() for n in list(cte_names) + list(subquery_aliases)} | set(allowed)

    referenced = []
    for node in root.find_all(exp.Table):
        name = _qualified_name(node)
        if name and name not in cte_names and name not in referenced:
            referenced.append(name)
    if not referenced:
        return None

    for column in root.find_all(exp.Column):
        name = (column.name or "").lower()
        if not name:
            continue
        # `t.*` 展开成 Column(Star) —— 不判
        if name == "*":
            continue
        if name in output_aliases and name in order_scoped_aliases:
            continue
        qualifier = (column.table or "").lower()
        if qualifier:
            if qualifier in opaque:
                continue
            real = alias_to_table.get(qualifier, qualifier)
            if real in opaque or not catalog.has_table(real):
                continue
            if not catalog.has_column(real, name):
                return (
                    "表 " + real + " 里没有字段 " + column.name
                    + "（该表字段：" + "、".join(c.name for c in catalog.get_table(real).columns) + "）"
                )
        else:
            if any(catalog.has_column(table, name) for table in referenced):
                continue
            return (
                "字段 " + column.name + " 在本次查询引用的表里都不存在（已引用："
                + "、".join(referenced) + "）"
            )
    return None


def _qualified_name(node):
    """Return schema-qualified table identity when SQL explicitly provides it."""
    name = (node.name or "").lower()
    schema = (node.db or "").lower()
    return f"{schema}.{name}" if schema else name


def _disambiguate_bare_columns(root, catalog, cte_names, subquery_aliases):
    """多表 JOIN 下，把歧义裸列限定到确定的表。

    背景：PostgreSQL 对 JOIN 后引用裸列（suppliers 与 awards 都有 supplier_id）
    报 `AmbiguousColumn`，模型在真实运行里反复踩（政采 30 题中 4 道 error 因此）。

    边界（缺一不可）：
      - 只处理**裸列**（无表前缀）；显式写了表前缀但写错的（`a.award_yuan` 而
        announcements 没有该列）留给 `_check_columns` 的存在性检查报错。
      - 只在列名**确实存在于目录**的 ≥2 张 FROM 表时动手；列不存在时不动
        （模型编造的列必须留在报错里，不能被「修复」掩盖）。
      - 归属取 FROM 顺序第一张含该列的表（`FROM x JOIN y` 中 x 优先）。
        语义安全性依据：同名列在各表之间是外键对齐的（JOIN 条件正是它们），
        同一行上取值相等，归到哪张表结果集等价——该前提已在 askdata_gov
        真库上验证（announcements.project_id vs awards.project_id mismatch=0）；
        若未来某库同名列并非外键对齐，执行结果不同，靠执行期报错与评测兜底。

    Returns:
        留痕字符串列表（如 ``"supplier_id -> suppliers"``），无修改返回空列表。
    """
    # FROM 顺序的实体表（跳过 CTE / 子查询替身）
    from_order: list[str] = []
    for node in root.find_all(exp.Table):
        name = _qualified_name(node)
        if not name or name in cte_names or name in subquery_aliases:
            continue
        if catalog.has_table(name) and name not in from_order:
            from_order.append(name)
    if len(from_order) < 2:
        return []  # 单表不存在歧义，直接跳过

    fixed: list[str] = []
    for column in root.find_all(exp.Column):
        if column.table:
            continue
        cname = (column.name or "").lower()
        if not cname or cname == "*":
            continue
        owners = [t for t in from_order if catalog.has_column(t, cname)]
        if len(owners) < 2:
            continue
        column.set("table", exp.Identifier(this=owners[0]))
        fixed.append(cname + " -> " + owners[0])
    return fixed


def _check_case_sensitive_identifiers(root, catalog, cte_names, dialect):
    """Require exact quoted identifiers where PostgreSQL would lowercase names."""
    if str(dialect).casefold() not in {"postgres", "postgresql"}:
        return None

    table_nodes = [node for node in root.find_all(exp.Table) if node.name]
    resolved: dict[int, TableMeta] = {}
    for node in table_nodes:
        if node.name.casefold() in cte_names:
            continue
        matches = _matching_tables(catalog, node)
        if len(matches) != 1:
            continue
        table = matches[0]
        resolved[id(node)] = table
        table_identifier = node.this
        if _identifier_requires_quote(table.name) and not _exact_quoted_identifier(
            table_identifier, table.name
        ):
            return (
                f"PostgreSQL 表名 {table.name} 区分大小写，必须用双引号按原大小写引用；"
                f"例如 \"{table.schema}\".\"{table.name}\""
            )
        schema_identifier = node.args.get("db")
        if _identifier_requires_quote(table.schema) and not _exact_quoted_identifier(
            schema_identifier, table.schema
        ):
            return f"PostgreSQL schema {table.schema} 区分大小写，必须用双引号按原大小写限定表名"

    aliases: dict[str, TableMeta] = {}
    for node in table_nodes:
        table = resolved.get(id(node))
        if table is None:
            continue
        aliases[(node.alias_or_name or node.name).casefold()] = table
        aliases.setdefault(node.name.casefold(), table)

    output_aliases = {
        alias.alias.casefold() for alias in root.find_all(exp.Alias) if alias.alias
    }
    referenced_tables = list({id(table): table for table in resolved.values()}.values())
    for column in root.find_all(exp.Column):
        if not column.name or column.name == "*" or column.name.casefold() in output_aliases:
            continue
        qualifier = (column.table or "").casefold()
        if qualifier:
            table = aliases.get(qualifier)
            candidates = [table] if table is not None else []
        else:
            candidates = referenced_tables
        matches = [
            actual for table in candidates if table is not None
            for actual in table.columns if actual.name.casefold() == column.name.casefold()
        ]
        if len(matches) != 1:
            continue
        actual = matches[0]
        if _identifier_requires_quote(actual.name) and not _exact_quoted_identifier(
            column.this, actual.name
        ):
            return f"PostgreSQL 字段名 {actual.name} 区分大小写，必须用双引号按原大小写引用"
    return None


def _matching_tables(catalog, node):
    tables = list({id(table): table for table in catalog.tables.values()}.values())
    matches = [table for table in tables if table.name.casefold() == node.name.casefold()]
    if node.db:
        matches = [table for table in matches if table.schema.casefold() == node.db.casefold()]
    return matches


def _identifier_requires_quote(name: str) -> bool:
    return name != name.lower() or re.fullmatch(r"[a-z_][a-z0-9_$]*", name) is None


def _exact_quoted_identifier(identifier, expected: str) -> bool:
    return bool(
        isinstance(identifier, exp.Identifier)
        and identifier.args.get("quoted")
        and identifier.this == expected
    )


def _top_level_limit(root):
    """取顶层 LIMIT 节点；没有返回 None。

    只认**顶层**，不看子查询里的 LIMIT：子查询限流不等于外层结果被限。
    """
    return root.args.get("limit")


def _top_level_group(root):
    """取顶层 GROUP BY 节点；集合运算没有顶层 group，返回 None。"""
    return root.args.get("group")


def is_read_only(sql, *, dialect="postgres"):
    """这条 SQL 是不是只读查询。给测试与"旁路校验"的场合用。

    判定与 `validate_sql` 的前三步完全一致（单语句 + 顶层 SELECT + 无禁止节点），
    只是不查库结构、不补 LIMIT。
    """
    text = (sql or "").strip()
    if not text:
        return False
    try:
        parsed = [node for node in sqlglot.parse(text, read=dialect) if node is not None]
    except Exception:  # noqa: BLE001
        return False
    if len(parsed) != 1:
        return False
    root = parsed[0]
    if not isinstance(root, _TOP_LEVEL_SELECTS):
        return False
    return not any(isinstance(node, _FORBIDDEN_NODES) for node in root.walk())


def referenced_tables(sql, *, dialect="postgres"):
    """从一条 SQL 里取出引用的表名（去重、保序）。纯文本工具，不查目录。"""
    text = (sql or "").strip()
    if not text:
        return []
    try:
        parsed = [node for node in sqlglot.parse(text, read=dialect) if node is not None]
    except Exception:  # noqa: BLE001
        return []
    if len(parsed) != 1:
        return []
    root = parsed[0]
    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE) if cte.alias_or_name}
    out = []
    for node in root.find_all(exp.Table):
        name = (node.name or "").lower()
        if name and name not in cte_names and name not in out:
            out.append(name)
    return out


__all__ = [
    "ValidationResult",
    "validate_sql",
    "is_read_only",
    "referenced_tables",
]
