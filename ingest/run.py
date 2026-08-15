"""Ingest runner. `python3 -m ingest.run bills|expedientes|votes|status`"""
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
     "votes": votes, "status": status}[cmd](con, *sys.argv[2:])
