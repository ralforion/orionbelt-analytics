"""Tables whose rows a GROUP BY keeps once per group.

A join that repeats a table's rows inflates a total over that table, unless
the grouping separates the repeats::

    SELECT s.id, SUM(c.credits)
    FROM students s
    JOIN student_courses sc ON sc.student_id = s.id
    JOIN courses c ON c.id = sc.course_id
    GROUP BY s.id

joins each course once per student taking it, and sums each course once per
student: the bridge's key is the pair (student_id, course_id), so a course and
a student match one bridge row. Without the GROUP BY, or grouped by
``s.name``, which two students may share, a course is counted once per
student.

Worked out as functional dependencies: from the measured table's key and the
grouping keys, a column equal to a known one is known (join and WHERE
equalities, and a column equal to a constant), and a table whose whole
declared primary key is known is known entirely. A measured table's rows are
kept once per group when every table of the SELECT ends up known. Only what
is certain counts:

- declared primary keys only: without one, a table may repeat anything;
- equalities that hold for every row: conjuncts, not OR alternatives;
- across an outer join, only from the side it keeps to the side it may fill
  with NULLs, whose missing row is still one row; none across a FULL join;
- inner, outer and cross joins only: the rows an ANTI join keeps fail its
  condition, and SEMI, ASOF, POSITIONAL, ClickHouse's ANY and ARRAY joins
  match rows otherwise;
- plain tables of the schema only: a CTE, a derived table, UNNEST or a
  table function may hold any number of rows per key;
- plain grouping keys: ROLLUP, CUBE, GROUP BY ALL add or change groups,
  and an expression (``date_trunc(...)``) fixes no column;
- a table renamed by a column alias list (``AS oi(qty, order_id, id)``) is
  not judged: its key may now go by another column's name;
- a window aggregate sums across the groups (``SUM(o.total) OVER ()``), so a
  table it reads is kept once per group for nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from sqlglot import exp

#: A column of one table reference in the SELECT: (alias, column), lower-cased.
Ref = tuple[str, str]

_GROUPING_SETS = (exp.Rollup, exp.Cube, exp.GroupingSets)


@dataclass(frozen=True)
class Keyed:
    """A table of the schema, as far as keys go."""

    #: Its declared primary key, lower-cased; empty when it has none.
    primary_key: tuple[str, ...]
    #: All of its columns, lower-cased.
    columns: tuple[str, ...]


#: The table a reference reads; None when it is no table of the schema.
Catalog = Callable[[exp.Table], Keyed | None]

# Directions an equality carries what is known.
_BOTH, _FORWARD = "both", "forward"

# Join kinds whose condition holds for every row they produce, one row per
# match.
_PLAIN_KINDS = frozenset({"", "INNER", "OUTER", "CROSS", "STRAIGHT_JOIN"})


def kept_once(select: exp.Select, catalog: Catalog) -> set[str]:
    """The tables of *select* whose rows its GROUP BY keeps once per group.

    Args:
        select: An aggregating SELECT.
        catalog: Keys and columns of each table it reads.

    Returns:
        Lower-cased names of the tables whose every reference is kept once.
    """
    group = select.args.get("group")
    if group is None or not group.expressions:
        return set()
    if any(group.args.get(k) for k in ("all", "rollup", "cube", "grouping_sets")):
        return set()
    if any(isinstance(key, _GROUPING_SETS) for key in group.expressions):
        return set()
    tables = _tables(select, catalog)
    if tables is None:
        return set()
    edges, pinned = _equalities(select, tables)
    if edges is None:
        return set()
    grouped = {
        ref for key in group.expressions if (ref := _group_ref(key, select, tables))
    }
    known = pinned | grouped
    by_name: dict[str, list[str]] = {}
    for alias, (name, _) in tables.items():
        by_name.setdefault(name, []).append(alias)
    kept = {
        name
        for name, aliases in by_name.items()
        if all(_all_known(alias, tables, edges, known) for alias in aliases)
    }
    windowed = _windowed(select, tables)
    return set() if windowed is None else kept - windowed


def _windowed(
    select: exp.Select, tables: dict[str, tuple[str, Keyed]]
) -> set[str] | None:
    """Tables a window function of *select* takes values from.

    None when one of its columns cannot be placed: any table may be read.
    """
    names: set[str] = set()
    for window in select.find_all(exp.Window):
        if window.find_ancestor(exp.Select) is not select:
            continue
        for column in window.this.find_all(exp.Column):
            if column.find_ancestor(exp.Select) is not select:
                continue
            ref = _ref(column, tables)
            if ref is None:
                return None
            names.add(tables[ref[0]][0])
    return names


def _tables(
    select: exp.Select, catalog: Catalog
) -> dict[str, tuple[str, Keyed]] | None:
    """Each FROM and JOIN reference by alias; None if any is no keyed table."""
    from_ = select.args.get("from_") or select.args.get("from")
    nodes = [from_.this] if from_ is not None else []
    nodes += [join.this for join in select.args.get("joins") or []]
    tables: dict[str, tuple[str, Keyed]] = {}
    for node in nodes:
        if (
            not isinstance(node, exp.Table)
            or not node.name
            or node.args.get("joins")
            or node.args.get("pivots")
        ):
            return None
        alias_node = node.args.get("alias")
        if isinstance(alias_node, exp.TableAlias) and alias_node.args.get("columns"):
            # Renamed columns: the key may now go by another column's name.
            return None
        keyed = catalog(node)
        if keyed is None or not keyed.primary_key:
            return None
        alias = (node.alias or node.name).lower()
        if alias in tables:
            return None
        tables[alias] = (node.name.lower(), keyed)
    return tables or None


def _conjuncts(condition: exp.Expr | None) -> Iterator[exp.Expr]:
    """The tests *condition* requires of every row: AND's operands, not OR's."""
    if condition is None:
        return
    node = condition.unnest() if isinstance(condition, exp.Paren) else condition
    if isinstance(node, exp.And):
        yield from _conjuncts(node.this)
        yield from _conjuncts(node.expression)
    else:
        yield node


def _ref(column: exp.Expr, tables: dict[str, tuple[str, Keyed]]) -> Ref | None:
    """The table reference *column* belongs to, if that is certain."""
    if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star):
        return None
    name = column.name.lower()
    if column.table:
        alias = column.table.lower()
        if alias in tables and name in tables[alias][1].columns:
            return alias, name
        return None
    owners = [alias for alias, (_, keyed) in tables.items() if name in keyed.columns]
    return (owners[0], name) if len(owners) == 1 else None


def _equalities(
    select: exp.Select, tables: dict[str, tuple[str, Keyed]]
) -> tuple[list[tuple[Ref, Ref, str]] | None, set[Ref]]:
    """The equalities that carry what is known, and the columns a constant fixes.

    None for the edges when a join folds columns it does not say how
    (``USING``, ``NATURAL``): which table's column it compares is unclear.
    """
    edges: list[tuple[Ref, Ref, str]] = []
    pinned: set[Ref] = set()

    def read(test: exp.Expr, joined: str | None, side: str) -> None:
        if not isinstance(test, exp.EQ):
            return
        left, right = test.this.unnest(), test.expression.unnest()
        a, b = _ref(left, tables), _ref(right, tables)
        if a and b:
            if side == "":
                edges.append((a, b, _BOTH))
            elif side == "LEFT":
                # Toward the table the join may fill with NULLs only.
                if b[0] == joined and a[0] != joined:
                    edges.append((a, b, _FORWARD))
                elif a[0] == joined and b[0] != joined:
                    edges.append((b, a, _FORWARD))
            elif side == "RIGHT":
                if a[0] == joined and b[0] != joined:
                    edges.append((a, b, _FORWARD))
                elif b[0] == joined and a[0] != joined:
                    edges.append((b, a, _FORWARD))
            return
        pin = (
            a
            if isinstance(right, exp.Literal)
            else b if isinstance(left, exp.Literal) else None
        )
        if pin and (side == "" or (side == "LEFT" and pin[0] == joined)):
            pinned.add(pin)

    for join in select.args.get("joins") or []:
        if join.args.get("using") or join.args.get("method"):
            # USING and NATURAL fold columns without saying whose; ASOF and
            # POSITIONAL match rows by no equality.
            return None, set()
        if (join.args.get("kind") or "").upper() not in _PLAIN_KINDS:
            return None, set()
        node = join.this
        joined = (
            (node.alias or node.name).lower() if isinstance(node, exp.Table) else None
        )
        side = (join.args.get("side") or "").upper()
        if side == "FULL":
            continue
        for test in _conjuncts(join.args.get("on")):
            read(test, joined, side)
    where = select.args.get("where")
    # WHERE holds for every row it keeps, outer joins or not.
    for test in _conjuncts(where.this if where is not None else None):
        read(test, None, "")
    return edges, pinned


def _group_ref(
    key: exp.Expr, select: exp.Select, tables: dict[str, tuple[str, Keyed]]
) -> Ref | None:
    """The column a grouping key fixes: itself, or the output it names."""
    key = key.unnest() if isinstance(key, exp.Paren) else key
    projection: exp.Expr | None = None
    if isinstance(key, exp.Literal) and not key.is_string:
        # GROUP BY 2: the second output.
        position = int(key.name) if key.name.isdigit() else 0
        if 1 <= position <= len(select.selects):
            projection = select.selects[position - 1]
    elif isinstance(key, exp.Column):
        ref = _ref(key, tables)
        if ref is not None:
            return ref
        if not key.table:
            # GROUP BY an output's alias, where the dialect allows it.
            projection = next(
                (
                    p
                    for p in select.selects
                    if p.alias_or_name.lower() == key.name.lower()
                ),
                None,
            )
    if isinstance(projection, exp.Alias):
        projection = projection.this
    return _ref(projection, tables) if projection is not None else None


def _all_known(
    measured: str,
    tables: dict[str, tuple[str, Keyed]],
    edges: list[tuple[Ref, Ref, str]],
    grouped: set[Ref],
) -> bool:
    """Whether *measured*'s key and the grouping keys fix one row of every table."""
    known = set(grouped)
    known.update((measured, column) for column in tables[measured][1].primary_key)
    whole: set[str] = set()
    changed = True
    while changed:
        changed = False
        for a, b, direction in edges:
            if a in known and b not in known:
                known.add(b)
                changed = True
            if direction == _BOTH and b in known and a not in known:
                known.add(a)
                changed = True
        for alias, (_, keyed) in tables.items():
            if alias not in whole and all(
                (alias, k) in known for k in keyed.primary_key
            ):
                whole.add(alias)
                known.update((alias, column) for column in keyed.columns)
                changed = True
    return whole == set(tables)
