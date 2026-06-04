# Gujarat (nProcure)

![status](https://img.shields.io/badge/scraper-fixture--mode-blue)
![engine](https://img.shields.io/badge/engine-nProcure-orange)

- **Portal:** https://www.nprocure.com / https://tender.nprocure.com
- **Engine:** **nProcure / (n)Code Solutions — NOT GePNIC.** No upstream OCDS client.
- **Cadence:** every 8h (`cron: 57 */8 * * *`, longer — nProcure is rate-sensitive)
- **Start point:** **net-new scraper.** This is the cross-engine portability test.
- **Status:** `data.sunshine.naklitechie.com/gujarat/status.json`.

## Why this source exists

CPPP and Rajasthan both run GePNIC; cloning one proves nothing about portability.
Gujarat runs a genuinely different engine (nProcure), with its own raw shape
(`tenderList` / `stages` / nProcure field names), its own corrigendum model, and a
DSC/session handshake (NIC-CA applet). `scraper/scrape.py` maps all of that onto the
*same* OCDS subset the GePNIC sources emit. That mapping is the abstraction test.

## Failure behaviour (Independence Principle)

nProcure's DSC/session handshake is the fragile bit. When it fails, new-tender
discovery aborts — but anything already captured stays served from the ledger, and
`status.json` is written `ok:false` with the error. The app then shows
"Gujarat: scraper failing" **without** affecting CPPP/Rajasthan or the app. The
fixture (`fixtures/gujarat/raw.json`) carries a `runError` to exercise exactly this.

## Run

```bash
python3 scraper/scrape.py --releases releases --serve ../../serve     # fixture mode
```

## Go-live notes (net-new — budget for these)

- DSC/session handshake + cookie/applet flow against nProcure.
- nProcure's own corrigendum/amendment model → OCDS `tenderAmendment` mapping.
- No reference OCDS client to lean on — validate the OCDS output by hand first.
