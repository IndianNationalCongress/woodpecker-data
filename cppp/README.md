# CPPP (Central Public Procurement Portal)

![status](https://img.shields.io/badge/scraper-fixture--mode-blue)
![engine](https://img.shields.io/badge/engine-GePNIC-informational)

- **Portal:** https://eprocure.gov.in / https://etenders.gov.in
- **Engine:** GePNIC (NIC)
- **Cadence:** every 6h (`cron: 17 */6 * * *`)
- **Start point:** extends [`switchr24/mcp-india-tenders`](https://github.com/switchr24/mcp-india-tenders) CPPP client.
- **Status:** read live at `data.sunshine.naklitechie.com/cppp/status.json`.

## OCDS mapping

GePNIC tender lifecycle → OCDS subset (see [`../README.md`](../README.md)):
`published→tender`, `corrigendum→tenderAmendment`, `bid opening→tenderUpdate`,
`award→award`. `ocid = sunshine-cppp-<portalTenderId>`.

## Run

```bash
python3 scraper/scrape.py --releases releases --serve ../../serve     # fixture mode
```

Fixtures (`fixtures/cppp/raw.json`) cover a full multi-corrigendum lifecycle, a
scanned-PDF tender (OCR path), and a tender whose portal link is dead (preserved-copy
UX). Live mode against eprocure.gov.in is the go-live step (see root `PICKUP.md`).
