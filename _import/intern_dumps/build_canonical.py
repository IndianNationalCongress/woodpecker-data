#!/usr/bin/env python3
"""
Build the canonical D1-shaped SQLite from the intern AOC + VPS dumps.

Two source SQLite dumps (provenance: a student intern scraped GePNIC's AOC full-view +
tender-detail pages by id, 2011-2026 — see plan/2026-06-25-d1-rearchitecture.md):
  * aoc_tenders.db   -> award OUTCOMES (contract value, winning bidder, # bids)
  * tenders_vps.db   -> deep tender DETAIL (EMD, fee, classifications, dates, work desc)

The two are largely COMPLEMENTARY (only ~363k of ~4.9M unique tender_ids overlap), so we
MERGE them by canonical id into one row per tender. The merge store *is* the output D1
table: each source UPSERTs (INSERT ... ON CONFLICT(ocid) DO UPDATE with per-column COALESCE),
so order doesn't matter and it scales to 5M on disk.

Canonical id (cross-source join key is the GePNIC tender_id, but it's DIRTY — only ~75% of
rows are well-formed YEAR_DEPT_NNNNNN_N):
  * well-formed tender_id  -> ocid = "wp-" + tender_id           (merges AOC<->VPS)
  * dirty/missing          -> ocid = "wpu-" + internal_id (MD5)  (kept, not id-joined)

Provenance CLASS is `imported` (grouped with Assam/CPPP), with a finer `source` tag
(aoc / vps / aoc+vps) for honesty.

Usage:
  python3 build_canonical.py --out ~/wp_d1_build/tenders.sqlite \
      [--aoc ~/Downloads/aoc_tenders.db] [--vps ~/Downloads/tenders_vps.db] \
      [--limit N]            # cap rows per source (fast pilot)
      [--where "org_name='Kerala'"]   # AOC-side filter (pilot a slice)
"""
import argparse, html, json, os, re, sqlite3, sys, time

# reuse the existing entity resolver so new entities collapse into the SAME pills
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "compile"))
from entities import entity_of  # noqa: E402

# ---------- normalization helpers ----------
_TID = re.compile(r"^\d{4}_[A-Za-z0-9]+_\d+(?:_\d+)?$")
_SLUG = re.compile(r"[^a-z0-9]+")
_DIGITS = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WS = re.compile(r"\s+")

def clean_text(s):
    if not s:
        return None
    # the dumps carry doubly-mangled entities like '&ampamp#x0d' -> unescape a few times
    for _ in range(3):
        if "&" not in s:
            break
        s = html.unescape(s)
    s = s.replace("\x0d", " ").replace("#x0d", " ")
    s = _WS.sub(" ", s).strip()
    return s or None

def clean_tid(s):
    s = (s or "").strip()
    return s if _TID.match(s) else None

