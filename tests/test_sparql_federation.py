"""SPARQL SERVICE is refused, so a query cannot make the server fetch a URL.

Oxigraph executes SERVICE by requesting the endpoint the query names, from the
server's network. Queries come from a model or a user, which made query_sparql
a way to have the server reach any host -- internal ones included -- while it
advertised itself as closed-world. pyoxigraph has no switch for this, so the
store refuses the keyword itself.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from src.oxigraph_store import FederatedQueryError, reject_federation

pytest.importorskip("pyoxigraph")


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * WHERE { SERVICE <http://10.0.0.1/sparql> { ?s ?p ?o } }",
        "select * where { service <http://x/> { ?s ?p ?o } }",
        "SELECT * WHERE { SERVICE SILENT <http://x/> { ?s ?p ?o } }",
        "SELECT * WHERE { SERVICE<http://x/>{ ?s ?p ?o } }",
        "SELECT * WHERE { SERVICE ?endpoint { ?s ?p ?o } }",
        # Codepoint escapes are decoded before SPARQL is parsed.
        "SELECT * WHERE { \\u0053ERVICE <http://x/> { ?s ?p ?o } }",
        "SELECT * WHERE { \\U00000053ERVICE <http://x/> { ?s ?p ?o } }",
        "ASK { SERVICE <http://x/> { ?s ?p ?o } }",
        "CONSTRUCT { ?s ?p ?o } WHERE { SERVICE <http://x/> { ?s ?p ?o } }",
    ],
)
def test_every_spelling_of_service_is_refused(query):
    with pytest.raises(FederatedQueryError):
        reject_federation(query)


@pytest.mark.parametrize(
    "query",
    [
        'SELECT * WHERE { ?s ?p "customer service" }',
        "SELECT * WHERE { ?s <http://example.com/SERVICE> ?o }",
        "SELECT ?service WHERE { ?service ?p ?o }",
        "PREFIX ex: <http://e/> SELECT * WHERE { ?s ex:service ?o }",
        "SELECT * WHERE { ?s ?p ?o } # no SERVICE here",
        'SELECT * WHERE { ?s ?p """a SERVICE in a long literal""" }',
    ],
)
def test_the_word_elsewhere_is_allowed(query):
    reject_federation(query)


class _Endpoint(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:
        self.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/sparql-results+json")
        self.end_headers()
        self.wfile.write(b'{"head":{"vars":[]},"results":{"bindings":[{}]}}')

    do_POST = do_GET

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def endpoint():
    _Endpoint.hits = []
    server = HTTPServer(("127.0.0.1", 0), _Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/sparql"
    server.shutdown()


@pytest.mark.parametrize("form", ["select", "ask", "construct"])
def test_the_store_never_contacts_the_endpoint(endpoint, form):
    from src.oxigraph_store import OxigraphStoreManager

    store = OxigraphStoreManager()
    pattern = f"SERVICE <{endpoint}> {{ ?s ?p ?o }}"
    run = {
        "select": lambda: store.query_sparql(f"SELECT * WHERE {{ {pattern} }}"),
        "ask": lambda: store.query_sparql_ask(f"ASK {{ {pattern} }}"),
        "construct": lambda: store.query_sparql_construct(
            f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ {pattern} }}"
        ),
    }[form]

    with pytest.raises(FederatedQueryError):
        run()
    assert _Endpoint.hits == []


# Queries that hide a SERVICE clause from a lexer that disagrees with the
# parser. {url} is the endpoint; each must be refused before Oxigraph sees it.
EVASIONS = [
    # A comment ends at CR as well as LF.
    "SELECT * WHERE { ?s ?p ?o # note\rSERVICE <{url}> { ?a ?b ?c } }",
    "SELECT * WHERE { ?s ?p ?o # note\r\nSERVICE <{url}> { ?a ?b ?c } }",
    # An escaped quote does not close a long string.
    "SELECT * WHERE { BIND('''x\\''' # decoy''' AS ?x) SERVICE <{url}> { ?a ?b ?c } }",
    'SELECT * WHERE { BIND("""x\\""" # decoy""" AS ?x) SERVICE <{url}> { ?a ?b ?c } }',
    # A letter outside ASCII right before the keyword.
    "SELECT * WHERE { ?s ?p ?o .éSERVICE <{url}> { ?a ?b ?c } }",
    # Escapes, either way round.
    "SELECT * WHERE { \\u0053ERVICE <{url}> { ?a ?b ?c } }",
    "SELECT * WHERE { BIND('\\u0027' AS ?x) SERVICE <{url}> { ?a ?b ?c } }",
    # An unterminated literal does not swallow what follows.
    "SELECT * WHERE { SERVICE <{url}> { ?a ?b ?c } } # '''",
    "SELECT * WHERE { BIND('x\nSERVICE <{url}> { ?a ?b ?c } }",
]


@pytest.mark.parametrize("template", EVASIONS)
def test_an_evasion_is_refused(template):
    with pytest.raises(FederatedQueryError):
        reject_federation(template.replace("{url}", "http://127.0.0.1:9/sparql"))


