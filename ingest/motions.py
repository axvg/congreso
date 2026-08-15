"""Motions (mociones de orden del día) from smociones-portal-service.

The only source with real, daily-growing bicameral volume: the new Congress has
filed far more motions than bills. Same envelope and paging convention as spley,
different base and a `codTipoParl` that is mandatory everywhere.
"""
import datetime as dt

from . import api, db

BASE = "https://api.congreso.gob.pe/smociones-portal-service"
PER_PAR = 2026


def call(path, data=None):
    r = api.get_json(f"{BASE}{path}", data=data)
    if r.get("status") != "success":
        raise RuntimeError(f"{path}: {r.get('code')} {r.get('status')}")
    return r["data"]


def listing(chamber, per_par=PER_PAR, page=200):
    start = 0
    while True:
        d = call("/mocion/lista-con-filtros",
                 {"codTipoParl": chamber, "perParId": per_par,
                  "pageSize": page, "rowStart": start})
        rows = d.get("mociones") or []
        if not rows:
            return
        yield from rows
        start += len(rows)
        if start >= d.get("rowsTotal", 0):
            return


def detail(chamber, num, per_par=PER_PAR):
    return call(f"/mocion/{chamber}/{per_par}/{num}")


def split_authors(raw):
    """' (P)Melgar Valdez, Elard Galo (Fuerza Popular);...' -> [(name, primary)].

    A leading (P) marks the primary signer; the trailing parenthesis is the bench.
    """
    out = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part:
            continue
        primary = part.startswith("(P)")
        part = part[3:] if primary else part
        name = part.split("(")[0].strip().rstrip(",")
        if name:
            out.append((name, primary))
    return out


def ingest(con, chamber, per_par=PER_PAR):
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    n = 0
    for m in listing(chamber, per_par):
        mid = f"{per_par}-{chamber}-{m['mocionNum']}"
        db.upsert(con, "motion", {
            "id": mid,
            "per_par": per_par,
            "chamber": chamber,
            "num": m["mocionNum"],
            "code": m.get("mocion"),
            "kind": m.get("desTipoMocion"),
            "summary": m.get("sumilla"),
            "status": m.get("desEstadoMocion"),
            "party": m.get("desGpar"),
            "presented_on": (m.get("fecPresentacion") or "")[:10] or None,
            "authors_raw": m.get("autores"),
            "fetched_at": now,
        })
        for i, (name, primary) in enumerate(split_authors(m.get("autores"))):
            db.upsert(con, "motion_signer", {
                "motion_id": mid, "name_raw": name,
                "rank": 0 if primary else i + 1})
        n += 1
    con.commit()
    return n


def demo():
    """`python3 -m ingest.motions` -- hits the live API."""
    rows = list(listing("S"))
    assert rows, "no senate motions"
    assert rows[0]["mocion"].endswith("-2026-2031-S"), rows[0]["mocion"]
    d = list(listing("D"))
    assert len(d) > len(rows), (len(d), len(rows))
    a = split_authors(" (P)Melgar Valdez, Elard Galo (Fuerza Popular)")
    assert a == [("Melgar Valdez, Elard Galo", True)], a
    det = detail("S", rows[0]["mocionNum"])
    assert det, "empty detail"
    print(f"ok: {len(d)} diputados motions, {len(rows)} senado, "
          f"detail keys {sorted(det)[:5]}")


if __name__ == "__main__":
    demo()
