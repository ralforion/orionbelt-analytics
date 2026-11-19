"""Table families: copies of one schema told apart by a table-name prefix.

OLD_ROOMS beside NEW_ROOMS, EU_ORDERS beside US_ORDERS: a join from one copy
into the other runs and pairs rows of two datasets. Families are found from
names, columns and declared keys; relationships are inferred within a copy,
the ontology records each table's family, and OBQC warns on a join that
crosses into another copy.
"""

import json
import random
import re
import tracemalloc
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

import src.main as main_module
import src.server_state as state_module
from src.database_manager import ColumnInfo, TableInfo
from src.handlers import connection as connection_handler
from src.handlers.schema import MAX_LISTED_FAMILIES, _family_groups
from src.main import mcp
from src.obqc_validator import OBQCIssueType, OBQCValidator
from src.ontology_generator import OntologyGenerator
from src.server_state import ServerState
from src.table_families import (
    _HISTORY,
    FamilyIndex,
    _confirm,
    _signatures,
    _Table,
    _without,
    find_families,
    split_name,
)

SHAPES = {
    "BUILDING": ["ID", "NAME", "CITY"],
    "ROOMS": ["ID", "BUILDING_ID", "FLOOR", "CAPACITY"],
    "BOOKINGS": ["ID", "ROOM_ID", "GUEST_ID", "CHECKIN", "CHECKOUT"],
    "GUESTS": ["ID", "NAME", "EMAIL"],
}


def _table(
    name: str,
    columns: list[str],
    pk: str | None = "ID",
    schema: str = "dw",
    fks: list[dict[str, str]] | None = None,
) -> TableInfo:
    return TableInfo(
        name,
        schema,
        [
            ColumnInfo(
                c, "INTEGER" if c.endswith("ID") else "VARCHAR", True, c == pk, False
            )
            for c in columns
        ],
        [pk] if pk else [],
        fks or [],
    )


def _copies(*prefixes: str, shapes: dict[str, list[str]] = SHAPES) -> list[TableInfo]:
    return [
        _table(f"{p}_{base}", cols) for p in prefixes for base, cols in shapes.items()
    ]


def _fk(column: str, table: str, referenced: str = "ID") -> dict[str, str]:
    return {
        "column": column,
        "referenced_table": table,
        "referenced_column": referenced,
    }


# --- Finding them -----------------------------------------------------------------


def test_a_name_splits_at_its_first_token() -> None:
    assert split_name("old_rooms") == ("OLD", "ROOMS")
    assert split_name("EU-Order Lines") == ("EU", "ORDER_LINES")
    assert split_name("rooms") is None


def test_two_copies_of_a_schema_are_two_families() -> None:
    (group,) = find_families(
        [*_copies("OLD", "NEW"), _table("DIM_DATE", ["ID", "DAY"])]
    )
    assert group.schema == "dw"
    assert group.families == {
        "NEW": ("NEW_BOOKINGS", "NEW_BUILDING", "NEW_GUESTS", "NEW_ROOMS"),
        "OLD": ("OLD_BOOKINGS", "OLD_BUILDING", "OLD_GUESTS", "OLD_ROOMS"),
    }
    assert group.copied == ("BOOKINGS", "BUILDING", "GUESTS", "ROOMS")


def test_columns_are_compared_without_their_tables_prefix() -> None:
    tables = [
        _table(f"{p}_{base}", [f"{p}_{c}" for c in cols], pk=f"{p}_ID")
        for p in ("OLD", "NEW")
        for base, cols in SHAPES.items()
    ]
    (group,) = find_families(tables)
    assert set(group.families) == {"NEW", "OLD"}


def test_two_shared_names_are_no_family() -> None:
    two = {k: SHAPES[k] for k in ("BUILDING", "ROOMS")}
    assert find_families(_copies("OLD", "NEW", shapes=two)) == []


