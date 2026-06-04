#!/usr/bin/env python3
"""
CPPP (GePNIC) LIVE fetcher — the real-portal SOURCE that feeds scrape.py's
existing OCDS/observation transform in --live mode.

SAFETY (non-negotiable, see scrape.py --live docstring):
  - READ-ONLY: GET only. Never POST, never submit a form, never log in.
  - Never solve / bypass a CAPTCHA. If we hit one on a needed page we STOP.
  - Polite + bounded: one request at a time, a descriptive UA, a sleep between
    requests, a per-request timeout, a hard cap on tenders.

WHAT IS PUBLICLY GET-FETCHABLE (verified against eprocure.gov.in, Jun 2026):
  - `?page=FrontEndListTendersbyDate&service=page` renders, with NO captcha in
    the page body, a table of ~10 recent tenders by closing date (e-Published /
    Bid-Closing / Opening dates, Title, Ref.No., Tender ID, per-tender detail
    link). The captcha on that page guards only the optional quick-SEARCH widget;
    the default browse-by-date listing is open.
  - Each row's "View Tender Information" link (`...&service=direct&...&sp=<tok>`)
    is session-bound: it 404s ("Stale Session") cold, but resolves to the full
    tender DETAIL page when the JSESSIONID from the listing fetch is carried.
    The detail page (also captcha-free) carries Organisation Chain, Tender ID,
    Reference Number, Tender Value, Work Description, category, bid dates, EMD,
    and the list of attached document names.

WHAT IS *NOT* GET-FETCHABLE (the honest boundary):
  - The document BINARIES. Every doc on the detail page is a single Tapestry
    listener anchor `<a id="docDownoad" href="...component=docDownoad...">` with
    NO per-file identifier in the URL — the file is chosen by a stateful form
    submit. Hitting that href via GET returns the captcha home page (text/html),
    not a PDF. Retrieving the real bytes would require a form submit (a POST-like
    listener call we are forbidden to make) on a captcha-guarded session.
  => So in --live we GET + content-address the real DETAIL-PAGE HTML (the public
     artifact we CAN fetch) and record each portal document with its real title +
     portal URL but sourceAlive=False (binary gated). Greedy on what's public,
     honest about what isn't.

stdlib only: urllib + re + html (no requests/bs4).
"""
from __future__ import annotations

import html as _html
import re
import time
import urllib.request
from http.cookiejar import CookieJar

PORTAL_BASE = "https://eprocure.gov.in/eprocure/app"
LISTING_URL = f"{PORTAL_BASE}?page=FrontEndListTendersbyDate&service=page"

USER_AGENT = (
    "SunshineArchive/0.1 "
    "(+https://github.com/NakliTechie/sunshine-data-ledger; "
    "civic-tech tender preservation)"
)

HARD_MAX = 10           # absolute ceiling on tenders per --live run
SLEEP_SECONDS = 2.5     # polite gap between requests
TIMEOUT_SECONDS = 30    # per-request timeout


class LiveBlocked(Exception):
    """Raised when a needed page is gated (captcha / login / stale-only). We STOP
    rather than try to circumvent it; the caller leaves fixture mode intact."""


# --------------------------------------------------------------------------- #
# polite, session-carrying GET client (one shared CookieJar = one session)
# --------------------------------------------------------------------------- #
def _opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar())
    )


def _get(opener, url: str, *, want_bytes: bool = False):
    """A single polite GET. Returns (text|bytes, content_type). GET only."""
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )
    # IMPORTANT: open via THIS opener (carries the CookieJar) — not the module-level
    # urllib.request.urlopen, which would use the global opener and drop the session
    # cookie, making every detail link return "Stale Session".
    with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
        ctype = resp.headers.get("Content-Type", "") or ""
        raw = resp.read()
    if want_bytes:
        return raw, ctype
    return raw.decode("utf-8", "replace"), ctype


