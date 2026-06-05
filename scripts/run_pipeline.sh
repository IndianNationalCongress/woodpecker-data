#!/usr/bin/env bash
#
# G1 — the whole pipeline, end-to-end, on fixtures, with zero network.
#
#   gen fixtures (PDFs/zips) -> per-source scrapers (OCDS releases + OCR -> serve/)
#   -> compile (records + monthly indices + status + manifest)
#
# Idempotent: the ledger is append-only, so re-running adds nothing new and just
# recompiles. Safe to run as many times as you like.
#
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# Prefer the pinned venv (deterministic fixtures: reportlab invariant + pinned
# reportlab/pillow). Falls back to bare python3. Override with PYTHON=/path/to/python.
PY="${PYTHON:-python3}"
[ -x "$ROOT/.venv/bin/python3" ] && PY="$ROOT/.venv/bin/python3"

echo "== Woodpecker pipeline (fixtures) ==  [python: $PY]"

echo "-- 1/3  generating binary fixtures"
"$PY" scripts/gen_fixtures.py

echo "-- 2/3  running source-module scrapers (independent)"
for src in cppp rajasthan gujarat; do
  # Independence Principle: one source failing must not stop the others.
  if "$PY" "data/$src/scraper/scrape.py"; then
    :
  else
    echo "   !! $src scraper exited non-zero (continuing — other sources unaffected)"
  fi
done

echo "-- 3/3  compiling serving layer"
"$PY" data/compile/compile.py

echo "== done =="
echo "Ledger:   data/<source>/{releases,observations}/"
echo "Serving:  serve/    (R2 stand-in; serve with: $PY scripts/serve.py)"