def test_a_mirrored_foreign_key_counts_as_evidence() -> None:
    # Two copies and a key declared in both: three pieces of evidence.
    tables = [_table(f"{p}_BUILDING", SHAPES["BUILDING"]) for p in ("OLD", "NEW")] + [
        _table(f"{p}_ROOMS", SHAPES["ROOMS"], fks=[_fk("BUILDING_ID", f"{p}_BUILDING")])
        for p in ("OLD", "NEW")
    ]
    (group,) = find_families(tables)
    assert group.copied == ("BUILDING", "ROOMS")


def test_a_mirrored_foreign_key_lowers_the_bar_for_its_ends() -> None:
    # The ROOMS copies share 3 of 5 columns (Jaccard 0.6): copies only as ends
    # of a key both families declare.
    def rooms(prefix: str, extra: str, fk: bool) -> TableInfo:
        return _table(
            f"{prefix}_ROOMS",
            ["ID", "BUILDING_ID", "FLOOR", extra],
            fks=[_fk("BUILDING_ID", f"{prefix}_BUILDING")] if fk else [],
        )

    common = [
        _table(f"{p}_{b}", SHAPES[b])
        for p in ("OLD", "NEW")
        for b in ("BUILDING", "GUESTS")
    ]
    linked = find_families(
        [*common, rooms("OLD", "SEATS", True), rooms("NEW", "AREA", True)]
    )
    assert "ROOMS" in linked[0].copied
    unlinked = find_families(
        [*common, rooms("OLD", "SEATS", False), rooms("NEW", "AREA", False)]
    )
    assert unlinked == []  # BUILDING and GUESTS alone are two


def test_history_tables_are_no_copies() -> None:
    hist = {f"{b}_HIST": cols for b, cols in SHAPES.items() if b != "GUESTS"}
    tables = _copies("OLD", "NEW", shapes={**hist, "GUESTS": SHAPES["GUESTS"]})
    assert find_families(tables) == []


def test_three_versions_are_one_group() -> None:
    (group,) = find_families(_copies("V1", "V2", "V3"))
    assert list(group.families) == ["V1", "V2", "V3"]


def test_a_family_holds_every_table_of_its_prefix() -> None:
    tables = [*_copies("OLD", "NEW"), _table("OLD_AUDIT", ["ID", "NOTE"])]
    (group,) = find_families(tables)
    assert "OLD_AUDIT" in group.families["OLD"]
    assert FamilyIndex.of([group]).family("dw", "old_audit") == "OLD"


def test_copies_in_two_schemas_are_no_family() -> None:
    tables = [
        _table(f"{p}_{b}", cols, schema=schema)
        for p, schema in (("OLD", "a"), ("NEW", "b"))
        for b, cols in SHAPES.items()
    ]
    assert find_families(tables) == []


def test_a_star_schema_is_no_family() -> None:
    star = [
        _table("FACT_SALES", ["ID", "CUSTOMER_ID", "PRODUCT_ID", "AMOUNT"]),
        _table("DIM_CUSTOMER", ["ID", "NAME"]),
        _table("DIM_PRODUCT", ["ID", "NAME"]),
    ]
    assert find_families(star) == []


def _peak(work: Any) -> tuple[Any, int]:
    """*work*'s result, and the memory it held at its peak, in bytes."""
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        result = work()
        return result, tracemalloc.get_traced_memory()[1] - before
    finally:
        tracemalloc.stop()


def test_prefixes_sharing_one_name_are_never_paired() -> None:
    # TENANT0_EVENTS ... TENANT999_EVENTS: 499,500 pairs of prefixes that
    # share one name, none of which can reach the evidence. Keeping them took
    # 128 MiB.
    tables = [_table(f"TENANT{i}_EVENTS", ["ID", "KIND", "AT"]) for i in range(1000)]
    groups, peak = _peak(lambda: find_families(tables))
    assert groups == []
    assert peak < 4 * 2**20, f"held {peak / 2**20:.0f} MiB"


