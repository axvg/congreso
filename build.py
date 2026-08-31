"""Static site for the bicameral Congress (2026-2031).

`python3 build.py [db] [outdir]` -> writes site/ from data/congreso.db.
Stdlib only: sqlite3 + f-strings. No template engine, no framework, no npm.
Every page is a file on disk, so the site deploys anywhere and needs no runtime.
"""
import csv
import datetime as dt
import difflib
import functools
import html
import io
import math
import os
import pathlib
import re
import shutil
import sys
import time
import unicodedata

from ingest import db, gastos

ROOT = pathlib.Path(__file__).resolve().parent
DBP = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else db.DB
OUT = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "site"

PORTAL = "https://wb2server.congreso.gob.pe/spley-portal/#"
MOCPORTAL = "https://wb2server.congreso.gob.pe/smociones-portal/#"
API = "https://api.congreso.gob.pe/spley-portal-service"
PER_PAGE = 150

CHAMBER = {"D": "Cámara de Diputados", "S": "Senado", "C": "Congreso de la República"}
EN = {"D": "la Cámara de Diputados", "S": "el Senado",
      "C": "el Congreso de la República"}
CHAMBER_SHORT = {"D": "Diputados", "S": "Senado", "C": "Congreso"}
SEG = {"D": "diputados", "S": "senado", "C": "congreso"}

# ---------------------------------------------------------------- vocabulary

# Every state the Congress publishes, mapped to a stage of the legislative
# track, a sentence a citizen can read, and what happens next. This is the
# whole point of the site: `EN COMISIÓN` is not an answer, it is a code.
STATUS = {
    "PRESENTADO": ("PRES",
        "Ingresó al registro oficial y nadie lo ha debatido todavía. El paso "
        "siguiente es que Trámite Documentario lo envíe a una comisión "
        "dictaminadora.",
        ["Decreto de envío a una o más comisiones.",
         "Estudio en comisión, que puede pedir opinión a ministerios y gremios."]),
    "EN COMISIÓN": ("COM",
        "Está en manos de una comisión, que debe estudiarlo y emitir un "
        "dictamen. Es la etapa en la que más proyectos se detienen. Abajo están "
        "las cifras del periodo anterior, cuántos salieron y cuántos se "
        "quedaron.",
        ["Dictamen de la comisión (favorable, favorable sustitutorio o de archivo).",
         "Si hay dictamen favorable, pasa al Orden del Día.",
         "Si la comisión no dictamina, el proyecto caduca al final del periodo."]),
    "PASA A COMISIÓN": ("COM",
        "Fue derivado a una comisión para su estudio.",
        ["Dictamen de la comisión.", "Con dictamen favorable, pasa al Orden del Día."]),
    "RETORNA A COMISIÓN": ("COM",
        "El Pleno lo devolvió a comisión. El texto debatido no convenció y "
        "debe ser reformulado.",
        ["Nuevo dictamen con el texto corregido.",
         "Regreso al Orden del Día del Pleno."]),
    "EN CUARTO INTERMEDIO": ("PLENO",
        "El debate en el Pleno se suspendió a pedido de la comisión o de una "
        "bancada para reformular el texto sobre la marcha. El proyecto sigue "
        "vivo y en agenda.",
        ["Lectura del texto sustitutorio y reanudación del debate.",
         "Votación en el mismo Pleno o en uno posterior."]),
    "DICTAMEN": ("COM",
        "La comisión ya se pronunció y emitió dictamen. Con eso el proyecto "
        "queda habilitado para ser debatido por el Pleno.",
        ["Ingreso al Orden del Día por acuerdo del Consejo Directivo.",
         "Debate y votación en el Pleno."]),
    "EN AGENDA CD": ("PLENO",
        "El Consejo Directivo lo tiene en agenda. Ese órgano decide qué se "
        "debate y en qué orden. Es el filtro entre el dictamen y el Pleno.",
        ["Acuerdo del Consejo Directivo para incorporarlo al Orden del Día.",
         "Debate y votación en el Pleno."]),
    "ORDEN DEL DÍA": ("PLENO",
        "Está en el Orden del Día: hace cola para ser debatido por el Pleno. "
        "Entrar a la cola no garantiza que se vea en la siguiente sesión.",
        ["Programación en la agenda de una sesión del Pleno.",
         "Debate y votación."]),
    "EN AGENDA DEL PLENO": ("PLENO",
        "Está programado en la agenda de una sesión del Pleno. Es el paso "
        "inmediatamente anterior al debate.",
        ["Debate en el Pleno.", "Votación; de aprobarse, segunda votación o exoneración."]),
    "EN AGENDA DE LA COMISIÓN PERMANENTE": ("PLENO",
        "Será visto por la Comisión Permanente, que legisla sobre las materias "
        "que el Pleno le delega, en especial fuera de legislatura.",
        ["Debate y votación en la Comisión Permanente."]),
    "EN DEBATE - PLENO": ("PLENO",
        "Se está debatiendo en el Pleno en este momento o en la sesión "
        "registrada.",
        ["Votación al cierre del debate."]),
    "APROBADO 1ERA. VOTACIÓN": ("PLENO",
        "El Pleno lo aprobó en primera votación. El Reglamento exige una "
        "segunda votación al menos siete días después, salvo que la Junta de "
        "Portavoces la exonere.",
        ["Segunda votación, o exoneración acordada por la Junta de Portavoces.",
         "Con la segunda votación aprobada, se redacta la autógrafa."]),
    "PENDIENTE 2DA. VOTACIÓN": ("PLENO",
        "Aprobado en primera votación y a la espera de la segunda. Muchos "
        "proyectos se detienen exactamente aquí.",
        ["Segunda votación en el Pleno."]),
    "EN RECONSIDERACIÓN": ("PLENO",
        "Un grupo de parlamentarios pidió reconsiderar la votación ya "
        "realizada. Hasta que la reconsideración se vote, el resultado "
        "anterior queda en suspenso.",
        ["Votación de la reconsideración.",
         "Si se rechaza, el resultado original queda firme."]),
    "APROBADO": ("PLENO",
        "El Pleno aprobó el texto.",
        ["Redacción y firma de la autógrafa de ley.",
         "Envío al Poder Ejecutivo."]),
    "AUTÓGRAFA": ("AUTOG",
        "El texto aprobado fue firmado y remitido al Presidente de la "
        "República. Tiene quince días útiles para promulgarlo u observarlo.",
        ["Promulgación por el Presidente, u observación con reparos.",
         "Si no hace ninguna de las dos, promulga el Presidente del Congreso.",
         "Publicación en el diario oficial El Peruano."]),
    "AUTÓGRAFA OBSERVADA": ("AUTOG",
        "El Poder Ejecutivo observó la autógrafa. Devolvió el texto al "
        "Congreso con reparos en lugar de promulgarlo.",
        ["Nuevo dictamen de la comisión sobre las observaciones.",
         "El Congreso puede allanarse a las observaciones o insistir; la "
         "insistencia requiere la mitad más uno del número legal."]),
    "ACUERDO DE COMISIÓN": ("COM",
        "La comisión acordó algo sobre el proyecto, por ejemplo acumularlo o "
        "pedir opiniones. Todavía no es un dictamen.",
        ["Dictamen de la comisión.", "Con dictamen favorable, pasa al Orden del Día."]),
    "EN DEBATE - COMISIÓN PERMANENTE": ("PLENO",
        "Se está debatiendo en la Comisión Permanente, que legisla sobre lo que "
        "el Pleno le delega, sobre todo fuera de legislatura.",
        ["Votación en la Comisión Permanente."]),
    "EN DEBATE DE LA COMISIÓN PERMANENTE": ("PLENO",
        "Se está debatiendo en la Comisión Permanente, que legisla sobre lo que "
        "el Pleno le delega, sobre todo fuera de legislatura.",
        ["Votación en la Comisión Permanente."]),
    "NO APROBADO": ("DEAD",
        "Se votó y no se aprobó. El proyecto queda rechazado; el mismo texto no "
        "puede volver a presentarse en la misma legislatura.",
        []),
    "NO ALCANZÓ Nº DE VOTOS": ("DEAD",
        "Se votó pero no reunió el número de votos que la materia exigía. No "
        "hubo mayoría suficiente, de modo que el texto no prosperó.",
        []),
    "PROMULGADO/PRESIDENTE DEL CONGRESO": ("LEY",
        "Promulgó el Presidente del Congreso. Ocurre cuando el Ejecutivo dejó "
        "vencer el plazo sin promulgar ni observar, o cuando el Congreso "
        "insistió sobre una autógrafa observada.",
        ["Publicación en el diario oficial El Peruano."]),
    "PROMULGADO/PRESIDENTE DE LA REPÚBLICA": ("LEY",
        "El Presidente de la República promulgó la ley.",
        ["Publicación en el diario oficial El Peruano.",
         "Entrada en vigencia al día siguiente de la publicación, salvo "
         "disposición distinta."]),
    "PUBLICADA EN EL DIARIO OFICIAL EL PERUANO": ("LEY",
        "Es ley. Se promulgó y se publicó en el diario oficial El Peruano.",
        []),
    "ACLARACIÓN": ("LEY",
        "Se publicó una fe de erratas o aclaración sobre el texto ya "
        "promulgado.",
        []),
    "AL ARCHIVO": ("DEAD",
        "Fue archivado. El expediente se cierra sin convertirse en ley; para "
        "revivir la idea hay que presentar un proyecto nuevo.",
        []),
    "DECRETO DE ARCHIVO": ("DEAD",
        "Archivado por decreto, normalmente al terminar el periodo "
        "parlamentario sin que la comisión dictaminara.",
        []),
    "RECHAZADO DE PLANO": ("DEAD",
        "Fue rechazado de plano, sin llegar a estudio de fondo.",
        []),
    "RETIRADO POR SU AUTOR": ("DEAD",
        "Su autor lo retiró. El expediente se cierra a pedido de quien lo "
        "presentó.",
        []),
}

STAGE_BLURB = {
    "PRES": "Ingreso formal del proyecto al registro del Congreso.",
    "COM": "Estudio y dictamen de la comisión especializada.",
    "PLENO": "Debate y votación en el hemiciclo de la cámara de origen.",
    "REV": "Revisión y votación en la otra cámara.",
    "AUTOG": "Texto firmado y remitido al Poder Ejecutivo.",
    "LEY": "Promulgación y publicación en El Peruano.",
}


def stages(per_par, chamber):
    """The track a bill has to walk. 2026 on is bicameral: origin -> revisora.

    ponytail: no published state says "in the revising chamber" yet, the
    vocabulary is still the unicameral one, so the REV node can be reached but
    never becomes the current one. When the Congress starts publishing a
    bicameral state, add it to STATUS with stage "REV" and this lights up.
    """
    ch = chamber or "C"
    if per_par < 2026 or ch == "C":
        return [("PRES", "Presentado"), ("COM", "En comisión"),
                ("PLENO", "Pleno del Congreso"), ("AUTOG", "Autógrafa"),
                ("LEY", "Ley publicada")]
    other = "S" if ch == "D" else "D"
    return [("PRES", "Presentado"), ("COM", "En comisión"),
            ("PLENO", f"Pleno de {CHAMBER_SHORT[ch]}"),
            ("REV", f"{CHAMBER_SHORT[other]} (cámara revisora)"),
            ("AUTOG", "Autógrafa"), ("LEY", "Ley publicada")]


def status_info(s):
    """(stage, sentence, next steps) for any published state, known or not."""
    k = (s or "").strip().upper()
    if k in STATUS:
        return STATUS[k]
    return ("PRES",
            f"El Congreso registra este proyecto como «{s}». No tenemos una "
            "explicación verificada de esa etapa; el expediente oficial "
            "enlazado abajo es la fuente.",
            ["Consultar el expediente oficial para el detalle del trámite."])


# ------------------------------------------------------------------- helpers

def esc(s):
    return html.escape(str(s if s is not None else ""))


def norm(s):
    """Fold to ASCII upper for name matching. Same rule as ingest.legislators,
    copied so build.py stays sqlite3-only."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^A-Z, ]", "", s.upper()).strip()


def slugify(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-") or "x"


def nice_name(full):
    """'Mendoza Bernedo, Liz Huli' -> 'Liz Huli Mendoza Bernedo'."""
    if "," in (full or ""):
        last, first = full.split(",", 1)
        return f"{first.strip()} {last.strip()}"
    return full or ""


MES = ("enero febrero marzo abril mayo junio julio agosto septiembre octubre "
       "noviembre diciembre").split()


def fecha(iso):
    if not iso:
        return ""
    try:
        d = dt.date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)[:10]
    return f"{d.day} de {MES[d.month - 1]} de {d.year}"


@functools.lru_cache(maxsize=None)
def _enc(n):
    """Ciphertext path segment for the official dossier viewer. Optional: the
    portal's deep link needs the AES helper, which lives in ingest and pulls a
    non-stdlib import, so a failure here only costs the deep link."""
    try:
        from ingest import spley
        return spley.enc(n)
    except Exception:
        return None


def bill_url(r, b):
    return f"{r}proyecto/{b['per_par']}/{b['chamber'] or 'C'}/{b['ply_num']}.html"


def leg_url(r, slug):
    return f"{r}parlamentario/{slug}.html"


def vote_url(r, v):
    return f'{r}votacion/{v["slug"]}.html'


def ctte_url(r, c):
    return f'{r}comision/{c["slug"]}.html'


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


# La escala tipográfica, en Python: los tamaños que se calculan a partir de una
# caja (el monograma dentro de un mosaico de 76 px) también salen a la página, y
# sin pasar por aquí meten un décimo y un undécimo tamaño que la hoja no tiene.
TYPE_SCALE = (11, 12.5, 13.5, 16, 17, 20, 24, 30, 40)


def step(px):
    """El paso de la escala más cercano a `px`."""
    return min(TYPE_SCALE, key=lambda s: abs(s - px))


def clip(s, n, tail="…"):
    """Shorten for display without ever cutting a word in half.

    Every display truncation on this site goes through here, because showing
    the site to somebody means sending them a link, and a link that previews as
    «…LA AMPLIACIÓN DE LA INFRA» reads as broken software. A hard `s[:70]` did
    exactly that to the <title> of 26% of the bill pages.

    Also collapses whitespace: official titles arrive with newlines inside
    them, and a <title> with a newline in it is not a title.
    """
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    # n+1 so a cut that lands exactly on a space keeps the last whole word; a
    # single word longer than the budget is the one case that has to be cut.
    cut = s[:n + 1]
    cut = cut.rsplit(" ", 1)[0] if " " in cut else s[:n]
    cut = cut.rstrip(" ,;:.·-—«(")
    return (cut or s[:n]) + tail


def short_title(code, title, n=64):
    """What a shared link previews as: the identifier, then as much of the
    title as fits on a word boundary. The code alone does not say what the
    document is about; the title alone does not say which expediente it is."""
    t = clip(title, n)
    return f"{code} · {t}" if t else str(code)


def num(n):
    """14864 -> '14 864'. Peru writes thousands with a space, and a comma here
    would collide with the commas inside bill titles."""
    return f"{n:,}".replace(",", " ")


def soles(x, cents=False):
    """631414.32 -> «S/ 631 414». Sin céntimos salvo que se pidan: seis dígitos
    con dos decimales, ciento treinta veces seguidas, son ruido."""
    s = f"{x:,.2f}" if cents else f"{round(x):,}"
    return "S/\u00a0" + s.replace(",", " ")


def soles_m(x):
    """73 347 942 -> «S/ 73,3 M». Un agregado de ocho dígitos en un titular de
    tarjeta se parte en dos líneas y no se lee; los millones sí."""
    if abs(x) < 1_000_000:
        return soles(x)
    return f"S/\u00a0{x / 1_000_000:.1f}".replace(".", ",") + "\u00a0M"


# ------------------------------------------------------- identidad de bancada
#
# Six benches, one visual identity each, repeated on every page they appear on.
# It carries three channels at once, because no single one survives every
# reader: the party's own logo (recognisable, but too small to read at row
# height), a colour (fails for colour-blind readers), and a two-letter monogram
# (works when the image 404s and in a text browser).
#
# The colour is OURS, not the party's. Six real party colours cannot be a legal
# categorical palette. Two of these logos are green and two are red, so the
# colours come from a validated ramp and the logo carries the party's own brand.
# Where the two agree (JP verde, RP celeste->azul, FP naranja->latón) the slot
# was chosen to agree.
#
# Slots run in the order the palette was validated in, and the hemicycle seats
# benches around the arc in that same order, so the only bench colours that ever
# TOUCH are the adjacent pairs. That is not the same as the only pairs a reader
# ever compares, which is all fifteen. See the measurement below, and
# BENCH_SHAPE, which is the answer to it.
#
# The six are NOT a lightness ladder in slot order, and an earlier version of
# this comment recited a run of six luminance steps per mode that the palette
# has never had. What is actually there, as relative luminance in slot order:
#
#     light  .087 .161 .255 .071 .277 .132
#     dark   .164 .220 .287 .145 .315 .193
#
# Two rows demo() reads back out of this comment and recomputes, so they
# cannot go stale again. In greyscale the weakest pair that actually touches on
# the arc is 1.44:1 in light and 1.25:1 in dark. The previous ramp managed
# 1.29:1 and 1.16:1 and its comment argued at length that ~1.13:1 was an
# arithmetic ceiling in dark; it is not, and what was actually missing was
# spread: letting the six sit at deliberately different lightnesses inside the
# validator's band buys separation in greyscale, in CVD and in normal vision
# at once, at the cost of the six not being equally salient. For six contiguous
# blocks on an arc that is a cost worth paying.
#
#   validate_palette.js "#016129,#4170B6,#B77F31,#63348E,#029FAB,#9C4A74"
#        --mode light --surface "#FAFAF8"   -> ALL CHECKS PASS,
#        worst adjacent CVD ΔE 11.4 (deutan), normal-vision 22.6, chroma floor
#        clear, lightness band clear, contrast all >= 3:1 (--gp5 3.07:1 is the
#        tightest, and it is measured on --ground, the page, not on --raised)
#   validate_palette.js "#2D8046,#4581D9,#BF873A,#8058AB,#12A8B6,#B55C89"
#        --mode dark --surface "#12161A"    -> ALL CHECKS PASS, with the CVD
#        check at WARN: worst adjacent ΔE 7.8 (deutan), normal-vision 22.6.
#        7.8 sits in the 6–8 band the validator allows only WITH a second
#        encoding, and BENCH_SHAPE is that second encoding.
#
# The arc order was chosen by measuring all fifteen pairs in both modes and
# taking the path that maximises the weakest neighbouring pair, subject to the
# three slots where our colour and the party's own brand agree staying put.
#
# AND THE OLD CLAIM STOPPED ONE CHECK SHORT, so this one does not. Adjacent
# pairs are five of fifteen. Run the same validator with `--pairs all`:
#
#        light   worst all-pairs CVD ΔE 11.3, normal vision 14.1
#        dark    worst all-pairs CVD ΔE 7.7, normal vision 11.4
#        greyscale, worst of fifteen: 1.07:1 light, 1.08:1 dark
#
# Light now clears the all-pairs CVD floor of 8 by a wide margin and misses the
# normal-vision floor of 15 by 0.9; dark clears neither. The previous ramp
# scored 3.4 and 7.1 on that same test, and the validator's own reference ramp
# scores 3.2, so this is a real improvement and still not a pass. demo() ports
# the validator's arithmetic, Machado (2009) on linear RGB and ΔE in OKLab ×100,
# and recomputes those six numbers on every build, so this block cannot go back
# to claiming a pass it never had.
#
# So the fix is a channel, not a hue: BENCH_SHAPE gives every bench its own
# silhouette, drawn by the same `mark()` in the seat and in the legend, exactly
# as the roll call has done per vote state since the beginning. What the colour
# is still required to do on its own is clear 3:1 against --ground; the rest is
# carried by the shape, the logo, the monogram on the arc and the name.
BENCH = {
    # nombre completo:            (slot, monograma, archivo de logo)
    "Juntos por el Perú":        (1, "JP", "juntos-por-el-peru.png"),
    "Renovación Popular":        (2, "RP", "renovacion-popular.png"),
    "Fuerza Popular":            (3, "FP", "fuerza-popular.png"),
    "Partido del Buen Gobierno": (4, "PBG", "partido-del-buen-gobierno.jpg"),
    "Ahora Nación":              (5, "AN", "ahora-nacion.jpg"),
    "Partido Cívico Obras":      (6, "PCO", "partido-civico-obras.png"),
}
BENCH_ORDER = sorted(BENCH, key=lambda p: BENCH[p][0])
# Wikimedia Commons, licence checked file by file. Five are public domain as to
# copyright and one, Juntos por el Perú, is CC BY-SA 4.0, which obliges us to
# name the author and link the licence. The credit block lives here and every
# page that shows a logo links to it.
LOGO_CREDIT = "acerca.html#logos"

# The one thing about the seating plan a reader could not work out alone: we
# invented the order. It used to be printed under every arc on the site, six
# figures deep; now it is the figure's own title and one line on /acerca, and
# the seat itself says who sits in it when you point at it.
CURULES = ("El orden dentro de la sala es el de este sitio: el Congreso no "
           "publica el plano de curules.")


def _logo_credits():
    """Author, licence and source page per logo, from the file the downloader
    wrote next to the images. Read once; every page that shows a logo prints a
    line from it, so the attribution can never drift from the files."""
    import json
    for base in (ROOT / "assets" / "logos", OUT / "logos"):
        try:
            return json.loads((base / "creditos.json").read_text("utf-8"))
        except Exception:
            continue
    return {}


LOGO_CREDITS = _logo_credits()


def bench_slot(party):
    """1..6 for a known bench, 0 for anything else. Never invents a slot: a
    name we do not know gets the neutral treatment, not a seventh colour."""
    return BENCH.get((party or "").strip(), (0,))[0]


# One silhouette per bench, in slot order. The arc needs a second channel.
#
# Measured, not asserted: six hues cannot be told apart on all fifteen pairs.
# The palette validator, run with `--pairs all`, fails our ramp at ΔE 3.4
# (protanope, light) and its own reference ramp at 3.2, so this is arithmetic
# and not a picking problem. The roll call has always had a shape per vote
# state; the bench arc had none and colour was the only difference between six
# benches. It has one now, drawn by the same `mark()` in the seat and in the
# legend, so the two cannot drift apart.
#
# ponytail: five filled silhouettes plus one hollow ring. A seventh bench would
# need a seventh shape before it needs a seventh colour.
BENCH_SHAPE = {1: ("circle", 0), 2: ("square", 0), 3: ("diamond", 0),
               4: ("triangle", 0), 5: ("hex", 0), 6: ("ring", 1),
               # not one of the six: hollow, so it never reads as a bench
               0: ("square", 1)}


def bench_shape(party):
    """(shape, hollow) for a bench, as POS_SHAPE does for a vote."""
    return BENCH_SHAPE[bench_slot(party)]


def bench_mono(party):
    """'Fuerza Popular' -> 'FP'. Initials for a bench we do not have on file,
    so the monogram channel still works for an alliance formed mid-period."""
    p = (party or "").strip()
    if p in BENCH:
        return BENCH[p][1]
    words = [w for w in re.split(r"[\s-]+", p)
             if w and w.lower() not in ("de", "del", "la", "el", "por", "y", "los")]
    return ("".join(w[0] for w in words[:3]) or "—").upper()


def bench_logo(party, r="", size=26, cls=""):
    """The bench's mark at a fixed square size. Two shapes arrive here, a
    transparent PNG and a solid-background JPG, so both sit inside the same
    tile with `object-fit:contain` and neither one sets the row height.

    The monogram is not a fallback for a missing file, it is what renders when
    the bench has no logo on file at all: never an <img src="">."""
    p = (party or "").strip()
    s = f"gp{bench_slot(p)}"
    box = f"width:{size}px;height:{size}px;font-size:{step(size / 3):g}px"
    if p in BENCH:
        return (f'<span class="blogo {s} {cls}" style="{box}">'
                f'<img loading="lazy" decoding="async" src="{r}logos/{BENCH[p][2]}" '
                f'alt="" width="{size}" height="{size}"></span>')
    return (f'<span class="blogo mono {s} {cls}" style="{box}" aria-hidden="true">'
            f"{esc(bench_mono(p))}</span>")


# ----------------------------------------------------------------------- CSS

CSS = """
/* LA PALETA. El registro oficial, sobre papel bond, con luz fría.
   Lo que había antes, crema #F7F4EF, titulares en Georgia y un rojo terracota,
   es el tríptico que cualquier generador produce para cualquier encargo, y se
   nombra a simple vista. Éste sale del material del asunto: el Diario de los
   Debates impreso en bond casi blanco, el rojo institucional del Estado
   peruano, y la piedra gris azulada del Palacio Legislativo. La escala neutra
   es FRÍA de arriba abajo: tinta, texto secundario, filetes, superficies. El
   único calor de la página son dos acentos reservados, el rojo, que sólo
   aparece donde habla la institución o donde hay que actuar, y un dorado sobrio
   para lo que sigue en trámite. Dos temperaturas: eso es exactamente lo que le
   faltaba al crema, donde todo era el mismo marrón claro.

   Tres familias de color, separadas a propósito:
   · shell:   ground/raised/sunk/ink/muted/off/line/accent/gold. La página.
   · estado:  si/no/abst/blanco/aus/lic/pres, más ok/wait/dead/sen/vio para
              las etapas del trámite. Lo que una votación o una etapa
              SIGNIFICA. Reservados: nunca se reutilizan como color de serie,
              y nunca van solos. Cada estado lleva además su palabra y su
              figura. Además son texto de 13 px en las fichas, así que los
              catorce miden 4.5:1 o más contra --ground Y contra --raised en
              los dos temas; ese techo es lo que impide una escalera más
              ancha, y es la razón por la que el intento anterior falló.
   · bancada: gp1..gp6, QUIÉN. Rampa categórica validada, ver BENCH.
   Todo valor se declara primero en el bloque raíz a secas, así que ningún
   color existe sólo dentro de una media query. */
:root{
  --ground:#FAFAF8; --raised:#FFFFFF; --sunk:#EDEFF1; --line:#D8DDE1;
  --ink:#20262B; --muted:#525E68; --off:#78848D; --shade:#0000000D;
  --accent:#A6192E; --gold:#8A6A22;
  --ok:#1F6B4F; --wait:#8A6A22; --dead:#5D6870;
  --sen:#2E5079; --dip:#A6192E; --vio:#5A4A9E; --grid:#CFD5DA;
  --si:#1B6B47; --no:#A32A2A; --abst:#7A5A12; --blanco:#41636E;
  --aus:#5D6870; --lic:#2E5079; --pres:#5A4A9E;
  /* cada gp es un relleno sobre --ground, así que el 3:1 se mide ahí:
     el más ajustado es --gp5 con 3.07:1 y --gp3 con 3.30:1 */
  --gp1:#016129; --gp2:#4170B6; --gp3:#B77F31;
  --gp4:#63348E; --gp5:#029FAB; --gp6:#9C4A74; --gp0:#525E68;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#12161A; --raised:#1A2026; --sunk:#161B20; --line:#2C343B;
  --ink:#E6EAED; --muted:#9AA6B0; --off:#6E7A84; --shade:#FFFFFF0D;
  --accent:#E8737E; --gold:#D2A64B;
  --ok:#5FB394; --wait:#D2A64B; --dead:#8B959D;
  --sen:#7FA3D6; --dip:#E8737E; --vio:#A79BEA; --grid:#333C44;
  --si:#4FB187; --no:#EE7A7A; --abst:#D3A33C; --blanco:#93B3BD;
  --aus:#8B959D; --lic:#7FA3D6; --pres:#A79BEA;
  --gp1:#2D8046; --gp2:#4581D9; --gp3:#BF873A;
  --gp4:#8058AB; --gp5:#12A8B6; --gp6:#B55C89; --gp0:#9AA6B0;
}}
:root[data-theme="dark"]{
  --ground:#12161A; --raised:#1A2026; --sunk:#161B20; --line:#2C343B;
  --ink:#E6EAED; --muted:#9AA6B0; --off:#6E7A84; --shade:#FFFFFF0D;
  --accent:#E8737E; --gold:#D2A64B;
  --ok:#5FB394; --wait:#D2A64B; --dead:#8B959D;
  --sen:#7FA3D6; --dip:#E8737E; --vio:#A79BEA; --grid:#333C44;
  --si:#4FB187; --no:#EE7A7A; --abst:#D3A33C; --blanco:#93B3BD;
  --aus:#8B959D; --lic:#7FA3D6; --pres:#A79BEA;
  --gp1:#2D8046; --gp2:#4581D9; --gp3:#BF873A;
  --gp4:#8058AB; --gp5:#12A8B6; --gp6:#B55C89; --gp0:#9AA6B0;
}
/* LA ESCALA TIPOGRÁFICA. Nueve pasos y ni uno más:
     11 · 12.5 · 13.5 · 16 · 17 · 20 · 24 · 30 · 40
   Antes había diecisiete tamaños distintos en una sola ficha de parlamentario,
   nueve de ellos entre 9.5 y 15 px. Eso no es una escala sino una dispersión:
   nada de lo que se repite tiene el mismo peso dos veces y el lector no puede
   aprender a leer la página. Los papeles son fijos: 11 el monoespaciado de
   etiqueta, 12.5 los pies de figura, 13.5 la interfaz densa, 16 el cuerpo y el
   título de una fila, 17 la entradilla y el h3, 20/24 los encabezados, 30 las
   cifras grandes, 40 el h1. demo() lee la hoja y falla si aparece un décimo.
   El h1 y el h2 usan clamp() entre pasos de esta misma lista. */
*{box-sizing:border-box}
/* `all:unset` on the sortable headers deleted their focus ring, and the sheet
   had no outline rule anywhere. One place, every focusable thing. */
:focus-visible{outline:3px solid var(--accent);outline-offset:2px;border-radius:2px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;
  scroll-behavior:auto!important}}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--ground);color:var(--ink);overflow-x:hidden;
  font:16px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none;border-bottom:1px solid var(--line)}
a:hover{border-bottom-color:var(--accent);color:var(--accent)}
/* DOS PESOS, no cinco. <b> inside an already-600 label resolves to `bolder`,
   which is 900, and that put an 11/900 and a 13.5/700 on the page next to the
   11/600 and 13.5/600 they were supposed to match. */
b,strong{font-weight:600}
/* SIN SERIFA, EN NINGÚN SITIO. Los titulares iban en Georgia, que junto al
   fondo crema y al rojo terracota completaba el tríptico que delata a una
   página generada. Aquí el display es la misma grotesca del sistema apretada
   de prosa (-.03em en el h1), y va en monoespaciado todo lo que es CIFRA o
   RÓTULO: los números grandes, las iniciales de un retrato que falta, la marca
   del sitio. Ése es el idioma de un padrón, de un acta y de un expediente, que
   es lo que este sitio es, y no el de una revista. */
h1,h2,h3{font-family:inherit;font-weight:600;text-wrap:balance;margin:0}
h1{font-size:clamp(24px,4.2vw,40px);letter-spacing:-.03em;line-height:1.14}
/* A 91-character all-caps official title set at 40px is four lines of shouting.
   Long headlines step down to the second level of the scale instead. At 27px it
   was exactly the size of the stat value three blocks below it. Two different
   things at one size is not a hierarchy, so it sits a step lower and the stat
   value a step higher. */
h1.long{font-size:clamp(20px,2.6vw,24px);line-height:1.26;letter-spacing:-.016em}
/* h2 has to stay under h1.long's FLOOR, not under h1's ceiling: 82% of these
   pages have a long title, and a fixed 24px h2 was bigger than its own page
   title at every width below 810px. No first level of hierarchy at all on a
   phone. h1.long floors at 20px and h2 at 17px, and h2 only reaches 20px at
   1053px wide, where h1.long is already 24. */
h2{font-size:clamp(17px,1.9vw,20px);margin:0 0 16px;padding-bottom:9px;
  border-bottom:1px solid var(--line);letter-spacing:-.018em}
h3{font-size:17px;letter-spacing:-.014em}
.h4{font:600 13.5px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.14em;
  text-transform:uppercase;color:var(--muted);margin:0 0 10px}
p{margin:0 0 12px;max-width:70ch}
.wrap{max-width:1040px;margin:0 auto;padding:20px clamp(14px,3.5vw,28px) 64px}
/* El rótulo va en el dorado sobrio, no en el rojo: el rojo institucional se
   reserva para lo que la institución decide y para lo que se puede pulsar. */
.eyebrow{font:600 11px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;
  letter-spacing:.16em;text-transform:uppercase;color:var(--gold)}
.mut{color:var(--muted)}
.sm{font-size:13.5px}
/* LA NAVEGACIÓN, COMO ARQUITECTURA Y NO COMO BARRA.
   Ocho destinos plegaron a tres filas primero y a un scroller lateral después.
   El scroller no mentía, el degradado decía que había más, pero a 375 px seis
   de los ocho seguían fuera de pantalla y sólo se llegaba a ellos arrastrando,
   que no es un índice: es un cajón.
   El arreglo no es de estilo. Los ocho destinos responden a tres preguntas
   distintas, y agrupados por esa pregunta son 2 + 3 + 2:
       Qué se propone     → Proyectos · Mociones
       Quién lo hace      → Parlamentarios · Bancadas · Comisiones
       Qué pasa en el Pleno → Votaciones · Asistencia
   Por debajo de 900 px la barra lleva la marca y un <details> nativo que abre
   ese índice completo, con los tres rótulos y una línea que dice para qué
   sirve cada sitio: sin JavaScript, operable con teclado por ser <details>, y
   con cada destino a un toque de 44 px. Por encima de 900 px caben los ocho en
   una fila y el <details> desaparece; los grupos se marcan ahí con un filete
   entre uno y otro. Sólo uno de los dos existe a cada ancho (`display:none`
   saca al otro también del árbol de accesibilidad), así que ningún destino se
   anuncia dos veces. La portada repite la misma agrupación en «Empezar por
   aquí», que es la otra mitad del arreglo: las tareas viven en la página. */
nav.top{position:sticky;top:0;z-index:30;background:var(--raised);
  border-bottom:1px solid var(--line);display:flex;flex-wrap:nowrap;
  align-items:center;gap:2px;padding:0 clamp(8px,3vw,20px)}
/* A scroller with `scrollbar-width:none` and no fade hid six of its eight
   destinations at 375px: scrollWidth 776 in a 292px box, and the only cue that
   more existed was «Parlamentarios» being clipped mid-word by the Tema button.
   Two affordances, no JavaScript:
   · the classic CSS scroll shadow: two `local` gradients painted in the page
     colour that ride WITH the content and so uncover the two `scroll` shadows
     underneath only at the edge that still has something behind it. The fade
     appears on the right at rest, on both sides mid-scroll, on the left at the
     end: it is never a decoration lying about where the content is;
   · a real scrollbar for anyone with a mouse, which is where a hidden one is
     least excusable. A finger gets the fade, which never steals a row of height.
   `.links` is also the sticky nav's only scroll container, so nothing here can
   reintroduce horizontal overflow on the page itself. */
nav.top .links{display:flex;flex:1;min-width:0;gap:2px;align-items:center;
  overflow-x:auto;overflow-y:hidden;-webkit-overflow-scrolling:touch;
  scrollbar-width:none;scroll-snap-type:x proximity;
  background:
    linear-gradient(to right,var(--raised) 40%,transparent) 0 0/34px 100% no-repeat local,
    linear-gradient(to left,var(--raised) 40%,transparent) 100% 0/34px 100% no-repeat local,
    linear-gradient(to right,var(--line),transparent) 0 0/22px 100% no-repeat scroll,
    linear-gradient(to left,var(--line),transparent) 100% 0/22px 100% no-repeat scroll}
nav.top .links::-webkit-scrollbar{height:0}
/* a fixed height so the scrollbar rides INSIDE the 45px bar: everything that
   sticks below the nav is offset by 45, and a scrollbar that grew the bar
   would slide all of it out of register on desktop only. */
@media(pointer:fine){nav.top .links{scrollbar-width:thin;height:45px}
  nav.top .links::-webkit-scrollbar{height:6px}
  nav.top .links::-webkit-scrollbar-thumb{background:var(--line);border-radius:3px}}
nav.top .links>a{flex:none;white-space:nowrap;scroll-snap-align:start}
nav.top .links .sep{flex:none;width:1px;height:18px;background:var(--line);
  margin:0 7px}
nav.top a,nav.top button,nav.top summary{display:inline-flex;align-items:center;
  min-height:44px;padding:0 10px;border:0;background:none;color:inherit;
  font:600 13.5px/1 system-ui,sans-serif;cursor:pointer;
  border-bottom:2px solid transparent}
nav.top a:hover,nav.top button:hover,nav.top summary:hover{color:var(--accent);
  border-bottom-color:var(--accent)}
nav.top .brand{font:600 16px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
  letter-spacing:.01em;margin-right:4px;color:var(--ink);flex:none}
nav.top button{flex:none;margin-left:auto}
/* el índice desplegable: <details> nativo, sin una línea de JavaScript */
nav.top .menu{flex:none;margin-left:auto}
nav.top summary{list-style:none;gap:8px;white-space:nowrap}
nav.top summary::-webkit-details-marker{display:none}
nav.top summary::after{content:"";width:8px;height:8px;flex:none;
  border:2px solid currentColor;border-width:0 2px 2px 0;
  transform:rotate(45deg) translate(-2px,-2px)}
nav.top .menu[open]>summary::after{transform:rotate(225deg) translate(-1px,-1px)}
nav.top .menu[open]>summary{color:var(--accent);border-bottom-color:var(--accent)}
nav.top .panel{position:absolute;left:0;right:0;top:100%;background:var(--raised);
  border-bottom:1px solid var(--line);box-shadow:0 10px 24px var(--shade);
  padding:8px clamp(8px,3vw,20px) 16px;max-height:calc(100vh - 46px);
  overflow-y:auto;display:grid;gap:4px}
nav.top .mh{display:block;font:600 11px/1.4 ui-monospace,Menlo,monospace;
  letter-spacing:.14em;text-transform:uppercase;color:var(--gold);
  padding:14px 0 6px;border-bottom:1px solid var(--line);margin-bottom:2px}
nav.top .panel a{display:flex;flex-direction:column;align-items:flex-start;
  justify-content:center;gap:2px;min-height:52px;padding:8px 10px;
  border-bottom:0;border-left:3px solid transparent;width:100%;
  font:600 16px/1.3 system-ui,sans-serif;white-space:normal}
nav.top .panel a small{font:400 13.5px/1.35 system-ui,sans-serif;
  color:var(--muted)}
nav.top .panel a:hover{background:var(--sunk);border-left-color:var(--accent);
  border-bottom-color:transparent}
nav.top .panel a:hover small{color:var(--muted)}
/* Uno u otro, nunca los dos: `display:none` también saca el duplicado del
   árbol de accesibilidad, así que ningún destino se lee dos veces. El
   scroller y sus sombras siguen ahí por encima de 900 px como red de
   seguridad para la franja 900–1000, donde los ocho van justos. */
@media(max-width:899px){nav.top .links{display:none}}
@media(min-width:900px){nav.top .menu{display:none}}
.crumb{font:12.5px/1.5 ui-monospace,Menlo,monospace;color:var(--muted);margin:18px 0 10px;
  word-break:break-word}
.crumb a{border:0}
.lede{font-size:17px;color:var(--ink);max-width:66ch;margin:14px 0 0}
section{margin:34px 0}
.card{background:var(--raised);border:1px solid var(--line);border-radius:2px;padding:18px 20px}
/* align-items:start: sin él la celda corta se estira a la altura de la larga
   y la portada enseña media tarjeta de aire (19 nombres de la Cámara contra
   12 mesas del Senado a tres líneas cada una). */
.grid{display:grid;gap:14px;align-items:start}
@media(min-width:760px){.g2{grid-template-columns:1fr 1fr}.g3{grid-template-columns:repeat(3,1fr)}}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.chip{display:inline-block;font:600 11px/1 ui-monospace,Menlo,monospace;letter-spacing:.1em;
  text-transform:uppercase;border:1px solid currentColor;border-radius:2px;padding:5px 7px;
  white-space:nowrap;vertical-align:middle}
.chip.d{color:var(--dip)} .chip.s{color:var(--sen)} .chip.c{color:var(--muted)}
.chip.ok{color:var(--ok)} .chip.wait{color:var(--wait)} .chip.dead{color:var(--dead)}
.chip.now{color:var(--accent)}
.chip.si{color:var(--si)} .chip.no{color:var(--no)} .chip.abst{color:var(--abst)}
/* la F de faltó va en rojo: es la única marca de la lista que acusa */
.chip.falta{color:var(--no)}
.gp1{color:var(--gp1)}.gp2{color:var(--gp2)}.gp3{color:var(--gp3)}
.gp4{color:var(--gp4)}.gp5{color:var(--gp5)}.gp6{color:var(--gp6)}.gp0{color:var(--gp0)}
.dotmark{display:inline-block;width:9px;height:9px;border-radius:50%;background:currentColor;
  margin-right:6px;vertical-align:baseline}
/* --- identidad de bancada: logo + color + monograma, siempre los tres ------ */
.blogo{display:inline-flex;align-items:center;justify-content:center;flex:none;
  border-radius:5px;background:var(--raised);border:1px solid var(--line);
  overflow:hidden;vertical-align:middle;box-shadow:inset 0 0 0 1px var(--shade)}
.blogo img{width:100%;height:100%;object-fit:contain;display:block}
.blogo.mono{font:700 11px/1 ui-monospace,Menlo,monospace;letter-spacing:.02em;
  color:currentColor;background:var(--sunk)}
.bchip{display:inline-flex;align-items:center;gap:8px;min-height:44px;padding:4px 10px 4px 4px;
  border:1px solid var(--line);border-radius:999px;background:var(--raised);
  font:600 13.5px/1.2 system-ui,sans-serif;max-width:100%;border-bottom:1px solid var(--line)}
.bchip:hover{border-color:currentColor}
.bchip .nm{color:var(--ink)}
.bchip .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bchip .rule{width:4px;align-self:stretch;border-radius:2px;background:currentColor;flex:none;
  margin:2px 0}
a.bchip{border-bottom:1px solid var(--line)}
a.bchip:hover{border-bottom-color:currentColor}
/* --- retratos ------------------------------------------------------------- */
.face{display:block;background:var(--sunk);border:1px solid var(--line);
  border-radius:4px;flex:none}
.shot{position:relative;overflow:hidden}
.shot>i{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  color:var(--muted);font:600 16px/1 ui-monospace,Menlo,monospace;font-style:normal;
  letter-spacing:.04em}
.shot>img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.noface{display:flex;align-items:center;justify-content:center;background:var(--sunk);
  border:1px solid var(--line);border-radius:4px;color:var(--muted);
  font:600 16px/1 ui-monospace,Menlo,monospace;letter-spacing:.04em;flex:none}
/* La cara dentro de una fila: 26×32. Declarado DESPUÉS de .face/.noface a
   propósito. Con la misma especificidad gana la última, y así fue como las
   clases de figura del hemiciclo perdieron una vez. */
.fmini{width:26px;height:32px;border-radius:3px}
.fmini>i,.noface.fmini{font-size:11px;letter-spacing:.02em}
/* Nombre + cara, la pieza que se repite en cuadros, coautorías y firmas */
.pchip{display:inline-flex;align-items:center;gap:8px;min-height:44px;
  padding:3px 12px 3px 4px;border:1px solid var(--line);border-radius:999px;
  background:var(--raised);font:600 13.5px/1.25 system-ui,sans-serif;
  max-width:100%;vertical-align:middle}
/* El nombre va en tinta, nunca en el color de su bancada: --gp5 sobre blanco
   mide 3.21:1 y esto es texto de 13.5 px. La bancada la dice el filete y el
   logotipo, igual que en .bchip. */
.pchip .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;
  color:var(--ink)}
.pchip.pl,.pchip.pl .nm{color:var(--muted);font-weight:400}
a.pchip:hover{border-color:var(--accent);color:var(--accent)}
a.pchip:hover .nm{color:var(--accent)}
/* y los tres sitios cuyas reglas de «enlace = 44 px de alto» ganarían por
   especificidad y lo devolverían a inline-block */
.roll li a.pchip,.sign a.pchip,td a.pchip,#t td a.pchip{display:inline-flex;
  padding:3px 12px 3px 4px;line-height:1.25;min-height:44px}
/* --- hemiciclo ------------------------------------------------------------ */
/* The box, not the <svg>, carries the width cap: the hover layer is positioned
   against it and has to be exactly the same rectangle as the drawing. */
.hemibox{position:relative;max-width:660px;margin:0 auto}
.hemi{display:block;width:100%;height:auto;overflow:visible}
.hemi a:hover .seat{stroke:var(--ink);stroke-width:3}
.hemi .seat.me{stroke:var(--accent);stroke-width:4}
.hemi a{border:0}
/* LA TARJETA DEL ESCAÑO. Un escaño con el puntero encima dice quién lo ocupa,
   con su cara y la marca de su bancada: es lo que antes ponía un párrafo
   debajo del diagrama explicando cómo leerlo.
   Sin JavaScript y sin <title>. Cada enlace cubre la caja entera para que la
   tarjeta pueda salir siempre en el mismo sitio, el hueco del arco, con
   tamaño en px y no en unidades del viewBox; el cuadradito <i> es la única
   parte que recibe el puntero, así que 190 enlaces apilados no se tapan.
   `display:none` no es sólo para esconderla: un <img loading="lazy"> dentro de
   una caja sin caja de formato no se descarga, y por eso los 190 retratos no
   se piden hasta que uno hace falta. */
.seatlayer{position:absolute;inset:0;pointer-events:none}
.seatlayer .sl{position:absolute;inset:0;border:0;pointer-events:none}
.seatlayer .sl>i{position:absolute;transform:translate(-50%,-50%);
  pointer-events:auto;border-radius:50%;cursor:pointer}
.seatlayer .sl:hover>i{box-shadow:0 0 0 2px var(--ink)}
.seatlayer .sl:hover,.seatlayer .sl:focus-visible{z-index:5}
.pop{display:none;position:absolute;left:50%;top:56%;
  transform:translate(-50%,-50%);width:250px;box-sizing:border-box;
  gap:10px;align-items:center;padding:9px 12px 9px 9px;
  background:var(--raised);border:1px solid var(--line);border-radius:6px;
  box-shadow:0 6px 22px rgba(0,0,0,.22);text-align:left}
.sl:hover .pop,.sl:focus-visible .pop,.sl:focus .pop{display:flex}
.pop .popt{min-width:0;display:flex;flex-direction:column;gap:4px}
.pop b{color:var(--ink);font:600 13.5px/1.2 system-ui,sans-serif;
  overflow-wrap:anywhere}
.pop .pb,.pop .pn{display:flex;align-items:center;gap:6px;color:var(--muted);
  font:400 11px/1.3 system-ui,sans-serif;min-width:0}
.pop .pb{overflow:hidden}
.pop .pn{font-weight:600;color:var(--ink)}
.hemi .cut{stroke:var(--muted);stroke-width:1.6;stroke-dasharray:7 6;opacity:.8}
.hemi .glab{fill:var(--ink);font-family:ui-monospace,Menlo,monospace;font-weight:600;
  letter-spacing:.08em}
.hemi .hole{fill:var(--muted);font-family:ui-monospace,Menlo,monospace;
  font-weight:600;text-anchor:middle;letter-spacing:.1em;text-transform:uppercase}
.hemi .holen{fill:var(--ink);font-weight:600;letter-spacing:-.01em;
  text-anchor:middle}
/* On a phone a 10px seat cannot be a 44px tap target and never will be. 130 of
   them need 250 000 px² and the viewport gives 94 000. So below 720px the
   seats stop being controls at all and the arc is a picture.
   This conforms under WCAG 2.5.8 by the *Equivalent* exception, not by any
   input-device rule. There is none; 2.5.8 applies to a mouse exactly as it
   applies to a finger. The exception holds because the roster further down the
   same page offers every one of the same links at 44px. If that roster ever
   stops being rendered, this rule stops being conformant with it. */
@media(max-width:719px){.hemi a{pointer-events:none}.seatlayer{display:none}}
/* The legend swatch that decodes a seat is an SVG drawn by `swatch()`, which
   calls the same SHAPE table the seat does. See the note there for why it is
   no longer a <span> with a shape class. */
.hemifig{margin:0}
.hemifig figcaption{color:var(--muted);font-size:13.5px;margin-top:10px;max-width:66ch}
.halls{display:grid;gap:32px}
@media(min-width:860px){.halls{grid-template-columns:1fr 1fr;gap:36px}}
.hall{border:1px solid var(--line);border-radius:6px;background:var(--raised);
  padding:18px 18px 16px}
.hall h3{margin:0 0 8px}
.hall .hemibox{max-width:520px;margin-top:6px}
/* --- gráficos ------------------------------------------------------------- */
.chart{display:block;width:100%;height:auto;font:12.5px/1 system-ui,sans-serif}
.chart text{fill:var(--muted);font-variant-numeric:tabular-nums}
.chart .val{fill:var(--ink);font-weight:600}
.chart .ax{stroke:var(--grid);stroke-width:1}
.chart .mark{stroke:var(--raised);stroke-width:2}
.chart .mark:hover{stroke:var(--ink)}
.figwrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0}
.figwrap>svg{min-width:320px}
figure{margin:0}
figcaption{color:var(--muted);font-size:13.5px;line-height:1.55;margin-top:10px;max-width:70ch}
/* --- leyenda compartida por barras, hemiciclo y columnas ------------------- */
.key{display:grid;grid-template-columns:repeat(auto-fit,minmax(184px,1fr));
  gap:0 18px;margin-top:10px;padding:0;list-style:none;font-size:13.5px}
.key li{display:flex;align-items:center;gap:8px;min-height:44px;min-width:0;
  padding:4px 0;line-height:1.35}
.key li>a{min-width:0;overflow-wrap:anywhere}
.key li>b,.key li>span.mut{white-space:nowrap;flex:none}
.key a{border:0;display:inline-flex;align-items:center;min-height:44px}
.key a:hover{color:currentColor;text-decoration:underline}
.key b{font-variant-numeric:tabular-nums;font-weight:600}
.key .sw{width:15px;height:15px;flex:none;overflow:visible}
/* The plain colour chip: a bench, whose identity is the logo and the name next
   to it. Written as span.sw, not .sw, so it cannot repaint the SVG swatches. */
.key span.sw{border-radius:3px;background:currentColor}
/* The striped chip matches the striped segment in the bar. Specificity above
   span.sw on purpose: an equal-specificity rule ordered before it is exactly
   how the shape classes used to lose. */
.key span.sw.sw-hatch{background:repeating-linear-gradient(45deg,
  currentColor 0 3px,transparent 3px 6px)}
/* --- retícula de bancadas / fichas ---------------------------------------- */
.people{list-style:none;margin:0;padding:0;display:grid;gap:10px;
  grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.people li{border:1px solid var(--line);border-radius:4px;background:var(--raised);
  overflow:hidden}
.people a{display:flex;flex-direction:column;gap:0;border:0;min-height:44px}
.people a:hover{background:var(--sunk)}
.people .ph{width:100%;aspect-ratio:4/5;background:var(--sunk);display:block;
  position:relative;overflow:hidden}
.people .ph>i{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;color:var(--muted);font:600 20px/1 ui-monospace,Menlo,monospace;
  font-style:normal}
.people .ph>img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
  display:block}
.people .noph{width:100%;aspect-ratio:4/5;display:flex;align-items:center;justify-content:center;
  background:var(--sunk);color:var(--muted);font:600 20px/1 ui-monospace,Menlo,monospace}
.people .cap{padding:9px 10px 11px;display:block}
.people .nm2{display:block;font:600 13.5px/1.3 system-ui,sans-serif;color:var(--ink)}
.people .sub{display:flex;align-items:center;gap:6px;margin-top:6px;color:var(--muted);
  font-size:11px}
.people .bar{height:4px;border-radius:0;border:0;background:currentColor;display:block}
/* --- una barra por bancada ------------------------------------------------ */
.benchrows{display:grid;gap:2px}
.brow{display:grid;grid-template-columns:1fr;gap:4px 12px;align-items:center;
  padding:8px 0;border-bottom:1px solid var(--line)}
.brow:last-child{border-bottom:0}
@media(min-width:640px){.brow{grid-template-columns:minmax(160px,220px) 1fr 44px 120px}}
.bl{display:inline-flex;align-items:center;gap:9px;min-height:44px;border:0;
  font:600 13.5px/1.25 system-ui,sans-serif}
.bl .nm{color:var(--ink)}
.bl::before{content:"";width:4px;align-self:stretch;margin:3px 0;border-radius:2px;
  background:currentColor;flex:none}
.bl .nm{overflow:hidden;text-overflow:ellipsis}
.brow .bar{height:16px}
.brow .barw{min-width:14px}   /* el largo de la barra es el tamaño de la bancada */
.brow .bn{font-variant-numeric:tabular-nums;font-weight:600;font-size:13.5px;text-align:right}
.brow .bs{font-size:12.5px;color:var(--muted)}
td.who2{padding:4px 12px}
/* estado de un voto: color reservado + punto + palabra, nunca color a secas */
.vote{display:inline-flex;align-items:center;font:600 13.5px/1.3 system-ui,sans-serif;
  white-space:nowrap}
.vote.si{color:var(--si)}.vote.no{color:var(--no)}.vote.abst{color:var(--abst)}
.vote.dead{color:var(--aus)}.vote.wait{color:var(--muted)}
/* --- padrón: buscador pegado arriba, riel de bancadas, secciones ---------- */
.stick{position:sticky;top:46px;z-index:20;background:var(--ground);
  padding:12px 0 8px;margin:0 0 12px;border-bottom:1px solid var(--line)}
.stick .filters{margin:0 0 8px}
.rail{display:flex;flex-wrap:wrap;gap:6px}
.railb{display:inline-flex;align-items:center;gap:8px;min-height:44px;padding:4px 12px 4px 5px;
  border:1px solid var(--line);border-radius:999px;background:var(--raised);
  font:600 12.5px/1.2 system-ui,sans-serif}
.railb b{font-variant-numeric:tabular-nums;color:var(--muted)}
.railb:hover{border-color:currentColor}
.railb span{color:var(--ink)}
.bsec{margin:30px 0 0;scroll-margin-top:120px}
.bsech{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:0 0 14px;
  padding:0 0 10px;border-bottom:3px solid currentColor;font-size:17px}
.bsech a{color:var(--ink);border:0;display:inline-flex;align-items:center;
  min-height:44px}
.bsech .cnt{font:400 12.5px/1.4 system-ui,sans-serif;color:var(--muted);
  margin-left:auto;font-family:ui-monospace,Menlo,monospace}
/* --- índice de bancadas --------------------------------------------------- */
.bgrid{list-style:none;margin:0;padding:0;display:grid;gap:14px;
  grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
.bgrid li{border:1px solid var(--line);border-radius:6px;background:var(--raised);
  padding:16px 16px 14px}
.bgrid li:hover{border-color:currentColor}
.bgrid a{display:flex;align-items:center;gap:14px;border:0;min-height:44px;
  margin-bottom:14px}
.bgrid .t2{flex:1;min-width:0}
.bgrid .nm2{display:block;font:600 16px/1.25 system-ui,sans-serif;color:var(--ink)}
.bgrid .sub{display:block;color:var(--muted);font-size:12.5px;margin-top:3px}
.bgrid .big{font:600 30px/1 ui-monospace,Menlo,monospace;letter-spacing:-.03em;font-variant-numeric:tabular-nums;
  color:currentColor}
.bhead{display:flex;align-items:center;gap:18px;margin:6px 0 0}
.bhead .eyebrow{display:block;margin-bottom:4px}
/* --- tira de asistencia: color + letra, nunca color solo ------------------ */
.cal{display:flex;flex-wrap:wrap;gap:18px 26px}
.mo{min-width:0}
.mol{display:block;font:600 11px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.13em;
  text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.mol b{color:var(--ink)}
.cells{display:flex;flex-wrap:wrap;gap:6px}
.cal .cell{width:46px;height:46px;border:1.5px solid currentColor;border-radius:5px;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  background:var(--raised);text-decoration:none}
.cal .cell b{font:700 16px/1 ui-monospace,Menlo,monospace}
.cal .cell span{font:400 11px/1.2 ui-monospace,Menlo,monospace;color:var(--muted)}
.cal .cell:hover{background:var(--sunk)}
/* --- tiras de comparación ------------------------------------------------- */
.strip{margin:0 0 20px}
.strip .lab{margin:0 0 4px;color:var(--ink);font:600 13.5px/1.4 system-ui,sans-serif;
  display:flex;justify-content:space-between;gap:14px;align-items:baseline;max-width:640px}
.strip .lab b{font:600 20px/1 ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
.strip svg{max-width:640px}
.chart .pk{stroke:var(--muted);stroke-width:2}
.chart .pkbar{fill:var(--muted)}
.chart .med{stroke:var(--muted);stroke-width:1.5;stroke-dasharray:3 3}
.chart .me{fill:var(--accent);stroke:var(--raised);stroke-width:2.5}
.strip p{margin:2px 0 0}
/* --- cabecera de ficha ---------------------------------------------------- */
.profile{display:grid;gap:18px;grid-template-columns:1fr;align-items:start;margin-top:20px}
@media(min-width:620px){.profile{grid-template-columns:132px 1fr;gap:24px}}
.profile .port{width:132px;height:165px;max-width:100%}
.profile .meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;align-items:center}
.tiles{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin:0}
.tile{border:1px solid var(--line);border-radius:4px;background:var(--raised);padding:14px 15px}
.tile dt{font:600 11px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.13em;
  text-transform:uppercase;color:var(--muted)}
.tile dd{margin:6px 0 0;font:600 30px/1.05 ui-monospace,Menlo,monospace;
  font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.tile dd small{display:block;font:400 12.5px/1.5 system-ui,sans-serif;color:var(--muted);
  letter-spacing:0;margin-top:5px}
.stat{display:flex;flex-wrap:wrap;gap:12px 30px;margin:0}
.stat div{min-width:104px}
.stat dt{font:600 11px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.13em;
  text-transform:uppercase;color:var(--muted)}
.stat dd{margin:2px 0 0;font:600 30px/1.1 ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums;
  letter-spacing:-.02em}
.stat dd small{font:400 12.5px/1.4 system-ui,sans-serif;color:var(--muted);display:block;
  letter-spacing:0}
/* legislative track */
.track{list-style:none;margin:0;padding:0;display:grid;gap:0}
.track li{position:relative;padding:0 0 18px 26px;border-left:2px solid var(--line)}
.track li:last-child{border-left-color:transparent;padding-bottom:0}
.track li .dot{position:absolute;left:-9px;top:2px;width:16px;height:16px;border-radius:50%;
  background:var(--ground);border:2px solid var(--line)}
.track li.done{border-left-color:var(--ok)}
.track li.done .dot{background:var(--ok);border-color:var(--ok)}
.track li.now{border-left-style:dashed;border-left-color:var(--accent)}
.track li.now .dot{border-color:var(--accent);border-width:5px;width:18px;height:18px;left:-10px}
.track li.todo{border-left-style:dashed}
.track li.todo .name,.track li.todo .why{color:var(--muted)}
.track li.stop{border-left-color:var(--dead)}
.track li.stop .dot{background:var(--dead);border-color:var(--dead)}
.track .name{font:600 16px/1.3 system-ui,sans-serif;display:block}
.track .why{display:block;font-size:13.5px;color:var(--muted);max-width:56ch;margin-top:2px}
.track .when{font:600 11px/1 ui-monospace,Menlo,monospace;color:var(--muted);
  letter-spacing:.06em;text-transform:uppercase}
@media(min-width:860px){
  .track{grid-auto-flow:column;grid-auto-columns:1fr;gap:0}
  .track li{padding:26px 14px 0 0;border-left:0;border-top:2px solid var(--line)}
  .track li:last-child{border-top-color:var(--line);padding-right:0}
  .track li.done{border-top-color:var(--ok)}
  .track li.now{border-top-style:dashed;border-top-color:var(--accent)}
  .track li.todo{border-top-style:dashed}
  .track li.stop{border-top-color:var(--dead)}
  .track li .dot{left:0;top:-9px}
  .track li.now .dot{left:-1px;top:-10px}
}
/* dossier timeline */
.tl{list-style:none;margin:0;padding:0}
.tl li{display:grid;grid-template-columns:1fr;gap:2px;padding:14px 0;
  border-bottom:1px solid var(--line)}
.tl li:last-child{border-bottom:0}
@media(min-width:700px){.tl li{grid-template-columns:150px 1fr;gap:4px 20px}}
.tl .when{font:600 11px/1.6 ui-monospace,Menlo,monospace;color:var(--muted);
  letter-spacing:.06em;text-transform:uppercase}
.tl .what{font-weight:600}
.tl .why{color:var(--muted);font-size:13.5px;max-width:62ch;margin-top:3px}
.doc{font:600 11px/1 ui-monospace,Menlo,monospace;letter-spacing:.08em;
  text-transform:uppercase;color:var(--accent);display:inline-block;margin-top:4px;
  padding:14px 0;min-height:44px}
.kv{margin:0;display:grid;gap:12px 24px}
@media(min-width:620px){.kv{grid-template-columns:1fr 1fr}}
.kv dt{font:600 11px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.13em;
  text-transform:uppercase;color:var(--muted)}
.kv dd{margin:2px 0 0}
.kv dd small{display:block;color:var(--muted);font-size:12.5px;line-height:1.5}
.who{display:flex;gap:14px;align-items:flex-start}
.who img{width:64px;height:80px;object-fit:cover;border:1px solid var(--line);
  border-radius:2px;background:var(--sunk);flex:none}
.who .nm{font-size:17px;font-weight:600;letter-spacing:-.012em;display:block}
.roll{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:8px 10px}
/* min-width:0 en el ítem flexible: sin esto «María de los Milagros Jackeline
   Jauregui Martínez» no se encoge, porque el mínimo por defecto de un ítem flex
   es su contenido, y sacaba la página 110 px a la derecha a 375. Con esto el nombre
   se corta con puntos suspensivos dentro de su ficha. */
.roll>li{font-size:13.5px;min-width:0;max-width:100%}
.roll li a{display:inline-block;padding:11px 0;min-height:44px}
.roll .rank{font:600 11px/1 ui-monospace,Menlo,monospace;color:var(--muted)}
.scroll{overflow:auto;max-height:78vh;border:1px solid var(--line);border-radius:2px;
  background:var(--raised);-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13.5px}
.scroll table{min-width:560px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font:600 11px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.13em;text-transform:uppercase;
  color:var(--muted);position:sticky;top:0;background:var(--raised);z-index:2;
  white-space:nowrap;box-shadow:inset 0 -1px 0 var(--line)}
th button{all:unset;cursor:pointer;display:block;width:100%;min-height:44px;
  line-height:44px;margin:-10px 0;font:inherit;color:inherit}
th button:hover{color:var(--accent)}
tbody tr:hover{background:var(--sunk)}
/* `top` here is measured inside .scroll, which is the scroll container, NOT
   against the viewport: 0 for the column headers, 45 for the group rows that
   stack under them. Setting 45/90 to clear the page nav pushed the header down
   inside its own box instead. */
tr.grp td{background:var(--sunk);font:600 11px/1.4 ui-monospace,Menlo,monospace;
  letter-spacing:.12em;text-transform:uppercase;position:sticky;top:45px;z-index:1}
.scroll{scroll-padding-top:45px}
td.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
td.nw{white-space:nowrap}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px}
.filters input,.filters select{min-height:44px;padding:0 12px;font:16px system-ui,sans-serif;
  color:var(--ink);background:var(--raised);border:1px solid var(--line);border-radius:2px}
.filters input{flex:1 1 220px;min-width:0}
.bar{display:flex;height:14px;border-radius:2px;overflow:hidden;border:1px solid var(--line);
  background:var(--sunk)}
.bar i{display:block;height:100%}
.prov{border-top:1px solid var(--line);margin-top:44px;padding-top:16px;color:var(--muted);
  font-size:13.5px}
.crumb a,.prov a{display:inline-block;padding:13px 0;line-height:18px}
/* «Inicio» is 43px of text; a tap target is 44. Padding it out and pulling the
   margin back keeps the crumb reading as one line while the target is legal. */
.crumb a{padding:13px 5px;margin:0 -5px;min-width:44px;text-align:center}
#t td{height:44px;padding:0 12px}
td a{display:inline-block;min-height:44px;line-height:24px;padding:10px 0}
#t td a{display:block;line-height:44px;padding:0}
a.chip{min-height:44px;display:inline-flex;align-items:center}
.sign a{display:inline-block;min-height:44px;line-height:22px;padding:11px 0}
.who a.nm{min-height:44px;display:inline-flex;align-items:center}
.roll.signers{gap:6px 8px}
.roll.signers li a,.roll.signers li .pl{display:inline-flex;align-items:center;gap:8px;
  min-height:44px;padding:5px 12px 5px 6px;border:1px solid var(--line);border-radius:999px;
  background:var(--raised);font-size:13.5px}
.roll.signers li .pl{color:var(--muted);padding-left:12px}
.roll.signers li a:hover{border-color:currentColor;color:currentColor}
/* «y 13 más» era un callejón sin salida: decía cuántos faltaban y no quiénes.
   Son 233 firmas en todo el registro, así que las que no caben van en la propia
   página y sólo se muestran al apuntar. `focus-within` con el disparador
   tabulable es lo que lo hace funcionar con teclado y con un dedo. */
.more{position:relative}
.more>.pl{cursor:help;text-decoration:underline dotted;text-underline-offset:3px}
.mpop{display:none;position:absolute;left:0;top:calc(100% + 4px);z-index:9;
  width:min(300px,84vw);max-height:280px;overflow-y:auto;padding:8px 10px;
  background:var(--raised);border:1px solid var(--line);border-radius:6px;
  box-shadow:0 6px 22px rgba(0,0,0,.22)}
.more:hover .mpop,.more:focus-within .mpop{display:block}
.mpop ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px}
.mpop li{display:flex;align-items:center;gap:7px;min-width:0;
  font:400 12.5px/1.3 system-ui,sans-serif;color:var(--ink)}
.mpop li a{color:var(--ink);text-decoration:none;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.mpop li a:hover{color:var(--accent)}
/* Mesa directiva: tres cargos, tres logotipos, tres nombres. El cargo va en
   versalitas a la izquierda porque lo que se compara entre comisiones es quién
   preside, no la lista entera. */
.mesa{list-style:none;margin:6px 0 0;padding:0;display:flex;flex-wrap:wrap;
  gap:4px 16px}
.mesa li{display:flex;align-items:center;gap:6px;min-width:0;
  font:400 12.5px/1.35 system-ui,sans-serif;color:var(--ink)}
.mesa .ro{font:600 11px/1 ui-monospace,Menlo,monospace;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em}
.mesa a{color:var(--ink);text-decoration:none;border-bottom:1px solid var(--line)}
.mesa a:hover{color:var(--accent);border-color:currentColor}
.cttelist{list-style:none;margin:10px 0 0;padding:0;display:flex;
  flex-direction:column}
.cttelist>li{padding:10px 0;border-top:1px solid var(--line)}
.cttelist>li:first-child{border-top:0}
.cttelist>li>.t{display:block;font:600 13.5px/1.3 system-ui,sans-serif;
  color:var(--ink);text-decoration:none}
.cttelist>li>.t:hover{color:var(--accent)}
.cttelist.tight>li{padding:6px 0}
.why a,.kv dd a{display:inline-block;min-height:44px;line-height:22px;padding:11px 0}
/* A link inside running prose is still a tap target on a phone. */
p a,.lede a,.note a,figcaption a{display:inline-block;min-height:44px;line-height:22px;
  padding:11px 0}
.gloss{border:1px solid var(--line);border-radius:2px;margin:0 0 14px;background:var(--raised)}
.gloss summary{cursor:pointer;padding:12px 16px;min-height:44px;display:flex;
  align-items:center;font:600 13.5px/1.4 system-ui,sans-serif}
.gloss dl,.gloss>p{padding:0 16px 16px;margin:0}
.gloss dd{font-size:13.5px;color:var(--muted)}
.prov b{color:var(--ink)}
.prov,.prov a{overflow-wrap:anywhere}   /* PDF filenames are 60 characters long */
.prov code{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;word-break:break-all}
code{overflow-wrap:anywhere}
.pager{display:flex;flex-wrap:wrap;gap:6px;margin-top:18px}
.pager a,.pager span{min-height:44px;min-width:44px;display:inline-flex;align-items:center;
  justify-content:center;padding:0 10px;border:1px solid var(--line);border-radius:2px;
  font:600 13.5px/1 ui-monospace,Menlo,monospace}
/* White on the accent is 3.63:1 in dark mode, and 13px text needs 4.5:1. The page
   ground reads on both accents: 6.73:1 light, 5.22:1 dark. */
.pager .on{background:var(--accent);color:var(--ground);border-color:var(--accent)}
.pager .mut{border-color:transparent;font-weight:400}
.facets{display:flex;flex-wrap:wrap;gap:8px}
.facets a{border:1px solid var(--line);border-radius:2px;padding:10px 12px;min-height:44px;
  display:inline-flex;align-items:center;gap:8px;font-size:13.5px}
.facets a b{font-variant-numeric:tabular-nums}
/* facetas como gráfico de barras: 5 545 y 1 no pueden pesar lo mismo */
.ranked{list-style:none;margin:0;padding:0;display:grid;gap:2px}
.ranked li{border-bottom:1px solid var(--line)}
.ranked li:last-child{border-bottom:0}
.ranked a{display:grid;grid-template-columns:1fr 64px 96px;gap:4px 12px;
  align-items:center;min-height:44px;padding:6px 0;border:0}
.ranked a:hover{color:inherit}
.ranked a:hover .lbl{color:var(--accent)}
@media(min-width:640px){.ranked a{grid-template-columns:minmax(150px,1.1fr) 2fr 64px 96px}}
.ranked .lbl{font-size:13.5px;line-height:1.3;grid-column:1/-1}
/* La fila que nombra una bancada lleva su logotipo, no sólo su nombre: es la
   misma marca que el escaño y la leyenda. Clase propia para no tocar las
   listas de facetas, que son texto y nada más. */
.ranked .bl{display:flex;align-items:center;gap:8px;min-width:0}
/* El nombre se parte en dos líneas antes que recortarse: «Partido del Buen
   Gobierno» y «Partido del Buen Gobierno» abreviados igual son la misma fila
   dos veces, y la columna mide 150 px dentro de una tarjeta. */
.ranked .bl>span{min-width:0;overflow-wrap:anywhere}
@media(min-width:640px){.ranked .lbl{grid-column:auto}}
.ranked .track{background:var(--sunk);border-radius:3px;height:14px;overflow:hidden;
  border:1px solid var(--line)}
.ranked .track i{display:block;height:100%}
.ranked .n{font:600 13.5px/1 ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums;
  text-align:right}
.ranked .pc{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums;
  text-align:right}
/* El ranking de gasto tiene una columna más (el puesto) y una barra de dos
   tramos, así que se coloca por áreas en vez de heredar la rejilla de arriba.
   `display:flex` en la pista: con el `display:block` que heredaba, el tramo de
   viajes se dibujaba DEBAJO del de planilla y desaparecía bajo el recorte. */
.ranked.gasto a{grid-template-columns:26px minmax(0,1fr) 104px}
.ranked.gasto .rk{grid-area:1/1;font:600 12.5px/1 ui-monospace,Menlo,monospace;
  color:var(--muted);font-variant-numeric:tabular-nums}
.ranked.gasto .lbl{grid-area:1/2;display:flex;align-items:center;gap:8px;
  flex-wrap:wrap;color:var(--ink)}
.ranked.gasto .lbl small{flex-basis:100%;margin-left:34px}
.ranked.gasto .n{grid-area:1/3}
.ranked.gasto .track{grid-area:2/2/3/4;display:flex}
@media(min-width:640px){
  .ranked.gasto a{grid-template-columns:26px minmax(150px,1.2fr) minmax(0,1.6fr) 104px}
  .ranked.gasto .track{grid-area:1/3}
  .ranked.gasto .n{grid-area:1/4}}
.feed{list-style:none;margin:0;padding:0}
/* `>`, no descendiente: las fichas de firmantes son <li> dentro de un <li> de
   .feed, y con `.feed li` cada una se llevaba el filete inferior de una fila
   del listado. */
.feed>li{padding:6px 0;border-bottom:1px solid var(--line)}
.feed>li:last-child{border-bottom:0}
.feed .t{display:block;font-size:16px;padding:9px 0;min-height:44px}
/* el padrón: la marca de bancada al lado del nombre, no solo texto gris */
.feed li>.rowmark{float:left;margin:11px 10px 0 0}
.feed .m{font:600 11px/1.6 ui-monospace,Menlo,monospace;color:var(--muted);letter-spacing:.06em}
/* Las bancadas que sostuvieron una votación, al lado del resultado. El
   logotipo se lee de un vistazo; el título en mayúsculas del acta, no. */
.vbs{display:flex;flex-wrap:wrap;gap:6px 18px;padding:0 0 8px}
.vb{display:inline-flex;align-items:center;gap:5px}
.vb em{font:600 11px/1.6 ui-monospace,Menlo,monospace;font-style:normal;
  letter-spacing:.06em;color:var(--muted)}
.vb.vsi em{color:var(--si)}
.vb.vno em{color:var(--no)}
.note{border-left:3px solid var(--wait);padding:10px 0 10px 14px;color:var(--muted);
  font-size:13.5px;margin:14px 0}
.grid.cards{grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}
/* bill text: the filed document, rendered as prose instead of dumped in a <pre> */
.txt{max-width:72ch}
.txt p{overflow-wrap:anywhere;margin:0 0 12px;max-width:none}
.txt h3{margin:26px 0 8px;scroll-margin-top:60px;overflow-wrap:anywhere}
.txt .scroll{max-height:none;margin:0 0 14px}
.txt pre{margin:0;padding:12px 14px;white-space:pre;
  font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.toc{list-style:none;margin:0;padding:0}
.toc li a{display:block;min-height:44px;line-height:1.35;padding:12px 0;
  border-bottom:1px solid var(--line);overflow-wrap:anywhere}
@media(min-width:760px){.toc{columns:2;column-gap:30px}.toc li{break-inside:avoid}}
/* --- listados largos: la fila con etapa, medida y agrupación ---------------
   Las 311 páginas de listado son las más largas del sitio: comision/agraria
   medía 50 963 px a 375, 63 pantallas. No tenían ni un solo
   elemento gráfico. 250 filas del mismo tamaño, del mismo peso y del mismo
   color, con el título corriendo 136 caracteres por línea mientras el propio
   .lede del sitio se corta en 66. Cada fila lleva ahora un riel del color de
   su etapa (el mismo color que esa etapa tiene en la barra de la portada), la
   etapa escrita, porque el color nunca va solo, y un título con medida de
   lectura.
   El estado que se repite en toda la página se dice una vez arriba y solo se
   imprime en las filas que se salen de él, que son las que hay que ver. */
/* minmax(0,1fr), no 1fr: el mínimo de una pista `1fr` es su contenido mínimo,
   y un `.chip` con `white-space:nowrap` y cuarenta caracteres, «Publicada en el
   Diario Oficial El Peruano», ensanchaba la pista 3 px por encima del ancho de
   la página a 375. Los rótulos largos de estado además se parten. */
.feed li.r{display:grid;grid-template-columns:minmax(0,1fr);gap:0 14px;
  align-items:start;padding:8px 0 10px 13px;border-left:3px solid currentColor}
.feed li.r>.chip{white-space:normal}
/* 104px, no 92: «INVESTIGACIÓN» en monospace de 11 px con 0.1em de espaciado
   mide 100, y con `overflow-wrap:anywhere` la etiqueta salía partida en
   «INVESTIGACI / ÓN» en las cuatro mociones de investigación. */
@media(min-width:520px){.feed li.r{grid-template-columns:104px minmax(0,1fr)}}
.feed .stg{display:block;font:600 11px/1.4 ui-monospace,Menlo,monospace;
  letter-spacing:.1em;text-transform:uppercase;color:currentColor;
  padding-top:11px;overflow-wrap:anywhere}
.feed li.r .t{font:600 16px/1.38 system-ui,sans-serif;color:var(--ink);
  max-width:62ch;padding:6px 0 3px}
.feed li.r .m{display:block}
.feed li.r>.stg{grid-row:1}
@media(min-width:520px){.feed li.r>.stg{grid-row:1/span 3}
  .feed li.r>.m,.feed li.r>.t,.feed li.r>.chip,.feed li.r>.sign{grid-column:2}
  /* nada que alinear si toda la página está en el mismo estado: la columna de
     la etapa desaparece. Después de la regla de arriba a propósito. Con la
     misma especificidad gana la última, y así fue como esta perdió una vez. */
  .feed.nostg li.r{grid-template-columns:minmax(0,1fr)}
  .feed.nostg li.r>*{grid-column:1}}
/* la cabecera de grupo: lo que convierte 250 filas en un documento navegable */
li.gh{display:flex;align-items:baseline;gap:12px;position:sticky;top:46px;
  z-index:5;background:var(--ground);padding:14px 0 7px;margin-top:8px;
  font:600 11px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.14em;
  text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line)}
li.gh:first-child{margin-top:0}
li.gh b{margin-left:auto;color:var(--ink);letter-spacing:.06em}
.ranked li.gh{border-bottom:1px solid var(--line)}
.ranked .lbl small{display:block;color:var(--muted);font-size:12.5px;
  margin-top:2px}
/* las seis etapas del trámite, con el color que ya tienen en la portada */
.st-pres{color:var(--sen)} .st-com{color:var(--wait)} .st-pleno{color:var(--accent)}
.st-autog{color:var(--vio)} .st-ley{color:var(--ok)} .st-dead{color:var(--dead)}
"""

# Shared by every list page: filter the rows already in the DOM, and accept the
# term from the URL so a party or district chip elsewhere can link straight to
# "the padrón, filtered to this".
FILTER_JS = """<script>
/* «rodriguez» tiene que encontrar a Rodríguez: nadie escribe las tildes en un
   buscador, y sin esto la lista devolvía cero. */
function nrm(s){return s.normalize("NFD").replace(/[\\u0300-\\u036f]/g,"").toLowerCase();}
function flt(){var v=nrm(q.value),n=0,t=0;
[].forEach.call(ls.children,function(li){
/* las cabeceras de grupo no son resultados: se van mientras se filtra y
   vuelven al borrar la búsqueda, para no dejar un rótulo sobre cero filas. */
if(li.className.indexOf("gh")>=0){li.style.display=v?"none":"";return;}
t++;var h=nrm(li.textContent).indexOf(v)<0;li.style.display=h?"none":"";n+=h?0:1;});
if(window.cnt)cnt.textContent=n+" de "+t+" en esta página.";
try{history.replaceState(null,"",v?"#q="+encodeURIComponent(q.value):"#");}catch(e){}}
q.oninput=flt;
var h0=decodeURIComponent(location.hash.replace(/^#(q=)?/,""));
if(h0){q.value=h0;flt();}
</script>"""

# Ordena en el DOM cualquier <table class="srt">: click en la cabecera ordena
# por esa columna, otro click invierte. Una celda con data-s ordena por ese
# valor (fechas en ISO); si ambas celdas parecen número (cifras con espacio
# fino, % o S/) compara como número, y si no, como texto en español. El roster
# de una votación tiene su propio orden con estado en la URL; esto es para las
# tablas que eran estáticas.
SORT_JS = """<script>
[].forEach.call(document.querySelectorAll("table.srt"),function(t){
 if(!t.tHead||!t.tBodies.length)return;
 var th=t.tHead.rows[0].cells,cur=-1,dir=1;
 [].forEach.call(th,function(h,i){
  if(!h.textContent.trim())return;
  var b=document.createElement("button");b.textContent=h.textContent;
  h.textContent="";h.appendChild(b);
  b.onclick=function(){
   dir=(cur===i)?-dir:1;cur=i;
   var tb=t.tBodies[0],rs=[].slice.call(tb.rows);
   var NUM=/^-?[\\d\\s\\u202f.,]+\\s*(%|S\\/)?$/;
   rs.sort(function(a,c){
    var x=(a.cells[i].dataset.s||a.cells[i].textContent).trim(),
        y=(c.cells[i].dataset.s||c.cells[i].textContent).trim(),r;
    if(NUM.test(x)&&NUM.test(y))
     r=parseFloat(x.replace(/[^0-9,.-]+/g,"").replace(",","."))
      -parseFloat(y.replace(/[^0-9,.-]+/g,"").replace(",","."));
    else r=x.localeCompare(y,"es");
    return (r||0)*dir;});
   rs.forEach(function(r){tb.appendChild(r);});
   [].forEach.call(th,function(h2){h2.removeAttribute("aria-sort");});
   h.setAttribute("aria-sort",dir>0?"ascending":"descending");
  };});
});
</script>"""

THEME = ("<script>(function(){var t=localStorage.getItem('tema');"
         "if(t)document.documentElement.setAttribute('data-theme',t);})()</script>")
TOGGLE = ("<button onclick=\"var d=document.documentElement,"
          "n=d.getAttribute('data-theme')==='dark'?'light':'dark';"
          "d.setAttribute('data-theme',n);localStorage.setItem('tema',n)\" "
          "aria-label=\"Cambiar tema claro u oscuro\">Tema</button>")


# Los ocho destinos, agrupados por la pregunta que responde cada uno. Una sola
# tabla: la barra ancha, el índice desplegable del móvil y el bloque «Empezar
# por aquí» de la portada se generan de aquí, así que no pueden discrepar.
NAV = (
    ("Qué se propone", (
        ("proyectos.html", "Proyectos de ley",
         "Todo lo presentado, con la etapa en la que está cada expediente"),
        ("mociones.html", "Mociones",
         "Investigación, control político, declaraciones y saludos"))),
    ("Quién lo hace", (
        ("parlamentarios.html", "Parlamentarios",
         "Los 190 escaños, con retrato, bancada y ficha de actividad"),
        ("bancadas.html", "Bancadas",
         "Las seis, con su tamaño, su unidad de voto y sus integrantes"),
        ("comisiones.html", "Comisiones",
         "Quién compone cada una y qué proyectos tiene en la mano"))),
    ("Qué pasa en el Pleno", (
        ("votaciones.html", "Votaciones",
         "Cómo votó cada quien, nombre por nombre, en cada lista nominal"),
        ("asistencia.html", "Asistencia",
         "Quién estuvo en la sala en cada toma de asistencia"),
        ("gasto.html", "Gasto",
         "Lo que cuesta cada despacho: planilla y viajes, mes a mes"))),
)


def nav_html(r):
    """La barra: la marca, los ocho destinos y el índice desplegable.

    A ancho de teléfono sólo existe el <details>; a partir de 900 px sólo la
    fila. Los dos salen de NAV, y la hoja de estilos oculta el que sobra.
    """
    row, first = [], True
    for _label, dests in NAV:
        if not first:
            row.append('<i class="sep" aria-hidden="true"></i>')
        first = False
        row += [f'<a href="{r}{u}">{esc(t)}</a>' for u, t, _d in dests]
    panel = "".join(
        f'<span class="mh">{esc(label)}</span>'
        + "".join(f'<a href="{r}{u}">{esc(t)}<small>{esc(d)}</small></a>'
                  for u, t, d in dests)
        for label, dests in NAV)
    return (f'<nav class="top"><a class="brand" href="{r}index.html">Hemiciclo</a>'
            f'<div class="links">{"".join(row)}</div>'
            f'<details class="menu"><summary>Secciones</summary>'
            f'<div class="panel">{panel}</div></details>{TOGGLE}</nav>')


def logo_footer(body, r):
    """The credit for the marks this page actually paints.

    CC BY-SA travels with the work, not with the credits page: 261 pages showed
    a logo and 9 linked the attribution, and 78 of the 82 that showed the one
    CC BY-SA logo named neither its author nor its licence. Scoped by what the
    page shows and built from the same JSON as the credits table, so it cannot
    drift from the files or claim a licence for a logo that is not on screen.

    Short on purpose. §3(a)(2) of CC BY-SA 4.0 lets the conditions be satisfied
    by a link to a resource that carries the required information, and
    /acerca#logos carries it in full: author, licence, source and the
    rasterisation notice, per logo. So the footer names the author and the
    licence, which is what has to be visible next to the work, and links the
    rest instead of reprinting 60 words of it on 261 pages.
    """
    shown = [p for p in BENCH_ORDER if f"logos/{BENCH[p][2]}" in body]
    if not shown:
        return ""
    sa = [p for p in shown
          if "BY-SA" in LOGO_CREDITS.get(p, {}).get("licencia", "").upper()]
    return ('<br>Logotipos: marcas de sus organizaciones. '
            + " ".join(f'El de {esc(p)}, por {esc(LOGO_CREDITS[p]["autor"])}, '
                       f'<a href="{CC_BY_SA}">CC BY-SA 4.0</a>.' for p in sa)
            + f' <a href="{r}{LOGO_CREDIT}">Autoría, licencia y '
              f'modificación de cada uno</a>.')


def shell(title, body, depth=0, desc=""):
    """Every page. `noindex` is deliberate and site-wide: this deploys public
    before it is finished, and a half-checked copy of the Congress's record has
    no business in a search index. Removing it is a one-line decision, taken
    once, here."""
    r = "../" * depth
    return f"""<!doctype html><html lang="es"><meta charset="utf-8">
<title>{esc(title)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="description" content="{esc(desc)}">
<style>{CSS}</style>{THEME}
{nav_html(r)}
<div class="wrap">{body}
<footer class="prov"><a href="{r}acerca.html">Acerca, fuentes y
correcciones</a> &middot; Registro independiente con datos publicados por el
Congreso del Perú. <b>Este sitio no es el Congreso.</b>{logo_footer(body, r)}
</footer></div>{SORT_JS if 'class="srt"' in body else ""}</html>"""


def gp(party):
    """Colour class for a bench. Fixed by name, not by the order the data
    happened to arrive in: the old version hashed on insertion order, so the
    same bench could change colour between two builds."""
    return f"gp{bench_slot(party)}"


def bench_slug(party):
    return slugify(party)


def bench_url(r, party):
    return f"{r}bancada/{bench_slug(party)}.html" if party in BENCH else \
        f"{r}parlamentarios.html#q={esc(party or '')}"


def party_chip(party, r=None, size=22):
    """Bench identity in one component: logo, colour rule, name. Used in every
    roster row, sponsor card, table cell and roll call, so the same bench always
    looks the same wherever the reader meets it."""
    if not party:
        return ""
    inner = (f'{bench_logo(party, r or "", size)}'
             f'<span class="rule"></span><span class="nm">{esc(party)}</span>')
    if r is None:
        return f'<span class="bchip {gp(party)}">{inner}</span>'
    return f'<a class="bchip {gp(party)}" href="{bench_url(r, party)}">{inner}</a>'


def district_chip(dist, r):
    if not dist:
        return ""
    return (f'<a class="chip c" href="{r}parlamentarios.html#q={esc(dist)}">'
            f'{esc(dist)}</a>')


def surname_first(full):
    """«Mendoza Bernedo, Liz Huli» as the chambers write it. A grid sorted by
    surname but printed given-name-first buries the sort key mid-string and the
    column reads as random."""
    if "," in (full or ""):
        last, first = full.split(",", 1)
        return f"{last.strip()}, {first.strip()}"
    return full or ""


def initials(full):
    """'Mendoza Bernedo, Liz' -> 'LM'. The placeholder for the 140 members of
    the 2021-2026 Congress whose portraits went down with the old roster."""
    n = nice_name(full).split()
    return ((n[0][:1] + (n[-1][:1] if len(n) > 1 else "")) or "?").upper()


# --------------------------------------------------------------- retratos
#
# The face is the strongest anchor this site has, and for 190 people it used to
# be an <img> pointed at the chamber's own CMS: 37.7 MB over 29 requests to fill
# 38 px thumbnails on /parlamentarios.html, and one upstream path change away
# from deleting the face from 330 fichas, the padrón and 6 bench pages at once.
#
# `assets/photos/` is the tracked source, like `assets/logos/`, written by
# `python3 -m ingest.photos` (download once, ffmpeg to 160 px and 400 px wide,
# commit). `main()` copies it into the output; nothing under `site/` is an input.
PHOTOS = ROOT / "assets" / "photos"
PHOTO_W = (160, 400)


@functools.lru_cache(maxsize=None)
@functools.lru_cache(maxsize=None)
def local_photo(slug):
    """True when every width of this portrait is in the repo. Checked, not
    assumed: a slug that never downloaded gets the lettered tile rather than a
    404, and the build still works on a clone with no photos at all."""
    return all((PHOTOS / f"{slug}-{w}.jpg").is_file() for w in PHOTO_W)


def photo_img(L, r, w, h, sizes):
    """<img> for a portrait we hold locally, or "" when we do not.

    `sizes` is what the layout will actually give the image, so a phone picks
    the 160 px file. Intrinsic width/height are always set: without them the
    row height is unknown until the bytes land and the list reflows.
    """
    if not (L and local_photo(L["slug"])):
        return ""
    b = f'{r}photos/{L["slug"]}'
    return (f'<img loading="lazy" decoding="async" width="{w}" height="{h}" '
            f'src="{b}-400.jpg" srcset="{b}-160.jpg 160w, {b}-400.jpg 400w" '
            f'sizes="{sizes}" '
            f'alt="Retrato oficial de {esc(nice_name(L["full_name"]))}">')


def avatar(L, w=48, h=60, cls="face", r=""):
    """A portrait, or a lettered tile when there is none.

    Never an empty <img src="">: that shipped once and rendered as a
    broken-image icon on 140 pages. The lettered tile also sits *behind* the
    photo, so a portrait the chamber deletes from under us degrades to the
    initials instead of to a grey rectangle, with no JavaScript involved."""
    box = f"width:{w}px;height:{h}px"
    ini = esc(initials(L["full_name"]) if L else "?")
    img = photo_img(L, r, w, h, f"{w}px")
    if img:
        return (f'<span class="{cls} shot" style="{box}">'
                f'<i aria-hidden="true" style="font-size:{step(w / 3):g}px">{ini}</i>'
                f"{img}</span>")
    return (f'<span class="noface {cls}" style="{box};font-size:{step(w / 3):g}px" '
            f'aria-hidden="true">{ini}</span>')


def face_mini(L, r=""):
    """El retrato más pequeño que sirve: 26×32, para poner una cara al lado de
    un nombre dentro de una fila.

    Misma disciplina que avatar(): las iniciales van DETRÁS de la foto, así que
    lo que se ve cuando no hay retrato, los 140 del Congreso anterior, es una
    baldosa con letras y nunca un <img src=""> roto."""
    ini = esc(initials(L["full_name"]) if L else "?")
    img = photo_img(L, r, 26, 32, "26px") if L else ""
    if img:
        return (f'<span class="face shot fmini">'
                f'<i aria-hidden="true">{ini}</i>{img}</span>')
    return f'<span class="noface fmini" aria-hidden="true">{ini}</span>'


def person_chip(L, r, name=None, cls="", bench=False):
    """Una persona, anclada por su cara.

    El sitio existe porque «una tabla de 130 nombres no se puede leer», y la
    cara es el ancla más fuerte que tenemos; hasta ahora sólo la usaban la
    ficha, el padrón y el autor principal de un proyecto. Esto la lleva a los
    sitios donde una fila NOMBRA a alguien y no enseñaba nada: cuadros de
    comisión, coautores, firmantes de una moción y lista nominal de una
    votación.
    """
    # `name` llega ya escrito: la lista nominal de una votación lo reordena en
    # el navegador y el texto tiene que ser el mismo que puso el servidor.
    nm = esc(name if name is not None else nice_name(L["full_name"] if L else ""))
    # `bench=True` cierra la ficha con el logotipo de su bancada. Quién firma una
    # moción y de qué bancada es la misma pregunta; hasta aquí el nombre venía
    # solo y había que abrir la ficha para saberlo.
    tail = bench_logo(L["party"], r, 18, "bmini") if bench and L else ""
    inner = f'{face_mini(L, r)}<span class="nm">{nm}</span>{tail}'
    if L:
        return f'<a class="pchip {cls}" href="{leg_url(r, L["slug"])}">{inner}</a>'
    return f'<span class="pchip pl {cls}">{inner}</span>'


def signer_pop(rest, r=""):
    """Los firmantes que no caben en la fila, con nombre y bancada.

    Se renderizan siempre y se ocultan con CSS: son 233 firmas en las 107
    mociones, así que «cargarlas todas» cuesta menos que cualquier alternativa
    con JavaScript, y funciona sin él."""
    if not rest:
        return ""
    li = []
    for s in rest:
        L = s["leg"]
        nm = esc(nice_name(L["full_name"] if L else s["name_raw"]))
        who = f'<a href="{leg_url(r, L["slug"])}">{nm}</a>' if L else f"<span>{nm}</span>"
        li.append(f'<li>{bench_logo(L["party"] if L else "", r, 18)}{who}</li>')
    return f'<span class="mpop"><ul>{"".join(li)}</ul></span>'


# ------------------------------------------------------------------ hemiciclo
#
# The site is called Hemiciclo and the chamber it tracks is a half-circle of
# desks, so the seating plan is the one picture this subject actually has. One
# <a> per seat, in the markup, with the member's name in the title: it works
# with no JS, it is crawlable, and it is the fastest way to answer "where is my
# representative and who sits next to her".

def seat_points(n, rows=None, r_in=0.46, span=176.0):
    """(x, y, radius) for `n` seats in concentric arcs, ordered by angle.

    Seats per row are proportional to the row's radius, which is what keeps the
    spacing even instead of cramming the inner arc. Returned in reading order
    left to right so a caller can lay contiguous benches along the arc.
    """
    if n <= 0:
        return []
    rows = rows or max(2, min(9, round(n ** 0.5 / 2) + 1))
    radii = [r_in + (1.0 - r_in) * (i / (rows - 1)) for i in range(rows)]
    tot = sum(radii)
    per = [max(1, round(n * x / tot)) for x in radii]
    # rounding drift lands on the outer rows, which have the most room
    i = 0
    while sum(per) != n:
        step = 1 if sum(per) < n else -1
        j = rows - 1 - (i % rows)
        if per[j] + step >= 1:
            per[j] += step
        i += 1
    gap = (1.0 - r_in) / max(rows - 1, 1)
    seat_r = min(gap * 0.36,
                 min(math.radians(span) * rr / max(k, 1) * 0.38
                     for rr, k in zip(radii, per)))
    out = []
    half = math.radians(span) / 2
    for rr, k in zip(radii, per):
        for s in range(k):
            a = -half + math.radians(span) * ((s + 0.5) / k)
            out.append((math.sin(a) * rr, -math.cos(a) * rr, seat_r, a))
    out.sort(key=lambda p: p[3])
    return [(x, y, sr) for x, y, sr, _ in out]


# A seat is drawn as a shape as well as a colour. Six benches at six lightnesses
# survive greyscale, but a roll call has to survive it too, and «a favor» green
# against «en contra» red is 1.11:1 in black and white. So every state owns a
# silhouette: you can count the diamonds without seeing a single hue.
SHAPE = {
    "circle": lambda x, y, r: f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}"',
    "diamond": lambda x, y, r: (
        f'<path d="M{x:.1f} {y - r * 1.25:.1f}L{x + r * 1.25:.1f} {y:.1f}'
        f'L{x:.1f} {y + r * 1.25:.1f}L{x - r * 1.25:.1f} {y:.1f}Z"'),
    "square": lambda x, y, r: (
        f'<rect x="{x - r * .92:.1f}" y="{y - r * .92:.1f}" '
        f'width="{r * 1.84:.1f}" height="{r * 1.84:.1f}" rx="{r * .22:.1f}"'),
    "triangle": lambda x, y, r: (
        f'<path d="M{x:.1f} {y - r * 1.3:.1f}L{x + r * 1.25:.1f} {y + r * .85:.1f}'
        f'L{x - r * 1.25:.1f} {y + r * .85:.1f}Z"'),
    "ring": lambda x, y, r: f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r * .72:.1f}"',
    # Sixth silhouette, added for the sixth bench: a hexagon reads as its own
    # thing next to a circle at legend size and does not collide with the
    # triangle the way a second, inverted triangle would.
    "hex": lambda x, y, r: (
        '<path d="M' + "L".join(
            f"{x + r * 1.12 * math.cos(math.radians(a)):.1f} "
            f"{y + r * 1.12 * math.sin(math.radians(a)):.1f}"
            for a in range(-90, 270, 60)) + 'Z"'),
}
# vote state -> (shape, hollow). Hollow marks read as "not a vote": absent,
# excused, chairing. Eight states, eight silhouettes. Before that, «sin voto
# registrado» shared the hollow ring with «ausente», and since both are drawn in
# the same grey they were the same seat twice.
POS_SHAPE = {
    "SI": ("circle", 0), "NO": ("diamond", 0), "ABST": ("triangle", 0),
    "BLANCO": ("square", 0), "AUSENTE": ("ring", 1), "LICENCIA": ("square", 1),
    "PRESIDENCIA": ("triangle", 1), "OTRO": ("diamond", 1),
    # Distinct from the ("ring", 1) fallback: a legend that draws two states
    # with one silhouette trips the greyscale check, and should.
    "SINRES": ("hex", 1),
}


def mark(shape, x, y, r, col, hollow=0, cls="", title="", href=""):
    """One seat. `hollow` swaps the fill for a thick stroke, which is a third
    channel on top of shape and colour."""
    fill = ("none" if hollow else col)
    stroke = (f'stroke="{col}" stroke-width="{max(r * .55, 1.6):.1f}"'
              if hollow else "")
    # No <title> when there is no text for it: an empty one is a tooltip that
    # opens blank, and the seats that carry a hover card pass title="" because
    # the card says the same thing without a one-second delay.
    s = (f'{SHAPE[shape](x, y, r)} class="seat {cls}" fill="{fill}" {stroke}>'
         + (f"<title>{esc(title)}</title>" if title else "")
         + ("</circle>" if shape in ("circle", "ring") else
            "</rect>" if shape == "square" else "</path>"))
    # SHAPE already opens the tag; prepending another "<" emitted "<<circle"
    # on 202 pages. Browsers recover, validators do not.
    if not href:
        return s
    # tabindex=-1 on purpose: 190 seats would be 190 tab stops announcing the
    # same names the roster below already lists at full size. The seat is a
    # pointer affordance and a crawlable link; the keyboard path is the roster.
    return (f'<a href="{href}" tabindex="-1" aria-label="{esc(title)}">{s}</a>')


def hemicycle(seats, r="", middle=None, sub="", ident="hemi", groups=None):
    """Inline SVG seating plan. `seats` is [(colour, label, href, shape_key)] in
    the order they sit, left to right. Nothing here needs JavaScript.

    `groups` is [(label, n)] in the same order: a hairline and a bench label are
    drawn at each boundary, because "grouped by bench" is useless if the picture
    never says where one bench stops.
    """
    pts = seat_points(len(seats))
    if not pts:
        return ""
    # The viewBox is fitted to the seats that actually exist, so the figure has
    # no dead band under the arc and keeps its aspect ratio for the drawing.
    R, pad = 470.0, 26.0
    sr0 = pts[0][2] * R
    xs = [p[0] * R for p in pts]
    ys = [p[1] * R for p in pts]
    x0, x1 = min(xs) - sr0 - pad, max(xs) + sr0 + pad
    y0, y1 = min(ys) - sr0 - pad, max(ys) + sr0 + pad
    if groups:
        # The bench labels sit outside the arc. `overflow:visible` let them
        # paint past the element and show up as page-level horizontal overflow
        # at 375px, so the box has to include them: the widest label, in the
        # units it will be drawn in.
        wide = max(len(f"{lab} {n}") for lab, n in groups) if groups else 0
        room = R * 0.17 + wide * 10.0
        x0, x1 = x0 - room, x1 + room
        y0 -= R * 0.17 + 20
    W, H = x1 - x0, y1 - y0
    cx, cy = -x0, -y0
    # Type is set in viewBox units, so it has to be scaled to the box: an
    # absolute 34px in a 900-unit box renders at 16px on screen.
    k = W / 620.0
    out = [f'<svg class="hemi" id="{ident}" viewBox="0 0 {W:.0f} {H:.0f}" '
           f'role="group" aria-roledescription="diagrama de escaños" '
           f'aria-label="{esc(sub or "Distribución de escaños")}">'
           f'<title>{esc(sub or "Distribución de escaños")}</title>']
    lay = []
    for (x, y, sr), s in zip(pts, seats):
        col, label, href, shape = s[:4]
        card = s[4] if len(s) > 4 else ""
        px, py, pr = cx + x * R, cy + y * R, max(sr * R, 5.0)
        sh = (shape if isinstance(shape, tuple) else (shape or "circle", 0))
        sh = tuple(sh) + ("",) * (3 - len(sh))
        # A seat with a card hands its link and its name to the overlay below,
        # so the page has one <a> per member and not two.
        out.append(mark(sh[0], px, py, pr, col, sh[1], sh[2],
                        "" if card and href else label,
                        "" if card and href else href))
        if card and href:
            lay.append(
                f'<a class="sl" href="{href}" tabindex="-1" '
                f'aria-label="{esc(label)}">'
                f'<i style="left:{100 * px / W:.2f}%;top:{100 * py / H:.2f}%;'
                f'width:{200 * pr / W:.2f}%;height:{200 * pr / H:.2f}%"></i>'
                f'{card}</a>')
    if groups:
        # Boundary rules sit on the mid-angle between the last seat of one bench
        # and the first of the next, drawn from just inside the arc to outside.
        i, angs = 0, []
        for g, (lab, n) in enumerate(groups):
            if n <= 0:
                continue
            mid = pts[min(i + n // 2, len(pts) - 1)]
            angs.append((lab, n, math.atan2(mid[0], -mid[1])))
            i += n
            if g < len(groups) - 1 and i < len(pts):
                a0 = math.atan2(pts[i - 1][0], -pts[i - 1][1])
                a1 = math.atan2(pts[i][0], -pts[i][1])
                a = (a0 + a1) / 2
                sx, sy = math.sin(a), -math.cos(a)
                out.append(
                    f'<line class="cut" x1="{cx + sx * R * .40:.1f}" '
                    f'y1="{cy + sy * R * .40:.1f}" x2="{cx + sx * R * 1.06:.1f}" '
                    f'y2="{cy + sy * R * 1.06:.1f}"/>')
        for lab, n, a in angs:
            lx, ly = cx + math.sin(a) * R * 1.13, cy - math.cos(a) * R * 1.13
            anchor = ("middle" if abs(math.degrees(a)) < 25 else
                      "end" if a < 0 else "start")
            out.append(f'<text class="glab" x="{lx:.1f}" y="{ly:.1f}" '
                       f'text-anchor="{anchor}" font-size="{15 * k:.0f}">'
                       f'{esc(lab)} {n}</text>')
    if middle:
        big, small = middle
        my = cy - R * 0.46 * 0.60
        out.append(f'<text class="holen" x="{cx:.0f}" y="{my:.0f}" '
                   f'font-size="{40 * k:.0f}">{esc(big)}</text>'
                   f'<text class="hole" x="{cx:.0f}" y="{my + 26 * k:.0f}" '
                   f'font-size="{14 * k:.0f}">{esc(small)}</text>')
    out.append("</svg>")
    # The hover layer rides on top of the arc, in HTML and not in the SVG, for
    # one reason: inside the viewBox a card is stuck with the SVG's own scale,
    # and the two halls on the portada render about 490 px wide, which puts a
    # 330-unit card at 126 px on screen. Out here it is 250 px at every width,
    # it obeys z-index, and its <img>s are `loading="lazy"` inside a
    # display:none box, so 190 portraits are not fetched to sit unseen.
    if lay:
        out.append(f'<div class="seatlayer">{"".join(lay)}</div>')
    return f'<div class="hemibox">{"".join(out)}</div>'


def seat_card(L, r="", note=""):
    """What a seat says when you point at it: the face, the name, the bench
    mark. Everything else the diagram used to explain in a caption under it.

    Built from `avatar()` and `bench_logo()`, the same two components every
    other row on this site uses, so a member looks the same in a hover card as
    in the roster. Empty for a seat with nobody joined to it: a card with no
    name is worse than the tooltip it replaced."""
    if not L:
        return ""
    return ('<span class="pop">' + avatar(L, 46, 58, "face", r)
            + f'<span class="popt"><b>{esc(nice_name(L["full_name"]))}</b>'
            + f'<span class="pb">{bench_logo(L["party"], r, 20)}'
            + f'{esc(L["party"] or "Sin grupo")}</span>'
            + (f'<span class="pn">{esc(note)}</span>' if note else "")
            + "</span></span>")


def bench_seats(legs, r="", colour=lambda L: f"var(--gp{bench_slot(L['party'])})",
                cls=lambda L: bench_shape(L["party"]), link=True):
    """Legislators sorted into the arc: benches contiguous, in palette order,
    each bench drawn in its own colour AND its own silhouette. Within a bench,
    alphabetical. Returns (seats, groups).

    `cls` is the seat's shape; a caller answering a different question overrides
    it. The bench page asks "which of these seats are ours"."""
    key = lambda L: (bench_slot(L["party"]) or 99, L["full_name"])  # noqa: E731
    ordered = sorted(legs, key=key)
    seats = [(colour(L),
              f'{nice_name(L["full_name"])}, {L["party"] or "sin grupo"}'
              f'{" · " + L["district"] if L["district"] else ""}',
              leg_url(r, L["slug"]) if link else "", cls(L),
              seat_card(L, r) if link else "")
             for L in ordered]
    groups, last = [], object()
    for L in ordered:
        p = L["party"] or "Sin grupo"
        if p != last:
            groups.append([bench_mono(p) if p != "Sin grupo" else "—", 0])
            last = p
        groups[-1][1] += 1
    return seats, [tuple(g) for g in groups]


def hall_svg(mem, ch, r=""):
    """A chamber drawn whole: one seat per member, benches contiguous and
    labelled. Used identically on the home page and the padrón."""
    seats, hg = bench_seats(mem, r)
    return hemicycle(seats, r, middle=(str(len(mem)), "escaños"),
                     sub=f"Escaños de {CHAMBER[ch]} por bancada",
                     ident=f"hemi-{ch}", groups=hg)


def swatch(col, shape=None, label=""):
    """One legend swatch.

    Drawn by `mark()`, the same function that draws the seat it decodes, so the
    key cannot disagree with the picture. It is an SVG and not a styled <span>
    for two measured reasons. The <span> version put each silhouette in a
    `.key .sw-circle` rule and the generic `.key .sw` rule below it. Identical
    specificity, so the later one won and painted every one of them back into a
    rounded square: the key separated the states by colour alone while the seat
    plan separated them by shape. And a hollow triangle, which is what presiding
    the session looks like on the arc, has no CSS shorthand at all.

    `shape` is (shape, hollow) as in POS_SHAPE; "hatch" for the striped chip
    that matches the striped segment of a bar; None for a plain colour chip,
    which is only ever a bench, whose identity is the logo and the name beside
    it and never the colour on its own.
    """
    if shape == "hatch":
        return f'<span class="sw sw-hatch" style="color:{col}"></span>'
    if not shape:
        return f'<span class="sw" style="background:{col}"></span>'
    sh, hollow = shape
    return (f'<svg class="sw" viewBox="0 0 16 16" role="img" '
            f'style="color:{col}">'
            + mark(sh, 8, 8, 5, "currentColor", hollow, title=label) + "</svg>")


def key_list(items, shapes=None):
    """The legend every chart on this site shares.

    The swatch wears the colour; the words never do. Coloured 13px text is the
    quiet way a chart fails contrast, and a legend whose label is only legible
    to someone who can see the hue is not a legend. That is also why every
    legend that decodes a seat plan or a state bar passes `shapes`.
    """
    out = []
    for i, (c, lab, v) in enumerate(items):
        if v == "":
            continue
        sw = swatch(c, shapes[i] if shapes else None,
                    re.sub("<[^>]+>", "", str(lab)))
        out.append(f"<li>{sw}<span>{lab}</span><b>{v}</b></li>")
    return f'<ul class="key">{"".join(out)}</ul>'


def pctxt(n, tot):
    """A share as text. 51 of 14 908 is not «0%». It is «menos del 1 %», and
    saying 0 next to a legend entry that is visibly drawn is a contradiction."""
    if not tot:
        return "—"
    p = 100 * n / tot
    return ("menos del 1 %" if 0 < p < 0.5 else
            f"{p:.1f} %" if p < 10 else f"{p:.0f} %")


# -------------------------------------------------------------------- charts
#
# Server-rendered SVG, no library and no runtime. Numbers are tabular, the axis
# is recessive, and every chart carries a caption saying what it measures. A
# chart nobody can name the units of is decoration.

def col_chart(rows, unit="", height=150, ident="c", bar_col="var(--accent)",
              every=1, partial=None):
    """Columns over time. `rows` is [(tick, title, value)] already in order.

    `tick` is what goes on the axis (blank for most of them); `title` is what
    the hover says and is never blank. A tooltip reading «: 106 proyectos» with
    no period on it is worse than no tooltip. `partial` marks the last
    column as an incomplete period so an 11-day month is not read as a crash.
    """
    if not rows:
        return ""
    n, top = len(rows), max(v for _, _, v in rows) or 1
    W = max(320, min(1000, n * 26))
    bw = W / n
    bars, labs = [], []
    for i, (lab, ttl, v) in enumerate(rows):
        h = (height - 26) * v / top
        x = i * bw
        last = partial and i == n - 1
        bars.append(f'<rect class="mark{" part" if last else ""}" '
                    f'x="{x + bw * .14:.1f}" '
                    f'y="{height - 22 - h:.1f}" width="{bw * .72:.1f}" '
                    f'height="{max(h, 1.2):.1f}" rx="2.5" '
                    f'fill="{"none" if last else bar_col}"'
                    + (f' stroke="{bar_col}" stroke-width="2" '
                       f'stroke-dasharray="3 2"' if last else "")
                    + f'><title>{esc(ttl)}: {num(v)} {esc(unit)}'
                    + (f". {esc(partial)}" if last else "")
                    + "</title></rect>")
        if i % every == 0 or i == n - 1:
            labs.append(f'<text x="{x + bw / 2:.1f}" y="{height - 6}" '
                        f'text-anchor="middle" font-size="11">{esc(lab)}</text>')
    hi = max(range(n), key=lambda i: rows[i][2])
    peak = (f'<text class="val" x="{hi * bw + bw / 2:.1f}" '
            f'y="{height - 28 - (height - 26) * rows[hi][2] / top:.1f}" '
            f'text-anchor="middle" font-size="12">{num(rows[hi][2])}</text>')
    return (f'<div class="figwrap"><svg class="chart" id="{ident}" '
            f'viewBox="0 0 {W} {height}" preserveAspectRatio="xMinYMid meet" '
            f'role="img" aria-label="{esc(unit)} por periodo">'
            f'<line class="ax" x1="0" y1="{height - 22}" x2="{W}" '
            f'y2="{height - 22}"/>' + "".join(bars) + peak + "".join(labs)
            + "</svg></div>")


def stack_bar(parts, total=None, height=15, gap=True):
    """One horizontal 100% bar. `parts` is [(colour, label, n)].

    Two fixes that are not cosmetic. A real 2px surface rule between segments,
    because two adjacent fills that a colour-blind reader cannot tell apart read
    as one segment of double the size. And a floor of 0.35% on the width, so a
    category with 1 item out of 14 908 is a visible sliver instead of a legend
    entry pointing at nothing.
    """
    tot = total or sum(n for _, _, n in parts) or 1
    shown = [(c, lab, n) for c, lab, n in parts if n]
    out = []
    for i, (c, lab, n) in enumerate(shown):
        w = max(100 * n / tot, 0.35)
        rule = (";border-right:2px solid var(--raised)"
                if gap and i < len(shown) - 1 else "")
        out.append(f'<i style="width:{w:.2f}%;background:{c}{rule}" '
                   f'title="{esc(lab)}: {num(n)} ({pctxt(n, tot)})"></i>')
    return f'<div class="bar" style="height:{height}px">{"".join(out)}</div>'


def bench_split(rows, party_of, r="", note=""):
    """«Who is in this room, by bench». The same block serves a chamber page, a
    committee and a bench page. Bar + a legend that names every bench."""
    c = {}
    for x in rows:
        c[party_of(x) or "Sin grupo"] = c.get(party_of(x) or "Sin grupo", 0) + 1
    if not c:
        return ""
    order = sorted(c, key=lambda p: (bench_slot(p) or 99, p))
    tot = sum(c.values())
    bar = stack_bar([(f"var(--gp{bench_slot(p)})", p, c[p]) for p in order], tot)
    # The swatch is the seat: same `mark()`, same silhouette, same colour. It
    # used to be a `<span class="sw">` square standing in for a round seat, the
    # one place where "the key is drawn by mark() so it cannot drift" was still
    # a claim rather than a fact.
    leg = ('<ul class="key">' + "".join(
        f'<li>{swatch(f"var(--gp{bench_slot(p)})", bench_shape(p), p)}'
        f'{bench_logo(p, r, 24)}<a href="{bench_url(r, p)}">{esc(p)}</a>'
        f'<b>{c[p]}</b> <span class="mut">{pctxt(c[p], tot)}</span></li>'
        for p in order) + "</ul>")
    return bar + leg + (f'<p class="sm mut">{note}</p>' if note else "")


def prov(lines):
    return ('<div class="prov"><b>Procedencia de los datos.</b> '
            + " ".join(l for l in lines if l) + "</div>")


# ---------------------------------------------------------------------- load

BILLREF = re.compile(r"(?:proyecto|pl|proposici[oó]n)[^0-9]{0,20}(\d{1,5})\s*[/-]\s*"
                     r"(\d{4})|(\d{4,5})\s*/\s*(\d{4})\s*-\s*[A-Z]{2}", re.I)


def subject_bill(d, v):
    """`vote.bill_id` is null on every roll call the Congress has published, so
    the only link left is the bill number written inside the asunto. Matches
    only an explicit `NNNN/YYYY` style reference, never a fuzzy title match,
    because a wrong bill on a vote page is worse than no bill."""
    m = BILLREF.search(v["subject"] or "")
    if not m:
        return None
    n = int(m.group(1) or m.group(3))
    for b in (d["bill_by_id"].get(f'{v["per_par"]}-{v["chamber"]}-{n}'),
              d["bill_by_id"].get(f'{v["per_par"]}-C-{n}')):
        if b:
            return b
    return None


def ctte_key(name):
    """Committee title reduced to what identifies it. The oficio blocks write
    the same body as «Comisión de Asuntos de X» or «Comisión de X» where the
    cuadro says «X», and those decorations are the whole difference."""
    s = unicodedata.normalize("NFD", name or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    s = re.sub(r"^comisi[oó]n (de|en) ", "", s)
    s = re.sub(r"^(en )?asuntos (de|en) ", "", s)
    return " ".join(re.sub(r"[^a-z ]", " ", s).split())


def ctte_aliases(con, cttes):
    """Five ids in `committee` are not committees. They were created from oficio
    amendment blocks that name an existing body with a longer title,
    «Comisión de Procedimientos Especiales» for «Procedimientos Especiales»,
    and they inflated the Senate list from 11 to 16 while making a member's
    «cambios posteriores» point at a committee they are already sitting on.

    A phantom is recognisable without a hardcoded id list: it carries amendment
    rows and no roster at all. It is folded into the closest real committee of
    the same chamber and periodo by title similarity. Nothing is written to the
    DB; the ids stay there and this runs again on the next build.

    ponytail: SequenceMatcher over a normalised title, cutoff 0.6. The worst
    real pair here scores 0.67 and the best wrong one 0.4. If the Congress ever
    files two committees whose names differ by one word, this needs the oficio's
    own text instead of the title.
    """
    counts = {}
    for r in con.execute("SELECT committee_id, amendment, count(*) n "
                         "FROM committee_member GROUP BY 1, 2"):
        counts.setdefault(r["committee_id"], {})[r["amendment"]] = r["n"]
    seated = [c for c in cttes.values() if counts.get(c["id"], {}).get(0)]
    phantom = [c for c in cttes.values()
               if counts.get(c["id"]) and not counts[c["id"]].get(0)]
    alias, names = {}, {}
    for a in phantom:
        ka = ctte_key(a["name"])
        best, score = None, 0.0
        for c in seated:
            if (c["per_par"], c["chamber"]) != (a["per_par"], a["chamber"]):
                continue
            s = difflib.SequenceMatcher(None, ka, ctte_key(c["name"])).ratio()
            if s > score:
                best, score = c, s
        if best and score >= 0.6:
            alias[a["id"]] = best
            names[a["id"]] = a["name"]
            cttes.pop(a["id"])
    return alias, names


def load(con):
    d = {"con": con}
    d["legs"] = [dict(r) for r in con.execute(
        "SELECT * FROM legislator ORDER BY chamber, last_name, first_name")]
    d["by_id"] = {l["id"]: l for l in d["legs"]}
    d["by_name"] = {norm(l["full_name"]): l for l in d["legs"]}
    # Roll-call PDFs print surnames only ("RETO OTERO") or "APELLIDOS, NOMBRE",
    # never the full name the bills API uses. No surname repeats inside a
    # chamber, so (chamber, apellidos) is a safe second key.
    d["by_last"] = {(l["chamber"], norm(l["last_name"])): l for l in d["legs"]}
    d["bills"] = [dict(r) for r in con.execute(
        "SELECT * FROM bill ORDER BY per_par DESC, presented_on DESC, ply_num DESC")]
    d["bill_by_id"] = {b["id"]: b for b in d["bills"]}
    # Text of each bill, metadata only: the bodies are gigabytes together and are
    # read one at a time in text_body(). The table fills in the background, so
    # this is 0 rows on one run and every bill on another; nothing here assumes
    # a size. `has_body` and `note` are different answers -- text we have, and a
    # document the Congress filed as an image -- and a bill with no row at all is
    # a third: not fetched yet.
    d["texts"] = {r["bill_id"]: dict(r) for r in con.execute(
        "SELECT bill_id, doc_id, pages, chars, note, source_url, fetched_at, "
        "body IS NOT NULL AS has_body FROM bill_text")}
    for b in d["bills"]:
        b["text"] = d["texts"].get(b["id"])
    d["spon"] = {}
    for r in con.execute("SELECT * FROM bill_sponsor ORDER BY bill_id, rank"):
        d["spon"].setdefault(r["bill_id"], []).append(dict(r))
    d["acts"] = {}
    for r in con.execute("SELECT * FROM bill_action ORDER BY bill_id, acted_on DESC"):
        d["acts"].setdefault(r["bill_id"], []).append(dict(r))
    # 37% of the corpus sits at EN COMISIÓN, so "which committee" is the whole
    # status. Empty until the dossier re-fetch lands; the pages degrade to what
    # they said before.
    d["cttes"] = {r["id"]: dict(r) for r in con.execute(
        "SELECT * FROM committee ORDER BY name")}
    d["alias"], d["alias_name"] = ctte_aliases(con, d["cttes"])
    d["bill_cttes"], d["ctte_bills"] = {}, {}
    for r in con.execute("SELECT * FROM bill_committee"):
        c = d["cttes"].get(r["committee_id"]) or d["alias"].get(r["committee_id"])
        if c:
            d["bill_cttes"].setdefault(r["bill_id"], []).append(c)
            d["ctte_bills"].setdefault(c["id"], []).append(r["bill_id"])
    # Rosters exist for the Senate only, and only because the session that
    # approved them was minuted. `amendment=1` is a change a bench filed later
    # by oficio over its own designation, NOT a thirteenth seat: it is kept
    # apart here so nothing downstream can sum it into a committee.
    d["ctte_mem"], d["leg_cttes"] = {}, {}
    for r in con.execute("SELECT * FROM committee_member ORDER BY name_raw"):
        c = d["cttes"].get(r["committee_id"])
        m = dict(r)
        if not c:
            c = d["alias"].get(r["committee_id"])
            if not c:
                continue
            # A row filed under an alias title is, by construction, a change to
            # the real committee: it only ever appears in an oficio block.
            m["amendment"] = 1
            m["alias_name"] = d["alias_name"][r["committee_id"]]
        m["committee_id"] = c["id"]
        # Por la cámara de la comisión, no por «S»: mientras sólo hubo rosters
        # del Senado daba igual, y en cuanto entra una comisión de Diputados
        # el nombre se busca en el padrón equivocado y no casa ninguno.
        m["leg"] = (d["by_id"].get(m["legislator_id"])
                    or d["by_last"].get((c["chamber"], norm(m["name_raw"]))))
        d["ctte_mem"].setdefault(c["id"], []).append(m)
        if m["leg"]:
            d["leg_cttes"].setdefault(m["leg"]["slug"], []).append(m)
    d["motions"] = [dict(r) for r in con.execute(
        "SELECT * FROM motion ORDER BY presented_on DESC, num DESC")]
    d["signers"] = {}
    for r in con.execute("SELECT * FROM motion_signer ORDER BY motion_id, rank"):
        d["signers"].setdefault(r["motion_id"], []).append(dict(r))
    d["votes"] = [dict(r) for r in con.execute(
        "SELECT * FROM vote ORDER BY held_on DESC, id DESC")]
    d["vrows"] = {}
    for r in con.execute("SELECT * FROM vote_row"):
        d["vrows"].setdefault(r["vote_id"], []).append(dict(r))

    # Name -> legislator is the only bridge between the bills API and the
    # chamber rosters: neither side publishes the other's id.
    for bid, ss in d["spon"].items():
        for s in ss:
            s["leg"] = (d["by_id"].get(s["legislator_id"])
                        or d["by_name"].get(norm(s["name_raw"])))
    mch = {m["id"]: m["chamber"] for m in d["motions"]}
    for mid, ss in d["signers"].items():
        for s in ss:
            s["leg"] = (d["by_id"].get(s["legislator_id"])
                        or d["by_name"].get(norm(s["name_raw"]))
                        or d["by_last"].get((mch.get(mid),
                                             norm(s["name_raw"].split(",")[0]))))
    vch = {v["id"]: v["chamber"] for v in d["votes"]}
    for vid, rs in d["vrows"].items():
        ch = vch.get(vid)
        for x in rs:
            nm = norm(x["name_raw"])
            x["leg"] = (d["by_id"].get(x["legislator_id"])
                        or d["by_name"].get(nm)
                        or d["by_last"].get((ch, nm))
                        or d["by_last"].get((ch, norm(x["name_raw"].split(",")[0]))))

    # Who chaired each sitting. Two independent signals, because being in the
    # chair is the difference between "did not vote" and "was not there", and
    # publishing the wrong one about a named person is a libel, not a bug.
    d["presided"] = set()
    for v in d["votes"]:
        for x in d["vrows"].get(v["id"], []):
            if x["position"] == "PRESIDENCIA" and x["leg"]:
                d["presided"].add((v["chamber"], v["held_on"], x["leg"]["slug"]))
        who = (v["session"] or "").split(":", 1)[-1].strip() if v["session"] else ""
        pl = d["by_last"].get((v["chamber"], norm(who.split(",")[0]))) if who else None
        if pl:
            d["presided"].add((v["chamber"], v["held_on"], pl["slug"]))

    # Attendance: 1 340 rows, one per member per taking. A session takes several,
    # so the taking (chamber, date, hora) is the unit, not the day.
    d["takings"], d["leg_att"] = {}, {}
    for r in con.execute("SELECT * FROM attendance"):
        x = dict(r)
        x["leg"] = (d["by_id"].get(x["legislator_id"])
                    or d["by_name"].get(norm(x["name_raw"])))
        k = (x["chamber"], x["held_on"], x["taken_at"])
        s = d["takings"].get(k)
        if not s:
            s = d["takings"][k] = {
                "chamber": x["chamber"], "held_on": x["held_on"],
                "taken_at": x["taken_at"], "rows": [],
                "source_url": x["source_url"], "sort": hour24(x["taken_at"]),
                "slug": f'{x["chamber"].lower()}-{x["held_on"]}-'
                        f'{slugify(x["taken_at"])}'}
        s["rows"].append(x)
        if x["leg"]:
            d["leg_att"].setdefault(x["leg"]["slug"], []).append((s, x))
    d["sesiones"] = sorted(d["takings"].values(),
                           key=lambda s: (s["held_on"], s["sort"]), reverse=True)
    for s in d["takings"].values():
        s["rows"].sort(key=lambda x: x["name_raw"])
        s["tally"] = att_tally(d, s, s["rows"])

    # The same person sat in both Congresses under two ids and two pages that
    # never referenced each other. Normalised name is the only bridge, same rule
    # as ingest.legislators.norm.
    d["twin"] = {}
    byname = {}
    for L in d["legs"]:
        byname.setdefault(norm(L["full_name"]), []).append(L)
    for group in byname.values():
        if len(group) > 1:
            for L in group:
                other = [o for o in group if o["id"] != L["id"]]
                d["twin"][L["slug"]] = sorted(other, key=lambda o: -o["per_par"])

    d["rates"] = base_rates(d)

    # reverse indexes
    d["leg_bills"], d["leg_motions"], d["leg_votes"] = {}, {}, {}
    for bid, ss in d["spon"].items():
        for s in ss:
            if s["leg"]:
                d["leg_bills"].setdefault(s["leg"]["slug"], []).append((bid, s["rank"]))
    for mid, ss in d["signers"].items():
        for s in ss:
            if s["leg"]:
                d["leg_motions"].setdefault(s["leg"]["slug"], []).append((mid, s["rank"]))
    for v in d["votes"]:
        for x in d["vrows"].get(v["id"], []):
            if x["leg"]:
                d["leg_votes"].setdefault(x["leg"]["slug"], []).append((v, x["position"]))
    d["bill_votes"] = {}
    for v in d["votes"]:
        v["bill"] = d["bill_by_id"].get(v["bill_id"] or "") or subject_bill(d, v)
        if v["bill"]:
            d["bill_votes"].setdefault(v["bill"]["id"], []).append(v)

    # Guessable permalinks: chamber + date + ordinal of the day, so you can
    # construct "the second Senate vote of 12 August 2026" without a hash.
    # ponytail: the ingester is moving vote.id to the same key; when it lands,
    # this reduces to slugify(v["id"]).
    seen = {}
    for v in sorted(d["votes"], key=lambda v: ((v["chamber"] or ""),
                                               (v["held_on"] or ""), v["id"])):
        k = (v["chamber"], v["held_on"])
        seen[k] = seen.get(k, 0) + 1
        v["slug"] = f'{(v["chamber"] or "x").lower()}-{v["held_on"]}-{seen[k]}'

    # Peer baselines: a margin means nothing until you know the other margins.
    # Also the per-vote facts every member page needs, computed once here so a
    # legislator page never has to re-derive (and never disagrees with) what
    # the vote page says.
    d["vstats"], d["gp_agree"] = {}, {}
    d["voutcome"], d["vstate"], d["vbloc"] = {}, {}, {}
    for v in d["votes"]:
        rs = d["vrows"].get(v["id"], [])
        tal, _c = tallies(v, rs)
        agree = len({t[2] for t in tal}) <= 1
        fin = v["n_yes_final"] is not None
        same = fin and (v["n_yes_final"], v["n_no_final"] or 0,
                        v["n_abstain_final"] or 0) == (v["n_yes"], v["n_no"] or 0,
                                                       v["n_abstain"] or 0)
        d["vstate"][v["id"]] = (
            ("confirmado", "confirmado en el diario de debates", "ok") if same else
            ("corregido", "corregido en sala", "ok") if fin else
            ("disputado", "lectura en disputa", "wait") if not agree else
            ("provisional", "acta provisional", "wait") if v["provisional"] else
            ("firme", "", ""))
        ty, tn, _ta = tal[0][2] if tal else (0, 0, 0)
        d["voutcome"][v["id"]] = v["result"] or (
            "Aprobada por mayoría simple" if ty > tn else
            "No alcanzó la mayoría simple")
        if not rs:
            continue
        c = {}
        for x in rs:
            c[x["position"]] = c.get(x["position"], 0) + 1
        em = sum(c.get(k, 0) for k in ("SI", "NO", "ABST"))
        s = d["vstats"].setdefault(v["chamber"], {"margin": [], "presence": []})
        if em:
            s["margin"].append(round(100 * c.get("SI", 0) / em))
        s["presence"].append(round(100 * em / len(rs)))
        win = max(("SI", "NO", "ABST"), key=lambda k: c.get(k, 0))
        bench = {}
        for x in rs:
            bench.setdefault(row_party(x), {}).setdefault(x["position"], 0)
            bench[row_party(x)][x["position"]] += 1
        for p, bc in bench.items():
            top = max(("SI", "NO", "ABST"), key=lambda k: bc.get(k, 0))
            a, t = d["gp_agree"].get((v["chamber"], p), (0, 0))
            d["gp_agree"][(v["chamber"], p)] = (a + (1 if top == win else 0), t + 1)
            if sum(bc.get(k, 0) for k in ("SI", "NO", "ABST")) >= 3:
                d["vbloc"][(v["id"], p)] = top
    load_gasto(d, con)
    return d


# --------------------------------------------------------------------- gasto
#
# El Congreso no publica lo que recibe cada parlamentario. Lo que sí existe, y
# en el Portal de Transparencia Estándar y no en el Congreso, es la planilla de
# cada despacho persona a persona y cada viaje autorizado con cargo a él. Eso es
# lo que cuesta un despacho, que no es lo mismo que lo que cobra su titular, y
# la diferencia se dice en cada sitio donde sale una cifra.
#
# Se agrega en SQL: son 266 883 filas de planilla y 38 018 de viajes, y traerlas
# a Python para sumarlas costaba más que todo el resto de `load()` junto.
GASTO_Y = 2025          # el último año civil completo publicado


def load_gasto(d, con):
    d["gasto"], d["gasto_mes"] = {}, {}
    have = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                       "AND name IN ('payroll','travel','rep_week')").fetchone()[0]
    if have < 3:
        d["rep_weeks"] = []
        return
    # Una persona, una fila. El padrón tiene a Barbarán Reyes dos veces con dos
    # grafías -- «Rosangela» y «Rosangella», una por cámara -- y el portal usa
    # sólo una, así que su planilla cae en el asiento de 2026 y sus viajes en el
    # de 2021. Sin unirlas, la lista la enseña dos veces: una con S/ 14 774 y
    # otra con medio millón. El par lo detecta el ingestor y lo publica; aquí
    # sólo se aplica, y para el año que se muestra gana el asiento de ese
    # periodo. Si aparece un segundo caso, su `demo()` falla antes que esto.
    pairs = {}
    for a, b in gastos.split_despachos(con):
        La, Lb = d["by_id"].get(a), d["by_id"].get(b)
        if La and Lb:
            pairs[a] = pairs[b] = {La["per_par"]: a, Lb["per_par"]: b}

    def seat(lid, y, m):
        """El asiento al que pertenece ese mes, no el que trajo la fila."""
        return pairs[lid].get(gastos.per_par(y, m), lid) if lid in pairs else lid

    for tab, key in (("payroll", "planilla"), ("travel", "viajes")):
        for r in con.execute(
                f"SELECT legislator_id id, year y, month m, sum(total) t, "
                f"count(*) n FROM {tab} WHERE legislator_id IS NOT NULL "
                f"GROUP BY 1, 2, 3"):
            g = d["gasto"].setdefault(seat(r["id"], r["y"], r["m"]), {})
            g.setdefault(r["y"], {"planilla": 0.0, "viajes": 0.0, "nviajes": 0})
            g[r["y"]][key] += r["t"] or 0.0
            if key == "viajes":
                g[r["y"]]["nviajes"] += r["n"]
    # Serie mensual del conjunto: es donde se ve el agujero de enero a abril de
    # 2026, que fue una suspensión acordada por la Mesa y no austeridad.
    for r in con.execute(
            "SELECT year y, month m, sum(total) t FROM travel "
            "WHERE legislator_id IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2"):
        d["gasto_mes"][(r["y"], r["m"])] = r["t"] or 0.0
    d["rep_weeks"] = [dict(r) for r in con.execute(
        "SELECT * FROM rep_week ORDER BY n DESC")]


def gasto_year(d, L, y=GASTO_Y):
    """-> dict del año o None. Nunca cero: un despacho sin filas es un despacho
    del que no se publicó nada, no uno que no gastó."""
    return (d["gasto"].get(L["id"]) or {}).get(y)


def gasto_rank(d, y=GASTO_Y):
    """[(total, legislator)] de mayor a menor, sólo los que tienen dato."""
    out = []
    for lid, ys in d["gasto"].items():
        g = ys.get(y)
        L = d["by_id"].get(lid)
        if g and L:
            out.append((g["planilla"] + g["viajes"], L, g))
    return sorted(out, key=lambda x: -x[0])


# ------------------------------------------------------------- text of a bill

# The filed document, as `pdftotext -layout` left it. Peruvian bills are
# structured into articles, disposiciones, exposición de motivos y fórmula
# legal, and that structure survives in the plain text as line prefixes. Detecting it is
# the difference between a readable law and GovTrack's 594-page dump with no
# table of contents and hashed anchors.
ORD = ("[úu]nico|primero|segundo|tercero|cuarto|quinto|sexto|s[ée]ptimo|octavo|"
       "noveno|d[ée]cim[oa]|und[ée]cimo|duod[ée]cimo")
# A heading that owns its whole line.
BIG = re.compile(
    r"^\s*(?:[IVXLC]+\.\s*|\d{1,2}[.)]\s*)?("
    r"exposici[óo]n de motivos.{0,60}"
    r"|an[áa]lisis (?:costo|de costo|costo[- ]beneficio).{0,60}"
    r"|f[óo]rmula legal.{0,60}"
    r"|efecto de la (?:vigencia|norma).{0,60}"
    r"|impacto (?:de la norma|econ[óo]mico|presupuestal).{0,60}"
    r"|vinculaci[óo]n con.{0,60}"
    r"|disposici[oó]n(?:es)? (?:complementari|transitori|final|derogatori|"
    r"modificatori)\w*.{0,60}"
    r"|(?:t[íi]tulo|cap[íi]tulo|secci[óo]n) [IVXLC]+\b.{0,70}"
    r")\s*[:.]?\s*$", re.I)
# A heading that opens a line and is followed by the text of the article itself.
ART = re.compile(rf"^\s*(art[íi]culo\s+(?:\d{{1,3}}|{ORD}))\s*[°ºª]?\s*"
                 r"(?:[.\-–—:)]+\s*|$)", re.I)
# Two runs of spaces inside a line is what a column gap looks like after -layout.
COL = re.compile(r"\S {3,}\S")


def text_blocks(body):
    """Plain text -> ('h', id, title) | ('p', text) | ('pre', text) blocks.

    ponytail: blank lines are the only paragraph signal `pdftotext -layout`
    leaves, and they are reliable here. If a filing ever comes through
    single-spaced, this renders it as one long paragraph rather than wrongly.
    """
    out, seen = [], {}

    def head(seed, title):
        i = slugify(seed)[:48] or "s"
        seen[i] = seen.get(i, 0) + 1
        if seen[i] > 1:
            i = f"{i}-{seen[i]}"
        out.append(("h", i, " ".join(title.split())))

    for blk in re.split(r"\n\s*\n", body or ""):
        lines = [l.rstrip() for l in blk.split("\n") if l.strip()]
        if not lines:
            continue
        first = lines[0].strip()
        m = BIG.match(first)
        if m:
            head(m.group(1), first)
            lines = lines[1:]
        else:
            m = ART.match(first)
            if m:
                rest = first[m.end():].strip()
                # "Artículo 1. Objeto de la presente ley" reads better in a table
                # of contents than "Artículo 1", so the first clause comes along.
                lead = clip(re.split(r"(?<=[.;:])\s", rest, maxsplit=1)[0], 80,
                            "") if rest else ""
                head(m.group(1), m.group(1) + (". " + lead if lead else ""))
                lines = ([rest] if rest else []) + lines[1:]
        if not lines:
            continue
        cols = sum(1 for l in lines if COL.search(l))
        if len(lines) >= 3 and cols >= max(2, round(len(lines) * 0.6)):
            out.append(("pre", "\n".join(lines)))   # a cuadro; keep it aligned
        else:
            out.append(("p", " ".join(" ".join(l.split()) for l in lines)))
    return out


def text_html(blocks, base=""):
    """-> (table of contents items, body blocks). `base` prefixes the anchors
    when the table of contents lives on a different page than the text."""
    toc, out = [], []
    for blk in blocks:
        if blk[0] == "h":
            toc.append(f'<li><a href="{base}#{blk[1]}">{esc(blk[2])}</a></li>')
            out.append(f'<h3 id="{blk[1]}">{esc(blk[2])}</h3>')
        elif blk[0] == "pre":
            out.append(f'<div class="scroll"><pre>{esc(blk[1])}</pre></div>')
        else:
            out.append(f"<p>{esc(blk[1])}</p>")
    return toc, out


# Server-rendered text, filtered in place. Only `#q=` is read from the hash, so
# an anchor like `#articulo-3` still scrolls instead of being taken as a query.
TEXT_JS = """<script>
(function(){var q=document.getElementById('q'),ls=document.getElementById('ls'),
c=document.getElementById('cnt');if(!q||!ls)return;
function f(){var v=q.value.toLowerCase(),n=0;
[].forEach.call(ls.children,function(e){
var h=v&&e.textContent.toLowerCase().indexOf(v)<0;e.style.display=h?'none':'';n+=h?0:1;});
c.textContent=v?n+' de '+ls.children.length+' bloques contienen ese texto.':'';
try{history.replaceState(null,'',v?'#q='+encodeURIComponent(q.value):'#texto');}catch(e){}}
q.oninput=f;var m=/^#q=(.*)$/.exec(location.hash);
if(m){q.value=decodeURIComponent(m[1]);f();}})()
</script>"""

# Past this many characters the text gets its own page: the bill page carries the
# table of contents and the ficha, and stays readable on a phone.
SPLIT = 60000

TEXT_SRC = ("Extraído con <code>pdftotext -layout</code> del PDF del expediente. "
            "Ante cualquier diferencia manda el original.")

ONE_VERSION = ('<span title="El dictamen, el texto sustitutorio y la autógrafa '
               'aparecen como PDF sueltos en el historial del expediente.">Es el '
               'texto <b>tal como fue presentado</b>. El Congreso no publica '
               'versiones comparables del articulado.</span>')


def text_corpus(d):
    """(readable, filed as an image, not fetched yet) over the whole corpus.
    Computed every build: the table fills in the background and any number
    written down here would be wrong by the next run."""
    read = img = 0
    for b in d["bills"]:
        t = b.get("text")
        if not t:
            continue
        read += bool(t["has_body"])
        img += not t["has_body"]
    return read, img, len(d["bills"]) - read - img


def text_body(d, bid):
    """The body, read one row at a time: 14 878 of these do not fit in RAM."""
    row = d["con"].execute("SELECT body FROM bill_text WHERE bill_id=?",
                           (bid,)).fetchone()
    return row["body"] if row else None


def text_stats(t):
    return (f'{num(t["pages"] or 0)} página{"s" if (t["pages"] or 0) != 1 else ""} '
            f'· {num(t["chars"] or 0)} caracteres')


def bill_text_html(d, b, r):
    """-> (section for the bill page, full-text page or None).

    Three states, and they are not the same thing: text we have, a document the
    Congress filed as an image, and a bill whose PDF we simply have not fetched
    yet. The third renders nothing at all. Claiming a bill has no text when it
    is our pipeline that is behind would be a lie about the Congress.
    """
    t = d["texts"].get(b["id"])
    if not t:
        return "", None
    pdf = (f'<a href="{esc(t["source_url"])}">PDF oficial del expediente ↗</a>'
           if t["source_url"] else "")
    if not t["has_body"]:
        # `note` is why. Say it in Spanish and quote it, rather than hiding the
        # section or implying the document does not exist.
        why = ("El documento pesa demasiado para tener texto: es un archivo de "
               "imágenes." if "MB" in (t["note"] or "") else
               "El Congreso publicó este proyecto como imagen escaneada, sin "
               "texto seleccionable.")
        return (f'<section id="texto"><h2>Texto del proyecto</h2>'
                f'<p>{why} El PDF se lee a la vista, pero no tiene capa de texto: '
                f'no hay nada que extraer ni buscar. {pdf}'
                + (f', {num(t["pages"])} página'
                   f'{"s" if t["pages"] != 1 else ""} de imágenes'
                   if t["pages"] else "")
                + f'.</p><p class="sm mut">Razón registrada por la ingesta: '
                f'<code>{esc(t["note"])}</code>.</p></section>'), None
    body = text_body(d, b["id"])
    if not body:
        return "", None      # row says there is a body and there is not: say nothing
    blocks = text_blocks(body)
    split = len(body) > SPLIT
    page = f'{b["ply_num"]}-texto.html'
    toc, out = text_html(blocks, page if split else "")
    toc_html = (f'<h3 style="margin-top:22px">Índice del articulado '
                f'({len(toc)})</h3><ol class="toc">{"".join(toc)}</ol>'
                if toc else
                '<p class="sm mut">Texto corrido: no trae artículos numerados '
                'ni secciones con título, así que no hay índice.</p>')
    head = (f'<p>{text_stats(t)}. {pdf}. {ONE_VERSION}</p>')
    search = ('<div class="filters" style="margin-top:18px">'
              '<input id="q" type="search" placeholder="Buscar dentro del texto"'
              ' aria-label="Buscar dentro del texto del proyecto">'
              '</div><p class="sm mut" id="cnt"></p>')
    if not split:
        sec = (f'<section id="texto"><h2>Texto del proyecto</h2>{head}'
               f'{toc_html}{search}<div class="txt" id="ls">{"".join(out)}</div>'
               f'<p class="sm mut">{TEXT_SRC}</p></section>{TEXT_JS}')
        return sec, None
    sec = (f'<section id="texto"><h2>Texto del proyecto</h2>{head}'
           f'<p>Es largo, así que va en <a href="{page}">su propia página</a>, con '
           f'buscador.</p>'
           f'{toc_html}<p class="sm mut">{TEXT_SRC}</p></section>')
    ch = b["chamber"] or "C"
    ttl = b["title"] or "Sin título registrado"
    full = f"""<div class="crumb"><a href="{r}index.html">Inicio</a> ›
<a href="{r}proyectos.html">Proyectos de ley</a> ›
<a href="{b["ply_num"]}.html">{esc(b["code"])}</a> › Texto</div>
<span class="eyebrow">Texto del proyecto {esc(b["code"])}</span>
<h1>{esc(clip(ttl, 150))}</h1>
<p class="lede">{text_stats(t)}. {pdf + "." if pdf else ""}
<a href="{b["ply_num"]}.html">Volver a la ficha del proyecto</a>.</p>
<p class="sm mut">{ONE_VERSION}</p>
{toc_html}
{search}
<div class="txt" id="ls">{"".join(out)}</div>
{TEXT_JS}
{prov([TEXT_SRC,
       f'Documento: {esc(t["doc_id"])} del expediente de {esc(CHAMBER[ch])}.',
       f'Descargado el {fecha(t["fetched_at"])}' if t["fetched_at"] else "",
       f'Cita sugerida: «Proyecto de ley {esc(b["code"])}, texto como fue '
       f'presentado, Congreso de la República del Perú, consultado el '
       f'{fecha(dt.date.today().isoformat())}».'])}"""
    return sec, shell(f'{b["code"]} · texto', full, depth=3,
                      desc=f'Texto completo del proyecto de ley {b["code"]}, '
                           f'por artículos y con buscador.')


# ---------------------------------------------------------------- bill pages

# The six stages, as they read in a list: a colour that matches the stack bar
# on the portada, and the word, because the colour is never the only channel.
STAGE_TAG = {"PRES": ("Presentado", "st-pres"), "COM": ("En comisión", "st-com"),
             "PLENO": ("En el Pleno", "st-pleno"), "REV": ("Cámara revisora", "st-pleno"),
             "AUTOG": ("Autógrafa", "st-autog"), "LEY": ("Ya es ley", "st-ley"),
             "DEAD": ("Archivado", "st-dead")}
STAGE_COL = {"PRES": "var(--sen)", "COM": "var(--wait)", "PLENO": "var(--accent)",
             "REV": "var(--accent)", "AUTOG": "var(--vio)", "LEY": "var(--ok)",
             "DEAD": "var(--dead)"}


def stage_badge(b):
    """The stage, in words, in the rail's own colour."""
    tag, _tone = STAGE_TAG.get(status_info(b["status"])[0], STAGE_TAG["PRES"])
    return f'<span class="stg">{esc(tag)}</span>'


def vote_feed_row(d, v, r=""):
    """One roll call in a list.

    The bench marks are the reason this function exists. Five all-caps titles
    stacked on a date is a wall of text you skip; the same five with the logos
    of the benches that carried each one can be read without reading. `vbloc`
    is the bench's own majority in that roll call, computed once in `load()`,
    so a list can never disagree with the breakdown on the vote page itself.
    """
    def side(ps, txt, cls):
        if not ps:
            return ""
        return (f'<span class="vb {cls}" title="{esc(txt)}: '
                f'{esc(", ".join(ps))}">'
                + "".join(bench_logo(p, r, 20) for p in ps)
                + f"<em>{esc(txt)}</em></span>")

    si = [p for p in BENCH_ORDER if d["vbloc"].get((v["id"], p)) == "SI"]
    no = [p for p in BENCH_ORDER if d["vbloc"].get((v["id"], p)) == "NO"]
    marks = side(si, "a favor", "vsi") + side(no, "en contra", "vno")
    if not marks and not d["vrows"].get(v["id"]):
        marks = '<span class="vb"><em>sin lista nominal</em></span>'
    tail = f'<span class="vbs">{marks}</span>' if marks else ""
    return (f'<li><span class="m">{esc(CHAMBER_SHORT[v["chamber"]])} · '
            f'{fecha(v["held_on"])}'
            f'{" · " + esc(v["result"]) if v["result"] else ""}</span>'
            f'<a class="t" href="{r}votacion/{v["slug"]}.html">'
            f'{esc(clip(v["subject"] or v["id"], 150))}</a>{tail}</li>')


def status_chip(b):
    """The published state, as a chip."""
    stage = status_info(b["status"])[0]
    cls = {"LEY": "ok", "DEAD": "dead"}.get(stage, "wait")
    return (f'<span class="chip {cls}" style="margin-top:6px">'
            f'{esc(b["status"] or "—")}</span>')


def repeated(b):
    """The two pieces of a row that say what state it is in. Handed to
    `paginate` separately from the row so it can lift them out where they turn
    out to say the same thing on most of the page: what is left in the scroll
    is then the rows that DIFFER, which is the whole reason to scroll. The rail
    keeps its colour either way, so the stage never stops being visible."""
    return (stage_badge(b), status_chip(b))


def bill_row(r, b, extra=""):
    """One bill in a list: a rail in the colour of its stage, the stage in
    words, the code and date, the title at reading measure, and the published
    state. The stage arrives on three channels: position, colour and word. A
    list of 150 rows is scanned, not read."""
    _tag, tone = STAGE_TAG.get(status_info(b["status"])[0], STAGE_TAG["PRES"])
    t = b.get("text")
    if t and t["has_body"]:
        extra += " · texto en línea"
    return (f'<li class="r {tone}">{stage_badge(b)}'
            f'<span class="m">{esc(b["code"])} · {fecha(b["presented_on"])}'
            f'{extra}</span>'
            f'<a class="t" href="{bill_url(r, b)}">{esc(clip(b["title"], 110))}</a>'
            f'{status_chip(b)}</li>')


# Mociones, by what the instrument actually is. The register publishes nine
# spellings of five things, and 87 of the 107 are greetings, a fact the page
# hid completely by printing them in arrival order with an identical chip.
MOC_FAMILY = {"inv": ("Investigación", "st-pleno"),
              "control": ("Control político", "st-autog"),
              "esp": ("Comisión especial", "st-pres"),
              "decl": ("Interés nacional", "st-com"),
              "otra": ("Otro tipo", "st-ley"),
              "saludo": ("Saludo", "st-dead")}
MOC_ORDER = ("inv", "control", "esp", "decl", "otra", "saludo")
MOC_COL = {"inv": "var(--accent)", "control": "var(--vio)", "esp": "var(--sen)",
           "decl": "var(--wait)", "otra": "var(--ok)", "saludo": "var(--dead)"}


def moc_family(kind):
    k = norm(kind or "")
    if "INVESTIGACION" in k:
        return "inv"
    if "INTERPELACION" in k or "INVITACION" in k or "MINISTROS" in k:
        return "control"
    if "COMISION" in k:
        return "esp"
    if "INTERES NACIONAL" in k or "NECESIDAD PUBLICA" in k or "DECLARACION" in k:
        return "decl"
    if "SALUDO" in k:
        return "saludo"
    return "otra"


def mes_de(iso):
    """'2021-08-13' -> 'agosto de 2021'. The group a list row belongs to."""
    if not iso:
        return "Sin fecha registrada"
    try:
        d = dt.date.fromisoformat(str(iso)[:10])
    except ValueError:
        return "Sin fecha registrada"
    return f"{MES[d.month - 1]} de {d.year}"


def stage_mix(bills, r=""):
    """The shape of a 250-row list, before reading it: one stacked bar and a
    legend over the stages of the rows on this page. Same two helpers as every
    other bar on the site, so it cannot look like a different chart."""
    c = {}
    for b in bills:
        k = status_info(b["status"])[0]
        c[k] = c.get(k, 0) + 1
    order = [k for k in ("PRES", "COM", "PLENO", "REV", "AUTOG", "LEY", "DEAD")
             if c.get(k)]
    if len(order) < 2:
        # A facet that IS one state, «Proyectos en estado PUBLICADA EN EL
        # DIARIO OFICIAL EL PERUANO», has nothing to split. Its shape over
        # time is the fact it does have, and it is the same axis the month
        # headings below are cut on.
        ds = [b["presented_on"][:10] for b in bills if b["presented_on"]]
        for cut, unit in ((7, "mes"), (10, "día")):
            per = {}
            for x in ds:
                per[x[:cut]] = per.get(x[:cut], 0) + 1
            ks = sorted(per)
            if len(ks) >= 3:
                pts = [(f'{MES[int(k[5:7]) - 1][:3]} {k[2:4]}'
                        if cut == 7 and k.endswith(("-01", "-07"))
                        else k[8:10] if cut == 10 else "",
                        fecha(k) if cut == 10
                        else f'{MES[int(k[5:7]) - 1]} de {k[:4]}', per[k])
                       for k in ks]
                return ('<figure style="margin:18px 0 0">'
                        + col_chart(pts, "proyectos", 130, "mix", "var(--wait)",
                                    every=max(1, len(ks) // 12))
                        + f'<figcaption>Cuántos entraron cada {unit}. Están todos '
                          f'en el mismo estado, así que lo que los distingue es '
                          f'cuándo llegaron.</figcaption></figure>')
        return ""
    tot = sum(c.values())
    return ('<figure style="margin:18px 0 0">'
            + stack_bar([(STAGE_COL[k], STAGE_TAG[k][0], c[k]) for k in order],
                        tot, height=18)
            + key_list([(STAGE_COL[k], esc(STAGE_TAG[k][0]),
                         f"{num(c[k])} · {pctxt(c[k], tot)}") for k in order])
            + '<figcaption>En qué etapa están los proyectos de este listado; cada '
              'fila lleva ese color en su riel.</figcaption></figure>')


def bill_feed(r, bills):
    """Rows for `paginate`: (mes, chip, <li>). The month groups the list and
    the chip is handed over separately so the page can hoist it if it turns out
    to say the same thing on most of its rows."""
    return [(mes_de(b["presented_on"]), repeated(b), bill_row(r, b))
            for b in bills]


def render_bill(d, b):
    r = "../../../"
    bid, ch = b["id"], b["chamber"] or "C"
    stage, sentence, nexts = status_info(b["status"])
    acts = d["acts"].get(bid, [])
    sponsors = d["spon"].get(bid, [])
    track = stages(b["per_par"], ch)

    # Which stages have been reached, and when. Actions give real dates; a bill
    # whose dossier has not been downloaded still has its filing date.
    when = {"PRES": b["presented_on"]}
    reached = {"PRES"}
    for a in reversed(acts):
        s = status_info(a["text"])[0]
        if s in ("DEAD",):
            continue
        reached.add(s)
        when.setdefault(s, a["acted_on"])
    if stage != "DEAD":
        reached.add(stage)
    order = [k for k, _ in track]
    cur = stage if stage in order else ("LEY" if stage == "DEAD" else "PRES")
    ci = order.index(cur) if cur in order else 0
    if stage == "DEAD":
        ci = max((order.index(s) for s in reached if s in order), default=0)

    # Naming the committee turns "en manos de una comisión" into an address.
    cts = d["bill_cttes"].get(bid, [])
    ct_links = ", ".join(f'<a href="{ctte_url(r, c)}">{esc(c["name"])}</a>'
                         for c in cts)
    # The opening sentence said "en manos de una comisión" on 5,545 pages while
    # naming that comisión twice further down. Say it where the reader lands --
    # but keep `sentence` plain, since it also feeds the meta description, where
    # markup would arrive escaped and visible.
    sentence_html = esc(sentence)
    if stage == "COM" and cts:
        named_ct = ("Está en manos de " + ("la " if len(cts) == 1 else "")
                    + ct_links)
        sentence_html = esc(sentence).replace(
            esc("Está en manos de una comisión"), named_ct, 1)
        sentence = sentence.replace(
            "una comisión", ", ".join(c["name"] for c in cts), 1)
    nodes = []
    for i, (k, label) in enumerate(track):
        # "Está en manos de una comisión" is not an address. When we know which
        # comisión, the node says so and links to it.
        why = esc(STAGE_BLURB[k])
        if k == "COM" and cts:
            label = " · ".join(c["name"] for c in cts)
            why = (f'En manos de {ct_links}, que debe estudiarlo y emitir dictamen.'
                   if len(cts) == 1 else
                   f'Derivado a {len(cts)} comisiones: {ct_links}. Cada una puede '
                   f'emitir su propio dictamen.')
        if stage == "DEAD" and i == ci:
            cls, tag = "stop", "Detenido aquí"
        elif i < ci or (i == ci == len(track) - 1 and stage == "LEY"):
            cls, tag = "done", "Cumplido"
        elif i == ci:
            cls, tag = "now", "Etapa actual"
        else:
            cls, tag = "todo", "Pendiente"
        w = when.get(k) if cls in ("done", "now", "stop") else None
        nodes.append(
            f'<li class="{cls}"><span class="dot"></span>'
            f'<span class="when">{tag}{" · " + fecha(w) if w else ""}</span>'
            f'<span class="name">{esc(label)}</span>'
            f'<span class="why">{why}</span></li>')
    track_html = f'<ol class="track">{"".join(nodes)}</ol>'

    # timeline: every row annotated. Without the dossier we still know two
    # facts, and we say so rather than showing an empty card.
    rows = []
    if acts:
        for a in acts:
            _, why, _ = status_info(a["text"])
            doc = (f'<a class="doc" href="{esc(a["doc_url"])}">Documento ↗</a>'
                   if a["doc_url"] else "")
            rows.append(f'<li><span class="when">{fecha(a["acted_on"])}</span>'
                        f'<div><span class="what">{esc(a["text"])}</span>'
                        f'<div class="why">{esc(why)}</div>{doc}</div></li>')
        tl_note = ""
    else:
        rows.append(f'<li><span class="when">{fecha(b["presented_on"])}</span>'
                    f'<div><span class="what">PRESENTADO</span>'
                    f'<div class="why">{esc(STATUS["PRESENTADO"][1])}</div></div></li>')
        if (b["status"] or "").upper() != "PRESENTADO":
            rows.insert(0, f'<li><span class="when">Sin fecha registrada</span>'
                           f'<div><span class="what">{esc(b["status"])}</span>'
                           f'<div class="why">{esc(sentence)}</div></div></li>')
        # We know our copy has no timeline rows. We do NOT know whether the
        # Congress published none or we simply have not fetched them, and the
        # page must not assert the second.
        tl_note = ('<div class="note">Nuestra copia no tiene movimientos de este '
                   'expediente: solo su fecha de presentación y su estado. El '
                   'expediente oficial, al pie, puede contener más.</div>')

    # sponsors
    named = [s for s in sponsors if s["leg"]]
    primary = next((s for s in sponsors if (s["rank"] or 0) == 0), None)
    spon_html = ""
    if primary:
        L = primary["leg"]
        if L:
            # No photo, party or district survives for the 2021 members: the
            # chamber sites were replaced at the handover. An <img src=""> is a
            # broken-image box, not a placeholder -- omit it, and say plainly
            # that the detail is gone rather than printing an empty label.
            seat = (f'{esc(CHAMBER[L["chamber"]])} por {esc(L["district"])}'
                    if L["district"] else
                    f'{esc(CHAMBER[L["chamber"]])} &middot; el padrón de su '
                    f'periodo ya no se publica')
            spon_html = (
                f'<div class="who">{avatar(L, 72, 90, r=r)}'
                + f'<div><a class="nm" href="{leg_url(r, L["slug"])}">'
                f'{esc(nice_name(L["full_name"]))}</a>'
                f'<div class="sm mut">{seat}</div>'
                + (f'<div style="margin-top:10px">{party_chip(L["party"], r)}</div>'
                   if L["party"] else "")
                + '</div></div>')
        else:
            spon_html = (
                f'<div class="who"><div><span class="nm">'
                f'{esc(nice_name(primary["name_raw"]))}</span>'
                f'<div class="sm mut">No figura en el padrón 2026-2031, así que '
                f'no tenemos su ficha.</div>'
                f'</div></div>')
    co = [s for s in sponsors if s is not primary]
    co_html = ""
    if co:
        items = []
        for s in co:
            L = s["leg"]
            if L:
                # cara, logotipo de bancada y nombre: los tres canales que el
                # resto del sitio ya usa, en la lista más repetida del corpus
                items.append(
                    f'<li class="{gp(L["party"])}">'
                    f'<a class="pchip" href="{leg_url(r, L["slug"])}">'
                    + face_mini(L, r)
                    + (bench_logo(L["party"], r, 22) if L["party"] else "")
                    + f'<span class="nm">{esc(nice_name(L["full_name"]))}</span>'
                      f'</a></li>')
            else:
                items.append(
                    f'<li>{person_chip(None, r, nice_name(s["name_raw"]))}</li>')
        # The 2021 members have no bench on record, so keying the split on a
        # NULL party printed a leading blank where a name should be.
        withb = [s["leg"] for s in named if s["leg"]["party"]]
        head = ""
        if withb:
            head = ('<figure style="margin:0 0 20px">'
                    + bench_split([L["party"] for L in withb], lambda p: p, r)
                    + f'<figcaption>{len(named)} de {len(sponsors)} firmantes '
                      f'cruzan con el padrón; el reparto va sobre los {len(withb)} '
                      f'con bancada registrada.</figcaption></figure>')
        co_html = (f'<section><h2>Coautores ({len(co)})</h2>{head}'
                   f'<ul class="roll signers">{"".join(items)}</ul></section>')

    # Who is holding this bill, by bench. 14 908 bill pages had no bench colour
    # anywhere, so the identity system stopped at the edge of the corpus: the
    # committee that decides a bill's fate is a room with a majority in it.
    ctte_bench = ""
    if cts:
        tits = [m for c in cts for m in d["ctte_mem"].get(c["id"], [])
                if m["role"] == "titular" and not m["amendment"]]
        if tits:
            one = len(cts) == 1
            ctte_bench = (
                '<section><h2>Quién decide en la comisión</h2><figure>'
                + bench_split(tits, lambda m: m["bench"] or (m["leg"] or {}).get("party"), r)
                + f'<figcaption>Las {len(tits)} plazas de titular '
                + (f'de {ct_links}' if one else f'de las {len(cts)} comisiones que lo tienen')
                + ', por bancada: un dictamen sale con la mayoría de ellas. '
                  'Fuente: Diario de los Debates del Senado.'
                  '</figcaption></figure></section>')

    votes = d["bill_votes"].get(bid, [])
    votes_html = ""
    if votes:
        vr = []
        for v in votes:
            _t, _c = tallies(v, d["vrows"].get(v["id"], []))
            y, nn, aa = _t[0][2] if _t else (0, 0, 0)
            vr.append(
                f'<tr><td data-s="{esc(v["held_on"] or "")}">{fecha(v["held_on"])}</td>'
                f'<td><a href="{vote_url(r, v)}">'
                f'{esc(clip(v["subject"] or v["id"], 110))}</a></td>'
                f'<td class="num">{y}</td><td class="num">{nn}</td>'
                f'<td class="num">{aa}</td>'
                f'<td>{esc(d["voutcome"][v["id"]])}</td></tr>')
        votes_html = (
            f'<section><h2>Votaciones nominales sobre este proyecto '
            f'({len(votes)})</h2><div class="scroll"><table class="srt"><thead><tr>'
            f'<th>Fecha</th><th>Asunto</th><th>A favor</th><th>En contra</th>'
            f'<th>Abst.</th><th>Resultado</th></tr></thead><tbody>'
            + "".join(vr) + '</tbody></table></div></section>')

    # ponytail: the timeline already links every document at its own stage, so
    # a second flat list of the same 20 rows is noise. Only the count and the
    # honest note about there being no on-site text survive here.
    text_sec, text_page = bill_text_html(d, b, r)
    ficha_texto = ""
    if text_sec:
        tt = b["text"]
        ficha_texto = (
            '<div><dt>Texto</dt><dd>'
            + (f'<a href="#texto">Leer el texto del proyecto</a>'
               f'<small>{text_stats(tt)}, con buscador dentro del '
               f'documento</small>'
               if tt["has_body"] else
               '<a href="#texto">Por qué no hay texto</a>'
               '<small>el Congreso lo presentó como imagen</small>')
            + "</dd></div>")
    docs = [a for a in acts if a["doc_url"]]
    docs_html = ""
    if docs:
        docs_html = (
            f"<section><h2>Documentos del expediente ({len(docs)})</h2>"
            f"<p class='sm mut'>Cada uno está enlazado arriba, en la etapa que lo "
            f"produjo. "
            + ("El Congreso solo los publica en PDF; "
               "<a href=\"#texto\">el texto, por artículos, está aquí</a>."
               if text_sec and b["text"]["has_body"] else
               "El Congreso solo los publica en PDF, nunca en HTML.")
            + "</p></section>")

    e1, e2 = _enc(b["per_par"]), _enc(b["ply_num"])
    off = (f"{PORTAL}/{SEG[ch]}/expediente/{e1}/{e2}" if e1
           else f"{PORTAL}/{SEG[ch]}/expediente/consulta")
    summ = (f'<section><h2>Sumilla oficial</h2><p>{esc(b["summary"])}</p></section>'
            if b["summary"] else "")
    nexts_html = ""
    if nexts:
        nexts_html = ("<section><h2>Qué falta para que sea ley</h2><ol>"
                      + "".join(f"<li>{esc(n)}</li>" for n in nexts) + "</ol></section>")
    elif stage == "LEY":
        nexts_html = ("<section><h2>Qué falta para que sea ley</h2>"
                      "<p>Nada: el trámite terminó. La norma está vigente.</p></section>")

    cls = {"LEY": "ok", "DEAD": "dead"}.get(stage, "now")
    # Official titles run to 300 characters. The headline gets the first clause
    # so the status sentence and the tracker stay in the first screen; the full
    # title is right below, verbatim.
    full = b["title"] or "Sin título registrado"
    head = clip(full, 150)
    body = f"""
<div class="crumb"><a href="{r}index.html">Inicio</a> ›
<a href="{r}proyectos.html">Proyectos de ley</a> ›
<a href="{r}proyectos/p{b["per_par"]}-{ch}.html">{esc(CHAMBER[ch])} {b["per_par"]}</a> ›
{esc(b["code"])}</div>
<span class="eyebrow">Proyecto de ley {esc(b["code"])}</span>
<h1{" class=\"long\"" if len(head) > 62 else ""}>{esc(head)}</h1>
<p class="lede"><span class="chip {cls}">{esc(b["status"] or "sin estado")}</span>
&nbsp;{sentence_html}</p>
<section><h2>Trámite legislativo</h2>{track_html}</section>
{rate_block(d, b, stage)}
{nexts_html}
{summ}
{text_sec}
<section><h2>Ficha</h2><dl class="kv">
<div><dt>Presentado</dt><dd>{fecha(b["presented_on"]) or "—"}</dd></div>
<div><dt>Cámara de origen</dt><dd>{esc(CHAMBER[ch])}</dd></div>
<div><dt>Proponente</dt><dd>{esc(b["proponent"] or "—")}</dd></div>
<div><dt>Periodo parlamentario</dt><dd>{b["per_par"]}-{b["per_par"] + 5}</dd></div>
<div><dt>Firmas</dt><dd>{len(sponsors)}<small>mediana de su periodo:
{d["rates"]["firmas"].get(b["per_par"], 0)} firmas por proyecto</small></dd></div>
<div><dt>Estado publicado</dt><dd>{esc(b["status"] or "—")}</dd></div>
{ficha_texto}
{f'<div><dt>{"Comisión dictaminadora" if len(cts) == 1 else "Comisiones"}</dt><dd>{ct_links}</dd></div>' if cts else ""}
</dl>
{f'<details class="gloss" style="margin-top:12px"><summary>Título oficial completo</summary><p>{esc(full)}</p></details>' if head != full else ""}
</section>
{"<section><h2>Autor principal</h2>" + spon_html + "</section>" if spon_html else ""}
{co_html}
{ctte_bench}
{votes_html}
<section><h2>Historial del expediente</h2>{tl_note}
<ul class="tl">{"".join(rows)}</ul></section>
{docs_html}
{prov([
    f'Registro de proyectos de ley del Congreso de la República, vía '
    f'<code>{API}/proyecto-ley</code>.',
    f'Expediente oficial: <a href="{esc(off)}">portal SPLey del Congreso ↗</a>.',
    f'Descargado el {fecha(b["fetched_at"])}' if b["fetched_at"] else "",
    'Esta copia se regenera en cada corrida de la ingesta: el retraso máximo '
    'frente al registro oficial es de una corrida.',
    f'Cita sugerida: «Proyecto de ley {esc(b["code"])}, Congreso de la '
    f'República del Perú, consultado el {fecha(dt.date.today().isoformat())}».',
])}
"""
    return (shell(short_title(b["code"], b["title"]), body, depth=3,
                  desc=clip(sentence, 180)),
            text_page)


# ---------------------------------------------------------- legislator pages

def leg_att_rate(d, L):
    """(asistió, denominador, licencias, presidió) for one member. The
    denominator is only the takings where showing up was the expectation: a
    licencia and the chair are both out of it."""
    xs = d["leg_att"].get(L["slug"], [])
    ok = lic = pre = falta = 0
    for s, x in xs:
        kind = att_state(d, s, x)[1]
        ok += kind == "presente"
        falta += kind == "falta"
        lic += kind == "excusa"
        pre += kind == "presidencia"
    return ok, ok + falta, lic, pre


# letra · color · texto · silueta. The shape is the one the same state wears in
# a seat plan, so the strip, its legend and the hemicycle agree.
ATT_CELL = {"presente": ("P", "var(--si)", "Asistió", ("circle", 0)),
            "falta": ("A", "var(--no)", "No asistió", ("ring", 1)),
            "excusa": ("L", "var(--lic)", "Con licencia", ("square", 1)),
            "presidencia": ("M", "var(--pres)", "Presidió la sesión",
                            ("triangle", 1))}


def att_strip(d, L, r="../"):
    """One cell per attendance taking, in the order they were taken.

    Thirteen takings is not a year, so this is a strip of the sessions that
    happened and not a month grid: a calendar of empty squares would claim we
    know something about the days in between. Every cell carries its letter as
    well as its colour, because red and green alone fail one man in twelve, and
    links to the session it comes from.

    `att_state` decides what each cell is, and nothing here second-guesses it:
    a licencia and the chair get their own state, never the absent one.
    """
    xs = sorted(d["leg_att"].get(L["slug"], []),
                key=lambda p: (p[0]["held_on"], p[0]["sort"]))
    if not xs:
        return ""
    cells, months = [], {}
    for s, x in xs:
        months.setdefault(s["held_on"][:7], []).append((s, x))
    for mk in sorted(months):
        y, m = mk.split("-")
        cs = []
        for s, x in months[mk]:
            lab, kind, why = att_state(d, s, x)
            ch, col, short, _sh = ATT_CELL.get(
                kind, ("?", "var(--muted)", lab, ("ring", 1)))
            cs.append(
                f'<a class="cell" style="color:{col}" '
                f'href="{r}asistencia/{esc(s["slug"])}.html" '
                f'title="{fecha(s["held_on"])}, {esc(s["taken_at"])}: {esc(lab)}">'
                f'<b>{ch}</b><span>{int(s["held_on"][8:])}</span></a>')
        cells.append(f'<div class="mo"><span class="mol">{MES[int(m) - 1]} {y} '
                     f'<b>{len(months[mk])}</b></span>'
                     f'<div class="cells">{"".join(cs)}</div></div>')
    seen = [k for k in ("presente", "falta", "excusa", "presidencia")
            if k in {att_state(d, s, x)[1] for s, x in xs}]
    leg = key_list([(ATT_CELL[k][1], f"<b>{ATT_CELL[k][0]}</b> {ATT_CELL[k][2]}",
                     sum(1 for s, x in xs if att_state(d, s, x)[1] == k))
                    for k in seen],
                   shapes=[ATT_CELL[k][3] for k in seen])
    return (f'<figure><div class="cal">{"".join(cells)}</div>{leg}'
            f'<figcaption>Una casilla por toma, en orden; el número es el día. '
            f'Solo aparecen los meses con sesión.</figcaption></figure>')


def peer_strips(d, L, base, r="../"):
    """Where this person sits in their own chamber, on axes we actually measure.

    GovTrack puts the member on a two-axis ideology-leadership scatter derived
    from the whole cosponsorship graph. This Congress has 44 bills; an axis
    built on that would be a number we invented printed next to a real person's
    name, so there isn't one. What is here instead is the same idea done from
    counts: one strip per measure, every colleague a tick, this person marked,
    the chamber median marked, and the denominator written out.
    """
    ch, slug = L["chamber"], L["slug"]
    peers = [P for P in d["legs"] if P["chamber"] == ch and P["per_par"] == L["per_par"]]
    if len(peers) < 10:
        return ""

    def bills_of(P):
        return sum(1 for x in d["leg_bills"].get(P["slug"], [])
                   if d["bill_by_id"][x[0]]["per_par"] == P["per_par"])

    def att_of(P):
        ok, den, _l, _p = leg_att_rate(d, P)
        return round(100 * ok / den) if den >= 5 else None

    def loyal(P):
        vs = [(v, p) for v, p in d["leg_votes"].get(P["slug"], [])
              if p in ("SI", "NO", "ABST")]
        agr = [1 for v, p in vs if d["vbloc"].get((v["id"], P["party"] or "Sin grupo")) == p]
        return round(100 * len(agr) / len(vs)) if len(vs) >= 3 else None

    axes = [
        ("Proyectos de ley firmados", bills_of, "",
         f'del periodo {L["per_par"]}-{L["per_par"] + 5}'),
        ("Mociones firmadas", lambda P: len(d["leg_motions"].get(P["slug"], [])), "",
         "sobre las mociones publicadas hasta hoy"),
        ("Asistencia al Pleno", att_of, "%",
         "sobre las tomas en que se le esperaba; licencia y presidencia fuera "
         "del denominador"),
        ("Votó con su bancada", loyal, "%",
         "sobre las votaciones en que emitió voto, frente a la mayoría de su "
         "grupo"),
    ]
    out = []
    for title, fn, unit, den in axes:
        vals = [(P, fn(P)) for P in peers]
        vals = [(P, v) for P, v in vals if v is not None]
        mine = next((v for P, v in vals if P["slug"] == slug), None)
        if mine is None or len(vals) < 10:
            continue
        xs = [v for _, v in vals]
        lo, hi = min(xs), max(xs)
        rng = (hi - lo) or 1
        med = median(xs)
        W, H = 640, 62
        px = lambda v: 14 + (W - 28) * (v - lo) / rng  # noqa: E731
        # Ties are the whole story here and the first version threw them away:
        # with 8 takings only six attendance values exist, so 48 people share
        # one number. A tick per person drew 130 people as 6 marks and told all
        # 48 of them they were «39.º». Each distinct value now gets ONE bar
        # whose height is how many people are on it, and the rank is a range.
        hist = {}
        for _P, v in vals:
            hist[v] = hist.get(v, 0) + 1
        tall = max(hist.values())
        bars = []
        for v, k in sorted(hist.items()):
            h = 6 + 16 * (k / tall)
            bars.append(f'<rect class="pkbar" x="{px(v) - 1.6:.1f}" '
                        f'y="{21 - h / 2:.1f}" width="3.2" height="{h:.1f}" '
                        f'rx="1.4"><title>{v}{esc(unit)}: {k} de '
                        f'{len(vals)}</title></rect>')
        ticks = "".join(bars)
        better = sum(1 for v in xs if v > mine)
        same = hist[mine]
        rank = better + 1
        rank_txt = (f"{rank}.º" if same == 1 else
                    f"{rank}.º a {rank + same - 1}.º, empatado con "
                    f"{same - 1} colega" + ("s" if same > 2 else ""))
        # The median label sits under the median line, and the two end labels
        # step out of its way rather than printing on top of it.
        mx, ends = px(med), []
        if mx > 104:
            ends.append(f'<text x="14" y="57" font-size="11">{lo}{esc(unit)}</text>')
        if mx < W - 118:
            ends.append(f'<text x="{W - 14}" y="57" font-size="11" '
                        f'text-anchor="end">{hi}{esc(unit)}</text>')
        anch = "start" if mx < 70 else "end" if mx > W - 70 else "middle"
        out.append(f"""<figure class="strip"><figcaption class="lab">{esc(title)}
<b>{mine}{esc(unit)}</b></figcaption>
<div class="figwrap"><svg class="chart" viewBox="0 0 {W} {H}"
 aria-label="{esc(title)}: {mine}{esc(unit)}, mediana de la cámara
 {med}{esc(unit)}, {esc(rank_txt)} de {len(vals)}">
<line class="ax" x1="14" y1="21" x2="{W - 14}" y2="21"/>{ticks}
<line class="med" x1="{mx:.1f}" y1="4" x2="{mx:.1f}" y2="38"/>
<circle class="me" cx="{px(mine):.1f}" cy="21" r="7.5"/>
{"".join(ends)}
<text x="{max(14, min(W - 14, mx)):.1f}" y="57" font-size="11"
 text-anchor="{anch}">mediana {med}{esc(unit)}</text>
</svg></div>
<p class="sm mut">{esc(rank_txt)} de {len(vals)} en {esc(CHAMBER[ch])}; {esc(den)}.</p>
</figure>""")
    if not out:
        return ""
    return (f'<section><h2>Dónde está en su cámara</h2>'
            f'<p class="sm mut">El círculo es esta persona; la vertical, la mediana '
            f'de {esc(CHAMBER[ch])}. Todo lo de aquí son recuentos. No publicamos un eje '
            f'ideológico porque no hay con qué calcularlo sin inventarlo.</p>'
            + "".join(out) + "</section>")


def att_block(d, L, base, r="../"):
    """Where a member's record is thinnest and the reader's question hardest:
    does this person turn up. One row per taking, every one linked to the
    session it comes from."""
    xs = d["leg_att"].get(L["slug"], [])
    if not xs:
        if L["per_par"] < 2026:
            return ""       # no taking of that Congress is in our copy: omit
        return ('<section><h2>Asistencia al Pleno</h2><p>No figura en las listas '
                'que hemos leído. Como nombran a los 190 en ejercicio, significa '
                'que su nombre no cruzó con el padrón, no que haya '
                'faltado.</p></section>')
    ok, den, lic, pre = leg_att_rate(d, L)
    rate, usable = pct_or_note(ok, den)
    peers = base["asist"].get(L["chamber"], [])
    cmp_txt = ""
    if usable and len(peers) >= 5:
        # Two different medians live on this site and both are 95-ish: this one
        # is over PEOPLE (each colleague's own rate), the one on /asistencia is
        # over TAKINGS (how full the room was each time). Neither number means
        # anything without its population, so each says which it is.
        cmp_txt = (f' · mediana de los {len(peers)} '
                   f'{"diputados" if L["chamber"] == "D" else "senadores"}: '
                   f'{median(peers)}%')
    rows = []
    for s, x in sorted(xs, key=lambda p: (p[0]["held_on"], p[0]["sort"]),
                       reverse=True):
        lab, kind, why = att_state(d, s, x)
        tone = {"presente": "ok", "falta": "falta", "presidencia": "s"}.get(
            kind, "wait")
        rows.append(
            f'<tr><td data-s="{esc(s["held_on"])} {esc(hour24(s["taken_at"]))}">'
            f'<a href="{r}asistencia/{esc(s["slug"])}.html">'
            f'{fecha(s["held_on"])}</a></td>'
            f'<td>{esc(s["taken_at"])}</td>'
            f'<td data-s="{ATT_LETTER.get(kind, "·")}">{att_mark(lab, kind, tone)}</td>'
            f'<td class="sm mut"><b>{esc(lab)}.</b> {esc(why)}</td></tr>')
    faltas = den - ok
    return f"""<section><h2>Asistencia al Pleno</h2>
<dl class="tiles">
<div class="tile"><dt>Asistió</dt><dd>{ok} de {den}<small>{esc(rate)}{cmp_txt}</small></dd></div>
<div class="tile"><dt>Inasistencias</dt><dd>{faltas}<small>tomas sin presencia ni
licencia</small></dd></div>
<div class="tile"><dt>Con licencia</dt><dd>{lic}<small>permiso de la Mesa Directiva:
fuera del denominador</small></dd></div>
{f'<div class="tile"><dt>Presidiendo</dt><dd>{pre}<small>dirigía la sesión: tampoco es falta</small></dd></div>' if pre else ""}
</dl>
<div style="margin-top:22px">{att_strip(d, L, r)}</div>
<div class="scroll" style="margin-top:18px"><table class="srt"><thead><tr><th>Sesión</th>
<th>Hora de la toma</th><th>Estado</th><th>Qué significa</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<p class="sm mut">La unidad es la toma, no el día: cada sesión pasa lista varias
veces. {len(xs)} tomas de {esc(CHAMBER[L["chamber"]])};
<a href="{r}asistencia.html">todas, sesión por sesión</a>.</p></section>"""


def render_leg(d, L, base):
    r = "../"
    slug = L["slug"]
    per = f'{L["per_par"]}-{L["per_par"] + 5}'
    cur = L["per_par"] >= 2026
    if L["party"] or L["district"]:
        dist = esc(L["district"] or "una circunscripción no registrada")
        lede = (f'Representa a <b>{dist}</b> en {esc(EN[L["chamber"]])} '
                f'durante el periodo {per}. Integra el grupo parlamentario '
                f'<b>{esc(L["party"] or "no registrado")}</b>.')
        thin = ""
    else:
        # The 2021-2026 roster survives only as names in the bills API: no
        # party, no district, no photo. Say that instead of rendering blanks.
        lede = f'Integró {esc(EN[L["chamber"]])} en el periodo {per}.'
        thin = ('<div class="note">Ficha mínima: del padrón 2021-2026 el Congreso '
                'solo mantiene publicado el nombre. Bancada, circunscripción, foto '
                'y contacto no sobrevivieron al cambio de periodo. Su rastro '
                'legislativo, abajo, sí está completo.</div>')
    # 22 people sat in both Congresses and had two pages that never referenced
    # each other: their record read as two strangers with the same name.
    twin = ""
    for o in d["twin"].get(slug, []):
        opar = f'{o["per_par"]}-{o["per_par"] + 5}'
        twin += (
            f'<div class="note">Es la misma persona que '
            f'<a href="{leg_url(r, o["slug"])}">'
            f'{esc(nice_name(o["full_name"]))}, {esc(CHAMBER[o["chamber"]])} '
            f'{opar}</a>: fue reelegida, así que su trayectoria va en dos fichas, '
            f'una por Congreso. Cruce por nombre normalizado, el único '
            f'identificador común entre ambos padrones.</div>')
    all_bills = sorted(d["leg_bills"].get(slug, []),
                       key=lambda x: (d["bill_by_id"][x[0]]["presented_on"] or ""),
                       reverse=True)
    # Twenty-two of the 190 also sat in the 2021-2026 unicameral Congress and
    # their old bills match by name. Counting those as work of this period
    # would put a senator at 530 against a chamber median of 0. But "this
    # period" is the member's own, not always 2026: hardcoding that printed
    # "0 proyectos firmados" on all 140 unicameral pages, directly above the
    # list of the bills they had in fact signed.
    mine = [x for x in all_bills
            if d["bill_by_id"][x[0]]["per_par"] == L["per_par"]]
    past = [x for x in all_bills
            if d["bill_by_id"][x[0]]["per_par"] != L["per_par"]]
    prim = [x for x in mine if (x[1] or 0) == 0]
    mots = d["leg_motions"].get(slug, [])
    vts = d["leg_votes"].get(slug, [])

    def baseline(peers, unit):
        return (f"<small>mediana de la cámara {median(peers)} · "
                f"máximo {max(peers)} {unit}</small>")

    stats = (f'<dl class="tiles">'
             f'<div class="tile"><dt>Proyectos firmados</dt><dd>{len(mine)}'
             f'{baseline(base["bills"][L["chamber"]], "proyectos")}</dd></div>'
             f'<div class="tile"><dt>Como autor principal</dt><dd>{len(prim)}'
             f'{baseline(base["prim"][L["chamber"]], "proyectos")}</dd></div>'
             f'<div class="tile"><dt>Mociones firmadas</dt><dd>{len(mots)}'
             f'{baseline(base["mots"][L["chamber"]], "mociones")}</dd></div>')
    # A presence rate is only honest over roll calls the member could have
    # voted in. Chairing the session and holding a licencia are not absences.
    # Publishing them as 0% attendance is a falsehood about a named person.
    votable = [(vv, p) for vv, p in vts if pos(p)[1] in ("voto", "falta")]
    excused = [(vv, p) for vv, p in vts if p == "LICENCIA"]
    chaired = [(vv, p) for vv, p in vts if p == "PRESIDENCIA"]
    other_ex = [(vv, p) for vv, p in vts
                if pos(p)[1] == "excusa" and p not in ("LICENCIA", "PRESIDENCIA")]
    if votable:
        present = sum(1 for _, p in votable if pos(p)[1] == "voto")
        pct = round(100 * present / len(votable))
        peers = base["asis"][L["chamber"]]
        stats += (f'<div class="tile"><dt>Emitió voto</dt><dd>{present} de {len(votable)}'
                  f'<small>' + (f'{pct}% · mediana de la cámara {median(peers)}%'
                                if len(votable) >= 5 else
                                f'son muy pocas votaciones para un porcentaje '
                                f'con sentido')
                  + (f' · {len(excused)} con licencia' if excused else "")
                  + (f' · {len(chaired)} presidiendo' if chaired else "")
                  + "</small></dd></div>")
    elif vts:
        why = " y ".join(x for x in [
            f"{len(excused)} con licencia" if excused else "",
            (f"{len(chaired)} presidiendo la sesión" if len(chaired) != 1
             else "una presidiendo la sesión") if chaired else "",
            f"{len(other_ex)} sin sentido de voto en el acta" if other_ex else "",
        ] if x)
        stats += (f'<div class="tile"><dt>Emitió voto</dt><dd>—<small>en las {len(vts)} '
                  f'votaciones publicadas no le correspondía votar: {why}'
                  "</small></dd></div>")
    stats += "</dl>"

    def bill_list(title, xs, note=""):
        if not xs:
            return ""
        return (f'<section><h2>{title} ({len(xs)})</h2>{note}'
                + ("<p class='sm mut'>Se listan los 60 más recientes.</p>"
                   if len(xs) > 60 else "")
                + '<ul class="feed">' + "".join(
                    bill_row(r, d["bill_by_id"][b],
                             extra=(" · autor principal" if (rk or 0) == 0 else ""))
                    for b, rk in xs[:60]) + "</ul></section>")

    bl = bill_list("Proyectos de ley firmados en este periodo", mine)
    bl += bill_list(
        "Proyectos firmados en el Congreso 2021-2026", past,
        "<p class='sm mut'>Cruzados por nombre con el registro anterior. No "
        "cuentan en las cifras de arriba.</p>")
    ml = ""
    if mots:
        by_id = {m["id"]: m for m in d["motions"]}
        ml = (f'<section><h2>Mociones firmadas ({len(mots)})</h2><ul class="feed">'
              + "".join(
                  f'<li><span class="m">{esc(by_id[m]["code"])} · '
                  f'{fecha(by_id[m]["presented_on"])}'
                  f'{" · promotor" if (rk or 0) == 0 else ""}</span>'
                  f'<a class="t" href="{r}mociones.html#m-{esc(m)}">'
                  f'{esc(clip(by_id[m]["summary"], 170))}</a></li>'
                  for m, rk in mots[:40]) + "</ul></section>")
    vl = ""
    if vts:
        vrows, broke, caveat = [], [], False
        for vv, p in vts:
            out = d["voutcome"][vv["id"]]
            bloc = d["vbloc"].get((vv["id"], L["party"] or "Sin grupo"))
            odd = p in ("SI", "NO", "ABST") and bloc and p != bloc
            if odd:
                broke.append((vv, p, bloc))
            key, lab, tone = d["vstate"][vv["id"]]
            if key == "disputado":
                caveat = True
            vrows.append(
                f'<tr><td data-s="{esc(vv["held_on"] or "")}">{fecha(vv["held_on"])}</td>'
                f'<td><a href="{vote_url(r, vv)}">'
                f'{esc(clip(vv["subject"] or vv["id"], 120))}</a>'
                + (f' <span class="chip {tone}">{esc(lab)}</span>' if lab else "")
                + f'</td><td>{esc(pos(p)[0])}'
                + (' <b>(rompió con su bancada)</b>' if odd else "")
                + f'</td><td>{esc(out)}</td></tr>')
        vl = ('<section><h2>Votaciones</h2><div class="scroll"><table class="srt">'
              '<thead><tr><th>Fecha</th><th>Asunto</th><th>Su voto</th>'
              '<th>Resultado</th></tr></thead><tbody>' + "".join(vrows)
              + "</tbody></table></div>"
              + ('<p class="sm mut">En las votaciones marcadas las fuentes '
                 'oficiales no coinciden. Esta fila sale de nuestra lectura del '
                 'PDF, y la votación enlazada explica por qué.</p>' if caveat else "")
              + (f'<p class="sm mut">Se apartó de la mayoría de '
                 f'{esc(L["party"] or "su grupo")} en '
                 f'{len(broke)} de {len(vts)} votaciones.</p>'
                 if broke else
                 f'<p class="sm mut">Votó siempre con la mayoría de '
                 f'{esc(L["party"] or "su grupo")}.</p>'
                 if any(p in ("SI", "NO", "ABST") for _, p in vts) else "")
              + "</section>")

    # Comisiones. The block GovTrack has and we did not: where this person
    # actually works. Senate only, because only the Senate's cuadros have been
    # published in a diario. A deputy gets the reason, not a blank card.
    mem = d["leg_cttes"].get(slug, [])
    firm = [m for m in mem if not m["amendment"]]
    chg = [m for m in mem if m["amendment"]]
    ct_html = ""
    if firm or chg:
        seats = {m["committee_id"]: m["role"] for m in firm}

        def ct_li(m, mark=False):
            c = d["cttes"][m["committee_id"]]
            # After folding the oficio-only committees into the real ones, a
            # change can land on a committee already listed above. Saying which
            # is the change beats printing the same name twice with no comment.
            same = mark and m["committee_id"] in seats
            note = (f' <span class="rank">el oficio lo dejó como '
                    f'{esc(m["role"])} en lugar de {esc(seats[m["committee_id"]])}'
                    f'</span>' if same and seats[m["committee_id"]] != m["role"]
                    else ' <span class="rank">confirmado por oficio</span>'
                    if same else "")
            return (f'<li><a href="{ctte_url(r, c)}">{esc(c["name"])}</a> '
                    f'<span class="rank">{esc(m["role"])}</span>{note}</li>')
        order = {"titular": 0, "suplente": 1}
        firm.sort(key=lambda m: (order.get(m["role"], 2),
                                 d["cttes"][m["committee_id"]]["name"]))
        ntit = sum(1 for m in firm if m["role"] == "titular")
        ct_html = (
            f'<section><h2>Comisiones ({len(firm)})</h2>'
            f'<ul class="roll">{"".join(ct_li(m) for m in firm)}</ul>'
            f'<p class="sm mut" title="El titular ocupa la plaza; el suplente vota '
            f'y firma el dictamen cuando el titular falta.">Titular en {ntit}'
            + (f', suplente en {len(firm) - ntit}' if len(firm) > ntit else "")
            + '.</p>'
            + (f'<h3 style="margin-top:18px">Cambios posteriores ({len(chg)})</h3>'
               f'<p class="sm mut">Designaciones que su bancada cambió por oficio: '
               f'sustituyen a otra persona, no se suman a las de arriba.</p>'
               f'<ul class="roll">{"".join(ct_li(m, mark=True) for m in chg)}</ul>'
               if chg else "")
            + '<p class="sm mut">Fuente: Diario de los Debates del Senado; ninguna '
              'cámara lo publica como dato.</p></section>')
    elif L["chamber"] == "D":
        ct_html = ('<section><h2>Comisiones</h2><p>La Cámara de Diputados no ha '
                   'publicado el diario en que aprobó sus cuadros de comisiones, '
                   'que es la única fuente que existe.</p></section>')
    elif L["chamber"] == "S":
        ct_html = ('<section><h2>Comisiones</h2><p>No figura en ningún cuadro del '
                   'diario en que el Senado los aprobó. Puede haber sido designado '
                   'después por oficio, en un acta que aún no leemos.</p></section>')

    contact = []
    if L["email"]:
        contact.append(f'<div><dt>Correo</dt><dd><a href="mailto:{esc(L["email"])}">'
                       f'{esc(L["email"])}</a></dd></div>')
    if L["votes_received"]:
        contact.append(f'<div><dt>Votos que lo eligieron</dt>'
                       f'<dd>{esc(L["votes_received"])}</dd></div>')
    if L["source_url"]:
        contact.append(f'<div><dt>Ficha oficial</dt><dd>'
                       f'<a href="{esc(L["source_url"])}">'
                       f'{esc(CHAMBER[L["chamber"]])} ↗</a></dd></div>')
    # Only render the block if it holds a way to reach the person, not just a
    # link back to the chamber. One outbound link is not a contact section.
    contact_html = ""
    if L["email"] or (L["votes_received"] and L["source_url"]):
        contact_html = (
            "<section><h2>Contacto</h2><dl class='kv'>" + "".join(contact) + "</dl>"
            + "<p class='sm mut'><b>Este sitio no es el Congreso.</b> No "
              "transmitimos mensajes ni gestionamos trámites. Escríbale por los "
              "canales oficiales de arriba.</p>"
            + "</section>")
    elif L["source_url"]:
        contact_html = (
            "<section><h2>Contacto</h2><p>El portal "
            f"{esc(CHAMBER[L['chamber']])} no publica un correo suyo; su "
            f"<a href=\"{esc(L['source_url'])}\">ficha oficial ↗</a> es el único "
            "canal directo que existe.</p>"
            "<p class='sm mut'><b>Este sitio no es el Congreso.</b></p></section>")
    bio = f'<section><h2>Reseña</h2><p>{esc(L["bio"])}</p></section>' if L["bio"] else ""

    # Where this person sits, drawn in their own chamber. One seat lit, the rest
    # in the bench colours: it places them among 130 colleagues in one glance and
    # is the same picture the chamber page and the vote pages use.
    seatmap = ""
    if cur:
        peers_ch = [P for P in d["legs"] if P["chamber"] == L["chamber"]
                    and P["per_par"] == L["per_par"]]
        if len(peers_ch) > 4:
            seats, hg = bench_seats(
                peers_ch, r,
                colour=lambda P: ("var(--ink)" if P["slug"] == slug
                                  else f"var(--gp{bench_slot(P['party'])})"),
                cls=lambda P: ("diamond", 0, "me") if P["slug"] == slug
                else ("circle", 0, ""))
            seatmap = f"""<section><h2>Dónde se sienta</h2>
<figure class="hemifig" title="{CURULES}">{hemicycle(
    seats, r, sub=f"Escaño de {nice_name(L['full_name'])} en {CHAMBER[L['chamber']]}",
    ident="hemi-seat", groups=hg)}
<figcaption>Su escaño es el rombo, entre los {len(peers_ch)} de
{esc(CHAMBER[L["chamber"]])}.</figcaption></figure></section>"""

    body = f"""
<div class="crumb"><a href="{r}index.html">Inicio</a> ›
<a href="{r}parlamentarios.html">Parlamentarios</a> ›
{esc(CHAMBER[L["chamber"]])}</div>
<span class="eyebrow">{esc(CHAMBER[L["chamber"]])} · {per}</span>
<h1>{esc(nice_name(L["full_name"]))}</h1>
<div class="profile">
{avatar(L, 132, 165, "face port", r)}
<div><p class="lede" style="margin-top:0">{lede}</p>
<div class="meta">{party_chip(L["party"], r)} {district_chip(L["district"], r)}
<span class="chip {L["chamber"].lower()}">{esc(CHAMBER_SHORT[L["chamber"]])}</span>
</div></div></div>
{thin}
{twin}
<section><h2>Actividad, comparada con su cámara</h2>{stats}
<p class="sm mut">Medianas sobre los {len(base["bills"][L["chamber"]])} integrantes
de la cámara, no sobre una muestra. El periodo {per} corre hasta julio de
{L["per_par"] + 5}.</p></section>
{peer_strips(d, L, base, r)}
{seatmap}
{att_block(d, L, base)}
{gasto_block(d, L, r)}
{ct_html}
{contact_html}
{bio}
{bl}
{ml}
{vl}
<section><h2>Descarga y cita</h2>
<p><a href="{esc(slug)}.csv" title="Proyectos, mociones, votaciones, comisiones y
cada toma de asistencia, con fecha, papel y enlace a la página donde consta.">
Descargar su expediente en CSV</a>, una fila por hecho registrado.</p>
<p class="sm mut">Cita sugerida: «{esc(nice_name(L["full_name"]))},
{esc(CHAMBER[L["chamber"]])} {per}», consultado el
{fecha(dt.date.today().isoformat())}.</p></section>
{prov([
    f'Ficha, foto, bancada y circunscripción: API del portal de la '
    f'{esc(CHAMBER[L["chamber"]])}'
    + (f' (<a href="{esc(L["source_url"])}">ficha oficial ↗</a>)' if L["source_url"] else "")
    + '.',
    (f'Asistencia: las listas en PDF de cada sesión, varias con el sello '
     f'PROVISIONAL de la propia cámara; <a href="{r}asistencia.html">cada '
     f'documento, enlazado</a>.') if d["leg_att"].get(slug) else "",
    f'Firmas de proyectos y mociones: <code>{API}</code>, cruzadas por nombre '
    'normalizado a falta de un identificador compartido.',
    f'Sitio regenerado el {fecha(dt.date.today().isoformat())}.',
])}
"""
    return shell(f'{nice_name(L["full_name"])} · {CHAMBER_SHORT[L["chamber"]]}',
                 body, depth=1,
                 desc=f'{nice_name(L["full_name"])}, {CHAMBER[L["chamber"]]} por '
                      f'{L["district"]}, {L["party"]}.')


# ---------------------------------------------------------------- vote pages

# One place for the whole vote vocabulary: label, class, order and definition.
# `class` is what keeps an unknown or excused state from being published as an
# absence. The default for anything unrecognised is "no le tocaba votar", never
# "faltó", because guessing the wrong way libels a named person.
POS = {
    "SI": ("A favor", "voto", 0,
           "Voto a favor de la propuesta."),
    "NO": ("En contra", "voto", 1,
           "Voto en contra."),
    "ABST": ("Abstención", "voto", 2,
             "Estuvo presente y decidió no tomar posición. Cuenta para el "
             "quórum, no para la mayoría."),
    "BLANCO": ("En blanco", "voto", 3,
               "Marcó su tarjeta sin optar por ninguna posición. Participa en "
               "la sesión pero su voto no suma a ninguna de las tres opciones."),
    "AUSENTE": ("Ausente", "falta", 4,
                "No estuvo en la sala al momento de la votación y no tenía "
                "permiso: es una inasistencia."),
    "LICENCIA": ("Con licencia", "excusa", 5,
                 "Permiso concedido por la Mesa Directiva. La falta está "
                 "justificada y no cuenta como inasistencia."),
    "PRESIDENCIA": ("Presidió la sesión", "excusa", 6,
                    "Dirigía el debate. Los propios reportes advierten que «en "
                    "este reporte de votación no se considera al congresista "
                    "que ejerce la presidencia»: no es una ausencia."),
    "OTRO": ("Sin voto registrado", "excusa", 7,
             "El acta no le asigna ningún sentido de voto y no dice por qué."),
    "SINRES": ("Sin respuesta", "excusa", 8,
               "El tablero lo publica como «Sin Res.»: registrado en la sesión "
               "sin marcar ningún sentido de voto. No lo contamos como "
               "inasistencia."),
}


def pos(p):
    """Never KeyErrors and never turns an unseen state into an absence."""
    return POS.get(p, (p or "Sin dato", "excusa", 9,
                       "Estado que el acta publica y que todavía no hemos "
                       "traducido. No lo contamos como inasistencia."))


POS_LABEL = {k: v[0] for k, v in POS.items()}
# Reserved status colours: what a vote MEANS, never a bench identity. Kept in
# one place so the seat plan, the bar, the legend and the chips cannot drift.
POS_COL = {"SI": "var(--si)", "NO": "var(--no)", "ABST": "var(--abst)",
           "BLANCO": "var(--muted)", "AUSENTE": "var(--aus)",
           "LICENCIA": "var(--lic)", "PRESIDENCIA": "var(--pres)",
           "OTRO": "var(--muted)"}
POS_CHIP = {"SI": "si", "NO": "no", "ABST": "abst", "AUSENTE": "dead"}


def pos_col(p):
    return POS_COL.get(p, "var(--muted)")


def pos_shape(p):
    """The silhouette this state is drawn with, in the seat plan and in every
    legend that decodes one. One lookup, so the two cannot drift."""
    return POS_SHAPE.get(p, ("ring", 1))

# ------------------------------------------------------------------ asistencia

# The plain attendance taking, which is a different record from a roll call: it
# says who was in the room, not how they voted. Same discipline as POS. The
# default for an unrecognised code is an excuse, never an absence.
ATT = {
    "PRE": ("Presente", "presente",
            "Registró su asistencia en el control de la sesión."),
    "AUS": ("Ausente", "falta",
            "No registró asistencia en esa toma y no consta que tuviera "
            "licencia: cuenta como inasistencia."),
    "LO": ("Licencia oficial", "excusa",
           "Licencia concedida por la Mesa Directiva para una comisión de "
           "servicios o un viaje de representación. No es una inasistencia."),
    "LE": ("Licencia por enfermedad", "excusa",
           "Licencia por enfermedad acreditada ante la Mesa Directiva. No es "
           "una inasistencia."),
    "LP": ("Licencia personal", "excusa",
           "Licencia personal concedida por la Mesa Directiva. Está "
           "justificada y no es una inasistencia."),
    "L": ("Licencia", "excusa",
          "Licencia registrada sin que la lista precise el motivo. No es una "
          "inasistencia."),
}


# La marca de una letra con la que las listas imprimen cada estado: A asistió,
# F faltó (en rojo), L licencia, P presidió. Siempre con el nombre completo en
# un title, en la celda de al lado o en el glosario: una letra suelta jamás
# puede convertir una licencia en falta.
ATT_LETTER = {"presente": "A", "falta": "F", "excusa": "L", "presidencia": "P"}


def att(st):
    return ATT.get((st or "").upper(),
                   (st or "Sin dato", "excusa",
                    "Estado que la lista de asistencia publica y que todavía no "
                    "hemos traducido. No lo contamos como inasistencia."))


def att_mark(lab, kind, tone):
    """The one-letter chip. The full label always travels with it."""
    return (f'<span class="chip {tone}" title="{esc(lab)}" aria-hidden="true">'
            f'{ATT_LETTER.get(kind, "·")}</span>'
            f'<span class="vh">{esc(lab)}</span>')


def att_state(d, s, x):
    """(label, kind, why) for one member in one taking.

    Two states are never inasistencia and both cost a real person their record
    if we get them wrong: a licencia is a justified absence, and whoever is in
    the chair is running the sitting, not skipping it.
    """
    st = (x["status"] or "").upper()
    if x["leg"] and (s["chamber"], s["held_on"], x["leg"]["slug"]) in d["presided"] \
            and st != "PRE":
        return ("Presidió la sesión", "presidencia",
                "Dirigía el debate en esa sesión: no es una inasistencia, "
                "aunque la lista no le registre marca de presente.")
    return att(st)


def att_tally(d, s, rows):
    """One taking, counted. `base` is the ONLY denominator any attendance rate
    on this site may divide by: a licencia is a permission and the chair is
    running the sitting, so neither is an opportunity to turn up. Dividing by
    `total` instead published 92 % where the fichas said 96 %, two different
    medians for the same chamber and nothing reconciling them."""
    c = {"presente": 0, "falta": 0, "excusa": 0, "presidencia": 0}
    for x in rows:
        c[att_state(d, s, x)[1]] += 1
    c["total"] = len(rows)
    c["base"] = c["presente"] + c["falta"]        # what a rate may divide by
    c["asistieron"] = c["presente"] + c["presidencia"]
    c["pct"] = round(100 * c["presente"] / c["base"]) if c["base"] else None
    return c


def hour24(t):
    """'04:17 PM' -> '16:17', so takings of one day sort in the order they were
    taken. Unparseable strings sort last rather than crashing the build."""
    try:
        return dt.datetime.strptime((t or "").strip().upper(),
                                    "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return t or "zz"


def pct_or_note(n, base, unit="tomas de asistencia"):
    """A percentage over a handful of events is noise dressed as a fact. Below
    five the page says the count and why it is not giving a rate."""
    if base >= 5:
        return f"{round(100 * n / base)}%", True
    return (f"son muy pocas {unit} para un porcentaje con sentido"), False


# -------------------------------------------------------------- base rates

def base_rates(d):
    """What actually happens to a bill, measured instead of asserted.

    The bill page claimed on 5 545 pages that «la mayoría de proyectos se quedan
    aquí hasta que termina el periodo». The 2021-2026 Congress is over and its
    14 864 expedientes are in the DB, so the claim is checkable. And it was
    wrong. These are the real rates, computed once per build over the completed
    period, which is the only one that can answer the question at all.
    """
    per = 2021
    bills = [b for b in d["bills"] if b["per_par"] == per]
    r = {"per": per, "n_bills": len(bills), "stage": {}, "dias": []}
    for b in bills:
        acts = d["acts"].get(b["id"], [])
        seen = {status_info(a["text"])[0] for a in acts}
        seen.add(status_info(b["status"])[0])
        seen.add("PRES")
        ley = "LEY" in seen
        dic = any((a["text"] or "").strip().upper() == "DICTAMEN" for a in acts)
        stuck = status_info(b["status"])[0]
        for s in seen:
            k = r["stage"].setdefault(s, {"n": 0, "ley": 0, "dictamen": 0,
                                          "quedo": 0})
            k["n"] += 1
            k["ley"] += ley
            k["dictamen"] += dic
            # "Se quedó aquí" means the trámite ended at this stage. A bill
            # whose last state is DICTAMEN is in stage COM but did leave the
            # comisión's hands, so counting it as stuck there would inflate the
            # very claim this block exists to check.
            k["quedo"] += (stuck == s and not ley
                           and not (s == "COM" and dic))
        if dic and b["presented_on"]:
            first = min((a["acted_on"] for a in acts
                         if (a["text"] or "").strip().upper() == "DICTAMEN"
                         and a["acted_on"]), default=None)
            if first and first >= b["presented_on"]:
                try:
                    r["dias"].append(
                        (dt.date.fromisoformat(first[:10])
                         - dt.date.fromisoformat(b["presented_on"][:10])).days)
                except ValueError:
                    pass
    r["mediana_dias"] = median(r["dias"])
    # Firmas per bill, by periodo: the ficha prints a count with nothing to
    # compare it against.
    r["firmas"] = {}
    for b in d["bills"]:
        r["firmas"].setdefault(b["per_par"], []).append(len(d["spon"].get(b["id"], [])))
    r["firmas"] = {k: median(v) for k, v in r["firmas"].items()}
    return r


def rate_block(d, b, stage):
    """The numbers behind the sentence, for the stage this bill is actually in.
    Says nothing at all when the completed period cannot answer honestly."""
    r = d["rates"]
    k = r["stage"].get(stage)
    if not k or k["n"] < 100:
        return ""
    pc = lambda n: pctxt(n, k["n"])  # noqa: E731
    per = f'{r["per"]}-{r["per"] + 5}'
    if stage == "COM":
        txt = (
            f'De los <b>{num(k["n"])}</b> proyectos del Congreso {per} que '
            f'llegaron a una comisión, <b>{num(k["dictamen"])}</b> ({pc(k["dictamen"])}) '
            f'recibieron dictamen y <b>{num(k["ley"])}</b> ({pc(k["ley"])}) terminaron '
            f'publicados como ley. <b>{num(k["quedo"])}</b> ({pc(k["quedo"])}) '
            f'se quedaron en comisión sin dictamen hasta que el periodo se '
            f'cerró: para ellos la comisión fue el final del trámite.')
        if r["dias"]:
            edad = ""
            if b["presented_on"]:
                try:
                    days = (dt.date.today()
                            - dt.date.fromisoformat(b["presented_on"][:10])).days
                    edad = (f' Este lleva <b>{num(days)}</b> días desde su '
                            f'presentación.')
                except ValueError:
                    pass
            txt += (f' Entre los que sí fueron dictaminados, la mediana fue de '
                    f'<b>{num(r["mediana_dias"])}</b> días entre la presentación y '
                    f'el dictamen, sobre {num(len(r["dias"]))} proyectos con ambas '
                    f'fechas registradas.{edad}')
    else:
        txt = (f'De los <b>{num(k["n"])}</b> proyectos del Congreso {per} que '
               f'alcanzaron esta etapa, <b>{num(k["ley"])}</b> ({pc(k["ley"])}) '
               f'llegaron a publicarse como ley y <b>{num(k["quedo"])}</b> '
               f'({pc(k["quedo"])}) se quedaron exactamente aquí.')
    return (f'<section><h2>Qué suele pasar en esta etapa</h2><p>{txt}</p>'
            f'<p class="sm mut" title="El {per} es el único periodo terminado, y '
            f'por eso el único que puede responder la pregunta.">Frecuencias '
            f'históricas del periodo {per}, no un pronóstico sobre este '
            f'expediente.</p></section>')

# Not every row of a roll call comes from the same place. A vote read aloud and
# recorded in the Diario de los Debates is a different kind of evidence from a
# mark on the electronic board, and the reader should be able to see which.
SRC = {"diario": ("voto oral",
                  "Voto emitido de viva voz y recogido del Diario de los "
                  "Debates, no del tablero electrónico."),
       "constancia": ("constancia",
                      "Constancia dejada por el parlamentario después de la "
                      "votación, incorporada al acta.")}
GP_FULL = {"FP": "Fuerza Popular", "JP": "Juntos por el Perú",
           "PBG": "Partido del Buen Gobierno", "RP": "Renovación Popular",
           "PCO": "Partido Cívico Obras", "OBRAS": "Partido Cívico Obras",
           "AN": "Ahora Nación"}
LEGAL = {"D": 130, "S": 60, "C": 130}
DE = {"D": "de la Cámara de Diputados", "S": "del Senado", "C": "del Congreso"}


def row_party(x):
    """The roster PDF abbreviates the bench; the padrón spells it out."""
    if x["leg"] and x["leg"]["party"]:
        return x["leg"]["party"]
    p = (x["party_raw"] or "").strip()
    return GP_FULL.get(p.upper(), p) or "Sin grupo"


def tallies(v, rows):
    """The three numbers a Peruvian roll call can have, in order of authority:
    the correction the chair read into the record, the tally the acta prints,
    and our own reading of the nominal list. They disagree often enough that
    the page has to be able to show more than one."""
    c = {}
    for x in rows:
        c[x["position"]] = c.get(x["position"], 0) + 1
    out = []
    if v["n_yes_final"] is not None:
        out.append(("final", "Corregido en sala",
                    (v["n_yes_final"], v["n_no_final"] or 0, v["n_abstain_final"] or 0),
                    v["final_source_url"] or v["source_url"]))
    if v["n_yes"] is not None:
        out.append(("acta", "Conteo impreso en el acta",
                    (v["n_yes"], v["n_no"] or 0, v["n_abstain"] or 0), v["source_url"]))
    if rows:
        out.append(("lista", "Nuestra lectura de la lista nominal",
                    (c.get("SI", 0), c.get("NO", 0), c.get("ABST", 0)), None))
    return out, c


def render_vote(d, v):
    r = "../"
    rows = d["vrows"].get(v["id"], [])
    ch = v["chamber"] or "D"
    tal, counts = tallies(v, rows)
    kind, kind_label, (yes, no, ab), _ = tal[0] if tal else (
        "lista", "Sin datos", (0, 0, 0), None)
    lic, aus = counts.get("LICENCIA", 0), counts.get("AUSENTE", 0)
    blank = counts.get("BLANCO", 0)
    chair = counts.get("PRESIDENCIA", 0)
    other = sum(n for k, n in counts.items()
                if pos(k)[1] == "excusa" and k != "LICENCIA")
    total = len(rows) or (yes + no + ab + (v["n_absent"] or 0))
    emitted = yes + no + ab
    novote = max(total - emitted, 0)
    # Rows the headline tally cannot account for. Two members vanished from an
    # earlier version of this page because we let them.
    unrec = max(total - emitted - lic - aus - other - blank, 0)
    disputed = len({t[2] for t in tal}) > 1

    def pc(n, base):
        return f"{round(100 * n / base)}%" if base else "—"

    # A corrected figure and a contradictory one are not the same thing: one is
    # the record working, the other is the record failing.
    resolved = v["n_yes_final"] is not None
    roster_ok = not rows or (yes, no, ab) == tal[-1][2]
    _k, _lab, _tone = d["vstate"][v["id"]]
    flag = (f'<a href="#lectura" class="chip {_tone}">'
            f'{"✓" if _k == "corregido" else "⚠"} {esc(_lab)}</a>') if _lab else ""

    # tally by grupo parlamentario
    parties = {}
    for x in rows:
        p = row_party(x)
        parties.setdefault(p, {}).setdefault(x["position"], 0)
        parties[p][x["position"]] += 1

    # ---- the seat plan of this roll call. One seat per person on the nominal
    # list, seated by bench in palette order and coloured by how they voted, so
    # a bench that split is visible as a break in the arc before you read a
    # single number. Every seat is an <a> to that member's ficha, in the markup.
    hemi = ""
    if rows:
        seat_rows = sorted(rows, key=lambda x: (bench_slot(row_party(x)) or 99,
                                                row_party(x),
                                                x["leg"]["full_name"] if x["leg"]
                                                else x["name_raw"]))
        seats, hgroups, last = [], [], object()
        for x in seat_rows:
            nm = nice_name(x["leg"]["full_name"] if x["leg"] else x["name_raw"])
            p = row_party(x)
            if p != last:
                hgroups.append([bench_mono(p), 0])
                last = p
            hgroups[-1][1] += 1
            seats.append((pos_col(x["position"]),
                          f'{nm}, {p}, {pos(x["position"])[0]}',
                          leg_url(r, x["leg"]["slug"]) if x["leg"] else "",
                          pos_shape(x["position"]),
                          seat_card(x["leg"], r, pos(x["position"])[0])))
        seen_pos = sorted(counts, key=lambda k: pos(k)[2])
        # The centre number is the count of the seats actually drawn, never the
        # headline tally: on a disputed roll call those differ by up to six, and
        # a picture that contradicts its own legend is worse than no picture.
        drawn_yes = counts.get("SI", 0)
        src_note = ("" if (yes, no, ab) == (counts.get("SI", 0), counts.get("NO", 0),
                                            counts.get("ABST", 0))
                    else f' <b>El diagrama y el encabezado no dicen lo mismo.</b> El arco '
                         f'dibuja nuestra lectura de la lista, {drawn_yes} a favor; '
                         f'el encabezado dice {yes}. '
                         f'<a href="#lectura">Por qué</a>.')
        # ponytail: la concesión de tamaño vive en el title del <figure>, no en
        # 130 palabras de pie de foto. Sigue siendo una divulgación; deja de ser
        # un párrafo de disculpa encima del gráfico.
        hemi = f"""<figure class="hemifig" title="En un teléfono cada escaño mide
unos 8 px: a ese tamaño el diagrama es una ilustración y la lista nominal de
abajo es la versión legible.">
{hemicycle(seats, r, middle=(str(drawn_yes), "a favor"),
           sub=f"Cómo votó cada escaño de {CHAMBER[ch]}", ident="hemi-vote",
           groups=[tuple(g) for g in hgroups])}
<figcaption>Nombre por nombre, en la
<a href="#quien">lista ordenable</a>.{src_note}</figcaption></figure>"""
    else:
        # One roll call of five had no diagram at all, because the acta has no
        # nominal list. The honest picture is not "no picture": it is the
        # chamber, whole, with every seat drawn as an empty ring, the shape
        # this site already uses for «no consta cómo votó». What the record is
        # missing is a fact about the record, and it is worth a graphic.
        mem = [L for L in d["legs"]
               if L["chamber"] == ch and L["per_par"] >= 2026]
        if mem:
            # No links on these seats: the WCAG 2.5.8 «Equivalent» exception
            # the arc relies on is the full-size roster further down the same
            # page, and on this page there is none to have. The diagram is a
            # statement about the record, so it is a picture and not a control.
            seats, hg = bench_seats(mem, r, colour=lambda L: "var(--off)",
                                    cls=lambda L: ("ring", 1), link=False)
            hemi = f"""<figure class="hemifig">
{hemicycle(seats, r, middle=(str(len(mem)), "sin voto en acta"),
           sub=f"Escaños de {CHAMBER[ch]}: esta votación no tiene lista nominal",
           ident="hemi-vote", groups=hg)}
<figcaption>Los {len(mem)} escaños {esc(DE[ch])}, todos huecos: el Congreso no
publicó lista nominal de esta votación, así que no consta cómo votó ninguno.
Quiénes son, en <a href="{r}parlamentarios.html">el padrón</a>.</figcaption></figure>"""

    # ---- how each bench voted, as a row of 100% bars. The table below has the
    # exact numbers; this answers "did anyone split" without reading it.
    bchart = ""
    if parties:
        seen_pos = sorted(counts, key=lambda k: pos(k)[2])
        prows = []
        # Six bars all drawn to the full width is not a bar chart: a bench of 41
        # and a bench of 10 had the same visual mass. The bar's LENGTH is now
        # the bench's size and its segments are the split inside it.
        biggest = max(sum(c.values()) for c in parties.values()) or 1
        for p, c in sorted(parties.items(),
                           key=lambda kv: (bench_slot(kv[0]) or 99, kv[0])):
            tot = sum(c.values())
            top = max(("SI", "NO", "ABST"), key=lambda k: c.get(k, 0))
            broke = sum(n for k, n in c.items()
                        if k in ("SI", "NO", "ABST") and k != top)
            prows.append(
                f'<div class="brow"><a class="bl {gp(p)}" href="{bench_url(r, p)}">'
                f'{bench_logo(p, r, 26)}<span class="nm">{esc(p)}</span></a>'
                + f'<div class="barw" style="width:{100 * tot / biggest:.1f}%">'
                + stack_bar([(pos_col(k), f"{pos(k)[0]} {c.get(k, 0)}", c.get(k, 0))
                             for k in seen_pos], tot)
                + "</div>"
                + f'<span class="bn">{tot}</span>'
                + (f'<span class="bs">{broke} se apartó</span>' if broke == 1 else
                   f'<span class="bs">{broke} se apartaron</span>' if broke else
                   '<span class="bs mut">en bloque</span>')
                + "</div>")
        bchart = ('<figure><div class="benchrows">' + "".join(prows) + "</div>"
                  + key_list([(pos_col(k), esc(pos(k)[0]), counts.get(k, 0))
                              for k in seen_pos],
                             shapes=[pos_shape(k) for k in seen_pos])
                  + '<figcaption>Largo de la barra: cuántos integrantes tiene la '
                    'bancada en esta lista. Tramos: en qué sentido votaron.'
                    '</figcaption></figure>')
    ptable = ""
    if parties:
        head = sorted(POS, key=lambda k: POS[k][2])
        head = [h for h in head if any(h in c for c in parties.values())]
        def agree(p):
            a, t = d["gp_agree"].get((ch, p), (0, 0))
            return f"{a} de {t}" if t > 1 else "—"

        # The same result was on this page three times in a row: the arc, six
        # bars, then a seven-row table. About 700 px saying one thing. The
        # table is the exact-numbers version, which is the one a reader asks
        # for second, so it folds. At 375px it was 807 px of table in a 347 px
        # window; folded, it costs one 44 px row until somebody wants it.
        ptable = ('<details class="gloss" style="margin-top:16px"><summary>'
                  'Las cifras exactas, bancada por bancada</summary>'
                  '<div class="scroll"><table class="srt"><thead><tr><th>Grupo parlamentario</th>'
                  + "".join(f'<th>{esc(pos(h)[0])}</th>' for h in head)
                  + '<th>Total</th><th>% a favor</th>'
                  '<th title="En cuántas votaciones nominales de esta cámara la '
                  'posición mayoritaria del grupo coincidió con la de la cámara.">'
                  'Con la mayoría</th></tr></thead><tbody>'
                  + "".join(
                      f'<tr><td class="who2"><a class="bl {gp(p)}" '
                      f'href="{bench_url(r, p)}">{bench_logo(p, r, 24)}'
                      f'<span class="nm">{esc(p)}</span></a></td>'
                      + "".join(f'<td class="num">{c.get(h, 0)}</td>' for h in head)
                      + f'<td class="num">{sum(c.values())}</td>'
                      f'<td class="num">{pc(c.get("SI", 0), sum(c.values()))}</td>'
                      f'<td class="num">{agree(p)}</td></tr>'
                      for p, c in sorted(parties.items(), key=lambda kv: -sum(kv[1].values())))
                  + f'<tr><td><b>Total</b></td>'
                  + "".join(f'<td class="num"><b>{counts.get(h, 0)}</b></td>' for h in head)
                  + f'<td class="num"><b>{len(rows)}</b></td>'
                  f'<td class="num"><b>{pc(counts.get("SI", 0), sum(counts.get(k, 0) for k in ("SI", "NO", "ABST")))}</b></td>'
                  '<td></td></tr>'
                  '</tbody></table></div>'
                  + ('<p class="sm mut">Este desglose sale de nuestra lectura de '
                     f'la lista, que no cuadra con el encabezado. {flag}</p>'
                     if not roster_ok else "")
                  + "</details>")

    # who broke with their bloc
    outliers = []
    for p, c in parties.items():
        if sum(c.values()) < 3:
            continue
        top = max(((k, n) for k, n in c.items() if k in ("SI", "NO", "ABST")),
                  key=lambda kv: kv[1], default=(None, 0))[0]
        for x in rows:
            if row_party(x) == p and x["position"] != top \
                    and x["position"] in ("SI", "NO", "ABST"):
                outliers.append((x, p, top))
    out_html = ""
    if rows and not outliers:
        out_html = ('<section><h2>Votaron distinto a su bancada</h2>'
                    '<p>Nadie: los '
                    f'{len([p for p, c in parties.items() if sum(c.values()) >= 3])} '
                    'grupos con tres o más presentes votaron en bloque.</p></section>')
    elif outliers:
        out_html = ('<section><h2>Votaron distinto a su bancada</h2>'
                    '<p class="sm mut">Frente a la posición mayoritaria de su propio '
                    'grupo en esta votación; se excluyen los grupos de menos de tres '
                    'presentes.</p><ul class="feed">' + "".join(
                        f'<li><span class="m">{esc(p)} votó mayoritariamente '
                        f'{esc(pos(top)[0])}</span>'
                        + (f'<a class="t" href="{leg_url(r, x["leg"]["slug"])}">'
                           f'{esc(nice_name(x["leg"]["full_name"]))}</a>'
                           if x["leg"] else
                           f'<span class="t">{esc(nice_name(x["name_raw"]))}</span>')
                        + f', {esc(pos(x["position"])[0])}</li>'
                        for x, p, top in outliers) + "</ul></section>")

    # Every word that appears in the bar, the stat block, the group table and
    # the filter dropdown, defined once where the reader meets them.
    gloss = ('<details class="gloss"><summary>Qué significa cada categoría '
             'de voto</summary><dl class="kv">' + "".join(
                 f'<div><dt>{esc(pos(k)[0])}</dt><dd>{esc(pos(k)[3])}</dd></div>'
                 for k in sorted(counts, key=lambda k: pos(k)[2]))
             + "</dl></details>")

    # The presiding officer is named in the lede and then appears as a
    # non-voter. The actas say why; nothing else on the page did.
    pres = ""
    if chair or other:
        who = (v["session"] or "").split(":", 1)[-1].strip() if v["session"] else ""
        pl = d["by_last"].get((ch, norm(who.split(",")[0]))) if who else None
        link = (f'<a href="{leg_url(r, pl["slug"])}">{esc(nice_name(pl["full_name"]))}</a>'
                if pl else esc(who))
        nn = chair or other
        pres = ('<p class="sm mut" title="Los reportes de votación advierten que '
                '«no se considera al congresista que ejerce la presidencia».">'
                + (f'Presidió {link}. ' if who else "")
                + f'{nn} '
                + ("integrante aparece" if nn == 1 else "integrantes aparecen")
                + ' sin sentido de voto por presidir la sesión; no cuenta como '
                  'inasistencia.</p>')

    # roster, rendered client-side so sort/filter can live in the URL
    # "Apellidos, Nombre": the order the chambers publish and the only one
    # where sorting the column alphabetically means anything.
    order = sorted(POS, key=lambda k: POS[k][2])
    data = ",".join(
        # 7 y 8: iniciales y si tenemos retrato local. draw() reconstruye la
        # misma cara que puso el servidor en lugar de perderla al reordenar.
        "[{},{},{},{},{},{},{},{},{}]".format(
            _js(x["leg"]["full_name"] if x["leg"] else x["name_raw"]),
            _js(x["leg"]["slug"] if x["leg"] else ""),
            _js(row_party(x)),
            _js((x["leg"]["district"] if x["leg"] else "") or "—"),
            _js(pos(x["position"])[0]),
            order.index(x["position"]) if x["position"] in order else 9,
            _js(SRC[x["source"]][0] if x["source"] in SRC else ""),
            _js(initials(x["leg"]["full_name"] if x["leg"] else x["name_raw"])),
            1 if x["leg"] and local_photo(x["leg"]["slug"]) else 0)
        for x in rows)
    # The bench table and the vote-colour table, handed to draw() so a re-sorted
    # row keeps the same logo and the same colour the server rendered.
    bl_js = "{" + ",".join(
        f'{_js(p)}:[{bench_slot(p)},{_js(BENCH[p][2])}]'
        for p in parties if p in BENCH) + "}"
    vc_js = ",".join(f"{_js(pos(k)[0])}:{_js(POS_CHIP.get(k, 'wait'))}"
                     for k in counts)
    roster = ""
    if rows:
        opts = "".join(f'<option>{esc(p)}</option>' for p in sorted(parties))
        vopts = "".join(f'<option>{esc(pos(p)[0])}</option>'
                        for p in sorted(counts))
        # Server-rendered in the default order (grupo, then apellidos). draw()
        # only takes over once the reader sorts or filters, so curl, crawlers
        # and reader mode see every legislator and every link.
        srv, g = [], None
        for x in sorted(rows, key=lambda x: (row_party(x),
                                             x["leg"]["full_name"] if x["leg"]
                                             else x["name_raw"])):
            p = row_party(x)
            if p != g:
                g = p
                srv.append(f'<tr class="grp"><td colspan="4">{esc(p)}</td></tr>')
            nm = x["leg"]["full_name"] if x["leg"] else x["name_raw"]
            cell = person_chip(x["leg"], r, nm)
            srv.append(
                f'<tr><td><span class="vote {POS_CHIP.get(x["position"], "wait")}">'
                f'<span class="dotmark"></span>{esc(pos(x["position"])[0])}</span>'
                + (f' <span class="chip wait" title="{esc(SRC[x["source"]][1])}">'
                   f'{esc(SRC[x["source"]][0])}</span>'
                   if x["source"] in SRC else "")
                + f'</td><td>{cell}</td>'
                f'<td class="who2"><span class="bl {gp(p)}">{bench_logo(p, r, 24)}'
                f'<span class="nm">{esc(p)}</span></span></td>'
                f'<td>{esc((x["leg"]["district"] if x["leg"] else "") or "—")}</td></tr>')
        roster = f"""<section id="quien"><h2>Cómo votó cada quien ({len(rows)})</h2>
{gloss}
<div class="filters">
<input id="q" type="search" placeholder="Busca a tu parlamentario o su región"
 aria-label="Buscar por nombre o circunscripción">
<select id="fgp" aria-label="Filtrar por grupo parlamentario">
<option value="">Todos los grupos</option>{opts}</select>
<select id="fv" aria-label="Filtrar por sentido del voto">
<option value="">Todos los votos</option>{vopts}</select>
</div>
<div class="scroll"><table id="t"><thead><tr>
<th><button data-c="4">Voto</button></th>
<th><button data-c="0">Parlamentario</button></th>
<th><button data-c="2">Grupo</button></th>
<th><button data-c="3">Circunscripción</button></th>
</tr></thead><tbody id="tb">{"".join(srv)}</tbody></table></div>
<p class="sm mut" id="cnt">{len(rows)} parlamentarios, por grupo.</p>
<script>
var BL={bl_js},VC={{{vc_js}}};
function face(x){{return '<span class="'+(x[8]
  ?'face shot fmini"><i aria-hidden="true">'+esc(x[7])
   +'</i><img loading="lazy" decoding="async" width="26" height="32" src="{r}photos/'
   +x[1]+'-160.jpg" alt="Retrato oficial de '+esc(x[0])+'">'
  :'noface fmini" aria-hidden="true">'+esc(x[7]))+"</span>";}}
function pchip(x){{var i=face(x)+'<span class="nm">'+esc(x[0])+"</span>";
 return x[1]?'<a class="pchip" href="{r}parlamentario/'+x[1]+'.html">'+i+"</a>"
  :'<span class="pchip pl">'+i+"</span>";}}
function blogo(p){{var b=BL[p];
 return '<span class="bl gp'+(b?b[0]:0)+'">'+(b
  ?'<span class="blogo" style="width:24px;height:24px"><img src="{r}logos/'+b[1]
   +'" alt="" width="24" height="24"></span>'
  :'<span class="blogo mono" style="width:24px;height:24px">'+esc(b2m(p))+"</span>")
  +'<span class="nm">'+esc(p)+"</span></span>";}}
function b2m(p){{return p.replace(/\\b(de|del|la|el|por|y|los)\\b/gi,"")
 .split(/[\\s-]+/).filter(Boolean).slice(0,3).map(function(w){{return w[0];}})
 .join("").toUpperCase()||"—";}}
var D=[{data}],S={{c:2,d:1,q:"",gp:"",v:""}},P=new URLSearchParams(location.hash.slice(1));
S.c=+(P.get("s")||2);S.d=+(P.get("d")||1);S.q=P.get("q")||"";S.gp=P.get("gp")||"";
S.v=P.get("v")||"";
q.value=S.q;fgp.value=S.gp;fv.value=S.v;
function esc(s){{return String(s).replace(/[&<>"]/g,function(c){{
 return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[c];}});}}
function nrm(s){{return String(s).normalize("NFD")
 .replace(/[\\u0300-\\u036f]/g,"").toLowerCase();}}
function draw(){{
 var q1=nrm(S.q);
 var rs=D.filter(function(x){{
  return (!q1||nrm(x[0]+" "+x[3]+" "+x[2]).indexOf(q1)>=0)
   &&(!S.gp||x[2]===S.gp)&&(!S.v||x[4]===S.v);}});
 rs.sort(function(a,b){{
  var k=S.c===4?5:S.c,r;
  r=k===5?(a[5]-b[5])*S.d:String(a[k]).localeCompare(String(b[k]),"es")*S.d;
  return r||String(a[0]).localeCompare(String(b[0]),"es");}});
 var h="",g=null;
 rs.forEach(function(x){{
  if((S.c===2||S.c===4)&&x[S.c]!==g){{g=x[S.c];
   h+='<tr class="grp"><td colspan="4">'+esc(g)+'</td></tr>';}}
  h+='<tr><td><span class="vote '+(VC[x[4]]||"wait")+'"><span class="dotmark"></span>'
   +esc(x[4])+"</span>"+(x[6]?' <span class="chip wait">'+esc(x[6])+"</span>":"")
   +"</td><td>"+pchip(x)
   +'</td><td class="who2">'+blogo(x[2])+"</td><td>"+esc(x[3])+"</td></tr>";}});
 tb.innerHTML=h;
 cnt.textContent=rs.length+" de "+D.length+" parlamentarios mostrados.";
 var p=new URLSearchParams();
 if(S.q)p.set("q",S.q);if(S.gp)p.set("gp",S.gp);if(S.v)p.set("v",S.v);
 p.set("s",S.c);p.set("d",S.d);
 try{{history.replaceState(null,"","#"+p);}}catch(e){{}}
}}
document.querySelectorAll("th button").forEach(function(b){{
 b.onclick=function(){{var c=+b.dataset.c;S.d=(S.c===c?-S.d:1);S.c=c;draw();}};}});
q.oninput=function(){{S.q=q.value;draw();}};
fgp.onchange=function(){{S.gp=fgp.value;draw();}};
fv.onchange=function(){{S.v=fv.value;draw();}};
if(S.q||S.gp||S.v||S.c!==2||S.d!==1)draw();
</script></section>"""

    bill = v["bill"]
    billlink = ""
    if bill:
        billlink = (
            f'<p>Corresponde al proyecto <a href="{bill_url(r, bill)}">'
            f'{esc(bill["code"])}</a>: {esc(clip(bill["title"], 160))}.'
            + ("" if v["bill_id"] else
               ' <span class="sm mut" title="El acta no trae un campo que '
               'identifique el proyecto.">Vínculo deducido del número citado en '
               'el asunto.</span>') + '</p>')
    # A vote is one step of a bill's passage. When it resolves to a bill, the
    # bill's own tracker answers "and now what"; when it does not, the honest
    # next step is the acta that supersedes this one.
    nxt = ""
    if bill:
        st, sent, steps = status_info(bill["status"])
        nxt = (f'<section><h2>Qué pasa después</h2><p>Tras esta votación, '
               f'<a href="{bill_url(r, bill)}">{esc(bill["code"])}</a> figura como '
               f'<b>{esc(bill["status"] or "sin estado")}</b>. {esc(sent)}</p>'
               + ("<ol>" + "".join(f"<li>{esc(s)}</li>" for s in steps) + "</ol>"
                  if steps else "") + "</section>")
    elif _k == "confirmado":
        nxt = ('<section><h2>Qué pasa después</h2><p>Nada: el Diario de los '
               'Debates ya confirmó este conteo.</p></section>')
    elif resolved:
        nxt = ('<section><h2>Qué pasa después</h2><p>La corrección leída en sala '
               'ya está aplicada aquí. Falta el acta definitiva de la Oficialía '
               'Mayor; cuando salga, compararemos las dos.</p></section>')
    elif v["provisional"] or (rows and not v["parsed"]):
        nxt = ('<section><h2>Qué pasa después</h2><p>El acta que usamos es '
               'provisional. Cuando la Oficialía Mayor publique la definitiva, con '
               'los votos orales, esta página mostrará ambas cifras.</p>'
               '</section>')
    legal = LEGAL.get(ch, 130)
    need = emitted // 2 + 1
    ok = yes >= need and yes > no
    outcome = (v["result"] or
               ("Aprobada por mayoría simple" if ok else "No alcanzó la mayoría simple"))
    derived = "" if v["result"] else (
        '<p class="sm mut">El acta no imprime veredicto. El resultado de arriba '
        'lo deducimos del conteo.</p>')
    thr = (f'<section><h2>Qué se necesitaba para aprobar</h2>'
           f'<p>Se aprueba por <b>mayoría simple</b>. Con {emitted} votos emitidos el '
           f'umbral estaba en <b>{need}</b> y hubo <b>{yes}</b>. '
           f'{"Se superó." if ok else "No se superó."}</p>'
           f'{derived}'
           f'<p class="sm mut">La mayoría del número legal, que el Reglamento exige '
           f'para otras materias, serían {legal // 2 + 1} de {legal}. Cuál regla '
           f'invocó la Mesa no consta en el acta.</p></section>')

    # The summary bar. It was the one bar on the site written by hand, and it
    # showed: four segments, no rule between them and not a single title, right
    # above the hemicycle. «En contra» and «abstención» are 3.6 ΔE apart for a
    # deuteranope, so with no rule they read as one segment of double the size.
    # It goes through the same two helpers as every other bar and legend now.
    bar = ""
    if total:
        segs = [(k, n) for k, n in
                (("SI", yes), ("NO", no), ("ABST", ab), ("BLANCO", blank),
                 ("LICENCIA", lic), ("AUSENTE", aus), ("PRESIDENCIA", chair),
                 ("OTRO", other - chair)) if n]
        parts = [(pos_col(k), pos(k)[0], n) for k, n in segs]
        keys = [(pos_col(k), esc(pos(k)[0]), f"{n} · {pc(n, total)}")
                for k, n in segs]
        shapes = [pos_shape(k) for k, _n in segs]
        if unrec:
            parts.append(("repeating-linear-gradient(45deg,var(--dead) 0 4px,"
                          "transparent 4px 8px)", "Sin reconciliar", unrec))
            keys.append(("var(--dead)", "Sin reconciliar",
                         f"{unrec} · {pc(unrec, total)}"))
            shapes.append("hatch")
        bar = stack_bar(parts, total, height=22) + key_list(keys, shapes)

    # Sources disagree often. Rather than picking one and hiding the rest, the
    # page prints all of them side by side and says which one it is using.
    warn = ""
    if disputed or v["provisional"] or (rows and not v["parsed"]):
        trows = "".join(
            f'<tr><td>{esc(lab)}{", <b>la que usamos arriba</b>" if k == kind else ""}'
            + (f' <a href="{esc(u)}">documento ↗</a>' if u else "") + "</td>"
            + "".join(f'<td class="num">{n}</td>' for n in t)
            + f'<td class="num">{sum(t)}</td></tr>'
            for k, lab, t, u in tal)
        lead = (
            '<b>El Diario de los Debates confirma el conteo del tablero.</b> '
            'Nada cambió entre el acta electrónica y la versión definitiva:'
            if _k == "confirmado" else
            '<b>El resultado se corrigió en la propia sesión.</b> El tablero no '
            'recoge los votos de viva voz. La presidencia los sumó y rectificó el '
            'conteo en el acto, y esa es la cifra que usamos:'
            if resolved else
            '<b>El registro se contradice consigo mismo.</b> Las fuentes no dan el '
            'mismo número. Usamos arriba la de más autoridad y dejamos el resto a '
            'la vista, para que discuta con el documento y no con nosotros:'
            if disputed else
            '<b>La cámara publicó esta acta como provisional.</b> Le falta lo que '
            'se votó de viva voz, así que la cifra puede moverse.'
            + (' Por ahora las fuentes que tenemos coinciden:' if len(tal) > 1
               else ''))
        warn = (
            f'<div class="note" id="lectura">{lead}'
            + (f'<div class="scroll" style="margin:12px 0"><table><thead><tr>'
               f'<th>Fuente</th><th>A favor</th><th>En contra</th><th>Abstención</th>'
               f'<th>Suma</th></tr></thead><tbody>{trows}</tbody></table></div>'
               if tal else "")
            + (f'<p>{esc(v["parse_note"])}</p>' if v["parse_note"] else "")
            + (f'<p>{unrec} de los {total} de la lista no quedan explicados por la '
               f'cifra de arriba. Van como «sin reconciliar» en la barra en vez de '
               f'desaparecer del total.</p>'
               if unrec else "")
            + ('' if not rows else
               '<p>El desglose y la lista nominal de abajo llevan la cifra '
               'corregida, con los votos orales marcados fila por fila.</p>'
               if roster_ok else
               '<p>El desglose y la lista nominal de abajo salen de nuestra lectura '
               'del PDF, no del encabezado. Contrástelos con el original antes de '
               'citarlos.</p>') + '</div>')

    # Peer baselines: this margin and this turnout against every other roll
    # call published by the same chamber.
    st = d["vstats"].get(ch, {"margin": [], "presence": []})
    peers = ""
    if len(st["margin"]) > 1:
        marg = round(100 * yes / emitted) if emitted else 0
        rank = sorted(st["margin"], reverse=True).index(
            min(st["margin"], key=lambda m: abs(m - marg))) + 1
        pres_pct = round(100 * emitted / total) if total else 0
        peers = (f'<p class="sm mut">Sobre las {len(st["margin"])} votaciones '
                 f'nominales de {esc(CHAMBER[ch])}: {marg}% a favor es el '
                 f'{rank}.º margen más amplio (mediana {median(st["margin"])}%), y '
                 f'votó el {pres_pct}% de la lista (mediana '
                 f'{median(st["presence"])}%).</p>')

    # A show-of-hands vote has a result and no numbers at all. Printing
    # "0 a favor" would be a lie; the honest page says the count does not exist.
    no_count = not rows and v["n_yes"] is None
    if no_count:
        lede_tail = ('. Sin conteo nominal: la votación no pasó por el sistema '
                     'electrónico.')
        lede_src = ("Así consta en el acta de la sesión."
                    if not v["parse_note"] else esc(v["parse_note"]))
        result_block = (
            '<section><h2>Resultado</h2><p>El acta consigna el acuerdo y ni un solo '
            'número. Esta página no puede decirle cómo votó su parlamentario, '
            'porque el Congreso tampoco lo publicó.</p>'
            + (f'<p class="sm mut">{esc(v["parse_note"])}</p>'
               if v["parse_note"] else "") + hemi + f'{pres}</section>')
    else:
        lede_tail = (f': {yes} a favor, {no} en contra, {ab} abstenciones'
                     + (f' y {novote} sin votar' if novote else "")
                     + f', sobre los {legal} escaños {esc(DE[ch])}.')
        lede_src = f"Cifra tomada de: {esc(kind_label.lower())}."
        # ponytail: el arco, cuatro azulejos y la barra decían la misma cifra
        # tres veces. Queda el arco con la leyenda de la barra pegada debajo; el
        # reparto sobre los votos emitidos ya lo hace «Qué se necesitaba».
        result_block = f"""<section><h2>Resultado</h2>{hemi}
<div style="margin-top:22px">{bar}</div>
{peers}
{pres}</section>
{thr}"""

    body = f"""
<div class="crumb"><a href="{r}index.html">Inicio</a> ›
<a href="{r}votaciones.html">Votaciones</a> ›
<a href="{r}votaciones.html#q={esc(v["held_on"] or "")}">{esc(CHAMBER_SHORT[ch])}
{fecha(v["held_on"])}</a></div>
<span class="eyebrow">{esc(CHAMBER[ch])} · votación nominal</span>
<h1{" class=\"long\"" if len(v["subject"] or "") > 62 else ""}>{esc(v["subject"] or "Votación sin asunto registrado")}</h1>
<p class="lede">{fecha(v["held_on"])}. <b>{esc(outcome)}</b>{lede_tail} {flag}<br>
<span class="sm mut">{lede_src}</span></p>
{billlink}
{result_block}
{warn}
{nxt}
{"<section><h2>Por grupo parlamentario</h2>" + bchart + ptable + "</section>" if ptable else ""}
{out_html}
{roster}
<section><h2>Descarga y cita</h2>
{f'<p><a href="{esc(v["slug"])}.csv" title="Identificador estable, nombre, grupo, circunscripción, sentido del voto y estado de fiabilidad de la lectura.">Descargar esta votación en CSV</a>, una fila por parlamentario.</p>' if rows else '<p>No hay CSV: sin votación nominal no hay filas que exportar.</p>'}
<p class="sm mut">Cita sugerida: «Votación nominal {esc(CHAMBER[ch])},
{fecha(v["held_on"])}: {esc(clip(v["subject"], 80))}», consultada el
{fecha(dt.date.today().isoformat())}.</p></section>
{prov([
    f'Acta publicada por {esc(CHAMBER[ch])}: '
    f'<a href="{esc(v["source_url"])}">documento original ↗</a>'
    f' ({esc(v["source_kind"] or "documento")}).',
    (f'Resultado corregido: <a href="{esc(v["final_source_url"])}">documento '
     f'definitivo ↗</a>.') if v["final_source_url"] else "",
    f'Descargado el {fecha(v["fetched_at"])}.' if v["fetched_at"] else "",
    esc(v["parse_note"] or ""),
    'La versión definitiva del acta se publica después; esta copia se regenera '
    'en cada corrida de la ingesta.',
    f'Identificador interno: <code>{esc(v["id"])}</code>.',
])}
"""
    return shell(short_title(CHAMBER_SHORT[ch] + " " + (fecha(v["held_on"]) or ""),
                             v["subject"] or v["id"], 58) + " · votación",
                 body, depth=1,
                 desc=f'Votación nominal en {CHAMBER[ch]}, {fecha(v["held_on"])}.')


def _js(s):
    return '"' + str(s or "").replace("\\", "\\\\").replace('"', '\\"') \
        .replace("<", "\\u003c").replace("\n", " ") + '"'


def vote_csv(d, v):
    """One row per legislator, with a stable id to join on and the reliability
    of the reading carried in the data. A download that drops the dispute the
    page shows is a worse lie than not publishing it."""
    rows = d["vrows"].get(v["id"], [])
    tal, _ = tallies(v, rows)
    disputed = len({t[2] for t in tal}) > 1
    state = d["vstate"][v["id"]][0]
    note = " | ".join(f"{lab}: {t[0]}-{t[1]}-{t[2]}" for _, lab, t, _ in tal)
    if v["parse_note"]:
        note += " | " + " ".join(v["parse_note"].split())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["votacion_id", "fecha", "camara", "asunto", "resultado_derivado",
                "estado_lectura", "nota_lectura", "fuente_url",
                "legislator_id", "slug", "parlamentario", "grupo_parlamentario",
                "circunscripcion", "voto", "fuente_fila"])
    kind, _lab, (yes, no, ab), _u = tal[0] if tal else ("", "", (0, 0, 0), None)
    res = v["result"] or ("Aprobada por mayoría simple" if yes > no
                          else "No alcanzó la mayoría simple")
    for x in rows:
        L = x["leg"]
        w.writerow([v["id"], v["held_on"], v["chamber"], v["subject"], res,
                    state, note, v["final_source_url"] or v["source_url"],
                    (L["id"] if L else x["legislator_id"]) or "",
                    L["slug"] if L else "",
                    L["full_name"] if L else x["name_raw"],
                    (L["party"] if L else None) or x["party_raw"] or "",
                    L["district"] if L else "", x["position"],
                    x["source"] or "tablero"])
    return buf.getvalue()


def leg_csv(d, L):
    """One legislator's whole record, one row per fact. Same shape for a bill,
    a motion, a vote, a committee seat and an attendance taking, because the
    question people bring to it, «what did this person do», does not care
    which table it came from."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["legislator_id", "slug", "parlamentario", "camara", "periodo",
                "grupo_parlamentario", "circunscripcion", "tipo", "fecha",
                "codigo", "titulo", "rol", "estado", "pagina"])
    def row(tipo, fecha_, codigo, titulo, rol, estado, page):
        w.writerow([L["id"], L["slug"], L["full_name"], CHAMBER[L["chamber"]],
                    f'{L["per_par"]}-{L["per_par"] + 5}', L["party"] or "",
                    L["district"] or "", tipo, fecha_ or "", codigo or "",
                    " ".join((titulo or "").split()), rol, estado or "", page])
    for bid, rank in sorted(d["leg_bills"].get(L["slug"], []),
                            key=lambda x: d["bill_by_id"][x[0]]["presented_on"] or "",
                            reverse=True):
        b = d["bill_by_id"][bid]
        row("proyecto", b["presented_on"], b["code"], b["title"],
            "autor principal" if (rank or 0) == 0 else "coautor", b["status"],
            bill_url("", b))
    by_mid = {m["id"]: m for m in d["motions"]}
    for mid, rank in d["leg_motions"].get(L["slug"], []):
        m = by_mid[mid]
        row("mocion", m["presented_on"], m["code"], m["summary"],
            "promotor" if (rank or 0) == 0 else "firmante", m["status"],
            f'mociones.html#m-{mid}')
    for v, p in d["leg_votes"].get(L["slug"], []):
        row("votacion", v["held_on"], v["id"], v["subject"], pos(p)[0],
            d["voutcome"][v["id"]], f'votacion/{v["slug"]}.html')
    for m in d["leg_cttes"].get(L["slug"], []):
        c = d["cttes"][m["committee_id"]]
        row("comision", "", c["slug"], c["name"],
            m["role"] + (" (cambio por oficio)" if m["amendment"] else ""),
            "", ctte_url("", c))
    for s, x in sorted(d["leg_att"].get(L["slug"], []),
                       key=lambda p: (p[0]["held_on"], p[0]["sort"]), reverse=True):
        lab, kind, _ = att_state(d, s, x)
        row("asistencia", s["held_on"], s["taken_at"],
            f'Toma de asistencia, {CHAMBER[s["chamber"]]}',
            "cuenta en el denominador" if kind in ("presente", "falta")
            else "excluida del denominador", lab,
            f'asistencia/{s["slug"]}.html')
    return buf.getvalue()


def bills_csv(d, bs):
    """The corpus behind a facet page, whole, not the 250 rows one page shows."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["bill_id", "codigo", "periodo", "camara", "numero", "titulo",
                "estado", "etapa", "presentado", "proponente", "comisiones",
                "firmas", "autor_principal", "pagina"])
    for b in bs:
        ss = d["spon"].get(b["id"], [])
        prim = next((s for s in ss if (s["rank"] or 0) == 0), None)
        w.writerow([
            b["id"], b["code"], f'{b["per_par"]}-{b["per_par"] + 5}',
            CHAMBER[b["chamber"] or "C"], b["ply_num"],
            " ".join((b["title"] or "").split()), b["status"] or "",
            status_info(b["status"])[0], b["presented_on"] or "",
            b["proponent"] or "",
            " | ".join(c["name"] for c in d["bill_cttes"].get(b["id"], [])),
            len(ss), prim["name_raw"] if prim else "", bill_url("", b)])
    return buf.getvalue()


# --------------------------------------------------------------- asistencia

def att_bar(t):
    # Same silhouettes as a roll call, for the same reason: presiding and a
    # licencia are not absences, and the reader should be able to see that
    # without seeing the colours.
    segs = [("Presentes", t["presente"], "var(--si)", ("circle", 0)),
            ("Presidiendo", t["presidencia"], "var(--pres)", ("triangle", 1)),
            ("Con licencia", t["excusa"], "var(--lic)", ("square", 1)),
            ("Ausentes", t["falta"], "var(--no)", ("ring", 1))]
    tot = t["total"] or 1
    shown = [s for s in segs if s[1]]
    return (stack_bar([(c, lab, n) for lab, n, c, _sh in segs], tot, height=22)
            + key_list([(c, esc(lab), f"{n} · {round(100 * n / tot)}%")
                        for lab, n, c, _sh in shown],
                       shapes=[sh for _l, _n, _c, sh in shown]))


def att_series(d):
    """Attendance per taking, one small chart per chamber.

    Two chambers with different rosters are two measures, so they are two
    charts and never two scales on one. A 60-seat Senate and a 130-seat
    Diputados sharing an axis would make the Senate look empty.
    """
    out = []
    for ch in ("D", "S"):
        ss = sorted([s for s in d["sesiones"] if s["chamber"] == ch],
                    key=lambda s: (s["held_on"], s["sort"]))
        if len(ss) < 3:
            continue
        rows = [(f'{int(s["held_on"][8:])}/{int(s["held_on"][5:7])}',
                 f'{fecha(s["held_on"])}, {s["taken_at"]}',
                 s["tally"]["presente"]) for s in ss]
        med = median([r[2] for r in rows])
        out.append(
            f'<figure><h3 style="margin:0 0 10px"><span class="chip '
            f'{ch.lower()}">{esc(CHAMBER[ch])}</span></h3>'
            + col_chart(rows, "presentes", 160, f"att-{ch}", "var(--si)")
            + f'<figcaption>Presentes en cada una de las {len(ss)} tomas, sobre '
              f'{LEGAL[ch]} escaños. Mediana: {med}.</figcaption></figure>')
    return "".join(out) or ""


def att_gloss(d, s):
    info = {}
    for x in s["rows"]:
        lab, k, w = att_state(d, s, x)
        tone = {"presente": "ok", "falta": "falta",
                "presidencia": "s"}.get(k, "wait")
        info[lab] = (ATT_LETTER.get(k, "·"), tone, w)
    return ('<details class="gloss" open><summary>Qué significa cada letra de '
            'la lista</summary><dl class="kv">' + "".join(
                f'<div><dt><span class="chip {t}">{m}</span> {esc(lab)}</dt>'
                f'<dd>{esc(w)}</dd></div>'
                for lab, (m, t, w) in sorted(info.items(),
                                             key=lambda i: i[1][0]))
            + "</dl></details>")


def render_att(d, s):
    """One taking, in full. The absences are the point, so they are named and
    linked rather than left to a percentage."""
    r = "../"
    ch = s["chamber"]
    t = s["tally"]
    prov_pdf = "PROVISIONAL" in (s["source_url"] or "").upper()
    rows, faltaron = [], []
    for x in sorted(s["rows"], key=lambda x: ((x["party_raw"] or ""), x["name_raw"])):
        lab, kind, _why = att_state(d, s, x)
        tone = {"presente": "ok", "falta": "falta", "presidencia": "s"}.get(kind, "wait")
        L = x["leg"]
        who = (f'<a href="{leg_url(r, L["slug"])}">{esc(nice_name(L["full_name"]))}</a>'
               if L else esc(nice_name(x["name_raw"])))
        rows.append(f'<tr><td data-s="{ATT_LETTER.get(kind, "·")}">'
                    f'{att_mark(lab, kind, tone)}</td>'
                    f'<td>{who}</td><td>{esc(x["party_raw"] or "—")}</td></tr>')
        if kind == "falta":
            faltaron.append(who)
    rate_txt, usable = pct_or_note(t["presente"], t["base"], "asistentes")
    return shell(
        f'Asistencia · {CHAMBER_SHORT[ch]} {fecha(s["held_on"])} {s["taken_at"]}',
        f"""<div class="crumb"><a href="{r}index.html">Inicio</a> ›
<a href="{r}asistencia.html">Asistencia</a> › {esc(CHAMBER[ch])}</div>
<span class="eyebrow">{esc(CHAMBER[ch])} · toma de asistencia</span>
<h1>Asistencia del {fecha(s["held_on"])}, {esc(s["taken_at"])}</h1>
<p class="lede"><b>{t["asistieron"]}</b> presentes de los {t["base"]} a quienes
les correspondía estar ({esc(rate_txt) if usable else "—"}). El quórum legal
{esc(DE[ch])} es {LEGAL.get(ch, 130) // 2 + 1}, la mitad más uno de
{LEGAL.get(ch, 130)}. Licencia y presidencia se cuentan aparte, nunca como
inasistencia.</p>
{'<div class="note">La cámara sella este documento como PROVISIONAL: si publica una versión definitiva, esta página cambiará con ella.</div>' if prov_pdf else ""}
<section><h2>Resultado de la toma</h2>{att_bar(t)}</section>
{f'<section><h2>Faltaron ({len(faltaron)})</h2><ul class="roll">' + "".join(f"<li>{w}</li>" for w in faltaron) + '</ul><p class="sm mut">Sin licencia en esta toma. Faltar a una toma no es faltar a la sesión. La lista se pasa varias veces y se puede entrar después.</p></section>' if faltaron else '<section><h2>Faltaron</h2><p>Nadie sin licencia.</p></section>'}
<section><h2>Lista completa ({t["total"]})</h2>
{att_gloss(d, s)}
<div class="filters"><input id="q" type="search"
 placeholder="Busca a tu parlamentario o su bancada" aria-label="Filtrar la lista"></div>
<div class="scroll"><table class="srt"><thead><tr><th>Estado</th><th>Parlamentario</th>
<th>Grupo parlamentario</th></tr></thead><tbody id="ls">{"".join(rows)}</tbody>
</table></div><p class="sm mut" id="cnt">{t["total"]} nombres, en el orden
oficial.</p>
{FILTER_JS}</section>
{prov([
    f'Lista de asistencia publicada por {esc(CHAMBER[ch])}: '
    f'<a href="{esc(s["source_url"])}">documento original ↗</a>.',
    'El Congreso no publica la asistencia como dato: cada toma, con su hora, se '
    'lee del PDF de la sesión.',
    'Documento marcado PROVISIONAL por la propia cámara.' if prov_pdf else "",
    f'Regenerado el {fecha(dt.date.today().isoformat())}.',
])}""", depth=1,
        desc=f'Asistencia de {CHAMBER[ch]} el {fecha(s["held_on"])}: '
             f'{t["asistieron"]} presentes, {t["falta"]} ausentes.')


def render_att_index(d, today):
    """The session-by-session view, plus the two rankings the data supports."""
    trows = []
    for s in d["sesiones"]:
        t = s["tally"]
        p, usable = pct_or_note(t["presente"], t["base"], "asistentes")
        trows.append(
            f'<tr><td class="nw" data-s="{esc(s["held_on"])} {esc(hour24(s["taken_at"]))}">'
            f'<a href="asistencia/{esc(s["slug"])}.html">'
            f'{fecha(s["held_on"])}</a></td><td>{esc(s["taken_at"])}</td>'
            f'<td><span class="chip {s["chamber"].lower()}">'
            f'{esc(CHAMBER_SHORT[s["chamber"]])}</span></td>'
            f'<td class="num">{t["presente"] + t["presidencia"]}</td>'
            f'<td class="num">{t["falta"]}</td>'
            f'<td class="num">{t["excusa"]}</td>'
            f'<td class="num">{esc(p) if usable else "—"}</td></tr>')
    # Who missed most. Only over members with a denominator worth dividing by,
    # and the count leads, not the rate.
    rank = []
    for L in d["legs"]:
        ok, den, lic, pre = leg_att_rate(d, L)
        if den:
            rank.append((den - ok, den, lic, pre, L))
    rank.sort(key=lambda x: (-x[0], x[4]["full_name"]))
    top = [x for x in rank if x[0]][:15]
    rrows = "".join(
        f'<tr><td><a href="parlamentario/{esc(L["slug"])}.html">'
        f'{esc(nice_name(L["full_name"]))}</a></td>'
        f'<td>{esc(CHAMBER_SHORT[L["chamber"]])}</td>'
        f'<td>{esc(L["party"] or "—")}</td>'
        f'<td class="num">{f}</td><td class="num">{den}</td>'
        f'<td class="num">{lic}</td></tr>'
        for f, den, lic, pre, L in top)
    per_ch = []
    for ch in ("D", "S"):
        ss = [s for s in d["sesiones"] if s["chamber"] == ch]
        if not ss:
            continue
        rates = [s["tally"]["pct"] for s in ss if s["tally"]["pct"] is not None]
        # The median OF THE TAKINGS, not of the members: half of these {n}
        # takings had this share of the room or better. The ficha of each
        # parlamentario carries the other one, over people, and says so.
        per_ch.append(
            f'<div class="tile"><dt>{esc(CHAMBER_SHORT[ch])}</dt>'
            f'<dd>{len(ss)}<small>tomas de asistencia · mediana de las '
            f'{len(rates)} tomas: {median(rates)}% de quienes debían estar'
            f'</small></dd></div>')
    total_rows = sum(s["tally"]["total"] for s in d["sesiones"])
    srcs = sorted({s["source_url"] for s in d["sesiones"] if s["source_url"]})
    body = f"""<span class="eyebrow">Asistencia al Pleno</span>
<h1>{len(d["sesiones"])} tomas de asistencia</h1>
<p class="lede">Quién estuvo en el hemiciclo y quién no, sesión por sesión.
{num(total_rows)} registros leídos de los PDF de las cámaras. La unidad es la
toma, no el día. Cada sesión pasa lista varias veces.</p>
<div class="note"><b>Una licencia no es una inasistencia.</b> Es un permiso de la
Mesa Directiva. Y <b>quien preside no está faltando</b>, aunque la lista no le
ponga marca. Ninguna de las dos entra en el denominador de esta página.</div>
<section><h2>Resumen por cámara</h2><dl class="tiles">{"".join(per_ch)}</dl></section>
<section><h2>Cuánta gente hubo en cada toma</h2>{att_series(d)}</section>
<section><h2>Sesión por sesión</h2>
<div class="scroll"><table class="srt"><thead><tr><th>Sesión</th><th>Hora</th><th>Cámara</th>
<th>Presentes</th><th>Ausentes</th><th>Licencias</th>
<th>% de quienes debían estar</th>
</tr></thead><tbody>{"".join(trows)}</tbody></table></div></section>
{f'''<section><h2>Quiénes acumulan más inasistencias</h2>
<div class="scroll"><table class="srt"><thead><tr><th>Parlamentario</th><th>Cámara</th>
<th>Grupo</th><th>Inasistencias</th><th>Tomas que le correspondían</th>
<th>Licencias</th></tr></thead><tbody>{rrows}</tbody></table></div>
<p class="sm mut">Conteos, no porcentajes. Sobre {len(d["sesiones"])} tomas un
porcentaje exagera cualquier diferencia. Las licencias van al lado para que no se
confundan con faltas.</p></section>''' if top else ""}
{prov([
    'Listas de asistencia en PDF de cada cámara: no existen como dato en ningún '
    'portal.',
    'Documentos: ' + " · ".join(f'<a href="{esc(u)}">{esc(pathlib.PurePath(u).name)} ↗</a>'
                                for u in srcs),
    'Varias llevan el sello PROVISIONAL de la propia cámara; la página de cada '
    'toma lo dice.',
    f'Regenerado el {fecha(today)}.',
])}"""
    return shell("Asistencia al Pleno", body, 0,
                 desc="Asistencia de diputados y senadores al Pleno, sesión por "
                      "sesión, con licencias contadas aparte.")


# --------------------------------------------------------------------- gasto

PTE = ("https://www.transparencia.gob.pe/personal/pte_transparencia_personal.aspx"
       "?id_entidad=16")
SEMANA = "https://www3.congreso.gob.pe/semana-representacion/"


def gasto_row(d, L, tot, g, rank, top, r=""):
    """Una fila del ranking: cara, bancada, barra partida en planilla y viajes.

    Las dos partes en la misma barra porque son de naturaleza distinta -- una es
    el sueldo de cuatro o cinco personas, la otra son pasajes -- y sumarlas en un
    número solo escondería que el segundo despacho más caro lo es por viajar."""
    pl, vi = g["planilla"], g["viajes"]
    return (f'<li><a href="{leg_url(r, L["slug"])}">'
            f'<span class="rk">{rank}</span>'
            f'<span class="lbl">{face_mini(L, r)}'
            f'{esc(nice_name(L["full_name"]))}'
            f'<small>{soles(pl)} en planilla · {soles(vi)} en '
            f'{num(g["nviajes"])} viajes</small></span>'
            f'<span class="track">'
            f'<i style="width:{100 * pl / top:.2f}%;background:var(--accent)"></i>'
            f'<i style="width:{100 * vi / top:.2f}%;background:var(--vio)"></i>'
            f'</span><span class="n">{soles(tot)}</span></a></li>')


def gasto_block(d, L, r="../"):
    """Lo que costó su despacho, en la ficha.

    Su puesto sale del mismo `gasto_rank()` que la página de gasto, así que las
    dos no pueden discrepar. Sin dato no se escribe nada: un despacho del que el
    portal no publicó filas no es un despacho de cero soles."""
    g = gasto_year(d, L)
    if not g:
        return ""
    rank = gasto_rank(d)
    tot = g["planilla"] + g["viajes"]
    pos_ = next(i + 1 for i, (_t, P, _g) in enumerate(rank) if P["id"] == L["id"])
    med = median([t for t, _P, _g in rank])
    return f"""<section><h2>Lo que costó su despacho en {GASTO_Y}</h2>
<dl class="tiles">
<div class="tile"><dt>Total</dt><dd>{soles(tot)}
<small>puesto {pos_} de {len(rank)}</small></dd></div>
<div class="tile"><dt>Planilla</dt><dd>{soles(g["planilla"])}
<small>sueldos de su equipo</small></dd></div>
<div class="tile"><dt>Viajes</dt><dd>{soles(g["viajes"])}
<small>{num(g["nviajes"])} autorizaciones</small></dd></div>
</dl>
<p class="sm mut">La mediana de los {len(rank)} despachos fue {soles(med)}. No es
lo que cobró: es lo que costó el despacho que se le asignó.
<a href="{r}gasto.html">Cómo se calcula</a>.</p></section>"""


def render_gasto(d, today):
    rank = gasto_rank(d)
    if not rank:
        return None
    tot = [t for t, _L, _g in rank]
    top, med = tot[0], median(tot)
    pl = sum(g["planilla"] for _t, _L, g in rank)
    vi = sum(g["viajes"] for _t, _L, g in rank)
    # Agrupado en tramos de cincuenta mil. 130 filas ordenadas de mayor a menor
    # y sin un solo corte son una lista en la que sólo se leen las tres primeras;
    # el tramo dice de un vistazo cuántos despachos hay en cada altura.
    band = lambda t: int(t // 50_000) * 50_000  # noqa: E731
    counts = {}
    for t, _L, _g in rank:
        counts[band(t)] = counts.get(band(t), 0) + 1
    rows, cur_band = [], object()
    for i, (t, L, g) in enumerate(rank):
        b = band(t)
        if b != cur_band:
            cur_band = b
            rows.append(f'<li class="gh">De {soles(b)} a {soles(b + 50_000)}'
                        f'<b>{counts[b]}</b></li>')
        rows.append(gasto_row(d, L, t, g, i + 1, top))
    rows = "".join(rows)

    # La serie mensual de viajes. Enero a abril de 2026 es un escalón, y no es
    # que viajaran menos: el Acuerdo 083-2025-2026/MESA-CR suspendió veintiún
    # acuerdos de facilidades del 1 de enero al 12 de abril. Sin decirlo, el
    # hueco se lee como austeridad.
    have = sorted(d["gasto_mes"])
    serie = ""
    if len(have) > 6:
        # Rellenando los meses vacíos. De enero a marzo de 2026 no hay ni una
        # fila, y una serie que sólo dibuja los meses que existen los saltaba
        # sin dejar hueco: la suspensión desaparecía en vez de verse.
        (y0, m0), (y1, m1) = have[0], have[-1]
        mm = [(y0 + (m0 - 1 + i) // 12, (m0 - 1 + i) % 12 + 1)
              for i in range((y1 - y0) * 12 + m1 - m0 + 1)]
        pts = [(f'{MES[m - 1][:3]} {y % 100:02d}' if m in (1, 7) else "",
                f'{MES[m - 1]} de {y}', round(d["gasto_mes"].get((y, m), 0)))
               for y, m in mm]
        serie = f"""<section><h2>Los viajes, mes a mes</h2>
<figure>{col_chart(pts, "soles en viajes", 170, "gm", "var(--vio)")}
<figcaption>Las tres columnas vacías de 2026 no son austeridad: el Acuerdo
083-2025-2026/MESA-CR suspendió veintiún acuerdos de facilidades parlamentarias
del 1 de enero al 12 de abril, en campaña electoral. Ni un viaje con cargo a un
despacho en esos tres meses.</figcaption></figure></section>"""

    ws = d["rep_weeks"]
    susp = sum(1 for w in ws if not w["held"])
    semana = ""
    if ws:
        last = [w for w in ws if w["starts_on"]][:12]
        semana = f"""<section><h2>Semanas de representación</h2>
<p>Una semana al mes el Pleno no sesiona y cada parlamentario viaja a su región.
Es el denominador de los viajes: {len(ws)} convocadas desde 2009, {susp} de ellas
suspendidas.</p>
<ul class="feed">{"".join(
    f'<li class="r {"st-dead" if not w["held"] else "st-pleno"}">'
    f'<span class="stg">{"suspendida" if not w["held"] else "N.º " + str(w["n"])}</span>'
    f'<span class="m">{esc(w["period"])}</span>'
    f'<span class="t" style="font-weight:400">{esc(w["raw"])}</span>'
    + (f'<span class="sm mut">{esc(w["note"])}</span>' if w["note"] else "")
    + "</li>" for w in last)}</ul>
<p class="sm mut">Las doce últimas. <a href="{SEMANA}">El calendario completo ↗</a>.</p>
</section>"""

    body = f"""<span class="eyebrow">Gasto</span>
<h1>Lo que cuesta un despacho</h1>
<p class="lede">No lo que cobra un parlamentario: lo que cuesta el despacho que
se le asigna. Sueldo por sueldo de quienes trabajan en él y viaje por viaje con
cargo a él. En {GASTO_Y}, la mediana fue <b>{soles(med)}</b>.</p>

<dl class="tiles">
<div class="tile"><dt>Despachos con dato</dt><dd>{len(rank)}
<small>en {GASTO_Y}</small></dd></div>
<div class="tile"><dt>Mediana</dt><dd>{soles(med)}
<small>de {soles(tot[-1])} a {soles(top)}</small></dd></div>
<div class="tile"><dt>En planilla</dt><dd>{soles_m(pl)}
<small>{pctxt(round(pl), round(pl + vi))} del total</small></dd></div>
<div class="tile"><dt>En viajes</dt><dd>{soles_m(vi)}
<small>{num(sum(g["nviajes"] for _t, _L, g in rank))} autorizaciones</small></dd></div>
</dl>

<div class="note">Son los despachos del Congreso 2021-2026. Del actual todavía no
hay nada: el portal publica cada mes a mediados del siguiente, y julio de 2026
es aún el Congreso saliente.</div>

<section><h2>Los {len(rank)} despachos de {GASTO_Y}</h2>
<div class="filters"><input id="q" type="search"
 placeholder="Filtrar por nombre" aria-label="Filtrar despachos"></div>
<ul class="ranked gasto" id="ls">{rows}</ul>
<p class="sm mut" id="cnt">Barra en dos tramos: planilla y viajes.</p>
{FILTER_JS}</section>
{serie}
{semana}

<section><h2>Lo que no se publica</h2>
<p>La asignación por gastos operativos —el dinero que el parlamentario recibe
además del sueldo— no se publica por persona. Tampoco su monto: el último
acuerdo que lo fija, el 118-2023-2024/MESA-CR, dice que se actualiza «tomando
como factor de actualización el Índice de Precios al Consumidor» y no imprime
ninguna cifra.</p>
<p>Hasta el 27 de julio de 2026 al menos había que rendirlo: el artículo 22 f)
del Reglamento anterior exigía una liquidación mensual a Tesorería con un 30 %
mínimo sustentado en comprobantes. Los tres reglamentos de la bicameralidad que
entraron en vigor ese día —004, 005 y 006-2025-2026-CR— no contienen la
expresión «gastos operativos» en ninguna parte y no exigen rendición alguna.</p>
<p>Por eso esta página cuenta despachos y no bolsillos, y por eso el resto que
el portal no atribuye a nadie —comisiones, Parlamento Andino, áreas
administrativas— se queda fuera en lugar de repartirse entre 130 para redondear
la cifra.</p></section>

{prov([
    f'Planilla y viajes: Portal de Transparencia Estándar, entidad 16, '
    f'<a href="{PTE}">Congreso de la República ↗</a>. El Congreso no lo publica '
    f'en su propio portal.',
    f'Cada despacho se identifica por el nombre del parlamentario en el campo '
    f'de dependencia, cruzado contra el padrón.',
    f'<a href="{SEMANA}">Calendario de semanas de representación ↗</a>.',
    f'Regenerado el {fecha(today)}.'])}"""
    return shell("Gasto", body, 0,
                 desc=f"Lo que costó cada despacho parlamentario en {GASTO_Y}: "
                      f"planilla y viajes, del portal de transparencia.")


# ---------------------------------------------------------------- comisiones

# Neither cámara publishes who sits on a committee: the portals list names and
# nothing else. The rosters come from the Diario de los Debates of the session
# where the Senate approved its cuadros, which we parse; `source_url` is our
# local mirror of that PDF, so the public copy is rebuilt from its filename.
DIARIO = "https://senado.congreso.gob.pe/wp-content/uploads/2026/08/"
# Quién preside sí está publicado, y en otro sitio que el cuadro de miembros.
MESAS = ("https://senado.congreso.gob.pe/mesas-directivas-y-horarios-de-"
         "sesiones-de-las-comisiones-parlamentarias/")
MESA_LABEL = {"presidencia": "Preside", "vicepresidencia": "Vicepreside",
              "secretaria": "Secretaría"}
MESA_ORDER = ("presidencia", "vicepresidencia", "secretaria")


def mesa_of(d, c):
    """-> [(cargo, miembro)] en orden de jerarquía, sólo los que existen."""
    by = {m["mesa"]: m for m in d["ctte_mem"].get(c["id"], []) if m.get("mesa")}
    return [(k, by[k]) for k in MESA_ORDER if k in by]


def mesa_row(d, c, r="", size=20):
    """La mesa directiva como tres personas con cara y logotipo de bancada.

    Es lo único que se sabe de una comisión antes de que dictamine nada, y es la
    respuesta a «quién manda aquí»: la presidencia decide si un proyecto se ve."""
    mesa = mesa_of(d, c)
    if not mesa:
        return ""
    li = []
    for cargo, m in mesa:
        bench = m["bench"] or (m["leg"] or {}).get("party")
        li.append(f'<li><span class="ro">{MESA_LABEL[cargo]}</span>'
                  f'{bench_logo(bench, r, size)}'
                  + (f'<a href="{leg_url(r, m["leg"]["slug"])}">'
                     f'{esc(nice_name(m["leg"]["full_name"]))}</a>' if m["leg"]
                     else f'<span>{esc(nice_name(m["name_raw"]))}</span>')
                  + "</li>")
    return f'<ul class="mesa">{"".join(li)}</ul>'


def diario_url(src):
    # ponytail: one diario so far, so one upload folder. A second one needs the
    # public URL stored at ingest time instead of reconstructed here.
    return DIARIO + pathlib.PurePath(src or "").name


def roster_src(ms):
    """El diario del que salió el cuadro.

    No es `ms[0]`: desde que las mesas directivas se ingieren de una página web,
    hay filas cuyo `source_url` es esa página, y ordenadas por apellido cualquiera
    de ellas puede caer la primera. `diario_url()` sobre una URL que acaba en «/»
    devolvía la carpeta de subidas del Senado y nada más."""
    return next((m["source_url"] for m in ms
                 if (m["source_url"] or "").lower().endswith(".pdf")), None)


def mem_li(r, m, tag=""):
    """One roster line: person (linked when they are in the padrón) + bancada.

    Con su cara: un cuadro de comisión son doce nombres seguidos y era la misma
    tabla ilegible que este trabajo existe para arreglar."""
    who = person_chip(m["leg"], r,
                      None if m["leg"] else nice_name(m["name_raw"]))
    bench = m["bench"] or (m["leg"] or {}).get("party")
    tag_html = f' <span class="rank">{esc(tag)}</span>' if tag else ""
    return f'<li>{who} {party_chip(bench, r)}{tag_html}</li>'


def alias_note(rows, c):
    """The oficios do not use the name the cuadro uses. Saying which title they
    wrote is cheaper than pretending the difference is not there."""
    names = sorted({m["alias_name"] for m in rows if m.get("alias_name")})
    if not names:
        return ""
    return ('<p class="sm mut">' + ("El oficio llama" if len(names) == 1
                                    else "Los oficios llaman")
            + " a esta comisión "
            + " y ".join(f"«{esc(n)}»" for n in names)
            + f'. Es el mismo órgano que el cuadro aprobado nombra '
              f'«{esc(c["name"])}»; lo tratamos como uno solo.</p>')


def roster(d, c, r="../"):
    """Composición of one committee, server-rendered.

    Amendment rows are shown apart and never counted: an oficio replaces a
    designation the bench already made, so folding it in would seat thirteen
    people in a committee of twelve.
    """
    ms = d["ctte_mem"].get(c["id"], [])
    if not ms:
        return ""
    tit = [m for m in ms if m["role"] == "titular" and not m["amendment"]]
    sup = [m for m in ms if m["role"] == "suplente" and not m["amendment"]]
    amd = [m for m in ms if m["amendment"]]
    src = diario_url(roster_src(ms))
    out = [f'<section><h2>Composición ({len(tit)} titulares'
           + (f", {len(sup)} suplentes" if sup else "") + ")</h2>"
           if tit or sup else
           f'<section><h2>Cambios de composición ({len(amd)})</h2>'
           f'<p>Bajo este nombre, el diario solo trae cambios de designación '
           f'presentados por oficio; el cuadro completo no aparece.</p>']
    if c["per_par"] != 2026:
        # ponytail: the ingester keyed this roster onto a committee of the same
        # name from the 2021-2026 Congress. Saying so is cheaper and more honest
        # than silently re-parenting rows here.
        out.append('<div class="note">Este cuadro es el del Senado 2026-2031; los '
                   'proyectos de abajo son del Congreso unicameral 2021-2026, que '
                   'tuvo una comisión homónima.</div>')
    if mesa_of(d, c):
        out.append('<h3>Mesa directiva</h3>' + mesa_row(d, c, r, 24)
                   + '<p class="sm mut" style="margin:8px 0 18px">La presidencia '
                     'fija la agenda: un proyecto se ve cuando ella lo pone.</p>')
    if tit:
        # Which benches hold this committee. A dictamen needs a majority of the
        # titulares, so the split is the whole story of what can come out of it.
        out.append('<figure style="margin:0 0 22px">' + bench_split(
            tit, lambda m: m["bench"] or (m["leg"] or {}).get("party"), r)
            + '<figcaption>Los titulares, por bancada: un dictamen sale con la '
              'mayoría de estas plazas.</figcaption></figure>')
        out.append(f'<h3>Titulares ({len(tit)})</h3><ul class="roll" id="titulares">'
                   + "".join(mem_li(r, m, MESA_LABEL.get(m.get("mesa"), ""))
                             for m in tit) + "</ul>")
    if sup:
        out.append(f'<h3 style="margin-top:18px">Suplentes ({len(sup)})</h3>'
                   f'<ul class="roll" id="suplentes">'
                   + "".join(mem_li(r, m, MESA_LABEL.get(m.get("mesa"), ""))
                             for m in sup) + "</ul>")
    if tit or sup:
        out.append('<p class="sm mut" style="margin-top:14px">El suplente ocupa la '
                   'plaza cuando el titular falta, y en esa sesión vota y firma el '
                   'dictamen.</p>')
    if amd:
        out.append(
            (f'<h3 style="margin-top:22px">Cambios posteriores ({len(amd)})</h3>'
             f'<p class="sm mut">Oficios leídos en sesión: estas {len(amd)} líneas '
             f'sustituyen designaciones del cuadro de arriba, no son plazas '
             f'adicionales.</p>' if tit or sup else
             '<p class="sm mut">Cada línea sustituye a quien la bancada había '
             'designado antes; no es una plaza adicional.</p>')
            + f'<ul class="roll" id="cambios">'
            + "".join(mem_li(r, m, tag=m["role"]) for m in amd)
            + "</ul>"
            + alias_note(amd, c)
            + f'<p><a class="doc" href="{esc(src)}">Oficio recogido en el '
              f'Diario de los Debates ↗</a></p>')
    out.append("</section>")
    return "".join(out)


def ctte_prov(d, c, today):
    """Provenance lines for a committee page: bills and roster have different
    sources, and the roster's is a PDF nobody publishes as data."""
    ms = d["ctte_mem"].get(c["id"], [])
    lines = [f'Comisiones asignadas a cada proyecto: expediente del proyecto en '
             f'<code>{API}/proyecto-ley</code>.']
    if any(m.get("mesa") for m in ms):
        lines.append(f'Mesa directiva: <a href="{MESAS}">cuadro de mesas '
                     f'directivas del Senado ↗</a>.')
    if roster_src(ms):
        src = diario_url(roster_src(ms))
        lines.append(
            f'Composición: <a href="{esc(src)}">Diario de los Debates del Senado, '
            f'sesión de aprobación de los cuadros ↗</a>. Ninguna cámara la publica '
            f'como dato, así que estos {len(ms)} nombres se leyeron de ese PDF.')
    if c["url"]:
        lines.append(f'<a href="{esc(c["url"])}">Página oficial de la comisión ↗</a>.')
    lines.append(f'Regenerado el {fecha(today)}.')
    return lines


# --------------------------------------------------------------- bancadas
#
# The bench is the unit people actually reason about, «¿qué hizo Fuerza
# Popular?», and it had no page at all. One per bench, plus an index, and every
# chip anywhere on the site points here.

def people_grid(legs, r="", per_bench=False):
    """Roster as portraits. Four words per row, not fourteen: photo, name,
    bench colour bar, region. The 140 members with no portrait get a lettered
    tile, never an empty <img>."""
    out = []
    for L in sorted(legs, key=lambda L: (bench_slot(L["party"]) or 99,
                                         L["full_name"])):
        # The initials sit behind the portrait, so a photo that has not loaded
        # yet, or that the chamber deletes from under us, leaves a lettered
        # tile rather than an empty grey rectangle.
        ini = esc(initials(L["full_name"]))
        img = photo_img(L, r, 400, 500, "(min-width:760px) 190px, 45vw")
        ph = (f'<span class="ph"><i aria-hidden="true">{ini}</i>{img}</span>'
              if img else f'<span class="noph">{ini}</span>')
        out.append(
            f'<li class="{gp(L["party"])}"><a href="{leg_url(r, L["slug"])}">'
            f'{ph}<span class="bar"></span><span class="cap">'
            f'<span class="nm2">{esc(surname_first(L["full_name"]))}</span>'
            f'<span class="sub">'
            + ("" if per_bench else bench_logo(L["party"], r, 22))
            + f'{esc(L["district"] or CHAMBER_SHORT[L["chamber"]])}</span>'
            f'</span></a></li>')
    return f'<ul class="people">{"".join(out)}</ul>'


def logo_credit_line(party):
    """The one line the licence needs, next to the mark it applies to. CC BY-SA
    4.0 wants the author and the licence wherever the work appears, not only on
    a credits page three clicks away."""
    c = LOGO_CREDITS.get(party, {})
    lic, autor = c.get("licencia", ""), c.get("autor", "")
    if "BY-SA" in lic.upper():
        return (f'por {esc(autor)}, <a href="{CC_BY_SA}">CC BY-SA 4.0</a>, '
                f'rasterizado desde el SVG de '
                f'<a href="{esc(c.get("pagina", ""))}">Wikimedia Commons</a>')
    if autor and norm(autor) != norm(party):
        return (f'de {esc(autor)}, dominio público, vía '
                f'<a href="{esc(c.get("pagina", ""))}">Wikimedia Commons</a>')
    # Commons credits five of these to the party itself, and «Logotipo de Fuerza
    # Popular de Fuerza Popular» is what naming the author then produces.
    return (f'dominio público, vía '
            f'<a href="{esc(c.get("pagina", ""))}">Wikimedia Commons</a>')


def render_bancada(d, party, today):
    r = "../"
    mem = [L for L in d["legs"] if L["party"] == party]
    dips = [L for L in mem if L["chamber"] == "D"]
    sens = [L for L in mem if L["chamber"] == "S"]
    # How this bench votes, over every roll call published so far.
    votes, unity = [], []
    for v in d["votes"]:
        rs = [x for x in d["vrows"].get(v["id"], []) if row_party(x) == party]
        if not rs:
            continue
        c = {}
        for x in rs:
            c[x["position"]] = c.get(x["position"], 0) + 1
        cast = sum(c.get(k, 0) for k in ("SI", "NO", "ABST"))
        top = max(("SI", "NO", "ABST"), key=lambda k: c.get(k, 0))
        votes.append((v, c, top, cast))
        if cast >= 3:
            unity.append(round(100 * c.get(top, 0) / cast))
    bills = [b for b in d["bills"] if b["per_par"] >= 2026
             and any(s["leg"] and s["leg"]["party"] == party
                     for s in d["spon"].get(b["id"], []))]
    mots = [m for m in d["motions"]
            if any(s["leg"] and s["leg"]["party"] == party
                   for s in d["signers"].get(m["id"], []))]
    ctte = [m for cid, ms in d["ctte_mem"].items() for m in ms
            if not m["amendment"] and m["role"] == "titular"
            and (m["bench"] == party or (m["leg"] or {}).get("party") == party)]

    # The site's own rule, printed on acerca.html: no percentage over fewer than
    # five opportunities. Four roll calls is four, so this says the count.
    if len(unity) >= 5:
        unity_tile = (f'<div class="tile"><dt>Unidad de voto</dt>'
                      f'<dd>{median(unity)}%<small>mediana de cuánto del grupo '
                      f'votó igual, sobre {len(unity)} votaciones con tres o más '
                      f'votos emitidos</small></dd></div>')
    elif unity:
        allsame = all(u == 100 for u in unity)
        unity_tile = (f'<div class="tile"><dt>Unidad de voto</dt>'
                      f'<dd>{len(unity)}<small>votaciones con tres o más votos '
                      f'emitidos: '
                      + ("en todas votó en bloque" if allsame else
                         "en " + str(sum(1 for u in unity if u == 100))
                         + " de ellas votó en bloque")
                      + '. Son muy pocas para un porcentaje</small></dd></div>')
    else:
        unity_tile = ""
    vrows = ""
    if votes:
        seen = sorted({k for _v, c, _t, _n in votes for k in c},
                      key=lambda k: pos(k)[2])
        vrows = ('<div class="benchrows">' + "".join(
            f'<div class="brow"><a class="bl" href="{vote_url(r, v)}">'
            f'<span class="nm">{fecha(v["held_on"])} · '
            f'{esc(clip(v["subject"], 60))}</span></a>'
            + stack_bar([(pos_col(k), f"{pos(k)[0]} {c.get(k, 0)}", c.get(k, 0))
                         for k in seen], sum(c.values()))
            + f'<span class="bn">{sum(c.values())}</span>'
            + f'<span class="bs">{esc(pos(top)[0].lower())}</span></div>'
            for v, c, top, _n in votes) + "</div>"
            + key_list([(pos_col(k), esc(pos(k)[0]),
                         sum(c.get(k, 0) for _v, c, _t, _n in votes))
                        for k in seen],
                       shapes=[pos_shape(k) for k in seen]))

    # This bench inside its own chamber: the seats it holds, lit.
    maps = []
    for ch, name in (("D", "Cámara de Diputados"), ("S", "Senado")):
        peers = [L for L in d["legs"] if L["chamber"] == ch and L["per_par"] >= 2026]
        got = sum(1 for L in peers if L["party"] == party)
        if not got:
            continue
        seats, hg = bench_seats(
            peers, r,
            colour=lambda L: (f"var(--gp{bench_slot(party)})"
                              if L["party"] == party else "var(--off)"),
            cls=lambda L: "circle" if L["party"] == party else ("ring", 1))
        maps.append(
            f'<figure class="hemifig"><h3>{esc(name)}</h3>'
            + hemicycle(seats, r, middle=(f"{got}", f"de {len(peers)}"),
                        sub=f"Escaños de {party} en {name}", ident=f"hemi-{ch}",
                        groups=hg)
            + f'<figcaption>{got} de {len(peers)} escaños '
              f'({round(100 * got / len(peers))} %): los círculos llenos.'
              f'</figcaption></figure>')

    cr = f"{r}{LOGO_CREDIT}"
    body = f"""<div class="crumb"><a href="{r}index.html">Inicio</a> ›
<a href="{r}bancadas.html">Bancadas</a> › {esc(party)}</div>
<div class="bhead">{bench_logo(party, r, 76)}
<div><span class="eyebrow">Grupo parlamentario</span>
<h1>{esc(party)}</h1></div></div>
<p class="lede">{len(mem)} parlamentarios en ejercicio: {len(dips)} en la
Cámara de Diputados y {len(sens)} en el Senado.</p>
<dl class="tiles" style="margin-top:22px">
<div class="tile"><dt>Escaños</dt><dd>{len(mem)}<small>{len(dips)} diputados ·
{len(sens)} senadores, de 190 en total</small></dd></div>
<div class="tile"><dt>Proyectos firmados</dt><dd>{len(bills)}<small>del periodo
2026-2031, con al menos una firma del grupo</small></dd></div>
<div class="tile"><dt>Mociones firmadas</dt><dd>{len(mots)}<small>de las
{len(d["motions"])} presentadas</small></dd></div>
{unity_tile}
<div class="tile"><dt>Plazas de titular</dt><dd>{len(ctte)}<small>en los cuadros
publicados, hoy solo los del Senado</small></dd></div>
</dl>
{"<section><h2>Dónde se sientan</h2>" + "".join(maps) + "</section>" if maps else ""}
{"<section><h2>Cómo ha votado el grupo</h2>" + vrows + "<p class='sm mut'>Una barra por votación, al 100 % de los integrantes del grupo en esa lista.</p></section>" if vrows else "<section><h2>Cómo ha votado el grupo</h2><p>Todavía no hay votaciones nominales publicadas en que figuren sus integrantes.</p></section>"}
<section><h2>Integrantes ({len(mem)})</h2>
{f'<h3 style="margin:18px 0 10px">Cámara de Diputados ({len(dips)})</h3>' + people_grid(dips, r, per_bench=True) if dips else ""}
{f'<h3 style="margin:26px 0 10px">Senado ({len(sens)})</h3>' + people_grid(sens, r, per_bench=True) if sens else ""}
</section>
{prov([
    f'Padrón, foto, bancada y circunscripción: portales de las cámaras, vía su '
    f'API REST.',
    f'Votaciones nominales: las actas en PDF de cada sesión.',
    f'Logotipo de {esc(party)}: {logo_credit_line(party)}. Identifica al grupo; '
    f'el partido no respalda este sitio. '
    f'<a href="{cr}">Autoría y licencia de cada logotipo</a>.',
    f'Regenerado el {fecha(today)}.'])}"""
    return shell(f"{party} · bancada", body, 1,
                 desc=f"{party}: {len(mem)} parlamentarios, cómo vota el grupo "
                      f"y qué ha firmado.")


def render_bancadas(d, today):
    rows = []
    for p in BENCH_ORDER:
        mem = [L for L in d["legs"] if L["party"] == p]
        if not mem:
            continue
        dips = sum(1 for L in mem if L["chamber"] == "D")
        sens = len(mem) - dips
        rows.append(
            f'<li class="{gp(p)}"><a href="bancada/{bench_slug(p)}.html">'
            f'{bench_logo(p, "", 56)}<span class="t2">'
            f'<span class="nm2">{esc(p)}</span>'
            f'<span class="sub">{dips} diputados · {sens} senadores</span>'
            f'</span><span class="big">{len(mem)}</span></a>'
            f'{stack_bar([(f"var(--gp{bench_slot(p)})", p, len(mem)), ("var(--sunk)", "resto", 190 - len(mem))], 190)}'
            f"</li>")
    tot = sum(1 for L in d["legs"] if L["per_par"] >= 2026)
    body = f"""<span class="eyebrow">Bancadas</span>
<h1>{len(rows)} grupos parlamentarios</h1>
<p class="lede">Los {len(rows)} grupos que se repartieron los {tot} escaños del
Congreso 2026-2031.</p>
<ul class="bgrid">{"".join(rows)}</ul>
<div class="note">El color de cada bancada lo asignamos nosotros, no es el del
partido. Los logotipos sí son suyos y están aquí para identificarlas, no porque
respalden este sitio.
<a href="{LOGO_CREDIT}">Autoría y licencia de cada logotipo</a>.</div>
{prov(['Padrón y bancada: portales de la Cámara de Diputados y del Senado, vía '
       'su API REST.',
       f'Logotipos: Wikimedia Commons, con la licencia de cada uno indicada en '
       f'<a href="{LOGO_CREDIT}">la página Acerca</a>.',
       f'Regenerado el {fecha(today)}.'])}"""
    return shell("Bancadas del Congreso 2026-2031", body, 0,
                 desc="Los seis grupos parlamentarios del Congreso peruano "
                      "2026-2031: escaños, integrantes y cómo votan.")


# --------------------------------------------------------------- list pages

def pager(name, n, total):
    """Anterior · 1 … 47 48 49 … 98 · Siguiente.

    The largest facet is 98 pages and printing all 98 numbers was five rows of
    44 px chrome above and below every list. A window plus the ends is the same
    navigation in one row."""
    if total < 2:
        return ""
    href = (lambda i: f'{name}{"" if i == 1 else "-" + str(i)}.html')
    want = sorted({1, 2, n - 1, n, n + 1, total - 1, total}
                  & set(range(1, total + 1)))
    out, last = [], 0
    if n > 1:
        out.append(f'<a href="{href(n - 1)}" rel="prev">‹ Anterior</a>')
    for i in want:
        if i - last > 1:
            out.append('<span class="mut">…</span>')
        out.append(f'<span class="on" aria-current="page">{i}</span>' if i == n
                   else f'<a href="{href(i)}">{i}</a>')
        last = i
    if n < total:
        out.append(f'<a href="{href(n + 1)}" rel="next">Siguiente ›</a>')
    return (f'<nav class="pager" aria-label="Páginas de este listado">'
            + "".join(out) + f'<span class="mut">{n} de {total}</span></nav>')


def paginate(name, title, intro, rows_html, depth, per=PER_PAGE, prov_lines=()):
    """Write name.html, name-2.html … Each page carries a filter box over its
    own rows. ponytail: no server, so the filter is per page, not per corpus;
    a real cross-corpus search needs a JSON index and fetch().

    `rows_html` is either plain `<li>` strings or `(grupo, repetido, <li>)`
    triples. With triples the page gets two things a 150-row list needs:

    · a sticky heading whenever the group changes, because an undifferentiated
      run of rows is dozens of screens with nothing to steer by, and the month
      is the one thing every row in these lists has; and

    · the hoist. `repetido` is a chip that may or may not be worth printing per
      row. When the same one lands on most of THIS page it is said once at the
      top and struck from the rows that agree with it, so what is left in the
      scroll is the exceptions. 162 of 250 rows said `EN COMISIÓN`; 19 of 24 on
      the tail page said `PUBLICADA EN EL DIARIO OFICIAL EL PERUANO`.
      Computed per page and not per facet: which state dominates changes as you
      page through a list sorted by date.
    """
    grouped = bool(rows_html) and isinstance(rows_html[0], tuple)
    pages = [rows_html[i:i + per] for i in range(0, len(rows_html), per)] or [[]]
    out = []
    for n, chunk in enumerate(pages, 1):
        nrows, hoist = len(chunk), ""
        if grouped:
            tally = {}
            for _g, rep, _li in chunk:
                if rep:
                    tally[rep] = tally.get(rep, 0) + 1
            drop, k = max(tally.items(), key=lambda kv: kv[1], default=((), 0))
            if k < max(8, 0.4 * nrows):
                drop = ()
            else:
                word = re.sub("<[^>]+>", " ", drop[-1]).strip()
                hoist = (f'<p class="sm mut"><b>{num(k)} de {num(nrows)} filas</b> '
                         f'están en «{esc(word)}». Las {num(nrows - k)} etiquetadas '
                         f'son las que no.</p>')
            marked, last = [], object()
            for g, rep, li in chunk:
                if g != last:
                    kk = sum(1 for g2, _r, _l in chunk if g2 == g)
                    marked.append(f'<li class="gh">{esc(g)}<b>{num(kk)}</b></li>')
                    last = g
                if drop and rep == drop:
                    for x in drop:
                        li = li.replace(x, "")
                marked.append(li)
            chunk = marked
        nav = pager(name, n, len(pages))
        body = f"""{intro}{hoist}
<div class="filters"><input id="q" type="search"
 placeholder="Filtrar esta página ({nrows} de {len(rows_html)})"
 aria-label="Filtrar"></div>
<ul class="feed{" nostg" if drop and k == nrows else ""}" id="ls">{"".join(chunk)}</ul>{nav}
{FILTER_JS}
{prov(list(prov_lines)) if prov_lines else ""}"""
        out.append((f'{name}{"" if n == 1 else "-" + str(n)}.html',
                    shell(title + (f" · página {n}" if n > 1 else ""), body, depth)))
    return out


# --------------------------------------------------------------------- build

def main():
    t0 = time.time()
    # One build at a time per output directory. The rmtree below deletes another
    # run's pages out from under it, so two overlapping builds leave a half
    # site and fail on a file that existed a second ago. That bit twice: a
    # deploy aborted mid-upload, and a self-check read a page another run had
    # just removed.
    OUT.mkdir(parents=True, exist_ok=True)
    lock = OUT / ".build.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        raise SystemExit(
            f"ya hay un build corriendo sobre {OUT} (pid "
            f"{lock.read_text(errors='replace').strip() or '?'}). "
            f"Si no es cierto, borra {lock}.") from None
    try:
        return _build(t0)      # demo() counts pages with what _build returns
    finally:
        lock.unlink(missing_ok=True)


def _build(t0):
    # Ids upstream are not stable (vote ids are being re-keyed right now), so a
    # page whose slug changed would otherwise linger forever as a dead copy.
    # Only our own output is removed; anything else in site/ is left alone.
    # `logos/` is NOT in this list and must never be: those files are checked
    # into the repo, not generated, and wiping them breaks every bench chip.
    for sub in ("proyecto", "proyectos", "parlamentario", "votacion", "comision",
                "asistencia", "bancada"):
        if (OUT / sub).exists():
            shutil.rmtree(OUT / sub)
    # The bench logos live in the repo, not in the database. Copy them into the
    # output so `build.py <db> <anywhere>` is a complete site: an earlier
    # version only worked when OUT happened to be the checked-in site/.
    # assets/ is the source and is tracked; site/ is generated and gitignored.
    # These lived under site/logos/ and were therefore the one input to the build
    # that a fresh `git clone` did not have -- and that an `rm -rf site` deleted
    # for good. Keep inputs out of the output directory.
    src_logos = ROOT / "assets" / "logos"
    if src_logos.is_dir():
        shutil.copytree(src_logos, OUT / "logos", dirs_exist_ok=True)
    # Same rule for the 190 portraits: tracked input under assets/, copied out.
    if PHOTOS.is_dir():
        shutil.copytree(PHOTOS, OUT / "photos", dirs_exist_ok=True)
    con = db.connect(DBP)
    d = load(con)
    today = dt.date.today().isoformat()
    n = {"proyecto": 0, "parlamentario": 0, "votacion": 0, "listado": 0, "csv": 0}

    # ---- bills. A long text gets its own page next to the ficha; a short one
    # stays inline, so most bills are one file, not two.
    n["texto"] = 0
    for b in d["bills"]:
        page, txt = render_bill(d, b)
        dr = OUT / "proyecto" / str(b["per_par"]) / (b["chamber"] or "C")
        write(dr / f'{b["ply_num"]}.html', page)
        n["proyecto"] += 1
        if txt:
            write(dr / f'{b["ply_num"]}-texto.html', txt)
            n["texto"] += 1

    # ---- legislators (peer baselines first: one pass, no N+1)
    base = {"bills": {}, "prim": {}, "mots": {}, "asis": {}, "asist": {}}
    for L in d["legs"]:
        c = L["chamber"]
        ok, den, _lic, _pre = leg_att_rate(d, L)
        if den >= 5:
            base["asist"].setdefault(c, []).append(round(100 * ok / den))
        mine = [x for x in d["leg_bills"].get(L["slug"], [])
                if d["bill_by_id"][x[0]]["per_par"] == L["per_par"]]
        base["bills"].setdefault(c, []).append(len(mine))
        base["prim"].setdefault(c, []).append(sum(1 for x in mine if (x[1] or 0) == 0))
        base["mots"].setdefault(c, []).append(len(d["leg_motions"].get(L["slug"], [])))
        vs = [x for x in d["leg_votes"].get(L["slug"], [])
              if pos(x[1])[1] in ("voto", "falta")]
        if vs:
            base["asis"].setdefault(c, []).append(
                round(100 * sum(1 for _, p in vs if pos(p)[1] == "voto") / len(vs)))
    for c in base:
        base[c].setdefault("D", [0])
        base[c].setdefault("S", [0])
    for L in d["legs"]:
        write(OUT / "parlamentario" / f'{L["slug"]}.html', render_leg(d, L, base))
        write(OUT / "parlamentario" / f'{L["slug"]}.csv', leg_csv(d, L))
        n["parlamentario"] += 1
        n["csv"] += 1

    # ---- asistencia: 13 takings, one page each plus the index
    for s in d["sesiones"]:
        write(OUT / "asistencia" / f'{s["slug"]}.html', render_att(d, s))
        n["listado"] += 1
    write(OUT / "asistencia.html", render_att_index(d, today))
    n["listado"] += 1

    g = render_gasto(d, today)
    if g:
        write(OUT / "gasto.html", g)
        n["listado"] += 1

    # ---- votes
    for v in d["votes"]:
        sl = v["slug"]
        write(OUT / "votacion" / f"{sl}.html", render_vote(d, v))
        n["votacion"] += 1
        if d["vrows"].get(v["id"]):
            write(OUT / "votacion" / f"{sl}.csv", vote_csv(d, v))
            n["csv"] += 1

    # ---- bill facets
    src = [f'Listado oficial de proyectos de ley, <code>{API}</code>, '
           f'regenerado el {fecha(today)}.']
    groups = {}   # key -> [page title, facet label, bills]

    def bucket(key, title, label, b):
        groups.setdefault(key, [title, label, []])[2].append(b)

    for b in d["bills"]:
        ch = b["chamber"] or "C"
        per = f'{b["per_par"]}-{b["per_par"] + 5}'
        bucket(f'p{b["per_par"]}-{ch}', f"{CHAMBER[ch]} · {per}",
               f"{CHAMBER[ch]} {per}", b)
        st = b["status"] or "sin estado"
        bucket(f"estado-{slugify(st)}", f"Proyectos en estado «{st}»", st, b)
        if b["presented_on"]:
            y = b["presented_on"][:4]
            bucket(f"anio-{y}", f"Proyectos presentados en {y}", y, b)
    for key, (title, _label, bs) in groups.items():
        # The CSV covers the facet, not the page: paginating a download would
        # be a download of the pagination.
        write(OUT / "proyectos" / f"{key}.csv", bills_csv(d, bs))
        n["csv"] += 1
        intro = (f'<div class="crumb"><a href="../index.html">Inicio</a> › '
                 f'<a href="../proyectos.html">Proyectos de ley</a></div>'
                 f'<span class="eyebrow">{num(len(bs))} proyectos</span>'
                 f'<h1>{esc(title)}</h1>'
                 + stage_mix(bs)
                 + f'<p><a href="{key}.csv" title="Una fila por proyecto, con '
                 f'estado, etapa, comisiones, número de firmas y el enlace a su '
                 f'ficha.">Descargar estos {num(len(bs))} proyectos en CSV</a>. Es el '
                 f'filtro completo, no solo esta página.</p>')
        for fn, htm in paginate(key, title, intro, bill_feed("../", bs), 1,
                                prov_lines=src):
            write(OUT / "proyectos" / fn, htm)
            n["listado"] += 1

    def facet_links(prefix, sort=None):
        ks = [k for k in groups if k.startswith(prefix)]
        ks.sort(key=sort or (lambda k: -len(groups[k][2])))
        return '<div class="facets">' + "".join(
            f'<a href="proyectos/{k}.html">{esc(groups[k][1])} '
            f'<b>{num(len(groups[k][2]))}</b></a>' for k in ks) + "</div>"

    def facet_bars(prefix, colour=lambda k: "var(--accent)"):
        """The same facets as a ranked bar chart. Twenty identical pills gave
        5 545 and 1 the same visual weight; a bar per row is the difference
        between a list of links and a picture of the corpus."""
        ks = sorted((k for k in groups if k.startswith(prefix)),
                    key=lambda k: -len(groups[k][2]))
        if not ks:
            return ""
        top = len(groups[ks[0]][2]) or 1
        tot = sum(len(groups[k][2]) for k in ks)
        return ('<ul class="ranked">' + "".join(
            f'<li><a href="proyectos/{k}.html">'
            f'<span class="lbl">{esc(groups[k][1])}</span>'
            f'<span class="track"><i style="width:{max(100 * len(groups[k][2]) / top, 0.6):.2f}%;'
            f'background:{colour(k)}"></i></span>'
            f'<span class="n">{num(len(groups[k][2]))}</span>'
            f'<span class="pc">{pctxt(len(groups[k][2]), tot)}</span></a></li>'
            for k in ks) + "</ul>")

    # ---- how much of the corpus is actually readable here. Computed, never
    # asserted: the extraction runs in the background and this changes daily.
    tread, timg, tpend = text_corpus(d)
    tot = len(d["bills"]) or 1
    texto_block = f"""<section id="texto"><h2>Cuántos proyectos se pueden leer aquí</h2>
<dl class="tiles">
<div class="tile"><dt>Con texto en línea</dt><dd>{num(tread)}
<small>{"menos del 1 %" if 0 < 100 * tread / tot < 0.5 else f"{100 * tread / tot:.0f} %"}
del registro · leídos del PDF oficial, con índice y buscador</small></dd></div>
<div class="tile"><dt>Publicados como imagen</dt><dd>{num(timg)}
<small>escaneados: el PDF no tiene capa de texto</small></dd></div>
<div class="tile"><dt>Todavía sin descargar</dt><dd>{num(tpend)}
<small>su PDF no ha pasado por nuestra ingesta; no que no tengan
texto</small></dd></div>
</dl>
<p class="sm mut" style="margin-top:12px">El Congreso no publica el articulado en
HTML ni como dato. Cada proyecto es un PDF, y ante cualquier diferencia manda el
original. En los listados van marcados «texto en línea».</p></section>"""

    # Where the whole corpus actually sits. Ordinal, not categorical: the stages
    # are a sequence, so the colour runs with the track and not around a wheel.
    dist = {}
    for b in d["bills"]:
        k = status_info(b["status"])[0]
        dist[k] = dist.get(k, 0) + 1
    slab = {"PRES": "Recién presentados", "COM": "En comisión",
            "PLENO": "En el Pleno", "AUTOG": "Autógrafa en el Ejecutivo",
            "LEY": "Ya son ley", "DEAD": "Archivados o retirados"}
    scol = {"PRES": "var(--sen)", "COM": "var(--wait)", "PLENO": "var(--accent)",
            "AUTOG": "var(--vio)", "LEY": "var(--ok)", "DEAD": "var(--dead)"}
    stot = sum(dist.values()) or 1
    etapa = ('<section><h2>En qué etapa está cada proyecto</h2><figure>'
             + stack_bar([(scol[k], slab[k], dist[k]) for k in slab if dist.get(k)],
                         stot, height=26)
             + key_list([(scol[k], esc(slab[k]),
                          f'{num(dist[k])} · {pctxt(dist[k], stot)}')
                         for k in slab if dist.get(k)])
             + f'<figcaption>Los {num(stot)} proyectos registrados por etapa. '
               f'Incluye el acervo 2021-2026, que es lo que permite ver qué '
               f'proporción llega a ser ley.</figcaption></figure></section>')
    hub = f"""<span class="eyebrow">Proyectos de ley</span>
<h1>{num(len(d["bills"]))} proyectos de ley</h1>
<p class="lede">Todo lo que se ha presentado en el Congreso desde 2021, con su estado
oficial traducido a lenguaje llano y el trámite que le falta a cada uno.</p>
{etapa}
{texto_block}
<section><h2>Por cámara y periodo</h2>{facet_links("p2")}</section>
<section><h2>Por estado publicado</h2>
<p class="sm mut">Los {sum(1 for k in groups if k.startswith("estado-"))} estados
que publica el Congreso, del más frecuente al más raro. La barra es relativa al
más numeroso.</p>
{facet_bars("estado-", lambda k: scol[status_info(groups[k][1])[0]])}</section>
<section><h2>Por año de presentación</h2>
{facet_bars("anio-")}</section>
<section><h2>Presentados más recientemente</h2>
<ul class="feed">{"".join(bill_row("", b) for b in d["bills"][:40])}</ul></section>
<section><h2>Descarga y cita</h2>
<p><a href="proyectos.csv" title="Código, periodo, cámara, título, estado, etapa,
fecha, proponente, comisiones, número de firmas, autor principal y enlace a la
ficha.">Descargar los {num(len(d["bills"]))} proyectos en CSV</a>, el corpus
entero. Cada filtro de arriba tiene además el suyo.</p>
<p class="sm mut">Cita sugerida: «Registro de proyectos de ley del Congreso de la
República del Perú», consultado el {fecha(today)}.</p>
</section>
{prov(src)}"""
    write(OUT / "proyectos.html", shell("Proyectos de ley", hub, 0))
    write(OUT / "proyectos.csv", bills_csv(d, d["bills"]))
    n["listado"] += 1
    n["csv"] += 1

    # ---- committees: the roster (Senado only) and the bills sitting there.
    # Half the committees have one and half the other, so each half of the page
    # is either rendered or explained, never left as an empty card.
    for c in d["cttes"].values():
        bs = [d["bill_by_id"][b] for b in d["ctte_bills"].get(c["id"], [])
              if b in d["bill_by_id"]]
        bs.sort(key=lambda b: b["presented_on"] or "", reverse=True)
        ms = d["ctte_mem"].get(c["id"], [])
        per = f'{c["per_par"]}-{c["per_par"] + 5}' if c["per_par"] else ""
        lede = ("Quiénes la integran, con su bancada, y qué proyectos de ley "
                "tiene en las manos. "
                if any(not m["amendment"] for m in ms) else
                f'Comisión {DE[c["chamber"] or "C"]}. ')
        lede += ("Una comisión que no dictamina archiva de hecho. Lo que siga aquí "
                 "cuando cierre el periodo caduca."
                 if bs else
                 "No tenemos proyectos derivados a esta comisión: las derivaciones "
                 "cargadas son las del Congreso 2021-2026.")
        intro = (f'<div class="crumb"><a href="../index.html">Inicio</a> › '
                 f'<a href="../comisiones.html">Comisiones</a> › '
                 f'{esc(CHAMBER[c["chamber"] or "C"])}</div>'
                 f'<span class="eyebrow">Comisión · '
                 f'{esc(CHAMBER[c["chamber"] or "C"])} {per}</span>'
                 f'<h1>{esc(c["name"])}</h1><p class="lede">{lede}</p>'
                 + roster(d, c))
        if not ms:
            intro += ('<div class="note">No publicamos su composición. La única fuente '
                      'es el Diario de los Debates de la sesión que aprueba el '
                      'cuadro, y solo tenemos el del Senado 2026-2031.</div>')
        plines = ctte_prov(d, c, today)
        if bs:
            intro += (f'<h2 style="margin-top:34px">Proyectos de ley en esta '
                      f'comisión ({num(len(bs))})</h2>' + stage_mix(bs))
            for fn, htm in paginate(c["slug"], c["name"], intro,
                                    bill_feed("../", bs), 1, prov_lines=plines):
                write(OUT / "comision" / fn, htm)
                n["listado"] += 1
        else:
            write(OUT / "comision" / f'{c["slug"]}.html',
                  shell(c["name"], intro + prov(plines), 1,
                        desc=f'Composición y proyectos de la {c["name"]}.'))
            n["listado"] += 1

    # ---- committee index. This is the page a reader lands on the moment they
    # tap Comisiones, and it was 35 identical grey rows. The one number that
    # differs between one comisión and the next is how many bills are parked in
    # it, which is also the page's own thesis, so it is drawn, ordered and
    # grouped by chamber instead of being a word at the end of a meta line.
    cttes = sorted(d["cttes"].values(),
                   key=lambda c: (-(c["per_par"] or 0), c["chamber"] or "C",
                                  -len(d["ctte_bills"].get(c["id"], [])), c["name"]))
    ctop = max((len(d["ctte_bills"].get(c["id"], [])) for c in cttes), default=0) or 1
    ctot = sum(len(d["ctte_bills"].get(c["id"], [])) for c in cttes) or 1
    crows, cg = [], object()
    for c in cttes:
        ms = d["ctte_mem"].get(c["id"], [])
        tit = sum(1 for m in ms if m["role"] == "titular" and not m["amendment"])
        nb = len(d["ctte_bills"].get(c["id"], []))
        g = (f'{CHAMBER[c["chamber"] or "C"]} · {c["per_par"]}-{c["per_par"] + 5}'
             if c["per_par"] else CHAMBER[c["chamber"] or "C"])
        if g != cg:
            cg = g
            k = sum(1 for c2 in cttes
                    if (f'{CHAMBER[c2["chamber"] or "C"]} · '
                        f'{c2["per_par"]}-{c2["per_par"] + 5}'
                        if c2["per_par"] else CHAMBER[c2["chamber"] or "C"]) == g)
            crows.append(f'<li class="gh">{esc(g)}<b>{k}</b></li>')
        meta = (f"{tit} titulares" if tit else
                "solo cambios por oficio" if ms else "composición no publicada")
        crows.append(
            f'<li><a href="{ctte_url("", c)}">'
            f'<span class="lbl">{esc(c["name"])}<small>{esc(meta)}</small></span>'
            f'<span class="track"><i style="width:{max(100 * nb / ctop, 0.6):.2f}%;'
            f'background:var(--wait)"></i></span>'
            f'<span class="n">{num(nb)}</span>'
            f'<span class="pc">{pctxt(nb, ctot) if nb else "sin copia"}</span>'
            f'</a></li>')
    withm = sum(1 for ms in d["ctte_mem"].values()
                if any(not m["amendment"] for m in ms))
    body = f"""<span class="eyebrow">Comisiones</span>
<h1>{len(d["cttes"])} comisiones</h1>
<p class="lede">La comisión es donde se decide casi todo: el 37 % de los proyectos
está parado en una, y la que no dictamina archiva de hecho. De {withm} tenemos
además el cuadro de miembros.</p>
<div class="note">Ninguna cámara publica la composición de sus comisiones como
dato. Estos cuadros salen del Diario de los Debates del Senado; la Cámara de
Diputados aún no publica el suyo, así que de sus comisiones solo existe el
nombre.</div>
<div class="filters"><input id="q" type="search"
 placeholder="Filtrar por nombre, cámara o periodo" aria-label="Filtrar comisiones"></div>
<ul class="ranked" id="ls">{"".join(crows)}</ul>
<p class="sm mut" id="cnt">La barra es cuántos proyectos tiene hoy cada comisión,
sobre la más cargada.</p>
{FILTER_JS}
{prov([
    f'Nombres y asignación de proyectos: expedientes en <code>{API}</code>.',
    f'Composición: <a href="{DIARIO}PLO-2026-3-SENADO.pdf">Diario de los Debates '
    f'del Senado ↗</a>, leído del PDF.',
    f'Regenerado el {fecha(today)}.'])}"""
    write(OUT / "comisiones.html", shell("Comisiones", body, 0))
    n["listado"] += 1

    # ---- bancadas: one page per bench plus the index. Every party chip on the
    # site lands here, so this is where the identity is defined for the reader.
    for p in BENCH_ORDER:
        if any(L["party"] == p for L in d["legs"]):
            write(OUT / "bancada" / f"{bench_slug(p)}.html",
                  render_bancada(d, p, today))
            n["listado"] += 1
    write(OUT / "bancadas.html", render_bancadas(d, today))
    n["listado"] += 1

    # ---- legislators index
    # The search results. 330 of these rows were a name in grey monospace with
    # no bench mark of any kind, on a site whose whole identity system is the
    # bench mark; they carry the logo now, like every other list of people.
    rows = "".join(
        f'<li class="{gp(L["party"])}">'
        + (bench_logo(L["party"], "", 22, "rowmark") if L["party"] else "")
        + f'<span class="m">{esc(CHAMBER_SHORT[L["chamber"]])} '
        f'{L["per_par"]}-{L["per_par"] + 5}'
        f'{" · " + esc(L["district"]) if L["district"] else ""}'
        f'{" · " + esc(L["party"]) if L["party"] else ""}</span>'
        f'<a class="t" href="parlamentario/{L["slug"]}.html">'
        f'{esc(nice_name(L["full_name"]))}</a></li>'
        for L in sorted(d["legs"], key=lambda L: (-L["per_par"], L["chamber"],
                                                  L["full_name"])))
    dips = sum(1 for L in d["legs"] if L["chamber"] == "D")
    sens = sum(1 for L in d["legs"] if L["chamber"] == "S")
    past = sum(1 for L in d["legs"] if L["per_par"] < 2026)
    chambers = ""
    for ch in ("D", "S"):
        mem = [L for L in d["legs"] if L["chamber"] == ch and L["per_par"] >= 2026]
        if not mem:
            continue
        chambers += f"""<figure class="hemifig" style="margin-bottom:34px"
 title="{CURULES} Cada bancada tiene su color y su figura porque seis colores no
se distinguen entre sí en las quince combinaciones posibles para quien no ve el
color igual que usted.">
<h3><span class="chip {ch.lower()}">{esc(CHAMBER[ch])}</span></h3>
{hall_svg(mem, ch)}
{bench_split(mem, lambda L: L["party"], "")}</figure>"""
    # The roster is the page people actually come for, so it opens with the
    # search and is cut into bench sections with their own headings and
    # anchors: the previous version was 39 000 px of ungrouped cards with the
    # search box at 75% of the document and no way to jump.
    now = [L for L in d["legs"] if L["per_par"] >= 2026]
    rail, sections = [], []
    for bp in BENCH_ORDER:
        mem = [L for L in now if L["party"] == bp]
        if not mem:
            continue
        nd = sum(1 for L in mem if L["chamber"] == "D")
        rail.append(f'<a class="railb" href="#{bench_slug(bp)}">'
                    f'{bench_logo(bp, "", 26)}<span>{esc(bp)}</span>'
                    f'<b>{len(mem)}</b></a>')
        sections.append(
            f'<section id="{bench_slug(bp)}" class="bsec {gp(bp)}">'
            f'<h3 class="bsech">{bench_logo(bp, "", 40)}'
            f'<a href="bancada/{bench_slug(bp)}.html">{esc(bp)}</a>'
            f'<span class="cnt">{len(mem)} escaños · {nd} diputados · '
            f'{len(mem) - nd} senadores</span></h3>'
            + people_grid(mem, "", per_bench=True) + "</section>")
    nogroup = [L for L in now if L["party"] not in BENCH]
    if nogroup:
        sections.append('<section id="sin-grupo" class="bsec gp0">'
                        f'<h3 class="bsech">Sin grupo registrado '
                        f'<span class="cnt">{len(nogroup)}</span></h3>'
                        + people_grid(nogroup, "", per_bench=True) + "</section>")
    body = f"""<span class="eyebrow">Padrón</span>
<h1>Busque a su parlamentario</h1>
<p class="lede">{dips} diputados y {sens} senadores del periodo 2026-2031{f", más {past} del Congreso unicameral 2021-2026 cuyas firmas siguen en el registro" if past else ""}.
Cada ficha reúne lo que firmó, lo que votó y si asiste.</p>
<div class="stick">
<div class="filters"><input id="q" type="search"
 placeholder="Escriba un nombre, una región o una bancada"
 aria-label="Filtrar el padrón"></div>
<div class="rail">{"".join(rail)}</div></div>
<ul class="feed" id="ls">{rows}</ul>
<p class="sm mut" id="cnt">{len(d["legs"])} parlamentarios.</p>
{FILTER_JS}
<div id="browse">
<section><h2>Las dos cámaras, escaño por escaño</h2>{chambers}</section>
<section><h2>Los {dips + sens} en ejercicio, por bancada</h2>
<p class="sm mut">Por bancada y, dentro de ella, por apellido.</p>
{"".join(sections)}</section></div>
<script>
/* Los 190 en ejercicio salían dos veces: una en la lista plana de búsqueda y
   otra, con retrato, en su bancada. 40 000 px de página, y a quien buscaba un
   nombre le quedaban 13 000 px de lista alrededor de su único resultado. Con
   JS, la lista plana es la vista de resultados y solo aparece al escribir; sin
   JS quedan las dos, que es lo que ve un rastreador. */
(function(){{var b=document.getElementById("browse"),f=document.getElementById("ls");
function sync(){{var on=q.value.trim().length>0;f.hidden=!on;b.hidden=on;}}
q.addEventListener("input",sync);sync();}})()
</script>
{prov([
    'Padrón, foto, bancada y circunscripción: portales de la '
    '<a href="https://diputados.congreso.gob.pe/">Cámara de Diputados ↗</a> y del '
    '<a href="https://senado.congreso.gob.pe/">Senado ↗</a>, vía su API REST. '
    'Los retratos son copias reducidas alojadas aquí, no enlaces al servidor de '
    'las cámaras.',
    f'Logotipos de bancada: Wikimedia Commons, '
    f'<a href="{LOGO_CREDIT}">autoría y licencia de cada uno</a>.',
    f'Regenerado el {fecha(today)}.'])}"""
    write(OUT / "parlamentarios.html", shell("Parlamentarios 2026-2031", body, 0))
    n["listado"] += 1

    # ---- motions index. 107 rows, and 107 of them carried the same chip:
    # «PARA SER VISTA POR LA JUNTA DE PORTAVOCES», repeated with zero variance
    # down 41 473 px. What actually differs between one moción and the next is
    # what kind of instrument it is, and 87 of the 107 are greetings, so that is
    # what groups the page and what the rail is coloured by. Said once above.
    fams, seen_st = {}, {m["status"] or "—" for m in d["motions"]}
    for m in d["motions"]:
        fam = moc_family(m["kind"])
        fams.setdefault(fam, []).append(m)
    mrows = []
    for fam in MOC_ORDER:
        ms = fams.get(fam)
        if not ms:
            continue
        label, tone = MOC_FAMILY[fam]
        # A moción de saludo is a formula: «que el Congreso salude a X por su
        # aniversario». 87 of them at full height were 30 000 px of the page,
        # and none of the twenty that do something were visible past them. The
        # routine instrument gets the compact row and says so in its heading;
        # the twenty that ask for an investigation, an interpelación or a
        # comisión get the full one, and come first.
        small = fam == "saludo"
        mrows.append(f'<li class="gh">{esc(label)}<b>{num(len(ms))}</b></li>')
        if small:
            mrows.append('<li class="ghn sm mut" style="padding:0 0 8px">Fórmulas '
                         'de cortesía: no obligan a nada.</li>')
        for m in ms:
            sg = d["signers"].get(m["id"], [])
            # Con cara. «Qué diputado la declaró» es la pregunta que trae a
            # alguien a una moción, y hasta aquí la respuesta era texto gris.
            keep = 1 if small else 3
            who = "".join(
                f'<li>{person_chip(s["leg"], "", None if s["leg"] else nice_name(s["name_raw"]), bench=True)}</li>'
                for s in sg[:keep])
            mrows.append(
                f'<li class="r {tone}" id="m-{esc(m["id"])}">'
                f'<span class="stg">{esc(label)}</span>'
                f'<span class="m">{esc(m["code"])} · '
                f'{esc(CHAMBER_SHORT[m["chamber"]])} · {fecha(m["presented_on"])}</span>'
                f'<span class="t">{esc(clip(m["summary"], 120 if small else 220))}</span>'
                f'<div class="sm mut sign" style="margin-top:6px">Firman:'
                f'<ul class="roll signers">{who}'
                + (f'<li class="more"><span class="pl" tabindex="0">'
                   f'y {len(sg) - keep} más</span>{signer_pop(sg[keep:])}</li>'
                   if len(sg) > keep else "")
                + '</ul></div>'
                + (f'<span class="chip wait" style="margin-top:6px">'
                   f'{esc(m["status"] or "")}</span>' if len(seen_st) > 1 else "")
                + "</li>")
    mmix = ('<figure style="margin:18px 0 0">'
            + stack_bar([(MOC_COL[f], MOC_FAMILY[f][0], len(fams[f]))
                         for f in MOC_ORDER if fams.get(f)], len(d["motions"]),
                        height=18)
            + key_list([(MOC_COL[f], esc(MOC_FAMILY[f][0]),
                         f'{len(fams[f])} · {pctxt(len(fams[f]), len(d["motions"]))}')
                        for f in MOC_ORDER if fams.get(f)])
            + '<figcaption>De qué tipo son. Las de saludo no obligan a nada; las '
              'de investigación, interpelación e invitación son control político. '
              'Abajo van en ese orden, no en el de llegada.'
              '</figcaption></figure>') if d["motions"] else ""
    onest = (f'<p class="sm mut">Las {len(d["motions"])} están en «'
             f'{esc(list(seen_st)[0])}»: la Junta de Portavoces decide cuáles se '
             f'ven en el Pleno y en qué orden.</p>'
             if len(seen_st) == 1 and d["motions"] else "")
    body = f"""<span class="eyebrow">Mociones de orden del día</span>
<h1>{len(d["motions"])} mociones</h1>
<p class="lede">El instrumento con el que el Congreso se pronuncia sin legislar:
saludos, pedidos de interpelación, comisiones investigadoras. En estos primeros
días hay más mociones que proyectos de ley.</p>
{mmix}
{onest}
<div class="filters"><input id="q" type="search"
 placeholder="Filtrar por texto, firmante o tipo" aria-label="Filtrar mociones"></div>
<ul class="feed" id="ls">{"".join(mrows)}</ul>
{FILTER_JS}
{prov([f'Registro de mociones del Congreso, '
       f'<code>https://api.congreso.gob.pe/smociones-portal-service</code> '
       f'(<a href="{MOCPORTAL}">portal oficial ↗</a>).',
       f'Regenerado el {fecha(today)}.'])}"""
    write(OUT / "mociones.html", shell("Mociones", body, 0))
    n["listado"] += 1

    # ---- votes index
    if d["votes"]:
        vrows = "".join(vote_feed_row(d, v) for v in d["votes"])
        extra = ""
    else:
        vrows = ""
        extra = ('<div class="note">Todavía no hay votaciones nominales '
                 'procesadas: las actas se publican en PDF unos días después de '
                 'cada sesión.</div>')
    body = f"""<span class="eyebrow">Votaciones nominales</span>
<h1>{len(d["votes"])} votaciones</h1>
<p class="lede">Cada votación nominal publicada, con el resultado por bancada,
quiénes rompieron con su grupo y la lista descargable.</p>{extra}
<ul class="feed" id="ls">{vrows}</ul>
{prov(['Actas de votación publicadas por la Cámara de Diputados y el Senado.',
       f'Regenerado el {fecha(today)}.'])}"""
    write(OUT / "votaciones.html", shell("Votaciones", body, 0))
    n["listado"] += 1

    # ---- acerca + robots. The site deploys public before it is finished, so
    # it ships closed to crawlers and open about what it is.
    write(OUT / "acerca.html", render_acerca(d, today))
    n["listado"] += 1
    write(OUT / "robots.txt",
          "# Este sitio todavía no está listo para ser indexado.\n"
          "User-agent: *\nDisallow: /\n")

    # ---- home
    write(OUT / "index.html", render_home(d, base, today))
    n["listado"] += 1

    con.close()
    print(f"páginas de proyecto : {n['proyecto']} "
          f"(+{n['texto']} de texto completo aparte)")
    print(f"páginas de parlamentario: {n['parlamentario']}")
    print(f"páginas de votación : {n['votacion']}")
    print(f"listados e índices  : {n['listado']} "
          f"(incluye {len(d['sesiones'])} tomas de asistencia)")
    print(f"descargas CSV       : {n['csv']} "
          f"(votaciones, parlamentarios, cada faceta de proyectos y el corpus)")
    print(f"total               : {sum(n.values()) - n['csv']} páginas HTML")
    print(f"tiempo              : {time.time() - t0:.1f}s -> {OUT}")
    print("CAPS: fichas de parlamentario listan 60 proyectos y 40 mociones "
          "recientes (el total sí se muestra); los listados paginan de "
          f"{PER_PAGE} en {PER_PAGE} sin recortar nada, agrupados por mes.")
    return n


REPO = "https://github.com/axvg/congreso"

CC_BY_SA = "https://creativecommons.org/licenses/by-sa/4.0/deed.es"


def logos_table():
    """Attribution for every bench logo, from the credits file the downloader
    wrote. One of the six is CC BY-SA 4.0, which obliges us to name the author
    and link the licence wherever the work is shown, so every page that shows a
    logo carries the one-line credit and links here, and both are built from
    the same JSON rather than from a list someone retyped."""
    cred = LOGO_CREDITS
    if not cred:
        # ponytail: the credit file ships next to the images; if it is missing
        # the honest page says so instead of printing an unsourced logo table.
        return ('<p class="note">No encontramos el archivo de créditos de los '
                'logotipos, así que no publicamos la tabla de autoría.</p>')
    rows = []
    for p in BENCH_ORDER:
        c = cred.get(p)
        if not c:
            continue
        lic = c.get("licencia", "")
        lic_html = (f'<a href="{CC_BY_SA}">CC BY-SA 4.0</a>'
                    if "BY-SA" in lic.upper() else
                    "Dominio público" if "PUBLIC" in lic.upper() else esc(lic))
        rows.append(
            f'<tr><td class="who2">{bench_logo(p, "", 32)}</td>'
            f'<td><a href="bancada/{bench_slug(p)}.html">{esc(p)}</a></td>'
            f'<td>{esc(c.get("autor") or "—")}</td>'
            f'<td>{lic_html}</td>'
            f'<td><a href="{esc(c.get("pagina", ""))}">Wikimedia Commons ↗</a></td>'
            f"</tr>")
    return ('<div class="scroll"><table><thead><tr><th></th><th>Bancada</th>'
            '<th>Autoría</th><th>Licencia</th><th>Origen</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
            '<p class="sm mut">El logotipo de Juntos por el Perú se publica bajo '
            f'<a href="{CC_BY_SA}">Creative Commons Atribución-CompartirIgual '
            '4.0</a>, que obliga a nombrar a su autor y a enlazar la licencia: '
            'ambas cosas están en la fila correspondiente. Los otros cinco son '
            'de dominio público en cuanto a derecho de autor, pero siguen siendo '
            'marcas de sus organizaciones.</p>'
            '<p class="sm mut"><b>Los hemos modificado.</b> Commons publica '
            'varios de estos archivos como SVG; aquí se sirven rasterizados a '
            'PNG o JPG de 320 px de ancho, sin ningún otro cambio de forma ni '
            'de color. La licencia CC BY-SA 4.0 obliga a indicar esa '
            'modificación y esto es la indicación.</p>')


def render_acerca(d, today):
    """Who is speaking, where every number comes from, and how to argue with it.
    Counts are computed, not typed: a page that claims the site has N of
    something has to be right the day after the next ingest."""
    tread, timg, tpend = text_corpus(d)
    LOGOS_TABLE = logos_table()
    prov_votes = sum(1 for v in d["votes"] if v["provisional"])
    prov_att = len({s["source_url"] for s in d["sesiones"]
                    if "PROVISIONAL" in (s["source_url"] or "").upper()})
    body = f"""<div class="crumb"><a href="index.html">Inicio</a> › Acerca</div>
<span class="eyebrow">Acerca de este sitio</span>
<h1>Qué es Hemiciclo y qué no es</h1>
<p class="lede"><b>Esto no es el Congreso de la República.</b> Es un registro
independiente, hecho por una persona, que toma los datos que el Congreso publica
y los ordena para que se puedan leer y comprobar. No representamos a ninguna
cámara, ni a ninguna bancada, ni gestionamos trámite alguno.</p>

<section><h2>Quién lo hace</h2>
<p>Lo construye y mantiene <b>axvg</b>, a título personal, sin financiamiento ni
vínculo con el Congreso, con partidos ni con medios. El código que descarga los
datos y genera estas páginas es público: <a href="{REPO}">{REPO}</a>. Cualquiera
puede correrlo y obtener este mismo sitio de las mismas fuentes.</p></section>

<section><h2>De dónde sale cada dato</h2>
<dl class="kv">
<div><dt>Proyectos de ley</dt><dd>Registro oficial del Congreso,
<code>{API}/proyecto-ley</code>, y el expediente de cada proyecto en el portal
SPLey: título, sumilla, estado, fecha, firmas, historial y comisiones.</dd></div>
<div><dt>Estado traducido a lenguaje llano</dt><dd>Lo agregamos nosotros. El
Congreso publica un código («EN COMISIÓN»); la frase que lo explica y los pasos
que faltan salen del Reglamento del Congreso. Es la única parte redactada del
sitio.</dd></div>
<div><dt>Parlamentarios</dt><dd>Portales de la
<a href="https://diputados.congreso.gob.pe/">Cámara de Diputados</a> y del
<a href="https://senado.congreso.gob.pe/">Senado</a>, vía su API REST. Del
padrón 2021-2026 solo sobrevive el nombre en el filtro de autores de la base de
proyectos: por eso esas fichas no tienen bancada ni foto.</dd></div>
<div><dt>Texto de los proyectos</dt><dd>El PDF que el propio Congreso adjunta al
expediente, convertido a texto con <code>pdftotext</code>. Hoy
{"se puede leer" if tread == 1 else "se pueden leer"}
aquí <b>{num(tread)}</b> de {num(len(d["bills"]))} proyectos;
<b>{num(timg)}</b> son imagen escaneada, sin texto que extraer, y
<b>{num(tpend)}</b> aún no pasan por la descarga.
<a href="proyectos.html#texto">El detalle, en el índice de proyectos</a>. Ante
cualquier diferencia manda el PDF.</dd></div>
<div><dt>Mociones</dt><dd>Registro de mociones,
<code>smociones-portal-service</code>.</dd></div>
<div><dt>Votaciones nominales</dt><dd>Las actas en PDF que cada cámara publica
después de la sesión, y el Diario de los Debates cuando existe. Leemos el
documento; no hay una versión en datos.</dd></div>
<div><dt>Asistencia</dt><dd>Las listas de asistencia en PDF de cada sesión, una
por toma. Tampoco existen como dato: <a href="asistencia.html">aquí está cada
documento enlazado</a>.</dd></div>
<div><dt>Comisiones</dt><dd>Los nombres, del expediente de cada proyecto. La
composición, del Diario de los Debates de la sesión en que el Senado aprobó sus
cuadros: ninguna cámara publica quién integra sus comisiones.</dd></div>
<div><dt>El cruce entre padrón y firmas</dt><dd>Por nombre normalizado. No
existe un identificador compartido entre el padrón de las cámaras y la base de
proyectos; cuando un nombre no cruza, la página lo dice en vez de
adivinar.</dd></div>
</dl></section>

<section><h2>Lo que es provisional, dicho como tal</h2>
<p>Parte del material de votación viene sellado por la propia cámara como
<b>INFORMACIÓN PROVISIONAL · SIN LOS VOTOS ORALES</b>: el tablero no recoge los
votos de viva voz, y el conteo definitivo se publica después en el Diario de los
Debates.
{f"Hoy {prov_votes} de las {len(d['votes'])} votaciones y {prov_att} de las listas de asistencia llevan ese sello." if prov_votes or prov_att else ""}
Donde la fuente es provisional, la página lo dice, muestra las cifras en
conflicto y nombra cuál usa. Ninguna se presenta como definitiva.</p>
<p>Lo mismo con las personas: <b>una licencia no es una inasistencia</b> y
<b>quien preside no está faltando</b>. Ninguna entra en el denominador de un
porcentaje de asistencia, y no publicamos porcentajes sobre menos de cinco
oportunidades: damos el conteo y decimos por qué.</p>
<p><b>El plano del hemiciclo lo inventamos nosotros.</b> {CURULES} Lo que sí es
del registro es quién ocupa cada escaño: los diagramas agrupan a cada bancada en
un tramo contiguo del arco y ordenan alfabéticamente dentro de ella. Dos escaños
vecinos en esta web no son dos escaños vecinos en el Palacio
Legislativo.</p></section>

<section id="logos"><h2>Logotipos de las bancadas</h2>
<p>Cada bancada aparece con su logotipo, un color y su nombre. <b>El color lo
asignamos nosotros</b> y no es el del partido. Seis colores de partido no se
distinguen entre sí para un lector con daltonismo, así que salen de una paleta
comprobada. <b>El logotipo sí es del partido.</b> Lo usamos para identificar al
grupo, igual que la prensa, y nunca para sugerir que el partido respalde este
sitio.</p>
{LOGOS_TABLE}
<p class="sm mut">Si usted representa a alguna de estas organizaciones y quiere
que retiremos o reemplacemos su logotipo, escríbanos en
<a href="{REPO}/issues">{REPO}/issues</a> y lo hacemos.</p></section>

<section><h2>Pedir una corrección</h2>
<p>Un nombre mal cruzado, un voto que no es el suyo, una inasistencia que era
una licencia: si algo sobre usted o sobre alguien más está mal, <b>pídanos la
corrección y la haremos</b>. Abra un caso en
<a href="{REPO}/issues">{REPO}/issues</a> indicando la página, el dato que está
mal y, si puede, el documento oficial que lo demuestra.</p>
<p>Dos advertencias. Si el error está en la fuente del Congreso lo corregimos
igual, pero anotando qué dice el documento original. Y no borramos hechos
publicados por el Congreso a pedido de quien aparece en ellos: un voto registrado
es un acto público.</p></section>

<section><h2>Licencia y reuso</h2>
<p>Los datos originales son actos públicos del Congreso de la República del
Perú y su reuso es libre. Lo que agregamos nosotros, o sea la traducción de los
estados, los cruces, las tasas calculadas y estas páginas, se ofrece bajo
<a href="https://creativecommons.org/licenses/by/4.0/deed.es">Creative Commons
Atribución 4.0</a>: úselo para lo que quiera, incluso comercialmente, citando
la fuente y enlazando a la página de la que lo tomó.</p>
<p>Para reusar en volumen hay CSV: la <a href="proyectos.html">lista de
proyectos</a> y cada uno de sus filtros, cada
<a href="votaciones.html">votación nominal</a> y la ficha de cada
<a href="parlamentarios.html">parlamentario</a>. Si necesita un corte que no
existe, pídalo en el repositorio.</p></section>

<section><h2>Por qué todavía no queremos aparecer en buscadores</h2>
<p>Cada página lleva <code>noindex</code> y el
<a href="robots.txt">robots.txt</a> desautoriza a los rastreadores. Es
deliberado: el sitio es público para poder ser revisado, no para ser el primer
resultado sobre una persona con nombre y apellido antes de que cada cruce esté
verificado. Se quitará cuando lo esté.</p></section>
{prov([
    'Se regenera entero en cada corrida de la ingesta: no hay base de datos en '
    'producción ni edición manual de páginas.',
    f'Última regeneración: {fecha(today)}. '
    f'{num(len(d["bills"]))} proyectos, {len(d["legs"])} parlamentarios, '
    f'{len(d["votes"])} votaciones y {len(d["sesiones"])} tomas de asistencia.',
    f'Código y correcciones: <a href="{REPO}">{REPO}</a>.',
])}"""
    return shell("Acerca de este sitio", body, 0,
                 desc="Quién hace Hemiciclo, de dónde sale cada dato, cómo pedir "
                      "una corrección y bajo qué licencia se puede reusar.")


def bench_rankings(d, r=""):
    """Las bancadas ordenadas por lo que hacen: cuánto presentan, cuánto firman,
    cuánto faltan y cuántas veces quedan del lado que gana.

    Nada se calcula dos veces. Los proyectos y las mociones salen de los mismos
    índices de firmas que usa la página de cada bancada, la asistencia del mismo
    `leg_att_rate` que la ficha de cada parlamentario, y «votó con la mayoría»
    de `gp_agree`, que `load()` cuenta una vez por votación. Una tabla que
    contradiga a la página de la bancada no puede aparecer aquí.

    Y cada lista dice sobre qué divide. Con cinco votaciones nominales y trece
    tomas de asistencia, un porcentaje suelto sería ruido con forma de dato:
    `pct_or_note` decide si lo hay, y la cifra pequeña de cada fila lleva el
    denominador pegado.
    """
    benches = [p for p in BENCH_ORDER
               if any(L["party"] == p and L["per_par"] >= 2026 for L in d["legs"])]
    if not benches:
        return ""
    mem = {p: sum(1 for L in d["legs"]
                  if L["party"] == p and L["per_par"] >= 2026) for p in benches}

    def signed(items, index):
        """Cuántas piezas lleva al menos una firma de cada bancada. Una pieza
        firmada por dos bancadas cuenta en las dos: es lo que la pregunta
        «quién presenta más» quiere saber, y por eso los totales no suman."""
        c = dict.fromkeys(benches, 0)
        for it in items:
            for p in {s["leg"]["party"] for s in index.get(it["id"], []) if s["leg"]}:
                if p in c:
                    c[p] += 1
        return c

    bills = signed([b for b in d["bills"] if b["per_par"] >= 2026], d["spon"])
    mots = signed(d["motions"], d["signers"])
    falta = dict.fromkeys(benches, 0)
    opor = dict.fromkeys(benches, 0)
    for L in d["legs"]:
        if L["party"] not in benches or L["per_par"] < 2026:
            continue
        ok, b, _lic, _pre = leg_att_rate(d, L)
        falta[L["party"]] += b - ok
        opor[L["party"]] += b
    agree = dict.fromkeys(benches, (0, 0))
    for (_ch, p), (a, t) in d["gp_agree"].items():
        if p in benches:
            x, y = agree[p]
            agree[p] = (x + a, y + t)

    def lst(title, rows, note):
        """rows: [(bancada, valor para la barra, cifra, denominador)], ya ordenada."""
        top = max((v for _p, v, _n, _s in rows), default=0) or 1
        return (f'<div class="card"><h3>{esc(title)}</h3>'
                '<ul class="ranked" style="margin-top:12px">' + "".join(
                    f'<li><a href="{bench_url(r, p)}">'
                    f'<span class="lbl bl">{bench_logo(p, r, 22)}'
                    f'<span>{esc(p)}</span></span>'
                    f'<span class="track"><i style="width:'
                    f'{max(100 * v / top, 1.2):.1f}%;'
                    f'background:var(--gp{bench_slot(p)})"></i></span>'
                    f'<span class="n">{esc(big)}</span>'
                    f'<span class="pc">{esc(small)}</span></a></li>'
                    for p, v, big, small in rows)
                + f'</ul><p class="sm mut" style="margin-top:10px">{note}</p></div>')

    def rate(n, base, unit):
        """«38 % de 260», o el conteo desnudo cuando el denominador es un puñado."""
        txt, is_pct = pct_or_note(n, base, unit)
        return f"{txt} de {num(base)}" if is_pct else f"sobre {num(base)}"

    ntot = sum(mem.values())
    out = [
        lst("Quién presenta más proyectos",
            sorted(((p, bills[p], num(bills[p]),
                     f"{bills[p] / mem[p]:.1f} por escaño") for p in benches),
                   key=lambda x: -x[1]),
            f"Proyectos del periodo 2026-2031 con al menos una firma del grupo. "
            f"Un proyecto firmado por dos bancadas cuenta en las dos, así que la "
            f"columna no suma {num(sum(bills.values()))}."),
        lst("Quién firma más mociones",
            sorted(((p, mots[p], num(mots[p]),
                     f"de {num(len(d['motions']))}") for p in benches),
                   key=lambda x: -x[1]),
            f"De las {num(len(d['motions']))} mociones presentadas, en cuántas "
            f"firma al menos un integrante del grupo."),
        lst("Quién falta más",
            sorted(((p, falta[p] / max(opor[p], 1), num(falta[p]),
                     rate(falta[p], opor[p], "oportunidades")) for p in benches),
                   key=lambda x: -x[1]),
            f"Inasistencias contadas persona por persona sobre las "
            f"{len(d['sesiones'])} tomas de asistencia publicadas: una licencia "
            f"y quien presidió la sesión no son oportunidades de faltar y no "
            f"entran en el denominador. La barra ordena por proporción, no por "
            f"tamaño del grupo."),
        lst("Quién vota con la mayoría",
            sorted(((p, agree[p][0] / max(agree[p][1], 1), num(agree[p][0]),
                     rate(agree[p][0], agree[p][1], "votaciones"))
                    for p in benches), key=lambda x: -x[1]),
            f"Votaciones nominales en que la posición mayoritaria del grupo fue "
            f"la que ganó. Solo hay {len(d['votes'])} actas procesadas: esto es "
            f"un conteo, todavía no una tendencia."),
    ]
    return (f'<section><h2>Las {len(benches)} bancadas, comparadas</h2>'
            f'<div class="grid g2">{"".join(out)}</div>'
            f'<p class="sm mut" style="margin-top:14px">{ntot} escaños en '
            f'{len(benches)} bancadas. Cada lista ordena por su propia medida y '
            f'lleva su denominador al lado.</p></section>')


def home_cttes(d, r=""):
    """Las comisiones de este Congreso, con quién las preside.

    En portada porque la comisión es donde muere o sale un proyecto y hasta aquí
    la palabra «comisiones» solo aparecía como un destino más del índice. Las
    del Senado traen mesa directiva porque el Senado la publica; las de la
    Cámara no, y la fila lo dice en lugar de dejar el hueco."""
    cur = [c for c in d["cttes"].values() if (c["per_par"] or 0) >= 2026]
    if not cur:
        return ""
    cols = []
    for ch in ("D", "S"):
        cs = sorted((c for c in cur if c["chamber"] == ch),
                    key=lambda c: (not mesa_of(d, c), c["name"]))
        if not cs:
            continue
        # Sin mesa que enseñar, la fila es sólo el nombre: repetir «mesa directiva
        # sin publicar» diecinueve veces dice una vez lo que pasa y dieciocho
        # veces nada. Va una sola vez, arriba.
        any_mesa = any(mesa_of(d, c) for c in cs)
        rows = "".join(
            f'<li><a class="t" href="{ctte_url(r, c)}">{esc(c["name"])}</a>'
            + mesa_row(d, c, r) + "</li>" for c in cs)
        cols.append(f'<div class="card"><h3><span class="chip {ch.lower()}">'
                    f'{esc(CHAMBER[ch])}</span> <b>{len(cs)}</b></h3>'
                    + ("" if any_mesa else '<p class="sm mut" style="margin:8px 0 0">'
                       'Aún no publica quién las preside.</p>')
                    + f'<ul class="cttelist{"" if any_mesa else " tight"}">'
                      f'{rows}</ul></div>')
    nmesa = sum(1 for c in cur if mesa_of(d, c))
    return f"""<section><h2>Las comisiones, y quién las preside</h2>
<div class="grid g2">{"".join(cols)}</div>
<p class="sm mut" style="margin-top:14px">La presidencia decide qué proyecto se
ve. Publicadas {nmesa} de {len(cur)} mesas: el Senado sí, la Cámara de Diputados
todavía no. <a href="{r}comisiones.html">Todas las comisiones</a>.</p></section>"""


def render_home(d, base, today):
    cur = [b for b in d["bills"] if b["per_par"] >= 2026]
    old = [b for b in d["bills"] if b["per_par"] < 2026]
    days = 0
    if cur:
        first = min(b["presented_on"] for b in cur if b["presented_on"])
        days = max((dt.date.fromisoformat(today) - dt.date.fromisoformat(first)).days, 1)
    old_days = 1
    if old:
        ds = [b["presented_on"] for b in old if b["presented_on"]]
        old_days = max((dt.date.fromisoformat(max(ds))
                        - dt.date.fromisoformat(min(ds))).days, 1)
    rate = len(cur) / days if days else 0
    old_rate = len(old) / old_days

    # The hero: both chambers as the thing they are, a half circle of seats.
    # Nothing else on this site says "Congress" as fast as this does.
    halls, cols = [], []
    for ch in ("D", "S"):
        bs = [b for b in cur if (b["chamber"] or "C") == ch][:5]
        ms = [m for m in d["motions"] if m["chamber"] == ch][:4]
        mem = [L for L in d["legs"] if L["chamber"] == ch and L["per_par"] >= 2026]
        nb = sum(1 for b in cur if (b["chamber"] or "C") == ch)
        nm = sum(1 for m in d["motions"] if m["chamber"] == ch)
        halls.append(f"""<figure class="hemifig hall">
<h3><span class="chip {ch.lower()}">{esc(CHAMBER[ch])}</span></h3>
{hall_svg(mem, ch)}
{bench_split(mem, lambda L: L["party"], "")}
</figure>""")
        cols.append(f"""<div class="card">
<h3><span class="chip {ch.lower()}">{esc(CHAMBER[ch])}</span></h3>
<dl class="tiles" style="margin:16px 0 20px">
<div class="tile"><dt>Proyectos</dt><dd>{nb}<small>presentados en esta cámara</small></dd></div>
<div class="tile"><dt>Mociones</dt><dd>{nm}<small>presentadas en esta cámara</small></dd></div></dl>
{"<h3>Últimos proyectos</h3><ul class='feed'>" + "".join(bill_row("", b) for b in bs) + "</ul>" if bs else ""}
{"<h3 style='margin-top:18px'>Últimas mociones</h3><ul class='feed'>" + "".join(
    f'<li><span class="m">{esc(m["code"])} · {fecha(m["presented_on"])}</span>'
    f'<a class="t" href="mociones.html#m-{esc(m["id"])}">'
    f'{esc(clip(m["summary"], 120))}</a></li>' for m in ms) + "</ul>" if ms else ""}
</div>""")

    # How many bills a Congress files, month by month. The completed 2021-2026
    # period is the only series long enough to have a shape, and its shape is
    # the answer to "is 44 bills in three weeks a lot".
    per_month = {}
    for b in d["bills"]:
        if b["presented_on"]:
            per_month[b["presented_on"][:7]] = per_month.get(b["presented_on"][:7], 0) + 1
    ms = sorted(per_month)
    flow = ""
    if len(ms) > 6:
        pts = [(f'{MES[int(k[5:]) - 1][:3]} {k[2:4]}' if k.endswith(("-01", "-07"))
                else "",
                f'{MES[int(k[5:]) - 1]} de {k[:4]}', per_month[k]) for k in ms]
        top_k = max(ms, key=lambda k: per_month[k])
        # The last column is however many days of the current month have
        # happened, drawn against sixty complete ones. Outlined and named as
        # partial, so a month that is 11 days old does not read as a collapse.
        lastd = int(max(b["presented_on"][:10] for b in d["bills"]
                        if b["presented_on"])[8:10])
        part = (f"mes en curso, solo {lastd} días" if ms[-1] == today[:7] else None)
        flow = f"""<section><h2>Cuántos proyectos entran cada mes</h2>
<figure>{col_chart(pts, "proyectos", 170, "flow", "var(--accent)", partial=part)}
<figcaption>Un mes por columna, de {fecha(ms[0] + "-01")[len("1 de "):]} a
{fecha(ms[-1] + "-01")[len("1 de "):]}. El pico es {MES[int(top_k[5:]) - 1]} de
{top_k[:4]}, con {num(per_month[top_k])}. Se marcan enero y julio, cuando abren
las legislaturas.
{"La última columna va punteada: el mes lleva " + str(lastd) + " días." if part else ""}
</figcaption></figure></section>"""

    # stage distribution of the whole corpus
    dist = {}
    for b in d["bills"]:
        dist[status_info(b["status"])[0]] = dist.get(status_info(b["status"])[0], 0) + 1
    lab = {"PRES": "Recién presentados", "COM": "En comisión",
           "PLENO": "En el Pleno", "AUTOG": "Autógrafa en el Ejecutivo",
           "LEY": "Ya son ley", "DEAD": "Archivados o retirados"}
    col = {"PRES": "var(--sen)", "COM": "var(--wait)", "PLENO": "var(--accent)",
           "AUTOG": "var(--vio)", "LEY": "var(--ok)", "DEAD": "var(--dead)"}
    tot = sum(dist.values()) or 1
    bar = (stack_bar([(col[k], lab[k], dist[k]) for k in lab if dist.get(k)], tot,
                     height=22)
           + key_list([(col[k], esc(lab[k]),
                        f'{num(dist[k])} · {pctxt(dist[k], tot)}')
                       for k in lab if dist.get(k)]))

    votes_block = ""
    if d["votes"]:
        votes_block = ("<section><h2>Últimas votaciones nominales</h2>"
                       "<ul class='feed'>"
                       + "".join(vote_feed_row(d, v) for v in d["votes"][:6])
                       + "</ul></section>")

    # «Empezar por aquí»: los mismos tres grupos que el índice de la barra, con
    # el tamaño real de cada destino al lado. Sale de NAV, así que no puede
    # discrepar de la navegación ni quedarse con un destino menos.
    #
    # Sin la glosa de cada destino. Eran ocho descripciones de dos líneas, 120
    # palabras para decir qué secciones hay, y la cifra ya lo dice: «Proyectos
    # de ley 14 908» no necesita que le expliquen qué es, y «Bancadas 6 · las
    # seis, con su tamaño…» se repetía su propio número en letra. El texto largo
    # sigue existiendo donde sirve: en el <details> de la barra, que a ancho de
    # teléfono es lo único que hay.
    sizes = {"proyectos.html": num(len(d["bills"])),
             "mociones.html": num(len(d["motions"])),
             "parlamentarios.html": num(len(d["legs"])),
             "bancadas.html": str(len(BENCH_ORDER)),
             "comisiones.html": num(len(d["cttes"])),
             "votaciones.html": num(len(d["votes"])),
             "asistencia.html": num(len(d["sesiones"])),
             "gasto.html": num(len(gasto_rank(d)))}
    start_here = "".join(
        f'<div class="card"><span class="eyebrow">{esc(label)}</span>'
        f'<ul class="toc" style="margin-top:6px">'
        + "".join(f'<li><a href="{u}"><b>{esc(t)}</b> '
                  f'<span class="mut sm">{sizes.get(u, "")}</span></a></li>'
                  for u, t, _dsc in dests)
        + "</ul></div>" for label, dests in NAV)

    nominal = sum(1 for v in d["votes"] if d["vrows"].get(v["id"]))
    last = max((b["presented_on"] or "") for b in d["bills"]) if d["bills"] else today
    body = f"""<span class="eyebrow">Congreso de la República del Perú · 2026-2031</span>
<h1>Qué está haciendo el Congreso ahora mismo</h1>
<p class="lede">Cada proyecto de ley con su etapa y cada votación con quién
votó qué. Último movimiento registrado: <b>{fecha(last)}</b>.</p>

<section><h2>Los 190 escaños, uno por uno</h2>
<div class="halls">{"".join(halls)}</div>
<p class="sm mut" style="margin-top:16px"><a href="bancadas.html">Las seis
bancadas, una por una</a>.</p></section>

{bench_rankings(d)}

{home_cttes(d)}

<section><h2>Actividad del Congreso actual</h2>
<dl class="tiles">
<div class="tile"><dt>Proyectos presentados</dt><dd>{len(cur)}
<small>{rate:.1f} por día en {days} días de legislatura</small></dd></div>
<div class="tile"><dt>Mociones</dt><dd>{len(d["motions"])}
<small>{len(d["motions"]) / max(days, 1):.1f} por día</small></dd></div>
<div class="tile"><dt>Firmas de parlamentarios</dt>
<dd>{sum(len(d["spon"].get(b["id"], [])) for b in cur)}
<small>en {len(cur)} proyectos</small></dd></div>
<div class="tile"><dt>Votaciones nominales</dt><dd>{len(d["votes"])}
<small>{nominal} con lista nominal</small></dd></div>
</dl>
<p class="sm mut" style="margin-top:14px">El anterior Congreso presentó
{old_rate:.1f} proyectos por día; este va a {rate:.1f}.</p></section>

<section><h2>Empezar por aquí</h2>
<div class="grid g3">{start_here}</div></section>

<section><h2>Lo último de cada cámara</h2><div class="grid g2">{"".join(cols)}</div></section>
{votes_block}
{flow}

<section><h2>Dónde están los {num(len(d["bills"]))} proyectos registrados</h2>
{bar}
<p class="sm mut" style="margin-top:12px">Incluye el acervo 2021-2026, que es el
que permite ver cuántos llegan a ser ley.
<a href="proyectos.html">Explorar por etapa</a>.</p></section>

<section><h2>Otros cortes del registro</h2><div class="facets">
<a href="mociones.html">Mociones <b>{len(d["motions"])}</b></a>
<a href="proyectos/p2026-D.html">Proyectos de Diputados <b>{sum(1 for b in cur if b["chamber"] == "D")}</b></a>
<a href="proyectos/p2026-S.html">Proyectos del Senado <b>{sum(1 for b in cur if b["chamber"] == "S")}</b></a>
<a href="proyectos.csv">El corpus en CSV <b>{num(len(d["bills"]))}</b></a>
</div></section>

{prov([
    'Todo el contenido proviene de tres fuentes oficiales: el registro de '
    f'proyectos de ley (<code>{API}</code>), el registro de mociones '
    '(<code>smociones-portal-service</code>) y los portales de la '
    '<a href="https://diputados.congreso.gob.pe/">Cámara de Diputados ↗</a> y el '
    '<a href="https://senado.congreso.gob.pe/">Senado ↗</a>.',
    'Lo único que agregamos es la traducción del estado a lenguaje llano y el '
    'cruce por nombre entre padrón y firmas.',
    f'Sitio regenerado el {fecha(today)}; el dato más reciente es del '
    f'{fecha(last)}.',
])}"""
    return shell("Hemiciclo · Congreso del Perú 2026-2031", body, 0,
                 desc="Registro público del Congreso bicameral del Perú: proyectos "
                      "de ley, mociones, votaciones y parlamentarios.")


def demo():
    """Self-check. `python3 build.py` runs the build, then this."""
    n = main()
    con = db.connect(DBP)
    b = dict(con.execute(
        "SELECT * FROM bill WHERE per_par=2026 ORDER BY ply_num DESC LIMIT 1").fetchone())
    p = OUT / "proyecto" / str(b["per_par"]) / b["chamber"] / f'{b["ply_num"]}.html'
    t = p.read_text()
    assert "Trámite legislativo" in t and "Etapa actual" in t, "stepper missing"
    assert "Qué falta para que sea ley" in t, "next steps missing"
    spons = [r["name_raw"] for r in con.execute(
        "SELECT name_raw FROM bill_sponsor WHERE bill_id=? ORDER BY rank", (b["id"],))]
    if spons:
        leg = con.execute("SELECT slug FROM legislator").fetchall()
        slugs = {r["slug"] for r in leg}
        assert any(f"{s}.html" in t for s in slugs), "no sponsor linked on bill page"
    # A sitting member: the 2021 rows carry no party, by design.
    L = dict(con.execute("SELECT * FROM legislator WHERE chamber IN ('D','S') "
                         "LIMIT 1").fetchone())
    lp = OUT / "parlamentario" / f'{L["slug"]}.html'
    assert L["party"] in lp.read_text(), "party missing on legislator page"
    for f in ("index.html", "proyectos.html", "parlamentarios.html",
              "votaciones.html", "mociones.html"):
        assert (OUT / f).exists(), f
    small = [p for p in OUT.rglob("*.html") if p.stat().st_size < 6000]
    assert not small, f"{len(small)} pages under 6 KB: {small[:3]}"
    nv = con.execute("SELECT count(*) FROM vote").fetchone()[0]
    assert n["votacion"] == nv, "vote pages != roll calls"

    # The roster must exist in the markup, not only inside a JS array: a crawler
    # with no JS has to see every legislator and every link. This is the
    # regression that cost us the link graph once.
    pages = {p: p.read_text() for p in (OUT / "votacion").glob("*.html")}
    for vid, cnt in con.execute(
            "SELECT vote_id, count(*) FROM vote_row GROUP BY 1"):
        pg, t = next((p, x) for p, x in pages.items() if f"<code>{vid}</code>" in x)
        i = t.index('<tbody id="tb">')
        body = t[i:t.index("</tbody>", i)]
        assert body.count("<tr") >= cnt, \
            f"{pg.name}: {body.count('<tr')} rows in markup, {cnt} in the DB"
        assert body.count("../parlamentario/") >= cnt - 3, \
            f"{pg.name}: only {body.count('../parlamentario/')} anchors for {cnt} rows"

    # No published falsehood about a named person: whoever chaired a session is
    # never reported as having skipped it, and a licencia is never an absence.
    for vid, name in con.execute(
            "SELECT vote_id, name_raw FROM vote_row WHERE position IN "
            "('OTRO','LICENCIA')"):
        row = con.execute(
            "SELECT slug FROM legislator WHERE upper(last_name)=upper(?) OR "
            "upper(full_name)=upper(?)",
            (name.split(",")[0], name)).fetchone()
        if not row:
            continue
        t = (OUT / "parlamentario" / f'{row["slug"]}.html').read_text()
        assert ">0%<" not in t.replace("(0%)", ""), \
            f'{row["slug"]}: 0% presence published over a licencia/presidencia'
        assert ">0 de " not in t.split("Mociones firmadas")[-1].split("</dl>")[0], \
            f'{row["slug"]}: excused roll call counted in the denominator'

    # No raw database enum may reach a reader: every position is spelled out.
    for raw in {r[0] for r in con.execute("SELECT DISTINCT position FROM vote_row")}:
        assert raw in POS, f"position {raw} has no Spanish label"
    for p in list((OUT / "parlamentario").glob("*.html"))[:40]:
        t = p.read_text()
        for raw in ("<td>SI<", "<td>ABST<", "<td>OTRO<", "<td>PRESIDENCIA<",
                    "<td>LICENCIA<", "<td>BLANCO<"):
            assert raw not in t, f"{p.name}: raw enum {raw} leaked into the page"

    # The page, the member's row and the export must tell the same story about
    # how reliable a roll call is.
    for vid, in con.execute("SELECT id FROM vote WHERE id IN "
                            "(SELECT vote_id FROM vote_row)"):
        pg, t = next((p, x) for p, x in pages.items() if f"<code>{vid}</code>" in x)
        csvp = pg.with_suffix(".csv")
        if csvp.exists():
            with csvp.open() as fh:
                state = next(csv.DictReader(fh))["estado_lectura"]
            assert state in ("firme", "confirmado", "corregido", "disputado",
                             "provisional"), state
            if state != "firme":
                assert "id=\"lectura\"" in t, f"{pg.name}: {state} but no disclosure"

    # ---- comisiones. "EN COMISIÓN" is only an answer if the page names which
    # one, so the bill page has to carry the name and the link, in the ficha and
    # inside the stepper node.
    row = con.execute(
        "SELECT b.per_par, b.chamber, b.ply_num, c.name, c.slug FROM bill b "
        "JOIN bill_committee bc ON bc.bill_id=b.id "
        "JOIN committee c ON c.id=bc.committee_id "
        "WHERE b.status LIKE 'EN COMISI%' LIMIT 1").fetchone()
    if row:
        t = (OUT / "proyecto" / str(row["per_par"]) / (row["chamber"] or "C")
             / f'{row["ply_num"]}.html').read_text()
        assert row["name"] in t, "bill in committee does not name it"
        node = t.split('class="track"')[1].split("</ol>")[0]
        assert f'comision/{row["slug"]}.html' in node, \
            "the stepper node does not link the committee holding the bill"

    # Every ordinary committee of this Senate seats exactly twelve titulares. A
    # page showing anything else means a join went wrong -- or that an oficio
    # amendment was counted as a thirteenth member, which it never is.
    ord12 = con.execute(
        "SELECT c.slug, count(*) n FROM committee_member m "
        "JOIN committee c ON c.id=m.committee_id "
        "WHERE m.role='titular' AND m.amendment=0 GROUP BY 1 HAVING n=12").fetchall()
    assert len(ord12) >= 8, f"only {len(ord12)} committees with 12 titulares"
    for c in ord12:
        t = (OUT / "comision" / f'{c["slug"]}.html').read_text()
        lst = t.split('id="titulares"')[1].split("</ul>")[0]
        assert lst.count("<li") == 12, \
            f'{c["slug"]}: {lst.count("<li")} titulares en la página, 12 en la base'
        amd = con.execute("SELECT count(*) FROM committee_member m JOIN committee c "
                          "ON c.id=m.committee_id WHERE c.slug=? AND m.amendment=1",
                          (c["slug"],)).fetchone()[0]
        if amd:
            assert "Cambios posteriores" in t, f'{c["slug"]}: amendment rows hidden'

    # A senator's page must list the committees they sit on, linked.
    sen = con.execute(
        "SELECT l.slug, count(*) n FROM committee_member m "
        "JOIN legislator l ON l.id=m.legislator_id WHERE m.amendment=0 "
        "GROUP BY 1 ORDER BY n DESC LIMIT 1").fetchone()
    t = (OUT / "parlamentario" / f'{sen["slug"]}.html').read_text()
    blk = t.split("<h2>Comisiones")[1].split("</section>")[0]
    assert blk.count("../comision/") >= sen["n"], \
        f'{sen["slug"]}: {blk.count("../comision/")} enlaces para {sen["n"]} comisiones'
    assert "titular" in blk or "suplente" in blk, "role missing on legislator page"

    # Diputados have no published cuadros: their pages must say why, not render
    # an empty card. Same rule for a committee with no bills.
    dip = con.execute("SELECT slug FROM legislator WHERE chamber='D' LIMIT 1").fetchone()
    t = (OUT / "parlamentario" / f'{dip["slug"]}.html').read_text()
    blk = t.split("<h2>Comisiones")[1].split("</section>")[0]
    assert "Cámara de Diputados no ha publicado" in blk, "deputy left without a reason"
    assert "<ul" not in blk and "../comision/" not in blk, "empty committee card"
    for p in (OUT / "comision").glob("*.html"):
        t = p.read_text()
        assert '<ul class="feed" id="ls"></ul>' not in t, f"{p.name}: empty bill list"
        assert '<ul class="roll" id="titulares"></ul>' not in t, f"{p.name}: empty roster"
    assert (OUT / "comisiones.html").exists()

    # ---- asistencia. 1 340 rows that were ingested and rendered nowhere. The
    # two rules that matter are about named people, so they are asserted against
    # the generated HTML, not against the helper that produced it.
    takings = con.execute(
        "SELECT chamber, held_on, taken_at, count(*) n FROM attendance "
        "GROUP BY 1, 2, 3").fetchall()
    assert takings, "no attendance in the DB"
    for k in takings:
        sl = (f'{k["chamber"].lower()}-{k["held_on"]}-'
              f'{slugify(k["taken_at"])}')
        t = (OUT / "asistencia" / f"{sl}.html").read_text()
        rows = t.split('<tbody id="ls">')[1].split("</tbody>")[0]
        assert rows.count("<tr") == k["n"], \
            f'{sl}: {rows.count("<tr")} filas en la página, {k["n"]} en la base'
        assert "../parlamentario/" in rows, f"{sl}: roster without links"
    assert (OUT / "asistencia.html").exists()
    idx = (OUT / "asistencia.html").read_text()
    assert idx.count("asistencia/") >= len(takings), "index misses a taking"
    assert re.search(r"mediana de las \d+ tomas", idx), \
        "el resumen por cámara no dice sobre qué población es su mediana"

    # A licencia is not an absence and neither is presiding: not in the label,
    # not in the denominator, not anywhere on the person's page.
    d2 = load(con)
    for L in d2["legs"]:
        xs = d2["leg_att"].get(L["slug"], [])
        if not xs:
            continue
        t = (OUT / "parlamentario" / f'{L["slug"]}.html').read_text()
        blk = t.split("<h2>Asistencia al Pleno</h2>")[1].split("</section>")[0]
        ok, den, lic, pre = leg_att_rate(d2, L)
        raw = con.execute(
            "SELECT status, count(*) FROM attendance WHERE legislator_id=? "
            "GROUP BY 1", (L["id"],)).fetchall()
        raw = dict(raw)
        assert den == raw.get("PRE", 0) + raw.get("AUS", 0) - pre, \
            f'{L["slug"]}: denominador {den} incluye licencias o presidencia'
        assert lic == sum(v for k, v in raw.items() if k in ("LO", "LE", "LP", "L")), \
            f'{L["slug"]}: licencias mal contadas'
        assert f"{ok} de {den}" in blk, f'{L["slug"]}: rate not published'
        # Two medians on this site read 95-ish and mean different things: this
        # one is over people, the one on /asistencia is over takings. Neither
        # may be published without saying which population it ran over.
        if "mediana" in blk:
            assert re.search(r"mediana de los \d+ (diputados|senadores)", blk), \
                f'{L["slug"]}: una mediana sin decir sobre qué población'
        # every excused taking is rendered as such, never as an absence.
        # Counted inside the table only: the strip above it names the same state
        # once more in each cell's title, and double-counting it would make this
        # check fire on a page that is right.
        tb = blk.split("<tbody>")[1].split("</tbody>")[0]
        # One per row: the bold label leading the "qué significa" cell. The
        # letter chip repeats the state in its title and hidden text, so a bare
        # substring count would triple-count every absence.
        assert tb.count("<b>Ausente.</b>") == den - ok, \
            f'{L["slug"]}: {tb.count("<b>Ausente.</b>")} ausencias en la ' \
            f'tabla, {den - ok} reales'
        if den < 5:
            assert "muy pocas" in blk, f'{L["slug"]}: bare rate over {den} takings'
    for s in d2["sesiones"]:
        for x in s["rows"]:
            if att_state(d2, s, x)[1] != "falta":
                continue
            assert (s["chamber"], s["held_on"],
                    (x["leg"] or {}).get("slug")) not in d2["presided"], \
                f'{x["name_raw"]}: presiding counted as an absence'

    # ---- the 22 people with a page in each Congress link both ways.
    twins = [(a, b) for a, bs in d2["twin"].items() for b in bs]
    assert len(twins) >= 44, f"only {len(twins)} cross-links"
    for slug, other in twins:
        t = (OUT / "parlamentario" / f"{slug}.html").read_text()
        assert f'{other["slug"]}.html' in t and "Es la misma persona" in t, \
            f"{slug} does not point at its other Congress"

    # ---- no page may name a phantom committee, and none may get a page.
    assert d2["alias"], "the five oficio-only committees resolved to nothing"
    ghosts = {con.execute("SELECT name FROM committee WHERE id=?",
                          (cid,)).fetchone()[0]: c
              for cid, c in d2["alias"].items()}
    for name, canon in ghosts.items():
        assert name != canon["name"]
        sl = slugify(name)
        assert not list((OUT / "comision").glob(f"{sl}*.html")), f"page for {name}"
    nsen = con.execute("SELECT count(*) FROM committee WHERE per_par=2026").fetchone()[0]
    assert len(d2["cttes"]) == nsen + 24 - len(ghosts), "committee count off"

    # ---- gasto. Lo que puede salir mal aquí es publicar una cifra que no es de
    # quien dice serla, así que se comprueban las tres cosas que la sostienen.
    rank = gasto_rank(d2)
    if rank:
        ids = [L2["id"] for _t, L2, _g in rank]
        assert len(ids) == len(set(ids)), \
            "un despacho sale dos veces en el ranking de gasto"
        assert all(L2["per_par"] == 2021 for _t, L2, _g in rank), \
            f"{GASTO_Y} es del Congreso 2021-2026 y hay asientos de otro periodo"
        # Nada repartido: la suma publicada tiene que ser exactamente la suma de
        # lo que el portal atribuye a un despacho, ni un sol del resto.
        att = con.execute(
            "SELECT (SELECT coalesce(sum(total),0) FROM payroll "
            " WHERE legislator_id IS NOT NULL AND year=?) "
            "+ (SELECT coalesce(sum(total),0) FROM travel "
            "   WHERE legislator_id IS NOT NULL AND year=?)", (GASTO_Y,) * 2
        ).fetchone()[0]
        assert abs(sum(t for t, _L, _g in rank) - att) < 1, \
            "el total publicado no cuadra con lo que el portal atribuye"
        g_html = (OUT / "gasto.html").read_text()
        assert "no se publica" in g_html, \
            "la página de gasto perdió lo que el Congreso NO publica"

    # ---- CSV on legislators and on the bill corpus, and they have to parse.
    L = d2["legs"][0]
    lp = OUT / "parlamentario" / f'{L["slug"]}.csv'
    with lp.open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows and {"tipo", "fecha", "pagina"} <= set(rows[0]), "leg CSV shape"
    kinds = {r["tipo"] for r in rows}
    assert kinds & {"proyecto", "mocion", "votacion", "comision", "asistencia"}, kinds
    assert f'{L["slug"]}.csv' in (OUT / "parlamentario" / f'{L["slug"]}.html').read_text(), \
        "legislator CSV not linked from the page"
    with (OUT / "proyectos.csv").open() as fh:
        allrows = list(csv.DictReader(fh))
    nb = con.execute("SELECT count(*) FROM bill").fetchone()[0]
    assert len(allrows) == nb, f"{len(allrows)} rows in proyectos.csv, {nb} bills"
    assert "proyectos.csv" in (OUT / "proyectos.html").read_text(), "corpus CSV unlinked"
    facet = con.execute("SELECT count(*) FROM bill WHERE per_par=2021").fetchone()[0]
    with (OUT / "proyectos" / "p2021-C.csv").open() as fh:
        assert len(list(csv.DictReader(fh))) == facet, "facet CSV != facet size"
    assert "p2021-C.csv" in (OUT / "proyectos" / "p2021-C.html").read_text(), \
        "facet CSV not linked on its own page"

    # ---- the bill page states a base rate as a number, with its denominator,
    # instead of asserting what "la mayoría" of bills do.
    row = con.execute("SELECT per_par, chamber, ply_num FROM bill "
                      "WHERE status='EN COMISIÓN' LIMIT 1").fetchone()
    t = (OUT / "proyecto" / str(row["per_par"]) / (row["chamber"] or "C")
         / f'{row["ply_num"]}.html').read_text()
    blk = t.split("<h2>Qué suele pasar en esta etapa</h2>")[1].split("</section>")[0]
    rt = d2["rates"]["stage"]["COM"]
    assert num(rt["n"]) in blk and num(rt["ley"]) in blk and num(rt["dictamen"]) in blk, \
        "the base rate on the bill page is not the computed one"
    # a share, stated as a share. «menos del 1 %» counts, «0%» never did
    assert re.search(r"\d+(?:\.\d)? %|menos del 1 %", blk), \
        "no percentage on the base-rate block"
    assert "(0%)" not in blk and "(0 %)" not in blk, \
        "a non-empty share published as 0%"
    assert "La mayoría de proyectos se quedan aquí" not in t, \
        "unbacked claim still published"

    # ---- the text of the bill on the site. Three states, three renderings, and
    # the table fills in the background: every check here is conditional on the
    # DB having reached that state, and none of them hardcodes a count.
    def billpath(row):
        return (OUT / "proyecto" / str(row["per_par"]) / (row["chamber"] or "C")
                / f'{row["ply_num"]}.html')

    tb = con.execute("SELECT bill_id FROM bill_text WHERE body IS NOT NULL "
                     "ORDER BY chars DESC LIMIT 1").fetchone()
    if tb:
        b2 = con.execute("SELECT * FROM bill WHERE id=?", (tb["bill_id"],)).fetchone()
        p2 = billpath(b2)
        t = p2.read_text()
        meta = con.execute("SELECT pages, chars, source_url, body FROM bill_text "
                           "WHERE bill_id=?", (b2["id"],)).fetchone()
        assert 'id="texto"' in t, f'{b2["id"]}: bill with text has no text section'
        assert '<a href="#texto">' in t, f'{b2["id"]}: no Texto link in the ficha'
        assert num(meta["chars"]) in t and meta["source_url"] in t, \
            f'{b2["id"]}: length or source PDF missing from the text section'
        tp = p2.with_name(f'{b2["ply_num"]}-texto.html')
        full = tp.read_text() if tp.exists() else t
        # Server-rendered: the text is in the markup, the search only filters it.
        assert 'id="ls"' in full and 'id="q"' in full, f'{b2["id"]}: no text search'
        assert full.count("<p>") > 1, f'{b2["id"]}: text not rendered as paragraphs'
        heads = [x for x in text_blocks(meta["body"]) if x[0] == "h"]
        if heads:
            toc = t.split('class="toc"')[1].split("</ol>")[0]
            for a in re.findall(r'href="[^"#]*#([^"]+)"', toc):
                assert f'id="{a}"' in full, f'{b2["id"]}: TOC anchor #{a} has no target'
            assert f'id="{heads[0][1]}"' in full, "first heading not anchored"
            assert len(re.findall(r'<li><a href="[^"]*#', toc)) == len(heads), \
                f'{b2["id"]}: table of contents misses a heading'

    nt = con.execute("SELECT bill_id, note FROM bill_text WHERE note IS NOT NULL "
                     "LIMIT 1").fetchone()
    if nt:
        b2 = con.execute("SELECT * FROM bill WHERE id=?", (nt["bill_id"],)).fetchone()
        t = billpath(b2).read_text()
        blk = t.split('<section id="texto">')[1].split("</section>")[0]
        assert esc(nt["note"]) in blk, "the reason there is no text is not quoted"
        assert "imagen" in blk, "no plain-Spanish explanation of the missing text"
        assert 'class="txt"' not in blk and 'id="q"' not in blk, \
            "empty text container rendered for a bill with no text"
        src = con.execute("SELECT source_url FROM bill_text WHERE bill_id=?",
                          (nt["bill_id"],)).fetchone()["source_url"]
        assert not src or src in blk, "the PDF is not linked when there is no text"

    nor = con.execute("SELECT per_par, chamber, ply_num FROM bill WHERE id NOT IN "
                      "(SELECT bill_id FROM bill_text) LIMIT 1").fetchone()
    if nor:
        assert 'id="texto"' not in billpath(nor).read_text(), \
            "a bill whose PDF we have not fetched claims something about its text"

    # The corpus counts are the DB's, not a number someone typed.
    tread, timg, tpend = text_corpus(d2)
    assert (tread, timg) == tuple(con.execute(
        "SELECT count(body), count(note) FROM bill_text t "
        "JOIN bill b ON b.id=t.bill_id").fetchone()), "text corpus counts drift"
    assert tread + timg + tpend == len(d2["bills"])
    blk = (OUT / "proyectos.html").read_text().split(
        '<section id="texto">')[1].split("</section>")[0]
    for k in (tread, timg, tpend):
        assert num(k) in blk, f"{k} not stated on the bill index"
    assert num(tread) in (OUT / "acerca.html").read_text(), \
        "acerca.html does not say how much of the corpus is readable"

    # ---- noindex and the legal page, on every page this build writes.
    # progress.html and balance.html belong to progress.py; robots.txt covers them.
    pages = [p for p in OUT.rglob("*.html")
             if p.name not in ("progress.html", "balance.html")]
    jp = f'logos/{BENCH["Juntos por el Perú"][2]}'
    # the file name, not the directory: «psicologos/» is a word in a bill text
    marks = [f"logos/{f}" for _s, _m, f in BENCH.values()]
    withlogo = 0
    titles, heads, lists = {}, {}, []
    for p in pages:   # one read per page: this walks 15k files
        t = p.read_text()
        assert '<meta name="robots" content="noindex,nofollow">' in t, f"{p}: indexable"
        assert "acerca.html" in t, f"{p}: no link to the legal page"
        # Harvested on the pass we are already making, so the checks below cost
        # no second walk over 17 500 files.
        titles[p] = re.search(r"<title>(.*?)</title>", t, re.S).group(1)
        h = re.search(r"<h1[^>]*>(.*?)</h1>", t, re.S)
        heads[p] = h.group(1) if h else ""
        # Nothing this site paints may come from another host. 190 portraits
        # were hotlinked at full resolution, 37.7 MB to fill 38 px thumbnails,
        # and one upstream rename from a site with no faces on it.
        assert not re.search(r'<img[^>]+src="https?:', t), \
            f"{p}: an <img> still points at another host"
        m0 = re.search(r'<ul class="(?:feed|ranked)[^"]*" id="ls">', t)
        if m0:
            body = t[m0.end():].split("</ul>")[0]
            if body.count("<li") >= 20:
                lists.append((p, t, body))
        for g in ghosts:
            assert f">{g}<" not in t, f"{p}: names the phantom committee {g}"
        # The licence travels with the work. Tested on every page that paints a
        # logo, not on a sample of eight: the sample passed while 252 pages
        # showed a mark with no credit at all, and 78 of them showed the one
        # logo whose licence obliges us to name its author.
        if any(m in t for m in marks):
            withlogo += 1
            assert LOGO_CREDIT in t, f"{p}: logotipo sin enlace a los créditos"
        if jp in t:
            for must in ("Txolo", "CC BY-SA 4.0", "modificación"):
                assert must in t, f"{p}: obra CC BY-SA sin «{must}»"
    assert withlogo >= 200, f"solo {withlogo} páginas con logotipo: ¿se dejaron de pintar?"
    assert (OUT / "robots.txt").read_text().strip().endswith("Disallow: /")
    ac = (OUT / "acerca.html").read_text()
    for must in ("no es el Congreso", "corrección", "Creative Commons",
                 "PROVISIONAL", "issues", REPO):
        assert must in ac, f"acerca.html says nothing about {must}"

    # ---- el hemiciclo. The seat plan is the one picture this subject has, and
    # it is only worth anything if it is in the markup: exactly one seat per
    # member, every seat an anchor to a real ficha, no JavaScript involved.
    def seat_block(text, ident):
        """The whole figure: the <svg> and the hover layer that sits on it. The
        link to a member's ficha moved out of the SVG and into that layer when
        the seat got a hover card, so an assertion that stops at </svg> counts
        the shapes and none of the links."""
        return text.split(f'id="{ident}"')[1].split("</div>")[0]

    def seats_in(text, ident):
        """A seat is a circle, a diamond, a square or a triangle now, so this
        counts the class every one of them carries, not one element name."""
        blk = seat_block(text, ident)
        # `class="seat ` with the space: the hover layer's own wrapper is
        # `class="seatlayer"` and counted as a 131st seat without it.
        return blk.count('class="seat '), blk.count('<a class="sl" href="')

    home = (OUT / "index.html").read_text()
    for ch, want in (("D", 130), ("S", 60)):
        got = con.execute("SELECT count(*) FROM legislator WHERE chamber=? "
                          "AND per_par>=2026", (ch,)).fetchone()[0]
        assert got == want, f"{ch}: {got} escaños en la base, {want} legales"
        ns, na = seats_in(home, f"hemi-{ch}")
        assert ns == want, f"portada, {ch}: {ns} escaños dibujados, {want} reales"
        assert na == want, f"portada, {ch}: {na} enlaces para {want} escaños"
    # and the links point at pages that exist
    hrefs = re.findall(r'<a class="sl" href="(parlamentario/[^"]+)"',
                       seat_block(home, "hemi-S"))
    assert len(set(hrefs)) == 60, f"{len(set(hrefs))} fichas distintas en 60 escaños"
    for h in hrefs[:10]:
        assert (OUT / h).exists(), f"escaño enlaza a {h}, que no existe"

    # ---- la tarjeta del escaño. El pie que explicaba cómo leer el diagrama se
    # borró porque la tarjeta dice lo mismo apuntando: si la tarjeta se queda
    # sin nombre, sin cara o sin marca de bancada, la portada pierde el dato y
    # no queda nada debajo que lo devuelva.
    for ch, want in (("D", 130), ("S", 60)):
        blk = seat_block(home, f"hemi-{ch}")
        assert blk.count('<span class="pop">') == want, \
            f'portada, {ch}: {blk.count(chr(60) + "span class=" + chr(34) + "pop")} '\
            f"tarjetas para {want} escaños"
        assert blk.count('class="popt"') == want, f"{ch}: tarjetas sin nombre"
        # cara y marca de bancada, una por tarjeta: el retrato local o la
        # baldosa de iniciales, y el logotipo o el monograma.
        assert (blk.count('class="face shot"') + blk.count('class="noface face"')
                == want), f"{ch}: tarjetas sin retrato ni baldosa"
        assert blk.count('class="blogo') == want, f"{ch}: tarjetas sin bancada"
        # y ninguna imagen se descarga hasta que se abre la tarjeta
        assert blk.count('loading="lazy"') >= want, f"{ch}: imágenes sin lazy"
        # un <title> por figura, el del diagrama. Un tooltip nativo por escaño
        # se abriría encima de la tarjeta un segundo después.
        assert blk.count("<title>") == 1, \
            f"{ch}: {blk.count('<title>')} tooltips nativos compitiendo con la tarjeta"

    # ---- el asunto de una votación es prosa. El tablero imprime las cabeceras
    # de su tabla en vertical, una letra por línea, y `pdftotext` las devuelve
    # en horizontal pegadas al final: «…APROBADA POR AMPLIA MAYORÍA V A T I A R
    # M F O IN I A P C O». Es la portada la que enseñaba eso.
    for vid, subj in con.execute("SELECT id, subject FROM vote "
                                 "WHERE subject IS NOT NULL"):
        assert not re.search(r"(?:\s+[A-ZÁÉÍÓÚÜÑ]{1,2}\b){4,}\s*$", subj), \
            f"{vid}: el asunto acaba en la cabecera vertical del tablero: "\
            f"…{subj[-44:]}"

    # A roll call draws one seat per row of the nominal list, coloured by the
    # vote and not by the bench, and every one of them is still a link.
    vpages = {p: p.read_text() for p in (OUT / "votacion").glob("*.html")}
    for vid, cnt in con.execute("SELECT vote_id, count(*) FROM vote_row GROUP BY 1"):
        pg, t = next((p, x) for p, x in vpages.items() if f"<code>{vid}</code>" in x)
        ns, na = seats_in(t, "hemi-vote")
        assert ns == cnt, f"{pg.name}: {ns} escaños dibujados, {cnt} filas"
        assert na >= cnt - 3, f"{pg.name}: {na} enlaces para {cnt} escaños"
        assert 'fill="var(--si)"' in t or 'fill="var(--no)"' in t, \
            f"{pg.name}: escaños sin color de voto"

    # ---- la leyenda que descifra el diagrama. The key used to separate the
    # states by colour alone: three of its four swatches lost their silhouette
    # to a `.key .sw` rule of equal specificity declared after them, and «con
    # licencia» and «presidió la sesión» were the same swatch twice (ΔE 10.3,
    # 1.06:1 in greyscale). Asserted on the generated HTML, per legend.
    def key_lists(t):
        return re.findall(r'<ul class="key">(.*?)</ul>', t, re.S)

    def silhouette(li):
        """What the swatch will actually be drawn as: the mark's geometry with
        the colour and the tooltip taken out."""
        sw = li.split("</svg>")[0]
        return re.sub(r"<title>.*?</title>", "",
                      re.sub(r'style="[^"]*"', "", sw))

    for pg, t in vpages.items():
        for ul in key_lists(t):
            sigs = []
            for li in re.findall(r"<li>(.*?)</li>", ul, re.S):
                if '<svg class="sw"' in li:
                    sigs.append(silhouette(li))
                else:   # a bench, named and logotyped; or the striped chip
                    assert 'class="blogo' in li or "sw-hatch" in li, \
                        f"{pg.name}: entrada de leyenda sin figura: {li[:90]}"
            assert len(sigs) == len(set(sigs)), \
                f"{pg.name}: dos entradas de una leyenda con la misma figura"
        # the caption promises greyscale legibility: it has to say at what
        # width. Only where the arc is coloured by the vote. The empty chamber
        # drawn for a roll call with no nominal list promises nothing.
        if 'id="hemi-vote"' in t and 'id="quien"' in t:
            assert "8 px" in t and "ilustración" in t, \
                f"{pg.name}: el pie promete blanco y negro sin decir a qué ancho"
        # ...and the CSS class of every swatch that still is a <span> exists
        for cls in re.findall(r'<span class="sw ([^"]+)"', t):
            for c in cls.split():
                assert re.search(rf"\.{c}\b[^{{]*\{{", CSS), \
                    f"{pg.name}: la clase .{c} no tiene regla en la hoja"

    # The figure in the key is the figure in the arc, state by state.
    for vid, in con.execute("SELECT DISTINCT vote_id FROM vote_row"):
        pg, t = next((p, x) for p, x in vpages.items() if f"<code>{vid}</code>" in x)
        flat = re.sub(r"<title>.*?</title>", "",
                      re.sub(r'style="[^"]*"', "", t))
        for k, in con.execute("SELECT DISTINCT position FROM vote_row "
                              "WHERE vote_id=?", (vid,)):
            assert silhouette(swatch("", pos_shape(k))) in flat, \
                f"{pg.name}: «{pos(k)[0]}» no lleva en la leyenda la figura de su escaño"

    # Every bar gets the treatment every other bar got: a rule on each internal
    # boundary and a title on each segment. The summary bar above the hemicycle
    # is the most prominent graphic on the page, and it was the one that had
    # neither.
    for pg, t in vpages.items():
        for j, b in enumerate(re.findall(r'<div class="bar"[^>]*>(.*?)</div>',
                                         t, re.S)):
            segs = re.findall(r"<i [^>]*>", b)
            assert all("title=" in s for s in segs), \
                f"{pg.name}: barra {j}, {sum('title=' in s for s in segs)} de "\
                f"{len(segs)} segmentos con título"
            assert sum("border-right" in s for s in segs) == max(len(segs) - 1, 0), \
                f"{pg.name}: barra {j} sin separador en cada frontera"

    # A member's own page draws their chamber with their seat marked, once.
    sen = con.execute("SELECT slug, chamber FROM legislator WHERE per_par>=2026 "
                      "LIMIT 1").fetchone()
    t = (OUT / "parlamentario" / f'{sen["slug"]}.html').read_text()
    ns, _na = seats_in(t, "hemi-seat")
    assert ns == LEGAL[sen["chamber"]], f'{sen["slug"]}: {ns} escaños en su cámara'
    assert t.split('id="hemi-seat"')[1].split("</svg>")[0].count("seat me") == 1, \
        f'{sen["slug"]}: su propio escaño no está marcado exactamente una vez'

    # ---- bench identity. Same colour, same logo, same name, everywhere.
    for p, (slot, mono, f) in BENCH.items():
        assert (OUT / "logos" / f).exists(), f"falta el logotipo de {p}"
        bp = OUT / "bancada" / f"{bench_slug(p)}.html"
        assert bp.exists(), f"{p} no tiene página de bancada"
        bt = bp.read_text()
        nm = con.execute("SELECT count(*) FROM legislator WHERE party=?",
                         (p,)).fetchone()[0]
        assert f"<h1>{esc(p)}</h1>" in bt
        assert bt.count("../parlamentario/") >= nm, \
            f"{p}: {bt.count('../parlamentario/')} enlaces para {nm} integrantes"
        # the three pages that lead with this bench actually show its logo. That
        # every page showing one reaches the credit is checked over every page
        # this build writes, further down: this used to be that claim's only
        # evidence, and it was eight pages out of 261.
        for page in (bt, (OUT / "parlamentarios.html").read_text(),
                     (OUT / "bancadas.html").read_text()):
            assert f"logos/{f}" in page and "acerca.html#logos" in page, \
                f"{p}: logotipo sin enlace a su atribución"
    ac = (OUT / "acerca.html").read_text()
    assert 'id="logos"' in ac and "creativecommons.org/licenses/by-sa" in ac, \
        "acerca.html no cumple la atribución CC BY-SA del logotipo de JP"
    assert "Txolo" in ac, "el autor del logotipo CC BY-SA no está nombrado"
    for p in BENCH:
        assert esc(p) in ac, f"{p} no figura en la tabla de créditos"
    # the colour class is fixed by name, not by insertion order
    assert gp("Fuerza Popular") == "gp3" and gp("Juntos por el Perú") == "gp1"
    assert gp("Un grupo que no existe") == "gp0"

    # ---- la paleta, medida contra la superficie sobre la que se pinta de
    # verdad: --ground, la página, y no --raised, la tarjeta. Con --raised el
    # hemiciclo pasaba con un 3.09:1 que sobre la página era 2.86:1.
    def _lum(h):
        v = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        v = [x / 12.92 if x <= .03928 else ((x + .055) / 1.055) ** 2.4 for x in v]
        return .2126 * v[0] + .7152 * v[1] + .0722 * v[2]

    def _cr(a, b):
        l1, l2 = _lum(a), _lum(b)
        return (max(l1, l2) + .05) / (min(l1, l2) + .05)

    themes = [tok for tok in
              (dict(re.findall(r"--([\w-]+): *(#[0-9A-Fa-f]{6})",
                               blk.split("}")[0]))
               for blk in CSS.split(":root")[1:]) if "ground" in tok]
    assert len(themes) == 3, f"{len(themes)} bloques de color, se esperaban 3"
    assert themes[1] == themes[2], "los dos bloques oscuros ya no coinciden"
    for tok in themes:
        for k in [f"gp{i}" for i in range(1, 7)] + ["off"]:
            for surf in ("ground", "raised"):
                assert _cr(tok[k], tok[surf]) >= 3, \
                    f'--{k} {tok[k]}: {_cr(tok[k], tok[surf]):.2f}:1 sobre --{surf}'
        # .pager .on: 13 px sobre el acento, en 275 páginas
        assert _cr(tok["accent"], tok["ground"]) >= 4.5, \
            f'--accent {tok["accent"]}: {_cr(tok["accent"], tok["ground"]):.2f}:1 '
        # Los colores de estado son relleno de escaño (3:1 bastaría) PERO
        # también son texto de 13 px en `.chip`, `.vote` y `.stg`, así que el
        # techo real es 4.5:1, y sobre las DOS superficies, porque la misma
        # ficha los pinta sobre la página y dentro de una tarjeta. Éste es el
        # límite que hizo fracasar el intento anterior de una escalera más
        # ancha, y por eso se mide en lugar de afirmarse.
        for k in ("si", "no", "abst", "blanco", "aus", "lic", "pres",
                  "ok", "wait", "dead", "sen", "vio", "gold", "muted"):
            for surf in ("ground", "raised"):
                assert _cr(tok[k], tok[surf]) >= 4.5, \
                    f'--{k} {tok[k]}: {_cr(tok[k], tok[surf]):.2f}:1 sobre ' \
                    f'--{surf}, y es texto de 13 px'
    assert "color:#fff" not in CSS, "texto blanco fijo: no se mide en los dos temas"
    # el peor par que se toca en el arco, en grises, en cada tema
    for tok, floor in zip(themes, (1.44, 1.25, 1.25)):
        worst = min(_cr(tok[f"gp{i}"], tok[f"gp{i + 1}"]) for i in range(1, 6))
        assert round(worst, 2) >= floor, \
            f"peor par contiguo en grises {worst:.2f}:1, el comentario dice {floor}"
    # y el comentario de la paleta no puede afirmar un escalón que la hoja no
    # tiene: sus dos filas se leen del propio fuente y se recalculan.
    src = pathlib.Path(__file__).read_text("utf-8")
    claim = re.search(r"#\s+light\s+([.\d ]+)\n#\s+dark\s+([.\d ]+)\n", src)
    assert claim, "la tabla de luminancias desapareció del comentario"
    for row, tok in zip(claim.groups(), themes[:2]):
        got = [round(_lum(tok[f"gp{i}"]), 3) for i in range(1, 7)]
        assert [float(x) for x in row.split()] == got, \
            f"el comentario dice {row.split()}; la hoja mide {got}"
    assert not re.search(r"0x[0-9A-F]{2}(\s*[·.]\s*0x[0-9A-F]{2})+", src), \
        "un comentario vuelve a recitar una escalera de escalones de luminancia"
    # y la excepción que se cita para los escaños es la que existe
    assert "2.5.8" in CSS and "Equivalent" in CSS, \
        "la nota de pointer-events no cita la excepción real de la 2.5.8"

    # ---- la tira de asistencia: cada toma una casilla, con su letra, y jamás
    # una licencia ni una presidencia pintadas como falta.
    for L in d2["legs"]:
        xs = d2["leg_att"].get(L["slug"], [])
        if not xs:
            continue
        t = (OUT / "parlamentario" / f'{L["slug"]}.html').read_text()
        cal = t.split('<div class="cal">')[1].split('<ul class="key"')[0]
        legend = t.split('<div class="cal">')[1].split("</figure>")[0]
        assert cal.count('class="cell"') == len(xs), \
            f'{L["slug"]}: {cal.count("class=\'cell\'")} casillas, {len(xs)} tomas'
        faltas = sum(1 for s, x in xs if att_state(d2, s, x)[1] == "falta")
        assert cal.count("<b>A</b>") == faltas, \
            f'{L["slug"]}: {cal.count("<b>A</b>")} casillas «A», {faltas} faltas reales'
        # colour is never the only channel
        for letter in ("P", "A", "L", "M"):
            if f"<b>{letter}</b>" in cal:
                assert ATT_CELL[
                    {"P": "presente", "A": "falta", "L": "excusa",
                     "M": "presidencia"}[letter]][1] in legend, \
                    f'{L["slug"]}: la casilla «{letter}» no está en la leyenda' 
        assert "asistencia/" in cal, f'{L["slug"]}: casillas sin enlace a la sesión'

    # ---- las tiras de comparación: solo ejes medidos, con su denominador.
    dip = con.execute("SELECT slug FROM legislator WHERE chamber='D' AND "
                      "per_par>=2026 LIMIT 1").fetchone()
    t = (OUT / "parlamentario" / f'{dip["slug"]}.html').read_text()
    blk = t.split("<h2>Dónde está en su cámara</h2>")[1].split("</section>")[0]
    assert blk.count('class="strip"') >= 2, "menos de dos ejes comparados"
    assert "mediana" in blk and "de 130 en" in blk, "falta la mediana o el rango"
    # Every axis has to be a thing we counted. The page says so out loud, and
    # each strip has to carry the denominator it was computed over.
    assert "publicamos un eje ideológico" in blk, \
        "la ficha no dice por qué no hay eje ideológico"
    for fig in blk.split('<figure class="strip"')[1:]:
        assert "de 130 en" in fig or "de 60 en" in fig, \
            "una tira comparativa sin su denominador"

    # ---- nada se corta a media palabra. Enseñar este sitio es mandar un
    # enlace, y un enlace cuya vista previa dice «…LA AMPLIACIÓN DE LA INFRA»
    # está roto. El <title> de 26% de las fichas de proyecto lo hacía.
    assert clip("uno dos tres", 8) == "uno dos…"
    assert clip("uno dos tres", 40) == "uno dos tres"
    assert clip("uno,  dos\ntres", 6) == "uno…", clip("uno,  dos\ntres", 6)
    assert clip("supercalifragilistico", 6) == "superc…"   # una sola palabra
    assert short_title("PL 1/2026", "") == "PL 1/2026"

    def whole_word(shown, full):
        """`shown` may be shorter than `full`, but never mid-word."""
        shown = html.unescape(shown).strip()
        full = " ".join(html.unescape(full).split())
        if not shown.endswith("…"):
            return True
        head = shown[:-1].rstrip()
        i = full.find(head)
        if i < 0:
            return False
        j = i + len(head)
        return j >= len(full) or not (head[-1:].isalnum() and full[j].isalnum())

    cut = 0
    for b in d2["bills"]:
        p = (OUT / "proyecto" / str(b["per_par"]) / (b["chamber"] or "C")
             / f'{b["ply_num"]}.html')
        ttl, h1 = titles[p], heads[p]
        assert whole_word(ttl, f'{b["code"]} · {b["title"] or ""}'), \
            f'{b["code"]}: <title> cortado a media palabra: {ttl!r}'
        assert whole_word(h1, b["title"] or ""), \
            f'{b["code"]}: h1 cortado a media palabra: {h1!r}'
        assert b["code"] in html.unescape(ttl), \
            f'{b["code"]}: el <title> no dice de qué expediente es'
        cut += ttl.endswith("…")
    for p, ttl in titles.items():
        assert "\n" not in ttl, f"{p}: un <title> con salto de línea"

    # ---- cada listado largo lleva su dispositivo gráfico. Las 311 páginas de
    # listado eran 250 filas idénticas sin un solo svg, img, barra, riel ni
    # monograma: el mismo fallo, «una tabla de 130 nombres no se puede leer»,
    # que este trabajo existe para arreglar, sobre la superficie más larga.
    assert len(lists) >= 100, f"solo {len(lists)} listados largos encontrados"
    for p, t, body in lists:
        assert 'class="gh"' in body or 'class="rail"' in t or 'class="bsec' in t, \
            f"{p.name}: {body.count('<li')} filas sin ninguna agrupación"
        assert ('<li class="r ' in body or "rowmark" in body
                or 'class="track"' in body), \
            f"{p.name}: filas sin marca por fila"
        assert ('<div class="bar"' in t or 'class="people"' in t
                or 'class="ranked"' in t or "<svg" in t), \
            f"{p.name}: listado sin ningún gráfico"
        # y ninguna repite un dato constante fila por fila
        for chip in re.findall(r'<span class="chip \w+"[^>]*>([^<]{12,})</span>',
                               body):
            rep = body.count(f">{chip}</span>")
            assert rep <= max(10, 0.45 * body.count("<li")), \
                f"{p.name}: «{chip.strip()[:40]}» repetido {rep} veces"

    # ---- la paleta de bancadas, medida en los QUINCE pares y no solo en los
    # cinco que se tocan en el arco. Misma aritmética que
    # `dataviz/scripts/validate_palette.js`: matrices de Machado (2009) sobre
    # RGB lineal y ΔE en OKLab ×100.
    #
    # El resultado, con `--pairs all`, es que NO pasa: 3.4 (protanopía, claro)
    # y 7.1 (oscuro), y la paleta de referencia del propio validador saca 3.2
    # en las mismas condiciones. Seis matices no se separan en quince pares; es
    # aritmética, no falta de oficio. Por eso el arco tiene desde ahora una
    # figura por bancada además del color, como la lista nominal tiene una por
    # sentido de voto, y lo que se afirma aquí es lo que se puede sostener:
    # contraste sobre la página, y una silueta distinta por bancada.
    MACHADO = {
        "protan": ((.152286, 1.052583, -.204868), (.114503, .786281, .099216),
                   (-.003882, -.048116, 1.051998)),
        "deutan": ((.367322, .860646, -.227968), (.280085, .672501, .047413),
                   (-.011820, .042940, .968881)),
        "tritan": ((1.255528, -.076749, -.178779), (-.078411, .930809, .147602),
                   (.004733, .691367, .303900)),
    }

    def _lin(h):
        return [x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4
                for x in (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))]

    def _oklab(v):
        l = (.4122214708 * v[0] + .5363325363 * v[1] + .0514459929 * v[2]) ** (1 / 3)
        m = (.2119034982 * v[0] + .6806995451 * v[1] + .1073969566 * v[2]) ** (1 / 3)
        s = (.0883024619 * v[0] + .2817188376 * v[1] + .6299787005 * v[2]) ** (1 / 3)
        return (.2104542553 * l + .7936177850 * m - .0040720468 * s,
                1.9779984951 * l - 2.4285922050 * m + .4505937099 * s,
                .0259040371 * l + .7827717662 * m - .8086757660 * s)

    def _sim(h, kind):
        v = _lin(h)
        return [max(0.0, min(1.0, sum(r[i] * v[i] for i in range(3))))
                for r in MACHADO[kind]]

    def dE(a, b, kind=None):
        x = _oklab(_sim(a, kind) if kind else _lin(a))
        y = _oklab(_sim(b, kind) if kind else _lin(b))
        return 100 * math.dist(x, y)

    pairs = [(i, j) for i in range(1, 7) for j in range(i + 1, 7)]
    assert len(pairs) == 15
    report = []
    for tok, mode in zip(themes, ("claro", "oscuro", "oscuro (toggle)")):
        gps = [tok[f"gp{i}"] for i in range(7) if i]
        worst_cvd = min(min(dE(gps[i - 1], gps[j - 1], k) for k in ("protan", "deutan"))
                        for i, j in pairs)
        worst_nor = min(dE(gps[i - 1], gps[j - 1]) for i, j in pairs)
        worst_grey = min(_cr(gps[i - 1], gps[j - 1]) for i, j in pairs)
        report.append((mode, worst_cvd, worst_nor, worst_grey))
        # lo que sí se sostiene: 3:1 contra la página, ya comprobado arriba, y
        # que ningún par sea literalmente el mismo color en los tres canales.
        assert worst_nor >= 5.0, f"{mode}: dos bancadas a ΔE {worst_nor:.1f}"
        assert worst_grey >= 1.0
    # y la figura, que es el canal que sí separa los quince pares
    shapes = [BENCH_SHAPE[i] for i in range(1, 7)]
    assert len(set(shapes)) == 6, "dos bancadas con la misma figura"
    for sh, _hollow in shapes:
        assert sh in SHAPE, f"figura {sh} sin dibujo"
    # y las seis cifras que el comentario de BENCH publica sobre los quince
    # pares se releen del propio fuente y se recalculan: la afirmación anterior
    # («every check PASS») sobrevivió porque nadie la volvía a medir.
    src2 = pathlib.Path(__file__).read_text("utf-8")
    claim15 = re.search(
        r"light\s+worst all-pairs CVD ΔE ([\d.]+), normal vision ([\d.]+)\n"
        r"#\s+dark\s+worst all-pairs CVD ΔE ([\d.]+), normal vision ([\d.]+)\n"
        r"#\s+greyscale, worst of fifteen: ([\d.]+):1 light, ([\d.]+):1 dark",
        src2)
    assert claim15, "desapareció la medición de los quince pares"
    got15 = [f"{report[0][1]:.1f}", f"{report[0][2]:.1f}",
             f"{report[1][1]:.1f}", f"{report[1][2]:.1f}",
             f"{report[0][3]:.2f}", f"{report[1][3]:.2f}"]
    assert list(claim15.groups()) == got15, \
        f"el comentario dice {claim15.groups()}; la hoja mide {got15}"
    # la leyenda de bancada dibuja la figura del escaño, en toda página que
    # pinte un hemiciclo de bancadas
    for f in ("index.html", "parlamentarios.html"):
        flat = re.sub(r"<title>.*?</title>", "",
                      re.sub(r'style="[^"]*"', "", (OUT / f).read_text()))
        for p6 in BENCH_ORDER:
            if bench_url("", p6) in flat:
                assert silhouette(swatch("", bench_shape(p6))) in flat, \
                    f"{f}: la leyenda de {p6} no lleva la figura de su escaño"

    # ---- la barra de navegación no puede esconder seis de sus ocho destinos
    # sin decirlo: el degradado `local` descubre la sombra del borde que
    # todavía tiene contenido detrás, y con ratón vuelve la barra de scroll.
    assert "no-repeat local" in CSS and "@media(pointer:fine)" in CSS, \
        "el scroller de la navegación se quedó sin señal de que hay más"
    # ...y el índice del teléfono: los ocho destinos, agrupados, sin JS y con
    # cada uno a un toque. Se comprueba en el HTML generado, no en NAV.
    dests = [u for _l, ds in NAV for u, _t, _d in ds]
    assert len(dests) == 8 and len(set(dests)) == 8
    nv = (OUT / "index.html").read_text().split("</nav>")[0]
    for u in dests:
        assert nv.count(f'href="{u}"') == 2, \
            f"{u} no aparece una vez en la fila y otra en el índice"
        assert (OUT / u).exists(), f"la navegación enlaza a {u}, que no existe"
    for label, _ds in NAV:
        assert f'class="mh">{esc(label)}' in nv, f"el índice perdió «{label}»"
    assert "<details class=\"menu\"><summary>" in nv, \
        "el índice del teléfono ya no es un <details> nativo"
    # los dos juegos de enlaces nunca están visibles a la vez
    assert "@media(max-width:899px){nav.top .links{display:none}}" in CSS \
        and "@media(min-width:900px){nav.top .menu{display:none}}" in CSS, \
        "la fila y el índice pueden mostrarse a la vez: destinos duplicados"
    # y la portada agrupa igual que la barra
    home0 = (OUT / "index.html").read_text().split("Empezar por aquí")[1] \
        .split("</section>")[0]
    for label, ds in NAV:
        assert esc(label) in home0, f"la portada no agrupa por «{label}»"
        for u, _t, _d in ds:
            assert f'href="{u}"' in home0, f"«Empezar por aquí» no lleva a {u}"

    # ---- una escala, no una dispersión
    used = sorted({float(x) for m in re.finditer(
        r"font(?:-size)?\s*:\s*([^;}\n]+)", CSS)
        for x in re.findall(r"(\d+(?:\.\d+)?)px", m.group(1))})
    assert set(used) <= set(TYPE_SCALE), \
        f"tamaños fuera de la escala: {sorted(set(used) - set(TYPE_SCALE))}"
    assert len(used) >= 7, "la escala se quedó sin pasos"

    # ---- los retratos son nuestros, con sus dos anchos y su texto alternativo
    withphoto = [L for L in d2["legs"] if L["photo_url"]]
    assert len(withphoto) >= 190, f"solo {len(withphoto)} con retrato"
    for L in withphoto:
        for w in PHOTO_W:
            assert (OUT / "photos" / f'{L["slug"]}-{w}.jpg').is_file(), \
                f'{L["slug"]}: falta el retrato de {w} px'
        t = (OUT / "parlamentario" / f'{L["slug"]}.html').read_text()
        assert f'photos/{L["slug"]}-400.jpg' in t and "srcset=" in t, \
            f'{L["slug"]}: su ficha no sirve el retrato local'
        assert f'alt="Retrato oficial de {esc(nice_name(L["full_name"]))}"' in t, \
            f'{L["slug"]}: retrato sin texto alternativo'
    big = max(p.stat().st_size for p in (OUT / "photos").iterdir())
    assert big < 300_000, f"un retrato de {big} bytes: no se reescaló"

    # ---- cada votación dibuja su hemiciclo, incluso la que no tiene lista
    # nominal: que el registro no diga cómo votó nadie es un hecho del
    # registro, y merece el mismo gráfico vacío.
    for p in (OUT / "votacion").glob("*.html"):
        assert 'id="hemi-vote"' in p.read_text(), f"{p.name}: votación sin hemiciclo"

    # ---- el crédito del logotipo va debajo de lo que califica, no entre el h1
    # y la entradilla, que es el segundo mejor sitio de la página.
    for p6 in BENCH_ORDER:
        bp = OUT / "bancada" / f"{bench_slug(p6)}.html"
        if not bp.exists():
            continue
        t = bp.read_text()
        head = t.split("<h1>")[1].split('<dl class="tiles"')[0]
        assert "Wikimedia" not in head and "licencia" not in head, \
            f"{p6}: el crédito del logotipo sigue encima del contenido"
        assert "Wikimedia" in t.split('<div class="prov">')[1], \
            f"{p6}: se perdió la obligación de atribuir"

    print("paleta de bancadas, los 15 pares: " + " · ".join(
        f"{m} CVD {a:.1f} / vista normal {b:.1f} / grises {c:.2f}:1"
        for m, a, b, c in report[:2]))
    print(f"títulos recortados en palabra entera: {cut} de {len(d2['bills'])}")

    # 2021 bills must reach people once the old roster lands; until then this
    # asserts the graph exists wherever the DB can support it.
    lk = con.execute(
        "SELECT b.per_par, b.chamber, b.ply_num, s.legislator_id FROM bill_sponsor s "
        "JOIN bill b ON b.id=s.bill_id WHERE s.legislator_id IS NOT NULL "
        "AND b.per_par<2026 LIMIT 1").fetchone()
    if lk:
        t = (OUT / "proyecto" / str(lk["per_par"]) / lk["chamber"]
             / f'{lk["ply_num"]}.html').read_text()
        assert "../../../parlamentario/" in t, "2021 bill page has no legislator link"
    con.close()
    print(f"ok: {sum(n.values()) - n['csv']} páginas, ninguna por debajo de 6 KB")


if __name__ == "__main__":
    demo()
