#!/usr/bin/env python3
"""Canonical archive: export the full corpus (incl. full descriptions that D1 truncates) to
year-sharded Parquet for R2 — the auditable, forkable source-of-truth layer.
Usage: python3 export_parquet.py [--out ~/wp_d1_build/parquet]"""
import argparse, os, sqlite3
import pyarrow as pa, pyarrow.parquet as pq

COLS = ["ocid","tender_id","entity_id","org_name","title","description","portal_type","state",
        "central","category","status","year","published_date","closing_date","award_date",
        "value_amount","emd","tender_fee","n_bids","winner_id","winner_name","winner_address",
        "has_detail","has_award","provenance","source","doc_url"]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/wp_d1_build/tenders.sqlite"))
    ap.add_argument("--out", default=os.path.expanduser("~/wp_d1_build/parquet"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    db = sqlite3.connect(a.db)
    years = [r[0] for r in db.execute("SELECT DISTINCT year FROM tenders ORDER BY year")]
    total = 0
    for y in years:
        where = "year IS NULL" if y is None else "year=%d" % y
        rows = db.execute("SELECT %s FROM tenders WHERE %s" % (",".join(COLS), where)).fetchall()
        if not rows:
            continue
        cols = {c: [r[i] for r in rows] for i, c in enumerate(COLS)}
        tbl = pa.table(cols)
        name = "year=%s.parquet" % ("null" if y is None else y)
        pq.write_table(tbl, os.path.join(a.out, name), compression="zstd")
        total += len(rows)
        sz = os.path.getsize(os.path.join(a.out, name))
        print("  %s : %s rows, %.1f MB" % (name, format(len(rows), ","), sz/1e6), flush=True)
    print("DONE: %s rows across %d shards" % (format(total, ","), len(years)))

if __name__ == "__main__":
    main()
