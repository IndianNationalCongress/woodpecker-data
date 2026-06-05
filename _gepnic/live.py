#!/usr/bin/env python3
"""
GePNIC (NIC eProcurement) LIVE fetcher — the real-portal SOURCE feeding the shared
GePNIC engine (scrape.py) for ANY GePNIC instance: CPPP (eprocure.gov.in) and the
state/org portals (mahatenders, tntenders, mptenders, tendersodisha, wbtenders,
etenders.kerala, jharkhandtenders, uktenders, hptenders, assamtenders, defproc, …).

All GePNIC instances share the same app structure, so one fetcher serves them all —
the ONLY per-portal input is `base_url`. This is the "normalize to our spline" win:
width across sources = a registry of base URLs, not N scrapers.

SAFETY (non-negotiable, unchanged from the CPPP original):
  - READ-ONLY: GET only. Never POST, never submit a form, never log in.
  - Never solve / bypass a CAPTCHA. If a needed page is gated we STOP (LiveBlocked).
  - Polite + bounded: one request at a time, a descriptive UA, a sleep between
    requests, a per-request timeout, a hard cap on tenders AND on pages walked.

PUBLICLY GET-FETCHABLE (verified across CPPP + state instances, Jun 2026):
  - `?page=FrontEndListTendersbyDate&service=page` renders a captcha-free table of
    recent tenders by closing date (the captcha guards only the optional SEARCH
    widget). The browse-by-date listing PAGINATES via session-bound anchors
    `…&sp=AFrontEndListTendersbyDate,table&sp=<N>` — following them (same session)
    yields pages 2..N → DEPTH.
  - Each row's detail link is session-bound (404s cold, resolves with the listing's
    JSESSIONID carried) → the captcha-free DETAIL page (fields + document names).

NOT GET-FETCHABLE (the honest boundary, unchanged): the document BINARIES — a stateful
Tapestry `docDownoad` listener with no per-file URL. So we content-address the CLEAN
detail-page TEXT we CAN fetch and record each portal PDF with its title + URL but
sourceAlive=False. Greedy on what's public, honest about what isn't.

stdlib only: urllib + re + html.
"""
from __future__ import annotations

import html as _html
import re
import time
import urllib.request
from http.cookiejar import CookieJar

DEFAULT_PORTAL_BASE = "https://eprocure.gov.in/eprocure/app"   # CPPP; overridden per source

USER_AGENT = (
    "WoodpeckerArchive/0.1 "
    "(+https://github.com/IndianNationalCongress/woodpecker-data; "
    "civic-tech tender preservation)"
)

HARD_MAX = 60           # absolute ceiling on tenders per --live run (depth, was 10)
MAX_PAGES = 8           # absolute ceiling on listing pages walked per run
SLEEP_SECONDS = 2.5     # polite gap between requests
TIMEOUT_SECONDS = 30    # per-request timeout


class LiveBlocked(Exception):
    """Raised when a needed page is gated (captcha / login / stale-only). We STOP
    rather than try to circumvent it; the caller leaves other sources intact."""


# --------------------------------------------------------------------------- #
# url helpers (portal-agnostic)
# --------------------------------------------------------------------------- #
def _host_of(base_url: str) -> str:
    m = re.match(r"(https?://[^/]+)", base_url)
    return m.group(1) if m else ""


def listing_url_for(base_url: str) -> str:
    return f"{base_url}?page=FrontEndListTendersbyDate&service=page"


def _absolutize(href: str, base_url: str) -> str:
    """Resolve a portal-relative href against the portal's app path / host."""
    if href.startswith("http"):
        return href
    host = _host_of(base_url)
    if href.startswith("?"):
        return base_url + href            # relative to the app path (…/app?…)
    if href.startswith("/"):
        return host + href                # absolute path on the host
    return host + "/" + href.lstrip("/")


# --------------------------------------------------------------------------- #
# polite, session-carrying GET client (one shared CookieJar = one session)
# --------------------------------------------------------------------------- #
def _opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar())
    )


def _get(opener, url: str):
    """A single polite GET. Returns (text, content_type). GET only."""
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )
    # open via THIS opener (carries the session CookieJar) — not the global urlopen,
    # which would drop the cookie and make detail/page links return "Stale Session".
    with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
        ctype = resp.headers.get("Content-Type", "") or ""
        raw = resp.read()
    return raw.decode("utf-8", "replace"), ctype


