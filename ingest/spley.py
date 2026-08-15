"""Bills (proyectos de ley) from api.congreso.gob.pe/spley-portal-service.

The `expediente` (full dossier: timeline, commissions, documents) takes its two
path segments AES-128-ECB encrypted with a key shipped in the Angular bundle.
Spring mangles `+` and `/` inside a path segment, so a ciphertext containing
either 400s. Ciphertext is deterministic, but the plaintext is parsed as an int
server-side -- so zero-padding ("2021" -> "002021") is a free way to reroll the
ciphertext until it is URL-clean. See `enc()`.
"""
import base64
import datetime as dt
import re
import unicodedata

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import api, db

KEY = b"ProdALg5ZrAsxBMD"  # environment.ENCRYPTION_KEY, spley-portal main bundle
CHAMBERS = {"C": "C", "D": "D", "S": "S"}


def _aes(plain: str) -> str:
    pad = 16 - len(plain) % 16
    data = (plain + chr(pad) * pad).encode()
    e = Cipher(algorithms.AES(KEY), modes.ECB()).encryptor()
    return base64.b64encode(e.update(data) + e.finalize()).decode()


def enc(n) -> str:
    """URL-safe ciphertext for an integer-valued path segment.

    Each extra zero rerolls the ciphertext, and roughly a quarter of rolls carry
    a `+` or `/`. Stopping at 8 tries left a handful of bills permanently
    unreachable -- 13682 needs exactly the 8th. 24 makes a miss about one in
    10^14; if it ever fires, the bill is genuinely unfetchable and should say so.
    """
    s = str(n)
    for pad in range(0, 24):
        c = _aes("0" * pad + s)
        if "+" not in c and "/" not in c:
            return c
    raise RuntimeError(f"no clean ciphertext for {n}")


def expediente(per_par, num, chamber="C"):
    """Full dossier. The route calls its first segment `:anio`, but the value the
    service actually wants is the perParId -- passing a bill's year 404s."""
    return api.get_json(
        f"{api.SPLEY}/expediente/{enc(per_par)}/{enc(num)}?codTipoParl={chamber}")


def periods():
    return api.spley("/periodo-parlamentario")


def filtros(per_par, chamber=None):
    """Filter vocabulary for a period: commissions, groups, and the author roster
    (`autores[].id` is the congresistaId that `lista-con-filtro` accepts)."""
    q = f"?codTipoParl={chamber}" if chamber else ""
    return api.spley(f"/periodo-parlamentario/{per_par}/filtros{q}")


def bill_id(per_par, chamber, ply_num):
    """Bill numbers restart per chamber, so deputy bill 4 and senate bill 4 both
    exist in 2026-2031. The chamber has to be part of the key."""
    return f"{per_par}-{chamber or 'C'}-{ply_num}"


def split_authors(raw):
    """'Luque Ibarra, Ruth; Bazan Narro, Sigrid' -> ordered list."""
    return [a.strip() for a in (raw or "").split(";") if a.strip()]


def ingest_list(con, per_par, per_leg=None, chamber=None, cap=100_000):
    """Walk the paged bill list into `bill` + `bill_sponsor`. Returns count.

    From 2026 on, `codTipoParl` is mandatory: omit it and the bicameral periods
    return an empty list rather than an error, which reads exactly like a
    congress that has filed nothing.
    """
    filt = {"perParId": per_par}
    if per_leg:
        filt["perLegId"] = per_leg
    if chamber:
        filt["codTipoParl"] = chamber
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    n = 0
    for p in api.paged("/proyecto-ley/lista-con-filtro", filt, cap=cap):
        ch = p.get("codTipoParlActual") or p.get("codTipoParl")
        bid = bill_id(p["perParId"], ch, p["pleyNum"])
        db.upsert(con, "bill", {
            "id": bid,
            "per_par": p["perParId"],
            "per_leg": per_leg,
            "ply_num": p["pleyNum"],
            "code": p.get("proyectoLey"),
            "chamber": ch,
            "title": p.get("titulo"),
            "status": p.get("desEstado"),
            "proponent": p.get("desProponente"),
            "presented_on": (p.get("fecPresentacion") or "")[:10] or None,
            "authors_raw": p.get("autores"),
            "fetched_at": now,
        })
        for i, name in enumerate(split_authors(p.get("autores"))):
            db.upsert(con, "bill_sponsor",
                      {"bill_id": bid, "name_raw": name, "rank": i})
        n += 1
        if n % 200 == 0:
            con.commit()
    con.commit()
    return n