def test_identical_tenants_are_compared_once() -> None:
    # A family per tenant: one class of identical prefixes, not 499,500 pairs.
    shapes = {k: SHAPES[k] for k in ("BUILDING", "ROOMS", "GUESTS")}
    tables = [
        _table(f"T{i}_{base}", cols)
        for i in range(1000)
        for base, cols in shapes.items()
    ]
    groups, peak = _peak(lambda: find_families(tables))
    (group,) = groups
    assert len(group.families) == 1000
    assert group.copied == ("BUILDING", "GUESTS", "ROOMS")
    assert peak < 16 * 2**20, f"held {peak / 2**20:.0f} MiB"


def _every_pair(tables: list[TableInfo]) -> dict[frozenset[str], set[str]]:
    """The families by comparing every pair of prefixes: the rule, slowly."""
    prefixed: dict[str, dict[str, _Table]] = defaultdict(dict)
    kept: dict[str, _Table] = {}
    for info in tables:
        split = split_name(info.name)
        assert split is not None
        prefix, base = split
        if base not in prefixed[prefix]:
            table = _Table(
                info.name,
                prefix,
                base,
                frozenset(_without(c.name, prefix) for c in info.columns),
                base.rsplit("_", 1)[-1] in _HISTORY,
            )
            prefixed[prefix][base] = kept[info.name.lower()] = table
    signatures = _signatures("dw", tables, kept)
    parent = {p: p for p in prefixed}

    def root(p: str) -> str:
        while parent[p] != p:
            p = parent[p]
        return p

    copied: dict[str, set[str]] = defaultdict(set)
    for p, q in combinations(sorted(prefixed), 2):
        shared = sorted(prefixed[p].keys() & prefixed[q].keys())
        copies = _confirm(
            [(prefixed[p][b], prefixed[q][b]) for b in shared],
            signatures[p] & signatures[q],
        )
        if copies:
            parent[root(q)] = root(p)
            copied[p].update(copies)
    groups: dict[str, set[str]] = defaultdict(set)
    for p in prefixed:
        if any(root(p) == root(o) for o in prefixed if o != p):
            groups[root(p)].add(p)
    return {
        frozenset(members): set().union(*(copied[m] for m in members))
        for members in groups.values()
    }


@pytest.mark.parametrize("seed", range(10))
def test_the_shortcuts_find_what_comparing_every_pair_finds(seed: int) -> None:
    # Pruning prefixes, classes of identical ones and counting shared names
    # must change nothing: random catalogs with copies, edits, history tables,
    # mirrored keys and prefixes repeated exactly.
    rng = random.Random(seed)
    bases = {f"B{i}": [f"C{i}_{j}" for j in range(rng.randint(2, 5))] for i in range(6)}
    bases["B0_HIST"] = bases["B0"]
    links = [("B1", "B0"), ("B2", "B1"), ("B3", "B0")]
    shapes: list[list[TableInfo]] = []
    for p in range(9):
        prefix = f"P{p}"
        if shapes and rng.random() < 0.25:  # the same tables as another prefix
            source = rng.choice(shapes)
            old = split_name(source[0].name)
            assert old is not None
            tables = [
                _table(
                    t.name.replace(old[0], prefix, 1),
                    [c.name for c in t.columns],
                    fks=[
                        _fk(
                            f["column"],
                            f["referenced_table"].replace(old[0], prefix, 1),
                        )
                        for f in t.foreign_keys
                    ],
                )
                for t in source
            ]
        else:
            tables = []
            held = [b for b in bases if rng.random() < 0.7]
            for base in held:
                cols = ["ID"] + [c for c in bases[base] if rng.random() > 0.15]
                cols += [f"X{rng.randint(0, 3)}"] if rng.random() < 0.3 else []
                fks = [
                    _fk(f"{to}_ID", f"{prefix}_{to}")
                    for frm, to in links
                    if frm == base and to in held and rng.random() < 0.6
                ]
                tables.append(_table(f"{prefix}_{base}", cols, fks=fks))
        if tables:
            shapes.append(tables)
    catalog = [t for tables in shapes for t in tables]
    expected = _every_pair(catalog)
    found = {frozenset(g.families): set(g.copied) for g in find_families(catalog)}
    assert found == expected


