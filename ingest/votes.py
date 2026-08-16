"""Roll-call votes, from the electronic tally boards *and* the diario de debates.

Both chambers publish, per plenary session, a PDF of the electronic board (an
attendance page plus one or more roll calls) and, days later, a `PLO-*.pdf`
transcript. The board is stamped "INFORMACIÓN PROVISIONAL / SIN LOS VOTOS ORALES"
and it means it: on 12/08/2026 the board printed 38-13-3 and the floor closed at
40-13-4 after three votes were read into the record. The transcript wins. Parsing
only the board is how you publish a wrong vote.

Board layouts, both with a real text layer:
  A ("VOTACIÓN NOMINAL") prints the same literal "P" under SI, NO and ABST. alike --
    the vote is the mark's *x position*, so rows come from `pdftotext -bbox-layout`
    and every mark is snapped to a column header inside its own block.
  B ("VOTACIÓN:") spells the position out, but long names eat the column gap, so it
    reads bbox words too and segments on the position token.

Two traps, both confirmed. The Diputados 05/08 board says "SENADO DE LA REPÚBLICA"
on its second roll call, so a vote's chamber comes from the host, never the header.
And `Asistencia_Congreso_*` on the senado host carries the *Diputados* roster, so
attendance takes its chamber from the roster each name resolves against instead.
"""
import collections
import datetime as dt
import hashlib
import html
import json
import pathlib
import re
import subprocess
import tempfile
import unicodedata
import urllib.parse

from . import api, db

HOSTS = {"D": "https://diputados.congreso.gob.pe", "S": "https://senado.congreso.gob.pe"}
PDF_DIR = db.ROOT / "data" / "pdf"

# Generous on purpose -- filenames lie, so the parser decides what a file really is.
# PLO-* is the diario de debates and matches none of the attendance words.
NAME_RE = re.compile(r"asistencia|votacion|votación|^PLO-", re.I)
WORD_RE = re.compile(
    r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>')

MONTHS = {m: i for i, m in enumerate(
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre "
    "noviembre diciembre".split(), 1)}
MONTHS["setiembre"] = 9

# Layout A legend: P (Presente) AUS (Ausente) LO (Licencia oficial) LE (enfermedad)
# LP (personal) L (sin goce de haber). Only "P" carries a position, and it carries
# it in x -- never in the text.
MARK_A = {"P", "LP", "LE", "LO", "L", "AUS"}
POS_B = {"SI": "SI", "NO": "NO", "ABST": "ABST", "Abst.": "ABST", "AUS": "AUSENTE",
         "LO": "LICENCIA", "LE": "LICENCIA", "LP": "LICENCIA", "L": "LICENCIA",
         "SinRes": "SINRES", "***": "PRESIDENCIA"}
ATTEND = {"PRE", "AUS", "LO", "LE", "LP", "L"}
CAST = ("SI", "NO", "ABST")
# One bench, two codes: layout A prints PCO, layout B and the attendance pages OBRAS.
PARTY_ALIAS = {"OBRAS": "PCO"}

ONES = {w: i for i, w in enumerate(
    "cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece "
    "catorce quince dieciseis diecisiete dieciocho diecinueve".split())}
ONES.update({"ninguno": 0, "ninguna": 0, "un": 1, "una": 1})
TENS = {"veinte": 20, "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60,
        "setenta": 70, "ochenta": 80, "noventa": 90, "cien": 100, "ciento": 100}

_N = r"([\wáéíóúñ]+(?: y [\wáéíóúñ]+)?)"
TALLY_RE = re.compile(
    r"Efectuada la votaci[óo]n[^,]*, se (aprueba|rechaza|desaprueba|acuerda)[^,]*, "
    rf"por {_N} votos? a favor,? {_N} en contra y {_N} abstenci", re.I)
FINAL_RE = re.compile(rf"{_N} votos a favor, {_N} en contra y {_N} abstenciones", re.I)
CORR_RE = re.compile(r"([A-ZÁÉÍÓÚÑ][A-Za-zÁÉÍÓÚÑáéíóúñ]+), (a favor|en contra|"
                     r"abstenci[óo]n)\b")
PRES_RE = re.compile(r"voto (a favor|en contra) de la presidencia")
CONSTANCIA_RE = re.compile(   # '*' ends it: the page footnote starts with ***
    r"deja constancia de la votaci[óo]n (a favor|en contra|en abstenci[óo]n) de l[oa]s "
    r"(?:diputad|senador|congresist)\w+ ([^.*]+)", re.I)
DIRECTION = {"a favor": "SI", "en contra": "NO", "abstención": "ABST",
             "abstencion": "ABST", "en abstención": "ABST", "en abstencion": "ABST"}


def media(chamber):
    """Yield every media item of a chamber, walking the X-WP-TotalPages header."""
    page, pages = 1, 1
    while page <= pages:
        h = {}
        items = json.loads(api.fetch(
            f"{HOSTS[chamber]}/wp-json/wp/v2/media?per_page=100&page={page}"
            "&_fields=id,date,source_url,mime_type", out=h))
        pages = int(h.get("X-WP-TotalPages") or 1)
        yield from items
        page += 1


def cached(url, offline=False):
    """Mirror the PDF under data/pdf/. Re-fetched every run on purpose: every board
    is stamped PROVISIONAL and gets replaced in place at the same URL, so a cache
    that never revalidates is a cache that serves a superseded vote forever.

    ponytail: unconditional GET of ~25 small PDFs. Store the ETag alongside and send
    If-None-Match once that is too much traffic.
    """
    p = PDF_DIR / urllib.parse.unquote(url.rsplit("/", 1)[-1])
    if offline and p.exists():
        return p
    body = api.fetch(url)
    if not p.exists() or p.read_bytes() != body:
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    return p


def _fold(s):
    """Accent-free upper case. 'Vásquez Chuquilín' -> 'VASQUEZ CHUQUILIN'."""
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").upper()


def norm(name):
    """'AGUINAGA RECUENCO,' -> ['AGUINAGA', 'RECUENCO']. Punctuation dropped."""
    return re.sub(r"[^A-Za-z ]", " ", _fold(name)).split()


def es_num(s):
    """'38' | 'tres' | 'cuarenta y dos' | 'ninguno' -> int. The diarios mix digits
    and spelled-out numbers inside one sentence."""
    words = _fold(s).lower().replace(" y ", " ").split()
    if words and words[0].isdigit():
        return int(words[0])
    total, seen = 0, False
    for w in words:
        if w in ONES:
            total, seen = total + ONES[w], True
        elif w in TENS:
            total, seen = total + TENS[w], True
        elif w.startswith("veinti") and w[6:] in ONES:
            total, seen = total + 20 + ONES[w[6:]], True
        else:
            break
    return total if seen else None


def _text(pdf):
    """Per-page plain text, for the headers and the printed tally."""
    return subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                          capture_output=True, text=True).stdout.split("\f")


