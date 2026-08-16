"""Lo que cuesta un despacho: planilla, viajes, y el calendario que los enmarca.

The Congress publishes none of this. It never published gastos operativos per
congresista -- the one instance in the whole Wayback record is a JPEG a member
put on his own page in 2012 -- and the bicameral reglamentos in force since
27/07/2026 deleted even the obligation to liquidate them (old art. 22 f: monthly,
to Tesorería, >=30% backed by receipts; the new texts contain the phrase nowhere).

What is public, machine-readable and ungated is the Portal de Transparencia
Estándar for entity 16. Three sources here:

  payroll  every person on the Congress payroll, monthly, with the despacho they
           serve named in plain text -- this is the join that makes a per-member
           cost possible at all
  travel   every authorised trip, with viáticos and pasajes, per despacho
  rep_week the 191 semanas de representación, the denominator for travel

Not here, because it does not exist as data: the congresista's own remuneración
and the asignación por función congresal. Both are uniform statutory amounts, and
the acuerdo that last set the asignación (118-2023-2024/MESA-CR) updates it "por
el Índice de Precios al Consumidor" without stating a figure -- the number lives
in an unpublished informe. Rendering a constant 190 times is not data.
"""
import re
import unicodedata
import xml.etree.ElementTree as ET

from . import api, db
from .votes import norm

PTE = "https://www.transparencia.gob.pe"
ENTIDAD = 16  # CONGRESO DE LA REPÚBLICA. There is no separate Diputados or Senado
# entity: pte_autocomplete.ashx?term=senado and ?term=diputados both answer []. The
# bicameral split created no new PTE pliego, so the chamber can only come from our
# own padrón, never from the source.

SEMANAS = "https://www3.congreso.gob.pe/semana-representacion/"

MESES = {m: i for i, m in enumerate(
    "enero febrero marzo abril mayo junio julio agosto setiembre octubre "
    "noviembre diciembre".split(), 1)}
MESES["septiembre"] = 9


def _rows(url, tag):
    """One PTE report as a list of dicts.

    Always XML, never the CSV twin: `VC_PERSONAL_DEPENDENCIA` contains raw
    newlines and the field is not quoted, so July 2026 is 4,138 lines for 4,051
    records and every naive parse silently shears ~2% of the payroll in half.
    The XML carries the same rows with the breaks inside the element.
    """
    body = api.fetch(url)
    # An empty selection answers 200 with a zero-byte body, not an empty document.
    # That is how an unpublished month and an empty régimen both look, so it is a
    # normal answer and never a parse failure.
    if not body.strip():
        return []
    root = ET.fromstring(body)
    return [{c.tag: (c.text or "").strip() for c in el}
            for el in root if el.tag == tag]


def personal_url(year, month):
    return (f"{PTE}/personal/pte_transparencia_personal_genera.aspx"
            f"?id_entidad={ENTIDAD}&in_anno_consulta={year}"
            f"&ch_mes_consulta={month:02d}&ch_tipo_regimen="
            f"&vc_dni_funcionario=&vc_nombre_funcionario=&ch_tipo_descarga=3")


def viaticos_url(year, month):
    return (f"{PTE}/contrataciones/pte_transparencia_contrataciones_genera.aspx"
            f"?id_entidad={ENTIDAD}&in_anno={year}&in_mes={month:02d}"
            f"&tipo_viaje=0&modo_viatico=0&Ver=&tipo_seleccion=2&formato=xml")


def personal(year, month):
    """Payroll for one month. Empty list until the month is published."""
    return _rows(personal_url(year, month), "Personal")


def viaticos(year, month):
    """Trips authorised in one month. Empty list until published.

    tipo_viaje=0 is both kinds, and the filter always agrees with the stored
    CH_VIATICOS_TIPO. Do not trust that field for what it claims to be: the PTE
    labels it "Viajes nacionales / Viajes internacionales", and in 2025-09 it is
    exactly that (17 of 17 kind=2 rows read 'Lima Colombia Lima'), but by 2026-06
    it is 544 kind=2 rows of which about 20 are foreign -- the rest are LIM-CIX
    and LIMA-TRUJILLO. Whatever the Congress started using it for in 2026, it is
    not the trip's destination. `route` is the only honest test of that.

    Money is always in the _N columns. The _E pair exists in the source and is
    zero in all 67 months, foreign trips included.
    """
    return _rows(viaticos_url(year, month), "Reporte")


