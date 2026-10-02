"""SPARQL SERVICE is refused, so a query cannot make the server fetch a URL.

Oxigraph executes SERVICE by requesting the endpoint the query names, from the
server's network. Queries come from a model or a user, which made query_sparql
a way to have the server reach any host -- internal ones included -- while it
advertised itself as closed-world. pyoxigraph has no switch for this, so the
store refuses the keyword itself.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

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
