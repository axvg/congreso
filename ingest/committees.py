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
"""
import difflib
import re
import subprocess

from . import db
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
    known = {r["slug"]: r["id"] for r in con.execute("SELECT id, slug FROM committee")}
    nxt = (con.execute("SELECT coalesce(max(id),0) FROM committee").fetchone()[0]
           or 0) + 1
    nc = nm = unmatched = 0
    for name, members in rosters.items():
        slug = slugify(name)
        cid = known.get(slug)
        if cid is None:
            cid = nxt = max(nxt, 10_000) + 1
            known[slug] = cid
            db.upsert(con, "committee", {
                "id": cid, "per_par": 2026, "chamber": chamber,
                "name": name, "slug": slug})
        nc += 1
        for raw, bench, role, amend in members:
            lid = match(raw, people)
            unmatched += lid is None
            db.upsert(con, "committee_member", {
                "committee_id": cid, "legislator_id": lid, "name_raw": raw,
                "bench": bench, "role": role, "amendment": amend,
                "source_url": str(pdf)})
            nm += 1
    con.commit()
    return nc, nm, unmatched


def demo():
    """`python3 -m ingest.committees` -- parses the cached Senate diario."""
    import pathlib
    p = db.ROOT / "data" / "pdf" / "PLO-2026-3-SENADO.pdf"
    assert p.exists(), f"missing {p}"
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
