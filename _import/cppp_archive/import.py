#!/usr/bin/env python3
"""
CPPP archive importer (GTI/DIGIWHIST India CPPP extract, 2012–18, ~172k awards).

Reads the durable records.jsonl.gz and emits the compact archive serving layer to
docs/cppp_archive/ — index-only (no per-tender record files), MONTH-sharded so no
index file approaches the 20 MB cap. Declared/imported data: provenance is set, and
the post-compile entity step re-keys these into per-entity sources alongside the rest.
"""
import gzip, json, os, collections

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
DOCS = os.path.join(REPO, "docs")
SID = "cppp_archive"
SRC = os.path.join(HERE, "records.jsonl.gz")
LATEST_LIMIT = 200

CATMAP = {"WORKS": "works", "SUPPLIES": "goods", "SERVICES": "services"}
WORD = {"works": "Works", "goods": "Goods", "services": "Services"}

def catof(rec):
    return CATMAP.get((rec.get("tender_supplytype") or "").strip(), "")

def synth_title(rec):
    t = (rec.get("tender_title") or "").strip()
    if t:
        return t
    word = WORD.get(catof(rec), "Contract")
    sup = (rec.get("bidder_name") or "").strip()
    yr = rec.get("_year") or ""
    s = f"{word} award · won by {sup}" if sup else f"{word} award · {(rec.get('buyer_name') or '').strip() or 'unknown buyer'}"
    return s + (f" ({yr})" if yr else "")

def entry(rec):
    yr = rec.get("_year")
    iso = rec.get("_awardDateISO") or (f"{yr}-01-01" if yr else "")
    pub = (iso + "T00:00:00Z") if iso else ""
    e = {
        "ocid": rec.get("tender_id"), "source": SID,
        "title": synth_title(rec), "buyer": (rec.get("buyer_name") or "").strip(),
        "category": catof(rec), "status": "complete", "latestTag": "award",
        "publishedDate": pub, "closingDate": "", "lastReleaseDate": pub,
        "releaseCount": 1, "corrigendumCount": 0, "hasDeadLink": False, "hasOcr": False,
        "hasUndeclaredChange": False, "pulled": False, "observationCount": 0,
        "imported": True, "fiscalYear": str(yr) if yr else "",
    }
    sup = (rec.get("bidder_name") or "").strip()
    if sup:
        e["supplier"] = sup
    nb = (rec.get("lot_bidscount") or "").strip()
    if nb.isdigit():
        e["numberOfTenderers"] = int(nb)
    if rec.get("_singleBidder") is True:
        e["singleBidder"] = True
    return e

def w(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":")); f.write("\n")

def main():
    shards = collections.defaultdict(list)
    n = 0
    with gzip.open(SRC, "rt", encoding="utf-8") as f:
        for line in f:
            e = entry(json.loads(line))
            shards[(e["publishedDate"] or "0000-00")[:7]].append(e)
            n += 1
    out = os.path.join(DOCS, SID, "index")
    if os.path.isdir(out):
        for fn in os.listdir(out):
            if fn.endswith(".json"):
                os.remove(os.path.join(out, fn))
    for m, items in shards.items():
        items.sort(key=lambda x: x["publishedDate"], reverse=True)
        w(os.path.join(out, f"{m}.json"), {"source": SID, "month": m, "count": len(items), "tenders": items})
    months = sorted(shards.keys(), reverse=True)
    latest = sorted((e for v in shards.values() for e in v), key=lambda x: x["publishedDate"], reverse=True)[:LATEST_LIMIT]
    w(os.path.join(out, "latest.json"), {"source": SID, "month": "latest", "count": len(latest), "months": months, "tenders": latest})
    w(os.path.join(DOCS, SID, "status.json"), {
        "label": "Central · CPPP archive (2012–18)", "engine": "import", "ok": True, "imported": True,
        "provenance": {"source": "Government Transparency Institute / DIGIWHIST — India CPPP extract",
                       "sourcePortal": "eprocure.gov.in", "license": "Open data — Government Transparency Institute (DIGIWHIST / opentender.eu)", "received": "2021-07-15"},
        "lastRun": "2021-07-15T00:00:00Z", "tenders": n, "months": months})
    big = max((os.path.getsize(os.path.join(out, f)) for f in os.listdir(out)), default=0)
    print(f"  [cppp_archive] {n:,} records -> {len(months)} month shard(s); largest index file {big/1e6:.1f} MB")

if __name__ == "__main__":
    main()