def _flat(pdf):
    """The whole document as one whitespace-collapsed line, for prose regexes."""
    return re.sub(r"\s+", " ", "\n".join(_text(pdf)))


def _words(pdf):
    """Per page, [(x0, y0, x1, y1, text)] sorted top-to-bottom, left-to-right."""
    xml = subprocess.run(["pdftotext", "-bbox-layout", str(pdf), "-"],
                         capture_output=True, text=True).stdout
    out = []
    for chunk in xml.split("<page ")[1:]:
        ws = []
        for a, b, c, d, t in WORD_RE.findall(chunk):
            a, b, c, d = float(a), float(b), float(c), float(d)
            # ponytail: the rotated "COPIA INFORMATIVA" watermark drops out on glyph
            # height alone (63pt vs 8pt body text). If a chamber ever ships a small
            # watermark, filter on the rotation matrix from `pdftotext -bbox` instead.
            if d - b > 12:
                continue
            ws.append((a, b, c, d, html.unescape(t)))
        out.append(sorted(ws, key=lambda w: (w[1], w[0])))
    return out


def _lines(words, tol=4.0):
    """Cluster words into visual rows -- a mark sits ~1pt above its own name."""
    out, cur = [], []
    for w in words:
        if cur and w[1] - cur[0][1] > tol:
            out.append(sorted(cur, key=lambda z: z[0]))
            cur = []
        cur.append(w)
    if cur:
        out.append(sorted(cur, key=lambda z: z[0]))
    return out


def _party(code):
    return PARTY_ALIAS.get(code, code)


def _zs(tokens):
    """z and s are interchangeable in these names: the diario writes Vázquez, the
    roster Vásquez, and the board truncates both."""
    return [t.replace("Z", "S") for t in tokens]


