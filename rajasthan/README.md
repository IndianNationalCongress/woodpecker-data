# Rajasthan e-Procurement

![status](https://img.shields.io/badge/scraper-fixture--mode-blue)
![engine](https://img.shields.io/badge/engine-GePNIC-informational)

- **Portal:** https://eproc.rajasthan.gov.in
- **Engine:** GePNIC (NIC) — same family as CPPP; the cheap GePNIC start.
- **Cadence:** every 6h (`cron: 37 */6 * * *`, offset from CPPP)
- **Start point:** existing OCDS client for Rajasthan (GePNIC).
- **Status:** `data.woodpecker.naklitechie.com/rajasthan/status.json`.

## OCDS mapping

Same GePNIC → OCDS subset as CPPP. `ocid = woodpecker-rajasthan-<portalTenderId>`.
The scraper is an **independent copy** (Independence Principle) — it shares no
runtime code with CPPP.

## Run

```bash
python3 scraper/scrape.py --releases releases --serve ../../serve     # fixture mode
```

Fixtures cover a publish → corrigendum → bid-opening lifecycle.
