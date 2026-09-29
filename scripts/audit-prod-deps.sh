#!/usr/bin/env bash
#
# Audit the dependency tree that actually ships for known advisories.
#
# Dependabot alerts scan the whole of uv.lock, dev group included, so its list
# is always wider than what a user installing the wheel is exposed to. This
# script is the narrower, blocking check: the production tree only.
#
# Why the flags are what they are:
#   --no-dev            the dev group never reaches a user
#   --no-emit-project   the project itself is not a third-party advisory target
#   --no-hashes         pip-audit cannot parse uv's --hash lines
#   --no-deps           the export is already fully resolved; re-resolving it
#                       would need network installs and defeat --disable-pip
#   --disable-pip       audit the locked versions, never whatever pip resolves
#
set -euo pipefail

# Advisories accepted as not reachable from this codebase. Each entry must
# mirror a Dependabot dismissal and say why, so the list stays auditable rather
# than becoming a place vulnerabilities go to be forgotten.
#
# chromadb 1.5.9 -- four unpatched CVEs in the server HTTP API. Not reachable:
# src/graphrag/vector_store_chromadb.py uses the embedded PersistentClient and
# never starts or calls the server. Dismissed as not_used on 2026-08-31
# (alerts #17, #34, #35, #36). Drop these the moment chromadb ships a fix.
#
# oauthlib 3.3.1 -- two advisories, both in authorization-server code:
#   CVE-2026-49265 (GHSA-xpv3-w29h-x7cv), a timing leak in PKCE code_verifier
#     comparison (oauth2/rfc6749/grant_types/authorization_code.py);
#   CVE-2026-49264 (GHSA-hj66-6f7g-4r5v), JSONP callback injection in
#     RevocationEndpoint when built with enable_jsonp=True
#     (oauth2/rfc6749/endpoints/revocation.py).
# Not reachable: oauthlib arrives only as an OAuth *client* dependency, of
# databricks-sql-connector and of kubernetes via chromadb. Nothing here runs an
# authorization server; no installed package imports oauthlib's server-side
# grant or endpoint code (authlib, which FastMCP uses, has its own and does not
# import oauthlib). Unfixable for now: the fix for both is oauthlib 4.0.0, and
# databricks-sql-connector 4.6.0, the latest, pins oauthlib<4.0.0. Dismissed as
# not_used on 2026-09-29 (alerts #38, #39). Drop these when
# databricks-sql-connector allows oauthlib 4.x.
#   Re-check both before adding OAuth to this server. They stay unreachable as
#   long as the server side is FastMCP's own auth, which uses joserfc and
#   cryptography, not oauthlib. A framework built on oauthlib's server side
#   (Flask-OAuthlib, django-oauth-toolkit, oauthlib's WebApplicationServer)
#   would make both reachable -- then oauthlib 4 is required, not an ignore.
IGNORED=(
  PYSEC-2026-311   # CVE-2026-45829
  CVE-2026-45830
  CVE-2026-45831
  CVE-2026-45833
  CVE-2026-49265   # oauthlib, alert #39
  CVE-2026-49264   # oauthlib, alert #38 -- listed before pip-audit knows it
)

REQUIREMENTS="$(mktemp -t prod-requirements.XXXXXX)"
trap 'rm -f "$REQUIREMENTS"' EXIT

uv export --no-dev --no-emit-project --no-hashes \
  --format requirements-txt -o "$REQUIREMENTS" --quiet

ignore_args=()
for vuln in "${IGNORED[@]}"; do
  ignore_args+=(--ignore-vuln "$vuln")
done

echo "Auditing $(grep -c '==' "$REQUIREMENTS") production packages" \
     "(${#IGNORED[@]} advisories ignored, see script header)."

exec uv tool run pip-audit \
  --disable-pip --no-deps -r "$REQUIREMENTS" "${ignore_args[@]}"