def _scan(words, vocab, stop=()):
    """[(party, name, token)] for a grid whose last cell is a literal token."""
    out = []
    for line in _lines(words):
        t = [w[4] for w in line]
        if any(s in t for s in stop):
            break
        cur = []
        for tok in t:
            if tok in ("+++", "---"):
                continue                 # decoration hung off SI / NO
            if tok in vocab and cur:
                if (len(cur) >= 2 and cur[0].isalpha() and cur[0].isupper()
                        and len(cur[0]) <= 6):
                    out.append((_party(cur[0]), " ".join(cur[1:]), tok))
                cur = []
            else:
                cur.append(tok)
    return out


def _blocks(words):
    """The (SI, NO, ABST) column centres of each side-by-side block on a layout A
    page, as [[(x, label), ...]], plus the block's column pitch."""
    head = [w[1] for w in words if w[4] == "ABST."]
    if not head:
        return []
    hy = min(head)
    anchors = sorted(((w[0] + w[2]) / 2, {"SI": "SI", "NO": "NO", "ABST.": "ABST"}[w[4]])
                     for w in words
                     if w[4] in ("SI", "NO", "ABST.") and abs(w[1] - hy) < 2)
    out = []
    for i in range(0, len(anchors) - 2, 3):
        b = anchors[i:i + 3]
        out.append((b, (b[2][0] - b[0][0]) / 2))
    return out


def rows_a(words):
    """Layout A -> [(party, name, position)]. Position decoded from the mark's x."""
    blocks = _blocks(words)
    if not blocks:
        return []
    hy = min(w[1] for w in words if w[4] == "ABST.")
    ents = []
    for line in _lines(words):
        if line[0][1] < hy + 8:
            continue
        ent = None
        for w in line:
            if w[4].isdigit() and w[4][0] != "0":  # the N.° column opens an entry
                if ent:
                    ents.append(ent)
                ent = [w]
            elif ent:
                ent.append(w)
        if ent:
            ents.append(ent)
    out = []
    for ent in ents:
        t = [w[4] for w in ent]
        # drops the tally line repeated under every page's column headers
        if len(t) < 3 or not (t[1].isalpha() and len(t[1]) <= 6):
            continue
        mark = ent[-1] if t[-1] in MARK_A else None
        name = " ".join(t[2:-1] if mark else t[2:])
        if not name:
            continue
        if mark is None:
            pos = "BLANCO"     # the cell is empty: not a vote, not an excuse either
        elif mark[4] == "AUS":
            pos = "AUSENTE"
        elif mark[4] != "P":
            pos = "LICENCIA"
        else:
            # Snap inside the mark's own block only, and at a quarter of the column
            # pitch -- measured drift is under 0.4pt against a 20.3pt pitch, so a
            # half-pitch window (the obvious choice) can never reject anything that
            # landed in the grid and would wave a misplaced mark straight through.
            cx = (mark[0] + mark[2]) / 2
            near = [(abs(cx - a), lab) for b, pitch in blocks for a, lab in b
                    if b[0][0] - pitch <= cx <= b[2][0] + pitch
                    and abs(cx - a) <= pitch / 4]
            pos = min(near)[1] if near else "BLANCO"
        out.append((_party(t[1]), name, pos))
    return out


def rows_b(words):
    """Layout B -> [(party, name, position)]. Segments on the position token."""
    return [(p, n, POS_B[tok])
            for p, n, tok in _scan(words, POS_B, ("Parlamentario", "Resultado"))]


def _pick(lines, label):
    """The number printed to the right of `label` in the summary block. Read from
    bbox rows, not -layout text: layout B renders the labels and their numbers as
    two separate text columns, so they land on different -layout lines."""
    lt = label.split()
    for line in lines:
        t = [w[4] for w in line]
        for i in range(len(t) - len(lt) + 1):
            if t[i:i + len(lt)] == lt:
                for x in t[i + len(lt):]:
                    if x.isdigit():
                        return int(x)
    return None


def _tally(lines, layout):
    """(yes, no, abstain, absent) exactly as the board prints them about itself."""
    labels = (("SI", "NO", "ABSTENCIONES", "AUSENTES") if layout == "A" else
              ("A FAVOR (SI)", "EN CONTRA (NO)", "ABSTENCIÓN", "AUSENTE"))
    return tuple(_pick(lines, l) for l in labels)


def _held_on(text):
    m = re.search(r"Fecha:\s*(\d{2})/(\d{2})/(\d{4})", text)
    if m:
        return f"{m[3]}-{m[2]}-{m[1]}"
    m = re.search(r"(\d{1,2}) de (\w+) de (\d{4})", text, re.I)
    if not m:
        m = re.search(r"(\d{1,2}) DE ([A-ZÁÉÍÓÚ]+) DE (\d{4})", _fold(text))
    if m and _fold(m[2]).lower() in MONTHS:
        return f"{m[3]}-{MONTHS[_fold(m[2]).lower()]:02d}-{int(m[1]):02d}"
    return None


