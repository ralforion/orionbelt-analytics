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
# oauthlib 3.3.1 -- CVE-2026-49265 (GHSA-xpv3-w29h-x7cv), a timing leak in PKCE
# code_verifier comparison on the authorization-server side
# (oauth2/rfc6749/grant_types/authorization_code.py). Not reachable: oauthlib
# arrives only as an OAuth *client* dependency, of databricks-sql-connector and
# of kubernetes via chromadb; nothing here runs an authorization server, and no
# installed package imports its server-side grant code. Unfixable for now:
# databricks-sql-connector 4.6.0, the latest, pins oauthlib<4.0.0, and 4.0.0 is
# the fix. Added 2026-09-29, the day the advisory appeared and before Dependabot
# raised an alert for it -- dismiss that alert as not_used when it arrives, and
# record its number here. Drop this when databricks-sql-connector allows 4.x.
IGNORED=(
  PYSEC-2026-311   # CVE-2026-45829
  CVE-2026-45830
  CVE-2026-45831
  CVE-2026-45833
  CVE-2026-49265   # oauthlib, see above
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
