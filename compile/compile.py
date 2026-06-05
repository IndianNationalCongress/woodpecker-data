#!/usr/bin/env python3
"""
Woodpecker compile / build step.

Reads the append-only ledger (releases/ + observations/ per source) and compiles
the serving layer into the R2 stand-in (serve/):

  serve/<source>/records/<ocid>.json   one compiled record per contracting process
  serve/<source>/index/<YYYY-MM>.json  monthly-sharded index (bounded at scale)
  serve/<source>/index/latest.json     default view (most-recent N across months)
  serve/sources.json                   manifest of sources + health (for the app)

A record carries TWO timelines:
  - the issuer's DECLARED timeline: ordered releases + a latest-wins compiledRelease;
  - Woodpecker's OBSERVED timeline: the ordered observations[] + an observedSummary
    (count, first/last seen, undeclared-change count, whether it was pulled).
The gap between them — a stateHash that moved with no release (silent edit), a
tender that 404'd (pull) — is surfaced as index flags for the app's list badges.

v1.0 record = ordered release list + latest-wins compiledRelease (no full OCDS
merge — documented simplification) + the observed timeline.

In cloud mode these writes target R2 via the S3 API; locally they hit serve/.
This is the build step, not a scraper — it reads across sources but never touches
a portal.
"""
import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Layout-agnostic: compile lives at <data-dir>/compile/compile.py in BOTH the dev
# monorepo (data/compile/...) and the deployed ledger repo (compile/... at root).
# parents[1] is the dir that holds the source dirs; ROOT (with fixtures/) holds serve/.
DATA_DIR = Path(__file__).resolve().parents[1]


def _find_root(start: Path) -> Path:
    for cand in [start, *start.parents]:
        if (cand / "fixtures").is_dir():
            return cand
    return start


ROOT = _find_root(DATA_DIR)
LATEST_LIMIT = 200  # default-view cap; month shards hold the full history


def load_releases(releases_dir):
    by_ocid = defaultdict(list)
    if not os.path.isdir(releases_dir):
        return by_ocid
    for ocid in sorted(os.listdir(releases_dir)):
        d = os.path.join(releases_dir, ocid)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".json"):
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    by_ocid[ocid].append(json.load(f))
    return by_ocid


def load_observations(observations_dir):
    """Observed timeline per ocid, ascending by observedAt (the capture-floor log)."""
    by_ocid = defaultdict(list)
    if not os.path.isdir(observations_dir):
        return by_ocid
    for ocid in sorted(os.listdir(observations_dir)):
        d = os.path.join(observations_dir, ocid)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".json"):
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    by_ocid[ocid].append(json.load(f))
    for ocid in by_ocid:
        by_ocid[ocid].sort(key=lambda o: o.get("observedAt", ""))
    return by_ocid


def observed_summary(obs_list):
    if not obs_list:
        return {"count": 0, "firstObserved": None, "lastObserved": None,
                "undeclaredChangeCount": 0, "pulled": False}
    return {
        "count": len(obs_list),
        "firstObserved": obs_list[0].get("observedAt"),
        "lastObserved": obs_list[-1].get("observedAt"),
        "undeclaredChangeCount": sum(1 for o in obs_list if o.get("undeclaredChange")),
        "pulled": obs_list[-1].get("availability") == "removed",
    }


def compile_record(ocid, releases, observations):
    releases = sorted(releases, key=lambda r: (r.get("date", ""), r.get("id", "")))

    snap = {"ocid": ocid, "tender": {}, "buyer": {}}
    docs_by_sha, all_tags, amendments = {}, [], []
    for r in releases:
        snap["date"] = r.get("date")
        snap["buyer"] = r.get("buyer", snap["buyer"])
        snap["tender"] = {**snap.get("tender", {}), **r.get("tender", {})}
        for tag in r.get("tag", []):
            if tag not in all_tags:
                all_tags.append(tag)
        for a in r.get("amendments", []):
            amendments.append(a)
        for d in r.get("documents", []):
            docs_by_sha[d["sha256"]] = d  # latest-wins on identical content
    snap["tag"] = ["compiled"]
    snap["documents"] = list(docs_by_sha.values())
    if amendments:
        snap["amendments"] = amendments

    return {
        "ocid": ocid,
        "releases": releases,
        "compiledRelease": snap,
        "amendments": amendments,
        "observations": observations,                 # OBSERVED timeline (ascending)
        "observedSummary": observed_summary(observations),
    }


