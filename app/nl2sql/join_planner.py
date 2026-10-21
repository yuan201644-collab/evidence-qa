"""仅依据已声明外键，构造有界的候选关联路径。"""

from __future__ import annotations

from collections import deque

from app.nl2sql.planning_models import JoinEdge, JoinPlan, LinkResult
from app.nl2sql.schema import SchemaCatalog, TableMeta


class JoinPlanner:
    """只使用元数据明确声明的外键，不按同名字段猜关联。"""

    def __init__(self, *, max_depth: int = 4, max_paths: int = 3) -> None:
        self.max_depth = max(1, max_depth)
        self.max_paths = max(1, max_paths)

    def plan(self, linked: LinkResult, catalog: SchemaCatalog, *, target_grain: str | None = None) -> JoinPlan:
        candidates = [item for item in linked.selected_tables
                      if not item.relation_only and item.score >= 0.5]
        # Column-owner matches are useful when no table itself was named, but
        # generic field matches must not force unrelated tables into a join.
        semantic = [
            item for item in candidates
            if "owner_of_column" not in item.score_parts
            or any(
                key in item.score_parts
                for key in ("explicit_column_alias", "explicit_value_match")
            )
        ]
        if not semantic and candidates:
            # 全部候选都只是“拥有命中的列”（owner_of_column），没有任何一张表
            # 被问题**明确点名**。此时不能随便挑一张当锚点：
            # `JoinPlan.tables` 在校验器里是“必用表”，挑错一张就会让每一条
            # 本来正确的 SQL 都被判 repair（实测：5 张表同分时挑中
            # contract_equipment，导致只查 work_orders 的 SQL 永远过不了）。
            #
            # 这些表本身是合理的候选，只是不构成业务关联依据，所以：
            #   - 不进入 `tables`（不强制使用、也不据此建关联路径）；
            #   - 放进 `optional_tables`（允许生成器使用，校验器不拦）；
            #   - 它们之间**已声明的外键**照常进 `edges`：这样生成器真写出
            #     显式 JOIN 时，仍要过外键路径核验（不能凭空关联），
            #     而不是因为“没有计划边”被一律拒绝。
            optional_keys = tuple(dict.fromkeys(
                _key(item.schema, item.table) for item in candidates
            ))
            optional_set = set(optional_keys)
            internal_edges = tuple(
                edge for edge in _foreign_edges(catalog)
                if _key(edge.left_schema, edge.left_table) in optional_set
                and _key(edge.right_schema, edge.right_table) in optional_set
            )
            return JoinPlan(
                target_grain=target_grain,
                tables=(),
                edges=internal_edges,
                optional_tables=optional_keys,
                evidence=tuple(f"{edge.left_table}.{','.join(edge.left_columns)} → "
                               f"{edge.right_table}.{','.join(edge.right_columns)} ({edge.source})"
                               for edge in internal_edges),
            )
        if target_grain:
            target = target_grain.casefold()
            semantic.sort(key=lambda item: (
                0 if target in {item.table.casefold(), item.qualified_name.casefold()} else 1,
                -item.score, item.schema, item.table,
            ))
        keys = list(dict.fromkeys(_key(item.schema, item.table) for item in semantic))
        if len(keys) <= 1:
            return JoinPlan(target_grain=target_grain, tables=tuple(keys))

        all_edges = _foreign_edges(catalog)
        chosen: list[JoinEdge] = []
        paths_seen: list[tuple[str, ...]] = []
        warnings: list[str] = []
        ambiguous = False
        anchor = keys[0]
        for target in keys[1:]:
            paths = _shortest_paths(anchor, target, all_edges, self.max_depth, self.max_paths + 1)
            if not paths:
                warnings.append(f"未找到仅依据已声明外键连接 {anchor} 与 {target} 的路径")
                continue
            if len(paths) > 1:
                # 等长路径之间不是一律歧义：优先取「不丢行」的那条。
                # 判据是路径上每一跳的外键列是否可空 —— 经过可空外键的一跳，
                # 取值为空的行会被 INNER JOIN 丢掉。
                # 实测（政采库）：items 到 projects 有两条 2 跳路径，
                # `items→announcements→projects`（856 条品目全有公告）
                # 与 `items→packages→projects`（只有 168 条有包组），
                # 后者会丢掉 688 条品目（80%）。两条都合法，但只有一条完整。
                survivors = _drop_free_paths(paths, all_edges, catalog)
                if len(survivors) == 1:
                    paths = survivors
                elif len(survivors) > 1:
                    paths = survivors
                    ambiguous = True
                    warnings.append(
                        f"{anchor} 与 {target} 存在多条同长外键路径，"
                        "且都经过非空外键，需确认业务关系"
                    )
                else:
                    ambiguous = True
                    warnings.append(f"{anchor} 与 {target} 存在多条同长外键路径，需确认业务关系")
            path = paths[0]
            paths_seen.append(path)
            for left, right in zip(path, path[1:]):
                edge = next((item for item in all_edges
                             if {_key(item.left_schema, item.left_table),
                                 _key(item.right_schema, item.right_table)} == {left, right}), None)
                if edge is not None and edge not in chosen:
                    chosen.append(edge)

        if warnings:
            warnings.append("仅使用目录中声明的外键；未按同名字段猜测关系")
        planned_tables = tuple(dict.fromkeys(
            table for path in paths_seen for table in path
        )) or tuple(keys)
        return JoinPlan(
            target_grain=target_grain,
            tables=planned_tables,
            edges=tuple(chosen),
            candidate_paths=tuple(paths_seen),
            ambiguous=ambiguous,
            warnings=tuple(warnings),
            evidence=tuple(f"{edge.left_table}.{','.join(edge.left_columns)} → "
                           f"{edge.right_table}.{','.join(edge.right_columns)} ({edge.source})"
                           for edge in chosen),
        )


