#!/usr/bin/env python3
"""
Shared GePNIC LIVE engine — runs ONE GePNIC source per invocation, emitting the same
OCDS + observation contract used everywhere. Source-agnostic: the only per-portal
inputs are --source, --label, --base-url. Width across states/orgs = a registry of
these (portals.json), driven by the CI workflow — NOT N copy-pasted scrapers.

Independence at runtime: a blocked or failed source writes a failing status.json and
exits cleanly (exit 0) so it NEVER aborts a sibling source's run.

Fixture mode lives elsewhere (cppp/scraper/scrape.py is the deterministic test spine).
THIS engine only does bounded, polite, GET-only live capture (see live.py SAFETY).
"""
import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ocds   # vendored helper (shared, source-agnostic)
import live   # generalized GePNIC live fetcher

ENGINE_DIR = Path(__file__).resolve().parent      # _gepnic/
DATA_ROOT = ENGINE_DIR.parent                      # repo root (ledger) / data/ (monorepo)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _live_date(s: str) -> str:
    """GePNIC 'DD-Mon-YYYY HH:MM AM/PM' -> 'YYYY-MM-DD'."""
    if not s:
        return ""
    m = re.match(r"(\d{2})-([A-Za-z]{3})-(\d{4})", s)
    if not m:
        return ""
    day, mon, year = m.group(1), m.group(2).title(), m.group(3)
    if mon not in _MONTHS:
        return ""
    return f"{year}-{_MONTHS[mon]:02d}-{int(day):02d}"


def _live_amount(s: str):
    """'2,49,968' / '57,35,090.00' (Indian grouping) -> int rupees, or None."""
    if not s:
        return None
    digits = re.sub(r"[^0-9.]", "", s)
    if not digits:
        return None
    try:
        return int(round(float(digits)))
    except ValueError:
        return None


def _live_clean_text(lt) -> str:
    """Readable plain text from the parsed detail fields + document list."""
    f = lt.get("fields", {})
    lines = [(lt.get("title") or "").strip(), ""]
    for cap, val in f.items():
        if val:
            lines.append(f"{cap}: {val}")
    docs = lt.get("documents", [])
    if docs:
        lines += ["", "Documents listed on the portal:"]
        lines += [f"  - {d}" for d in docs]
    return "\n".join(l for l in lines if l is not None).strip() + "\n"


def build_tender(tender_id, title, org, category, status, value, closing):
    t = {
        "id": tender_id,
        "title": title,
        "status": status,
        "procuringEntity": {"name": org, "id": f"IN-ORG-{slug(org)[:40]}"},
    }
    if category:
        t["mainProcurementCategory"] = category.lower()
    if value:
        t["value"] = {"amount": value, "currency": "INR"}
    if closing:
        t["tenderPeriod"] = {"endDate": ocds.iso(closing)}
    return t


def _observable(tender: dict) -> dict:
    return {
        "title": tender.get("title"),
        "status": tender.get("status"),
        "value": tender.get("value"),
        "tenderPeriod": tender.get("tenderPeriod"),
    }


def parse_live_tender(lt, serve_files, base_url, source):
    """One live GePNIC tender dict -> (ordered OCDS releases, observations) for `source`.

    Document bytes are not GET-fetchable on GePNIC (stateful Tapestry listener). So the
    content-addressed artifact is the real, publicly-fetched CLEAN detail-page TEXT;
    each portal PDF is recorded with title + URL but sourceAlive=False (binary gated)."""
    f = lt.get("fields", {})
    tid = (f.get("Tender ID") or lt.get("tender_id") or "").strip()
    org = (f.get("Organisation Chain") or "Unknown Organisation").strip()
    title = (f.get("Work Description") or f.get("Title") or lt.get("title") or tid).strip()
    category = f.get("Tender Category") or f.get("Product Category") or ""
    value = _live_amount(f.get("Tender Value in ₹") or f.get("Tender Value") or "")
    closing = _live_date(f.get("Bid Submission End Date") or lt.get("closing") or "")
    published = _live_date(f.get("Published Date") or lt.get("published") or "") or closing

    ocid = ocds.ocid_for(source, tid)

    documents = []
    clean_text = _live_clean_text(lt)
    wayback = live.wayback_save(lt.get("detail_url", "")) if lt.get("detail_url") else None
    if clean_text:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(clean_text)
            tmp_path = tmp.name
        try:
            doc = ocds.store_document(
                serve_files,
                src_path=tmp_path,
                doc_id=f"{ocid}-0001-record",
                document_type="tenderNotice",
                title=f"Captured tender record (text) — {tid}",
                source_url=lt.get("detail_url", base_url),
                source_alive=True,
            )
            if wayback:
                doc["waybackUrl"] = wayback
            documents.append(doc)
        finally:
            os.unlink(tmp_path)

    for i, name in enumerate(lt.get("documents", []), start=1):
        documents.append({
            "id": f"{ocid}-0001-doc-{i}",
            "documentType": "biddingDocuments",
            "title": name,
            "url": None,
            "sourceUrl": lt.get("detail_url", base_url),
            "sourceAlive": False,
            "sha256": None,
            "format": "application/pdf",
            "textUrl": None,
            "extractionMethod": "none",
        })

    cum_docs = [{"id": d["id"], "sha256": d["sha256"]}
                for d in documents if d.get("sha256")]

    tender = build_tender(tid, title, org, category, "active", value, closing)
    tender["description"] = title

    rel = ocds.build_release(
        source=source, tender_id=tid, seq=1, event_type="published",
        date=published, buyer={"name": org, "id": f"IN-ORG-{slug(org)[:40]}"},
        tender=tender, documents=documents, amendment=None,
    )

    fetches = [{
        "observedAt": ocds.now_iso(), "declared": True,
        "state": _observable(tender),
        "doc_hashes": list(cum_docs),
        "raw": {"event": "published", "source": "live", "tender": tender,
                "portalTenderId": tid, "reference": f.get("Tender Reference Number", ""),
                "documents": [{"id": d["id"], "title": d["title"],
                               "sourceAlive": d["sourceAlive"]} for d in documents]},
    }]
    observations = ocds.build_observation_sequence(
        source=source, ocid=ocid, fetches=fetches, serve_files_dir=serve_files)
    return [rel], observations


