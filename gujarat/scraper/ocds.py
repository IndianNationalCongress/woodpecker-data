"""
Sunshine OCDS helper — vendored per-source (Independence Principle).

This module is COPIED into each source's scraper/ dir, not imported across
sources. That is deliberate: one source must never be able to break another by
changing shared code. Keep edits source-local; if you improve this, copy the
improvement outward consciously.

It implements the v1.0 OCDS *subset* described in sunshine-spec-001:
  - ocid scheme:  sunshine-<source>-<portalTenderId>
  - releases tagged: tender / tenderAmendment / tenderUpdate / award
  - documents content-addressed by sha256, text extracted (pdftotext -> OCR)

No third-party deps: stdlib + the poppler/tesseract CLIs.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone

SCHEME = "sunshine"

# Portal event -> OCDS release tag (spec table).
TAG_FOR_EVENT = {
    "published": "tender",
    "corrigendum": "tenderAmendment",
    "bidOpening": "tenderUpdate",
    "award": "award",
}

# OCDS tender.status by lifecycle stage.
STATUS_FOR_EVENT = {
    "published": "active",
    "corrigendum": "active",
    "bidOpening": "active",
    "award": "complete",
}


# --------------------------------------------------------------------------- #
# identifiers + time
# --------------------------------------------------------------------------- #
def ocid_for(source: str, tender_id: str) -> str:
    return f"{SCHEME}-{source}-{tender_id}"


def iso(date_str: str) -> str:
    """Normalise 'YYYY-MM-DD' (or already-ISO) to 'YYYY-MM-DDT00:00:00Z'."""
    if not date_str:
        return ""
    if "T" in date_str:
        return date_str
    return f"{date_str}T00:00:00Z"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# binaries: content-address + text extraction (the OCR path)
# --------------------------------------------------------------------------- #
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _pdftotext(pdf_path: str) -> str:
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=120,
        )
        return out.stdout.decode("utf-8", "replace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _ocr_pdf(pdf_path: str) -> str:
    """Scanned PDF -> images (pdftoppm) -> tesseract (Hindi + English)."""
    text_parts: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        prefix = os.path.join(td, "page")
        try:
            subprocess.run(
                ["pdftoppm", "-r", "200", "-png", pdf_path, prefix],
                capture_output=True, timeout=300, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            return ""
        for name in sorted(os.listdir(td)):
            if not name.endswith(".png"):
                continue
            try:
                out = subprocess.run(
                    ["tesseract", os.path.join(td, name), "stdout",
                     "-l", "hin+eng"],
                    capture_output=True, timeout=300,
                )
                text_parts.append(out.stdout.decode("utf-8", "replace"))
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
    return "\n".join(text_parts)


def extract_text(path: str) -> tuple[str, str]:
    """Return (text, method). method in {'pdftotext','ocr','none','raw'}."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        text = _pdftotext(path)
        if len(re.sub(r"\s", "", text)) >= 20:
            return text, "pdftotext"
        ocr = _ocr_pdf(path)
        if ocr.strip():
            return ocr, "ocr"
        return "", "none"
    if ext in (".txt", ".csv"):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(), "raw"
    # zips / binaries we don't crack open at v1.0
    return "", "none"


def store_document(serve_files_dir: str, *, src_path: str, doc_id: str,
                   document_type: str, title: str, source_url: str,
                   source_alive: bool) -> dict:
    """
    Content-address a binary into the (local R2 stand-in) /files/ store, extract
    its text, and return the OCDS document object.

    Dedup: a sha256 already present is not re-written (corrigenda that re-attach
    the same file collapse to one copy — the vision's dedup requirement).

    url/textUrl are stored as DATA-relative paths ('/files/<sha>.<ext>') so the
    app's single DATA constant is the only local<->cloud switch.
    """
    os.makedirs(serve_files_dir, exist_ok=True)
    sha = sha256_file(src_path)
    ext = os.path.splitext(src_path)[1].lower().lstrip(".") or "bin"
    fmt = mimetypes.guess_type(src_path)[0] or "application/octet-stream"

    blob_name = f"{sha}.{ext}"
    blob_path = os.path.join(serve_files_dir, blob_name)
    if not os.path.exists(blob_path):
        with open(src_path, "rb") as s, open(blob_path, "wb") as d:
            d.write(s.read())

    text, method = extract_text(src_path)
    text_url = None
    if method != "none":
        txt_name = f"{sha}.txt"
        txt_path = os.path.join(serve_files_dir, txt_name)
        if not os.path.exists(txt_path):
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(text)
        text_url = f"/files/{txt_name}"

    return {
        "id": doc_id,
        "documentType": document_type,
        "title": title,
        "url": f"/files/{blob_name}",          # DATA-relative (R2 copy)
        "sourceUrl": source_url,                # original portal URL, kept forever
        "sourceAlive": bool(source_alive),      # false => "removed from portal"
        "sha256": sha,
        "format": fmt,
        "textUrl": text_url,
        "extractionMethod": method,
    }


# --------------------------------------------------------------------------- #
# release construction + append-only ledger write
# --------------------------------------------------------------------------- #
def build_release(*, source: str, tender_id: str, seq: int, event_type: str,
                  date: str, buyer: dict, tender: dict, documents: list,
                  amendment: dict | None = None) -> dict:
    ocid = ocid_for(source, tender_id)
    rel = {
        "ocid": ocid,
        "id": f"{ocid}-{seq:04d}",
        "date": iso(date),
        "tag": [TAG_FOR_EVENT.get(event_type, "tender")],
        "initiationType": "tender",
        "buyer": buyer,
        "tender": tender,
        "documents": documents,
    }
    if amendment:
        rel["amendments"] = [amendment]
    return rel


