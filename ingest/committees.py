"""Committee membership, parsed out of the Diario de los Debates.

Nobody publishes this as data. The chambers list committees by name and nothing
else, and the WordPress taxonomy that would carry it has one term. But the
session where the chamber approves its committee rosters prints them in full, by
committee, by bench, split into titulares and suplentes:

    Comision de Economia
    Fuerza Popular
    Titulares: Flores Ruiz, Noriega Reategui, Schaeffer Cuculiza.
    Suplentes: Del Aguila Cardenas, Melgar Valdez.

Names are surname-only, so they go through the same fuzzy join the vote roster
uses. A member can hold a seat in several committees and be titular in one and
suplente in another.

The second half of this module does not parse a PDF at all. The diario only
exists once a chamber's Pleno has approved a cuadro de comisiones, and the
Cámara de Diputados has not published one -- so the nómina of its 19 committees
comes off the web, and the Senado's mesas directivas (presidencia,
vicepresidencia, secretaría) off the one table where the Senado prints them.
"""
import difflib
import html
import re
import subprocess

from . import api, db, spley
from .legislators import norm
from .spley import slugify

# A heading is a committee only if a bench follows it; a bare mention in prose
# does not start a roster. Benches are the six that exist in this Congress.
BENCHES = ["Fuerza Popular", "Juntos por el Perú", "Renovación Popular",
           "Partido del Buen Gobierno", "Partido Cívico Obras", "Ahora Nación"]
# "Accesitario" and "Accesitario suplente" are the same seat as a suplente. The
# colon is not always typed ("Titulares Melgar Valdez, ...").
ROLE_RE = re.compile(
    r"^\s*(Titular|Suplente|Accesitari[oa])[a-z]*(?:\s+suplentes?)?\s*:?\s+(.*)$", re.I)
PAGE_RE = re.compile(r"^\s*\d{1,3}\s*$")
# After a bench files an Oficio, what follows are *changes* to its earlier
# designations, not extra members. Merging the two double-counts the committee.
AMEND_RE = re.compile(r"los cambios son los siguientes|Oficio\s+\d+-\d{4}-\d{4}", re.I)


def bench_of(line):
    """-> (bench, trailing names) for a bench line, else (None, None).

    Three shapes in one document: bare "Fuerza Popular", "Fuerza Popular:" with
    the roster below it, and "Fuerza Popular: Miyashiro Arashiro." with the whole
    (one-member) roster on the same line, which is how Etica Parlamentaria is
    written.
    """
    head, sep, rest = line.partition(":")
    head = head.strip()
    if head not in BENCHES:
        return None, None
    return head, rest.strip() if sep else None


def text(path):
    return subprocess.run(["pdftotext", "-layout", str(path), "-"],
                          capture_output=True, text=True).stdout


def clean_committee(name):
    """Trim the trailing prose the layout glues onto a heading."""
    name = re.sub(r"\s+", " ", name).strip(" .·")
    # headings wrap; the roster ones never contain a verb clause
    return re.sub(r"\s+(que|para que|a fin de)\b.*$", "", name, flags=re.I)


def split_names(chunk):
    """'Flores Ruiz, Noriega Reategui, Yamashiro Ore.' -> three surnames.

    The separator is a comma, but names themselves never contain one here --
    the diario prints surnames only, unlike the bill API which uses
    'Apellidos, Nombres'.
    """
    out = []
    # a full stop mid-chunk also separates, but only before a new capitalised name
    for n in re.split(r"[,;]|\.\s+(?=[A-ZÁÉÍÓÚÑ])", chunk.replace(" y ", ", ")):
        n = re.sub(r"\s+", " ", n).strip(" .")
        if n and not n.lower().startswith(("titular", "suplente")) and len(n) > 3:
            out.append(n)
    return out


