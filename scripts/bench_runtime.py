#!/usr/bin/env python3
"""Repeatable runtime measurements for the paths performance work touched.

Every number in a performance commit here was produced by a throwaway script,
which makes it unrepeatable the moment the branch lands. This is the same work
as a committed, opt-in tool: it builds its own schemas, its own DuckDB file and
its own Chroma store in a temporary directory, never reads ``.env`` and never
touches a configured workspace, so it can run on a laptop without a database.

    uv run python scripts/bench_runtime.py                  # everything, TF-IDF
    uv run python scripts/bench_runtime.py --list
    uv run python scripts/bench_runtime.py --only reflect_schema --tables 200
    uv run python scripts/bench_runtime.py --backend minilm  # needs the model
    uv run python scripts/bench_runtime.py --json out.json

Nothing here runs in CI. It is a measuring tool, not a test: it asserts only
the equivalences that make a measurement meaningful, and a regression shows up
as a number, not a failure.

**Event-loop lag** is measured the way it matters to this server: a ticker task
counts how often it gets to run while the work is in flight, and the longest
gap between two of its turns is how long the loop was unavailable. One tick and
a gap the length of the whole operation means every other session was frozen.

**Database round trips** are counted with a SQLAlchemy ``before_cursor_execute``
listener, so reflection and query benchmarks report statements, not guesses.

What this does *not* cover, and the plan still asks for: custom ontologies
through the tool layer, and worker queueing. Those need the MCP layer in the
loop, not just the services.

The MiniLM backend is never selected implicitly: it is the server's default,
but it downloads a model on first use, so it has to be asked for.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TFIDF = "tfidf"
MINILM = "minilm"


# ----------------------------------------------------------------------------
# Result plumbing
# ----------------------------------------------------------------------------


@dataclass
class Measurement:
    """One benchmark's outcome: its samples and whatever else it counted."""

    name: str
    unit: str = "ms"
    samples: list[float] = field(default_factory=list)
    counters: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def summary(self) -> dict[str, Any]:
        """The samples reduced to the figures worth printing.

        Returns:
            Median, p95, min and max, plus every counter the benchmark kept.
        """
        ordered = sorted(self.samples)
        summary: dict[str, Any] = {
            "name": self.name,
            "unit": self.unit,
            "runs": len(ordered),
        }
        if ordered:
            summary |= {
                "p50": round(statistics.median(ordered), 3),
                "p95": round(
                    ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3
                ),
                "min": round(ordered[0], 3),
                "max": round(ordered[-1], 3),
            }
        summary |= self.counters
        if self.notes:
            summary["notes"] = self.notes
        return summary


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


async def _with_loop_lag(
    work: Callable[[], Coroutine[Any, Any, Any]],
) -> tuple[Any, float, int, float]:
    """Run *work*, reporting how available the event loop stayed.

    Args:
        work: A no-argument callable returning the coroutine to run.

    Returns:
        The work's result, its duration in ms, how many turns a trivial task
        got while it ran, and the longest gap between two of those turns in ms.
    """
    gaps: list[float] = []
    stop = False

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticking = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    started = time.perf_counter()
    result = await work()
    elapsed = _ms(started)
    stop = True
    await ticking
    return result, elapsed, len(gaps), (max(gaps) * 1000 if gaps else float("nan"))


# ----------------------------------------------------------------------------
# Fixtures, all synthetic and local
# ----------------------------------------------------------------------------


def _schema_dicts(
    prefix: str, tables: int, columns: int, schema: str = "public"
) -> list[dict[str, Any]]:
    """A chain of tables, each referencing the one before it."""
    return [
        {
            "name": f"{prefix}{i}",
            "schema": schema,
            "comment": f"business entity {i}",
            "columns": [
                {
                    "name": f"col_{c}",
                    "data_type": "INTEGER",
                    "is_nullable": True,
                    "is_primary_key": c == 0,
                    "is_foreign_key": c == 1 and i > 0,
                    "foreign_key_table": f"{prefix}{i - 1}" if (c == 1 and i) else None,
                    "foreign_key_column": "col_0" if (c == 1 and i) else None,
                }
                for c in range(columns)
            ],
            "primary_keys": ["col_0"],
            "foreign_keys": (
                []
                if i == 0
                else [
                    {
                        "column": "col_1",
                        "referenced_table": f"{prefix}{i - 1}",
                        "referenced_column": "col_0",
                    }
                ]
            ),
        }
        for i in range(tables)
    ]


