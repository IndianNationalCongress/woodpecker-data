#!/usr/bin/env python3
"""
Export the canonical sqlite into chunked SQL for `wrangler d1 import --remote`.

D1 quirks this works around:
  * import file practical size -> chunk DATA into ~MAXMB files.
  * 30s/statement -> FTS is rebuilt server-side in rowid-range chunks, not one big INSERT.
  * FTS5 shadow tables don't import cleanly -> we ship base tables only + a rebuild step.

Emits into OUTDIR:
  00_schema.sql      base table DDL (no indexes yet -> faster bulk insert)
  01_data_NNN.sql    chunked multi-row INSERTs (tenders, awards, entities, suppliers)
  98_index.sql       CREATE INDEX (after data)
  99_fts_NNN.sql     CREATE fts + chunked INSERT..SELECT populate
  load.sh            runs them all via wrangler in order

Usage: python3 export_d1.py --db ~/wp_d1_build/tenders.sqlite --out ~/wp_d1_build/d1sql [--limit N]
"""
import argparse, os, sqlite3

BASE_TABLES = {
    "tenders": ["ocid","tender_id","entity_id","org_name","title","description","portal_type",
                "state","central","category","status","year","published_date","closing_date",
                "award_date","value_amount","emd","tender_fee","n_bids","winner_id","winner_name",
                "winner_address","has_detail","has_award","provenance","source","doc_url"],
    "awards": ["ocid","supplier_id","value_amount","award_date"],
    "entities": ["entity_id","label","central","state","n_tenders","total_value"],
    "suppliers": ["supplier_id","name","address","n_awards","total_won"],
}
SCHEMA = """CREATE TABLE tenders (
  ocid TEXT PRIMARY KEY, tender_id TEXT, entity_id TEXT, org_name TEXT, title TEXT,
  description TEXT, portal_type TEXT, state TEXT, central INTEGER, category TEXT, status TEXT,
  year INTEGER, published_date INTEGER, closing_date INTEGER, award_date INTEGER,
  value_amount INTEGER, emd INTEGER, tender_fee INTEGER, n_bids INTEGER,
  winner_id TEXT, winner_name TEXT, winner_address TEXT,
  has_detail INTEGER DEFAULT 0, has_award INTEGER DEFAULT 0, provenance TEXT, source TEXT, doc_url TEXT);
CREATE TABLE awards (ocid TEXT, supplier_id TEXT, value_amount INTEGER, award_date INTEGER, PRIMARY KEY(ocid,supplier_id));
CREATE TABLE entities (entity_id TEXT PRIMARY KEY, label TEXT, central INTEGER, state TEXT, n_tenders INTEGER, total_value INTEGER);
CREATE TABLE suppliers (supplier_id TEXT PRIMARY KEY, name TEXT, address TEXT, n_awards INTEGER, total_won INTEGER);
"""
INDEXES = [
    "CREATE INDEX ix_entity ON tenders(entity_id,published_date)",
    "CREATE INDEX ix_value ON tenders(value_amount)",
    "CREATE INDEX ix_winner ON tenders(winner_id)",
    "CREATE INDEX ix_cat ON tenders(category,year)",
    "CREATE INDEX ix_year ON tenders(year)",
    "CREATE INDEX ix_state ON tenders(state)",
    "CREATE INDEX ix_awards_sup ON awards(supplier_id)",
]

