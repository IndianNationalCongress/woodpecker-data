#!/usr/bin/env python3
"""
Export the canonical sqlite into chunked SQL for `wrangler d1 execute --remote --file`.

Two modes:
  * FULL  (default)      -> emit the whole corpus (schema + data + indexes + FTS rebuild).
                           Guarded by the write-volume estimator; genuinely expensive.
  * DIFF  (--snapshot S) -> emit ONLY the rows that changed vs a snapshot of what D1 already
                           holds. A routine load becomes a few-K write event instead of ~90M.
                           This is the structural fix for the 2026-06-25 $52 bill.

D1 bills EVERY b-tree write as a "row written": the base row + one write per index (incl. the
implicit PRIMARY-KEY index) + FTS5 shadow rows. So the two levers that matter are (a) how many
LOGICAL rows we touch and (b) the per-row amplification (index count + FTS shadow rows). DIFF
attacks (a); the lean 4-index schema + `columnsize=0` FTS attacks (b).

D1 quirks this works around:
  * import file practical size -> chunk DATA into ~MAXMB files.
  * 30s/statement -> FTS is populated server-side in rowid-range chunks, not one big INSERT.
  * FTS5 shadow tables don't import cleanly -> we ship base tables only + a rebuild/patch step.

Emits into OUTDIR (FULL mode):
  00_schema.sql      base table DDL (IF NOT EXISTS, no indexes yet -> faster bulk insert)
  01_data_NNN.sql    chunked multi-row INSERTs (tenders, awards, entities, suppliers)
  98_index.sql       CREATE INDEX IF NOT EXISTS (after data)
  99_fts_NNN.sql     CREATE fts + chunked INSERT..SELECT populate
  load.sh            runs them all via wrangler in order (with an empty-target reload guard)

Emits into OUTDIR (DIFF mode — no CREATE INDEX, no full FTS rebuild):
  00_schema.sql      IF NOT EXISTS (harmless; guarantees the tables exist)
  01_data_NNN.sql    per-row UPSERTs for new/changed rows only
  50_delete.sql      DELETEs for rows removed since the snapshot
  99_fts_NNN.sql     incremental FTS delete+insert for the touched tenders only
  load.sh            runs them; no reload guard (a diff is meant to stack)

Usage:
  # full (rare, deliberate — guarded by the write ceiling + a WP_RELOAD=YES empty-target check):
  python3 export_d1.py --db ~/wp_d1_build/tenders.sqlite --out ~/wp_d1_build/d1sql
  # routine incremental load against a mirror of current D1 contents:
  python3 export_d1.py --db ~/wp_d1_build/tenders.sqlite --out ~/wp_d1_build/d1sql \
      --snapshot ~/wp_d1_build/d1_current.sqlite
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
# primary key per table (drives diff keying + ON CONFLICT targets)
PK = {"tenders": ["ocid"], "awards": ["ocid","supplier_id"],
      "entities": ["entity_id"], "suppliers": ["supplier_id"]}

SCHEMA = """CREATE TABLE IF NOT EXISTS tenders (
  ocid TEXT PRIMARY KEY, tender_id TEXT, entity_id TEXT, org_name TEXT, title TEXT,
  description TEXT, portal_type TEXT, state TEXT, central INTEGER, category TEXT, status TEXT,
  year INTEGER, published_date INTEGER, closing_date INTEGER, award_date INTEGER,
  value_amount INTEGER, emd INTEGER, tender_fee INTEGER, n_bids INTEGER,
  winner_id TEXT, winner_name TEXT, winner_address TEXT,
  has_detail INTEGER DEFAULT 0, has_award INTEGER DEFAULT 0, provenance TEXT, source TEXT, doc_url TEXT);