def _f(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def per_par(year, month):
    """Which Congress a payroll month belongs to.

    The bicameral Congress installed 27/07/2026, four days before the month ended,
    but the July 2026 payroll is still the outgoing despachos: of 127 despachos in
    that file, 126 are 2021-2026 members. So July goes to the old period and August
    is the first month of the new one.
    """
    return 2026 if (year, month) >= (2026, 8) else 2021


def roster(con):
    """sorted name tokens -> [(id, per_par)]. Deliberately a multimap: 22 people
    sit in the padrón twice, once per period, and folding them together would let
    a 2021-2026 despacho resolve to a 2026-2031 seat."""
    out = {}
    for l in con.execute("SELECT id, per_par, full_name FROM legislator"):
        out.setdefault(tuple(sorted(norm(l["full_name"]))), []).append(
            (l["id"], l["per_par"]))
    return out


def resolve(legs, name, period):
    """A despacho name -> legislator id, or None.

    Exact token-multiset match, never a prefix or subset one. PTE writes
    'MERINO DE LAMA MANUEL ARTURO' where the padrón has 'Merino de Lama, Manuel
    Arturo', so ordering and punctuation have to go, but nothing else may: a
    subset match happily maps the área 'DESPACHO CONGRESAL' onto a member.
    Returns None on a tie rather than guessing.
    """
    hits = legs.get(tuple(sorted(norm(name))), [])
    hits = [h for h in hits if h[1] == period] or hits
    return hits[0][0] if len(hits) == 1 else None


def ingest_month(con, year, month, legs=None):
    """One month of payroll and travel. Returns (payroll rows, travel rows).

    Idempotent: a month that came back with rows is deleted and rewritten, so
    re-running is a refresh and never a duplicate.
    """
    legs = roster(con) if legs is None else legs
    period = per_par(year, month)
    np = nt = 0
    rows, trips = personal(year, month), viaticos(year, month)
    # Fetch both before deleting either: an empty month is the normal answer for
    # anything not yet published, and wiping a stored month to replace it with
    # nothing is how a publication lag turns into data loss. PK_ID_PERSONAL looks
    # globally unique and monotonic today, but a corrected re-publication with
    # fresh ids would otherwise leave the superseded rows behind forever.
    if rows:
        con.execute("DELETE FROM payroll WHERE year=? AND month=?", (year, month))
    if trips:
        con.execute("DELETE FROM travel WHERE year=? AND month=?", (year, month))
    for r in rows:
        dep = r.get("VC_PERSONAL_DEPENDENCIA", "")
        db.upsert(con, "payroll", {
            "id": int(r["PK_ID_PERSONAL"]), "year": year, "month": month,
            "regimen": r.get("VC_PERSONAL_REGIMEN_LABORAL"),
            "name_raw": " ".join(x for x in (r.get("VC_PERSONAL_PATERNO"),
                                             r.get("VC_PERSONAL_MATERNO"),
                                             r.get("VC_PERSONAL_NOMBRES")) if x),
            "cargo": r.get("VC_PERSONAL_CARGO"), "dependencia": dep,
            "legislator_id": resolve(legs, dep, period),
            "remuneracion": _f(r.get("MO_PERSONAL_REMUNERACIONES")),
            "honorarios": _f(r.get("MO_PERSONAL_HONORARIOS")),
            "incentivo": _f(r.get("MO_PERSONAL_INCENTIVO")),
            "gratificacion": _f(r.get("MO_PERSONAL_GRATIFICACION")),
            "otros": _f(r.get("MO_PERSONAL_OTROS_BENEFICIOS")),
            "total": _f(r.get("MO_PERSONAL_TOTAL")),
            "source_url": personal_url(year, month)})
        np += 1
    for r in trips:
        area = r.get("VC_VIATICOS_AREA", "")
        db.upsert(con, "travel", {
            "id": int(r["PK_VIATICOS"]), "year": year, "month": month,
            "kind": r.get("CH_VIATICOS_TIPO"), "area_raw": area,
            "legislator_id": resolve(legs, area, period),
            "traveller": r.get("VC_VIATICOS_USUARIOS"),
            "went_on": _date(r.get("DT_VIATICOS_FECHAS")),
            "returned_on": _date(r.get("DT_VIATICOS_FECHAS_RETORNO")),
            "route": r.get("VC_VIATICOS_RUTA"),
            "authority": r.get("VC_VIATICOS_AUTORIZACION"),
            "resolution": r.get("VC_VIATICOS_RESOLUCION"),
            "pasajes": _f(r.get("DC_VIATICOS_COSTO_PASAJES_N")),
            "viaticos": _f(r.get("DC_VIATICOS_VIA_N")),
            "total": _f(r.get("DC_VIATICOS_TOTAL_N")),
            "pasajes_ext": _f(r.get("DC_VIATICOS_COSTO_PASAJES_E")),
            "viaticos_ext": _f(r.get("DC_VIATICOS_VIA_E")),
            "total_ext": _f(r.get("DC_VIATICOS_TOTAL_E")),
            "source_url": viaticos_url(year, month)})
        nt += 1
    con.commit()
    return np, nt


def _date(s):
    """'17/06/2026' -> '2026-06-17'."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}" if m else None


def months(start=(2021, 1), end=None):
    """Every (year, month) from `start` to `end` inclusive; end defaults to now.

    PTE registers month M in the first half of M+1 (measured from FEC_REG: May ->
    Jun 5, Jun -> Jul 8, Jul -> Aug 12), so the newest one or two months come back
    empty. That is not an error; it is the publication lag.
    """
    import datetime as dt
    if end is None:
        t = dt.date.today()
        end = (t.year, t.month)
    y, m = start
    while (y, m) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def ingest(con, start_year=2021):
    """Backfill payroll and travel. PTE holds personal from 2013 and viáticos from
    2009; 2021 is where our padrón starts, so earlier months would store rows that
    can never resolve to a member."""
    legs = roster(con)
    np = nt = empty = 0
    for y, m in months((int(start_year), 1)):
        a, b = ingest_month(con, y, m, legs)
        np, nt = np + a, nt + b
        empty += not (a or b)
        if a or b:
            print(f"  {y}-{m:02d}: {a} planilla, {b} viajes", flush=True)
    print(f"gastos: {np} filas de planilla, {nt} viajes, {empty} meses sin publicar",
          flush=True)
    return np, nt


# --- semanas de representación -------------------------------------------------

def rep_weeks(html_text=None):
    """Parse the calendar. Returns dicts ready for the rep_week table."""
    s = html_text if html_text is not None else api.fetch(SEMANAS).decode(
        "utf-8", "replace")
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", s, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) != 3:
            continue
        n, period, raw = (_flat(t) for t in tds)
        if not n.isdigit():
            continue                                    # the header row
        link = re.search(r'href="([^"]+)"', tds[1])
        up = raw.upper()
        held = not ("SUSPENDIDA" in up or "NO SE REALIZ" in up)
        start, end, note = _week_dates(raw, period)
        out.append({"n": int(n), "period": period, "starts_on": start,
                    "ends_on": end, "held": int(held), "raw": raw, "note": note,
                    "oficio_url": _abs(link[1]) if link else None})
    return out


def _flat(cell):
    import html as _h
    return re.sub(r"\s+", " ", _h.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


def _abs(href):
    return href if href.startswith("http") else "https://www3.congreso.gob.pe" + href


def _week_dates(raw, period):
    """'Del lunes 13 al viernes 17 de julio de 2026' -> ('2026-07-13','2026-07-17').

    Two shapes matter. The month can be named once at the end or twice when the
    week straddles one ('Del lunes 28 de mayo al viernes 1 de junio de 2018').

    And the year in the prose is not trustworthy: week 187 reads "Del lunes 23 al
    viernes 27 de marzo de 2036". The MES / AÑO column says MARZO 2026 and the
    series is monotonic, so 2036 is a typo. We do not silently fix it -- `raw`
    keeps the cell verbatim, `note` records the correction, and the parsed dates
    use the column's year so the calendar sorts.
    """
    yr = re.search(r"\b(\d{4})\b", period)
    col_year = int(yr[1]) if yr else None
    # Two traps in the weekday. The space after it is optional -- six cells read
    # "Del lunes16 al viernes 20 de mayo de 2022" -- and it is accented, so an
    # ASCII class quietly drops every week ending on a sábado or a miércoles.
    m = re.search(r"[Dd]el\s+[^\W\d_]+\s*(\d{1,2})(?:\s+de\s+(\w+))?\s+al\s+"
                  r"[^\W\d_]+\s*(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", raw)
    if not m:
        return None, None, None
    d1, m1, d2, m2, year = m[1], m[2], m[3], m[4], int(m[5])
    note = None
    if col_year and year != col_year:
        note = f"año publicado en la prosa: {year}; MES/AÑO dice {col_year}"
        year = col_year
    mo2 = MESES.get(_fold(m2))
    mo1 = MESES.get(_fold(m1)) if m1 else mo2
    if not mo1 or not mo2:
        return None, None, note
    # A week that starts in December and ends in January belongs to two years.
    y1 = year - 1 if mo1 > mo2 else year
    return (f"{y1}-{mo1:02d}-{int(d1):02d}", f"{year}-{mo2:02d}-{int(d2):02d}", note)


def _fold(s):
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def split_despachos(con):
    """Members whose planilla and viajes land on different legislator ids.

    One human can sit in the padrón twice under two spellings -- the chambers
    publish 'Barbaran Reyes, Rosangela Andrea' for 2026-2031 and the 2021-2026
    roster has 'Barbarán Reyes, Rosangella Andrea' -- and PTE uses both. The
    exact-match join then splits one despacho's cost in two, which on a cost page
    reads as a member who spent S/14,774 in a year instead of S/600,000.

    Not repaired by loosening the match: one letter of edit distance is exactly
    the licence that lets 'DESPACHO CONGRESAL' become a member. Reported instead,
    so a second case fails the self-check rather than reaching a page.
    """
    seen, out = {}, []
    for l in con.execute("SELECT id, full_name FROM legislator ORDER BY id"):
        # Collapse doubled letters: 'rosangella' -> 'rosangela'. Loose enough to
        # catch the spelling variants the two chambers publish, tight enough that
        # no two actual members have ever collided under it.
        key = tuple(sorted(re.sub(r"(.)\1+", r"\1", t) for t in norm(l["full_name"])))
        if key in seen and seen[key] != l["id"]:
            out.append((seen[key], l["id"]))
        seen.setdefault(key, l["id"])
    # Only a real problem where both halves actually carry spend.
    spent = {r[0] for r in con.execute(
        "SELECT DISTINCT legislator_id FROM payroll WHERE legislator_id IS NOT NULL "
        "UNION SELECT DISTINCT legislator_id FROM travel "
        "WHERE legislator_id IS NOT NULL")}
    return [(a, b) for a, b in out if a in spent and b in spent]


def ingest_weeks(con):
    """Store the calendar. Full refresh: it is one page and rows get edited in
    place upstream. Returns (weeks, suspended)."""
    ws = rep_weeks()
    con.execute("DELETE FROM rep_week")
    for w in ws:
        db.upsert(con, "rep_week", w)
    con.commit()
    return len(ws), sum(1 for w in ws if not w["held"])


def demo():
    """`python3 -m ingest.gastos` -- hits the live sources."""
    # The payroll join is the whole premise, so check it on a month we have read.
    rows = personal(2026, 7)
    assert len(rows) == 4051, len(rows)
    con = db.connect()
    legs = roster(con)
    deps = {r["VC_PERSONAL_DEPENDENCIA"] for r in rows}
    hit = {d for d in deps if resolve(legs, d, 2021)}
    assert len(hit) == 127, len(hit)
    assert resolve(legs, "DESPACHO CONGRESAL", 2021) is None, "área matched a member"
    # Régimen 7 "Altos Funcionarios" is where a congresista's own pay would sit.
    # It has been empty for entity 16 in every month ever checked, which is why
    # this module can cost a despacho and not a member.
    assert not _rows(personal_url(2026, 7).replace(
        "ch_tipo_regimen=", "ch_tipo_regimen=7"), "Personal"), "congresistas in PTE?"

    # One member is two padrón rows under two spellings, so her cost splits. Any
    # second case is a new bug and must not reach a page silently.
    split = split_despachos(con)
    assert len(split) <= 1, split

    trips = viaticos(2026, 6)
    assert len(trips) == 770, len(trips)
    assert sum(1 for t in trips if resolve(legs, t["VC_VIATICOS_AREA"], 2021)) > 300
    # All the money is in the _N columns, foreign trips included -- if the _E pair
    # ever starts being populated, every total here is an undercount.
    assert not [t for t in trips if float(t["DC_VIATICOS_TOTAL_E"] or 0)], "_E in use"

    ws = rep_weeks()
    assert len(ws) == 192, len(ws)          # 191 numbered; 103 is used twice
    assert max(w["n"] for w in ws) == 191, "calendar grew -- new period?"
    # Two different ways of not happening, counted apart because they are: a week
    # SUSPENDIDA was called off, one marked NO SE REALIZÓ was never convened.
    susp = [w for w in ws if "SUSPEND" in w["raw"].upper()]
    assert len(susp) == 13, len(susp)
    assert sum(1 for w in ws if not w["held"]) == 45, "held flag moved"
    # Five held weeks are prose we deliberately do not parse: enumerated day lists
    # ("Durante los días jueves 22, viernes 23, lunes 26..."), one "A partir del"
    # with no end, and one with no year at all. They keep `raw` and a NULL range.
    assert sum(1 for w in ws if w["held"] and not w["starts_on"]) == 5, "date parse moved"
    typo = [w for w in ws if w["n"] == 187][0]
    assert "2036" in typo["raw"] and typo["starts_on"] == "2026-03-23", typo
    print(f"ok: {len(rows)} en planilla ({len(hit)} despachos resueltos), "
          f"{len(trips)} viajes, {len(ws)} semanas de representación")


if __name__ == "__main__":
    demo()
