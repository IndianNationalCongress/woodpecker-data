#!/usr/bin/env python3
"""
Forward-pass: sync live OCDS records (the daily gepnic-live cron output) into the D1 serving
layer, so the new backend stays fresh instead of being a static snapshot.

Maps each compiled OCDS record -> the D1 `tenders` row, keyed by wp-<tender.id> (the SAME key
the bulk AOC/VPS dumps used), so a live scrape ENRICHES/UPDATES the existing row rather than
creating a duplicate. Upserts via INSERT OR REPLACE in batched SQL through `wrangler d1
execute --file`. (Embedding the new tenders into Vectorize is a follow-on step — reuse
embed_vectorize.py over the day's new ocids.)

Usage:
  python3 sync_d1.py path/to/record.json [more.json ...]
  python3 sync_d1.py --since <git-ref>     # records changed since ref (run in woodpecker-data)
  python3 sync_d1.py --all                 # every docs/*/records/*.json (full re-sync)

CI: set CLOUDFLARE_API_TOKEN (D1 edit) + CLOUDFLARE_ACCOUNT_ID; wrangler picks them up.
"""
import argparse, glob, json, os, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(__file__))
from build_canonical import (clean_text, clean_tid, parse_dt, parse_amt, parse_int,
                             cat_of, supplier_of, COLS, _SET)  # reuse mappers + COALESCE upsert
from export_d1 import sqlstr
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "compile"))
from entities import entity_of

# where wrangler.toml lives (data repo root by default; works in the cron + locally).
# CI auth: CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID env.
API_DIR = os.environ.get("WP_WRANGLER_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

def map_record(rec):
    """compiled OCDS record -> a dict matching the D1 tenders columns (COLS)."""
    cr = rec.get("compiledRelease", {})
    t = cr.get("tender", {}) or {}
    tid = clean_tid(t.get("id") or "")
    if not tid:
        return None  # forward-pass only handles well-formed ids (dirty ones came via dumps)
    ocid = "wp-" + tid
    buyer = (cr.get("buyer", {}) or {}).get("name") or (t.get("procuringEntity", {}) or {}).get("name")
    e = entity_of(buyer)
    awards = cr.get("awards") or []
    win_name = win_id = award_date = None
    val = (t.get("value") or {}).get("amount")
    if awards:
        a = awards[0]
        sup = (a.get("suppliers") or [{}])[0].get("name")
        win_id, win_name = supplier_of(sup)
        val = (a.get("value") or {}).get("amount") or val
        award_date = parse_dt(a.get("date"))
    has_award = 1 if awards else 0
    tp = t.get("tenderPeriod") or {}
    row = {c: None for c in COLS}
    row.update({
        "ocid": ocid, "tender_id": tid, "org_name": clean_text(buyer),
        "title": clean_text(t.get("title")), "description": clean_text(t.get("description")),
        "category": cat_of(t.get("mainProcurementCategory")),
        "status": "awarded" if has_award else (t.get("status") or "active"),
        "year": int(tid[:4]) if tid[:4].isdigit() else None,
        "published_date": parse_dt(cr.get("date")), "closing_date": parse_dt(tp.get("endDate")),
        "award_date": award_date, "value_amount": parse_amt(val) if val else None,
        "n_bids": parse_int((t.get("numberOfTenderers"))), "winner_id": win_id, "winner_name": win_name,
        "has_detail": 1, "has_award": has_award, "source": "live", "provenance": "observed",
        "doc_url": None,
    })
    row["entity_id"], row["central"], row["state"] = e["id"], (1 if e["central"] else 0), e["state"]
    return row

def files_arg(a):
    if a.all:
        return glob.glob("docs/*/records/*.json")
    if a.since:
        out = subprocess.run(["git", "diff", "--name-only", a.since, "--", "docs"],
                             capture_output=True, text=True).stdout.split()
        return [f for f in out if "/records/" in f and f.endswith(".json") and os.path.exists(f)]
    return a.files

# Row-count ceiling for the forward-pass. The daily cron runs `--since HEAD~1` (a handful of
# rows); a bad diff (rebase/mass touch) or an accidental `--all` (~228k rows, each amplified by
# the tenders indexes) could balloon into a large, unintended D1 write. Abort above this unless
# --force. Sized well above a normal day but far below a full-corpus reload.
DEFAULT_MAX_ROWS = 50_000

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--since"); ap.add_argument("--all", action="store_true")
    ap.add_argument("--db", default="woodpecker"); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS,
                    help="abort if the forward-pass would upsert more than this many rows (0=unlimited)")
    ap.add_argument("--force", action="store_true", help="bypass the --max-rows ceiling")
    a = ap.parse_args()
    if a.all and not a.force:
        print("REFUSING: --all re-syncs the entire corpus (unbounded). Re-run with --force if intended,"
              "\nor use --since <ref> for an incremental forward-pass.", file=sys.stderr)
        raise SystemExit(2)
    files = files_arg(a)
    if not files:
        print("no records to sync"); return
    rows = []
    for f in files:
        try:
            r = map_record(json.load(open(f)))
            if r: rows.append(r)
        except Exception as e:
            print(f"skip {f}: {e}", file=sys.stderr)
    print(f"{len(rows)} live tenders -> D1 upsert")
    if a.max_rows and len(rows) > a.max_rows and not a.force:
        print(f"ABORT: {len(rows):,} rows exceeds --max-rows={a.max_rows:,} (guards a runaway forward-pass)."
              "\nRe-run with --force or a tighter --since if this volume is genuinely intended.",
              file=sys.stderr)
        raise SystemExit(2)
    if not rows or a.dry_run:
        if a.dry_run and rows: print("sample:", {k: rows[0][k] for k in ("ocid","org_name","entity_id","value_amount","has_award")})
        return
    # batched ON CONFLICT DO UPDATE with per-column COALESCE (live fills gaps / updates status;
    # never clobbers award value/EMD/winner that came from the richer AOC/VPS dumps).
    # <=40 rows/stmt for D1's 100KB statement cap.
    collist = ",".join(COLS)
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as fh:
        for i in range(0, len(rows), 40):
            vals = ",".join("(" + ",".join(sqlstr(r[c]) for c in COLS) + ")" for r in rows[i:i+40])
            fh.write(f"INSERT INTO tenders ({collist}) VALUES {vals} ON CONFLICT(ocid) DO UPDATE SET {_SET};\n")
        path = fh.name
    subprocess.run(["npx", "wrangler", "d1", "execute", a.db, "--remote", "--file", path, "-y"],
                   cwd=API_DIR, check=True)
    os.remove(path)
    print("synced. (next: embed new ocids into Vectorize via embed_vectorize.py)")

if __name__ == "__main__":
    main()