CREATE TABLE IF NOT EXISTS awards (ocid TEXT, supplier_id TEXT, value_amount INTEGER, award_date INTEGER, PRIMARY KEY(ocid,supplier_id));
CREATE TABLE IF NOT EXISTS entities (entity_id TEXT PRIMARY KEY, label TEXT, central INTEGER, state TEXT, n_tenders INTEGER, total_value INTEGER);
CREATE TABLE IF NOT EXISTS suppliers (supplier_id TEXT PRIMARY KEY, name TEXT, address TEXT, n_awards INTEGER, total_won INTEGER);
"""

# LEAN index set — every secondary index is +1 physical write per row per load, so we keep ONLY
# the indexes the serving Worker's queries actually use (woodpecker/api/src/index.js). Cut from 7:
#   dropped ix_winner      — no query filters tenders.winner_id (suppliers come pre-rolled-up).
#   dropped ix_year        — year-only is a low-cardinality scan; category+year is served by ix_cat.
#   dropped ix_awards_sup  — awards are only ever fetched by ocid (PK prefix), never by supplier_id.
#   upgraded ix_state      -> (state,published_date): same 1 write/row but now serves the sorted
#                             state facet (/search state=... ORDER BY published_date DESC).
# Net: tenders 6->4 secondary, awards 1->0. Saves ~2x6.3M + 1x(awards) writes per full load.
INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_entity ON tenders(entity_id,published_date)",  # entity drill-down + default sort
    "CREATE INDEX IF NOT EXISTS ix_state  ON tenders(state,published_date)",      # state facet + default sort
    "CREATE INDEX IF NOT EXISTS ix_cat    ON tenders(category,year)",             # category nav (+ optional year)
    "CREATE INDEX IF NOT EXISTS ix_value  ON tenders(value_amount)",              # value range filter + sort=value
]

# Contentless FTS5. `columnsize=0` drops the %_docsize shadow table (one write per doc): the Worker
# ranks by keyset order, never bm25(), so per-column length normalisation is unused -> pure savings.
FTS_COLS = ["title", "description", "org_name", "winner_name"]
FTS_CREATE = ("CREATE VIRTUAL TABLE IF NOT EXISTS tenders_fts USING "
              "fts5(title,description,org_name,winner_name,content='',columnsize=0)")


def sqlstr(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"

def shipped_select(cols):
    """The column projection actually shipped to D1: description capped to 400 chars (the full
    work-description lives in the R2 blob). Used identically on both sides of a diff so an
    unchanged row is byte-identical and never re-emitted."""
    return ",".join("substr(description,1,400)" if c == "description" else c for c in cols)

# --- Write-volume guard -----------------------------------------------------
# Estimates the physical D1 row-writes a load will cost and refuses to emit load.sh above a
# ceiling unless --force. A full reload of the 6.3M-row corpus genuinely amplifies ~12x here.
DEFAULT_MAX_WRITES = 20_000_000     # abort above this many projected row-writes
FTS_WRITE_FACTOR   = 3              # ~shadow-table rows written per FTS5 doc (with columnsize=0)

def _indexes_per_table():
    """count secondary indexes declared in INDEXES, per table."""
    counts = {t: 0 for t in BASE_TABLES}
    for stmt in INDEXES:
        tbl = stmt.split(" ON ", 1)[1].split("(", 1)[0].strip()
        if tbl in counts:
            counts[tbl] += 1
    return counts

def _mult(tbl, sec):
    return 1 + 1 + sec[tbl]              # base row + implicit PK index + secondary indexes

def estimate_full(db, limit=0):
    """(total_writes, breakdown lines) for a FULL load of `db`."""
    sec = _indexes_per_table()
    lines, total = [], 0
    for tbl in BASE_TABLES:
        n = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        if limit and tbl == "tenders":
            n = min(n, limit)
        w = n * _mult(tbl, sec)
        total += w
        lines.append(f"  {tbl:<10} {n:>12,} rows x {_mult(tbl,sec)} (1 base +1 pk +{sec[tbl]} idx) = {w:>14,}")
    fts_docs = db.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
    if limit:
        fts_docs = min(fts_docs, limit)
    fts_w = fts_docs * FTS_WRITE_FACTOR
    total += fts_w
    lines.append(f"  {'tenders_fts':<10} {fts_docs:>12,} docs x ~{FTS_WRITE_FACTOR} (fts5 shadow)      = {fts_w:>14,}")
    return total, lines

def estimate_diff(counts):
    """(total_writes, breakdown lines) for a DIFF load, given per-table (upserts, deletes) counts
    and the FTS op count. `counts` = {tbl: (n_upsert, n_delete)} plus counts['_fts'] = n_fts_ops."""
    sec = _indexes_per_table()
    lines, total = [], 0
    for tbl in BASE_TABLES:
        up, dl = counts.get(tbl, (0, 0))
        touched = up + dl
        w = touched * _mult(tbl, sec)
        total += w
        lines.append(f"  {tbl:<10} {up:>9,} upsert +{dl:>7,} del x {_mult(tbl,sec)} = {w:>12,}")
    fts_ops = counts.get("_fts", 0)
    fts_w = fts_ops * FTS_WRITE_FACTOR
    total += fts_w
    lines.append(f"  {'tenders_fts':<10} {fts_ops:>9,} fts ops               x ~{FTS_WRITE_FACTOR} = {fts_w:>12,}")
    return total, lines

def guard(est, max_writes, force):
    """Print the estimate and abort above the ceiling unless --force. Raises SystemExit on abort."""
    if max_writes and est > max_writes and not force:
        print(f"\nABORT: projected {est:,} writes exceeds --max-writes={max_writes:,}.")
        print("This is the guard against the 2026-06-25 90M-write incident. A FULL reload of the")
        print("whole corpus is genuinely this expensive (4 indexes + FTS5 still amplify ~12x).")
        print("To load cheaply, prefer a DIFF load: --snapshot <mirror-of-current-D1>. If you really")
        print("intend a full reload, re-run with --force.")
        raise SystemExit(2)
    if force and max_writes and est > max_writes:
        print(f"--force set: proceeding despite {est:,} > {max_writes:,} projected writes.")


# ============================ FULL mode emission ============================
def emit_full(db, a, est):
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
        selexpr = shipped_select(cols)
        sql = f"SELECT {selexpr} FROM {tbl}"
        if a.limit and tbl == "tenders":
            sql += f" LIMIT {a.limit}"
        cur = db.execute(sql)
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

    # FTS: create + chunked populate by rowid range (full desc; the R2 blob keeps the untruncated text)
    maxrow = db.execute("SELECT MAX(rowid) FROM tenders").fetchone()[0] or 0
    lo0 = max(1, a.fts_since_rowid + 1)     # incremental append: only rowids past the high-water mark
    fts_files = []
    fi = 0
    with open(os.path.join(a.out, f"99_fts_{fi:03d}.sql"), "w") as f:
        f.write(FTS_CREATE + ";\n")
    fts_files.append(f"99_fts_{fi:03d}.sql")
    ftscols = ",".join(FTS_COLS)
    lo = lo0
    while lo <= maxrow:
        hi = lo + a.fts_chunk
        fi += 1
        name = f"99_fts_{fi:03d}.sql"
        with open(os.path.join(a.out, name), "w") as f:
            f.write(f"INSERT INTO tenders_fts(rowid,{ftscols}) "
                    f"SELECT rowid,{ftscols} FROM tenders "
                    f"WHERE rowid>={lo} AND rowid<{hi};\n")
        fts_files.append(name)
        lo = hi

    order = ["00_schema.sql"] + files + ["98_index.sql"] + fts_files
    write_load_sh(a, order, est, reload_guard=True)
    print(f"wrote {len(order)} sql files to {a.out}")
    print(f"  FULL load: data chunks {len(files)}  fts chunks {len(fts_files)-1}  maxrow {maxrow:,}")


# ============================ DIFF mode emission ============================
def _proj_map(cols):
    return {c: i for i, c in enumerate(cols)}

def emit_diff(db, a, snapshot):
    """Attach the snapshot (a mirror of what D1 currently holds), emit only changed rows.

    Correctness contract: the snapshot MUST carry the same rowids D1 has (i.e. it is the
    actual last-shipped DB, or a mirror kept in lockstep by applying the same diffs). New
    tenders get an explicit rowid allocated past the snapshot's MAX(rowid) and that SAME rowid
    is used for the base INSERT and the FTS insert, so the FTS join (t.rowid=f.rowid) stays
    consistent without a full rebuild. Changed rows upsert in place (rowid preserved).
    """
    db.execute("ATTACH DATABASE ? AS snap", (snapshot,))
    counts = {}
    files = []
    ftscols = ",".join(FTS_COLS)

    # ---- schema (idempotent; guarantees tables exist) ----
    with open(os.path.join(a.out, "00_schema.sql"), "w") as f:
        f.write(SCHEMA)
        f.write(FTS_CREATE + ";\n")

    data_fh = open(os.path.join(a.out, "01_data_000.sql"), "w")
    files.append("01_data_000.sql")
    del_lines = []
    fts_lines = []

    # tenders rowid bookkeeping for FTS consistency
    snap_rowid = dict(db.execute("SELECT ocid, rowid FROM snap.tenders").fetchall())
    orig_snap_ocids = set(snap_rowid)       # membership BEFORE we allocate rowids for new ocids
    base_max = db.execute("SELECT COALESCE(MAX(rowid),0) FROM snap.tenders").fetchone()[0]
    next_rowid = base_max

    for tbl, cols in BASE_TABLES.items():
        collist = ",".join(cols)
        proj = shipped_select(cols)
        idx = _proj_map(cols)
        pk = PK[tbl]
        # new + changed: shipped projection present in canonical but not byte-identical in snapshot
        up_rows = db.execute(
            f"SELECT {proj} FROM main.{tbl} EXCEPT SELECT {proj} FROM snap.{tbl}"
        ).fetchall()
        # deletes: pk present in snapshot but gone from canonical
        pkcols = ",".join(pk)
        del_rows = db.execute(
            f"SELECT {pkcols} FROM snap.{tbl} EXCEPT SELECT {pkcols} FROM main.{tbl}"
        ).fetchall()
        counts[tbl] = (len(up_rows), len(del_rows))

        # ON CONFLICT DO UPDATE set-list (every non-pk column)
        setlist = ",".join(f"{c}=excluded.{c}" for c in cols if c not in pk)

        for r in up_rows:
            vals = ",".join(sqlstr(v) for v in r)
            if tbl == "tenders":
                ocid = r[idx["ocid"]]
                if ocid in snap_rowid:                       # changed -> upsert in place (rowid preserved)
                    data_fh.write(f"INSERT INTO tenders ({collist}) VALUES ({vals}) "
                                  f"ON CONFLICT(ocid) DO UPDATE SET {setlist};\n")
                else:                                        # new -> explicit rowid so FTS matches
                    next_rowid += 1
                    data_fh.write(f"INSERT INTO tenders (rowid,{collist}) VALUES ({next_rowid},{vals});\n")
                    snap_rowid[ocid] = next_rowid            # remember for the FTS pass below
            else:
                data_fh.write(f"INSERT INTO {tbl} ({collist}) VALUES ({vals}) "
                              f"ON CONFLICT({pkcols}) DO UPDATE SET {setlist};\n")

        # deletes
        for r in del_rows:
            cond = " AND ".join(f"{c}={sqlstr(v)}" for c, v in zip(pk, r))
            del_lines.append(f"DELETE FROM {tbl} WHERE {cond};\n")

    # ---- incremental FTS: unindex old text, index new text, for touched tenders only ----
    tcols = BASE_TABLES["tenders"]
    tidx = _proj_map(tcols)
    up_tenders = db.execute(
        f"SELECT {shipped_select(tcols)} FROM main.tenders "
        f"EXCEPT SELECT {shipped_select(tcols)} FROM snap.tenders"
    ).fetchall()
    changed_new_ocids = [r[tidx["ocid"]] for r in up_tenders]
    del_tender_ocids = [r[0] for r in db.execute(
        "SELECT ocid FROM snap.tenders EXCEPT SELECT ocid FROM main.tenders").fetchall()]

    # which touched ocids already existed in the snapshot (they need an FTS 'delete' of old text first)
    existed = set(changed_new_ocids) & orig_snap_ocids
    old_text = _fts_text(db, "snap", existed | set(del_tender_ocids))
    new_text = _fts_text(db, "main", set(changed_new_ocids))
    fts_ops = 0
    for ocid in changed_new_ocids:
        rid = snap_rowid.get(ocid)
        if rid is None:
            continue
        if ocid in existed and ocid in old_text:             # changed -> delete old fts entry first
            fts_lines.append(f"INSERT INTO tenders_fts(tenders_fts,rowid,{ftscols}) "
                             f"VALUES('delete',{rid},{_fts_vals(old_text[ocid])});\n")
            fts_ops += 1
        t = new_text.get(ocid)
        if t is not None:
            fts_lines.append(f"INSERT INTO tenders_fts(rowid,{ftscols}) VALUES({rid},{_fts_vals(t)});\n")
            fts_ops += 1
    for ocid in del_tender_ocids:                            # removed -> unindex
        rid = snap_rowid.get(ocid)
        t = old_text.get(ocid)
        if rid is not None and t is not None:
            fts_lines.append(f"INSERT INTO tenders_fts(tenders_fts,rowid,{ftscols}) "
                             f"VALUES('delete',{rid},{_fts_vals(t)});\n")
            fts_ops += 1
    counts["_fts"] = fts_ops

    data_fh.close()
    order = ["00_schema.sql"] + files
    if del_lines:
        with open(os.path.join(a.out, "50_delete.sql"), "w") as f:
            f.writelines(del_lines)
        order.append("50_delete.sql")
    if fts_lines:
        with open(os.path.join(a.out, "99_fts_000.sql"), "w") as f:
            f.writelines(fts_lines)
        order.append("99_fts_000.sql")

    est, breakdown = estimate_diff(counts)
    print("projected D1 row-writes for this DIFF load:")
    print("\n".join(breakdown))
    print(f"  {'TOTAL':<10}                                 = {est:>12,}")
    guard(est, a.max_writes, a.force)
    write_load_sh(a, order, est, reload_guard=False)
    tot_up = sum(counts[t][0] for t in BASE_TABLES)
    tot_del = sum(counts[t][1] for t in BASE_TABLES)
    print(f"wrote {len(order)} sql files to {a.out}")
    print(f"  DIFF load: {tot_up:,} upserts  {tot_del:,} deletes  {fts_ops:,} fts ops  (vs {snapshot})")
    db.execute("DETACH DATABASE snap")

def _fts_text(db, schema, ocids):
    """{ocid: (title,description,org_name,winner_name)} for the given ocids from `schema`.tenders."""
    if not ocids:
        return {}
    out, ocids = {}, list(ocids)
    cols = ",".join(["ocid"] + FTS_COLS)
    for i in range(0, len(ocids), 400):
        chunk = ocids[i:i+400]
        ph = ",".join("?" * len(chunk))
        for row in db.execute(f"SELECT {cols} FROM {schema}.tenders WHERE ocid IN ({ph})", chunk):
            out[row[0]] = row[1:]
    return out

def _fts_vals(t):
    return ",".join(sqlstr(v) for v in t)


# ============================ load.sh ============================
def write_load_sh(a, order, est, reload_guard):
    D = a.out
    with open(os.path.join(a.out, "load.sh"), "w") as f:
        f.write("#!/bin/bash\nset -e\nDB=woodpecker\n")
        # Re-run guard: load.sh can be executed directly, bypassing export_d1.py's Python check.
        # Bake the estimate + ceiling in so a stale/rediscovered load.sh can't silently push a
        # massive load — it demands WP_CONFIRM_LOAD=YES for over-ceiling runs.
        f.write(f"EST_WRITES={est}\nMAX_WRITES={a.max_writes}\n")
        f.write('if [ "$MAX_WRITES" -gt 0 ] && [ "$EST_WRITES" -gt "$MAX_WRITES" ] && '
                '[ "$WP_CONFIRM_LOAD" != "YES" ]; then\n')
        f.write('  echo "REFUSING: this load writes ~$EST_WRITES D1 rows (> $MAX_WRITES ceiling)."\n')
        f.write('  echo "This guards the 2026-06-25 90M-write incident. To proceed: WP_CONFIRM_LOAD=YES ./load.sh"\n')
        f.write('  exit 2\nfi\n')
        f.write("cd /Users/chiragpatnaik/Code/woodpecker/api  # picks up wrangler.toml (account+d1)\n")
        if reload_guard:
            # Idempotency: a FULL load assumes an empty target. Refuse to stack onto a non-empty
            # DB (which would double the row count + re-pay every index write) unless WP_RELOAD=YES.
            # If `tenders` doesn't exist yet (first load) the query errors -> EXIST empty -> allowed.
            f.write('EXIST=$(npx wrangler d1 execute $DB --remote --json '
                    '--command "SELECT COUNT(*) AS n FROM tenders" 2>/dev/null '
                    "| grep -o '\"n\":[0-9]*' | head -1 | grep -o '[0-9]*')\n")
            f.write('if [ -n "$EXIST" ] && [ "$EXIST" -gt 0 ] && [ "$WP_RELOAD" != "YES" ]; then\n')
            f.write('  echo "REFUSING: target D1 already holds $EXIST tenders rows; a FULL load would stack on top."\n')
            f.write('  echo "For a deliberate full reload, wipe the DB first, then: WP_RELOAD=YES ./load.sh"\n')
            f.write('  echo "For a routine update, use a DIFF load instead: export_d1.py --snapshot <mirror>."\n')
            f.write('  exit 2\nfi\n')
        for nm in order:
            f.write(f'echo ">> {nm}"; npx wrangler d1 execute $DB --remote --file {D}/{nm} -y\n')
    os.chmod(os.path.join(a.out, "load.sh"), 0o755)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/wp_d1_build/tenders.sqlite"))
    ap.add_argument("--out", default=os.path.expanduser("~/wp_d1_build/d1sql"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--maxmb", type=int, default=45)         # per data chunk
    ap.add_argument("--rows-per-insert", type=int, default=40)  # D1 caps a statement at 100KB
    ap.add_argument("--fts-chunk", type=int, default=150000)  # rowid range per FTS populate stmt
    ap.add_argument("--fts-since-rowid", type=int, default=0,
                    help="FULL mode: only (re)populate FTS for rowids past this high-water mark")
    ap.add_argument("--snapshot", default=None,
                    help="DIFF mode: a SQLite mirror of current D1 contents; emit only changed rows")
    ap.add_argument("--max-writes", type=int, default=DEFAULT_MAX_WRITES,
                    help="abort if a load would write more than this many D1 rows (0=unlimited)")
    ap.add_argument("--force", action="store_true",
                    help="bypass the write-volume ceiling (use only for a deliberate full reload)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    db = sqlite3.connect(a.db)

    if a.snapshot:
        emit_diff(db, a, os.path.expanduser(a.snapshot))
        db.close()
        return

    # --- FULL mode: pre-flight write-volume guard, then emit ---
    est, breakdown = estimate_full(db, a.limit)
    print("projected D1 row-writes for this FULL load:")
    print("\n".join(breakdown))
    print(f"  {'TOTAL':<10} {'':>12}                                = {est:>14,}")
    guard(est, a.max_writes, a.force)
    emit_full(db, a, est)
    db.close()

if __name__ == "__main__":
    main()