def is_heading(lines, i, names=frozenset()):
    """A committee heading is whatever line a roster starts under.

    Matching on the words "Comision de" misses the ones the transcript writes
    bare -- "Procedimientos Especiales", "Regimenes de Excepcion" -- and silently
    folds their members into the committee above, which is how one committee
    ended up with 19 titulares against a house norm of 12.

    But the purely structural rule ("a bench follows it") is too loose on its
    own: a Suplentes list that wraps onto its own line also sits directly above
    the next bench, and three of those became committees. So a heading is a line
    followed by a bench, that is not itself a bench or a role line, and that is
    not a list of people -- which is what `names` is for.
    """
    line = lines[i].strip()
    if (not line or bench_of(line)[0] or ROLE_RE.match(line)
            or PAGE_RE.match(line) or line.lower().startswith("grupos parlamentarios")):
        return False
    if len(line) < 8 or line.endswith((",", ":")) or line[0].islower():
        return False
    formal = line.lower().startswith(("comisi", "subcomisi"))
    if not formal:
        # a wrapped roster line: commas, or the line is simply somebody's name
        if "," in line or norm(line) in names:
            return False
        if any(norm(n) in names for n in split_names(line)):
            return False
    for nxt in lines[i + 1:i + 6]:
        nxt = nxt.strip()
        if not nxt or PAGE_RE.match(nxt):
            continue
        if nxt.lower().startswith("grupos parlamentarios"):
            return True
        return bool(bench_of(nxt)[0]) or (formal and bool(ROLE_RE.match(nxt)))
    return False


def parse(body, names=frozenset()):
    """-> {committee_name: [(name_raw, bench, role, amendment)]}"""
    out, cur, bench, amend = {}, None, None, 0
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if AMEND_RE.search(line):
            amend = 1
        if is_heading(lines, i, names):
            cur, bench = clean_committee(line), None
            out.setdefault(cur, [])
        elif bench_of(line)[0]:
            bench, inline = bench_of(line)
            # "Fuerza Popular: Miyashiro Arashiro." -- roster on the bench line
            if inline and cur and not ROLE_RE.match(inline):
                for name in split_names(inline):
                    out[cur].append((name, bench, "titular", amend))
        elif cur:
            m = ROLE_RE.match(line)
            if m:
                role = ("titular" if m.group(1).lower().startswith("titular")
                        else "suplente")
                chunk = m.group(2)
                # the list often continues on the next non-blank line
                j = i + 1
                while j < len(lines) and not chunk.rstrip().endswith("."):
                    nxt = lines[j].strip()
                    if not nxt:
                        j += 1
                        continue
                    if (bench_of(nxt)[0] or ROLE_RE.match(nxt)
                            or is_heading(lines, j, names)):
                        break
                    chunk += " " + nxt
                    j += 1
                i = j - 1
                for name in split_names(chunk):
                    out[cur].append((name, bench, role, amend))
        i += 1
    return {k: v for k, v in out.items() if v}


def match(name_raw, people):
    """Surname match against the roster.

    The transcript is typed by hand and misspells names freely -- Scheafer and
    Schaeffer for Schaefer, Vazquez for Vasquez, Luque Barra for Luque Ibarra --
    and sometimes prints the given names first. So: exact, then containment
    either way, then a close match. Only ever accept an unambiguous winner.
    """
    key = norm(name_raw)
    if key in people:
        return people[key]
    hits = {lid for surname, lid in people.items()
            if f" {surname} " in f" {key} " or f" {key} " in f" {surname} "}
    if len(hits) == 1:
        return hits.pop()
    near = difflib.get_close_matches(key, people, n=2, cutoff=0.86)
    if len(near) == 1 or (len(near) == 2 and people[near[0]] == people[near[1]]):
        return people[near[0]]
    return None