# --- Relationships ----------------------------------------------------------------


def _inferred(tables: list[TableInfo]) -> set[str]:
    return {
        f"{r.source_table}.{r.column} -> {r.target_table}.{r.target_column}"
        for r in OntologyGenerator().infer_relationships(tables)
    }


def test_keys_are_inferred_within_each_copy() -> None:
    assert _inferred(_copies("OLD", "NEW")) == {
        f"{p}_{source} -> {p}_{target}.ID"
        for p in ("OLD", "NEW")
        for source, target in (
            ("ROOMS.BUILDING_ID", "BUILDING"),
            ("BOOKINGS.ROOM_ID", "ROOMS"),
            ("BOOKINGS.GUEST_ID", "GUESTS"),
        )
    }


def test_a_fact_table_finds_its_dimensions() -> None:
    star = [
        _table("FACT_SALES", ["SALE_ID", "CUSTOMER_ID", "PRODUCT_ID"], pk="SALE_ID"),
        _table("DIM_CUSTOMER", ["CUSTOMER_ID", "NAME"], pk="CUSTOMER_ID"),
        # No key declared, as warehouses often have none.
        _table("DIM_PRODUCT", ["PRODUCT_ID", "NAME"], pk=None),
    ]
    assert _inferred(star) == {
        "FACT_SALES.CUSTOMER_ID -> DIM_CUSTOMER.CUSTOMER_ID",
        "FACT_SALES.PRODUCT_ID -> DIM_PRODUCT.PRODUCT_ID",
    }
    (high,) = [
        r
        for r in OntologyGenerator().infer_relationships(star)
        if r.target_table == "DIM_CUSTOMER"
    ]
    assert high.confidence == "high"  # customer names DIM_CUSTOMER exactly


def test_every_name_form_is_looked_at_before_another_prefix() -> None:
    # customer_id is spelled like US_CUSTOMER, but EU_ORDERS has its own
    # EU_CUSTOMERS.
    tables = [
        _table("EU_ORDERS", ["ID", "CUSTOMER_ID"]),
        _table("EU_CUSTOMERS", ["ID", "NAME"]),
        _table("US_CUSTOMER", ["ID", "NAME"]),
    ]
    assert _inferred(tables) == {"EU_ORDERS.CUSTOMER_ID -> EU_CUSTOMERS.ID"}


def test_copies_it_cannot_choose_between_are_no_relationship() -> None:
    tables = [
        _table("OLD_ROOMS", SHAPES["ROOMS"]),
        _table("NEW_ROOMS", SHAPES["ROOMS"]),
        _table("BOOKINGS", ["ID", "ROOM_ID"]),
    ]
    assert not any(r.startswith("BOOKINGS.") for r in _inferred(tables))


def test_a_compound_name_is_no_prefix() -> None:
    # order_items holds the items of orders: item_id is no key of it.
    tables = [
        _table("REVIEWS", ["ID", "ITEM_ID", "STARS"]),
        _table("ORDER_ITEMS", ["ID", "ORDER_ID", "QTY"]),
        _table("ORDERS", ["ID", "TOTAL"]),
    ]
    assert _inferred(tables) == {"ORDER_ITEMS.ORDER_ID -> ORDERS.ID"}


def test_a_family_prefix_is_one_beside_a_table_named_like_it() -> None:
    # NEWS is a table, so NEW_ could read as a compound name; as a family's
    # prefix it is a prefix, and NEW_ROOMS keeps to NEW_BUILDING.
    tables = [*_copies("OLD", "NEW"), _table("NEWS", ["ID", "HEADLINE"])]
    inferred = _inferred(tables)
    assert "NEW_ROOMS.BUILDING_ID -> NEW_BUILDING.ID" in inferred
    assert not any("NEW_" in r.split(" -> ")[0] and "OLD_" in r for r in inferred)