_DT_FMTS = ("%d-%b-%Y %I:%M %p", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d")
def parse_dt(s):
    s = (s or "").strip()
    if not s:
        return None
    import datetime
    for f in _DT_FMTS:
        try:
            return int(datetime.datetime.strptime(s, f).replace(tzinfo=datetime.timezone.utc).timestamp())
        except ValueError:
            continue
    return None

def parse_amt(s):
    """'₹ 18,74,075' / '1874075' / '₹ 0' -> int rupees (None if 0/blank).

    Sanity ceiling: ~0.0004% of source 'Contract Value' cells are corrupt — the scraper
    concatenated multiple package values on multi-bidder awards (e.g. '12538161253816' =
    two ₹12.5L packages mashed together). No single Indian govt contract reaches ₹1 lakh
    crore (1e12), so values >= that are dropped as garbage (the award row + winner are kept;
    only the unreliable amount is nulled). Multi-bidder value ambiguity below the ceiling is
    a known refinement (see plan/2026-06-25-d1-rearchitecture.md).
    """
    if s is None:
        return None
    m = _DIGITS.search(str(s))
    if not m:
        return None
    n = m.group(0).replace(",", "")
    try:
        v = int(round(float(n)))
    except ValueError:
        return None
    if not v or v >= 1_000_000_000_000:
        return None
    return v

def parse_int(s):
    m = re.search(r"\d+", str(s or ""))
    return int(m.group(0)) if m else None

def slug(s):
    return _SLUG.sub("-", (s or "").lower()).strip("-")

def supplier_of(name):
    name = clean_text(name)
    if not name:
        return None, None
    # normalize: drop common suffixes for the id, keep display name
    key = re.sub(r"\b(pvt|private|ltd|limited|co|company|and|the|m/s)\b", " ", name.upper())
    key = _WS.sub(" ", re.sub(r"[^A-Z0-9 ]", " ", key)).strip()
    sid = "sup-" + (slug(key) or slug(name))
    return sid[:120], name

def normkeys(d):
    """detail_json keys vary ('EMD' vs 'EMD *') -> index by alnum-lowercased key."""
    return {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in d.items()}

def cat_of(s):
    s = (s or "").strip().lower()
    if not s:
        return None
    if "work" in s:
        return "works"
    if "good" in s or "supply" in s or "procure" in s:
        return "goods"
    if "servic" in s or "consult" in s:
        return "services"
    return s[:24]

# ---------- D1 schema ----------
SCHEMA = """
PRAGMA journal_mode=WAL; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY; PRAGMA cache_size=-200000;
CREATE TABLE IF NOT EXISTS tenders (
  ocid TEXT PRIMARY KEY, tender_id TEXT, entity_id TEXT, org_name TEXT, title TEXT,
  description TEXT, portal_type TEXT, state TEXT, central INTEGER,
  category TEXT, status TEXT, year INTEGER,
  published_date INTEGER, closing_date INTEGER, award_date INTEGER,
  value_amount INTEGER, emd INTEGER, tender_fee INTEGER, n_bids INTEGER,
  winner_id TEXT, winner_name TEXT, winner_address TEXT,
  has_detail INTEGER DEFAULT 0, has_award INTEGER DEFAULT 0,
  provenance TEXT DEFAULT 'imported', source TEXT, doc_url TEXT
);
CREATE TABLE IF NOT EXISTS awards (
  ocid TEXT, supplier_id TEXT, value_amount INTEGER, award_date INTEGER,
  PRIMARY KEY (ocid, supplier_id)
);
CREATE TABLE IF NOT EXISTS entities (
  entity_id TEXT PRIMARY KEY, label TEXT, central INTEGER, state TEXT,
  n_tenders INTEGER DEFAULT 0, total_value INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS suppliers (
  supplier_id TEXT PRIMARY KEY, name TEXT, address TEXT,
  n_awards INTEGER DEFAULT 0, total_won INTEGER DEFAULT 0
);
"""

# UPSERT with per-column COALESCE so the two sources merge order-independently.
COLS = ["ocid","tender_id","org_name","title","description","portal_type","category","status",
        "year","published_date","closing_date","award_date","value_amount","emd","tender_fee",
        "n_bids","winner_id","winner_name","winner_address","has_detail","has_award","source","doc_url"]
_PH = ",".join("?" * len(COLS))
_SET = ",".join(
    f"{c}=COALESCE(excluded.{c},tenders.{c})" for c in COLS
    if c not in ("ocid","has_detail","has_award","source")
) + ", has_detail=MAX(tenders.has_detail,excluded.has_detail)" \
  + ", has_award=MAX(tenders.has_award,excluded.has_award)" \
  + ", source=CASE WHEN tenders.source IS NULL THEN excluded.source " \
    "WHEN excluded.source IS NULL OR instr(tenders.source,excluded.source)>0 THEN tenders.source " \
    "ELSE tenders.source||'+'||excluded.source END"
UPSERT = f"INSERT INTO tenders ({','.join(COLS)}) VALUES ({_PH}) ON CONFLICT(ocid) DO UPDATE SET {_SET}"

def row_tuple(d):
    return tuple(d.get(c) for c in COLS)

# ---------- source passes ----------
def ocid_for(tid, internal_id):
    ct = clean_tid(tid)
    return ("wp-" + ct, ct) if ct else ("wpu-" + internal_id, None)

def run_aoc(src, out, limit, where):
    sql = ("SELECT t.internal_id,t.tender_id,t.org_name,t.title,t.year,t.aoc_date,t.closing_date,"
           "d.details_json FROM aoc_tenders t JOIN aoc_details d ON t.internal_id=d.internal_id")
    if where:
        sql += " WHERE " + where
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur = src.execute(sql)
    batch, n = [], 0
    while True:
        rows = cur.fetchmany(5000)
        if not rows:
            break
        for internal_id, tid, org, title, year, aoc_date, closing, dj in rows:
            ocid, ctid = ocid_for(tid, internal_id)
            try:
                nd = normkeys(json.loads(dj)) if dj else {}
            except Exception:
                nd = {}
            sid, sname = supplier_of(nd.get("nameoftheselectedbidders"))
            val = parse_amt(nd.get("contractvalue"))
            adate = parse_dt(nd.get("contractdate")) or parse_dt(aoc_date)
            rec = {
                "ocid": ocid, "tender_id": ctid,
                "org_name": clean_text(nd.get("organisationname") or org),
                "title": clean_text(nd.get("tenderdescription") or title),
                "description": clean_text(nd.get("tenderdescription")),
                "portal_type": None, "category": cat_of(nd.get("tendertype")),
                "status": "awarded", "year": year,
                "published_date": parse_dt(nd.get("publisheddate")),
                "closing_date": parse_dt(closing), "award_date": adate,
                "value_amount": val, "emd": None, "tender_fee": None,
                "n_bids": parse_int(nd.get("numberofbidsreceived")),
                "winner_id": sid, "winner_name": sname,
                "winner_address": clean_text(nd.get("addressoftheselectedbidders")),
                "has_detail": 0, "has_award": 1, "source": "aoc",
                "doc_url": clean_text(nd.get("tenderdocument")),
            }
            batch.append(row_tuple(rec))
            if sid and val:
                batch_awards.append((ocid, sid, val, adate))
        out.executemany(UPSERT, batch)
        if batch_awards:
            out.executemany("INSERT OR IGNORE INTO awards VALUES (?,?,?,?)", batch_awards)
            batch_awards.clear()
        n += len(batch); batch.clear()
        if n % 100000 < 5000:
            print(f"  AOC {n:,}", flush=True)
    return n

def run_vps(src, out, limit, where):
    sql = ("SELECT t.internal_id,t.tender_id,t.organisation_name,t.title,t.status,"
           "t.e_published_date,t.bid_submission_closing_date,d.details_json "
           "FROM tenders t JOIN tender_details d ON t.internal_id=d.internal_id")
    if where:
        sql += " WHERE " + where
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur = src.execute(sql)
    batch, n = [], 0
    while True:
        rows = cur.fetchmany(5000)
        if not rows:
            break
        for internal_id, tid, org, title, status, epub, closing, dj in rows:
            ocid, ctid = ocid_for(tid, internal_id)
            try:
                nd = normkeys(json.loads(dj)) if dj else {}
            except Exception:
                nd = {}
            yr = None
            if ctid:
                yr = int(ctid[:4])
            rec = {
                "ocid": ocid, "tender_id": ctid,
                "org_name": clean_text(nd.get("organisationname") or org),
                "title": clean_text(nd.get("tendertitle") or title),
                "description": clean_text(nd.get("workdescription")),
                "portal_type": clean_text(nd.get("organisationtype")),
                "category": cat_of(nd.get("tendercategory")),
                "status": clean_text(status) or "tender", "year": yr,
                "published_date": parse_dt(nd.get("epublisheddate") or epub),
                "closing_date": parse_dt(nd.get("bidsubmissionenddate") or closing),
                "award_date": None,
                "value_amount": None, "emd": parse_amt(nd.get("emd")),
                "tender_fee": parse_amt(nd.get("tenderfee")),
                "n_bids": None, "winner_id": None, "winner_name": None, "winner_address": None,
                "has_detail": 1, "has_award": 0, "source": "vps",
                "doc_url": clean_text(nd.get("tenderdocument")),
            }
            batch.append(row_tuple(rec))
        out.executemany(UPSERT, batch)
        n += len(batch); batch.clear()
        if n % 100000 < 5000:
            print(f"  VPS {n:,}", flush=True)
    return n

def finalize(out):
    """Compute entity_id/state from merged org_name; roll up entities + suppliers.

    Scale-safe: resolve entity_of() once per DISTINCT org (not per row), apply via an
    indexed UPDATE..FROM, and roll up entities/suppliers with single GROUP BY passes
    (no correlated per-entity subqueries — those were O(entities x rows)).
    """
    print("  finalize: entity map (per distinct org)...", flush=True)
    orgs = [r[0] for r in out.execute("SELECT DISTINCT org_name FROM tenders").fetchall()]
    out.execute("CREATE TEMP TABLE org_map(org TEXT PRIMARY KEY, eid TEXT, central INT, state TEXT)")
    rows = []
    for org in orgs:
        e = entity_of(org)
        rows.append((org, e["id"], 1 if e["central"] else 0, e["state"]))
        ent_label[e["id"]] = (e["label"], 1 if e["central"] else 0, e["state"])
    out.executemany("INSERT OR IGNORE INTO org_map VALUES (?,?,?,?)", rows)
    print(f"    {len(orgs):,} distinct orgs -> {len(ent_label):,} entities; applying...", flush=True)
    out.execute("""UPDATE tenders SET entity_id=org_map.eid, central=org_map.central, state=org_map.state
                   FROM org_map WHERE org_map.org=tenders.org_name""")

    # LEAN index set — kept in lockstep with export_d1.INDEXES (the set actually shipped to D1).
    # These 4 are exactly what the serving Worker queries need; see export_d1.py for the rationale
    # (dropped ix_winner/ix_year/ix_awards_sup; upgraded ix_state -> composite with published_date).
    # ix_winner used to speed the suppliers rollup below; that GROUP BY now sorts instead — a
    # one-time local cost, not a D1 write, so it stays dropped.
    print("  finalize: indexes...", flush=True)
    for ix in ["CREATE INDEX IF NOT EXISTS ix_entity ON tenders(entity_id,published_date)",
               "CREATE INDEX IF NOT EXISTS ix_state ON tenders(state,published_date)",
               "CREATE INDEX IF NOT EXISTS ix_cat ON tenders(category,year)",
               "CREATE INDEX IF NOT EXISTS ix_value ON tenders(value_amount)"]:
        out.execute(ix)

    print("  finalize: entities rollup...", flush=True)
    out.execute("DELETE FROM entities")
    agg = out.execute("SELECT entity_id, COUNT(*), COALESCE(SUM(value_amount),0) "
                      "FROM tenders WHERE entity_id IS NOT NULL GROUP BY entity_id").fetchall()
    out.executemany(
        "INSERT INTO entities (entity_id,label,central,state,n_tenders,total_value) VALUES (?,?,?,?,?,?)",
        [(eid, ent_label.get(eid, (eid, 0, None))[0], ent_label.get(eid, (eid, 0, None))[1],
          ent_label.get(eid, (eid, 0, None))[2], n, v) for eid, n, v in agg])

    print("  finalize: suppliers rollup...", flush=True)
    out.execute("""INSERT INTO suppliers (supplier_id,name,n_awards,total_won)
        SELECT winner_id, MAX(winner_name), COUNT(*), COALESCE(SUM(value_amount),0)
        FROM tenders WHERE winner_id IS NOT NULL GROUP BY winner_id""")

    print("  finalize: FTS...", flush=True)
    out.execute("DROP TABLE IF EXISTS tenders_fts")
    # columnsize=0 drops the %_docsize shadow table (one write/doc): the Worker ranks by keyset
    # order, never bm25(), so per-column length data is dead weight. Mirrors export_d1.FTS_CREATE.
    out.execute("CREATE VIRTUAL TABLE tenders_fts USING fts5(title,description,org_name,winner_name,content='',columnsize=0)")
    out.execute("""INSERT INTO tenders_fts(rowid,title,description,org_name,winner_name)
        SELECT rowid,title,description,org_name,winner_name FROM tenders""")

batch_awards = []
ent_label = {}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--aoc", default=os.path.expanduser("~/Downloads/aoc_tenders.db"))
    ap.add_argument("--vps", default=os.path.expanduser("~/Downloads/tenders_vps.db"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--where", default="")
    ap.add_argument("--vps-where", default="")
    a = ap.parse_args()
    t0 = time.time()
    out = sqlite3.connect(os.path.expanduser(a.out))
    out.executescript(SCHEMA)
    aoc = sqlite3.connect(f"file:{a.aoc}?mode=ro", uri=True)
    vps = sqlite3.connect(f"file:{a.vps}?mode=ro", uri=True)
    print("== AOC pass ==", flush=True)
    na = run_aoc(aoc, out, a.limit, a.where); out.commit()
    print(f"AOC rows: {na:,}  ({time.time()-t0:.0f}s)", flush=True)
    print("== VPS pass ==", flush=True)
    nv = run_vps(vps, out, a.limit, a.vps_where or a.where if False else a.vps_where); out.commit()
    print(f"VPS rows: {nv:,}  ({time.time()-t0:.0f}s)", flush=True)
    finalize(out); out.commit()
    tot = out.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
    awd = out.execute("SELECT COUNT(*) FROM tenders WHERE has_award=1").fetchone()[0]
    det = out.execute("SELECT COUNT(*) FROM tenders WHERE has_detail=1").fetchone()[0]
    both = out.execute("SELECT COUNT(*) FROM tenders WHERE has_award=1 AND has_detail=1").fetchone()[0]
    val = out.execute("SELECT COUNT(*) FROM tenders WHERE value_amount IS NOT NULL").fetchone()[0]
    ents = out.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    sups = out.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
    print(f"\n== DONE ({time.time()-t0:.0f}s) ==")
    print(f"  tenders: {tot:,}  | has_award {awd:,}  has_detail {det:,}  both {both:,}")
    print(f"  with value: {val:,}  | entities {ents:,}  suppliers {sups:,}")
    out.commit(); out.close()

if __name__ == "__main__":
    main()
