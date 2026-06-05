#!/usr/bin/env python3
"""
Bulk OCDS import: Assam historical procurement (FY2016-21) -> a browsable archive source.

This is the "official bulk -> our pipeline" route for multi-year history that the live
GePNIC listing can't reach (it gates deep history behind a captcha). The source data is
the Assam Finance Department's OCDS export, published on data.gov.in / the Open Contracting
registry (GODL-India), mirrored by CivicDataLab. assamtenders.gov.in is itself a GePNIC
portal, so the schema maps cleanly to ours.

PROVENANCE, kept honest: this is DECLARED data (an official export), NOT observed by
Woodpecker — there are no capture-floor snapshots/stateHashes (we didn't fetch the pages).
So it's a SEPARATE source `assam_archive`, marked `engine:import`, and the app renders it
apart from the live-observed capture. It is intentionally NOT folded into the live-corpus
stats headline or the semantic index — it's the historical archive you can browse.

Storage is COMPACT: per-year index shards + latest.json + status.json (no 30k per-tender
record files). The app renders an imported tender's detail straight from its index entry.
"""
import argparse
import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SOURCE = "assam_archive"
LABEL = "Assam · archive (2016–21)"
SCHEME = "woodpecker"
PROVENANCE = {
    "imported": True,
    "source": "Assam Finance Dept OCDS export — data.gov.in / Open Contracting registry (CivicDataLab mirror)",
    "sourcePortal": "assamtenders.gov.in (GePNIC)",
    "license": "GODL-India (Government Open Data License – India)",
    "note": "Declared official export — NOT observed by Woodpecker (no capture-floor snapshots).",
}
_MON = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _date(s):
    m = re.match(r"(\d{2})-([A-Za-z]{3})-(\d{4})", (s or "").strip())
    if not m:
        return ""
    mon = _MON.get(m.group(2).title())
    if not mon:
        return ""
    return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}T00:00:00Z"


def _amount(s):
    d = re.sub(r"[^0-9.]", "", s or "")
    if not d:
        return None
    try:
        return int(round(float(d)))
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="/tmp/assam_ocds.csv")
    ap.add_argument("--serve", default="./docs")
    args = ap.parse_args()

    out = os.path.join(args.serve, SOURCE)
    os.makedirs(os.path.join(out, "index"), exist_ok=True)
    imported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    entries, shards, seen = [], {}, set()
    with open(args.csv, encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            tid = (r.get("tender/id") or "").strip()
            if not tid or tid in seen:
                continue
            seen.add(tid)
            pub = _date(r.get("tender/datePublished"))
            val = _amount(r.get("tender/value/amount"))
            e = {
                "ocid": f"{SCHEME}-{SOURCE}-{tid}",
                "source": SOURCE,
                "title": (r.get("tender/title") or tid).strip(),
                "buyer": (r.get("buyer/name") or "").strip(),
                "category": (r.get("tender/mainProcurementCategory") or "").strip().lower(),
                "value": ({"amount": val, "currency": "INR"} if val else {}),
                "status": (r.get("tender/status") or "").strip().lower() or "active",
                "publishedDate": pub,
                "closingDate": "",
                "latestTag": "tender",
                "lastReleaseDate": pub,
                "releaseCount": 1,
                "corrigendumCount": 0,
                "hasDeadLink": False,
                "hasOcr": False,
                "hasUndeclaredChange": False,
                "pulled": False,
                "observationCount": 0,
                "imported": True,
                "fiscalYear": (r.get("fiscal_year") or "").strip(),
                "reference": (r.get("tender/externalReference") or "").strip(),
                "procurementMethod": (r.get("tender/procurementMethod") or "").strip(),
            }
            entries.append(e)
            yr = (pub[:4] or e["fiscalYear"] or "unknown")
            shards.setdefault(yr, []).append(e)

    def wj(path, obj):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)

    for yr, items in shards.items():
        items.sort(key=lambda e: e["publishedDate"], reverse=True)
        wj(os.path.join(out, "index", f"{yr}.json"),
           {"source": SOURCE, "month": yr, "count": len(items), "tenders": items})

    latest = sorted(entries, key=lambda e: e["publishedDate"], reverse=True)[:200]
    wj(os.path.join(out, "index", "latest.json"),
       {"source": SOURCE, "month": "latest", "count": len(latest),
        "months": sorted(shards.keys(), reverse=True), "tenders": latest})

    wj(os.path.join(out, "status.json"),
       {"source": SOURCE, "label": LABEL, "engine": "import", "ok": True,
        "mode": "import", "lastRun": imported_at, "importedAt": imported_at,
        "tenders": len(entries), "tendersCaptured": len(entries),
        "provenance": PROVENANCE, "lastError": None})

    print(f"[{SOURCE}] imported {len(entries)} tenders across {len(shards)} year shard(s) "
          f"-> {out}  (years: {', '.join(sorted(shards))})")


if __name__ == "__main__":
    main()