# The board PDFs print the tally table's column headers vertically, one letter
# per line. `pdftotext` reads them as a horizontal run of isolated letters glued
# to the end of the subject: «...APROBADA POR AMPLIA MAYORÍA V A T I A R M F O
# IN I A P C O». Four or more one- and two-letter tokens in a row at the very
# end are never prose, so they come off. Anchored at the end on purpose: «B)»
# and «Y» inside a sentence keep their place.
TRAIL_COLS = re.compile(r"(?:\s+[A-ZÁÉÍÓÚÜÑ]{1,2}\b){4,}\s*$")


def _subject(text):
    """One subject line, whitespace collapsed and the vertical column headers
    taken off. Every subject this module writes goes through here."""
    s = re.sub(r"\s+", " ", text or "").strip()
    return TRAIL_COLS.sub("", s).strip() or None


def _asunto(text):
    m = re.search(r"(?:ASUNTO|Asunto):\s*(.+?)\n\s*\n", text, re.S)
    return _subject(m[1]) if m else None


def rollcalls(pdf):
    """Split a board PDF into roll calls: [(layout, asunto, [page indexes])].

    Layout 'M' is a vote taken a mano alzada after the electronic system failed --
    real, minuted, and with no nominal list to publish. Attendance-only pages carry
    none of the three markers and never become a vote.
    """
    out = []
    for i, t in enumerate(_text(pdf)):
        lay = ("A" if "VOTACIÓN NOMINAL" in t else "B" if "VOTACIÓN:" in t
               else "M" if "MANO ALZADA" in t else None)
        if not lay:
            continue
        key = (lay, _asunto(t))
        if out and out[-1][0] == key and out[-1][1][-1] == i - 1 and lay != "M":
            out[-1][1].append(i)
        else:
            out.append((key, [i]))
    return [(k[0], k[1], idx) for k, idx in out]


def _constancia(text, seats):
    """Apply "el presidente ... deja constancia de la votación a favor de los
    diputados X, Y y Z" -- an oral vote printed under the roster, which the grid
    above it does not carry. Only rows the board did not already count can move."""
    fixed, moved = list(seats), set()
    for direction, names in CONSTANCIA_RE.findall(re.sub(r"\s+", " ", text)):
        pos = DIRECTION[_fold(direction).lower()]
        for raw in re.split(r",| y ", names):
            want = norm(raw)
            if not 1 < len(want) <= 4:   # a surname pair, never a stray sentence
                continue
            for i, (party, name, p) in enumerate(fixed):
                if p not in CAST and _zs(norm(name)[:len(want)]) == _zs(want):
                    fixed[i] = (party, name, pos)
                    moved.add(name)
                    break
    return [(p, n, pos, "constancia" if n in moved else "grid")
            for p, n, pos in fixed]