def _star(dimensions: int) -> list[dict[str, Any]]:
    """One fact table joined to N dimensions, the shape join lookups walk."""
    fact = {
        "name": "fact",
        "schema": "public",
        "columns": [{"name": "id", "data_type": "INTEGER"}],
        "foreign_keys": [
            {
                "column": f"d{i}_id",
                "referenced_table": f"dim{i}",
                "referenced_column": "id",
            }
            for i in range(dimensions)
        ],
    }
    dims = [
        {
            "name": f"dim{i}",
            "schema": "public",
            "columns": [{"name": "id", "data_type": "INTEGER"}],
            "foreign_keys": [],
        }
        for i in range(dimensions)
    ]
    return [fact, *dims]


def _duckdb_with_tables(path: Path, tables: int, columns: int) -> Any:
    """A connected manager over a DuckDB file holding a chain of tables."""
    from src.database_manager import DatabaseManager

    manager = DatabaseManager()
    if not manager.connect_duckdb(str(path)):
        raise RuntimeError("could not connect to the benchmark DuckDB file")
    with manager.engine.connect() as conn:
        for i in range(tables):
            body = ", ".join(f"c{j} INTEGER" for j in range(columns))
            reference = f", ref INTEGER REFERENCES t{i - 1}(id)" if i else ""
            conn.exec_driver_sql(
                f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY, {body}{reference})"
            )
        conn.commit()
    return manager


class _RoundTrips:
    """Counts statements a SQLAlchemy engine actually sends."""

    def __init__(self, engine: Any) -> None:
        from sqlalchemy import event

        self.count = 0
        self._engine = engine
        self._event = event

        def counted(*_args: Any, **_kwargs: Any) -> None:
            self.count += 1

        self._listener = counted

    def __enter__(self) -> Self:
        self._event.listen(self._engine, "before_cursor_execute", self._listener)
        self.count = 0
        return self

    def __exit__(self, *_exc: object) -> None:
        self._event.remove(self._engine, "before_cursor_execute", self._listener)


# ----------------------------------------------------------------------------
# Benchmarks
# ----------------------------------------------------------------------------


def bench_join_lookup(opts: argparse.Namespace, workdir: Path) -> Measurement:
    """Join-path lookups over a star schema, the undirected-snapshot path."""
    from src.graphrag.retriever import GraphRetriever

    retriever = GraphRetriever()
    retriever.build_graph(_star(opts.tables))
    retriever.find_join_path("dim0", "dim1")

    measurement = Measurement("join_lookup", notes=f"{opts.tables}-dimension star")
    for _ in range(opts.repeat):
        started = time.perf_counter()
        for i in range(1, min(opts.tables, 20)):
            retriever.find_join_path("dim0", f"dim{i}")
        measurement.samples.append(_ms(started) / min(opts.tables - 1, 19))

    # The cost the snapshot removes, for comparison.
    fresh: list[float] = []
    for _ in range(opts.repeat):
        retriever._undirected = None
        started = time.perf_counter()
        retriever.find_join_path("dim0", "dim1")
        fresh.append(_ms(started))
    measurement.counters["without_snapshot_p50_ms"] = round(statistics.median(fresh), 3)
    return measurement