def _strip(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def _looks_blocked(html_text: str) -> str | None:
    low = html_text.lower()
    if "stale session" in low or "session has timed out" in low:
        return "stale-session (server discarded the session before we could read it)"
    # A captcha INPUT/refresh in the page is fine on the listing (search widget);
    # only treat it as a block if the page is essentially nothing but a captcha gate
    # (no listing table, no detail fields) — caller checks emptiness separately.
    return None


# --------------------------------------------------------------------------- #
# listing parse
# --------------------------------------------------------------------------- #
_DATE_RE = r"[0-9]{2}-[A-Za-z]{3}-[0-9]{4}\s+[0-9:]{4,8}\s*[AP]M"


def parse_listing(html_text: str) -> list[dict]:
    """FrontEndListTendersbyDate body -> ordered list of row dicts:
        {tender_id, ref, title, detail_path, published, closing, opening}.
    Rows are <tr class="even|odd">…</tr>; the title is an <a id="DirectLink…">
    whose component IDs Tapestry auto-suffixes (DirectLink, DirectLink_0, …)."""
    rows: list[dict] = []
    for chunk in re.split(r'<tr\s+class="(?:even|odd)"', html_text)[1:]:
        chunk = chunk.split("</tr>")[0]
        lm = re.search(
            r'<a id="DirectLink[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            chunk, re.S,
        )
        if not lm:
            continue
        detail_path = _html.unescape(lm.group(1))
        title = _strip(lm.group(2)).strip("[]")
        tail = chunk[lm.end():]
        bracks = re.findall(r"\[([^\]]*)\]", tail)
        ref = bracks[0].strip() if len(bracks) >= 1 else ""
        tid = bracks[1].strip() if len(bracks) >= 2 else ""
        dates = re.findall(rf"({_DATE_RE})", chunk)
        rows.append({
            "tender_id": tid,
            "ref": ref,
            "title": title,
            "detail_path": detail_path,
            "published": dates[0] if len(dates) >= 1 else "",
            "closing": dates[1] if len(dates) >= 2 else "",
            "opening": dates[2] if len(dates) >= 3 else "",
        })
    return rows


# --------------------------------------------------------------------------- #
# detail parse
# --------------------------------------------------------------------------- #
def parse_detail(html_text: str) -> dict:
    """Tender detail page -> {fields: {caption: value}, documents: [names]}.
    The page is caption/value cell pairs: <td class="td_caption">L</td>
    <td class="td_field">V</td> (either may wrap <b>; captions hold entities
    like &#8377; for the rupee sign)."""
    cells = re.findall(
        r'<td\b[^>]*class="(td_caption|td_field)"[^>]*>(.*?)</td>',
        html_text, re.S | re.I,
    )
    fields: dict[str, str] = {}
    i = 0
    while i < len(cells):
        kind, val = cells[i]
        if kind == "td_caption" and i + 1 < len(cells) and cells[i + 1][0] == "td_field":
            cap = _strip(val).rstrip(":").strip()
            fv = _strip(cells[i + 1][1])
            if cap and cap not in fields:
                fields[cap] = fv
            i += 2
        else:
            i += 1

    documents = [
        _strip(d)
        for d in re.findall(r'<a id="docDownoad[^"]*"[^>]*>(.*?)</a>', html_text, re.S)
        if _strip(d)
    ]
    return {"fields": fields, "documents": documents}


# --------------------------------------------------------------------------- #
# orchestration: listing -> N detail pages (one session, polite, bounded)
# --------------------------------------------------------------------------- #
def fetch_recent_tenders(max_n: int, *, verbose: bool = True) -> list[dict]:
    """Return up to `max_n` real recent tenders, each:
        {tender_id, ref, title, published, closing, opening,
         detail_url, detail_html, fields, documents}.
    GET-only, captcha-free, session-carried. Raises LiveBlocked if a needed page
    is gated, so the caller can keep fixture mode intact and report the blocker."""
    n = max(1, min(int(max_n), HARD_MAX))
    opener = _opener()

    if verbose:
        print(f"[cppp:live] GET listing {LISTING_URL}")
    listing_html, _ = _get(opener, LISTING_URL)
    blocked = _looks_blocked(listing_html)
    if blocked:
        raise LiveBlocked(f"listing page blocked: {blocked}")

    rows = parse_listing(listing_html)
    rows = [r for r in rows if r["tender_id"] and r["detail_path"]]
    if not rows:
        # The listing rendered but held no tender rows — almost always means the
        # browse-by-date listing got captcha-gated. Treat as a hard block.
        raise LiveBlocked(
            "listing rendered no tender rows (browse-by-date appears captcha-gated)"
        )
    rows = rows[:n]
    if verbose:
        print(f"[cppp:live] listing parsed: {len(rows)} tender(s) (cap {n})")

    out: list[dict] = []
    for idx, row in enumerate(rows, start=1):
        time.sleep(SLEEP_SECONDS)
        detail_url = row["detail_path"]
        if detail_url.startswith("/"):
            detail_url = "https://eprocure.gov.in" + detail_url
        if verbose:
            print(f"[cppp:live]  ({idx}/{len(rows)}) GET detail {row['tender_id']}")
        try:
            detail_html, _ = _get(opener, detail_url)
        except Exception as exc:  # noqa: BLE001 — a single detail failure must not abort the run
            if verbose:
                print(f"[cppp:live]    detail fetch failed ({exc}); skipping")
            continue
        if _looks_blocked(detail_html):
            if verbose:
                print("[cppp:live]    detail page stale/blocked; skipping")
            continue
        parsed = parse_detail(detail_html)
        out.append({
            **row,
            "detail_url": detail_url,
            "detail_html": detail_html,
            "fields": parsed["fields"],
            "documents": parsed["documents"],
        })
    if not out:
        raise LiveBlocked("no tender detail pages were fetchable (all stale/blocked)")
    return out
