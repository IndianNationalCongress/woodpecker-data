#!/usr/bin/env python3
"""
Gujarat (nProcure / (n)Code Solutions) source-module scraper.

Engine: nProcure — NOT GePNIC. There is no upstream OCDS client for nProcure, so
this is the net-new scraper that proves the source-module abstraction across
engines. Its raw model (tenderList / stages / nProcure field names) is deliberately
different from the GePNIC sources; the OCDS output is identical in shape. That
mapping IS the portability test.

Per run, per tender:
  - emit OCDS release(s) for each DECLARED stage (publish/corrigendum/opening/award);
  - write an OBSERVATION + content-addressed snapshot for every fetch (the OBSERVED
    timeline) — including re-polls that aren't tied to a declared stage;
  - content-address + OCR each attachment into the R2 stand-in;
  - append release + observation JSON to the ledger (never rewrite); write status.json.

The observation layer is the v1.0 capture floor: a silent edit (stateHash moved with
no corrigendum) and a pull (availability:removed) are only knowable if every fetch is
captured. Diff UI is v1.1; the diff DATA is captured here.

Failure behaviour (Independence Principle): nProcure needs a DSC/session handshake
(NIC-CA applet). When that handshake fails, new-tender discovery aborts — but any
tender already captured stays served from the ledger. This run simulates that:
the previously-captured tender is (idempotently) re-emitted — releases AND
observations, so the OBSERVED timeline keeps growing even in a degraded run — then
status is written ok:false with the handshake error, so the app shows
"Gujarat: scraper failing" WITHOUT taking CPPP/Rajasthan or the app down.

SELF-CONTAINED (Independence Principle): stdlib + the vendored ocds.py beside it.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ocds  # vendored helper

SOURCE = "gujarat"
LABEL = "Gujarat"

# Layout-agnostic paths: this scraper lives at <source>/scraper/scrape.py in BOTH
# the dev monorepo (data/gujarat/...) and the deployed ledger repo (gujarat/... at
# root). The <source>/ dir holds releases/ + observations/; ROOT (the dir with
# fixtures/) holds fixtures/ + serve/. Resolving both relative to __file__ means
# zero path edits when the data half splits into IndianNationalCongress/woodpecker-data.
SRC_DIR = Path(__file__).resolve().parents[1]            # the <source>/ dir


def _find_root(start: Path) -> Path:
    for cand in [start, *start.parents]:
        if (cand / "fixtures").is_dir():
            return cand
    return start


ROOT = _find_root(SRC_DIR)

# nProcure stage -> internal event type
STAGE_EVENT = {
    "PUBLISHED": "published",
    "CORRIGENDUM": "corrigendum",
    "TECHNICAL_OPENING": "bidOpening",
    "FINANCIAL_OPENING": "bidOpening",
    "AWARDED": "award",
}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _observable(tender: dict) -> dict:
    """The subset of tender state that defines a stateHash (what a re-poll compares)."""
    return {
        "title": tender.get("title"),
        "status": tender.get("status"),
        "value": tender.get("value"),
        "tenderPeriod": tender.get("tenderPeriod"),
    }


def parse_nprocure_tender(t, serve_files, base_url):
    """nProcure tenderList entry -> (ordered OCDS releases, ordered observations)."""
    org = t["department"]
    tid = t["nitId"]
    ocid = ocds.ocid_for(SOURCE, tid)
    pub = next(s for s in t["stages"] if s["stage"] == "PUBLISHED")
    title = pub["tenderTitle"]
    category = pub.get("workCategory")
    last_value = t.get("estimatedCost")
    last_closing = pub.get("bidEndDate")

    releases, fetches, seq = [], [], 0
    cum_docs = []          # cumulative [{id, sha256}] across the lifecycle (for stateHash)
    cur_tender = None

    for s in t["stages"]:
        seq += 1
        etype = STAGE_EVENT.get(s["stage"], "published")
        last_closing = s.get("bidEndDate", last_closing)
        status = ocds.STATUS_FOR_EVENT.get(etype, "active")

        documents = []
        for i, a in enumerate(s.get("attachments", []), start=1):
            src = os.path.join(ROOT, "fixtures", SOURCE, "files", a["fileName"])
            source_url = f"{base_url}/nprocure/downloadDoc?nit={tid}&f={a['fileName']}"
            doc = ocds.store_document(
                serve_files,
                src_path=src,
                doc_id=f"{ocid}-{seq:04d}-{i}",
                document_type=a.get("category", "tenderNotice"),
                title=a["label"],
                source_url=source_url,
                source_alive=a.get("available", True),
            )
            documents.append(doc)
            cum_docs.append({"id": doc["id"], "sha256": doc["sha256"]})

        tender = {
            "id": tid, "title": title, "status": status,
            "procuringEntity": {"name": org, "id": f"IN-ORG-{slug(org)[:40]}"},
        }
        if category:
            tender["mainProcurementCategory"] = category.lower()
        if last_value:
            tender["value"] = {"amount": last_value, "currency": "INR"}
        if last_closing:
            tender["tenderPeriod"] = {"endDate": ocds.iso(last_closing)}

        releases.append(ocds.build_release(
            source=SOURCE, tender_id=tid, seq=seq, event_type=etype,
            date=s["eventDate"], buyer={"name": org, "id": f"IN-ORG-{slug(org)[:40]}"},
            tender=tender, documents=documents,
        ))

        cur_tender = tender
        # a declared stage = an observation that coincides with a release
        fetches.append({
            "observedAt": s["eventDate"], "declared": True,
            "state": _observable(tender),
            "doc_hashes": list(cum_docs),
            "raw": {"event": etype, "date": s["eventDate"], "tender": tender,
                    "documents": [{"id": d["id"], "sha256": d["sha256"], "title": d["title"]}
                                  for d in documents]},
        })

    # extra observations: re-polls NOT tied to a declared stage (silent edits / pulls)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(ROOT / "fixtures" / SOURCE / "raw.json"))
    ap.add_argument("--releases", default=str(SRC_DIR / "releases"))
    ap.add_argument("--observations", default=str(SRC_DIR / "observations"))
    ap.add_argument("--serve", default=str(ROOT / "serve"))
    args = ap.parse_args()

    raw = json.load(open(args.raw, encoding="utf-8"))
    base_url = raw.get("portalBase", "https://www.nprocure.com")
    serve_files = os.path.join(args.serve, "files")
    serve_source = os.path.join(args.serve, SOURCE)

    written, obs_written, tenders = 0, 0, 0
    # Capture whatever is already discoverable (idempotent, append-only) — both the
    # DECLARED ledger (releases) and the OBSERVED ledger (observations).
    for t in raw.get("tenderList", []):
        tenders += 1
        releases, observations = parse_nprocure_tender(t, serve_files, base_url)
        for rel in releases:
            _, did_write = ocds.write_release(args.releases, rel)
            written += did_write
        for obs in observations:
            _, did_write = ocds.write_observation(args.observations, obs)
            obs_written += did_write

    # nProcure handshake failure: discovery aborted, but captured tenders persist —
    # releases AND observations are already written above, so the OBSERVED timeline
    # keeps growing even in a degraded run.
    run_error = raw.get("runError")
    if run_error:
        ocds.write_status(serve_source, {
            "source": SOURCE, "label": LABEL, "engine": "nprocure",
            "ok": False,
            "lastRun": raw.get("staleSince", ocds.now_iso()),
            "tendersCaptured": tenders, "releasesWritten": written,
            "observationsWritten": obs_written,
            "lastError": run_error,
        })
        print(f"[{SOURCE}] DEGRADED: {run_error} "
              f"({tenders} previously-captured tenders still served, "
              f"{obs_written} new observations)")
        return

    ocds.write_status(serve_source, {
        "source": SOURCE, "label": LABEL, "engine": "nprocure",
        "ok": True, "lastRun": ocds.now_iso(),
        "tendersCaptured": tenders, "releasesWritten": written,
        "observationsWritten": obs_written,
        "lastError": None,
    })
    print(f"[{SOURCE}] {tenders} tenders, {written} new releases, "
          f"{obs_written} new observations.")


if __name__ == "__main__":
    main()
