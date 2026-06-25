#!/usr/bin/env python3
"""
Rescue the stub-id collapse (the "BHEL EDN artifact"). Clean tender_ids that map to >N distinct
AOC detail pages are org-level STUBS (e.g. 2018_BHEL_1 x8620) — distinct tenders the tender_id
keying wrongly merged into ONE canonical row = DROPPED. Re-key each such page by internal_id
(wpu-<id>) so every tender survives. Principle: duplication < dropping.

Patches the LOCAL canonical (~/wp_d1_build/tenders.sqlite) and writes D1 sync SQL +
the list of rescued ocids (to embed) and stale ocids (to drop from Vectorize).

Usage: python3 rescue_stubs.py [--min-pages 5]
"""
import argparse, json, os, sqlite3, sys
sys.path.insert(0, os.path.dirname(__file__))
from build_canonical import (clean_text, clean_tid, parse_dt, parse_amt, parse_int,
                             cat_of, supplier_of, normkeys, COLS)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "compile"))
from entities import entity_of

AOC = os.path.expanduser("~/Downloads/aoc_tenders.db")
LOCAL = os.path.expanduser("~/wp_d1_build/tenders.sqlite")
OUT = os.path.expanduser("~/wp_d1_build/rescue")
os.makedirs(OUT, exist_ok=True)

def aoc_record(internal_id, tid_clean, org, title, year, aoc_date, closing, dj):
    """AOC row -> canonical dict (same mapping as build_canonical.run_aoc) but ocid=wpu-<id>."""
    try:
        nd = normkeys(json.loads(dj)) if dj else {}
    except Exception:
        nd = {}
    sid, sname = supplier_of(nd.get("nameoftheselectedbidders"))
    val = parse_amt(nd.get("contractvalue"))
    e = entity_of(clean_text(nd.get("organisationname") or org))
    return {
        "ocid": "wpu-" + internal_id, "tender_id": None,   # stub id is unreliable -> drop it
        "entity_id": e["id"], "org_name": clean_text(nd.get("organisationname") or org),
        "title": clean_text(nd.get("tenderdescription") or title),
        "description": clean_text(nd.get("tenderdescription")),
        "portal_type": None, "state": e["state"], "central": 1 if e["central"] else 0,
        "category": cat_of(nd.get("tendertype")), "status": "awarded", "year": year,
        "published_date": parse_dt(nd.get("publisheddate")), "closing_date": parse_dt(closing),
        "award_date": parse_dt(nd.get("contractdate")) or parse_dt(aoc_date),
        "value_amount": val if (sname and "," not in (sname or "")) else val,  # keep; multi-bidder handled later
        "emd": None, "tender_fee": None, "n_bids": parse_int(nd.get("numberofbidsreceived")),
        "winner_id": sid, "winner_name": sname, "winner_address": clean_text(nd.get("addressoftheselectedbidders")),
        "has_detail": 0, "has_award": 1, "provenance": "imported", "source": "aoc",
        "doc_url": clean_text(nd.get("tenderdocument")),
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-pages", type=int, default=5)
    a = ap.parse_args()
    aoc = sqlite3.connect("file:%s?mode=ro" % AOC, uri=True)
    print("indexing aoc tender_id (one-time)...", flush=True)
    try:
        sqlite3.connect(AOC).execute("CREATE INDEX IF NOT EXISTS ix_tid ON aoc_tenders(tender_id)")
    except Exception as e:
        print("  (index note:", e, ")")

    print("finding stub tender_ids (>%d distinct detail pages, clean format)..." % a.min_pages, flush=True)
    stubs = []
    for tid, n in aoc.execute(
        "SELECT tender_id, COUNT(DISTINCT internal_id) c FROM aoc_tenders WHERE tender_id<>'' "
        "GROUP BY tender_id HAVING c > ?", (a.min_pages,)):
        if clean_tid(tid):                      # only ids my ETL treated as clean (= collapsed)
            stubs.append(tid)
    print("  %d stub tender_ids" % len(stubs), flush=True)

    local = sqlite3.connect(LOCAL)
    collist = ",".join(COLS)
    ph = ",".join("?" * len(COLS))
    rescued, stale = [], []
    ins = 0
    for i, tid in enumerate(stubs):
        stale.append("wp-" + tid)               # the collapsed canonical row to remove
        rows = aoc.execute(
            "SELECT t.internal_id,t.org_name,t.title,t.year,t.aoc_date,t.closing_date,d.details_json "
            "FROM aoc_tenders t JOIN aoc_details d ON t.internal_id=d.internal_id WHERE t.tender_id=?",
            (tid,)).fetchall()
        batch = []
        for internal_id, org, title, year, aoc_date, closing, dj in rows:
            rec = aoc_record(internal_id, tid, org, title, year, aoc_date, closing, dj)
            batch.append(tuple(rec[c] for c in COLS))
            rescued.append(rec["ocid"])
        # local: replace collapsed row with the distinct pages
        local.execute("DELETE FROM tenders WHERE ocid=?", ("wp-" + tid,))
        local.executemany(
            "INSERT OR IGNORE INTO tenders (%s) VALUES (%s)" % (collist, ph), batch)
        ins += len(batch)
        if i % 100 == 0:
            print("  %d/%d stubs, +%d rows" % (i, len(stubs), ins), flush=True)
    local.commit()
    print("LOCAL patched: removed %d collapsed rows, inserted %d distinct tenders" % (len(stale), ins))
    json.dump({"rescued": rescued, "stale": stale, "stub_tids": stubs}, open(os.path.join(OUT, "delta.json"), "w"))
    print("wrote", os.path.join(OUT, "delta.json"), "(%d rescued ocids, %d stale)" % (len(rescued), len(stale)))

if __name__ == "__main__":
    main()