def main():
    ap = argparse.ArgumentParser(description="Shared GePNIC live engine (one source/run).")
    ap.add_argument("--source", required=True, help="source id (e.g. cppp, maharashtra)")
    ap.add_argument("--label", default=None, help="human label (default: source id)")
    ap.add_argument("--base-url", required=True, help="GePNIC app base URL for this portal")
    ap.add_argument("--max", type=int, default=10, help="max tenders (hard cap 60)")
    ap.add_argument("--max-pages", type=int, default=8, help="max listing pages walked")
    ap.add_argument("--serve", default=str(DATA_ROOT / "serve"))
    ap.add_argument("--releases", default=None)
    ap.add_argument("--observations", default=None)
    args = ap.parse_args()

    source = args.source
    label = args.label or source
    base_url = args.base_url
    releases_dir = args.releases or str(DATA_ROOT / source / "releases")
    observations_dir = args.observations or str(DATA_ROOT / source / "observations")
    serve_files = os.path.join(args.serve, "files")
    serve_source = os.path.join(args.serve, source)

    def _status(ok, tenders, written, obs_written, err):
        ocds.write_status(serve_source, {
            "source": source, "label": label, "engine": "gepnic",
            "ok": ok, "mode": "live", "lastRun": ocds.now_iso(),
            "tendersCaptured": tenders, "releasesWritten": written,
            "observationsWritten": obs_written, "lastError": err,
        })

    # incremental: skip detail-fetching tenders already in the archive, so a full-listing
    # walk only pays for NEW tenders (the daily cron can sweep the whole listing cheaply).
    existing = set()
    prefix = ocds.ocid_for(source, "")
    if os.path.isdir(releases_dir):
        for d in os.listdir(releases_dir):
            if d.startswith(prefix):
                existing.add(d[len(prefix):])

    tenders = written = obs_written = 0
    try:
        live_tenders = live.fetch_recent_tenders(
            args.max, base_url, max_pages=args.max_pages, tag=source, skip_tids=existing)
    except live.LiveBlocked as blk:
        msg = f"live mode blocked (no circumvention attempted): {blk}"
        print(f"[{source}] {msg}", file=sys.stderr)
        _status(False, 0, 0, 0, msg)
        return  # exit 0 — independence: a blocked source must not abort siblings
    except Exception as exc:  # noqa: BLE001 — network/parse failure for THIS source only
        msg = f"{type(exc).__name__}: {exc}"
        print(f"[{source}] live fetch failed: {msg}", file=sys.stderr)
        _status(False, 0, 0, 0, msg)
        return

    for lt in live_tenders:
        tenders += 1
        try:
            releases, observations = parse_live_tender(lt, serve_files, base_url, source)
        except Exception as exc:  # noqa: BLE001 — one bad tender must not drop the batch
            print(f"[{source}] parse failed for {lt.get('tender_id')}: {exc}", file=sys.stderr)
            continue
        for rel in releases:
            _, did = ocds.write_release(releases_dir, rel)
            written += did
        for obs in observations:
            _, did = ocds.write_observation(observations_dir, obs)
            obs_written += did

    _status(True, tenders, written, obs_written, None)
    print(f"[{source}] {tenders} tenders, {written} new releases, "
          f"{obs_written} new observations.")


if __name__ == "__main__":
    main()