def bench_embed_schema(opts: argparse.Namespace, workdir: Path) -> Measurement:
    """Embedding a schema, batched, on the chosen backend."""
    from src.graphrag.embedder import SchemaEmbedder

    tables = _schema_dicts("t", opts.tables, opts.columns)
    measurement = Measurement(
        "embed_schema", notes=f"{opts.tables} tables x {opts.columns} columns"
    )
    elements = 0
    for _ in range(max(1, opts.repeat // 2)):
        embedder = SchemaEmbedder(opts.backend)
        started = time.perf_counter()
        result = embedder.batch_embed_schema(tables, [])
        measurement.samples.append(_ms(started))
        elements = sum(len(group) for group in result.values())
    measurement.counters |= {"backend": opts.backend, "elements": elements}
    return measurement


def bench_index_schema(opts: argparse.Namespace, workdir: Path) -> Measurement:
    """Indexing a schema into GraphRAG, and how free the loop stays."""
    import src.graphrag.vector_store_chromadb as store
    from src.graphrag.manager import GraphRAGManager

    store.OUTPUT_DIR = workdir / "chroma"
    tables = _schema_dicts("t", opts.tables, opts.columns)
    measurement = Measurement(
        "index_schema", notes=f"{opts.tables} tables x {opts.columns} columns"
    )

    async def run() -> None:
        for run_index in range(max(1, opts.repeat // 4)):
            manager = GraphRAGManager(
                embedding_model=opts.backend,
                connection_id=f"bench-index-{run_index}",
                schema_name="public",
            )
            _, elapsed, ticks, longest = await _with_loop_lag(
                lambda: manager.aindex_schema(tables, "public", accumulate=False)
            )
            measurement.samples.append(elapsed)
            measurement.counters |= {
                "loop_ticks": ticks,
                "longest_loop_block_ms": round(longest, 3),
                "backend": opts.backend,
            }

    asyncio.run(run())
    return measurement


def bench_save_state(opts: argparse.Namespace, workdir: Path) -> Measurement:
    """Persisting GraphRAG state for several accumulated schemas."""
    import src.graphrag.vector_store_chromadb as store
    from src.graphrag.manager import GraphRAGManager

    store.OUTPUT_DIR = workdir / "chroma-save"
    schemas = ["public", "analytics", "archive"]
    manager = GraphRAGManager(
        embedding_model=TFIDF, connection_id="bench-save", schema_name=schemas[0]
    )
    tables = max(4, opts.tables // 4)
    manager.initialize_from_schema(
        _schema_dicts("p", tables, opts.columns, schemas[0]), schema_name=schemas[0]
    )
    for name in schemas[1:]:
        manager.accumulate_schema(
            _schema_dicts(name[0], tables, opts.columns, name), schema_name=name
        )

    out = workdir / "state"
    manager.save_state(out)  # warm
    measurement = Measurement("save_state", notes=f"{len(schemas)} schemas")
    for _ in range(max(1, opts.repeat // 4)):
        started = time.perf_counter()
        manager.save_state(out)
        measurement.samples.append(_ms(started))

    written = list((out / "bench-save").glob("*.json"))
    measurement.counters |= {
        "files": len(written),
        "bytes": sum(path.stat().st_size for path in written),
    }
    return measurement


def bench_ontology(opts: argparse.Namespace, workdir: Path) -> Measurement:
    """Generating an ontology, then getting OBQC semantics from it."""
    from src.database_manager import TableInfo
    from src.obqc_validator import OBQCValidator, prepare_ontology
    from src.ontology_generator import OntologyGenerator

    base = "http://example.com/ontology/"
    tables = [
        TableInfo.from_dict(entry)
        for entry in _schema_dicts("t", opts.tables, opts.columns)
    ]

    generator = OntologyGenerator(base)
    started = time.perf_counter()
    turtle = generator.generate_from_schema(tables)
    generate_ms = _ms(started)

    path = workdir / "ontology.ttl"
    path.write_text(turtle, encoding="utf-8")

    measurement = Measurement(
        "ontology_to_obqc", notes=f"{opts.tables} tables, {len(turtle) / 1e6:.2f} MB"
    )
    cold: list[float] = []
    for _ in range(max(1, opts.repeat // 4)):
        started = time.perf_counter()
        reader = OntologyGenerator(base)
        reader.load_from_file(str(path))
        OBQCValidator().load_ontology(reader.graph, base)
        cold.append(_ms(started))

    prepared = prepare_ontology(generator.graph, base)
    for _ in range(opts.repeat):
        started = time.perf_counter()
        OBQCValidator().load_prepared(prepared)
        measurement.samples.append(_ms(started))

    measurement.counters |= {
        "generate_ms": round(generate_ms, 1),
        "parse_and_extract_p50_ms": round(statistics.median(cold), 1),
        "turtle_bytes": len(turtle.encode("utf-8")),
    }
    return measurement


def bench_validate_sql(opts: argparse.Namespace, workdir: Path) -> Measurement:
    """OBQC validation of a mixed query set against a prepared ontology."""
    from src.database_manager import TableInfo
    from src.obqc_validator import OBQCValidator, prepare_ontology
    from src.ontology_generator import OntologyGenerator

    base = "http://example.com/ontology/"
    tables = [
        TableInfo.from_dict(entry)
        for entry in _schema_dicts("t", min(opts.tables, 40), opts.columns)
    ]
    generator = OntologyGenerator(base)
    generator.generate_from_schema(tables)
    validator = OBQCValidator()
    validator.load_prepared(prepare_ontology(generator.graph, base))

    queries = [
        "SELECT col_0 FROM t1",
        "SELECT t1.col_0, t0.col_2 FROM t1 JOIN t0 ON t1.col_1 = t0.col_0",
        "SELECT col_0, count(*) FROM t1 GROUP BY col_0",
        "WITH recent AS (SELECT col_0 FROM t1) SELECT col_0 FROM recent",
        "SELECT sum(t0.col_2) FROM t0 JOIN t1 ON t1.col_1 = t0.col_0",
        "SELECT nosuchcolumn FROM t1",
    ]
    for sql in queries:
        validator.validate(sql)

    measurement = Measurement("validate_sql", notes=f"{len(queries)} queries per run")
    for _ in range(opts.repeat):
        started = time.perf_counter()
        for sql in queries:
            validator.validate(sql)
        measurement.samples.append(_ms(started) / len(queries))
    return measurement


def bench_reflect_schema(opts: argparse.Namespace, workdir: Path) -> Measurement:
    """Reflecting a DuckDB schema: one pass against one table at a time."""
    from src.async_utils import run_db

    manager = _duckdb_with_tables(workdir / "reflect.duckdb", opts.tables, opts.columns)
    names = manager.get_tables("main")

    bulk = manager.analyze_tables(names, "main")
    per_table = {name: manager._driver.analyze_table(name, "main") for name in names}
    from dataclasses import asdict

    mismatched = [
        name
        for name in bulk
        if per_table.get(name) is None or asdict(bulk[name]) != asdict(per_table[name])
    ]

    measurement = Measurement(
        "reflect_schema", notes=f"{len(names)} tables x {opts.columns} columns"
    )
    with _RoundTrips(manager.engine) as trips:
        for _ in range(max(1, opts.repeat // 4)):
            started = time.perf_counter()
            manager.analyze_tables(names, "main")
            measurement.samples.append(_ms(started))
        bulk_trips = trips.count / max(1, opts.repeat // 4)

    per_samples: list[float] = []
    with _RoundTrips(manager.engine) as trips:
        started = time.perf_counter()
        for name in names:
            manager._driver.analyze_table(name, "main")
        per_samples.append(_ms(started))
        per_trips = trips.count

    async def run() -> None:
        _, _, ticks, longest = await _with_loop_lag(
            lambda: run_db(manager.analyze_tables, names, "main")
        )
        measurement.counters |= {
            "loop_ticks": ticks,
            "longest_loop_block_ms": round(longest, 3),
        }

    asyncio.run(run())
    measurement.counters |= {
        "one_at_a_time_ms": round(per_samples[0], 1),
        "round_trips_one_pass": round(bulk_trips, 1),
        "round_trips_one_at_a_time": per_trips,
        "metadata_mismatches": mismatched,
    }
    return measurement


def bench_execute_query(opts: argparse.Namespace, workdir: Path) -> Measurement:
    """Executing a bounded query, and how free the loop stays while it runs."""
    from src.async_utils import run_db

    manager = _duckdb_with_tables(workdir / "query.duckdb", 2, 4)
    sql = "SELECT i FROM range(200000) AS t(i) WHERE i % 3 = 0"
    manager.execute_sql_query(sql, limit=100)

    measurement = Measurement("execute_query", notes="200k-row scan, limit 100")

    async def run() -> None:
        for _ in range(max(1, opts.repeat // 4)):
            result, elapsed, ticks, longest = await _with_loop_lag(
                lambda: run_db(manager.execute_sql_query, sql, 100)
            )
            measurement.samples.append(elapsed)
            measurement.counters |= {
                "rows": result["row_count"],
                "loop_ticks": ticks,
                "longest_loop_block_ms": round(longest, 3),
            }

    asyncio.run(run())

    with _RoundTrips(manager.engine) as trips:
        manager.execute_sql_query(sql, limit=100)
        measurement.counters["round_trips_per_query"] = trips.count
    return measurement


def bench_shared_connection(opts: argparse.Namespace, workdir: Path) -> Measurement:
    """A second session on a database another session already discovered."""
    from src.server_state import ServerState

    manager = _duckdb_with_tables(workdir / "shared.duckdb", opts.tables, opts.columns)
    names = manager.get_tables("main")

    state = ServerState()
    first = state.get_session("first")
    state.bind_session(first, "bench-conn", manager)

    # What the first session pays: reflection, and the round trips it takes.
    manager.clear_metadata_cache()
    measurement = Measurement("shared_connection", notes=f"{len(names)} tables")
    with _RoundTrips(manager.engine) as trips:
        started = time.perf_counter()
        analyzed = manager.analyze_tables(names, "main")
        cold = _ms(started)
        cold_trips = trips.count
    first.cache_schema_analysis("main", list(analyzed.values()))

    # What a second session on the same database pays: the runtime is shared,
    # so the schema is already there and the connection is already open.
    second = state.get_session("second")
    runtime = state.bind_session(second, "bench-conn", manager)
    with _RoundTrips(manager.engine) as trips:
        for _ in range(opts.repeat):
            started = time.perf_counter()
            reused = second.get_cached_schema("main")
            measurement.samples.append(_ms(started))
        warm_trips = trips.count

    measurement.counters |= {
        "first_session_ms": round(cold, 1),
        "first_session_round_trips": cold_trips,
        "second_session_round_trips": warm_trips,
        "tables_seen_by_second": len(reused or []),
        "sessions_on_runtime": runtime.holders,
        "one_manager_shared": second.db_manager is first.db_manager,
    }
    return measurement


def bench_connection_churn(opts: argparse.Namespace, workdir: Path) -> Measurement:
    """Connecting, discovering and disconnecting, over and over."""
    from src.server_state import ServerState

    path = workdir / "churn.duckdb"
    _duckdb_with_tables(path, max(4, opts.tables // 8), opts.columns).disconnect()

    from src.database_manager import DatabaseManager

    state = ServerState()
    measurement = Measurement("connection_churn", notes="connect + reflect + close")
    runtimes: set[int] = set()

    for cycle in range(max(2, opts.repeat)):
        started = time.perf_counter()
        manager = DatabaseManager()
        if not manager.connect_duckdb(str(path)):
            raise RuntimeError("could not reconnect to the churn database")
        session = state.get_session(f"churn-{cycle}")
        runtime = state.bind_session(session, f"conn-{cycle}", manager)
        runtimes.add(id(runtime))
        manager.analyze_tables(manager.get_tables("main"), "main")
        state.unbind_session(session)
        state.cleanup_session(f"churn-{cycle}")
        measurement.samples.append(_ms(started))

    measurement.counters |= {
        "cycles": len(measurement.samples),
        "runtimes_created": len(runtimes),
        "runtimes_left_bound": len(getattr(state, "_runtimes", {})),
        "sessions_left": len(getattr(state, "_sessions", {})),
    }
    return measurement


BENCHMARKS: dict[str, Callable[[argparse.Namespace, Path], Measurement]] = {
    "join_lookup": bench_join_lookup,
    "embed_schema": bench_embed_schema,
    "index_schema": bench_index_schema,
    "save_state": bench_save_state,
    "ontology_to_obqc": bench_ontology,
    "validate_sql": bench_validate_sql,
    "reflect_schema": bench_reflect_schema,
    "execute_query": bench_execute_query,
    "shared_connection": bench_shared_connection,
    "connection_churn": bench_connection_churn,
}


# ----------------------------------------------------------------------------
# Environment and entry point
# ----------------------------------------------------------------------------


def _environment(opts: argparse.Namespace) -> dict[str, Any]:
    """What a number has to be read against to mean anything."""
    from importlib.metadata import PackageNotFoundError, version

    def package(name: str) -> str:
        try:
            return version(name)
        except PackageNotFoundError:
            return "absent"

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            cwd=Path(__file__).resolve().parent.parent,
        ).stdout.strip()
    except OSError:
        commit = "unknown"

    return {
        "commit": commit or "unknown",
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "backend": opts.backend,
        "tables": opts.tables,
        "columns": opts.columns,
        "repeat": opts.repeat,
        "packages": {
            name: package(name)
            for name in (
                "sqlglot",
                "networkx",
                "sqlalchemy",
                "chromadb",
                "duckdb",
                "rdflib",
                "fastmcp",
            )
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Run the selected benchmarks and print a table.

    Args:
        argv: Command-line arguments, or None to read ``sys.argv``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", help="benchmark name; repeatable")
    parser.add_argument("--list", action="store_true", help="list the benchmarks")
    parser.add_argument(
        "--backend",
        default=TFIDF,
        choices=[TFIDF, MINILM],
        help=f"embedding backend ({MINILM} downloads a model on first use)",
    )
    parser.add_argument("--tables", type=int, default=60)
    parser.add_argument("--columns", type=int, default=12)
    parser.add_argument("--repeat", type=int, default=8)
    parser.add_argument("--json", type=Path, help="also write the results here")
    opts = parser.parse_args(argv)

    if opts.list:
        for name, function in BENCHMARKS.items():
            print(f"{name:18} {(function.__doc__ or '').splitlines()[0]}")
        return 0

    chosen = opts.only or list(BENCHMARKS)
    unknown = [name for name in chosen if name not in BENCHMARKS]
    if unknown:
        parser.error(f"unknown benchmark(s): {', '.join(unknown)}")

    environment = _environment(opts)
    print(
        f"orionbelt runtime benchmarks | {environment['commit']} | "
        f"python {environment['python']} | {environment['platform']} | "
        f"backend {opts.backend}"
    )
    print(
        f"schemas: {opts.tables} tables x {opts.columns} columns, "
        f"{opts.repeat} repeats\n"
    )

    results = []
    with tempfile.TemporaryDirectory(prefix="orionbelt-bench-") as temporary:
        workdir = Path(temporary)
        for name in chosen:
            scratch = workdir / name
            scratch.mkdir(parents=True, exist_ok=True)
            tracemalloc.start()
            started = time.perf_counter()
            try:
                measurement = BENCHMARKS[name](opts, scratch)
            except Exception as error:  # a broken benchmark must not hide the rest
                tracemalloc.stop()
                print(f"{name:18} FAILED: {error}")
                results.append({"name": name, "error": str(error)})
                continue
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            measurement.counters["peak_python_mib"] = round(peak / 2**20, 1)
            measurement.counters["wall_s"] = round(time.perf_counter() - started, 1)
            summary = measurement.summary()
            results.append(summary)

            headline = (
                f"p50 {summary['p50']} {summary['unit']}"
                if "p50" in summary
                else "no samples"
            )
            extras = " ".join(
                f"{key}={value}"
                for key, value in measurement.counters.items()
                if key not in {"wall_s"}
            )
            print(f"{name:18} {headline:22} {extras}")

    if opts.json:
        opts.json.write_text(
            json.dumps({"environment": environment, "results": results}, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote {opts.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