def ingest(con, chamber, pdf):
    """Parse one diario into `committee` + `committee_member`. Returns counts."""
    people = {}
    for r in con.execute(
            "SELECT id, last_name, full_name FROM legislator WHERE chamber=?",
            (chamber,)):
        people[norm(r["last_name"] or r["full_name"])] = r["id"]

    rosters = parse(text(pdf), frozenset(people))
    # Scope the lookup by chamber and period: committee names repeat across
    # congresses ("Inteligencia" exists in both), and matching on the slug alone
    # hung this Senate's roster off a 2021 unicameral committee.
    known = {(r["slug"], r["chamber"], r["per_par"]): r["id"]
             for r in con.execute(
                 "SELECT id, slug, chamber, per_par FROM committee")}
    nxt = (con.execute("SELECT coalesce(max(id),0) FROM committee").fetchone()[0]
           or 0) + 1
    nc = nm = unmatched = 0
    for name, members in rosters.items():
        slug = slugify(name)
        key = (slug, chamber, 2026)
        cid = known.get(key)
        if cid is None:
            cid = nxt = max(nxt, 10_000) + 1
            known[key] = cid
            db.upsert(con, "committee", {
                "id": cid, "per_par": 2026, "chamber": chamber,
                "name": name, "slug": slug})
        nc += 1
        for raw, bench, role, amend in members:
            lid = match(raw, people)
            unmatched += lid is None
            # El diario no sabe nada de mesas: reemplazar la fila no puede
            # borrar el cargo que ingest_mesas le selló. Correr los módulos en
            # el otro orden dejaba 5 sellos de 36 y el autochequeo lo cazó.
            old = con.execute(
                "SELECT mesa FROM committee_member WHERE committee_id=? "
                "AND name_raw=? AND role=?", (cid, raw, role)).fetchone()
            db.upsert(con, "committee_member", {
                "committee_id": cid, "legislator_id": lid, "name_raw": raw,
                "bench": bench, "role": role, "amendment": amend,
                "mesa": old["mesa"] if old else None,
                "source_url": str(pdf)})
            nm += 1
    con.commit()
    return nc, nm, unmatched


# ---------------------------------------------------------------------------
# The web nómina, and the mesas directivas.

WP = "https://{}.congreso.gob.pe/wp-json/wp/v2"
HOST = {"D": "diputados", "S": "senado"}
# Cargos are typed in the gender of whoever holds them, so the regex has to
# spell both out: `Presidenta?` matches "President" and then stalls on the "e"
# of "Presidente", which silently drops two thirds of the table.
MESA_RE = re.compile(
    r"<strong>(Vicepresident[ae]|President[ae]|Secretari[oa]):</strong>\s*"
    r"(?:<br\s*/?>)?\s*(.*?)<br\s*/?>\s*Grupo\s+[Pp]arlamentario:\s*([^<]*)", re.S)
# Only the office matters downstream; the -a/-o of the title is the person's.
MESA = {"p": "presidencia", "v": "vicepresidencia", "s": "secretaria"}


