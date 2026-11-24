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
- plain grouping keys: ROLLUP and CUBE add groups, and an expression
  (``date_trunc(...)``) fixes no column. A bare GROUP BY ALL groups by every
  output holding no aggregate; of those, plain columns and plain stars
  (``s.*``) are its keys -- leaving out an output only makes the groups
  coarser, never finer than the database's. With anything after it
  (``GROUP BY ALL ROLLUP (x)``) ALL is the set quantifier, not judged;
- a grouping modifier changes the groups: ROLLUP, CUBE, GROUPING SETS,
  ClickHouse's WITH TOTALS (a row over all rows) -- only plain keys and a
  bare GROUP BY ALL are judged;
- a name may be read two ways: ClickHouse takes ``student_id`` for an alias
  of the same SELECT before a column of that name, other databases the
  column. A grouping key or output read either way has to keep a table once
  under each reading;
- a table renamed by a column alias list (``AS oi(qty, order_id, id)``) is
  not judged: its key may now go by another column's name;
- a window aggregate sums across the groups (``SUM(o.total) OVER ()``), so a
  table it reads is kept once per group for nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from itertools import product
from math import prod

from sqlglot import exp

#: A column of one table reference in the SELECT: (alias, column), lower-cased.
Ref = tuple[str, str]

_GROUPING_SETS = (exp.Rollup, exp.Cube, exp.GroupingSets)

#: Combinations of readings tried before ambiguous keys count for nothing.
_MAX_READINGS = 64


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
    if group is None:
        return set()
    # Plain keys and a bare ALL only: ROLLUP, CUBE, GROUPING SETS and WITH
    # TOTALS add rows over several groups -- and whatever sqlglot adds next.
    if any(v for k, v in group.args.items() if k not in ("expressions", "all")):
        return set()
    every_output = bool(group.args.get("all"))
    if every_output and group.expressions:
        return set()  # GROUP BY ALL x: the set quantifier
    if not every_output and (
        not group.expressions
        or any(isinstance(key, _GROUPING_SETS) for key in group.expressions)
    ):
        return set()
    tables = _tables(select, catalog)
    if tables is None:
        return set()
    edges, pinned = _equalities(select, tables)
    if edges is None:
        return set()
    # What each key may stand for: one reading, or several when a name is
    # both a column and an alias.
    keys = (
        _output_readings(select, tables)
        if every_output
        else [_group_readings(key, select, tables) for key in group.expressions]
    )
    by_name: dict[str, list[str]] = {}
    for alias, (name, _) in tables.items():
        by_name.setdefault(name, []).append(alias)

    def kept_by(grouped: set[Ref]) -> set[str]:
        known = pinned | grouped
        return {
            name
            for name, aliases in by_name.items()
            if all(_all_known(alias, tables, edges, known) for alias in aliases)
        }

    settled = {r for readings in keys if len(readings) == 1 for r in readings if r}
    open_keys = [readings for readings in keys if len(readings) > 1]
    if prod(len(readings) for readings in open_keys) > _MAX_READINGS:
        open_keys = []  # too many to try: those keys fix nothing
    kept: set[str] | None = None
    for choice in product(*open_keys):
        found = kept_by(settled | {r for r in choice if r})
        kept = found if kept is None else kept & found
    kept = kept or set()
    windowed = _windowed(select, tables)
    return set() if windowed is None else kept - windowed


def _output_readings(
    select: exp.Select, tables: dict[str, tuple[str, Keyed]]
) -> list[list[Ref | None]]:
    """The columns a bare GROUP BY ALL groups by: its unaggregated outputs.

    Every dialect with GROUP BY ALL groups by each output holding no
    aggregate. A plain column never holds one -- unless its name is read as
    an alias that does -- and a plain star stands for columns: ``s.*`` all
    of s's, ``*`` every table's. Expressions are left out -- they fix no
    column -- and so are stars with EXCLUDE, REPLACE or RENAME, which may
    drop or change a key.

    Returns:
        Per output key, what it may stand for (see :func:`_readings`).
    """
    keys: list[list[Ref | None]] = []
    for projection in select.selects:
        own = projection if isinstance(projection, exp.Alias) else None
        node = projection.this if own is not None else projection
        while isinstance(node, exp.Paren):
            node = node.this
        star = node.this if isinstance(node, exp.Column) else node
        if isinstance(star, exp.Star):
            if any(star.args.values()):
                continue
            aliases = [node.table.lower()] if isinstance(node, exp.Column) else tables
            for alias in aliases:
                if alias in tables:
                    keys.extend([[(alias, c)] for c in tables[alias][1].columns])
        elif isinstance(node, exp.Column):
            keys.append(_readings(node, select, tables, own))
    return keys


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