def parse(pdf, chamber, url, ordinals):
    """[(vote, [(party, name, position, source)])] for one board PDF, or None when
    the file has no text layer. `ordinals` counts roll calls already seen per day so
    a vote id stays (chamber, date, ordinal) rather than keyed on bytes."""
    texts = _text(pdf)
    if not any(t.strip() for t in texts):
        return None        # a "_Final" rescan: image only, legacy OCR is out of scope
    words = _words(pdf)
    doc_date = _held_on("\n".join(texts))
    provisional = int(bool(re.search(r"INFORMACI[ÓO]N PROVISIONAL|SIN LOS VOTOS ORALES",
                                     _fold("\n".join(texts))))
                      or "provisional" in pdf.name.lower())
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    out = []
    for layout, asunto, idx in rollcalls(pdf):
        text = "\n".join(texts[i] for i in idx)
        held = _held_on(text) or doc_date
        k = ordinals[chamber, held]
        ordinals[chamber, held] += 1
        chair = re.search(r"Presiden\w*:\s*([^\n]+)", text)
        v = {
            "id": f"{chamber}-{held}-{k}",
            "per_par": 2026,   # ponytail: the only bicameral period so far; read it
                               # off held_on against /periodo-parlamentario if it isn't
            "chamber": chamber,
            "held_on": held,
            # ponytail: no presided_by column, and the chair is worth keeping --
            # parked in `session` until someone needs to query it.
            "session": f"Presidencia: {chair[1].strip()}" if chair else None,
            "subject": asunto, "result": None,
            "n_yes": None, "n_no": None, "n_abstain": None, "n_absent": None,
            "n_yes_final": None, "n_no_final": None, "n_abstain_final": None,
            "provisional": provisional, "final_source_url": None,
            "source_url": url, "source_kind": "pdf",
            "parsed": 0, "parse_note": None, "fetched_at": now,
        }
        if layout == "M":
            v["subject"] = _subject(text)
            v["result"] = "APROBADO" if "APROBAD" in _fold(text) else None
            v["parse_note"] = ("votación a mano alzada tras falla del sistema: "
                               "no se publicó lista nominal")
            out.append((v, []))
            continue
        seats, lines = [], []
        for i in idx:
            seats += (rows_a if layout == "A" else rows_b)(words[i])
            lines += _lines(words[i])
        seats = _constancia(text, seats)
        n = collections.Counter(s[2] for s in seats)
        yes, no, abst, absent = _tally(lines, layout)
        ok = (n["SI"], n["NO"], n["ABST"]) == (yes, no, abst)
        # Reproducing a board that says of itself that it is incomplete is not
        # validation. Only `supersede` can raise parsed on a provisional source.
        note = (None if ok and not provisional else
                f"reproduce el tablero {yes}/{no}/{abst}; pendiente diario de debates"
                if ok else
                f"decoded SI/NO/ABST {n['SI']}/{n['NO']}/{n['ABST']} != board "
                f"{yes}/{no}/{abst} over {len(seats)} rows")
        v.update(n_yes=yes, n_no=no, n_abstain=abst, n_absent=absent,
                 parsed=int(ok and not provisional), parse_note=note)
        out.append((v, seats))
    return out


def attendance(pdf, url):
    """[(held_on, taken_at, party, name, status, url)] for every attendance taking.
    A session takes several, so `Hora:` is part of the key."""
    texts = _text(pdf)
    if not any("ASISTENCIA:" in t for t in texts):
        return []
    words, out = _words(pdf), []
    for i, text in enumerate(texts):
        m = re.search(r"ASISTENCIA:\s*Fecha:\s*(\d{2})/(\d{2})/(\d{4})\s*Hora:\s*"
                      r"([\d:]+\s*[AP]M)", text)
        if not m:
            continue
        held, taken = f"{m[3]}-{m[2]}-{m[1]}", re.sub(r"\s+", " ", m[4])
        for party, name, status in _scan(words[i], ATTEND):
            out.append((held, taken, party, name, status, url))
    return out


def diario(pdf, chamber, url):
    """[(chamber, held_on, ordinal, result, board, floor, fixes, url)] for each roll
    call minuted in a diario de debates. Chamber comes from the host, like a board's:
    both chambers number their diarios PLO-2026-N and both sat on 05/08/2026.

    The board tally is the one the relator read out; the floor tally is what it
    became after the oral votes ("Más: Vázquez, abstención." / "Figallo, a favor." /
    "Se consigna también el voto a favor de la presidencia.").
    """
    flat = _flat(pdf)
    held = _held_on(flat)
    hits = list(TALLY_RE.finditer(flat))
    out = []
    for k, m in enumerate(hits):
        board = tuple(es_num(g) for g in m.group(2, 3, 4))
        end = hits[k + 1].start() if k + 1 < len(hits) else len(flat)
        fin = FINAL_RE.search(flat, m.end(), end)
        fixes = []
        if fin:
            window = flat[m.end():fin.start()]
            fixes = [(n, DIRECTION[_fold(d).lower()]) for n, d in CORR_RE.findall(window)]
            fixes += [("PRESIDENCIA", DIRECTION[d])
                      for d in PRES_RE.findall(window)]
        floor = tuple(es_num(g) for g in fin.group(1, 2, 3)) if fin else board
        result = {"aprueba": "APROBADO", "acuerda": "APROBADO"}.get(
            m[1].lower(), "RECHAZADO")
        out.append((chamber, held, k, result, board, floor, fixes, url))
    return out


