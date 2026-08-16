"""Read the scanned 2021-2026 roll calls with a vision model instead of an OCR.

426 session records are retrievable and every one is printed-then-rescanned:
`pdftotext` returns one byte per page. They are the largest untouched body of
data in the project -- an 86-page file is one attendance sheet plus ~84 votes.

Why not tesseract. In these actas the vote is not a word, it is a mark in one of
several columns, and which column it lands in is the whole datum. An OCR returns
characters and loses the geometry; asking a model that sees the page returns the
column. Measured on a real page of `Asistencia_y_votacion_sesion_24_6_2026.pdf`
(52 pages, zero text layer), `agy` read the asunto, the printed totals 67/2/2 and
the per-legislator codes `SI +++`, `aus`, `SinRes` correctly.

Why it is still a pilot. A model can also invent a row, and a fabricated vote
next to a real politician's name is the worst failure this project can produce.
So nothing here is stored unless the transcribed rows reproduce the tally the
acta prints for itself -- the same oracle the text-layer parser uses.

Needs `agy` on PATH and permission to use its tools headlessly. It is blocked by
default: a headless run cannot answer a permission prompt. Grant it either with
an allow-rule in agy's settings.json, or per-run with --dangerously-skip-permissions
(which approves every tool, so prefer the rule).
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import db
from .ocr import page_count, printed_totals  # shared with the tesseract pilot

PROMPT = (
    "Lee {img} (acta de votación del Congreso del Perú, escaneada) y transcribe "
    "TODAS las filas de la tabla, una por línea, con el formato exacto\n"
    "  APELLIDOS NOMBRES | GRUPO | VOTO\n"
    "donde VOTO es exactamente uno de: SI NO ABST AUS LO LE LP SINRES.\n"
    "Mapea 'SI +++'→SI, 'NO ---'→NO, 'Abst.'→ABST, 'aus'→AUS, 'SinRes'→SINRES.\n"
    "No omitas ninguna fila y no inventes ninguna: si una casilla está ilegible, "
    "escribe ILEGIBLE como voto.\n"
    "Termina con dos líneas sueltas:\n"
    "  ASUNTO: <el asunto de la votación>\n"
    "  IMPRESO: si=<n> no=<n> abst=<n>   (lo que dice el recuadro de resultado)"
)
ROW = re.compile(r"^(.+?)\s*\|\s*([A-ZÁÉÍÓÚÑ. ]{1,12})\s*\|\s*([A-Z]+)\s*$", re.M)
VOTES = {"SI", "NO", "ABST", "AUS", "LO", "LE", "LP", "SINRES"}


def available():
    return bool(shutil.which("agy"))


def render(pdf, page, dpi=200):
    """One page to PNG. 200 dpi is enough for these 150-300 dpi scans."""
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["pdftoppm", "-r", str(dpi), "-f", str(page), "-l", str(page),
                    "-png", str(pdf), str(tmp / "pg")], check=True, timeout=300)
    return next(tmp.glob("pg*.png"))


def ask(img, timeout=900):
    """Run agy over one page image. Returns its raw text."""
    r = subprocess.run(
        ["agy", "--sandbox", "--add-dir", str(img.parent),
         "--print", PROMPT.format(img=img.name)],
        capture_output=True, text=True, timeout=timeout)
    out = r.stdout.strip()
    if "permission" in out.lower() and "denied" in out.lower():
        raise PermissionError(
            "agy no pudo usar sus herramientas en modo headless. Añade una "
            "allow-rule en su settings.json y vuelve a intentarlo.")
    return out


def parse(text):
    """-> (asunto, printed, rows). Rows outside the vote vocabulary are dropped
    rather than guessed at; they show up as a count mismatch, which is the point."""
    rows = [(n.strip(), g.strip(), v) for n, g, v in ROW.findall(text)
            if v in VOTES]
    asunto = next((m.group(1).strip() for m in
                   re.finditer(r"^ASUNTO:\s*(.+)$", text, re.M)), None)
    m = re.search(r"IMPRESO:\s*si=(\d+)\s+no=(\d+)\s+abst=(\d+)", text, re.I)
    printed = tuple(int(x) for x in m.groups()) if m else printed_totals(text)
    return asunto, printed, rows


def agrees(printed, rows):
    """The only reason to trust a transcription: it reproduces the acta's own
    totals. Without that there is no way to tell a good read from a fluent one."""
    if not printed:
        return False
    got = tuple(sum(1 for *_, v in rows if v == k) for k in ("SI", "NO", "ABST"))
    return got == tuple(printed)


def read_page(pdf, page):
    """-> dict or None. None means: do not store this, it did not check out."""
    img = render(pdf, page)
    try:
        asunto, printed, rows = parse(ask(img))
    finally:
        shutil.rmtree(img.parent, ignore_errors=True)
    return {"asunto": asunto, "printed": printed, "rows": rows,
            "ok": agrees(printed, rows)}


def pilot(pdf, pages=3):
    """Measure, do not ingest. `python3 -m ingest.ocr_agy <acta.pdf>`"""
    if not available():
        print("agy no está en PATH.")
        return
    pdf = Path(pdf)
    n = page_count(pdf)
    print(f"{pdf.name}: {n} páginas")
    ok = 0
    for p in range(2, min(2 + pages, n + 1)):
        try:
            r = read_page(pdf, p)
        except PermissionError as e:
            print(f"  p{p}: {e}")
            return
        except Exception as e:  # noqa: BLE001 - the pilot reports failures as data
            print(f"  p{p}: {e!r}")
            continue
        ok += r["ok"]
        print(f"  p{p}: {len(r['rows']):3} filas · impreso {r['printed']} · "
              f"{'CUADRA' if r['ok'] else 'NO CUADRA'} · {(r['asunto'] or '')[:46]}")
    print(f"\n{ok}/{pages} páginas reproducen los totales impresos.")
    print("Si esto no llega a 3/3, las 426 actas no valen el cómputo: sin el "
          "conteo impreso no hay forma de saber si una fila es real.")


if __name__ == "__main__":
    import sys
    _ = db
    pilot(sys.argv[1] if len(sys.argv) > 1 else
          "data/legacy/Asistencia_y_votacion_sesion_24_6_2026.pdf")