def _expands(projection: exp.Expr) -> bool:
    """Whether *projection* stands for any number of outputs.

    A star outside any function and any subquery, however wrapped: ``*``,
    ``t.*``, ``(*)``, ``((t.*))``, ``* LIKE 'e%'`` (sqlglot reads that as a
    comparison of the star). And ``COLUMNS(...)`` anywhere in it. A star a
    function takes is one output (``COUNT(*)``, ``struct_pack(*)``), and so
    is a subquery's (``(SELECT * ...)``).
    """
    select = projection.parent
    for star in projection.find_all(exp.Star):
        node = star.parent
        while node is not None and node is not select:
            if isinstance(node, (exp.Func, exp.Query)):
                break
            node = node.parent
        else:
            return True
    return any(True for _ in projection.find_all(exp.Columns))


def output_at(expressions: Sequence[exp.Expr], key: exp.Expr) -> exp.Expr | None:
    """The output a positional grouping key names: ``GROUP BY 2``, the second.

    None when *key* is no position, it is past the outputs, or a star before
    it leaves its place open: in ``SELECT *, order_date ... GROUP BY 2`` the
    second output is the table's second column, not ``order_date``.
    """
    key = key.unnest() if isinstance(key, exp.Paren) else key
    if not (isinstance(key, exp.Literal) and not key.is_string):
        return None
    if not key.name.isdigit() or not 1 <= int(key.name) <= len(expressions):
        return None
    position = int(key.name)
    if any(_expands(p) for p in expressions[:position]):
        return None
    return expressions[position - 1]


def _aliases(select: exp.Select) -> dict[str, list[exp.Alias]]:
    """The aliases *select* defines, by name.

    In its outputs and, as ClickHouse allows, anywhere else in it -- not in
    its subqueries.
    """
    found: dict[str, list[exp.Alias]] = {}
    for alias in select.find_all(exp.Alias):
        if alias.alias and alias.find_ancestor(exp.Select) is select:
            found.setdefault(alias.alias.lower(), []).append(alias)
    return found


def _readings(
    column: exp.Column,
    select: exp.Select,
    tables: dict[str, tuple[str, Keyed]],
    own: exp.Expr | None = None,
) -> list[Ref | None]:
    """What *column* may stand for: a table's column, or an alias by its name.

    Unqualified, ``student_id`` is the bridge's column to most databases but,
    to ClickHouse, ``MAX(s.id) AS student_id`` of the same SELECT. Each
    reading is a column, or None where it fixes none (an aggregate, an
    expression, an alias read through another alias). *own* is the output
    whose value *column* is: its alias names it, it is no reading of it.
    """
    ref = _ref(column, tables)
    if column.table:
        return [ref]
    aliases = _aliases(select)
    others = [a for a in aliases.get(column.name.lower(), []) if a is not own]
    if not others:
        return [ref]
    readings: list[Ref | None] = [] if ref is None else [ref]
    for alias in others:
        target = alias.this
        while isinstance(target, exp.Paren):
            target = target.this
        through_alias = (
            isinstance(target, exp.Column)
            and not target.table
            and any(a is not alias for a in aliases.get(target.name.lower(), []))
        )
        readings.append(None if through_alias else _ref(target, tables))
    return list(dict.fromkeys(readings))


def _group_readings(
    key: exp.Expr, select: exp.Select, tables: dict[str, tuple[str, Keyed]]
) -> list[Ref | None]:
    """What a grouping key may stand for: itself, or the output it names."""
    key = key.unnest() if isinstance(key, exp.Paren) else key
    if isinstance(key, exp.Literal) and not key.is_string:
        # GROUP BY 2: the second output, if its place is certain.
        projection = output_at(select.selects, key)
        if projection is None:
            return [None]
        own = projection if isinstance(projection, exp.Alias) else None
        node = projection.this if own is not None else projection
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.Column):
            return _readings(node, select, tables, own)
        return [None]
    if not isinstance(key, exp.Column):
        return [None]
    readings = _readings(key, select, tables)
    if readings == [None] and not key.table:
        # GROUP BY an output's name, where the dialect allows it.
        projection = next(
            (p for p in select.selects if p.alias_or_name.lower() == key.name.lower()),
            None,
        )
        if isinstance(projection, exp.Column):
            return _readings(projection, select, tables)
    return readings


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