def supersede(con, corrections):
    """Apply the diario's floor result over the board's. Re-runnable: the board rows
    are rewritten on every ingest, so this re-applies from scratch each time.

    An oral vote is only ever recorded for a member the electronic system missed, so
    only a row that is not already SI/NO/ABST may move. That rule is also what
    disambiguates "Vázquez" between the two senators of that name.
    """
    n = 0
    for ch, held, k, result, board, floor, fixes, url in corrections:
        vid = f"{ch}-{held}-{k}"
        v = con.execute("SELECT id, n_yes, n_no, n_abstain FROM vote WHERE id=?",
                        (vid,)).fetchone()
        if not v:
            continue
        if (v["n_yes"], v["n_no"], v["n_abstain"]) != board:
            con.execute("UPDATE vote SET parse_note=? WHERE id=?",
                        (f"board {v['n_yes']}/{v['n_no']}/{v['n_abstain']} != diario "
                         f"{board[0]}/{board[1]}/{board[2]}: not the same roll call",
                         vid))
            continue
        rows = con.execute("SELECT name_raw, position FROM vote_row WHERE vote_id=?",
                           (vid,)).fetchall()
        for who, pos in fixes:
            want = norm(who)
            hit = [r["name_raw"] for r in rows if r["position"] not in CAST
                   and (r["position"] == who
                        or _zs(norm(r["name_raw"])[:len(want)]) == _zs(want))]
            if len(hit) != 1:
                continue
            con.execute("UPDATE vote_row SET position=?, source='diario' "
                        "WHERE vote_id=? AND name_raw=?", (pos, vid, hit[0]))
            n += 1
        got = collections.Counter(
            r[0] for r in con.execute(
                "SELECT position FROM vote_row WHERE vote_id=?", (vid,)))
        ok = (got["SI"], got["NO"], got["ABST"]) == floor
        con.execute(
            "UPDATE vote SET n_yes_final=?, n_no_final=?, n_abstain_final=?, "
            "final_source_url=?, result=coalesce(result,?), parsed=?, parse_note=? "
            "WHERE id=?",
            (*floor, url, result, int(ok),
             None if ok else f"decoded SI/NO/ABST {got['SI']}/{got['NO']}/"
             f"{got['ABST']} != diario {floor[0]}/{floor[1]}/{floor[2]}", vid))
    con.commit()
    return n


def initials(party):
    """'Partido Del Buen Gobierno' -> 'PBG', the code the PDFs print. Short words
    are the filler the acronym drops (del, por, el)."""
    return "".join(w[0] for w in norm(party) if len(w) > 3)


def _roster(con):
    """chamber -> [(name tokens, party code, id)]."""
    legs = collections.defaultdict(list)
    for l in con.execute("SELECT id, chamber, full_name, party FROM legislator"):
        legs[l["chamber"]].append((norm(l["full_name"]), initials(l["party"]), l["id"]))
    return legs


def _resolve(legs, chambers, name, party):
    """(legislator id, chamber) for a truncated PDF name, or (None, None)."""
    t = norm(name)
    if not t:
        return None, None
    pool = [(toks, p, i, ch) for ch in chambers for toks, p, i in legs[ch]]
    hits = [(i, p, ch) for toks, p, i, ch in pool if _zs(toks[:len(t)]) == _zs(t)]
    if len(hits) != 1:  # truncation can start mid-surname ("DE LA CRUZ")
        hits = [(i, p, ch) for toks, p, i, ch in pool
                if any(_zs(toks[k:k + len(t)]) == _zs(t) for k in range(len(toks)))]
    if len(hits) > 1:   # "VELÁSQUEZ" is two senators; the group breaks the tie
        hits = [h for h in hits if h[1] == party] or hits
    return (hits[0][0], hits[0][2]) if len(hits) == 1 else (None, None)


def link(con):
    """Attach name_raw -> legislator.id on vote_row and attendance. Re-runnable, and
    safe to run before the legislator table exists: names in the PDFs are truncated
    ("ANDRADE SALGUERO DE"), so this is a prefix match, never an equality one."""
    legs, n = _roster(con), 0
    for r in con.execute(
            "SELECT r.vote_id, r.name_raw, r.party_raw, v.chamber FROM vote_row r "
            "JOIN vote v ON v.id = r.vote_id WHERE r.legislator_id IS NULL").fetchall():
        i, _ = _resolve(legs, [r["chamber"]], r["name_raw"], r["party_raw"])
        if i:
            con.execute("UPDATE vote_row SET legislator_id=? WHERE vote_id=? "
                        "AND name_raw=?", (i, r["vote_id"], r["name_raw"]))
            n += 1
    con.commit()
    return n


def save_attendance(con, rows, host_chamber, legs):
    """Store one PDF's attendance takings. The chamber is whichever roster the name
    resolves against -- Asistencia_Congreso_* is served by senado and holds the
    Diputados roster."""
    for held, taken, party, name, status, url in rows:
        i, ch = _resolve(legs, "DS", name, party)
        db.upsert(con, "attendance", {
            "chamber": ch or host_chamber, "held_on": held, "taken_at": taken,
            "legislator_id": i, "name_raw": name, "party_raw": party,
            "status": status, "source_url": url})
    return len(rows)


