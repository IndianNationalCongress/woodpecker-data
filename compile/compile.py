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
    }, entries


def build_stats(entries, manifest, generated):
    """Corpus-wide aggregates for the app's stats dashboard — precomputed so the app
    fetches ONE stats.json instead of all per-source indices. Scales with the corpus."""
    cat = defaultdict(lambda: {"count": 0, "value": 0})
    src = {m["id"]: {"id": m["id"], "label": m["label"], "count": 0, "value": 0} for m in manifest}
    band = {"lt1cr": 0, "1to10cr": 0, "gt10cr": 0, "none": 0}
    status, month, year = defaultdict(int), defaultdict(int), defaultdict(int)
    total_value = with_value = undeclared = pulled = dead = 0
    # per-year buckets: count/value + breakdowns by category, value-band and source, so the
    # app's Stats scrubber can re-aggregate ANY [from,to] window client-side from one stats.json
    # (sum the selected years) — no need to load all tenders. cat/src carry [count, value].
    ybk = {}
    for e in entries:
        amt = (e.get("value") or {}).get("amount")
        c = (e.get("category") or "").strip().lower() or "other"
        cat[c]["count"] += 1
        s = src.get(e["source"])
        if s:
            s["count"] += 1
        status[(e.get("status") or "other")] += 1
        m = (e.get("publishedDate") or "")[:7]
        yr = m[:4] if len(m) == 7 else None
        if yr:
            month[m] += 1
            year[yr] += 1
        bandkey = ("none" if not amt
                   else "lt1cr" if amt < 1e7
                   else "1to10cr" if amt < 1e8 else "gt10cr")
        if amt:
            total_value += amt
            with_value += 1
            cat[c]["value"] += amt
            if s:
                s["value"] += amt
        band[bandkey] += 1
        if yr:
            b = ybk.get(yr)
            if b is None:
                b = ybk[yr] = {"count": 0, "value": 0,
                               "cat": defaultdict(lambda: [0, 0]),
                               "band": defaultdict(int),
                               "src": defaultdict(lambda: [0, 0])}
            b["count"] += 1
            b["band"][bandkey] += 1
            bc = b["cat"][c]; bc[0] += 1
            bs = b["src"][e["source"]]; bs[0] += 1
            if amt:
                b["value"] += amt; bc[1] += amt; bs[1] += amt
        undeclared += 1 if e.get("hasUndeclaredChange") else 0
        pulled += 1 if e.get("pulled") else 0
        dead += 1 if e.get("hasDeadLink") else 0
    pdates = sorted(d for d in (e.get("publishedDate") for e in entries) if d)
    return {
        "generated": generated,
        "totalTenders": len(entries),
        "dateRange": {"from": pdates[0] if pdates else None,
                      "to": pdates[-1] if pdates else None},
        "totalValue": total_value,
        "withValue": with_value,
        "sources": len(manifest),
        "byCategory": dict(cat),
        "bySource": sorted(src.values(), key=lambda x: -x["count"]),
        "byValueBand": band,
        "byStatus": dict(status),
        "byMonth": dict(sorted(month.items())),
        "byYear": dict(sorted(year.items())),
        "yearBuckets": {
            y: {"count": b["count"], "value": b["value"],
                "cat": {k: v for k, v in b["cat"].items()},
                "band": dict(b["band"]),
                "src": {k: v for k, v in b["src"].items()}}
            for y, b in sorted(ybk.items())
        },
        "observed": {"undeclaredChanges": undeclared, "pulled": pulled, "deadLinks": dead},
        "imported": {
            "count": sum(m.get("tenders", 0) for m in manifest if m.get("imported")),
            "sources": [{"id": m["id"], "label": m["label"], "tenders": m.get("tenders", 0)}
                        for m in manifest if m.get("imported")],
        },
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
        have = {d for d in os.listdir(args.data)
                if os.path.isdir(os.path.join(args.data, d, "releases"))}
        # also include WORKING-but-empty sources: they have a served status.json but no
        # releases yet (so a pill still shows; they fill in as the daily cron captures).
        if os.path.isdir(args.serve):
            have |= {d for d in os.listdir(args.serve)
                     if os.path.exists(os.path.join(args.serve, d, "status.json"))}
        sources = sorted(have)

    manifest, all_entries = [], []
    print(f"Compiling {len(sources)} source(s): {', '.join(sources)}")
    for s in sources:
        sp = os.path.join(args.serve, s, "status.json")
        st = json.load(open(sp, encoding="utf-8")) if os.path.exists(sp) else {}
        if st.get("engine") == "import":
            # Pre-built bulk import (e.g. Assam/Himachal OCDS). DON'T recompile (would clobber it).
            # Surface it as a source/pill AND fold its entries into the portal totals + stats
            # (imports ARE counted) — but they stay marked declared/imported, NOT observed: they
            # carry no capture-floor, so they add 0 to the silent-edit / pull tallies.
            idx = os.path.join(args.serve, s, "index")
            months, ents = [], []
            if os.path.isdir(idx):
                for fn in os.listdir(idx):
                    if fn.endswith(".json") and fn != "latest.json":
                        months.append(fn[:-5])
                        ents.extend(json.load(open(os.path.join(idx, fn), encoding="utf-8")).get("tenders", []))
            months.sort(reverse=True)
            manifest.append({
                "id": s, "label": st.get("label", s), "engine": "import",
                "ok": st.get("ok", True), "lastRun": st.get("lastRun"),
                "tenders": st.get("tenders", 0), "observations": 0,
                "undeclaredChanges": 0, "pulled": 0, "imported": True,
                "provenance": st.get("provenance"), "months": months,
            })
            all_entries.extend(ents)
            print(f"  [{s}] imported source: {st.get('tenders', 0)} tenders (folded into totals; declared, not observed)")
            continue
        me, ents = compile_source(s, args.data, args.serve)
        manifest.append(me)
        all_entries.extend(ents)

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_json(os.path.join(args.serve, "sources.json"),
               {"sources": manifest, "generated": generated})
    write_json(os.path.join(args.serve, "stats.json"),
               build_stats(all_entries, manifest, generated))
    print(f"Compile complete -> serve/  ({len(all_entries)} tenders; stats.json written)")


if __name__ == "__main__":
    main()