def test_no_key_is_inferred_into_another_copy() -> None:
    # OLD keeps no ROOMS of its own: NEW_ROOMS is still NEW's, as NEW has its
    # own BOOKINGS, so OLD_BOOKINGS.ROOM_ID gets no relationship...
    tables = [
        *_copies(
            "OLD",
            "NEW",
            shapes={k: SHAPES[k] for k in ("BUILDING", "GUESTS", "BOOKINGS")},
        ),
        _table("NEW_ROOMS", SHAPES["ROOMS"]),
    ]
    assert not any(r.startswith("OLD_BOOKINGS.ROOM_ID") for r in _inferred(tables))
    # ...while a table only one family has, and no other has a copy of, is
    # shared by all of them.
    tables = [
        *_copies(
            "OLD", "NEW", shapes={k: SHAPES[k] for k in ("BUILDING", "GUESTS", "ROOMS")}
        ),
        _table("NEW_RATES", ["ID", "PRICE"]),
        _table("OLD_DEALS", ["ID", "RATE_ID"]),
    ]
    assert "OLD_DEALS.RATE_ID -> NEW_RATES.ID" in _inferred(tables)


def test_a_tables_own_key_names_no_copy_of_it() -> None:
    # Keyless copies: customer_id in OLD_CUSTOMER is its own key, not
    # NEW_CUSTOMER's, as customer_id in CUSTOMER would be.
    tables = [
        _table(f"{p}_{base}", columns, pk=None)
        for p in ("OLD", "NEW")
        for base, columns in {
            "CUSTOMER": ["CUSTOMER_ID", "NAME", "EMAIL"],
            "PRODUCT": ["PRODUCT_ID", "NAME", "PRICE"],
            "ORDER": ["ORDER_ID", "CUSTOMER_ID", "PRODUCT_ID", "QTY"],
        }.items()
    ]
    assert _inferred(tables) == {
        f"{p}_ORDER.{key}_ID -> {p}_{key}.{key}_ID"
        for p in ("OLD", "NEW")
        for key in ("CUSTOMER", "PRODUCT")
    }


def test_a_whole_name_still_wins_over_a_prefixed_one() -> None:
    tables = [
        _table("ROOMS", SHAPES["ROOMS"]),
        _table("BUILDING", SHAPES["BUILDING"]),
        _table("OLD_BUILDING", SHAPES["BUILDING"]),
    ]
    assert "ROOMS.BUILDING_ID -> BUILDING.ID" in _inferred(tables)


def test_a_key_is_inferred_only_to_a_column_that_exists() -> None:
    # Without a declared key the target's column of the same name, else id;
    # a table with neither gets no relationship rather than one to "id".
    sales = _table("SALES", ["SALE_ID", "CUSTOMER_ID"], pk="SALE_ID")
    named = _table("CUSTOMER", ["CUSTOMER_ID", "NAME"], pk=None)
    neither = _table("CUSTOMER", ["NAME", "EMAIL"], pk=None)
    assert _inferred([sales, named]) == {"SALES.CUSTOMER_ID -> CUSTOMER.CUSTOMER_ID"}
    assert _inferred([sales, neither]) == set()


# --- The ontology and OBQC ----------------------------------------------------------


def _ontology(tables: list[TableInfo]) -> OntologyGenerator:
    generator = OntologyGenerator(base_uri="http://example.com/ontology/")
    generator.generate_from_schema(tables)
    return generator


def test_the_ontology_records_each_tables_family() -> None:
    generator = _ontology([*_copies("OLD", "NEW"), _table("DIM_DATE", ["ID", "DAY"])])
    families = {
        str(generator.graph.value(s, generator.oba_ns.tableName)): str(o)
        for s, o in generator.graph.subject_objects(generator.oba_ns.tableFamily)
    }
    assert families["OLD_ROOMS"] == "OLD" and families["NEW_GUESTS"] == "NEW"
    assert "DIM_DATE" not in families


