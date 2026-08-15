"""OCR pilot for the 2021-2026 roll-call backlog.

426 session records are retrievable and every one is a printed-then-rescanned
image: `pdftotext` returns one byte per page, `pdffonts` lists none. They are the
largest untouched body of data in the project -- an 86-page file is one
attendance sheet plus ~84 separate roll calls.

This is a PILOT, deliberately. OCR over a table where the vote is encoded by the
horizontal position of a mark is exactly where it fails, and failing quietly
would put wrong votes next to real names. So the pilot only measures, against the
one oracle that cannot be argued with: every acta prints its own totals.

Needs `tesseract-ocr` and `tesseract-ocr-spa`, which are not installed here and
need root. Run the pilot before deciding whether the other 421 are worth it:

    sudo apt install -y tesseract-ocr tesseract-ocr-spa
    python3 -m ingest.ocr
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import api, db

INDEX = ("https://www2.congreso.gob.pe/Sicr/RelatAgenda/"
         "PlenoComiPerm20112016.nsf/Votacion?ReadViewEntries&ExpandView&Count=1000")
DIR = db.ROOT / "data" / "legacy"
# The printed summary block, whichever of the two layouts the acta uses.
TOTAL_RE = re.compile(
    r"(?:A\s*FAVOR|SI)\D{0,20}(\d{1,3}).{0,80}?(?:EN\s*CONTRA|NO)\D{0,20}(\d{1,3})"
    r".{0,80}?ABSTEN\w*\D{0,20}(\d{1,3})", re.S | re.I)


def have_tesseract():
    return bool(shutil.which("tesseract"))


def page_count(pdf):
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    m = re.search(r"Pages:\s+(\d+)", out)
    return int(m.group(1)) if m else 0


def ocr(pdf, first=1, last=None, dpi=300):
    """Rasterise and OCR a page range. Returns the recognised text.

    300 dpi because these are 150-300 dpi scans of a printed grid; below that the
    single-letter marks in the vote columns stop being separable.
    """
    last = last or page_count(pdf)
    with tempfile.TemporaryDirectory() as tmp:
        stem = Path(tmp) / "p"
        subprocess.run(["pdftoppm", "-r", str(dpi), "-f", str(first), "-l", str(last),
                        "-png", str(pdf), str(stem)], check=True, timeout=900)
        out = []
        for img in sorted(Path(tmp).glob("p-*.png")):
            r = subprocess.run(
                ["tesseract", str(img), "stdout", "-l", "spa", "--psm", "6"],
                capture_output=True, text=True, timeout=300)
            out.append(r.stdout)
    return "\n\f\n".join(out)


def printed_totals(text):
    """-> (si, no, abst) as the document states them about itself, or None."""
    m = TOTAL_RE.search(text)
    return tuple(int(g) for g in m.groups()) if m else None


def sample(n=5):
    """The Domino view is XML; take the first n 2021-2026 PDFs it lists."""
    xml = api.fetch(INDEX).decode("utf-8", "replace")
    hits = re.findall(r'unid="([0-9A-F]{32})"', xml)
    return hits[:n]


def pilot(n=5, pages=4):
    """Measure, do not ingest. Prints a table you can decide from."""
    if not have_tesseract():
        print("tesseract no está instalado. Para correr el piloto:\n"
              "  sudo apt install -y tesseract-ocr tesseract-ocr-spa")
        return None
    DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for unid in sample(n):
        url = (f"https://www2.congreso.gob.pe/Sicr/RelatAgenda/"
               f"PlenoComiPerm20112016.nsf/Apleno/{unid}/$FILE/acta.pdf")
        p = DIR / f"{unid}.pdf"
        try:
            if not p.exists():
                p.write_bytes(api.fetch(url, timeout=180))
            plain = subprocess.run(["pdftotext", str(p), "-"],
                                   capture_output=True, text=True).stdout
            text = ocr(p, 1, min(pages, page_count(p)))
            rows.append((unid[:8], page_count(p), len(plain.strip()),
                         len(text.strip()), printed_totals(text)))
        except Exception as e:  # noqa: BLE001 - the pilot reports failures as data
            rows.append((unid[:8], 0, 0, 0, f"error: {e}"))
    print(f"{'acta':10} {'págs':>5} {'texto':>6} {'ocr':>7}  totales impresos")
    for r in rows:
        print(f"{r[0]:10} {r[1]:>5} {r[2]:>6} {r[3]:>7}  {r[4]}")
    got = sum(1 for r in rows if isinstance(r[4], tuple))
    print(f"\n{got}/{len(rows)} actas con totales legibles por OCR.")
    print("Si esto no llega a 5/5, los 421 restantes no valen el cómputo:\n"
          "sin los totales impresos no hay forma de validar las filas.")
    return rows


if __name__ == "__main__":
    pilot()