def _foreign_edges(catalog: SchemaCatalog) -> list[JoinEdge]:
    edges: list[JoinEdge] = []
    rows = list({id(table): table for table in catalog.tables.values()}.values())
    for table in rows:
        for fk in table.foreign_keys:
            target = _resolve_target(catalog, fk.ref_table)
            if target is None or not fk.columns or not fk.ref_columns:
                continue
            if len(fk.columns) != len(fk.ref_columns):
                continue
            source_key = _table_key(table)
            target_key = _table_key(target)
            # 只有外键完整列组恰好等于源表主键时才允许标为一对一；
            # 复合主键的子集并不唯一。
            one_to_one = (
                len(fk.columns) == len(table.primary_key)
                and {col.lower() for col in fk.columns}
                    == {pk.lower() for pk in table.primary_key}
            )
            edges.append(JoinEdge(
                left_schema=table.schema, left_table=table.name,
                left_columns=tuple(fk.columns), right_schema=target.schema,
                right_table=target.name, right_columns=tuple(fk.ref_columns),
                source="schema_meta.foreign_keys",
                cardinality="one_to_one" if one_to_one else "many_to_one",
            ))
    return edges


def _shortest_paths(start: str, end: str, edges: list[JoinEdge], max_depth: int, max_paths: int) -> list[tuple[str, ...]]:
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        left = _key(edge.left_schema, edge.left_table)
        right = _key(edge.right_schema, edge.right_table)
        adjacency.setdefault(left, []).append(right)
        adjacency.setdefault(right, []).append(left)
    queue = deque([(start, (start,))])
    found: list[tuple[str, ...]] = []
    shortest: int | None = None
    while queue:
        node, path = queue.popleft()
        if shortest is not None and len(path) - 1 > shortest:
            break
        if node == end:
            shortest = len(path) - 1
            found.append(path)
            if len(found) >= max_paths:
                break
            continue
        if len(path) - 1 >= max_depth:
            continue
        for neighbor in sorted(adjacency.get(node, ())):
            if neighbor not in path:
                queue.append((neighbor, path + (neighbor,)))
    return found


def _step_is_drop_free(
    left: str, right: str, edges: list[JoinEdge], catalog: SchemaCatalog
) -> bool | None:
    """这一跳是否不会丢行。True=不丢，False=会丢，None=查不到列的元数据。

    只判「外键列可空」这一件事：外键列有 NULL 的行，INNER JOIN 到父表时
    匹配不上就被丢掉。主键一侧永远不空，所以只看子表那一端的外键列。
    """
    edge = next(
        (item for item in edges
         if {_key(item.left_schema, item.left_table),
             _key(item.right_schema, item.right_table)} == {left, right}),
        None,
    )
    if edge is None:
        return None
    child_table = edge.left_table
    child_columns = edge.left_columns
    table = _find_table(catalog, edge.left_schema, child_table)
    if table is None:
        return None
    by_name = {column.name.lower(): column for column in table.columns}
    nullables = []
    for name in child_columns:
        column = by_name.get(name.lower())
        if column is None:
            return None
        nullables.append(column.nullable)
    return not any(nullables)


def _drop_free_paths(paths: list[tuple[str, ...]], edges: list[JoinEdge], catalog: SchemaCatalog) -> list[tuple[str, ...]]:
    """从等长路径里挑出不丢行的那些。

    任一跳查不到列元数据时，该路径保留（不因信息缺失而排除合法路径）。
    """
    survivors: list[tuple[str, ...]] = []
    for path in paths:
        keep = True
        for left, right in zip(path, path[1:]):
            verdict = _step_is_drop_free(left, right, edges, catalog)
            if verdict is False:
                keep = False
                break
        if keep:
            survivors.append(path)
    return survivors


def _find_table(catalog: SchemaCatalog, schema: str, name: str) -> TableMeta | None:
    for table in catalog.tables.values():
        if table.schema == schema and table.name == name:
            return table
    return None


def _resolve_target(catalog: SchemaCatalog, name: str) -> TableMeta | None:
    key = (name or "").lower()
    matches = [table for table in catalog.tables.values()
               if table.name.lower() == key or _table_key(table) == key]
    return matches[0] if len(matches) == 1 else None


def _table_key(table: TableMeta) -> str:
    return _key(table.schema, table.name)


def _key(schema: str, table: str) -> str:
    return f"{schema}.{table}".lower()


__all__ = ["JoinPlanner"]
