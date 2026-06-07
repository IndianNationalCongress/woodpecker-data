#!/usr/bin/env bash
#
# Rebuild the served layer (docs/) for woodpecker-data, end-to-end:
#
#   1. bulk archives  (_import/*/import.py)              -> docs/<archive>/index
#   2. compile        (live sources + FOLD archives + RE-KEY everything by procuring
#                      entity, provenance per record)    -> docs/<entity>/ + sources.json + stats.json
#   3. semantic index (embed the whole entity corpus)    -> docs/search/
#
# Per-source GePNIC scrapers run separately (GitHub Actions cron) and commit releases/;
# this script rebuilds the served layer from the committed ledger + the bulk archives.
# Idempotent: re-running just recompiles.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
# the search step needs an embedding model (fastembed); point at a python env that has it
SEARCH_PY="${SEARCH_PYTHON:-$PY}"

echo "-- 1/3  importing bulk archives (declared OCDS exports: Assam / Himachal / CPPP)"
for imp in _import/*/import.py; do
  [ -f "$imp" ] || continue
  echo "   $imp"; "$PY" "$imp"
done

echo "-- 2/3  compiling docs/  (live sources + fold archives + re-key by procuring entity)"
"$PY" compile/compile.py --serve docs

echo "-- 3/3  building the semantic index over the entity corpus"
if "$SEARCH_PY" -c "import fastembed" 2>/dev/null; then
  "$SEARCH_PY" _search/build_index.py
else
  echo "   !! skipped: '$SEARCH_PY' has no fastembed — set SEARCH_PYTHON to a venv that does"
fi

echo "== done -> docs/ =="