def test_an_ontology_with_families_conforms_to_the_shapes() -> None:
    from src.shacl_validator import shacl_available, validate_ontology

    if not shacl_available():
        pytest.skip("pyshacl not installed")
    ttl = OntologyGenerator(
        base_uri="http://example.com/ontology/"
    ).generate_from_schema(_copies("OLD", "NEW"))
    report = validate_ontology(ttl)
    assert report["conforms"], report.get("report")


def _validator(tables: list[TableInfo]) -> OBQCValidator:
    validator = OBQCValidator()
    validator.load_ontology(_ontology(tables).graph, "http://example.com/ontology/")
    return validator


def _family_warnings(validator: OBQCValidator, sql: str) -> list[Any]:
    return [
        i
        for i in validator.validate(sql, dialect="duckdb").issues
        if i.issue_type == OBQCIssueType.CROSS_FAMILY_JOIN
    ]


@pytest.fixture(scope="module")
def copies() -> OBQCValidator:
    return _validator(
        [
            *_copies("OLD", "NEW"),
            _table("DIM_DATE", ["ID", "DAY"]),
            _table("OLD_LEGACY_RATES", ["ID", "RATE"]),
            _table("NEW_PAYMENTS", ["ID", "AMOUNT"]),
        ]
    )


def test_a_join_into_another_copy_is_warned(copies: OBQCValidator) -> None:
    (warning,) = _family_warnings(
        copies,
        "SELECT b.NAME, COUNT(*) FROM OLD_ROOMS r "
        "JOIN NEW_BUILDING b ON r.BUILDING_ID = b.ID GROUP BY b.NAME",
    )
    assert "OLD_ROOMS (family OLD)" in warning.message
    assert "NEW_BUILDING (family NEW)" in warning.message
    assert warning.suggestion == (
        "Join within one copy: OLD_ROOMS with OLD_BUILDING or NEW_ROOMS with NEW_BUILDING"
    )
    assert warning.severity.value == "warning"


def test_a_comma_join_into_another_copy_is_warned(copies: OBQCValidator) -> None:
    sql = "SELECT r.ID FROM OLD_ROOMS r, NEW_BUILDING b WHERE r.BUILDING_ID = b.ID"
    assert len(_family_warnings(copies, sql)) == 1


@pytest.mark.parametrize(
    "sql",
    [
        # Within one copy.
        "SELECT r.ID FROM OLD_ROOMS r JOIN OLD_BUILDING b ON r.BUILDING_ID = b.ID",
        # Two copies of one table compared on purpose.
        "SELECT o.ID FROM OLD_ROOMS o JOIN NEW_ROOMS n ON o.ID = n.ID",
        # A shared table, in no family.
        "SELECT b.ID FROM OLD_BOOKINGS b JOIN DIM_DATE d ON b.CHECKIN = d.ID",
        # Neither family has the other's table.
        "SELECT r.ID FROM OLD_LEGACY_RATES r JOIN NEW_PAYMENTS p ON r.ID = p.ID",
    ],
)
def test_joins_that_stay_in_a_copy_are_not_warned(
    copies: OBQCValidator, sql: str
) -> None:
    assert _family_warnings(copies, sql) == []


def test_a_filter_in_on_joins_nothing(copies: OBQCValidator) -> None:
    # o.FLOOR > 0 names OLD_ROOMS in NEW_BUILDING's ON without joining it:
    # the same query with the filter in WHERE.
    sql = (
        "SELECT o.ID FROM OLD_ROOMS o JOIN NEW_ROOMS n ON o.ID = n.ID "
        "JOIN NEW_BUILDING b ON n.BUILDING_ID = b.ID AND o.FLOOR > 0"
    )
    assert _family_warnings(copies, sql) == []
    # An equality that does join across, written in that ON, still counts.
    crossing = (
        "SELECT o.ID FROM OLD_ROOMS o JOIN NEW_ROOMS n ON o.ID = n.ID "
        "JOIN NEW_BUILDING b ON n.BUILDING_ID = b.ID AND o.BUILDING_ID = b.ID"
    )
    (warning,) = _family_warnings(copies, crossing)
    assert "OLD_ROOMS (family OLD)" in warning.message