def doc_url(archivo_id):
    """Public PDF route for an attached document.

    `/archivo/uuid/{uuid}` 400s with "Token de captcha no proporcionado"; the
    numeric-id route the viewer uses for inline display is open. The id is
    base64'd exactly as the bundle does it (btoa).
    """
    tok = base64.b64encode(str(archivo_id).encode()).decode()
    return f"{api.SPLEY}/archivo/{tok}/pdf"


def slugify(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s)).strip("-")


# An action can carry several files and the first is not always the bill: a
# PRESENTADO whose first attachment is an "OFICIO ... (ADHESIÓN)" is somebody
# signing on, not the text. Prefer anything that is not obviously an annex.
NOT_THE_BILL = ("oficio", "adhesi", "derivacion", "derivación", "decreto",
                "dictamen", "autografa", "autógrafa", "acumula")


def pick_doc(archivos):
    files = list(archivos or [])
    if not files:
        return None
    plain = [f for f in files
             if not any(w in (f.get("nombreArchivo") or "").lower()
                        for w in NOT_THE_BILL)]
    return (plain or files)[0]


def save_expediente(con, per_par, chamber, ply_num, d):
    """Store one fetched dossier: summary, status, and the action timeline."""
    if d.get("status") != "success":
        return 0
    d = d["data"]
    g, bid = d["general"], bill_id(per_par, chamber, ply_num)
    con.execute(
        "UPDATE bill SET summary=?, status=?, title=coalesce(?,title) WHERE id=?",
        (g.get("sumilla"), g.get("desEstado"), g.get("titulo"), bid))
    for c in d.get("comisiones") or []:
        db.upsert(con, "committee", {
            "id": c["comisionId"], "per_par": per_par, "chamber": chamber,
            "name": c["nombre"], "slug": slugify(c["nombre"]),
            "url": c.get("pagWeb")})
        db.upsert(con, "bill_committee",
                  {"bill_id": bid, "committee_id": c["comisionId"]})
    n = 0
    for s in d.get("seguimientos") or []:
        doc = pick_doc(s.get("archivos"))
        db.upsert(con, "bill_action", {
            "bill_id": bid,
            "acted_on": (s.get("fecha") or "")[:10],
            "text": s.get("desEstado"),
            # The uuid route is behind reCAPTCHA ("Token de captcha no
            # proporcionado"). The numeric id route is the one the viewer itself
            # uses to render inline, and is open -- so keep the id, not the uuid.
            "doc_id": doc["proyectoArchivoId"] if doc else None,
            "doc_name": doc.get("nombreArchivo") if doc else None,
            "doc_url": doc_url(doc["proyectoArchivoId"]) if doc else None,
        })
        n += 1
    return n


def demo():
    """Self-check against live data. `python3 -m ingest.spley`"""
    assert enc(2021) == _aes("002021"), "padding reroll changed"
    assert "+" not in enc(2021) and "/" not in enc(2021)
    d = expediente(2021, 2730)
    assert d["status"] == "success", d
    g = d["data"]["general"]
    assert g["proyectoLey"] == "02730/2021-CR", g
    assert len(d["data"]["seguimientos"]) > 3
    ps = {p["perParId"] for p in periods()}
    assert {2021, 2026} <= ps, ps
    print(f"ok: expediente {g['proyectoLey']} "
          f"({len(d['data']['seguimientos'])} actions), periods {sorted(ps)}")


if __name__ == "__main__":
    demo()
