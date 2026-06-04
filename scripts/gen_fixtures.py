#!/usr/bin/env python3
"""
Generate the binary fixtures (PDFs + BOQ zips) referenced by the raw.json files.

Deterministic and offline: we synthesise valid artifacts rather than committing
binaries to git (see .gitignore). One PDF is rendered as a page IMAGE with no
text layer, so the scraper's pdftotext step finds nothing and falls through to
tesseract OCR — exercising the scanned-PDF path for real.

Run: python3 scripts/gen_fixtures.py
"""
import csv
import io
import json
import os
import zipfile

from reportlab import rl_config
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image, ImageDraw, ImageFont

# DETERMINISM: reportlab stamps a wall-clock /CreationDate + random /ID by default,
# so every run produced a different sha256 → the committed ledger's content-addressed
# shas became unreproducible (all file/text links 404'd on a fresh clone). invariant
# mode pins the date + derives a deterministic /ID. With pinned reportlab+pillow
# (requirements.txt) a fresh clone and CI reproduce byte-identical fixtures.
rl_config.invariant = 1

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures")


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= width:
            cur = f"{cur} {w}".strip()
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def text_pdf(path, title, org, body_lines):
    """A normal, selectable-text PDF (pdftotext handles this)."""
    c = canvas.Canvas(path, pagesize=A4, invariant=1)
    w, h = A4
    y = h - 2.5 * cm
    c.setFont("Helvetica-Bold", 13)
    c.drawString(2 * cm, y, "GOVERNMENT OF INDIA — e-PROCUREMENT")
    y -= 0.8 * cm
    c.setFont("Helvetica", 10)
    c.drawString(2 * cm, y, org)
    y -= 1.1 * cm
    c.setFont("Helvetica-Bold", 12)
    for ln in _wrap(title, 70):
        c.drawString(2 * cm, y, ln)
        y -= 0.7 * cm
    y -= 0.4 * cm
    c.setFont("Helvetica", 10)
    for para in body_lines:
        for ln in _wrap(para, 95):
            if y < 2.5 * cm:
                c.showPage()
                y = h - 2.5 * cm
                c.setFont("Helvetica", 10)
            c.drawString(2 * cm, y, ln)
            y -= 0.55 * cm
        y -= 0.3 * cm
    c.showPage()
    c.save()