def test_either_alternative_of_an_on_pairs_rows(copies: OBQCValidator) -> None:
    # Both alternatives join old rooms to new buildings, as the same test in
    # WHERE does.
    on = (
        "SELECT r.ID FROM OLD_ROOMS r JOIN NEW_BUILDING b "
        "ON r.BUILDING_ID = b.ID OR r.BACKUP_BUILDING_ID = b.ID"
    )
    where = (
        "SELECT r.ID FROM OLD_ROOMS r, NEW_BUILDING b "
        "WHERE r.BUILDING_ID = b.ID OR r.BACKUP_BUILDING_ID = b.ID"
    )
    assert len(_family_warnings(copies, on)) == 1
    assert len(_family_warnings(copies, where)) == 1


def test_a_cte_named_like_a_table_is_no_copy(copies: OBQCValidator) -> None:
    # This OLD_ROOMS reads new rooms, joined in either order; so does a
    # derived table aliased like one.
    cte = "WITH OLD_ROOMS AS (SELECT * FROM NEW_ROOMS) "
    for sql in (
        cte + "SELECT r.ID FROM OLD_ROOMS r JOIN NEW_BUILDING b "
        "ON r.BUILDING_ID = b.ID",
        cte + "SELECT r.ID FROM NEW_BUILDING b JOIN OLD_ROOMS r "
        "ON r.BUILDING_ID = b.ID",
        "SELECT b.ID FROM (SELECT * FROM NEW_ROOMS) OLD_ROOMS "
        "JOIN NEW_BUILDING b ON OLD_ROOMS.BUILDING_ID = b.ID",
    ):
        assert _family_warnings(copies, sql) == [], sql
    # A name qualified by its schema is the table, CTE or not.
    for sql in (
        cte + "SELECT r.ID FROM dw.OLD_ROOMS r JOIN dw.NEW_BUILDING b "
        "ON r.BUILDING_ID = b.ID",
        cte + "SELECT r.ID FROM dw.NEW_BUILDING b JOIN dw.OLD_ROOMS r "
        "ON r.BUILDING_ID = b.ID",
    ):
        assert len(_family_warnings(copies, sql)) == 1, sql
    # A CTE's own join is read in its own SELECT, and warned there.
    inside = (
        "WITH stays AS (SELECT r.ID FROM OLD_ROOMS r "
        "JOIN NEW_BUILDING b ON r.BUILDING_ID = b.ID) SELECT * FROM stays"
    )
    assert len(_family_warnings(copies, inside)) == 1


def test_an_ontology_without_families_warns_of_none() -> None:
    generator = _ontology(_copies("OLD", "NEW"))
    for triple in list(
        generator.graph.triples((None, generator.oba_ns.tableFamily, None))
    ):
        generator.graph.remove(triple)
    validator = OBQCValidator()
    validator.load_ontology(generator.graph, "http://example.com/ontology/")
    sql = "SELECT r.ID FROM OLD_ROOMS r JOIN NEW_BUILDING b ON r.BUILDING_ID = b.ID"
    assert _family_warnings(validator, sql) == []


# --- Discovery ----------------------------------------------------------------------


def test_discovery_lists_the_families() -> None:
    section = _family_groups(
        [*_copies("OLD", "NEW"), _table("DIM_DATE", ["ID", "DAY"])]
    )
    (group,) = section["table_families"]
    assert group == {
        "schema": "dw",
        "families": {
            "NEW": ["NEW_BOOKINGS", "NEW_BUILDING", "NEW_GUESTS", "NEW_ROOMS"],
            "OLD": ["OLD_BOOKINGS", "OLD_BUILDING", "OLD_GUESTS", "OLD_ROOMS"],
        },
        "copied_tables": ["BOOKINGS", "BUILDING", "GUESTS", "ROOMS"],
    }
    assert "table_families_note" in section
    assert _family_groups(_copies("OLD")) == {}


