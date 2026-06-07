# Right to Information application — procurement records (template)

> Fillable template. Replace every `[BRACKETED]` field. The Schedule (§3) is the
> RTI-askable subset of `_import/backfill_schema.json` (sourceRoute = rti) — keep the two in sync.
> Notes for the applicant are at the bottom; delete them before filing.

---

To,
The Public Information Officer (PIO),
**[Name of public authority — e.g. Office of the Engineer-in-Chief, Public Works Department]**
[Office address]
[State / UT]

**Subject:** Application under Section 6(1) of the Right to Information Act, 2005 — tender and procurement records, requested in electronic form.

Sir / Madam,

Under Section 6(1) of the Right to Information Act, 2005, I request the following information held by your public authority.

### 1. Information sought

For every tender invited or floated by **[public authority / specific office, division or circle]** during the period **[DD-MM-YYYY] to [DD-MM-YYYY]** *[optional: "having an estimated value of ₹[X] lakh or more"]*, please provide the particulars listed in the Schedule at §3 below, as a **single electronic spreadsheet, one row per tender**.

### 2. Form of the information — Section 7(9)

These particulars are maintained by your office in electronic form in the e-procurement system (**GePNIC / Central Public Procurement Portal / [nProcure / other]**). Under Section 7(9) I request the information **in that electronic form — a CSV or Excel file** — and not as printed pages. Exporting existing electronic records into a spreadsheet is *retrieval of information already held*, not the creation of new information, and is therefore within the scope of the Act.

The columns sought are set out as the header row of the **attached spreadsheet template (Annexure-2)**. **Please populate every column your office holds on record, and leave blank only those for which no information is held** — a particular not held is not a ground to withhold the rest.

### 3. Schedule of particulars (per tender)

**A. Tender identification & notice**
- Tender ID / NIT number; tender reference number
- Inviting office (full organisation chain); tender-inviting authority — name & designation
- Work / item description; procurement category (works / goods / services); procurement method (open / limited / EOI / single / global)
- Estimated value put to tender; EMD; tender-document fee
- Date published; bid-submission start and end dates; pre-bid meeting date
- Final status (live / cancelled / re-tendered / awarded)

**B. Corrigenda / amendments**
- For each corrigendum: date, nature of the change, and the value or date changed (old → new)

**C. Bid opening & participation**
- Technical and financial bid-opening dates
- **Number of bids received**
- Names of the bidders who participated

**D. Award / result**
- Name of the successful bidder and its vendor / GST registration number
- **Awarded value**, and the date and reference number of the Award of Contract (AOC) / Letter of Award
- Where the contract was not awarded to the lowest bidder, the reason recorded
- Contract / agreement number; date of signing; contract value; contract period; present status (completed / ongoing / terminated)

**E. Documents** *(for the awarded tenders, or the specific Tender IDs at Annexure-1)*
- A list of the documents on record for each tender (NIT, BOQ / schedule, corrigenda, comparative statement, AOC); and
- Copies of the **Notice Inviting Tender** and the **Award of Contract**.

### 4. Severability — Section 10

If any particular is treated as exempt (for instance under Section 8(1)(d)), I request that the **remaining non-exempt particulars be provided after severance under Section 10**, and that for each item withheld the specific exemption relied on, and the field it applies to, be stated. I respectfully note that the **name of the successful bidder, the awarded value, the number of bids received, and the dates of a concluded public contract** have repeatedly been held disclosable in the public interest, commercial-confidence notwithstanding.

### 5. Fee

I enclose the application fee of **₹10** by **[Indian Postal Order / Demand Draft / court-fee stamp / online payment reference]**, in favour of **[Accounts Officer / as prescribed by the public authority]**.
*[If applicable: I belong to the Below Poverty Line category and am exempt from fee under Section 7(5); a copy of my BPL card is enclosed.]*
I undertake to pay any further fee chargeable under Section 7(3) for the cost of providing the information, and request prior intimation of that cost, with its calculation, **before** it is incurred.

### 6. Declarations

- I am a **citizen of India**.
- If the information sought is held by, or is more closely connected with the functions of, another public authority, please **transfer this application under Section 6(3)** within five days and inform me of the transfer.
- Please provide the information within the **30 days** prescribed by Section 7(1).

Yours faithfully,

**[Full name]**
[Full postal address]
[Email] · [Phone]

Date: **[ ]**  Place: **[ ]**

*Enclosures:* (1) Fee of ₹10 — [instrument & number]. (2) Annexure-2 — spreadsheet column template (`rti_template.csv`). (3) *[Annexure-1: list of specific Tender IDs, if §3E used.]* (4) *[BPL card copy, if claiming exemption.]*

---
---

## Notes to the applicant — delete before filing

**Where to file**
- **Central** public authorities (CPPP / a Union ministry, NIC): file online at **rtionline.gov.in** (fee paid in-portal).
- **States** with their own RTI portal (e.g. Maharashtra, and a growing set): use that portal.
- Otherwise: **post / hand-deliver** to the PIO with the ₹10 fee as an **IPO or DD** (a court-fee stamp where the state allows it). Keep proof of dispatch.

**Keep it answerable.** Bound the request to *one authority + a date window* (and, if you can, a value floor). "All tenders, all time, statewide" invites a Section 7(9) "disproportionate diversion of resources" brush-off; "[PWD Division X], 01-04-2016 to 31-03-2021, ≥ ₹10 lakh" does not.

**Expect deflections, and pre-empt them.**
- *"It's already on the portal."* It is not: the portal exposes **no date-range bulk export**, and **no award-stage data** (supplier, awarded value, bid count) in any downloadable form. Say so.
- *"Commercial confidence" (§8(1)(d)) / third-party (§11)* will be aimed at **bidder-level financials**. The §4 severability paragraph is built to save the core award/contract/count fields even if the losing bidders' detailed quotes are withheld.

**If you get nothing, or a partial / evasive reply**
- No reply in **30 days** = deemed refusal. File a **First Appeal** to the First Appellate Authority (the officer senior to the PIO, named in the reply or on the authority's website) **within 30 days**.
- Still unsatisfied → **Second Appeal** to the Central / State Information Commission (CIC / SIC), within 90 days.

**Retention reality.** RTI reaches records that *exist and are retained*. Tender files commonly carry a 5–10 year retention; contract / agreement files longer. Pre-e-procurement-rollout records (roughly pre-2012 in many states) may be paper-only or already weeded — so the award stage will not reach as far back as the oldest scraped notices.

**Two filing targets, by goal**
- Want the **award/contract lifecycle of a real department** (the data the portal never had) → that department's PIO.
- Want a **platform-wide structured export** → the NIC / CPPP nodal ministry PIO; ask directly for the OCDS or database export.

**On import.** A reply lands as **declared, not observed** data — provenance-label it like `assam_archive` (see `provenanceRecord` + the `bulkImport` path in `_import/backfill_schema.json`): `engine:import`, `imported:true`, no capture-floor snapshots. The reply fills the index-entry row directly (and the award fields under `awardFieldsToAdd`); optionally tag it `witness:'rti'` so a disclosed figure that disagrees with the portal becomes a v1.2 corroboration signal.