@pytest.mark.parametrize("template", EVASIONS)
def test_an_evasion_never_reaches_the_endpoint(endpoint, template):
    from src.oxigraph_store import OxigraphStoreManager

    store = OxigraphStoreManager()
    query = template.replace("{url}", endpoint)

    with pytest.raises(FederatedQueryError):
        store.query_sparql(query)
    assert _Endpoint.hits == []


@pytest.mark.parametrize(
    "query",
    [
        'SELECT * WHERE { ?s ?p "customer service" }',
        "SELECT * WHERE { ?s <http://example.com/SERVICE> ?o }",
        "SELECT ?service WHERE { ?service ?p ?o }",
        "PREFIX ex: <http://e/> SELECT * WHERE { ?s ex:service ?o }",
        "SELECT * WHERE { ?s ?p ?o } # SERVICE <{url}> {}",
        "SELECT * WHERE { ?s ?p '''SERVICE <{url}> { ?a ?b ?c }''' }",
    ],
)
def test_what_is_allowed_really_does_not_federate(endpoint, query):
    # Run unguarded: if Oxigraph would have contacted the endpoint, allowing
    # the query would have been wrong.
    import pyoxigraph

    query = query.replace("{url}", endpoint)
    reject_federation(query)
    list(pyoxigraph.Store().query(query))
    assert _Endpoint.hits == []


# Local-name escapes: "\#" and "\'" are part of a prefixed name, not the start
# of a comment or a string. Each is a pattern placed in all three query forms.
ESCAPED_NAME_PATTERNS = [
    "BIND(ex:\\# AS ?x) SERVICE <{url}> { ?a ?b ?c }",
    "BIND(ex:\\' AS ?x) SERVICE <{url}> { ?a ?b ?c } BIND(ex:\\' AS ?y)",
    "BIND(ex:a\\#b AS ?x) SERVICE <{url}> { ?a ?b ?c }",
    "BIND(:\\# AS ?x) SERVICE <{url}> { ?a ?b ?c }",
    "BIND(ex:\\#\\' AS ?x) SERVICE <{url}> { ?a ?b ?c }",
]
_PREFIXES = "PREFIX ex: <http://example.org/> PREFIX : <http://example.org/d/> "


@pytest.mark.parametrize("form", ["select", "ask", "construct"])
@pytest.mark.parametrize("pattern", ESCAPED_NAME_PATTERNS)
def test_an_escaped_local_name_does_not_hide_service(endpoint, form, pattern):
    from src.oxigraph_store import OxigraphStoreManager

    store = OxigraphStoreManager()
    body = pattern.replace("{url}", endpoint)
    run = {
        "select": lambda: store.query_sparql(f"{_PREFIXES}SELECT * WHERE {{ {body} }}"),
        "ask": lambda: store.query_sparql_ask(f"{_PREFIXES}ASK {{ {body} }}"),
        "construct": lambda: store.query_sparql_construct(
            f"{_PREFIXES}CONSTRUCT {{ ?a ?b ?c }} WHERE {{ {body} }}"
        ),
    }[form]

    with pytest.raises(FederatedQueryError):
        run()
    assert _Endpoint.hits == []


def test_an_escaped_local_name_alone_is_allowed(endpoint):
    import pyoxigraph

    query = f"{_PREFIXES}SELECT * WHERE {{ BIND(ex:\\# AS ?x) BIND(ex:a\\'b AS ?y) }}"
    reject_federation(query)
    list(pyoxigraph.Store().query(query))
    assert _Endpoint.hits == []


# Oxigraph needs no boundary between SERVICE and the tokens around it.
ADJACENT_PATTERNS = [
    # Trailing adjacency.
    "SERVICESILENT <{url}> { ?a ?b ?c }",
    "SERVICEex:sparql { ?a ?b ?c }",
    "SERVICE<{url}>{ ?a ?b ?c }",
    # Leading adjacency, after a triple's object.
    "?a ?b 1SERVICE <{url}> { ?a ?b ?c }",
    "?a ?b trueSERVICE <{url}> { ?a ?b ?c }",
    "?a ?b 1.5SERVICE <{url}> { ?a ?b ?c }",
    "?a ?b 'x'SERVICE <{url}> { ?a ?b ?c }",
    # Both, and mixed case.
    "?a ?b falsesErViCeSILENT <{url}> { ?a ?b ?c }",
]


def _prefixes_for(endpoint: str) -> str:
    # ex:sparql must name the endpoint for the trailing prefixed-name case.
    base = endpoint[: -len("sparql")]
    return f"PREFIX ex: <{base}> PREFIX : <http://example.org/d/> "


