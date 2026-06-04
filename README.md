# sunshine-data — ledger + scrapers + compile

The sovereign data pipeline. **Append-only**: corrections are *new releases*, never
edits or deletes to committed release JSON. The git history is the audit trail and
the fork point — anyone can clone this and rebuild the whole archive.

```
sunshine-data/                 (= this data/ dir; becomes its own repo)
├── cppp/                       central — eprocure.gov.in / GePNIC
│   ├── scraper/                scrape.py + vendored ocds.py
│   ├── releases/<ocid>/<release-id>.json    committed OCDS releases (the ledger)
│   ├── README.md               source URL, OCDS subset, status badge, cadence
│   └── .github/workflows/cppp.yml
├── rajasthan/                  GePNIC
├── gujarat/                    nProcure / (n)Code — non-GePNIC, net-new scraper
├── compile/compile.py          build step → records + indices + manifest
└── .github/workflows/compile.yml
```

> **What goes to git vs R2.** Git holds **only OCDS release JSON** — lightweight,
> diffable, forkable. Binaries (PDFs/zips) and extracted text go **straight to R2**,
> content-addressed by sha256 and deduped. Never commit binaries.

## The OCDS subset (v1.0)

We adopt [OCDS](https://standard.open-contracting.org/) and use a pragmatic subset.

- **ocid**: `sunshine-<source>-<portalTenderId>` — self-prefixed; we are an
  unofficial mirror, not a registered publisher.
- **release.tag**: `tender` (published) · `tenderAmendment` (corrigendum) ·
  `tenderUpdate` (bid opening) · `award`.
- **release**: `{ ocid, id, date, tag[], buyer, tender{title,value,status,
  tenderPeriod,…}, documents[] }`.
- **documents[]**: `{ id, documentType, title, url (R2, DATA-relative), sourceUrl
  (original portal — kept forever, even after it 404s), sourceAlive, sha256, format,
  textUrl (R2 .txt), extractionMethod }`.
- **record** (compiled, v1.0): `{ ocid, releases[] (ordered), compiledRelease
  (latest-wins snapshot), amendments[] }`. We do **not** implement full OCDS merge
  rules at v1.0 — known, documented simplification.

## OCR / text extraction

Inline in the scraper: `pdftotext -layout` for text PDFs; if that yields nothing,
fall through to **tesseract (Hindi + English)** on `pdftoppm` page images. Output is
written to R2 as `/files/<sha256>.txt`. Escalation to a Cloudflare Container/Queue is
**per-source, on volume** — not built at v1.0.

## How to add a source (the source-module contract)

Sunshine is built to be forked and extended. Adding a portal is self-contained — you
never touch another source's code (Independence Principle).

1. **`mkdir <source>/scraper`** and copy `cppp/scraper/ocds.py` into it (vendored —
   each source owns its copy so a change can't break siblings). Language is your call;
   the only contract is the OCDS output.
2. **Write `<source>/scraper/scrape.py`.** Per run it must:
   discover new/changed tenders → emit OCDS release(s) (`ocds.build_release`) → push
   each artifact through `ocds.store_document` (content-address + OCR) → append release
   JSON with `ocds.write_release` (never overwrite) → write `status.json` with
   `ocds.write_status` (`ok`, `lastRun`, `lastError`, counts). See the three existing
   scrapers — GePNIC sources share a shape; `gujarat/` shows a genuinely different
   engine (nProcure) mapped onto the same OCDS output.
3. **Add `<source>/.github/workflows/<source>.yml`** — your own cron, your own status
   badge. No orchestrator, no shared job.
4. **Add `<source>/README.md`** (copy a sibling): portal URL, OCDS subset notes,
   status badge, cadence.
5. **Register it in the app** — one `makeSource(...)` line (see `app/README.md`).

That's it. The compile step auto-discovers any `<source>/releases/` dir.

## Run the pipeline locally

From the repo root: `bash ../scripts/run_pipeline.sh` (or, scrapers individually:
`python3 cppp/scraper/scrape.py --releases cppp/releases --serve ../serve`, then
`python3 compile/compile.py --data . --serve ../serve`).

## Sources

| Source | Engine | Status |
|---|---|---|
| [CPPP (central)](cppp/) | GePNIC (NIC) | fixture-mode |
| [Rajasthan](rajasthan/) | GePNIC (NIC) | fixture-mode |
| [Gujarat](gujarat/) | nProcure / (n)Code | fixture-mode (net-new) |