def ingest(con, chamber=None, offline=False):
    """Mirror, parse and store every board, diario and attendance taking.
    Returns (votes, vote rows, attendance rows)."""
    seen, ordinals = {}, collections.Counter()
    nv = nr = na = 0
    corrections, legs = [], _roster(con)
    chambers = [chamber] if chamber else ["D", "S"]
    for ch in chambers:
        # A run is a full refresh: every row is re-derived from the mirrored PDFs in
        # seconds, and the boards get replaced in place upstream, so keeping stale
        # rows around is the only way to end up serving a withdrawn roll call.
        con.execute("DELETE FROM vote_row WHERE vote_id IN "
                    "(SELECT id FROM vote WHERE chamber=?)", (ch,))
        con.execute("DELETE FROM vote WHERE chamber=?", (ch,))
    for ch in chambers:
        for m in media(ch):
            if m.get("mime_type") != "application/pdf":
                continue
            url = m["source_url"]
            if not NAME_RE.search(urllib.parse.unquote(url.rsplit("/", 1)[-1])):
                continue
            p = cached(url, offline)
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            if sha in seen:
                print(f"  dup of {seen[sha]}: {p.name}", flush=True)
                continue
            seen[sha] = p.name
            if p.name.upper().startswith("PLO-"):
                got = diario(p, ch, url)
                corrections += got
                print(f"  diario {p.name}: {len(got)} roll calls minuted", flush=True)
                continue
            na += save_attendance(con, attendance(p, url), ch, legs)
            calls = parse(p, ch, url, ordinals)
            if not calls:
                print(f"  {'no text layer' if calls is None else 'no roll call'}: "
                      f"{p.name}", flush=True)
                continue
            for v, seats in calls:
                db.upsert(con, "vote", v)
                for party, name, pos, src in seats:
                    db.upsert(con, "vote_row", {
                        "vote_id": v["id"], "name_raw": name, "party_raw": party,
                        "position": pos, "source": src})
                nv, nr = nv + 1, nr + len(seats)
                print(f"  {v['id']} parsed={v['parsed']} prov={v['provisional']} "
                      f"{len(seats)} rows {v['n_yes']}/{v['n_no']}/{v['n_abstain']} "
                      f"{(v['subject'] or '')[:55]}", flush=True)
            con.commit()
    print(f"  superseded {supersede(con, corrections)} rows from the diarios",
          flush=True)
    con.commit()
    return nv, nr, na