def _strip(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def _looks_blocked(html_text: str) -> str | None:
    low = html_text.lower()
    if "stale session" in low or "session has timed out" in low:
        return "stale-session (server discarded the session before we could read it)"
    return None


# --------------------------------------------------------------------------- #
# best-effort Internet Archive preservation (free, no infra of our own)
# --------------------------------------------------------------------------- #
WAYBACK_SAVE = "https://web.archive.org/save/"
_wb_disabled = False   # circuit-breaker: after one miss skip the rest of the run


def wayback_save(url: str, *, timeout: int = 10) -> str | None:
    """Best-effort: ask the Internet Archive to snapshot `url`; return the snapshot
    URL if determinable, else None. NEVER raises, tightly timed out. Self-limiting:
    one miss disables it for the rest of the run (Wayback rate-limits SPN)."""
    global _wb_disabled
    if _wb_disabled or not url:
        return None
    try:
        req = urllib.request.Request(
            WAYBACK_SAVE + url, method="GET", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final = resp.geturl() or ""
            cl = resp.headers.get("Content-Location", "") or ""
        if "/web/" in final:
            return final
        if cl.startswith("/web/"):
            return "https://web.archive.org" + cl
    except Exception:
        pass
    _wb_disabled = True
    return None


# --------------------------------------------------------------------------- #
# listing parse (+ pagination)
# --------------------------------------------------------------------------- #
_DATE_RE = r"[0-9]{2}-[A-Za-z]{3}-[0-9]{4}\s+[0-9:]{4,8}\s*[AP]M"


def parse_listing(html_text: str) -> list[dict]:
    """FrontEndListTendersbyDate body -> ordered list of row dicts:
        {tender_id, ref, title, detail_path, published, closing, opening}."""
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


def page_links(html_text: str, base_url: str) -> list[str]:
    """GePNIC listing pagination: session-bound anchors to
    `…sp=AFrontEndListTendersbyDate,table&sp=<N>` (page N, N>=2). Returns absolute
    URLs in page order. Following them with the SAME opener yields more rows."""
    found: dict[int, str] = {}
    for m in re.finditer(
        r'href="([^"]*sp=A?FrontEndListTendersbyDate%2Ctable&(?:amp;)?sp=(\d+)[^"]*)"',
        html_text,
    ):
        page = int(m.group(2))
        if page >= 2 and page not in found:
            found[page] = _absolutize(_html.unescape(m.group(1)), base_url)
    return [found[p] for p in sorted(found)]


# --------------------------------------------------------------------------- #
# detail parse
# --------------------------------------------------------------------------- #
def parse_detail(html_text: str) -> dict:
    """Tender detail page -> {fields: {caption: value}, documents: [names]}."""
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
# orchestration: listing (+ pages) -> N detail pages (one session, polite, bounded)
# --------------------------------------------------------------------------- #
def fetch_recent_tenders(
    max_n: int,
    base_url: str = DEFAULT_PORTAL_BASE,
    *,
    max_pages: int = MAX_PAGES,
    tag: str = "gepnic",
    verbose: bool = True,
) -> list[dict]:
    """Return up to `max_n` real recent tenders from a GePNIC portal, walking the
    browse-by-date listing across up to `max_pages` pages for DEPTH. GET-only,
    captcha-free, session-carried. Raises LiveBlocked if a needed page is gated."""
    n = max(1, min(int(max_n), HARD_MAX))
    pages_cap = max(1, min(int(max_pages), MAX_PAGES))
    opener = _opener()
    listing_url = listing_url_for(base_url)

    if verbose:
        print(f"[{tag}:live] GET listing {listing_url}")
    listing_html, _ = _get(opener, listing_url)
    blocked = _looks_blocked(listing_html)
    if blocked:
        raise LiveBlocked(f"listing page blocked: {blocked}")

    rows = [r for r in parse_listing(listing_html) if r["tender_id"] and r["detail_path"]]
    if not rows:
        # Distinguish a WORKING-but-empty portal (valid GePNIC page, "No Tenders found"
        # on the current date) from a real gate. Empty is ok (the source is tracked,
        # the daily cron picks up tenders when they appear); a gate is a hard stop.
        if re.search(r"No\s+Tenders?\s+found", listing_html, re.I):
            if verbose:
                print(f"[{tag}:live] valid GePNIC page, no tenders on the current date — empty (ok)")
            return []
        raise LiveBlocked(
            "listing rendered no tender rows (browse-by-date appears captcha-gated)"
        )

    # --- DEPTH: walk pagination links (session-carried) until we have enough rows ---
    seen_ids = {r["tender_id"] for r in rows}
    if len(rows) < n:
        pages = page_links(listing_html, base_url)[: pages_cap - 1]
        for pidx, purl in enumerate(pages, start=2):
            if len(rows) >= n:
                break
            time.sleep(SLEEP_SECONDS)
            if verbose:
                print(f"[{tag}:live] GET listing page {pidx}")
            try:
                page_html, _ = _get(opener, purl)
            except Exception as exc:  # noqa: BLE001 — one bad page must not abort the run
                if verbose:
                    print(f"[{tag}:live]   page {pidx} fetch failed ({exc}); stopping pagination")
                break
            for r in parse_listing(page_html):
                if r["tender_id"] and r["detail_path"] and r["tender_id"] not in seen_ids:
                    seen_ids.add(r["tender_id"])
                    rows.append(r)

    rows = rows[:n]
    if verbose:
        print(f"[{tag}:live] listing parsed: {len(rows)} tender(s) (cap {n})")

    out: list[dict] = []
    for idx, row in enumerate(rows, start=1):
        time.sleep(SLEEP_SECONDS)
        detail_url = _absolutize(row["detail_path"], base_url)
        if verbose:
            print(f"[{tag}:live]  ({idx}/{len(rows)}) GET detail {row['tender_id']}")
        try:
            detail_html, _ = _get(opener, detail_url)
        except Exception as exc:  # noqa: BLE001 — a single detail failure must not abort
            if verbose:
                print(f"[{tag}:live]    detail fetch failed ({exc}); skipping")
            continue
        if _looks_blocked(detail_html):
            if verbose:
                print(f"[{tag}:live]    detail page stale/blocked; skipping")
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
