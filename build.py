"""Static site for the bicameral Congress (2026-2031).

`python3 build.py [db] [outdir]` -> writes site/ from data/congreso.db.
Stdlib only: sqlite3 + f-strings. No template engine, no framework, no npm.
Every page is a file on disk, so the site deploys anywhere and needs no runtime.
"""
import csv
import datetime as dt
import functools
import html
import io
import pathlib
import re
import shutil
import sys
import time
import unicodedata

from ingest import db

ROOT = pathlib.Path(__file__).resolve().parent
DBP = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else db.DB
OUT = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "site"

PORTAL = "https://wb2server.congreso.gob.pe/spley-portal/#"
MOCPORTAL = "https://wb2server.congreso.gob.pe/smociones-portal/#"
API = "https://api.congreso.gob.pe/spley-portal-service"
PER_PAGE = 250

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
        "Ingresó al registro oficial. Todavía no ha sido debatido por nadie: "
        "el siguiente paso es que el área de Trámite Documentario lo envíe a "
        "una comisión dictaminadora.",
        ["Decreto de envío a una o más comisiones.",
         "Estudio en comisión, que puede pedir opinión a ministerios y gremios."]),
    "EN COMISIÓN": ("COM",
        "Está en manos de una comisión, que debe estudiarlo y emitir un "
        "dictamen. La mayoría de proyectos se quedan aquí hasta que termina el "
        "periodo y son archivados.",
        ["Dictamen de la comisión (favorable, favorable sustitutorio o de archivo).",
         "Si hay dictamen favorable, pasa al Orden del Día.",
         "Si la comisión no dictamina, el proyecto caduca al final del periodo."]),
    "PASA A COMISIÓN": ("COM",
        "Fue derivado a una comisión para su estudio.",
        ["Dictamen de la comisión.", "Con dictamen favorable, pasa al Orden del Día."]),
    "RETORNA A COMISIÓN": ("COM",
        "El Pleno lo devolvió a comisión: el texto debatido no convenció y "
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
        "debate y en qué orden: es el filtro entre el dictamen y el Pleno.",
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
        "El Poder Ejecutivo observó la autógrafa: devolvió el texto al "
        "Congreso con reparos en lugar de promulgarlo.",
        ["Nuevo dictamen de la comisión sobre las observaciones.",
         "El Congreso puede allanarse a las observaciones o insistir; la "
         "insistencia requiere la mitad más uno del número legal."]),
    "ACUERDO DE COMISIÓN": ("COM",
        "La comisión adoptó un acuerdo sobre el proyecto —acumularlo, pedir "
        "opiniones, priorizarlo— sin que eso sea todavía un dictamen.",
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
        "Es ley: fue promulgada y publicada en el diario oficial El Peruano.",
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

    ponytail: no published state says "in the revising chamber" yet — the
    vocabulary is still the unicameral one — so the REV node can be reached but
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


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


def num(n):
    """14864 -> '14 864'. Peru writes thousands with a space, and a comma here
    would collide with the commas inside bill titles."""
    return f"{n:,}".replace(",", " ")


# ----------------------------------------------------------------------- CSS

CSS = """
:root{
  --ground:#F7F4EF; --raised:#FFFDFA; --ink:#171310; --muted:#6E655C;
  --line:#E2DAD0; --accent:#9E2B32; --ok:#2F6B62; --wait:#B07219; --dead:#8A8178;
  --sen:#2F4A6B; --dip:#9E2B32; --sunk:#F0EAE1;
  --gpa:#9E2B32; --gpb:#2F4A6B; --gpc:#6B4E9E; --gpd:#2F6B62; --gpe:#B07219; --gpf:#7A5C3A;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#14100E; --raised:#1E1917; --ink:#EDE7DF; --muted:#9E948A;
  --line:#332B27; --accent:#D4636A; --ok:#6FB3A4; --wait:#D9A046; --dead:#6E655C;
  --sen:#7FA3CC; --dip:#D4636A; --sunk:#191413;
  --gpa:#D4636A; --gpb:#7FA3CC; --gpc:#A98CD8; --gpd:#6FB3A4; --gpe:#D9A046; --gpf:#BFA07A;
}}
:root[data-theme="dark"]{
  --ground:#14100E; --raised:#1E1917; --ink:#EDE7DF; --muted:#9E948A;
  --line:#332B27; --accent:#D4636A; --ok:#6FB3A4; --wait:#D9A046; --dead:#6E655C;
  --sen:#7FA3CC; --dip:#D4636A; --sunk:#191413;
  --gpa:#D4636A; --gpb:#7FA3CC; --gpc:#A98CD8; --gpd:#6FB3A4; --gpe:#D9A046; --gpf:#BFA07A;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--ground);color:var(--ink);overflow-x:hidden;
  font:16px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none;border-bottom:1px solid var(--line)}
a:hover{border-bottom-color:var(--accent);color:var(--accent)}
h1,h2,h3{font-family:ui-serif,Georgia,"Times New Roman",serif;font-weight:600;
  text-wrap:balance;margin:0}
h1{font-size:clamp(24px,4vw,38px);letter-spacing:-.015em;line-height:1.18}
h2{font-size:19px;margin:0 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h3{font-size:16px}
p{margin:0 0 12px;max-width:70ch}
.wrap{max-width:1040px;margin:0 auto;padding:20px clamp(14px,3.5vw,28px) 64px}
.eyebrow{font:600 11px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;
  letter-spacing:.16em;text-transform:uppercase;color:var(--accent)}
.mut{color:var(--muted)}
.sm{font-size:13.5px}
nav.top{position:sticky;top:0;z-index:30;background:var(--raised);
  border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;align-items:center;
  gap:2px;padding:0 clamp(8px,3vw,20px)}
nav.top a,nav.top button{display:inline-flex;align-items:center;min-height:44px;
  padding:0 12px;border:0;background:none;color:inherit;font:600 13px/1 system-ui,sans-serif;
  cursor:pointer;border-bottom:2px solid transparent}
nav.top a:hover,nav.top button:hover{color:var(--accent);border-bottom-color:var(--accent)}
nav.top .brand{font-family:ui-serif,Georgia,serif;font-weight:600;font-size:15px;
  letter-spacing:-.01em;margin-right:8px}
nav.top .sp{margin-left:auto}
.crumb{font:12px/1.5 ui-monospace,Menlo,monospace;color:var(--muted);margin:18px 0 10px;
  word-break:break-word}
.crumb a{border:0}
.lede{font-size:17px;color:var(--ink);max-width:66ch;margin:14px 0 0}
section{margin:34px 0}
.card{background:var(--raised);border:1px solid var(--line);border-radius:2px;padding:18px 20px}
.grid{display:grid;gap:14px}
@media(min-width:760px){.g2{grid-template-columns:1fr 1fr}.g3{grid-template-columns:repeat(3,1fr)}}
.chip{display:inline-block;font:600 10.5px/1 ui-monospace,Menlo,monospace;letter-spacing:.1em;
  text-transform:uppercase;border:1px solid currentColor;border-radius:2px;padding:5px 7px;
  white-space:nowrap;vertical-align:middle}
.chip.d{color:var(--dip)} .chip.s{color:var(--sen)} .chip.c{color:var(--muted)}
.chip.ok{color:var(--ok)} .chip.wait{color:var(--wait)} .chip.dead{color:var(--dead)}
.chip.now{color:var(--accent)}
.gpa{color:var(--gpa)}.gpb{color:var(--gpb)}.gpc{color:var(--gpc)}
.gpd{color:var(--gpd)}.gpe{color:var(--gpe)}.gpf{color:var(--gpf)}.gpz{color:var(--muted)}
.dotmark{display:inline-block;width:9px;height:9px;border-radius:50%;background:currentColor;
  margin-right:6px;vertical-align:baseline}
.stat{display:flex;flex-wrap:wrap;gap:12px 30px;margin:0}
.stat div{min-width:104px}
.stat dt{font:600 10px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.13em;
  text-transform:uppercase;color:var(--muted)}
.stat dd{margin:2px 0 0;font:600 26px/1.1 ui-serif,Georgia,serif;font-variant-numeric:tabular-nums;
  letter-spacing:-.02em}
.stat dd small{font:400 12px/1.4 system-ui,sans-serif;color:var(--muted);display:block;
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
.track .name{font:600 15px/1.3 system-ui,sans-serif;display:block}
.track .why{display:block;font-size:13px;color:var(--muted);max-width:56ch;margin-top:2px}
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
.tl .doc{font:600 11px/1 ui-monospace,Menlo,monospace;letter-spacing:.08em;
  text-transform:uppercase;color:var(--accent);display:inline-block;margin-top:4px;
  padding:14px 0;min-height:44px}
.kv{margin:0;display:grid;gap:12px 24px}
@media(min-width:620px){.kv{grid-template-columns:1fr 1fr}}
.kv dt{font:600 10px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.13em;
  text-transform:uppercase;color:var(--muted)}
.kv dd{margin:2px 0 0}
.who{display:flex;gap:14px;align-items:flex-start}
.who img{width:64px;height:80px;object-fit:cover;border:1px solid var(--line);
  border-radius:2px;background:var(--sunk);flex:none}
.who .nm{font-family:ui-serif,Georgia,serif;font-size:18px;font-weight:600;display:block}
.roll{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:8px 10px}
.roll li{font-size:14px}
.roll li a{display:inline-block;padding:11px 0;min-height:44px}
.roll .rank{font:600 10px/1 ui-monospace,Menlo,monospace;color:var(--muted)}
.scroll{overflow:auto;max-height:78vh;border:1px solid var(--line);border-radius:2px;
  background:var(--raised);-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:14px}
.scroll table{min-width:560px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font:600 10px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.13em;text-transform:uppercase;
  color:var(--muted);position:sticky;top:0;background:var(--raised);z-index:2;white-space:nowrap}
th button{all:unset;cursor:pointer;display:block;width:100%;min-height:44px;
  line-height:44px;margin:-10px 0;font:inherit;color:inherit}
th button:hover{color:var(--accent)}
tbody tr:hover{background:var(--sunk)}
tr.grp td{background:var(--sunk);font:600 11px/1.4 ui-monospace,Menlo,monospace;
  letter-spacing:.12em;text-transform:uppercase;position:sticky;top:45px;z-index:1}
td.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px}
.filters input,.filters select{min-height:44px;padding:0 12px;font:14px system-ui,sans-serif;
  color:var(--ink);background:var(--raised);border:1px solid var(--line);border-radius:2px}
.filters input{flex:1 1 220px;min-width:0}
.bar{display:flex;height:14px;border-radius:2px;overflow:hidden;border:1px solid var(--line);
  background:var(--sunk)}
.bar i{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:8px;font-size:13px}
.prov{border-top:1px solid var(--line);margin-top:44px;padding-top:16px;color:var(--muted);
  font-size:13px}
.crumb a,.prov a{display:inline-block;padding:13px 0;line-height:18px}
#t td{height:44px;padding:0 12px}
td a{display:inline-block;min-height:44px;line-height:24px;padding:10px 0}
#t td a{display:block;line-height:44px;padding:0}
a.chip{min-height:44px;display:inline-flex;align-items:center}
.sign a{display:inline-block;min-height:44px;line-height:22px;padding:11px 0}
.who a.nm{min-height:44px;display:inline-flex;align-items:center}
.gloss{border:1px solid var(--line);border-radius:2px;margin:0 0 14px;background:var(--raised)}
.gloss summary{cursor:pointer;padding:12px 16px;min-height:44px;display:flex;
  align-items:center;font:600 13px/1.4 system-ui,sans-serif}
.gloss dl{padding:0 16px 16px;margin:0}
.gloss dd{font-size:13.5px;color:var(--muted)}
.prov b{color:var(--ink)}
.prov code{font-family:ui-monospace,Menlo,monospace;font-size:12px;word-break:break-all}
.pager{display:flex;flex-wrap:wrap;gap:6px;margin-top:18px}
.pager a,.pager span{min-height:44px;min-width:44px;display:inline-flex;align-items:center;
  justify-content:center;padding:0 10px;border:1px solid var(--line);border-radius:2px;
  font:600 13px/1 ui-monospace,Menlo,monospace}
.pager .on{background:var(--accent);color:#fff;border-color:var(--accent)}
.facets{display:flex;flex-wrap:wrap;gap:8px}
.facets a{border:1px solid var(--line);border-radius:2px;padding:10px 12px;min-height:44px;
  display:inline-flex;align-items:center;gap:8px;font-size:13.5px}
.facets a b{font-variant-numeric:tabular-nums}
.feed{list-style:none;margin:0;padding:0}
.feed li{padding:6px 0;border-bottom:1px solid var(--line)}
.feed li:last-child{border-bottom:0}
.feed .t{display:block;font-size:14.5px;padding:9px 0;min-height:44px}
.feed .m{font:600 11px/1.6 ui-monospace,Menlo,monospace;color:var(--muted);letter-spacing:.06em}
.note{border-left:3px solid var(--wait);padding:10px 0 10px 14px;color:var(--muted);
  font-size:13.5px;margin:14px 0}
.grid.cards{grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}
"""

# Shared by every list page: filter the rows already in the DOM, and accept the
# term from the URL so a party or district chip elsewhere can link straight to
# "the padrón, filtered to this".
FILTER_JS = """<script>
function flt(){var v=q.value.toLowerCase(),n=0;
[].forEach.call(ls.children,function(li){
var h=li.textContent.toLowerCase().indexOf(v)<0;li.style.display=h?"none":"";n+=h?0:1;});
if(window.cnt)cnt.textContent=n+" de "+ls.children.length+" en esta página.";
try{history.replaceState(null,"",v?"#q="+encodeURIComponent(q.value):"#");}catch(e){}}
q.oninput=flt;
var h0=decodeURIComponent(location.hash.replace(/^#(q=)?/,""));
if(h0){q.value=h0;flt();}
</script>"""

THEME = ("<script>(function(){var t=localStorage.getItem('tema');"
         "if(t)document.documentElement.setAttribute('data-theme',t);})()</script>")
TOGGLE = ("<button onclick=\"var d=document.documentElement,"
          "n=d.getAttribute('data-theme')==='dark'?'light':'dark';"
          "d.setAttribute('data-theme',n);localStorage.setItem('tema',n)\" "
          "aria-label=\"Cambiar tema claro u oscuro\">Tema</button>")


def shell(title, body, depth=0, desc=""):
    r = "../" * depth
    return f"""<!doctype html><html lang="es"><meta charset="utf-8">
<title>{esc(title)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{esc(desc)}">
<style>{CSS}</style>{THEME}
<nav class="top"><a class="brand" href="{r}index.html">Hemiciclo</a>
<a href="{r}proyectos.html">Proyectos</a>
<a href="{r}parlamentarios.html">Parlamentarios</a>
<a href="{r}votaciones.html">Votaciones</a>
<a href="{r}mociones.html">Mociones</a>
<span class="sp"></span>{TOGGLE}</nav>
<div class="wrap">{body}</div></html>"""


GP_CLASS = {}


def gp(party):
    """Stable colour class per grupo parlamentario."""
    if not party:
        return "gpz"
    if party not in GP_CLASS:
        GP_CLASS[party] = "gp" + "abcdef"[len(GP_CLASS) % 6]
    return GP_CLASS[party]


def party_chip(party, r=None):
    """Links to the padrón filtered to that bench when a path prefix is given."""
    if not party:
        return ""
    inner = f'<span class="dotmark"></span>{esc(party)}'
    if r is None:
        return f'<span class="chip {gp(party)}">{inner}</span>'
    return (f'<a class="chip {gp(party)}" '
            f'href="{r}parlamentarios.html#q={esc(party)}">{inner}</a>')


def district_chip(dist, r):
    if not dist:
        return ""
    return (f'<a class="chip c" href="{r}parlamentarios.html#q={esc(dist)}">'
            f'{esc(dist)}</a>')


def prov(lines):
    return ('<div class="prov"><b>Procedencia de los datos.</b> '
            + " ".join(l for l in lines if l) + "</div>")


# ---------------------------------------------------------------------- load

BILLREF = re.compile(r"(?:proyecto|pl|proposici[oó]n)[^0-9]{0,20}(\d{1,5})\s*[/-]\s*"
                     r"(\d{4})|(\d{4,5})\s*/\s*(\d{4})\s*-\s*[A-Z]{2}", re.I)


def subject_bill(d, v):
    """`vote.bill_id` is null on every roll call the Congress has published, so
    the only link left is the bill number written inside the asunto. Matches
    only an explicit `NNNN/YYYY` style reference — no fuzzy title matching,
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


def load(con):
    d = {}
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
    d["bill_cttes"], d["ctte_bills"] = {}, {}
    for r in con.execute("SELECT * FROM bill_committee"):
        c = d["cttes"].get(r["committee_id"])
        if c:
            d["bill_cttes"].setdefault(r["bill_id"], []).append(c)
            d["ctte_bills"].setdefault(c["id"], []).append(r["bill_id"])
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
    return d


# ---------------------------------------------------------------- bill pages

def bill_row(r, b, extra=""):
    stage = status_info(b["status"])[0]
    cls = {"LEY": "ok", "DEAD": "dead"}.get(stage, "wait")
    return (f'<li><span class="m">{esc(b["code"])} · {fecha(b["presented_on"])}'
            f'{extra}</span>'
            f'<a class="t" href="{bill_url(r, b)}">{esc((b["title"] or "")[:190])}</a>'
            f'<span class="chip {cls}" style="margin-top:6px">{esc(b["status"] or "—")}'
            f'</span></li>')


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
    ct_links = ", ".join(f'<a href="{r}comision/{esc(c["slug"])}.html">'
                         f'{esc(c["name"])}</a>' for c in cts)
    nodes = []
    for i, (k, label) in enumerate(track):
        if k == "COM" and cts:
            label = " · ".join(c["name"] for c in cts)
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
            f'<span class="why">{esc(STAGE_BLURB[k])}</span></li>')
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
        tl_note = ('<div class="note">Nuestra copia no tiene movimientos '
                   'registrados para este expediente: solo la fecha de '
                   'presentación y el estado vigente, ambos del listado oficial. '
                   'El expediente completo, enlazado al pie, es la fuente '
                   'autoritativa y puede contener más.</div>')

    # sponsors
    named = [s for s in sponsors if s["leg"]]
    primary = next((s for s in sponsors if (s["rank"] or 0) == 0), None)
    spon_html = ""
    if primary:
        L = primary["leg"]
        if L:
            spon_html = (
                f'<div class="who">'
                f'<img loading="lazy" src="{esc(L["photo_url"] or "")}" alt="">'
                f'<div><a class="nm" href="{leg_url(r, L["slug"])}">'
                f'{esc(nice_name(L["full_name"]))}</a>'
                f'<div class="sm mut">{esc(CHAMBER[L["chamber"]])} por '
                f'{esc(L["district"] or "circunscripción no registrada")}</div>'
                f'<div style="margin-top:8px">{party_chip(L["party"])}</div></div></div>')
        else:
            spon_html = (
                f'<div class="who"><div><span class="nm">'
                f'{esc(nice_name(primary["name_raw"]))}</span>'
                f'<div class="sm mut">Autor principal. No figura en el padrón '
                f'2026-2031, así que no tenemos su ficha: los proyectos del '
                f'periodo 2021-2026 fueron firmados por otro Congreso.</div>'
                f'</div></div>')
    co = [s for s in sponsors if s is not primary]
    co_html = ""
    if co:
        items = []
        for s in co:
            L = s["leg"]
            if L:
                items.append(f'<li><a href="{leg_url(r, L["slug"])}">'
                             f'{esc(nice_name(L["full_name"]))}</a> '
                             f'<span class="{gp(L["party"])}" title="{esc(L["party"])}">'
                             f'<span class="dotmark"></span></span></li>')
            else:
                items.append(f'<li>{esc(nice_name(s["name_raw"]))}</li>')
        split = {}
        for s in named:
            split[s["leg"]["party"]] = split.get(s["leg"]["party"], 0) + 1
        head = ""
        if split:
            head = ("<p class='sm mut'>" + " · ".join(
                f"{esc(k)} {v}" for k, v in sorted(split.items(), key=lambda x: -x[1]))
                + f" · {len(named)} de {len(sponsors)} autores identificados en el "
                  "padrón vigente.</p>")
        co_html = (f'<section><h2>Coautores ({len(co)})</h2>{head}'
                   f'<ul class="roll">{"".join(items)}</ul></section>')

    votes = d["bill_votes"].get(bid, [])
    votes_html = ""
    if votes:
        vr = []
        for v in votes:
            _t, _c = tallies(v, d["vrows"].get(v["id"], []))
            y, nn, aa = _t[0][2] if _t else (0, 0, 0)
            vr.append(
                f'<tr><td>{fecha(v["held_on"])}</td>'
                f'<td><a href="{vote_url(r, v)}">'
                f'{esc((v["subject"] or v["id"])[:110])}</a></td>'
                f'<td class="num">{y}</td><td class="num">{nn}</td>'
                f'<td class="num">{aa}</td>'
                f'<td>{esc(d["voutcome"][v["id"]])}</td></tr>')
        votes_html = (
            f'<section><h2>Votaciones nominales sobre este proyecto '
            f'({len(votes)})</h2><div class="scroll"><table><thead><tr>'
            f'<th>Fecha</th><th>Asunto</th><th>A favor</th><th>En contra</th>'
            f'<th>Abst.</th><th>Resultado</th></tr></thead><tbody>'
            + "".join(vr) + '</tbody></table></div>'
            '<p class="sm mut">Cada votación enlaza al detalle por bancada y a '
            'la lista completa de cómo votó cada parlamentario.</p></section>')

    # ponytail: the timeline already links every document at its own stage, so
    # a second flat list of the same 20 rows is noise. Only the count and the
    # honest note about there being no on-site text survive here.
    docs = [a for a in acts if a["doc_url"]]
    docs_html = ""
    if docs:
        docs_html = (
            f"<section><h2>Documentos del expediente ({len(docs)})</h2>"
            f"<p class='sm mut'>Cada documento está enlazado arriba, en la etapa "
            f"del trámite que lo produjo: así se ve de qué fase sale cada PDF. "
            f"El Congreso no publica el texto de los proyectos en HTML, solo "
            f"estos PDF en sus propios servidores, de modo que no podemos "
            f"ofrecer el texto en esta página ni compararlo entre versiones.</p>"
            f"</section>")

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
    head = full if len(full) <= 150 else full[:150].rsplit(" ", 1)[0] + "…"
    body = f"""
<div class="crumb"><a href="{r}index.html">Inicio</a> ›
<a href="{r}proyectos.html">Proyectos de ley</a> ›
<a href="{r}proyectos/p{b["per_par"]}-{ch}.html">{esc(CHAMBER[ch])} {b["per_par"]}</a> ›
{esc(b["code"])}</div>
<span class="eyebrow">Proyecto de ley {esc(b["code"])}</span>
<h1>{esc(head)}</h1>
<p class="lede"><span class="chip {cls}">{esc(b["status"] or "sin estado")}</span>
&nbsp;{esc(sentence)}</p>
<section><h2>Trámite legislativo</h2>{track_html}</section>
{nexts_html}
{summ}
<section><h2>Ficha</h2><dl class="kv">
<div><dt>Presentado</dt><dd>{fecha(b["presented_on"]) or "—"}</dd></div>
<div><dt>Cámara de origen</dt><dd>{esc(CHAMBER[ch])}</dd></div>
<div><dt>Proponente</dt><dd>{esc(b["proponent"] or "—")}</dd></div>
<div><dt>Periodo parlamentario</dt><dd>{b["per_par"]}-{b["per_par"] + 5}</dd></div>
<div><dt>Firmas</dt><dd>{len(sponsors)}</dd></div>
<div><dt>Estado publicado</dt><dd>{esc(b["status"] or "—")}</dd></div>
{f'<div><dt>{"Comisión dictaminadora" if len(cts) == 1 else "Comisiones"}</dt><dd>{ct_links}</dd></div>' if cts else ""}
</dl>
{f'<dl class="kv" style="margin-top:12px;grid-template-columns:1fr"><div><dt>Título oficial completo</dt><dd>{esc(full)}</dd></div></dl>' if head != full else ""}
</section>
{"<section><h2>Autor principal</h2>" + spon_html + "</section>" if spon_html else ""}
{co_html}
{votes_html}
<section><h2>Historial del expediente</h2>{tl_note}
<ul class="tl">{"".join(rows)}</ul></section>
{docs_html}
{prov([
    f'Registro de proyectos de ley del Congreso de la República, vía '
    f'<code>{API}/proyecto-ley</code>.',
    f'Expediente oficial: <a href="{esc(off)}">portal SPLey del Congreso ↗</a>.',
    f'Descargado el {fecha(b["fetched_at"])}' if b["fetched_at"] else "",
    'El Congreso actualiza el estado de un expediente el mismo día en que se '
    'registra el movimiento; esta copia se regenera cuando corre la ingesta, '
    'de modo que el retraso máximo es de una corrida.',
    f'Cita sugerida: «Proyecto de ley {esc(b["code"])}, Congreso de la '
    f'República del Perú, consultado el {fecha(dt.date.today().isoformat())}».',
])}
"""
    return shell(f'{b["code"]} · {(b["title"] or "")[:70]}', body, depth=3,
                 desc=sentence[:180])


# ---------------------------------------------------------- legislator pages

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
        lede = (f'Integró {esc(EN[L["chamber"]])} en el periodo {per}. '
                f'Esta ficha existe para que las firmas de aquel Congreso '
                f'enlacen a una persona.')
        thin = ('<div class="note">Ficha mínima. Del padrón 2021-2026 el '
                'Congreso solo mantiene publicado el nombre en el filtro de '
                'autores de la base de proyectos: bancada, circunscripción, '
                'foto y contacto no sobrevivieron al cambio de periodo, y no '
                'los inventamos. Lo que sí es completo es su rastro '
                'legislativo, abajo.</div>')
    all_bills = sorted(d["leg_bills"].get(slug, []),
                       key=lambda x: (d["bill_by_id"][x[0]]["presented_on"] or ""),
                       reverse=True)
    # Twenty-two of the 190 also sat in the 2021-2026 unicameral Congress and
    # their old bills match by name. Counting those as work of this period
    # would put a senator at 530 against a chamber median of 0.
    mine = [x for x in all_bills if d["bill_by_id"][x[0]]["per_par"] >= 2026]
    past = [x for x in all_bills if d["bill_by_id"][x[0]]["per_par"] < 2026]
    prim = [x for x in mine if (x[1] or 0) == 0]
    mots = d["leg_motions"].get(slug, [])
    vts = d["leg_votes"].get(slug, [])

    def baseline(peers, unit):
        return (f"<small>mediana de la cámara {median(peers)} · "
                f"máximo {max(peers)} {unit}</small>")

    stats = (f'<dl class="stat">'
             f'<div><dt>Proyectos firmados</dt><dd>{len(mine)}'
             f'{baseline(base["bills"][L["chamber"]], "proyectos")}</dd></div>'
             f'<div><dt>Como autor principal</dt><dd>{len(prim)}'
             f'{baseline(base["prim"][L["chamber"]], "proyectos")}</dd></div>'
             f'<div><dt>Mociones firmadas</dt><dd>{len(mots)}'
             f'{baseline(base["mots"][L["chamber"]], "mociones")}</dd></div>')
    # A presence rate is only honest over roll calls the member could have
    # voted in. Chairing the session and holding a licencia are not absences —
    # publishing them as 0% attendance is a falsehood about a named person.
    votable = [(vv, p) for vv, p in vts if pos(p)[1] in ("voto", "falta")]
    excused = [(vv, p) for vv, p in vts if p == "LICENCIA"]
    chaired = [(vv, p) for vv, p in vts if p == "PRESIDENCIA"]
    other_ex = [(vv, p) for vv, p in vts
                if pos(p)[1] == "excusa" and p not in ("LICENCIA", "PRESIDENCIA")]
    if votable:
        present = sum(1 for _, p in votable if pos(p)[1] == "voto")
        pct = round(100 * present / len(votable))
        peers = base["asis"][L["chamber"]]
        stats += (f'<div><dt>Emitió voto</dt><dd>{present} de {len(votable)}'
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
        stats += (f'<div><dt>Emitió voto</dt><dd>—<small>en las {len(vts)} '
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
        "<p class='sm mut'>Cruzados por nombre con el registro del Congreso "
        "unicameral anterior. No cuentan en las cifras de arriba, que miden "
        "solo el periodo 2026-2031.</p>")
    ml = ""
    if mots:
        by_id = {m["id"]: m for m in d["motions"]}
        ml = (f'<section><h2>Mociones firmadas ({len(mots)})</h2><ul class="feed">'
              + "".join(
                  f'<li><span class="m">{esc(by_id[m]["code"])} · '
                  f'{fecha(by_id[m]["presented_on"])}'
                  f'{" · promotor" if (rk or 0) == 0 else ""}</span>'
                  f'<a class="t" href="{r}mociones.html#m-{esc(m)}">'
                  f'{esc((by_id[m]["summary"] or "").strip()[:170])}</a></li>'
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
                f'<tr><td>{fecha(vv["held_on"])}</td>'
                f'<td><a href="{vote_url(r, vv)}">'
                f'{esc((vv["subject"] or vv["id"])[:120])}</a>'
                + (f' <span class="chip {tone}">{esc(lab)}</span>' if lab else "")
                + f'</td><td>{esc(pos(p)[0])}'
                + (' <b>(rompió con su bancada)</b>' if odd else "")
                + f'</td><td>{esc(out)}</td></tr>')
        vl = ('<section><h2>Votaciones</h2><div class="scroll"><table>'
              '<thead><tr><th>Fecha</th><th>Asunto</th><th>Su voto</th>'
              '<th>Resultado</th></tr></thead><tbody>' + "".join(vrows)
              + "</tbody></table></div>"
              + ('<p class="sm mut">En las votaciones marcadas, las fuentes '
                 'oficiales no coinciden entre sí y la fila de esta persona '
                 'proviene de nuestra lectura del PDF. La votación enlazada '
                 'explica la discrepancia.</p>' if caveat else "")
              + (f'<p class="sm mut">Se apartó de la posición mayoritaria de '
                 f'{esc(L["party"] or "su grupo")} en '
                 f'{len(broke)} de las {len(vts)} votaciones publicadas.</p>'
                 if broke else
                 f'<p class="sm mut">Votó siempre con la posición mayoritaria de '
                 f'{esc(L["party"] or "su grupo")} en las votaciones publicadas.</p>'
                 if any(p in ("SI", "NO", "ABST") for _, p in vts) else "")
              + "</section>")

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
            + ("<p class='sm mut'>Si vive en "
               f"{esc(L['district'] or 'su circunscripción')}, usted es su "
               "representado y su oficina atiende pedidos de constituyentes; si "
               "no, puede escribirle igual, pero la prioridad es de quienes "
               "representa.</p>" if L["email"] else "")
            + "<p class='sm mut'><b>Este sitio no es el Congreso.</b> Es un "
              "registro independiente construido a partir de los datos que el "
              "Congreso publica. No transmitimos mensajes ni gestionamos "
              "trámites: escríbale por los canales oficiales de arriba.</p>"
            + "</section>")
    elif L["source_url"]:
        contact_html = (
            "<section><h2>Contacto</h2><p>El portal "
            f"{esc(CHAMBER[L['chamber']])} no publica un correo para esta "
            f"persona; su <a href=\"{esc(L['source_url'])}\">ficha oficial ↗</a> "
            "es el único canal directo que existe.</p>"
            "<p class='sm mut'><b>Este sitio no es el Congreso.</b> Es un "
            "registro independiente construido a partir de los datos que el "
            "Congreso publica.</p></section>")
    bio = f'<section><h2>Reseña</h2><p>{esc(L["bio"])}</p></section>' if L["bio"] else ""

    body = f"""
<div class="crumb"><a href="{r}index.html">Inicio</a> ›
<a href="{r}parlamentarios.html">Parlamentarios</a> ›
{esc(CHAMBER[L["chamber"]])}</div>
<span class="eyebrow">{esc(CHAMBER[L["chamber"]])} · {per}</span>
<h1>{esc(nice_name(L["full_name"]))}</h1>
<div class="who" style="margin-top:18px">
{f'<img loading="lazy" src="{esc(L["photo_url"])}" alt="" style="width:96px;height:120px">' if L["photo_url"] else ""}
<div><p class="lede" style="margin-top:0">{lede}</p>
<div>{party_chip(L["party"], r)} {district_chip(L["district"], r)}
<span class="chip {L["chamber"].lower()}">{esc(CHAMBER_SHORT[L["chamber"]])}</span>
</div></div></div>
{thin}
<section><h2>Actividad, comparada con su cámara</h2>{stats}
<p class="sm mut">Las medianas se calculan sobre los
{len(base["bills"][L["chamber"]])} integrantes de la cámara, no sobre una
muestra. El periodo {per} corre hasta el 26 de julio de {L["per_par"] + 5}; la
próxima elección general es en abril de {L["per_par"] + 5}.</p></section>
{contact_html}
{bio}
{bl}
{ml}
{vl}
{prov([
    f'Ficha, foto, bancada y circunscripción: portal de la '
    f'{esc(CHAMBER[L["chamber"]])}'
    + (f' (<a href="{esc(L["source_url"])}">ficha oficial ↗</a>)' if L["source_url"] else "")
    + ', vía su API REST pública.',
    f'Firmas de proyectos y mociones: <code>{API}</code>. El Congreso no publica '
    'un identificador compartido entre el padrón y la base de proyectos, así que '
    'el cruce se hace por nombre normalizado.',
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
# absence — the default for anything unrecognised is "no le tocaba votar", never
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
}


def pos(p):
    """Never KeyErrors and never turns an unseen state into an absence."""
    return POS.get(p, (p or "Sin dato", "excusa", 9,
                       "Estado que el acta publica y que todavía no hemos "
                       "traducido. No lo contamos como inasistencia."))


POS_LABEL = {k: v[0] for k, v in POS.items()}

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
        out.append(("acta", "Tally impreso en el acta",
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
    ptable = ""
    if parties:
        head = sorted(POS, key=lambda k: POS[k][2])
        head = [h for h in head if any(h in c for c in parties.values())]
        def agree(p):
            a, t = d["gp_agree"].get((ch, p), (0, 0))
            return f"{a} de {t}" if t > 1 else "—"

        ptable = ('<div class="scroll"><table><thead><tr><th>Grupo parlamentario</th>'
                  + "".join(f'<th>{esc(pos(h)[0])}</th>' for h in head)
                  + '<th>Total</th><th>% a favor</th>'
                  '<th>Con la mayoría</th></tr></thead><tbody>'
                  + "".join(
                      f'<tr><td><span class="{gp(p)}"><span class="dotmark"></span></span>'
                      f'<a href="{r}parlamentarios.html#q={esc(p)}">{esc(p)}</a></td>'
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
                  '<p class="sm mut">«Con la mayoría» cuenta en cuántas de las '
                  'votaciones nominales publicadas por esta cámara la posición '
                  'mayoritaria del grupo coincidió con la de la cámara.</p>'
                  + ('<p class="sm mut">Ojo: este desglose se calcula sobre '
                     'nuestra lectura de la lista nominal, que no cuadra con la '
                     f'cifra del encabezado. {flag}</p>'
                     if not roster_ok else ""))

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
                    '<p>Nadie: en esta votación los '
                    f'{len([p for p, c in parties.items() if sum(c.values()) >= 3])} '
                    'grupos parlamentarios con tres o más presentes votaron en '
                    'bloque, sin una sola deserción.</p></section>')
    elif outliers:
        out_html = ('<section><h2>Votaron distinto a su bancada</h2>'
                    '<p class="sm mut">Comparado con la posición mayoritaria de su '
                    'propio grupo en esta misma votación. Los grupos de menos de tres '
                    'integrantes presentes se excluyen porque no tienen mayoría '
                    'interna que romper.</p><ul class="feed">' + "".join(
                        f'<li><span class="m">{esc(p)} votó mayoritariamente '
                        f'{esc(pos(top)[0])}</span>'
                        + (f'<a class="t" href="{leg_url(r, x["leg"]["slug"])}">'
                           f'{esc(nice_name(x["leg"]["full_name"]))}</a>'
                           if x["leg"] else
                           f'<span class="t">{esc(nice_name(x["name_raw"]))}</span>')
                        + f' — {esc(pos(x["position"])[0])}</li>'
                        for x, p, top in outliers) + "</ul></section>")

    # Every word that appears in the bar, the stat block, the group table and
    # the filter dropdown, defined once where the reader meets them.
    gloss = ('<details class="gloss" open><summary>Qué significa cada categoría '
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
        pres = ('<p class="sm mut">Quien preside no figura con voto: los propios '
                'reportes advierten que «en este reporte de votación no se '
                'considera al congresista que ejerce la presidencia». '
                + (f'En esta sesión presidió {link}. ' if who else "")
                + f'Por eso {nn} '
                + ("integrante aparece" if nn == 1 else "integrantes aparecen")
                + ' sin sentido de voto, y no lo contamos como inasistencia ni '
                  'en esta página ni en su ficha personal.</p>')

    # roster, rendered client-side so sort/filter can live in the URL
    # "Apellidos, Nombre": the order the chambers publish and the only one
    # where sorting the column alphabetically means anything.
    order = sorted(POS, key=lambda k: POS[k][2])
    data = ",".join(
        "[{},{},{},{},{},{},{}]".format(
            _js(x["leg"]["full_name"] if x["leg"] else x["name_raw"]),
            _js(x["leg"]["slug"] if x["leg"] else ""),
            _js(row_party(x)),
            _js((x["leg"]["district"] if x["leg"] else "") or "—"),
            _js(pos(x["position"])[0]),
            order.index(x["position"]) if x["position"] in order else 9,
            _js(SRC[x["source"]][0] if x["source"] in SRC else ""))
        for x in rows)
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
            cell = (f'<a href="{leg_url(r, x["leg"]["slug"])}">{esc(nm)}</a>'
                    if x["leg"] else esc(nm))
            srv.append(
                f'<tr><td>{esc(pos(x["position"])[0])}'
                + (f' <span class="chip wait" title="{esc(SRC[x["source"]][1])}">'
                   f'{esc(SRC[x["source"]][0])}</span>'
                   if x["source"] in SRC else "")
                + f'</td><td>{cell}</td><td>{esc(p)}</td>'
                f'<td>{esc((x["leg"]["district"] if x["leg"] else "") or "—")}</td></tr>')
        roster = f"""<section><h2>Cómo votó cada quien ({len(rows)})</h2>
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
<p class="sm mut" id="cnt">{len(rows)} parlamentarios, ordenados por grupo
parlamentario. Pulse una cabecera para reordenar.</p>
<script>
var D=[{data}],S={{c:2,d:1,q:"",gp:"",v:""}},P=new URLSearchParams(location.hash.slice(1));
S.c=+(P.get("s")||2);S.d=+(P.get("d")||1);S.q=P.get("q")||"";S.gp=P.get("gp")||"";
S.v=P.get("v")||"";
q.value=S.q;fgp.value=S.gp;fv.value=S.v;
function esc(s){{return String(s).replace(/[&<>"]/g,function(c){{
 return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[c];}});}}
function draw(){{
 var q1=S.q.toLowerCase();
 var rs=D.filter(function(x){{
  return (!q1||(x[0]+" "+x[3]+" "+x[2]).toLowerCase().indexOf(q1)>=0)
   &&(!S.gp||x[2]===S.gp)&&(!S.v||x[4]===S.v);}});
 rs.sort(function(a,b){{
  var k=S.c===4?5:S.c,r;
  r=k===5?(a[5]-b[5])*S.d:String(a[k]).localeCompare(String(b[k]),"es")*S.d;
  return r||String(a[0]).localeCompare(String(b[0]),"es");}});
 var h="",g=null;
 rs.forEach(function(x){{
  if((S.c===2||S.c===4)&&x[S.c]!==g){{g=x[S.c];
   h+='<tr class="grp"><td colspan="4">'+esc(g)+'</td></tr>';}}
  h+="<tr><td>"+esc(x[4])+(x[6]?' <span class="chip wait">'+esc(x[6])+"</span>":"")
   +"</td><td>"+(x[1]
   ?'<a href="{r}parlamentario/'+x[1]+'.html">'+esc(x[0])+"</a>":esc(x[0]))
   +"</td><td>"+esc(x[2])+"</td><td>"+esc(x[3])+"</td></tr>";}});
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
            f'{esc(bill["code"])}</a>: {esc((bill["title"] or "")[:160])}.'
            + ("" if v["bill_id"] else
               ' <span class="sm mut">Vínculo deducido del número de proyecto '
               'citado en el asunto: el acta no trae un campo que lo '
               'identifique.</span>') + '</p>')
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
        nxt = ('<section><h2>Qué pasa después</h2><p>Nada pendiente en el '
               'registro: el Diario de los Debates ya confirmó este conteo, así '
               'que la cifra de esta página es la definitiva.</p></section>')
    elif resolved:
        nxt = ('<section><h2>Qué pasa después</h2><p>La corrección leída en sala '
               'ya está aplicada en esta página. Falta el acta definitiva que la '
               'Oficialía Mayor publica semanas después: cuando salga, '
               'compararemos ambas y diremos si cambia algo.</p></section>')
    elif v["provisional"] or (rows and not v["parsed"]):
        nxt = ('<section><h2>Qué pasa después</h2><p>El acta que sirve de fuente '
               'es provisional. La Oficialía Mayor publica después el resultado '
               'definitivo, con los votos orales incorporados; cuando aparezca, '
               'esta página mostrará ambas cifras y cuál reemplaza a cuál.</p>'
               '</section>')
    legal = LEGAL.get(ch, 130)
    need = emitted // 2 + 1
    ok = yes >= need and yes > no
    outcome = (v["result"] or
               ("Aprobada por mayoría simple" if ok else "No alcanzó la mayoría simple"))
    derived = "" if v["result"] else (
        '<p class="sm mut">El acta de esta sesión no imprime un veredicto en '
        'texto: publica la lista de votos y nada más. El resultado de arriba lo '
        'deducimos del conteo, no lo copiamos del documento.</p>')
    thr = (f'<section><h2>Qué se necesitaba para aprobar</h2>'
           f'<p>Una votación ordinaria se aprueba por <b>mayoría simple</b>: más votos '
           f'a favor que en contra, sobre el quórum ya verificado de la sesión. '
           f'Se emitieron {emitted} votos, así que el umbral estaba en <b>{need}</b> '
           f'y hubo <b>{yes}</b>. {"Se superó." if ok else "No se superó."}</p>'
           f'{derived}'
           f'<p class="sm mut">Como referencia: las materias que el Reglamento sujeta '
           f'a la mayoría del número legal exigen {legal // 2 + 1} votos sobre los '
           f'{legal} escaños {DE[ch]}. Cuál de las dos reglas invocó '
           f'la Mesa no consta en el acta como dato estructurado, y no lo '
           f'inventamos.</p></section>')

    bar = ""
    if total:
        segs = [("SI", yes, "var(--ok)"), ("NO", no, "var(--accent)"),
                ("ABST", ab, "var(--wait)"), ("BLANCO", blank, "var(--gpf)"),
                ("LICENCIA", lic, "var(--gpc)"),
                ("AUSENTE", aus, "var(--dead)"),
                ("PRESIDENCIA", chair, "var(--sen)"),
                ("OTRO", other - chair, "var(--muted)")]
        extra = ([("Sin reconciliar", unrec, "repeating-linear-gradient(45deg,"
                   "var(--dead) 0 4px,transparent 4px 8px)")] if unrec else [])
        bar = ('<div class="bar">' + "".join(
            f'<i style="width:{100 * n / total:.1f}%;background:{c}"></i>'
            for _, n, c in segs + extra if n) + "</div><div class='legend'>" + "".join(
            f'<span style="color:{c}"><span class="dotmark"></span>'
            f'{esc(pos(k)[0])} {n} ({pc(n, total)})</span>'
            for k, n, c in segs if n)
            + (f'<span class="mut"><span class="dotmark"></span>Sin reconciliar '
               f'{unrec} ({pc(unrec, total)})</span>' if unrec else "")
            + "</div>")

    # Sources disagree often. Rather than picking one and hiding the rest, the
    # page prints all of them side by side and says which one it is using.
    warn = ""
    if disputed or v["provisional"] or (rows and not v["parsed"]):
        trows = "".join(
            f'<tr><td>{esc(lab)}{" — <b>la que usamos arriba</b>" if k == kind else ""}'
            + (f' <a href="{esc(u)}">documento ↗</a>' if u else "") + "</td>"
            + "".join(f'<td class="num">{n}</td>' for n in t)
            + f'<td class="num">{sum(t)}</td></tr>'
            for k, lab, t, u in tal)
        lead = (
            '<b>El Diario de los Debates confirma el conteo del tablero.</b> '
            'La versión definitiva del registro coincide con el acta '
            'electrónica: nada cambió entre una y otra. Las cifras, para que '
            'pueda comprobarlo:'
            if _k == "confirmado" else
            '<b>El resultado de esta votación se corrigió en la propia sesión.</b> '
            'El tablero electrónico no recoge los votos que se emiten de viva '
            'voz; la presidencia los suma y rectifica el conteo en el acto. '
            'Mostramos la cifra corregida y dejamos a la vista de dónde sale:'
            if resolved else
            '<b>Las fuentes de esta votación no coinciden.</b> El Congreso '
            'publica primero un acta electrónica provisional y después el '
            'resultado corregido con los votos orales; nuestra lectura del PDF '
            'es una tercera cifra. Están las tres:')
        warn = (
            f'<div class="note" id="lectura">{lead}'
            f'<div class="scroll" style="margin:12px 0"><table><thead><tr>'
            f'<th>Fuente</th><th>A favor</th><th>En contra</th><th>Abstención</th>'
            f'<th>Suma</th></tr></thead><tbody>{trows}</tbody></table></div>'
            + (f'<p>{esc(v["parse_note"])}</p>' if v["parse_note"] else "")
            + (f'<p>{unrec} parlamentarios de los {total} de la lista no quedan '
               f'explicados por la cifra que mostramos arriba; aparecen como «sin '
               f'reconciliar» en la barra en vez de desaparecer del total.</p>'
               if unrec else "")
            + ('<p>El desglose por bancada y la lista nominal de más abajo '
               'coinciden con la cifra corregida: los votos orales están '
               'incorporados fila por fila y marcados como tales.</p>'
               if roster_ok else
               '<p>El desglose por bancada y la lista nominal de más abajo '
               'salen de nuestra lectura del PDF, no de la cifra del '
               'encabezado: contrástelos con el documento original antes de '
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
        peers = (f'<p class="sm mut">Para comparar: {esc(CHAMBER[ch])} lleva '
                 f'{len(st["margin"])} votaciones nominales publicadas. El '
                 f'{marg}% a favor de esta es el {rank}.º margen más amplio de '
                 f'las {len(st["margin"])}; la mediana es {median(st["margin"])}%. '
                 f'Votaron {pres_pct}% de los presentes en la lista, frente a una '
                 f'mediana de {median(st["presence"])}%.</p>')

    # A show-of-hands vote has a result and no numbers at all. Printing
    # "0 a favor" would be a lie; the honest page says the count does not exist.
    no_count = not rows and v["n_yes"] is None
    if no_count:
        lede_tail = ('. No hay conteo nominal: la votación no pasó por el '
                     'sistema electrónico, de modo que no existe registro de '
                     'cómo votó cada quien.')
        lede_src = ("Así consta en el acta de la sesión."
                    if not v["parse_note"] else esc(v["parse_note"]))
        result_block = (
            '<section><h2>Resultado</h2><p>El acta consigna el sentido del '
            'acuerdo pero ningún número: no hubo votación nominal que '
            'registrar. Esta página no puede decirle cómo votó su '
            'parlamentario en esta ocasión, porque el Congreso tampoco lo '
            'publicó.</p>'
            + (f'<p class="sm mut">{esc(v["parse_note"])}</p>'
               if v["parse_note"] else "") + f'{pres}</section>')
    else:
        lede_tail = (f': {yes} a favor, {no} en contra, {ab} abstenciones'
                     + (f' y {novote} sin votar' if novote else "")
                     + f', sobre los {legal} escaños {esc(DE[ch])}.')
        lede_src = f"Cifra tomada de: {esc(kind_label.lower())}."
        result_block = f"""<section><h2>Resultado</h2>{bar}
<dl class="stat" style="margin-top:18px">
<div><dt>A favor</dt><dd>{yes}<small>{pc(yes, emitted)} de los votos emitidos</small></dd></div>
<div><dt>En contra</dt><dd>{no}<small>{pc(no, emitted)} de los votos emitidos</small></dd></div>
<div><dt>Abstenciones</dt><dd>{ab}<small>{pc(ab, emitted)} de los votos emitidos</small></dd></div>
<div><dt>Sin votar</dt><dd>{novote}<small>{pc(novote, legal)} del número legal
{f"· {lic} con licencia" if lic else ""}</small></dd></div>
</dl>
{peers}
{pres}</section>
{thr}"""

    body = f"""
<div class="crumb"><a href="{r}index.html">Inicio</a> ›
<a href="{r}votaciones.html">Votaciones</a> ›
<a href="{r}votaciones.html#q={esc(v["held_on"] or "")}">{esc(CHAMBER_SHORT[ch])}
{fecha(v["held_on"])}</a></div>
<span class="eyebrow">{esc(CHAMBER[ch])} · votación nominal</span>
<h1>{esc(v["subject"] or "Votación sin asunto registrado")}</h1>
<p class="lede">{fecha(v["held_on"])}. <b>{esc(outcome)}</b>{lede_tail} {flag}<br>
<span class="sm mut">{lede_src}</span></p>
{warn}
{billlink}
{result_block}
{nxt}
{"<section><h2>Por grupo parlamentario</h2>" + ptable + "</section>" if ptable else ""}
{out_html}
{roster}
<section><h2>Descarga y cita</h2>
{f'<p><a href="{esc(v["slug"])}.csv">Descargar esta votación en CSV</a> — una fila por parlamentario, con su identificador estable, nombre, grupo, circunscripción, sentido del voto y el estado de fiabilidad de la lectura.</p>' if rows else '<p>No hay CSV que descargar: sin votación nominal no hay filas que exportar.</p>'}
<p class="sm mut">Cita sugerida: «Votación nominal {esc(CHAMBER[ch])},
{fecha(v["held_on"])}: {esc((v["subject"] or "")[:80])}», consultada el
{fecha(dt.date.today().isoformat())}.</p></section>
{prov([
    f'Acta publicada por {esc(CHAMBER[ch])}: '
    f'<a href="{esc(v["source_url"])}">documento original ↗</a>'
    f' ({esc(v["source_kind"] or "documento")}).',
    (f'Resultado corregido: <a href="{esc(v["final_source_url"])}">documento '
     f'definitivo ↗</a>.') if v["final_source_url"] else "",
    f'Descargado el {fecha(v["fetched_at"])}.' if v["fetched_at"] else "",
    esc(v["parse_note"] or ""),
    'El Congreso publica el acta electrónica el mismo día y la versión '
    'definitiva después; esta copia se regenera en cada corrida de la ingesta.',
    f'Identificador interno de esta votación: <code>{esc(v["id"])}</code>.',
])}
"""
    return shell(f'{(v["subject"] or v["id"])[:70]} · votación', body, depth=1,
                 desc=f'Votación nominal en {CHAMBER[ch]}, {fecha(v["held_on"])}.')


def _js(s):
    return '"' + str(s or "").replace("\\", "\\\\").replace('"', '\\"') \
        .replace("<", "\\u003c").replace("\n", " ") + '"'


def vote_csv(d, v):
    """One row per legislator, with a stable id to join on and the reliability
    of the reading carried in the data — a download that drops the dispute the
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


# --------------------------------------------------------------- list pages

def paginate(name, title, intro, rows_html, depth, per=PER_PAGE, prov_lines=()):
    """Write name.html, name-2.html … Each page carries a filter box over its
    own rows. ponytail: no server, so the filter is per page, not per corpus;
    a real cross-corpus search needs a JSON index and fetch()."""
    pages = [rows_html[i:i + per] for i in range(0, len(rows_html), per)] or [[]]
    out = []
    for n, chunk in enumerate(pages, 1):
        nav = ""
        if len(pages) > 1:
            nav = '<div class="pager">' + "".join(
                f'<span class="on">{i}</span>' if i == n else
                f'<a href="{name}{"" if i == 1 else "-" + str(i)}.html">{i}</a>'
                for i in range(1, len(pages) + 1)) + "</div>"
        body = f"""{intro}
<div class="filters"><input id="q" type="search"
 placeholder="Filtrar esta página ({len(chunk)} de {len(rows_html)})"
 aria-label="Filtrar"></div>
<ul class="feed" id="ls">{"".join(chunk)}</ul>{nav}
{FILTER_JS}
{prov(list(prov_lines)) if prov_lines else ""}"""
        out.append((f'{name}{"" if n == 1 else "-" + str(n)}.html',
                    shell(title + (f" · página {n}" if n > 1 else ""), body, depth)))
    return out


# --------------------------------------------------------------------- build

def main():
    t0 = time.time()
    # Ids upstream are not stable (vote ids are being re-keyed right now), so a
    # page whose slug changed would otherwise linger forever as a dead copy.
    # Only our own output is removed; anything else in site/ is left alone.
    for sub in ("proyecto", "proyectos", "parlamentario", "votacion", "comision"):
        if (OUT / sub).exists():
            shutil.rmtree(OUT / sub)
    con = db.connect(DBP)
    d = load(con)
    today = dt.date.today().isoformat()
    n = {"proyecto": 0, "parlamentario": 0, "votacion": 0, "listado": 0, "csv": 0}

    # ---- bills
    for b in d["bills"]:
        write(OUT / "proyecto" / str(b["per_par"]) / (b["chamber"] or "C")
              / f'{b["ply_num"]}.html', render_bill(d, b))
        n["proyecto"] += 1

    # ---- legislators (peer baselines first: one pass, no N+1)
    base = {"bills": {}, "prim": {}, "mots": {}, "asis": {}}
    for L in d["legs"]:
        c = L["chamber"]
        mine = [x for x in d["leg_bills"].get(L["slug"], [])
                if d["bill_by_id"][x[0]]["per_par"] >= 2026]
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
        n["parlamentario"] += 1

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
        intro = (f'<div class="crumb"><a href="../index.html">Inicio</a> › '
                 f'<a href="../proyectos.html">Proyectos de ley</a></div>'
                 f'<span class="eyebrow">{num(len(bs))} proyectos</span>'
                 f'<h1>{esc(title)}</h1>')
        for fn, htm in paginate(key, title, intro,
                                [bill_row("../", b) for b in bs], 1, prov_lines=src):
            write(OUT / "proyectos" / fn, htm)
            n["listado"] += 1

    def facet_links(prefix, sort=None):
        ks = [k for k in groups if k.startswith(prefix)]
        ks.sort(key=sort or (lambda k: -len(groups[k][2])))
        return '<div class="facets">' + "".join(
            f'<a href="proyectos/{k}.html">{esc(groups[k][1])} '
            f'<b>{num(len(groups[k][2]))}</b></a>' for k in ks) + "</div>"

    hub = f"""<span class="eyebrow">Proyectos de ley</span>
<h1>{num(len(d["bills"]))} proyectos de ley</h1>
<p class="lede">Todo lo que se ha presentado en el Congreso desde 2021, con su estado
oficial traducido a lenguaje llano y el trámite que le falta a cada uno.</p>
<section><h2>Por cámara y periodo</h2>{facet_links("p2")}</section>
<section><h2>Por etapa del trámite</h2>{facet_links("estado-")}</section>
<section><h2>Por año de presentación</h2>
{facet_links("anio-", sort=lambda k: k)}</section>
<section><h2>Presentados más recientemente</h2>
<ul class="feed">{"".join(bill_row("", b) for b in d["bills"][:40])}</ul></section>
{prov(src)}"""
    write(OUT / "proyectos.html", shell("Proyectos de ley", hub, 0))
    n["listado"] += 1

    # ---- committees
    for c in d["cttes"].values():
        bs = [d["bill_by_id"][b] for b in d["ctte_bills"].get(c["id"], [])
              if b in d["bill_by_id"]]
        bs.sort(key=lambda b: b["presented_on"] or "", reverse=True)
        intro = (f'<div class="crumb"><a href="../index.html">Inicio</a> › '
                 f'<a href="../proyectos.html">Proyectos de ley</a> › Comisiones'
                 f'</div><span class="eyebrow">Comisión dictaminadora</span>'
                 f'<h1>{esc(c["name"])}</h1><p class="lede">Los '
                 f'{num(len(bs))} proyectos de ley que han pasado por esta '
                 f'comisión. Una comisión que no dictamina archiva de hecho: '
                 f'al terminar el periodo, lo que sigue aquí caduca.</p>')
        for fn, htm in paginate(
                c["slug"], c["name"], intro, [bill_row("../", b) for b in bs], 1,
                prov_lines=[f'Comisiones asignadas por el Congreso, del '
                            f'expediente de cada proyecto en <code>{API}</code>.',
                            (f'<a href="{esc(c["url"])}">Página oficial de la '
                             f'comisión ↗</a>.') if c["url"] else "",
                            f'Regenerado el {fecha(today)}.']):
            write(OUT / "comision" / fn, htm)
            n["listado"] += 1

    # ---- legislators index
    rows = "".join(
        f'<li><span class="m">{esc(CHAMBER_SHORT[L["chamber"]])} '
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
    body = f"""<span class="eyebrow">Padrón</span>
<h1>{len(d["legs"])} parlamentarios</h1>
<p class="lede">{dips} diputados y {sens} senadores del periodo 2026-2031
{f", más {past} integrantes del Congreso unicameral 2021-2026 cuyas firmas siguen en el registro" if past else ""}.
Cada ficha reúne lo que firmó, lo que votó y cómo se compara con la mediana de
su propia cámara.</p>
<div class="filters"><input id="q" type="search"
 placeholder="Nombre, región o bancada" aria-label="Filtrar parlamentarios"></div>
<ul class="feed" id="ls">{rows}</ul>
{FILTER_JS}
{prov([
    'Padrón, foto, bancada y circunscripción: portales de la '
    '<a href="https://diputados.congreso.gob.pe/">Cámara de Diputados ↗</a> y del '
    '<a href="https://senado.congreso.gob.pe/">Senado ↗</a>, vía su API REST.',
    f'Regenerado el {fecha(today)}.'])}"""
    write(OUT / "parlamentarios.html", shell("Parlamentarios 2026-2031", body, 0))
    n["listado"] += 1

    # ---- motions index
    mrows = []
    for m in d["motions"]:
        sg = d["signers"].get(m["id"], [])
        who = ", ".join(
            (f'<a href="parlamentario/{s["leg"]["slug"]}.html">'
             f'{esc(nice_name(s["leg"]["full_name"]))}</a>' if s["leg"]
             else esc(nice_name(s["name_raw"]))) for s in sg[:6])
        mrows.append(
            f'<li id="m-{esc(m["id"])}"><span class="m">{esc(m["code"])} · '
            f'{esc(CHAMBER_SHORT[m["chamber"]])} · {fecha(m["presented_on"])} · '
            f'{esc(m["kind"] or "")}</span>'
            f'<span class="t">{esc((m["summary"] or "").strip()[:400])}</span>'
            f'<div class="sm mut sign" style="margin-top:6px">Firman: {who}'
            f'{" y " + str(len(sg) - 6) + " más" if len(sg) > 6 else ""}</div>'
            f'<span class="chip wait" style="margin-top:6px">'
            f'{esc(m["status"] or "")}</span></li>')
    body = f"""<span class="eyebrow">Mociones de orden del día</span>
<h1>{len(d["motions"])} mociones</h1>
<p class="lede">La moción es el instrumento con el que el Congreso se pronuncia sin
legislar: saludos, pedidos de interpelación, conformación de comisiones
investigadoras. En los primeros días de este Congreso hay más mociones que
proyectos de ley, así que es aquí donde se ve la actividad real.</p>
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
        vrows = "".join(
            f'<li><span class="m">{esc(CHAMBER_SHORT[v["chamber"]])} · '
            f'{fecha(v["held_on"])} · {esc(v["result"] or "")}</span>'
            f'<a class="t" href="votacion/{v['slug']}.html">'
            f'{esc(v["subject"] or v["id"])}</a></li>' for v in d["votes"])
        extra = ""
    else:
        vrows = ""
        extra = ('<div class="note">Todavía no hay ninguna votación nominal '
                 'procesada. El Congreso 2026-2031 publica las actas en PDF unos '
                 'días después de cada sesión; en cuanto haya una con listado '
                 'nominal aparecerá aquí, con el detalle de cómo votó cada '
                 'parlamentario y su descarga en CSV.</div>')
    body = f"""<span class="eyebrow">Votaciones nominales</span>
<h1>{len(d["votes"])} votaciones</h1>
<p class="lede">Cada votación nominal publicada, con el resultado por bancada,
quiénes rompieron con su grupo y la lista completa descargable.</p>{extra}
<ul class="feed" id="ls">{vrows}</ul>
{prov(['Actas de votación publicadas por la Cámara de Diputados y el Senado.',
       f'Regenerado el {fecha(today)}.'])}"""
    write(OUT / "votaciones.html", shell("Votaciones", body, 0))
    n["listado"] += 1

    # ---- home
    write(OUT / "index.html", render_home(d, base, today))
    n["listado"] += 1

    con.close()
    print(f"páginas de proyecto : {n['proyecto']}")
    print(f"páginas de parlamentario: {n['parlamentario']}")
    print(f"páginas de votación : {n['votacion']} (+{n['csv']} CSV)")
    print(f"listados e índices  : {n['listado']}")
    print(f"total               : {sum(n.values()) - n['csv']} páginas HTML")
    print(f"tiempo              : {time.time() - t0:.1f}s -> {OUT}")
    print("CAPS: fichas de parlamentario listan 60 proyectos y 40 mociones "
          "recientes (el total sí se muestra); los listados paginan de "
          f"{PER_PAGE} en {PER_PAGE} sin recortar nada.")
    return n


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

    cols = []
    for ch in ("D", "S"):
        bs = [b for b in cur if (b["chamber"] or "C") == ch][:6]
        ms = [m for m in d["motions"] if m["chamber"] == ch][:5]
        seats = sum(1 for L in d["legs"] if L["chamber"] == ch)
        nb = sum(1 for b in cur if (b["chamber"] or "C") == ch)
        nm = sum(1 for m in d["motions"] if m["chamber"] == ch)
        cols.append(f"""<div class="card">
<h3><span class="chip {ch.lower()}">{esc(CHAMBER[ch])}</span></h3>
<dl class="stat" style="margin:14px 0 18px">
<div><dt>Escaños</dt><dd>{seats}</dd></div>
<div><dt>Proyectos</dt><dd>{nb}</dd></div>
<div><dt>Mociones</dt><dd>{nm}</dd></div></dl>
{"<h3>Últimos proyectos</h3><ul class='feed'>" + "".join(bill_row("", b) for b in bs) + "</ul>" if bs else ""}
{"<h3 style='margin-top:18px'>Últimas mociones</h3><ul class='feed'>" + "".join(
    f'<li><span class="m">{esc(m["code"])} · {fecha(m["presented_on"])}</span>'
    f'<a class="t" href="mociones.html#m-{esc(m["id"])}">'
    f'{esc((m["summary"] or "").strip()[:120])}</a></li>' for m in ms) + "</ul>" if ms else ""}
</div>""")

    # stage distribution of the whole corpus
    dist = {}
    for b in d["bills"]:
        dist[status_info(b["status"])[0]] = dist.get(status_info(b["status"])[0], 0) + 1
    lab = {"PRES": "Recién presentados", "COM": "En comisión",
           "PLENO": "En el Pleno", "AUTOG": "Autógrafa en el Ejecutivo",
           "LEY": "Ya son ley", "DEAD": "Archivados o retirados"}
    col = {"PRES": "var(--sen)", "COM": "var(--wait)", "PLENO": "var(--accent)",
           "AUTOG": "var(--gpc)", "LEY": "var(--ok)", "DEAD": "var(--dead)"}
    tot = sum(dist.values()) or 1
    bar = ('<div class="bar">' + "".join(
        f'<i style="width:{100 * dist[k] / tot:.1f}%;background:{col[k]}"></i>'
        for k in lab if dist.get(k)) + '</div><div class="legend">' + "".join(
        f'<span style="color:{col[k]}"><span class="dotmark"></span>{lab[k]} '
        f'{num(dist[k])} ({100 * dist[k] / tot:.0f}%)</span>'
        for k in lab if dist.get(k)) + "</div>")

    votes_block = ""
    if d["votes"]:
        votes_block = ("<section><h2>Últimas votaciones nominales</h2><ul class='feed'>"
                       + "".join(
                           f'<li><span class="m">{esc(CHAMBER_SHORT[v["chamber"]])} · '
                           f'{fecha(v["held_on"])} · {esc(v["result"] or "")}</span>'
                           f'<a class="t" href="votacion/{v['slug']}.html">'
                           f'{esc(v["subject"] or v["id"])}</a></li>'
                           for v in d["votes"][:6]) + "</ul></section>")

    last = max((b["presented_on"] or "") for b in d["bills"]) if d["bills"] else today
    body = f"""<span class="eyebrow">Congreso de la República del Perú · 2026-2031</span>
<h1>Qué está haciendo el Congreso ahora mismo</h1>
<p class="lede">Un registro público del Congreso bicameral: 130 diputados, 60
senadores, cada proyecto de ley con la etapa exacta en la que está y lo que le
falta, cada moción con quién la firmó y cada votación nominal con el detalle de
quién votó qué. Último movimiento registrado: <b>{fecha(last)}</b>.</p>

<section><h2>Actividad del Congreso actual</h2>
<dl class="stat">
<div><dt>Proyectos presentados</dt><dd>{len(cur)}
<small>{rate:.1f} por día en {days} días de legislatura</small></dd></div>
<div><dt>Mociones</dt><dd>{len(d["motions"])}
<small>{len(d["motions"]) / max(days, 1):.1f} por día</small></dd></div>
<div><dt>Firmas de parlamentarios</dt>
<dd>{sum(len(d["spon"].get(b["id"], [])) for b in cur)}
<small>en los {len(cur)} proyectos de este periodo</small></dd></div>
<div><dt>Votaciones nominales</dt><dd>{len(d["votes"])}
<small>actas con listado nominal procesadas</small></dd></div>
</dl>
<p class="sm mut" style="margin-top:14px">Para comparar: el Congreso 2021-2026
presentó {num(len(old))} proyectos en {old_days} días, es decir {old_rate:.1f} por
día. Este Congreso va a {"un ritmo mayor" if rate > old_rate else "un ritmo menor"}
que aquel.</p></section>

<section><h2>Las dos cámaras</h2><div class="grid g2">{"".join(cols)}</div></section>
{votes_block}

<section><h2>Dónde están los {num(len(d["bills"]))} proyectos registrados</h2>
{bar}
<p class="sm mut" style="margin-top:12px">Incluye el acervo del Congreso
unicameral 2021-2026, que es el que permite ver cuántos proyectos llegan
realmente a ser ley. <a href="proyectos.html">Explorar por etapa</a>.</p></section>

<section><h2>Empezar por aquí</h2><div class="facets">
<a href="proyectos.html">Todos los proyectos de ley <b>{num(len(d["bills"]))}</b></a>
<a href="parlamentarios.html">Padrón de parlamentarios <b>{len(d["legs"])}</b></a>
<a href="mociones.html">Mociones <b>{len(d["motions"])}</b></a>
<a href="votaciones.html">Votaciones nominales <b>{len(d["votes"])}</b></a>
<a href="proyectos/p2026-D.html">Proyectos de Diputados <b>{sum(1 for b in cur if b["chamber"] == "D")}</b></a>
<a href="proyectos/p2026-S.html">Proyectos del Senado <b>{sum(1 for b in cur if b["chamber"] == "S")}</b></a>
</div></section>

{prov([
    'Todo el contenido proviene de tres fuentes oficiales: el registro de '
    f'proyectos de ley (<code>{API}</code>), el registro de mociones '
    '(<code>smociones-portal-service</code>) y los portales de la '
    '<a href="https://diputados.congreso.gob.pe/">Cámara de Diputados ↗</a> y el '
    '<a href="https://senado.congreso.gob.pe/">Senado ↗</a>.',
    'No editorializamos: el estado de cada proyecto es el que publica el '
    'Congreso; lo único que agregamos es la traducción de ese estado a lenguaje '
    'llano y el cruce por nombre entre padrón y firmas.',
    f'Sitio regenerado el {fecha(today)}; el dato más reciente que contiene es '
    f'del {fecha(last)}.',
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
    L = dict(con.execute("SELECT * FROM legislator LIMIT 1").fetchone())
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