def index_entry(source, record):
    releases = record["releases"]
    snap = record["compiledRelease"]
    tender = snap.get("tender", {})
    published = next((r for r in releases if "tender" in r.get("tag", [])), releases[0])

    all_docs = [d for r in releases for d in r.get("documents", [])]
    corrigenda = sum(1 for r in releases if "tenderAmendment" in r.get("tag", []))
    summary = record.get("observedSummary") or observed_summary(record.get("observations", []))

    return {
        "ocid": record["ocid"],
        "source": source,
        "title": tender.get("title", ""),
        "buyer": (snap.get("buyer") or {}).get("name", ""),
        "category": tender.get("mainProcurementCategory", ""),
        "value": tender.get("value", {}),
        "status": tender.get("status", ""),
        "publishedDate": published.get("date", ""),
        "closingDate": (tender.get("tenderPeriod") or {}).get("endDate", ""),
        "latestTag": (releases[-1].get("tag") or [""])[0],
        "lastReleaseDate": releases[-1].get("date", ""),
        "releaseCount": len(releases),
        "corrigendumCount": corrigenda,
        "hasDeadLink": any(d.get("sourceAlive") is False and d.get("url") for d in all_docs),
        "hasOcr": any(d.get("extractionMethod") == "ocr" for d in all_docs),
        # observed-timeline flags -> list badges (the transparency signal)
        "hasUndeclaredChange": summary["undeclaredChangeCount"] > 0,
        "pulled": summary["pulled"],
        "observationCount": summary["count"],
    }


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def compile_source(source, data_dir, serve_dir):
    releases_dir = os.path.join(data_dir, source, "releases")
    observations_dir = os.path.join(data_dir, source, "observations")
    by_ocid = load_releases(releases_dir)
    obs_by_ocid = load_observations(observations_dir)
    entries = []
    for ocid, releases in by_ocid.items():
        record = compile_record(ocid, releases, obs_by_ocid.get(ocid, []))
        write_json(os.path.join(serve_dir, source, "records", f"{ocid}.json"), record)
        entries.append(index_entry(source, record))

    # monthly shards keyed by publish month
    shards = defaultdict(list)
    for e in entries:
        month = (e["publishedDate"] or "0000-00")[:7]
        shards[month].append(e)
    for month, items in shards.items():
        items.sort(key=lambda e: e["lastReleaseDate"], reverse=True)
        write_json(os.path.join(serve_dir, source, "index", f"{month}.json"),
                   {"source": source, "month": month, "count": len(items), "tenders": items})

    latest = sorted(entries, key=lambda e: e["lastReleaseDate"], reverse=True)[:LATEST_LIMIT]
    write_json(os.path.join(serve_dir, source, "index", "latest.json"),
               {"source": source, "month": "latest", "count": len(latest),
                "months": sorted(shards.keys(), reverse=True), "tenders": latest})

    # surface the scraper's status.json health into the manifest
    status_path = os.path.join(serve_dir, source, "status.json")
    status = {}
    if os.path.exists(status_path):
        status = json.load(open(status_path, encoding="utf-8"))
    obs_total = sum(len(o) for o in obs_by_ocid.values())
    undeclared = sum(1 for e in entries if e["hasUndeclaredChange"])
    pulled = sum(1 for e in entries if e["pulled"])
    print(f"  [{source}] {len(entries)} records, {len(shards)} month shard(s), "
          f"{obs_total} observations ({undeclared} silent-edit, {pulled} pulled), "
          f"health={'ok' if status.get('ok') else 'FAILING'}")
    return {
        "id": source,
        "label": status.get("label", source),
        "engine": status.get("engine", ""),
        "ok": status.get("ok", True),
        "lastRun": status.get("lastRun"),
        "tenders": len(entries),
        "observations": obs_total,
        "undeclaredChanges": undeclared,
        "pulled": pulled,
        "months": sorted(shards.keys(), reverse=True),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DATA_DIR))
    ap.add_argument("--serve", default=str(ROOT / "serve"))
    ap.add_argument("--sources", nargs="*",
                    help="source ids to compile (default: autodiscover)")
    args = ap.parse_args()

    if args.sources:
        sources = args.sources
    else:
        sources = sorted(
            d for d in os.listdir(args.data)
            if os.path.isdir(os.path.join(args.data, d, "releases"))
        )

    manifest = []
    print(f"Compiling {len(sources)} source(s): {', '.join(sources)}")
    for s in sources:
        manifest.append(compile_source(s, args.data, args.serve))

    write_json(os.path.join(args.serve, "sources.json"),
               {"sources": manifest,
                "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    print("Compile complete -> serve/")


if __name__ == "__main__":
    main()
