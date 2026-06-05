#!/usr/bin/env python3
"""
CPPP (Central Public Procurement Portal) source-module scraper.

Engine: GePNIC (NIC). v1.0 runs in FIXTURE MODE against fixtures/cppp/raw.json
(the brief: fixtures are the spine, no unbounded live scraping). A live mode that
hits eprocure.gov.in is the tomorrow-step; the transform below is what both feed.

Per run, per tender:
  - emit OCDS release(s) for each DECLARED lifecycle event (publish/corr/award...);
  - write an OBSERVATION + content-addressed snapshot for every fetch (the OBSERVED
    timeline) — including re-polls that aren't tied to a declared event;
  - content-address + OCR each artifact into the R2 stand-in;
  - append release + observation JSON to the ledger (never rewrite); write status.json.

The observation layer is the v1.0 capture floor: a silent edit (stateHash moved with
no corrigendum) and a pull (availability:removed) are only knowable if every fetch is
captured. Diff UI is v1.1; the diff DATA is captured here.

SELF-CONTAINED (Independence Principle): stdlib + the vendored ocds.py beside it.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ocds  # vendored helper
import live  # GePNIC live fetcher (used only in --live mode)

SOURCE = "cppp"
LABEL = "Central (CPPP)"

# Layout-agnostic paths: this scraper lives at <source>/scraper/scrape.py in BOTH
# the dev monorepo (data/cppp/...) and the deployed ledger repo (cppp/... at root).
# The <source>/ dir holds releases/ + observations/; ROOT (the dir with fixtures/)
# holds fixtures/ + serve/. Resolving both relative to __file__ means zero path
# edits when the data half splits into NakliTechie/woodpecker-data.
SRC_DIR = Path(__file__).resolve().parents[1]            # the <source>/ dir


def _find_root(start: Path) -> Path:
    for cand in [start, *start.parents]:
        if (cand / "fixtures").is_dir():
            return cand
    return start


ROOT = _find_root(SRC_DIR)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


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
    """The subset of tender state that defines a stateHash (what a re-poll compares)."""
    return {
        "title": tender.get("title"),
        "status": tender.get("status"),
        "value": tender.get("value"),
        "tenderPeriod": tender.get("tenderPeriod"),
    }


def parse_tender(t, serve_files, base_url):
    """GePNIC raw tender -> (ordered OCDS releases, ordered observations)."""
    org = t["org"]
    tid = t["tender_id"]
    ocid = ocds.ocid_for(SOURCE, tid)
    pub = next(e for e in t["events"] if e["type"] == "published")
    title, category = pub["title"], pub.get("category")
    last_value = pub.get("value")
    last_closing = pub.get("closing")

    releases, fetches, seq = [], [], 0
    cum_docs = []          # cumulative [{id, sha256}] across the lifecycle (for stateHash)
    cur_tender = None

    for e in t["events"]:
        seq += 1
        etype = e["type"]
        last_value = e.get("value", last_value)
        last_closing = e.get("closing", last_closing)
        status = ocds.STATUS_FOR_EVENT.get(etype, "active")

        documents = []
        for i, d in enumerate(e.get("docs", []), start=1):
            src = os.path.join(ROOT, "fixtures", SOURCE, "files", d["file"])
            source_url = f"{base_url}/DownloadFile?fileId={tid}-{d['file']}"
            doc = ocds.store_document(
                serve_files,
                src_path=src,
                doc_id=f"{ocid}-{seq:04d}-{i}",
                document_type=d.get("type", "tenderNotice"),
                title=d["name"],
                source_url=source_url,
                source_alive=d.get("alive", True),
            )
            documents.append(doc)
            cum_docs.append({"id": doc["id"], "sha256": doc["sha256"]})

        tender = build_tender(tid, title, org, category, status, last_value, last_closing)
        amendment = None
        if etype == "corrigendum":
            amendment = {
                "id": str(e.get("seq", seq)),
                "date": ocds.iso(e["date"]),
                "rationale": e.get("summary", ""),
            }
            tender["description"] = e.get("summary", "")
        elif etype in ("bidOpening", "award"):
            tender["description"] = e.get("summary", "")
        if etype == "award" and e.get("supplier"):
            tender["award"] = {"suppliers": [{"name": e["supplier"]}],
                               "value": tender.get("value")}

        releases.append(ocds.build_release(
            source=SOURCE, tender_id=tid, seq=seq, event_type=etype,
            date=e["date"], buyer={"name": org, "id": f"IN-ORG-{slug(org)[:40]}"},
            tender=tender, documents=documents, amendment=amendment,
        ))

        cur_tender = tender
        # a declared event = an observation that coincides with a release
        fetches.append({
            "observedAt": e["date"], "declared": True,
            "state": _observable(tender),
            "doc_hashes": list(cum_docs),
            "raw": {"event": etype, "date": e["date"], "tender": tender,
                    "documents": [{"id": d["id"], "sha256": d["sha256"], "title": d["title"]}
                                  for d in documents]},
        })

    # extra observations: re-polls NOT tied to a declared event (silent edits / pulls)
    base_state = _observable(cur_tender) if cur_tender else {}
    for obs in t.get("observations", []):
        if obs.get("removed"):
            fetches.append({"observedAt": obs["observedAt"], "removed": True})
            continue
        patched = dict(base_state)
        patched.update(obs.get("statePatch", {}))
        fetches.append({
            "observedAt": obs["observedAt"], "declared": False, "state": patched,
            "doc_hashes": list(cum_docs),
            "raw": {"event": "re-poll", "observedAt": obs["observedAt"],
                    "state": patched, "note": obs.get("note", "")},
        })
        base_state = patched   # later observations see the edited state

    observations = ocds.build_observation_sequence(
        source=SOURCE, ocid=ocid, fetches=fetches, serve_files_dir=serve_files)
    return releases, observations


# --------------------------------------------------------------------------- #
# LIVE mode: a real GePNIC tender (from live.fetch_recent_tenders) -> the SAME
# OCDS + observation contract as fixture mode. Only the SOURCE of the tender
# differs (real portal vs raw.json); the mapping below mirrors parse_tender for
# a single observed 'published' lifecycle point.
# --------------------------------------------------------------------------- #
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _live_date(s: str) -> str:
    """GePNIC 'DD-Mon-YYYY HH:MM AM/PM' -> 'YYYY-MM-DD' (date only; ocds.iso adds T00:00:00Z)."""
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
    """'2,49,968' (Indian grouping) / '57,35,090.00' -> int rupees, or None."""
    if not s:
        return None
    digits = re.sub(r"[^0-9.]", "", s)
    if not digits:
        return None
    try:
        return int(round(float(digits)))
    except ValueError:
        return None


def _live_clean_text(lt):
    """Readable plain text from the parsed detail fields + document list — the
    'plain text we keep ourselves', clean (not raw HTML markup) and searchable."""
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


def parse_live_tender(lt, serve_files, base_url):
    """One live GePNIC tender dict -> (ordered OCDS releases, observations).

    We observe a single 'published' lifecycle point (these are active tenders).
    DOCUMENT NOTE: the real PDF bytes on GePNIC are not GET-fetchable (a stateful
    Tapestry listener, no per-file URL — see live.py). So the content-addressed
    artifact we store is the real, publicly-fetched DETAIL-PAGE HTML; each portal
    document is recorded with its real title + portal URL but sourceAlive=False
    (binary gated). That keeps the capture greedy on what's public and honest
    about what isn't.
    """
    f = lt.get("fields", {})
    tid = (f.get("Tender ID") or lt.get("tender_id") or "").strip()
    org = (f.get("Organisation Chain") or "Unknown Organisation").strip()
    title = (f.get("Work Description") or f.get("Title") or lt.get("title") or tid).strip()
    # GePNIC 'Tender Category' is Works/Goods/Services (OCDS mainProcurementCategory)
    category = f.get("Tender Category") or f.get("Product Category") or ""
    value = _live_amount(f.get("Tender Value in ₹") or f.get("Tender Value in ₹") or "")
    closing = _live_date(f.get("Bid Submission End Date") or lt.get("closing") or "")
    published = _live_date(f.get("Published Date") or lt.get("published") or "") or closing

    ocid = ocds.ocid_for(SOURCE, tid)

    # --- store the CLEAN extracted text (not raw HTML) as this fetch's artifact,
    #     and best-effort seed a free Internet Archive snapshot of the page ---
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
                source_alive=True,           # the detail PAGE is live + fetched
            )
            if wayback:
                doc["waybackUrl"] = wayback   # free Internet Archive snapshot of the page
            documents.append(doc)
        finally:
            os.unlink(tmp_path)

    # --- record each portal-listed PDF (binary gated => sourceAlive False) ---
    for i, name in enumerate(lt.get("documents", []), start=1):
        documents.append({
            "id": f"{ocid}-0001-doc-{i}",
            "documentType": "biddingDocuments",
            "title": name,
            "url": None,                     # binary not retrievable by GET (gated)
            "sourceUrl": lt.get("detail_url", base_url),
            "sourceAlive": False,            # PDF is behind a stateful listener
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
        source=SOURCE, tender_id=tid, seq=1, event_type="published",
        date=published, buyer={"name": org, "id": f"IN-ORG-{slug(org)[:40]}"},
        tender=tender, documents=documents, amendment=None,
    )

    # one fetch -> one observation: declared True, availability present
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
        source=SOURCE, ocid=ocid, fetches=fetches, serve_files_dir=serve_files)
    return [rel], observations


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(ROOT / "fixtures" / SOURCE / "raw.json"))
    ap.add_argument("--releases", default=str(SRC_DIR / "releases"))
    ap.add_argument("--observations", default=str(SRC_DIR / "observations"))
    ap.add_argument("--serve", default=str(ROOT / "serve"))
    ap.add_argument("--live", action="store_true",
                    help="Pull a small, bounded set of REAL recent tenders from the "
                         "public CPPP/GePNIC browse-by-date listing (GET-only, no "
                         "captcha, no POST). Default off = fixture mode.")
    ap.add_argument("--max", type=int, default=3,
                    help="Max tenders to pull in --live mode (default 3, hard cap 10).")
    args = ap.parse_args()

    base_url = "https://eprocure.gov.in/eprocure/app"
    serve_files = os.path.join(args.serve, "files")
    serve_source = os.path.join(args.serve, SOURCE)

    written, obs_written, tenders = 0, 0, 0
    try:
        if args.live:
            # --- LIVE: real portal is the SOURCE; same OCDS/observation transform ---
            try:
                live_tenders = live.fetch_recent_tenders(args.max)
            except live.LiveBlocked as blk:
                # We hit a gate we won't circumvent. Leave fixture data intact,
                # record the blocker, and exit cleanly (not a crash).
                msg = f"live mode blocked (no circumvention attempted): {blk}"
                print(f"[{SOURCE}] {msg}", file=sys.stderr)
                ocds.write_status(serve_source, {
                    "source": SOURCE, "label": LABEL, "engine": "gepnic",
                    "ok": False, "mode": "live", "lastRun": ocds.now_iso(),
                    "tendersCaptured": 0, "releasesWritten": 0,
                    "observationsWritten": 0, "lastError": msg,
                })
                return
            for lt in live_tenders:
                tenders += 1
                releases, observations = parse_live_tender(lt, serve_files, base_url)
                for rel in releases:
                    _, did_write = ocds.write_release(args.releases, rel)
                    written += did_write
                for obs in observations:
                    _, did_write = ocds.write_observation(args.observations, obs)
                    obs_written += did_write
        else:
            # --- FIXTURE (default, unchanged): raw.json is the SOURCE ---
            raw = json.load(open(args.raw, encoding="utf-8"))
            base_url = raw.get("portalBase", base_url)
            for t in raw["tenders"]:
                tenders += 1
                releases, observations = parse_tender(t, serve_files, base_url)
                for rel in releases:
                    _, did_write = ocds.write_release(args.releases, rel)
                    written += did_write
                for obs in observations:
                    _, did_write = ocds.write_observation(args.observations, obs)
                    obs_written += did_write
        status = {
            "source": SOURCE, "label": LABEL, "engine": "gepnic",
            "ok": True, "mode": "live" if args.live else "fixture",
            "lastRun": ocds.now_iso(),
            "tendersCaptured": tenders, "releasesWritten": written,
            "observationsWritten": obs_written,
            "lastError": None,
        }
        ocds.write_status(serve_source, status)
        print(f"[{SOURCE}] {tenders} tenders, {written} new releases, "
              f"{obs_written} new observations.")
    except Exception as exc:  # noqa: BLE001 - a scraper crash must not be silent
        ocds.write_status(serve_source, {
            "source": SOURCE, "label": LABEL, "engine": "gepnic",
            "ok": False, "mode": "live" if args.live else "fixture",
            "lastRun": ocds.now_iso(),
            "tendersCaptured": tenders, "releasesWritten": written,
            "observationsWritten": obs_written,
            "lastError": f"{type(exc).__name__}: {exc}",
        })
        raise


if __name__ == "__main__":
    main()