def scanned_pdf(path, title, org, body_lines):
    """
    A PDF whose only content is a rasterised image of the text — i.e. a 'scan'.
    No text layer, so pdftotext returns ~nothing and the scraper must OCR it.
    """
    W, H = 1240, 1754  # ~150dpi A4
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    # Pillow's bundled scalable default font (>=10.1) — legible at size, identical
    # across macOS/Linux/CI (no system-font hunting), and a real text-bearing scan
    # for the OCR path. The old hardcoded /usr/share/fonts DejaVu path existed only
    # on Linux; on macOS it fell back to a microscopic bitmap font (non-deterministic
    # bytes + barely-OCR'able). load_default(size=...) fixes both.
    f_big = ImageFont.load_default(size=30)
    f_med = ImageFont.load_default(size=24)
    y = 90
    d.text((90, y), "GOVERNMENT OF INDIA - e-PROCUREMENT", font=f_big, fill="black"); y += 60
    d.text((90, y), org, font=f_med, fill="black"); y += 70
    for ln in _wrap(title, 52):
        d.text((90, y), ln, font=f_big, fill="black"); y += 44
    y += 30
    for para in body_lines:
        for ln in _wrap(para, 66):
            d.text((90, y), ln, font=f_med, fill="black"); y += 36
        y += 20
    # slight rotation so it reads like a real scan and stresses OCR a little
    img = img.rotate(0.4, expand=False, fillcolor="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    c = canvas.Canvas(path, pagesize=A4, invariant=1)
    pw, ph = A4
    c.drawImage(ImageReader(buf), 0, 0, width=pw, height=ph)
    c.showPage()
    c.save()


def boq_zip(path, title, rows):
    """A BOQ zip containing a CSV — stands in for the portal's BOQ bundle."""
    csv_buf = io.StringIO()
    wr = csv.writer(csv_buf)
    wr.writerow(["Sl.No", "Item Description", "Unit", "Qty", "Rate (INR)"])
    for r in rows:
        wr.writerow(r)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("BOQ.csv", csv_buf.getvalue())
        z.writestr("README.txt", f"Bill of Quantities for: {title}\n")


def body_for(title, org, event):
    """Construct plausible document body text from the event."""
    lines = [
        f"Reference: {org}.",
        "This document is published on the Central Public Procurement Portal "
        "and forms part of the contracting process record.",
    ]
    if event.get("summary"):
        lines.append("Subject: " + event["summary"])
    if event.get("closing"):
        lines.append(f"Last date and time for online bid submission: {event['closing']} 15:00 hrs.")
    if event.get("value"):
        lines.append(f"Estimated contract value: INR {event['value']:,}.")
    lines.append(
        "Bidders must hold a valid Class-III Digital Signature Certificate and be "
        "registered on the portal. Earnest Money Deposit as specified in the BOQ. "
        "The procuring entity reserves the right to amend or cancel this tender.")
    lines.append(f"Tender title: {title}")
    return lines


# --------------------------------------------------------------------------- #
def collect_files():
    """Yield (out_path, kind, title, org, event, scanned) for every referenced file."""
    jobs = []

    def add(source, fname, kind, title, org, event, scanned=False):
        out = os.path.join(FIXTURES, source, "files", fname)
        jobs.append((out, kind, title, org, event, scanned))

    # GePNIC-shaped sources (cppp, rajasthan)
    for source in ("cppp", "rajasthan"):
        raw = json.load(open(os.path.join(FIXTURES, source, "raw.json")))
        for t in raw["tenders"]:
            org = t["org"]
            title = next((e["title"] for e in t["events"] if e["type"] == "published"), t["tender_id"])
            for e in t["events"]:
                for d in e.get("docs", []):
                    fn = d["file"]
                    kind = "zip" if fn.endswith(".zip") else ("scanned" if d.get("scanned") else "pdf")
                    add(source, fn, kind, d["name"], org, e, scanned=d.get("scanned", False))

    # nProcure-shaped source (gujarat)
    raw = json.load(open(os.path.join(FIXTURES, "gujarat", "raw.json")))
    for t in raw["tenderList"]:
        org = t["department"]
        title = next((s["tenderTitle"] for s in t["stages"] if s["stage"] == "PUBLISHED"), t["nitId"])
        for s in t["stages"]:
            for a in s.get("attachments", []):
                fn = a["fileName"]
                ev = {"summary": s.get("tenderTitle"), "closing": s.get("bidEndDate"),
                      "value": t.get("estimatedCost")}
                kind = "zip" if fn.endswith(".zip") else "pdf"
                add("gujarat", fn, kind, a["label"], org, ev)
    return jobs


def main():
    jobs = collect_files()
    made = 0
    for out, kind, title, org, event, scanned in jobs:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if kind == "zip":
            boq_zip(out, title, [
                [1, "Earthwork excavation in ordinary soil", "Cum", 4200, 285],
                [2, "PCC 1:4:8 foundation", "Cum", 310, 5400],
                [14, "Structural steel fabrication", "MT", 18.2, 78500],
                [27, "Vitrified tile flooring 600x600", "Sqm", 1850, 1240],
            ])
        elif kind == "scanned":
            scanned_pdf(out, title, org, body_for(title, org, event))
        else:
            text_pdf(out, title, org, body_for(title, org, event))
        made += 1
        print(f"  fixture: {os.path.relpath(out, ROOT)}  ({kind})")
    print(f"Generated {made} fixture artifacts.")


if __name__ == "__main__":
    main()
