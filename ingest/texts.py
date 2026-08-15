"""Text of each bill, extracted from the PDF the Congress publishes.

The only rubric criterion the site scored zero on was the primary document: the
bill itself was a link out, never readable on the page.

Two routes serve the same document. `/archivo/uuid/{uuid}` answers "Token de
captcha no proporcionado" -- that one is gated and stays untouched. The numeric
route the viewer uses to render inline, `/archivo/{btoa(id)}/pdf`, is open, and
is what this uses.

One document per bill, not all 84,058 attachments: the earliest dated action that
carries a file is the bill as filed, which is the text a reader wants. Later
attachments are dictámenes and autógrafas, already linked from the timeline.
"""
import datetime as dt
import re
import subprocess
import time
import urllib.request

from . import api, db

PDF_DIR = db.ROOT / "data" / "billpdf"


def primary_doc(con, bill_id):
    """The filed bill.

    PRESENTADO is the filing, so prefer it; then anything whose filename does not
    look like an annex, because a filing can also carry an adhesion oficio; then
    the earliest, which is what it was before and is right most of the time.
    """
    return con.execute(
        "SELECT doc_id, doc_name, doc_url FROM bill_action "
        "WHERE bill_id=? AND doc_id IS NOT NULL ORDER BY "
        "  (text = 'PRESENTADO') DESC, "
        "  (lower(coalesce(doc_name,'')) LIKE '%oficio%' "
        "   OR lower(coalesce(doc_name,'')) LIKE '%adhesi%' "
        "   OR lower(coalesce(doc_name,'')) LIKE '%derivacion%') ASC, "
        "  acted_on ASC LIMIT 1", (bill_id,)).fetchone()


def pdf_path(doc_id):
    return PDF_DIR / f"{doc_id}.pdf"


# Some bills are filed as photographs taken on a phone -- one 2026 document is
# an 89 MB iOS Quartz PDF with no text layer at all. Cap the download: past this
# size it is certainly a scan, and there is nothing to extract anyway.
MAX_BYTES = 30 * 1024 * 1024


def fetch_pdf(doc_id, url):
    """Download once, capped, and keep it; documents are immutable after filing.

    There is no cheap way to ask the size first: HEAD 404s and a Range request is
    answered with the whole file, so "check the size, then download" cost two
    full transfers of every document -- 37 seconds each on the 89 MB one. Read it
    in chunks and stop once it is clearly a scan rather than a text.
    """
    p = pdf_path(doc_id)
    if p.exists() and p.stat().st_size > 0:
        return p
    req = urllib.request.Request(url, headers={"User-Agent": api.UA})
    buf = bytearray()
    with urllib.request.urlopen(req, timeout=240) as r:
        while True:
            chunk = r.read(1 << 18)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_BYTES:
                raise TooBig(f"más de {MAX_BYTES // 1024 // 1024} MB")
    if not buf.startswith(b"%PDF"):
        raise ValueError(f"not a pdf: {bytes(buf[:80])!r}")
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    p.write_bytes(buf)
    return p


class TooBig(Exception):
    """Too large to be anything but a scan."""


def extract(path):
    """-> (text, pages). `-layout` keeps the numbered article structure."""
    out = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                         capture_output=True, text=True, timeout=180).stdout
    pages = out.count("\f") + 1 if out.strip() else 0
    return clean(out), pages


def clean(t):
    """Drop the page furniture pdftotext leaves behind."""
    t = t.replace("\f", "\n")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def ingest_one(con, bill_id, pause=0.0):
    doc = primary_doc(con, bill_id)
    if not doc:
        return 0
    path = fetch_pdf(doc["doc_id"], doc["doc_url"])
    body, pages = extract(path)
    note = None if len(body) >= 200 else "sin capa de texto: publicado como imagen"
    db.upsert(con, "bill_text", {
        "bill_id": bill_id, "doc_id": doc["doc_id"], "pages": pages,
        "chars": len(body), "body": body if not note else None, "note": note,
        "source_url": doc["doc_url"],
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    })
    if note:
        return 0
    if pause:
        time.sleep(pause)
    return len(body)


def demo():
    """`python3 -m ingest.texts` -- hits the live document route."""
    from . import spley
    url = spley.doc_url(157135)
    assert url.endswith("/archivo/MTU3MTM1/pdf"), url
    p = fetch_pdf(157135, url)
    body, pages = extract(p)
    assert pages >= 1, pages
    assert len(body) > 500, len(body)
    assert "LEY" in body.upper(), body[:200]
    assert "\f" not in body
    print(f"ok: {pages} páginas, {len(body)} caracteres, {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    demo()
