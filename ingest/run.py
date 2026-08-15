"""Ingest runner.

`python3 -m ingest.run bills|members|expedientes|votes|textos|status`
"""
import concurrent.futures as cf
import sys

from . import db, spley


def bills(con):
    for per_par, chambers in ((2026, ("D", "S")), (2021, (None,))):
        for ch in chambers:
            n = spley.ingest_list(con, per_par, chamber=ch)
            print(f"per_par {per_par} {ch or 'C'}: {n} bills", flush=True)


def members(con):
    from . import legislators
    for ch in ("D", "S"):
        n, matched = legislators.ingest(con, ch)
        print(f"{ch}: {n} legislators, {matched} joined to the bills API", flush=True)


def votes(con, chamber=None):
    from . import votes as v
    nv, nr, na = v.ingest(con, chamber)
    print(f"votes: {nv} roll calls, {nr} rows, {na} attendance records, "
          f"{v.link(con)} names joined to legislators", flush=True)


def textos(con, limit=None, workers=4):
    """Download and extract the filed text of every bill that has one."""
    from . import texts
    q = ("SELECT DISTINCT a.bill_id FROM bill_action a WHERE a.doc_id IS NOT NULL "
         "AND NOT EXISTS (SELECT 1 FROM bill_text t WHERE t.bill_id=a.bill_id)")
    if limit:
        q += f" LIMIT {int(limit)}"
    ids = [r[0] for r in con.execute(q).fetchall()]
    print(f"textos: {len(ids)} por descargar", flush=True)

    def grab(bid):
        doc = texts.primary_doc(con, bid)
        try:
            return bid, doc, texts.fetch_pdf(doc["doc_id"], doc["doc_url"]), None
        except texts.TooBig as e:
            return bid, doc, None, f"documento de {e}: publicado como imagen"

    ok = skip = fail = 0
    # ponytail: 4 workers and no backoff beyond api.fetch's; it is a public
    # service and a bulk read, so stay well under what a browsing user costs.
    with cf.ThreadPoolExecutor(int(workers)) as pool:
        for i, fut in enumerate(cf.as_completed(pool.submit(grab, b) for b in ids), 1):
            try:
                bid, doc, path, note = fut.result()
                body, pages = ("", 0) if note else texts.extract(path)
                if path:
                    # ponytail: the text is what we keep; the PDFs are ~7 GB of
                    # files already read and one public GET away.
                    path.unlink(missing_ok=True)
                if not note and len(body) < 200:
                    note = "sin capa de texto: publicado como imagen"
                # Store either way, with the reason -- so the page can say why
                # there is no text instead of hiding the section, and so the run
                # does not re-download it tomorrow.
                db.upsert(con, "bill_text", {
                    "bill_id": bid, "doc_id": doc["doc_id"], "pages": pages,
                    "chars": len(body), "body": None if note else body,
                    "note": note, "source_url": doc["doc_url"],
                    "fetched_at": _now()})
                ok += not note
                skip += bool(note)
            except Exception as e:  # noqa: BLE001
                fail += 1
                if fail < 6:
                    print(f"  {e!r}", flush=True)
            if i % 200 == 0:
                con.commit()
                print(f"  {i}/{len(ids)} ok={ok} sin-texto={skip} fail={fail}",
                      flush=True)
    con.commit()
    print(f"textos: ok={ok} sin-texto={skip} fail={fail}", flush=True)


def _now():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def expedientes(con, limit=None, workers=6, redo=0):
    """Backfill dossiers for bills that have no actions yet.

    Fetches concurrently, writes from this thread only -- sqlite connections are
    not shared across threads. Pass redo=1 to re-fetch everything, which is what
    you want after adding a field the dossier already carried.
    """
    q = "SELECT id, per_par, ply_num, code, chamber FROM bill b "
    if not int(redo):
        q += ("WHERE NOT EXISTS (SELECT 1 FROM bill_action a WHERE a.bill_id=b.id) ")
    q += "ORDER BY presented_on DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = con.execute(q).fetchall()
    print(f"expedientes: {len(rows)} to fetch", flush=True)

    def grab(b):
        return b, spley.expediente(b["per_par"], b["ply_num"], b["chamber"] or "C")

    ok = fail = acts = 0
    with cf.ThreadPoolExecutor(int(workers)) as pool:
        for i, fut in enumerate(cf.as_completed(pool.submit(grab, b) for b in rows), 1):
            try:
                b, d = fut.result()
                acts += spley.save_expediente(con, b["per_par"], b["chamber"],
                                              b["ply_num"], d)
                ok += 1
            except Exception as e:  # noqa: BLE001 - one bad dossier must not stop the run
                fail += 1
                if fail < 6:
                    print(f"  {e!r}", flush=True)
            if i % 250 == 0:
                con.commit()
                print(f"  {i}/{len(rows)} ok={ok} fail={fail} actions={acts}", flush=True)
    con.commit()
    print(f"expedientes: ok={ok} fail={fail} actions={acts}", flush=True)


def status(con):
    for t in ("legislator", "bill", "bill_sponsor", "bill_action", "vote", "vote_row"):
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"{t:15} {n:>7}")
    print("coverage:", db.coverage(con))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    con = db.connect()
    {"bills": bills, "members": members, "expedientes": expedientes,
     "votes": votes, "textos": textos, "status": status}[cmd](con, *sys.argv[2:])
