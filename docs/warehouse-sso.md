# Warehouse single sign-on

A named connection can run its queries **as the signed-in user** instead of
the server's own login. The user's token from `MCP_AUTH=jwt` is exchanged for
a token the warehouse issues for that user, and the connection opens with it.
The warehouse then applies the user's own grants, row-level security and
masking, and its query history names them.

The settings and error codes are those of OrionBelt Semantic Layer, so one
guide serves both.

| Database | How the user's token becomes a warehouse token | This release |
| --- | --- | --- |
| Databricks | OAuth token federation: an RFC 8693 exchange at the workspace's `/oidc/v1/token` | yes |
| Snowflake | External OAuth: the identity provider issues it (Entra ID on-behalf-of, or RFC 8693 at Okta or Keycloak) | not yet |
| BigQuery | Workforce identity federation at Google's security token service | not yet |

Other databases keep the service login.

## Settings

Only a connection named in `OBA_DATABASES` can log in as the user, and only
with its own `DB_<NAME>_` settings: unlike its other settings, these never
fall back to an unprefixed variable, since a stray `AUTH=user` must not
switch every database over.

```env
MCP_AUTH=jwt
MCP_AUTH_ISSUER=https://login.example.com/
MCP_AUTH_AUDIENCE=orionbelt-analytics

OBA_DATABASES=finance
DB_FINANCE_TYPE=databricks
DB_FINANCE_AUTH=user
DB_FINANCE_DATABRICKS_SERVER_HOSTNAME=adb-1234567890.12.azuredatabricks.net
DB_FINANCE_DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123
DB_FINANCE_DATABRICKS_CATALOG=finance
DB_FINANCE_DATABRICKS_SCHEMA=gold
```

| Setting | Meaning |
| --- | --- |
| `DB_<NAME>_AUTH` | `service` (default) or `user` |
| `DB_<NAME>_OAUTH_CLIENT_ID` | Databricks: only for a service-principal federation policy |

The server does not start when a connection with `AUTH=user`:

- runs without `MCP_AUTH=jwt` (there is no user to log in as);
- is a database that cannot log in as the user in this release;
- lacks `DATABRICKS_SERVER_HOSTNAME` or `DATABRICKS_HTTP_PATH`;
- has an `AUTH` value that is neither `service` nor `user`.

A `DATABRICKS_ACCESS_TOKEN` it would otherwise read is ignored, and the log
says so.

## Databricks

On the Databricks side, create an account-level **federation policy** that
trusts your identity provider:

- its issuer is `MCP_AUTH_ISSUER`, and its audiences include
  `MCP_AUTH_AUDIENCE`, the audience of the tokens your MCP clients send;
- its subject claim names the Databricks user (usually `email`), and each
  user who signs in exists in the account under that name.

A policy for a service principal instead needs that principal's application
ID as `DB_<NAME>_OAUTH_CLIENT_ID`.

The server asks for the `all-apis` scope. The token sits in no connection
string: it is set as each connection opens, and Databricks connections are not
pooled, so every query reads the current one.

## Who the connection is

A connection that logs in as the user is that user's own. Its identity is the
user's issuer and subject (`iss|sub`) together with how it signs in -- the
service principal, exchange, scope or workforce provider -- never the
warehouse token (which changes every hour) and never a name the warehouse
reports. Two connections to one warehouse that sign in differently are two
connections, even for the same user. With it, each user gets their own:

- connection runtime and database manager;
- schema cache, GraphRAG index and Chroma collection;
- workspace on disk: ontology versions, semantic models, RDF.

This is deliberate. Under per-user grants the schema itself differs between
users, so a shared cache or index would show one user tables only another may
see. A workspace from before the source logged in as the user (a shared,
service-login one) is never taken over.

`list_databases` marks such a database with
`"login": "as the signed-in user"`. `connect_database(db_type=...)` on a
type with named connections connects to that connection (or asks which, if
there are several): the type's unprefixed variables are only their shared
defaults, and never a service login around a named one's `AUTH=user`.

## Tokens over time

Warehouse tokens last about an hour; MCP clients refresh the user's token on
their own schedule.

- `connect_database` exchanges the caller's token before connecting, so a
  user who may not use the database is told before anything connects.
- Every later tool call that may use the connection renews the warehouse
  token first, when it expires within two minutes, from that call's token;
  concurrent calls exchange once. `connect_database` and `list_databases` do
  not use it, so a failed renewal never keeps a user from switching to
  another database.
- The driver reads the current token as a connection opens, in a worker
  thread; it never exchanges.
- Work a tool starts in the background uses the token renewed for that call.
  If it runs past that token's end, it fails, and the next call renews.
- A request whose own token has expired (an idle user, a client that did
  not refresh) is rejected with HTTP 401 before any tool runs; the client
  signs in again, and the next call renews from the new token.

Tokens are kept in memory only, and never logged or put in an error message;
an error text from the token service is passed on with whatever this server
sent it -- the user's token, our client's secret -- blanked out.

## Errors

`connect_database` answers a failed sign-in with an error of type
`data_source_sign_in`, whose `code` says what to do and whose `suggestions`
say it in words. Any other tool, when renewing fails, fails with a tool error
that starts with the code (`[SIGN_IN_REQUIRED] ...`).

| Code | Meaning |
| --- | --- |
| `SIGN_IN_REQUIRED` | No signed-in user: stdio, `MCP_AUTH=none`, a client without a token, or a sign-in that has expired |
| `DATA_SOURCE_SIGN_IN_REFUSED` | The warehouse's token service refused this user; the response carries its reason |
| `DATA_SOURCE_SIGN_IN_INTERACTION_REQUIRED` | The identity provider wants the user at its sign-in page first (MFA, conditional access, consent); `claims` carries Entra's challenge |
| `DATA_SOURCE_SIGN_IN_UNAVAILABLE` | The token service could not be reached, or answered nonsense |

## What it costs

Each user runs `discover_schema` and builds GraphRAG for each database they
use, once, and again after their runtime is evicted and restored from their
own workspace. Measured with `scripts/bench_runtime.py` on 300 tables of 12
columns (TF-IDF embeddings, a local DuckDB file, an Apple-silicon laptop):

| Step | Time per user |
| --- | --- |
| Reflecting the schema | 0.4 s, 7 round trips (a warehouse adds its latency to each) |
| Indexing GraphRAG | 2.8 s |
| Generating an ontology | 5.6 s, when they generate one |

Under the service login, a second session on the same database pays none of
this: it shares the first one's runtime.

Disk: one workspace and one Chroma directory per user and database.
`cleanup_workspace` and `cleanup_old_versions` work on the caller's own.
Memory: one runtime per active user; idle sessions are evicted as before.