def write_release(releases_dir: str, release: dict) -> tuple[str, bool]:
    """
    Append-only write. Path: <releases_dir>/<ocid>/<release-id>.json.
    Never overwrites an existing committed release (corrections are NEW releases).
    Returns (path, written?).
    """
    d = os.path.join(releases_dir, release["ocid"])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{release['id']}.json")
    if os.path.exists(path):
        return path, False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(release, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path, True


def write_status(serve_source_dir: str, status: dict) -> str:
    os.makedirs(serve_source_dir, exist_ok=True)
    path = os.path.join(serve_source_dir, "status.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


# --------------------------------------------------------------------------- #
# observation layer — the OBSERVED timeline (v1.0 capture floor)
#
# OCDS releases above are the issuer's *declared* timeline. Sunshine adds an
# *observed* timeline: every fetch of a known ocid writes an observation +
# a full content-addressed snapshot. The gap between the two timelines —
# a stateHash that moved with no declared release (a silent edit), a tender
# that 404s (a pull) — is the transparency signal. A snapshot not captured is
# unrecoverable, so this is a v1.0 floor even though the diff UI is v1.1.
# --------------------------------------------------------------------------- #
def canonical_state(tender: dict, doc_shas: list) -> str:
    """Deterministic canonical JSON of a tender's observable state. Stable across
    runs/machines, so stateHash is reproducible (the diff substrate depends on it)."""
    state = {
        "title": tender.get("title", ""),
        "status": tender.get("status", ""),
        "value": tender.get("value"),
        "tenderPeriod": tender.get("tenderPeriod"),
        "documents": sorted(doc_shas),
    }
    return json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def store_snapshot(serve_files_dir: str, raw_state: dict) -> str:
    """Content-address a full raw per-fetch snapshot into /files/<sha>.json (R2
    stand-in). This is the artifact that makes a vanished state recoverable.
    Returns a DATA-relative url. Deduped like binaries (identical state = one blob)."""
    os.makedirs(serve_files_dir, exist_ok=True)
    blob = json.dumps(raw_state, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8")
    sha = _sha256_bytes(blob)
    path = os.path.join(serve_files_dir, f"{sha}.json")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(blob)
    return f"/files/{sha}.json"


def _obs_filename(observed_at: str) -> str:
    """ISO timestamp -> filesystem-safe, lexically-sortable filename."""
    return re.sub(r"[^0-9TZ]", "", observed_at) or "obs"


def build_observation(*, source, ocid, observed_at, availability, state_hash,
                      doc_hashes, snapshot_url, undeclared_change) -> dict:
    return {
        "ocid": ocid,
        "observedAt": iso(observed_at),       # ISO timestamp of OUR fetch
        "witness": f"sunshine:{source}",      # v1.0 = sunshine only; reserved for wayback/cdl/cag (v1.2)
        "availability": availability,         # 'present' | 'altered' | 'removed'
        "stateHash": state_hash,              # sha256 of canonicalised observable state
        "documents": doc_hashes,              # [{id, sha256}] visible this fetch
        "snapshotUrl": snapshot_url,          # R2 path to the full raw snapshot
        "undeclaredChange": bool(undeclared_change),
    }


def build_observation_sequence(*, source, ocid, fetches, serve_files_dir) -> list:
    """Normalised fetches -> observation records, computing stateHash, availability,
    and undeclaredChange.

    fetch = {
      observedAt: ISO/date,
      removed:   bool,            # this fetch 404'd / delisted -> a PULL
      declared:  bool,            # this fetch coincided with a declared OCDS release
      state:     dict,            # observable tender state (for the hash)
      doc_hashes:[{id, sha256}],  # documents visible this fetch (cumulative)
      raw:       dict,            # full raw snapshot to content-address
    }

    undeclaredChange := stateHash moved vs the previous observation AND no declared
    release happened at this fetch  ==  a SILENT EDIT.
    """
    observations, prev_hash = [], None
    for f in sorted(fetches, key=lambda x: x["observedAt"]):
        if f.get("removed"):
            snap = store_snapshot(serve_files_dir,
                                  {"ocid": ocid, "observedAt": f["observedAt"], "availability": "removed"})
            observations.append(build_observation(
                source=source, ocid=ocid, observed_at=f["observedAt"], availability="removed",
                state_hash=prev_hash or "", doc_hashes=[], snapshot_url=snap, undeclared_change=False))
            continue
        doc_shas = [d["sha256"] for d in f.get("doc_hashes", [])]
        h = _sha256_bytes(canonical_state(f["state"], doc_shas).encode("utf-8"))
        if prev_hash is None:
            availability, undeclared = "present", False
        elif h != prev_hash:
            availability, undeclared = "altered", (not f.get("declared", False))
        else:
            availability, undeclared = "present", False
        snap = store_snapshot(serve_files_dir,
                              f.get("raw") or {"ocid": ocid, "observedAt": f["observedAt"], "state": f["state"]})
        observations.append(build_observation(
            source=source, ocid=ocid, observed_at=f["observedAt"], availability=availability,
            state_hash=h, doc_hashes=f.get("doc_hashes", []), snapshot_url=snap, undeclared_change=undeclared))
        prev_hash = h
    return observations


def write_observation(observations_dir: str, observation: dict) -> tuple:
    """Append-only write: <observations_dir>/<ocid>/<observedAt>.json. Never
    overwrites — a captured observation is immutable (it's the audit trail)."""
    d = os.path.join(observations_dir, observation["ocid"])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{_obs_filename(observation['observedAt'])}.json")
    if os.path.exists(path):
        return path, False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(observation, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path, True
