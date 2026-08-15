"""The 130 diputados and 60 senadores, from the chambers' WordPress REST API.

Six taxonomies (grupo_parlamentario, partido_politico, distrito_electoral,
periodo_parlamentario, genero, condicion) are registered with show_in_rest=false,
so they are absent from /wp/v2/taxonomies and `?grupo_parlamentario=x` is accepted
and silently ignored. They leak anyway: get_post_class() writes every term slug
into `class_list`. That is the only structured source for party and district.

There is no shared id between WordPress and the bills API -- normalised name
matching is the only bridge (189/190 exact, one spelling variant).
"""
import re
import unicodedata

from . import api, db, spley
from .spley import slugify

CHAMBERS = {
    "D": ("diputados.congreso.gob.pe", "diputado"),
    "S": ("senado.congreso.gob.pe", "senador"),
}
PER_PAR = 2026


def norm(s):
    """Fold to ASCII lowercase for name matching: 'Barbarán' -> 'barbaran'."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z, ]", "", s.lower()).strip()


def terms(class_list, prefix):
    """Pull a taxonomy term slug out of the leaked WP class list."""
    for c in class_list or []:
        if c.startswith(prefix + "-"):
            return c[len(prefix) + 1:]
    return None


# Taxonomy slugs are ASCII, so the accents are gone and Title Case mangles the
# connectives. Both vocabularies are short and closed, so spell them out.
DISTRICTS = [
    "Amazonas", "Áncash", "Apurímac", "Arequipa", "Ayacucho", "Cajamarca",
    "Callao", "Cusco", "Huancavelica", "Huánuco", "Ica", "Junín", "La Libertad",
    "Lambayeque", "Lima", "Lima Provincias", "Loreto", "Madre de Dios",
    "Moquegua", "Pasco", "Piura", "Puno", "San Martín", "Tacna", "Tumbes",
    "Ucayali", "Peruanos en el Exterior", "Peruanos en el Extranjero",
    "Lima Metropolitana", "Nacional Distrito Único",
]
SMALL = {"de", "del", "la", "las", "el", "los", "por", "y", "en"}


def titleize(slug):
    if not slug:
        return None
    words = slug.split("-")
    return " ".join(w.title() if i == 0 or w not in SMALL else w
                    for i, w in enumerate(words))


def pretty(slug, vocab):
    """Recover the accented display form of a slug from a known vocabulary."""
    if not slug:
        return None
    want = norm(slug.replace("-", " "))
    return next((v for v in vocab if norm(v) == want), titleize(slug))


def roster(chamber):
    """All members of one chamber. 2 requests for D, 1 for S."""
    host, post_type = CHAMBERS[chamber]
    out, page = [], 1
    while True:
        # no _fields: WP drops _embedded whenever _fields is present
        rows = api.get_json(
            f"https://{host}/wp-json/wp/v2/{post_type}"
            f"?per_page=100&page={page}&_embed=wp:featuredmedia")
        out += rows
        if len(rows) < 100:
            return out
        page += 1


def photo(rec):
    media = (rec.get("_embedded") or {}).get("wp:featuredmedia") or []
    # Filenames are not derivable from the slug -- one is spelt 'Bermedo' for
    # a legislator slugged 'bernedo'. Always read it off the embed.
    return media[0].get("source_url") if media else None


def contact(link):
    """Email lives only in the rendered profile page, not in REST."""
    try:
        html = api.fetch(link).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a missing profile must not stop the run
        return None, None
    email = re.search(r"[\w.+-]+@congreso\.gob\.pe", html)
    votes = re.search(r"Votaci[oó]n obtenida.{0,200}?([\d,\.]{3,})", html, re.S)
    return (email.group(0) if email else None,
            votes.group(1) if votes else None)


def ingest(con, chamber):
    """Roster -> `legislator`, joined to the bills API congresistaId by name.

    Always fetches the profile pages. There used to be a flag to skip them; since
    upsert is INSERT OR REPLACE, calling with it set silently blanked the email of
    every member already stored. 190 extra GETs is cheaper than that.
    """
    f = spley.filtros(PER_PAR, chamber)
    autores = {norm(a["descripcion"]): a["id"] for a in f["autores"]}
    # the bills API spells the bench names properly; the senate list is partial,
    # so borrow the diputados one, which carries all six.
    groups = [g["descripcion"] for g in
              spley.filtros(PER_PAR, "D")["gruposParlamentarios"]]
    # surname-only fallback closes the one spelling variant between the sources
    by_surname = {k.split(",")[0]: v for k, v in autores.items()}

    n = matched = 0
    for r in roster(chamber):
        name = r["title"]["rendered"].strip()
        key = norm(name)
        cid = autores.get(key) or by_surname.get(key.split(",")[0])
        matched += cid is not None
        last, _, first = name.partition(",")
        email, votes = contact(r["link"])
        db.upsert(con, "legislator", {
            "id": f"{PER_PAR}-{chamber}-{r['id']}",
            "per_par": PER_PAR,
            "chamber": chamber,
            "codigo": cid,
            "full_name": name,
            "slug": r["slug"],
            "last_name": last.strip() or None,
            "first_name": first.strip() or None,
            "party": pretty(terms(r.get("class_list"), "grupo_parlamentario"), groups),
            "district": pretty(terms(r.get("class_list"), "distrito_electoral"),
                               DISTRICTS),
            "photo_url": photo(r),
            "source_url": r["link"],
            "active": int(terms(r.get("class_list"), "condicion") != "electo"),
            "email": email,
            "votes_received": votes,
            "bio": (r.get("content") or {}).get("rendered") or None,
        })
        n += 1
    con.commit()
    return n, matched


def ingest_prior(con, per_par=2021):
    """Names-only roster for a past period, from the bills API author filter.

    The chamber websites were replaced at the handover and no roster endpoint for
    2021-2026 survives, so this is all that is left: name and congresistaId, no
    party, district or photo. Without it the 14,864 bills of that period render
    every author as dead text, which is 99.7% of the corpus.
    """
    n = 0
    for a in spley.filtros(per_par)["autores"]:
        name = a["descripcion"].strip()
        last, _, first = name.partition(",")
        db.upsert(con, "legislator", {
            "id": f"{per_par}-C-{a['id']}", "per_par": per_par, "chamber": "C",
            "codigo": a["id"], "full_name": name,
            "slug": f"{slugify(name)}-{per_par}",
            "last_name": last.strip() or None, "first_name": first.strip() or None,
            "active": 0,
        })
        n += 1
    con.commit()
    return n


def link_sponsors(con):
    """Fill bill_sponsor.legislator_id / motion_signer.legislator_id by name."""
    people = {}
    for r in con.execute("SELECT id, full_name, per_par FROM legislator"):
        people.setdefault(norm(r["full_name"]), {})[r["per_par"]] = r["id"]
    total = 0
    for table, key in (("bill_sponsor", "bill_id"), ("motion_signer", "motion_id")):
        rows = con.execute(
            f"SELECT s.rowid, s.name_raw, b.per_par FROM {table} s "
            f"JOIN {'bill' if table == 'bill_sponsor' else 'motion'} b "
            f"ON b.id = s.{key}").fetchall()
        for r in rows:
            m = people.get(norm(r["name_raw"]))
            if not m:
                continue
            # prefer the period the bill belongs to, else whoever carries the name
            lid = m.get(r["per_par"]) or next(iter(m.values()))
            con.execute(f"UPDATE {table} SET legislator_id=? WHERE rowid=?",
                        (lid, r["rowid"]))
            total += 1
    con.commit()
    return total


def demo():
    """`python3 -m ingest.legislators` -- hits the live API."""
    assert norm("Barbarán Reyes, Rosangella") == "barbaran reyes, rosangella"
    d, s = roster("D"), roster("S")
    assert len(d) == 130, len(d)
    assert len(s) == 60, len(s)
    assert photo(d[0]), "no featured media on first diputado"
    parties = {terms(r.get("class_list"), "grupo_parlamentario") for r in d}
    assert "fuerza-popular" in parties, parties
    ids = {a["id"] for a in spley.filtros(PER_PAR, "D")["autores"]}
    assert len(ids) == 130, len(ids)
    print(f"ok: {len(d)} diputados, {len(s)} senadores, "
          f"{len(parties)} groups, {len(ids)} authors in the bills API")


if __name__ == "__main__":
    demo()