def sqlstr(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"

# --- Write-volume guard -----------------------------------------------------
# D1 bills EVERY b-tree write as a "row written": the base row + one write per
# index (incl. the implicit PRIMARY-KEY index) + FTS5 shadow-table rows. A full
# reload of the 6.3M-row corpus into this 7-index + FTS5 schema amplifies ~14x
# to ~90M writes — which is exactly what caused the 2026-06-25 $52 bill. This
# guard estimates the physical writes a load will cost and refuses to emit
# load.sh above a ceiling unless you pass --force.
DEFAULT_MAX_WRITES = 20_000_000     # abort above this many projected row-writes
FTS_WRITE_FACTOR   = 4              # ~shadow-table rows written per FTS5 document

def _indexes_per_table():
    """count secondary indexes declared in INDEXES, per table."""
    counts = {t: 0 for t in BASE_TABLES}
    for stmt in INDEXES:
        # "CREATE INDEX <name> ON <table>(...)"
        tbl = stmt.split(" ON ", 1)[1].split("(", 1)[0].strip()
        if tbl in counts:
            counts[tbl] += 1
    return counts

def estimate_writes(db, limit=0):
    """(total_writes, per-table breakdown lines) for a full load of `db`."""
    sec = _indexes_per_table()
    lines, total = [], 0
    for tbl in BASE_TABLES:
        n = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        if limit and tbl == "tenders":
            n = min(n, limit)
        mult = 1 + 1 + sec[tbl]          # base row + implicit PK index + secondary indexes
        w = n * mult
        total += w
        lines.append(f"  {tbl:<10} {n:>12,} rows x {mult} (1 base +1 pk +{sec[tbl]} idx) = {w:>14,}")
    # FTS5 populate over every tenders row
    fts_docs = db.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
    if limit:
        fts_docs = min(fts_docs, limit)
    fts_w = fts_docs * FTS_WRITE_FACTOR
    total += fts_w
    lines.append(f"  {'tenders_fts':<10} {fts_docs:>12,} docs x ~{FTS_WRITE_FACTOR} (fts5 shadow)      = {fts_w:>14,}")
    return total, lines

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/wp_d1_build/tenders.sqlite"))
    ap.add_argument("--out", default=os.path.expanduser("~/wp_d1_build/d1sql"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--maxmb", type=int, default=45)         # per data chunk
    ap.add_argument("--rows-per-insert", type=int, default=40)  # D1 caps a statement at 100KB
    ap.add_argument("--fts-chunk", type=int, default=150000)  # rowid range per FTS populate stmt
    ap.add_argument("--max-writes", type=int, default=DEFAULT_MAX_WRITES,
                    help="abort if a load would write more than this many D1 rows (0=unlimited)")
    ap.add_argument("--force", action="store_true",
                    help="bypass the write-volume ceiling (use only for a deliberate full reload)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    db = sqlite3.connect(a.db)

    # --- pre-flight write-volume guard: estimate before emitting anything ---
    est, breakdown = estimate_writes(db, a.limit)
    print("projected D1 row-writes for this load:")
    print("\n".join(breakdown))
    print(f"  {'TOTAL':<10} {'':>12}                                = {est:>14,}")
    if a.max_writes and est > a.max_writes and not a.force:
        print(f"\nABORT: projected {est:,} writes exceeds --max-writes={a.max_writes:,}.")
        print("This is the guard against the 2026-06-25 90M-write incident. A full reload of the")
        print("whole corpus is genuinely this expensive here (7 indexes + FTS5 ~14x amplify).")
        print("If you really intend a full reload, re-run with --force. To load cheaply, prefer an")
        print("incremental diff (sync_d1.py --since <ref>) instead of regenerating the entire DB.")
        db.close()
        raise SystemExit(2)
    if a.force and a.max_writes and est > a.max_writes:
        print(f"--force set: proceeding despite {est:,} > {a.max_writes:,} projected writes.")

    with open(os.path.join(a.out, "00_schema.sql"), "w") as f:
        f.write(SCHEMA)

    chunk_i, files = 0, []
    fh = None
    size = 0
    def newchunk():
        nonlocal fh, chunk_i, size
        if fh:
            fh.close()
        name = f"01_data_{chunk_i:03d}.sql"
        files.append(name)
        fh = open(os.path.join(a.out, name), "w")
        chunk_i += 1
        size = 0
    newchunk()

    for tbl, cols in BASE_TABLES.items():
        collist = ",".join(cols)
        # lean D1: cap description in the row to ~400 chars (FTS still indexes this; full
        # work-description lives in the R2 blob). Keeps the D1 db + import dump small.
        selexpr = ",".join("substr(description,1,400)" if c == "description" else c for c in cols)
        sql = f"SELECT {selexpr} FROM {tbl}"
        if a.limit and tbl == "tenders":
            sql += f" LIMIT {a.limit}"
        cur = db.execute(sql)
        buf = []
        while True:
            rows = cur.fetchmany(a.rows_per_insert)
            if not rows:
                break
            values = ",".join("(" + ",".join(sqlstr(v) for v in r) + ")" for r in rows)
            stmt = f"INSERT INTO {tbl} ({collist}) VALUES {values};\n"
            fh.write(stmt)
            size += len(stmt)
            if size > a.maxmb * 1024 * 1024:
                newchunk()
    if fh:
        fh.close()

    with open(os.path.join(a.out, "98_index.sql"), "w") as f:
        f.write(";\n".join(INDEXES) + ";\n")

    # FTS: create + chunked populate by rowid range
    maxrow = db.execute("SELECT MAX(rowid) FROM tenders").fetchone()[0] or 0
    fts_files = []
    fi = 0
    with open(os.path.join(a.out, f"99_fts_{fi:03d}.sql"), "w") as f:
        f.write("CREATE VIRTUAL TABLE tenders_fts USING fts5(title,description,org_name,winner_name,content='');\n")
    fts_files.append(f"99_fts_{fi:03d}.sql")
    lo = 1
    while lo <= maxrow:
        hi = lo + a.fts_chunk
        fi += 1
        name = f"99_fts_{fi:03d}.sql"
        with open(os.path.join(a.out, name), "w") as f:
            f.write("INSERT INTO tenders_fts(rowid,title,description,org_name,winner_name) "
                    f"SELECT rowid,title,description,org_name,winner_name FROM tenders "
                    f"WHERE rowid>={lo} AND rowid<{hi};\n")
        fts_files.append(name)
        lo = hi

    order = ["00_schema.sql"] + files + ["98_index.sql"] + fts_files
    D = a.out
    with open(os.path.join(a.out, "load.sh"), "w") as f:
        f.write("#!/bin/bash\nset -e\nDB=woodpecker\n")
        # Re-run guard: load.sh can be executed directly, bypassing export_d1.py's Python
        # check. Bake the estimate + ceiling in so a stale/rediscovered load.sh can't silently
        # push a massive load — it demands WP_CONFIRM_LOAD=YES for over-ceiling runs.
        f.write(f"EST_WRITES={est}\nMAX_WRITES={a.max_writes}\n")
        f.write('if [ "$MAX_WRITES" -gt 0 ] && [ "$EST_WRITES" -gt "$MAX_WRITES" ] && '
                '[ "$WP_CONFIRM_LOAD" != "YES" ]; then\n')
        f.write('  echo "REFUSING: this load writes ~$EST_WRITES D1 rows (> $MAX_WRITES ceiling)."\n')
        f.write('  echo "This guards the 2026-06-25 90M-write incident. To proceed: WP_CONFIRM_LOAD=YES ./load.sh"\n')
        f.write('  exit 2\nfi\n')
        f.write("cd /Users/chiragpatnaik/Code/woodpecker/api  # picks up wrangler.toml (account+d1)\n")
        for nm in order:
            # `d1 execute --remote --file` IS the bulk path (it uploads + imports server-side);
            # wrangler has no separate `d1 import` subcommand.
            f.write(f'echo ">> {nm}"; npx wrangler d1 execute $DB --remote --file {D}/{nm} -y\n')
    os.chmod(os.path.join(a.out, "load.sh"), 0o755)
    print(f"wrote {len(order)} sql files to {a.out}")
    print(f"  data chunks: {len(files)}  fts chunks: {len(fts_files)-1}  maxrow: {maxrow:,}")

if __name__ == "__main__":
    main()