def demo():
    """Self-check against the live sources. `python3 -m ingest.votes`"""
    assert es_num("38") == 38 and es_num("tres") == 3 and es_num("ninguno") == 0
    assert es_num("Cuarenta") == 40 and es_num("cuatro") == 4
    assert es_num("ciento veintiocho") == 128 and es_num("treinta y uno") == 31

    # A layout A mark stranded between two columns must be rejected, not snapped.
    # Column centres 43 / 63 / 86, so the pitch is 21.5 and the window 5.4pt.
    hdr = [(40, 10, 46, 17, "SI"), (60, 10, 66, 17, "NO"), (80, 10, 92, 17, "ABST.")]
    row = [(5, 30, 9, 37, "1"), (12, 30, 20, 37, "FP"), (22, 30, 34, 37, "PEREZ")]
    assert rows_a(hdr + row + [(41, 30, 45, 37, "P")]) == [("FP", "PEREZ", "SI")]
    assert rows_a(hdr + row + [(50, 30, 54, 37, "P")]) == [("FP", "PEREZ", "BLANCO")]

    # The board and what the floor actually settled on, per roll call.
    want = {
        ("D", "2026/08/ASISTENCIA-VOTACION-PLENO-DIPUTADOS-20260805-provisional.pdf"): [
            ("A", "2026-08-05", (111, 15, 0), (111, 15, 0)),
            # Same file, second roll call: the summary block says 119 while its own
            # per-party table and every mark in the grid say 121, and four more votes
            # arrive by constancia below the roster. No diario published, so this one
            # cannot be closed and must stay parsed=0.
            ("B", "2026-08-05", (119, 0, 0), (125, 0, 0)),
            ("M", "2026-08-05", (None, None, None), (0, 0, 0))],
        ("S", "2026/08/ASISTENCIA-VOTACION-PLENO-SENADO_PROVISIONAL_-05-08-2026.pdf"): [
            ("A", "2026-08-05", (55, 0, 0), (55, 0, 0))],
        ("S", "2026/08/Asistencia_y_votacion_PROVISIONAL_sesion_12_08_2026.pdf"): [
            ("B", "2026-08-12", (38, 13, 3), (38, 13, 3))],
    }
    for (ch, path), exp in want.items():
        pdf = cached(f"{HOSTS[ch]}/wp-content/uploads/{path}")
        calls = parse(pdf, ch, path, collections.Counter())
        assert len(calls) == len(exp), (path, len(calls), len(exp))
        for (v, seats), (layout, held, board, decoded) in zip(calls, exp):
            got = collections.Counter(s[2] for s in seats)
            assert v["chamber"] == ch and v["held_on"] == held, v
            # Every board published so far is provisional, so none of them can
            # validate itself -- only a diario de debates raises parsed.
            assert v["provisional"] == 1 and v["parsed"] == 0, v
            assert (v["n_yes"], v["n_no"], v["n_abstain"]) == board, v
            assert (got["SI"], got["NO"], got["ABST"]) == decoded, (path, got)
            print(f"ok: {ch} {held} layout {layout} {len(seats)} rows "
                  f"board {board} decoded {decoded} parsed={v['parsed']}")

    # The diario is the record of last resort, and it disagrees with the board.
    d = diario(cached(f"{HOSTS['S']}/wp-content/uploads/2026/08/PLO-2026-3-SENADO.pdf"),
               "S", "PLO-2026-3-SENADO.pdf")
    assert len(d) == 1, d
    ch, held, k, result, board, floor, fixes, _ = d[0]
    assert (ch, held, k, result) == ("S", "2026-08-12", 0, "APROBADO"), d
    assert board == (38, 13, 3) and floor == (40, 13, 4), d
    assert fixes == [("Vázquez", "ABST"), ("Figallo", "SI"), ("PRESIDENCIA", "SI")], fixes
    print(f"ok: diario 2026-08-12 board {board} -> floor {floor} via {fixes}")

    # End to end in a throwaway DB. This is the regression that must never come
    # back: the board said 38-13-3 and three legislators were recorded wrongly.
    with tempfile.TemporaryDirectory() as tmp:
        con = db.connect(pathlib.Path(tmp) / "t.db")
        board_pdf = cached(f"{HOSTS['S']}/wp-content/uploads/2026/08/"
                           "Asistencia_y_votacion_PROVISIONAL_sesion_12_08_2026.pdf")
        for v, seats in parse(board_pdf, "S", "board.pdf", collections.Counter()):
            db.upsert(con, "vote", v)
            for party, name, pos, src in seats:
                db.upsert(con, "vote_row", {
                    "vote_id": v["id"], "name_raw": name, "party_raw": party,
                    "position": pos, "source": src})
        assert supersede(con, d) == 3
        v = con.execute("SELECT * FROM vote WHERE id='S-2026-08-12-0'").fetchone()
        assert (v["n_yes"], v["n_no"], v["n_abstain"]) == (38, 13, 3), dict(v)
        assert (v["n_yes_final"], v["n_no_final"], v["n_abstain_final"]) == (40, 13, 4), \
            dict(v)
        assert v["parsed"] == 1 and v["provisional"] == 1, dict(v)
        assert v["final_source_url"] and v["result"] == "APROBADO", dict(v)
        got = collections.Counter(r[0] for r in con.execute(
            "SELECT position FROM vote_row WHERE vote_id='S-2026-08-12-0'"))
        assert (got["SI"], got["NO"], got["ABST"]) == (40, 13, 4), got
        seat = {n: (p, s) for n, p, s in con.execute(
            "SELECT name_raw, position, source FROM vote_row "
            "WHERE vote_id='S-2026-08-12-0' AND source='diario'")}
        assert len(seat) == 3 and all(p == "SI" or p == "ABST" for p, _ in seat.values())
        by = {n.split(",")[0].split()[0]: p for n, (p, _) in seat.items()}
        assert by == {"FIGALLO": "SI", "VÁSQUEZ": "ABST", "TORRES": "SI"}, seat
        print(f"ok: superseded {board} -> {floor}, corrected {by}")

    # attendance-only files must never become votes
    assert rollcalls(cached(f"{HOSTS['S']}/wp-content/uploads/2026/07/"
                            "Asistencia_Senado_sesion_27-7-2026.pdf")) == []


if __name__ == "__main__":
    demo()