@pytest.mark.parametrize("form", ["select", "ask", "construct"])
@pytest.mark.parametrize("pattern", ADJACENT_PATTERNS)
def test_adjacent_tokens_do_not_hide_service(endpoint, form, pattern):
    from src.oxigraph_store import OxigraphStoreManager

    store = OxigraphStoreManager()
    prefixes = _prefixes_for(endpoint)
    body = pattern.replace("{url}", endpoint)
    run = {
        "select": lambda: store.query_sparql(f"{prefixes}SELECT * WHERE {{ {body} }}"),
        "ask": lambda: store.query_sparql_ask(f"{prefixes}ASK {{ {body} }}"),
        "construct": lambda: store.query_sparql_construct(
            f"{prefixes}CONSTRUCT {{ ?a ?b ?c }} WHERE {{ {body} }}"
        ),
    }[form]

    with pytest.raises(FederatedQueryError):
        run()
    assert _Endpoint.hits == []


def test_names_that_contain_the_word_are_allowed():
    reject_federation(
        "PREFIX ex: <http://e/> "
        "SELECT ?xSERVICE WHERE { ?xSERVICE ex:aSERVICE ex:serviceType }"
    )


def test_a_prefix_named_like_the_keyword_is_refused_by_design():
    # The prefix stays visible to the scan; renaming it is the remedy.
    with pytest.raises(FederatedQueryError):
        reject_federation(
            "PREFIX myservice: <http://e/> SELECT * WHERE { ?s myservice:p ?o }"
        )


class TestTheGrammarGateAlone:
    """The rdflib parse is a second gate: it holds with the scanner switched off."""

    @pytest.fixture(autouse=True)
    def _scanner_off(self, monkeypatch):
        import src.oxigraph_store as store_module

        monkeypatch.setattr(store_module, "_sparql_code", lambda _text: "")

    @pytest.mark.parametrize(
        "template",
        EVASIONS
        + [_PREFIXES + "SELECT * WHERE { " + p + " }" for p in ESCAPED_NAME_PATTERNS]
        + ["SELECT * WHERE { " + p + " }" for p in ADJACENT_PATTERNS],
    )
    def test_every_known_bypass_is_refused(self, template):
        query = template.replace("{url}", "http://127.0.0.1:9/sparql")
        query = query.replace("ex:sparql", "<http://127.0.0.1:9/sparql>")

        with pytest.raises(FederatedQueryError):
            reject_federation(query)

    def test_a_query_the_grammar_cannot_read_is_refused(self):
        with pytest.raises(FederatedQueryError, match="not standard SPARQL"):
            reject_federation("SELECT * WHERE { ?s ?p ?o ")

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * WHERE { ?s ?p ?o } LIMIT 10",
            "ASK { ?s a <http://www.w3.org/2002/07/owl#Class> }",
            "CONSTRUCT { ?s ?p ?o } WHERE { GRAPH ?g { ?s ?p ?o } }",
            "PREFIX oba: <https://ralforion.com/ns/oba#> "
            "SELECT ?t WHERE { ?c oba:tableName ?t FILTER(CONTAINS(?t, 'service')) }",
        ],
    )
    def test_ordinary_queries_pass(self, query):
        reject_federation(query)


class TestTheCheckRunsWithinTheDeadline:
    """Parsing a large query takes seconds; it must neither outrun the timeout
    nor hold the event loop while it runs."""

    def _slow_check(self, monkeypatch: Any, seconds: float) -> None:
        import time

        import src.oxigraph_store as store_module

        real = store_module._grammar_finds_service

        def slow(query: str) -> bool:
            time.sleep(seconds)
            return real(query)

        monkeypatch.setattr(store_module, "_grammar_finds_service", slow)

    def test_the_timeout_covers_the_federation_check(self, monkeypatch):
        import time

        from src.oxigraph_store import OxigraphStoreManager

        self._slow_check(monkeypatch, 2.0)
        store = OxigraphStoreManager()

        started = time.perf_counter()
        with pytest.raises(TimeoutError):
            store.query_sparql("SELECT * WHERE { ?s ?p ?o }", timeout_seconds=0.3)

        assert time.perf_counter() - started < 1.0

    @pytest.mark.parametrize("form", ["SELECT", "ASK"])
    async def test_the_event_loop_keeps_serving_while_a_query_is_checked(
        self, monkeypatch, form
    ):
        import asyncio
        import time
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock

        import src.handlers.rdf as rdf
        from src.oxigraph_store import OxigraphStoreManager

        self._slow_check(monkeypatch, 0.6)
        monkeypatch.setattr(rdf, "notify_client", AsyncMock())
        store = OxigraphStoreManager()
        services = SimpleNamespace(get_oxigraph_store=lambda _ctx: store)
        query = (
            "SELECT * WHERE { ?s ?p ?o }" if form == "SELECT" else "ASK { ?s ?p ?o }"
        )

        gaps: list[float] = []

        async def heartbeat() -> None:
            last = time.perf_counter()
            for _ in range(20):
                await asyncio.sleep(0.05)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.01)  # let the heartbeat start ticking first
        result = await rdf.query_sparql(Mock(), query, 5, services)
        await beat

        assert result["success"] is True, result
        # A held loop shows as one gap as long as the check (0.6s).
        assert max(gaps) < 0.3