def test_discovery_lists_a_few_families_and_counts_the_rest() -> None:
    tables = [_table(f"T{i:02d}_{b}", c) for i in range(25) for b, c in SHAPES.items()]
    (group,) = _family_groups(tables)["table_families"]
    assert list(group["families"]) == [f"T{i:02d}" for i in range(MAX_LISTED_FAMILIES)]
    assert group["more_families"] == 25 - MAX_LISTED_FAMILIES


@pytest.fixture
def hotel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    import duckdb

    path = tmp_path / "hotel.duckdb"
    con = duckdb.connect(str(path))
    for prefix in ("old", "new"):
        con.execute(
            f"CREATE TABLE {prefix}_building (id INTEGER PRIMARY KEY, name VARCHAR, city VARCHAR)"
        )
        con.execute(
            f"CREATE TABLE {prefix}_rooms (id INTEGER PRIMARY KEY, building_id INTEGER, "
            "floor INTEGER, capacity INTEGER)"
        )
        con.execute(
            f"CREATE TABLE {prefix}_guests (id INTEGER PRIMARY KEY, name VARCHAR, email VARCHAR)"
        )
        con.execute(
            f"INSERT INTO {prefix}_building VALUES (1, 'North', 'Oslo'), (2, 'South', 'Bergen')"
        )
        con.execute(f"INSERT INTO {prefix}_rooms VALUES (10, 1, 1, 2), (11, 2, 3, 4)")
    con.close()
    fresh = ServerState()
    monkeypatch.setattr(state_module, "_server_state", fresh)
    monkeypatch.setattr(main_module, "_server_state", fresh)
    for module in ("src.paths", "src.handlers.ontology_validation"):
        monkeypatch.setattr(f"{module}.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(connection_handler, "detect_workspace", lambda _cid: None)
    monkeypatch.setenv("AUTO_GRAPHRAG", "false")
    monkeypatch.delenv("OBA_DATABASES", raising=False)
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("DUCKDB_DATABASE_PATH", str(path))
    yield tmp_path
    fresh.cleanup()


def _data(result: Any) -> Any:
    return result.data if not isinstance(result.data, str) else json.loads(result.data)


async def test_through_the_tools(hotel: Path) -> None:
    async with Client(mcp) as client:
        connected = await client.call_tool("connect_database", {"db_type": "duckdb"})
        text = (
            connected.data
            if isinstance(connected.data, str)
            else json.dumps(connected.data)
        )
        match = re.search(r"(ob_[a-z0-9]{6})", text)
        assert match
        on = {"connection": match.group(1)}

        found = _data(
            await client.call_tool("discover_schema", {**on, "schema_name": "main"})
        )
        (group,) = found["table_families"]
        assert set(group["families"]) == {"NEW", "OLD"}
        assert group["copied_tables"] == ["BUILDING", "GUESTS", "ROOMS"]

        await client.call_tool("generate_ontology", {**on, "schema_name": "main"})
        crossed = _data(
            await client.call_tool(
                "execute_sql_query",
                {
                    **on,
                    "checklist_completed": True,
                    "sql_query": "SELECT r.id, b.name FROM main.old_rooms r "
                    "JOIN main.new_building b ON r.building_id = b.id",
                },
            )
        )
        assert crossed["success"] is True, crossed  # it runs: that is the trouble
        assert any(
            "family OLD" in w and "new_building" in w for w in crossed["warnings"]
        )

        within = _data(
            await client.call_tool(
                "execute_sql_query",
                {
                    **on,
                    "checklist_completed": True,
                    "sql_query": "SELECT r.id, b.name FROM main.old_rooms r "
                    "JOIN main.old_building b ON r.building_id = b.id",
                },
            )
        )
        assert within["success"] is True
        assert not any("family" in w for w in within.get("warnings", []))