def untag(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def page(chamber, slug):
    """-> (link, rendered body) of one WordPress page, by slug.

    By slug and not by id because ids are not stable across a rebuild of the
    site, and by page and not by search because both chambers keep stale copies
    of these lists around: `comisiones-ordinarias-legislativas` is an April
    draft that still spells "Educación, Cultura y Deportes" in the plural.
    """
    d = api.get_json(f"{WP.format(HOST[chamber])}/pages?slug={slug}"
                     "&_fields=id,link,content")
    if not d:
        raise RuntimeError(f"{HOST[chamber]}: no existe la página /{slug}/")
    return d[0]["link"], d[0]["content"]["rendered"]


# The chamber page is typed by hand and drifts from the API's spelling; each
# known erratum maps to the API's key. A new mismatch still raises in
# nomina_diputados, which is where it should be caught, not papered over.
CTTE_ALIAS = {
    # page: "Innovación, Tecnológia" — API: "Innovación Tecnológica"
    "ciencia, innovacion, tecnologia y sociedad digital":
        "ciencia, innovacion tecnologica y sociedad digital",
}


def ctte_key(name):
    """Committee names are quoted with and without their prefix, everywhere."""
    k = norm(re.sub(r"^comisi[oó]n\s+(de\s+|en\s+asuntos\s+de\s+|en\s+)?", "",
                    name.replace("(Art. 48)", ""), flags=re.I))
    return CTTE_ALIAS.get(k, k)


def nomina_diputados(per_par=2026):
    """-> [(comision_id, name, kind)] for the 19 committees of the Cámara.

    Three sources, because no single one carries all three facts:

    * `/comisiones/` on the chamber site names all 19 and splits them into
      ordinarias legislativas and ordinarias no legislativas -- and has no ids;
    * `periodo-parlamentario/{per_par}/filtros?codTipoParl=D` is the only place
      that says which *chamber* a committee belongs to, and it lists only the 16
      that can receive a bill, so the three no legislativas are simply absent;
    * `/comisiones` carries all 90 committees of every congress since 2021, ids
      included, with no chamber and no period field whatsoever.

    So the filtros pin down where the Cámara's block of ids starts, `/comisiones`
    turns names into ids inside that block, and the page decides which 19 are in
    it. A name that fails to resolve raises: quietly returning 16 of 19 would
    read exactly like success.
    """
    _, body = page("D", "comisiones")
    kind, names = None, []
    for m in re.finditer(r'class="(titulo-seccion|nombre-comision)">(.*?)</div>',
                         body, re.S):
        if m.group(1) == "titulo-seccion":
            kind = ("no legislativa" if "No Legislativa" in m.group(2)
                    else "legislativa")
        else:
            names.append((kind, re.sub(r"^\d+\.\s*", "", untag(m.group(2))).strip(" .")))

    bills = {c["id"] for c in spley.filtros(per_par, "D")["comisiones"]}
    if not bills:
        raise RuntimeError("filtros D sin comisiones: la API cambió de forma")
    ids = {}
    for c in api.spley("/comisiones"):
        if c["comisionId"] >= min(bills):      # the Cámara's block, 2026 onwards
            ids.setdefault(ctte_key(c["nombreComision"]),
                           []).append((c["comisionId"], c["nombreComision"]))

    out = []
    for kind, name in names:
        hit = ids.get(ctte_key(name))
        if not hit or len(hit) > 1:
            raise RuntimeError(f"«{name}» no resuelve a un id único: {hit}")
        # Keep the legislative system's own spelling: it is what the expediente
        # of a bill will say, and the whole point of carrying its id.
        out.append((hit[0][0], hit[0][1], kind))
    if len(bills - {i for i, _, k in out if k == "legislativa"}):
        raise RuntimeError("la página y los filtros no coinciden en las 16")
    return out


def ingest_nomina(con, per_par=2026):
    """The Cámara's 19 committees, keyed on the id the bills API uses.

    No members: the Cámara has approved no cuadro de comisiones, and the roster
    tables its site *does* serve under each committee's own URL are the Senado's
    Comisión de Constitución pasted 7 times over -- 23 senadores, byte for byte
    identical from one page to the next, linking to senado.congreso.gob.pe. They
    are placeholder text. Ingesting them would have given the Cámara de
    Diputados a committee system staffed entirely by senators.
    """
    urls = {p["slug"]: p["link"] for p in api.get_json(
        f"{WP.format(HOST['D'])}/pages?per_page=100&_fields=slug,link")}

    def page_url(slug):
        """The committee's own page, if it has one yet. Three slugs for one name:
        bare, prefixed "comision-de-", and either of those truncated to fit
        ("...afroperuano") or with the article glued on ("...-art-48")."""
        for cand in (slug, f"comision-de-{slug}"):
            hit = urls.get(cand) or next(
                (u for s, u in urls.items()
                 if s and len(s) > 20 and (s.startswith(cand) or cand.startswith(s))),
                None)
            if hit:
                return hit
        return None

    n = 0
    for cid, name, _kind in nomina_diputados(per_par):
        slug = slugify(name)
        db.upsert(con, "committee", {"id": cid, "per_par": per_par, "chamber": "D",
                                     "name": name, "slug": slug,
                                     "url": page_url(slug)})
        n += 1
    con.commit()
    return n


def mesas_senado():
    """-> [(committee name, [(mesa, person, bench)])] for the Senado's 11.

    One page, one table, one row per committee: nº, tipo, comisión, sesión de
    instalación, mesa directiva, horario. The Cámara publishes no equivalent --
    its Junta de Portavoces put "distribución de las presidencias,
    vicepresidencias y secretarías" on the agenda of 11/08/2026 and the agenda
    PDF carries only the heading, never the annex.
    """
    _, body = page("S", "mesas-directivas-y-horarios-de-sesiones-de-las-"
                        "comisiones-parlamentarias")
    body = re.sub(r"<style>.*?</style>", "", body, flags=re.S)
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        td = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(td) < 5:
            continue                                   # the header row
        mesa = [(MESA[c[0].lower()], untag(who), untag(bench))
                for c, who, bench in MESA_RE.findall(td[4])]
        if mesa:
            out.append((untag(td[2]), mesa))
    return out


def ingest_mesas(con, chamber="S"):
    """Stamp `mesa` onto the roster rows of whoever holds each committee office.

    Returns (committees, offices, inserted). `role` is deliberately left as the
    diario recorded it, even where the two sources contradict each other: the
    Senado has a suplente presiding a committee, which artículo 44 forbids, so
    somebody moved between the cuadro and the election of the mesa. Promoting
    him to titular to make the page read nicely put a thirteenth titular on a
    committee of twelve -- inventing a seat to hide a disagreement between two
    sources, when saying both is the whole job.

    `inserted` counts officers the cuadro does not seat on that committee at
    all. They land as amendment=1, which is what that flag means: a seat that
    postdates the approved cuadro and is not part of its twelve.
    """
    assert chamber == "S", "sólo el Senado publica sus mesas directivas"
    link, _ = page(chamber, "mesas-directivas-y-horarios-de-sesiones-de-las-"
                            "comisiones-parlamentarias")
    # Five committee ids exist only inside an oficio de cambio and have no roster
    # of their own; their names are near-copies of the real ones ("Comisión de
    # Defensa Nacional y Orden Interno" against "Comisión de Defensa Nacional"),
    # so matching against them would hang every mesa off a phantom.
    cand = {ctte_key(r["name"]): r["id"] for r in con.execute(
        "SELECT id, name FROM committee c WHERE chamber=? AND per_par=2026 "
        "AND EXISTS (SELECT 1 FROM committee_member m "
        "            WHERE m.committee_id=c.id AND m.amendment=0)", (chamber,))}
    people = {}
    for r in con.execute("SELECT id, last_name, full_name FROM legislator "
                         "WHERE chamber=?", (chamber,)):
        people[norm(r["full_name"])] = r["id"]
        people.setdefault(norm(r["last_name"] or ""), r["id"])

    nc = nm = ins = 0
    for name, mesa in mesas_senado():
        key = ctte_key(name)
        cid = cand.get(key)
        if cid is None:
            # containment before difflib: "control politico" is a prefix of
            # "control politico sobre actos normativos del poder ejecutivo y
            # regimenes de excepcion", which scores 0.55 on a ratio.
            hits = {v for k, v in cand.items() if k in key or key in k}
            near = difflib.get_close_matches(key, cand, n=1, cutoff=0.8)
            cid = hits.pop() if len(hits) == 1 else (cand[near[0]] if near else None)
        if cid is None and "bicameral" in key:
            # The Senado now publishes mesas for bicameral committees, which no
            # cuadro seats. Resolve the id against the bills API and let the
            # officers land as amendment=1 -- the flag for a seat outside an
            # approved cuadro -- instead of pretending the page said nothing.
            apic = {ctte_key(c["nombreComision"]): c
                    for c in api.spley("/comisiones")
                    if "bicameral" in ctte_key(c["nombreComision"])}
            near = difflib.get_close_matches(key, apic, n=1, cutoff=0.8)
            if near:
                c = apic[near[0]]
                db.upsert(con, "committee", {
                    "id": c["comisionId"], "per_par": 2026, "chamber": "C",
                    "name": c["nombreComision"],
                    "slug": slugify(c["nombreComision"])})
                cid = c["comisionId"]
        if cid is None:
            raise RuntimeError(f"mesa sin comisión: «{name}»")
        nc += 1
        for office, who, bench in mesa:
            lid = match(who, people)
            # Any row for that person on that committee, amendment or not: an
            # oficio row is how half of them got the seat in the first place.
            done = con.execute(
                "UPDATE committee_member SET mesa=? "
                "WHERE committee_id=? AND legislator_id=?",
                (office, cid, lid)).rowcount if lid else 0
            if not done:
                db.upsert(con, "committee_member", {
                    "committee_id": cid, "legislator_id": lid, "name_raw": who,
                    "bench": bench, "role": "titular", "mesa": office,
                    "amendment": 1, "source_url": link})
                ins += 1
            nm += 1
    con.commit()
    return nc, nm, ins


def demo_web():
    """The live half: the Cámara's nómina and the Senado's mesas directivas."""
    n = nomina_diputados()
    kinds = {}
    for _, _, k in n:
        kinds[k] = kinds.get(k, 0) + 1
    # Reglamento de la Cámara de Diputados, artículos 45 and 46: sixteen
    # ordinarias legislativas and three ordinarias no legislativas. A different
    # count means the Pleno created or merged one -- or the page broke.
    assert kinds == {"legislativa": 16, "no legislativa": 3}, kinds
    ids = [i for i, _, _ in n]
    assert len(set(ids)) == 19, ids
    got = {name for _, name, _ in n}
    for must in ("Constitución, Reglamento y Relaciones Exteriores",
                 "Ciencia, Innovación Tecnológica y Sociedad Digital",
                 "Acusaciones Constitucionales"):
        assert must in got, (must, sorted(got))

    m = mesas_senado()
    assert len(m) >= 11, [c for c, _ in m]
    offices = {o for _, mesa in m for o, _, _ in mesa}
    assert offices == set(MESA.values()), offices
    assert all(len(mesa) == 3 for _, mesa in m), \
        [(c, len(x)) for c, x in m if len(x) != 3]
    benches = {b for _, mesa in m for _, _, b in mesa}
    assert benches <= set(BENCHES), benches - set(BENCHES)

    con = db.connect()
    # Recording a mesa must not move a seat. An earlier version promoted the
    # suplente who presides Desarrollo Productivo to titular -- artículo 44 says
    # he cannot be one -- and left that committee with thirteen titulares out of
    # a house norm of twelve.
    seats = [r[0] for r in con.execute(
        "SELECT count(*) FROM committee_member m JOIN committee c "
        "ON c.id=m.committee_id WHERE c.chamber='S' AND m.role='titular' "
        "AND m.amendment=0 GROUP BY c.id")]
    assert not seats or seats.count(12) >= 8, seats
    stamped = dict(con.execute("SELECT mesa, count(*) FROM committee_member "
                               "WHERE mesa IS NOT NULL GROUP BY 1"))
    assert not stamped or stamped == {o: len(m) for o in MESA.values()}, stamped
    print(f"ok: {len(n)} comisiones de la Cámara "
          f"(ids {min(ids)}-{max(ids)} de la API de proyectos), "
          f"{sum(len(x) for _, x in m)} cargos de mesa en {len(m)} del Senado")


def demo():
    """`python3 -m ingest.committees` -- live sources, then the cached diario."""
    import pathlib
    demo_web()
    p = db.ROOT / "data" / "pdf" / "PLO-2026-3-SENADO.pdf"
    if not p.exists():
        # The diario is fetched by ingest.votes and cached under data/; on a cold
        # CI run it is simply not there yet. Skipping beats failing the build for
        # a missing input this module does not own.
        print("sin el diario en caché; nada que comprobar")
        return
    con = db.connect()
    names = frozenset(
        norm(x["last_name"] or x["full_name"])
        for x in con.execute("SELECT last_name, full_name FROM legislator "
                             "WHERE chamber='S'"))
    r = parse(text(p), names)
    assert not any(norm(k) in names for k in r), \
        [k for k in r if norm(k) in names]
    assert len(r) >= 8, f"only {len(r)} committees: {list(r)}"
    # every ordinary committee of this Senate seats exactly 12 titulares; a count
    # that drifts means a heading was missed and its members folded into the one
    # above, which is the failure mode this parser keeps rediscovering.
    sizes = {k: sum(1 for _, _, ro, a in v if ro == "titular" and not a)
             for k, v in r.items()}
    twelve = [k for k, n in sizes.items() if n == 12]
    assert len(twelve) >= 8, sizes
    eco = next(v for k, v in r.items() if "Economía" in k or "Economia" in k)
    names = {n for n, _, _, _ in eco}
    assert "Flores Ruíz" in names or "Flores Ruiz" in names, sorted(names)
    roles = {role for _, _, role, _ in eco}
    assert roles == {"titular", "suplente"}, roles
    benches = {b for _, b, _, a in eco if b and not a}
    assert len(benches) >= 4, benches
    assert not any("," in n for v in r.values() for n, *_ in v), "comma leaked"
    amended = sum(a for v in r.values() for *_, a in v)
    assert amended, "the Oficio amendment block was not detected"
    print(f"ok: {len(r)} comisiones, "
          f"{sum(len(v) for v in r.values())} asignaciones "
          f"({amended} por oficio de cambio), "
          f"{len(benches)} bancadas en Economía")
    _ = pathlib


if __name__ == "__main__":
    demo()
