"""Generate the live progress page from the database + status.json.

`python3 progress.py` -> site/progress.html. Numbers come from the DB, so the
page cannot drift from reality; prose comes from status.json.
"""
import datetime as dt
import html
import json
import pathlib

from ingest import db

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "site" / "progress.html"
STATUS = ROOT / "status.json"

CSS = """
:root{
  --ground:#FAFAF8; --raised:#FFFFFF; --ink:#20262B; --muted:#525E68;
  --line:#D8DDE1; --accent:#A6192E; --ok:#1F6B4F; --wait:#8A6A22; --dead:#5D6870;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#12161A; --raised:#1A2026; --ink:#E6EAED; --muted:#9AA6B0;
  --line:#2C343B; --accent:#E8737E; --ok:#5FB394; --wait:#D2A64B; --dead:#8B959D;
}}
:root[data-theme="dark"]{
  --ground:#12161A; --raised:#1A2026; --ink:#E6EAED; --muted:#9AA6B0;
  --line:#2C343B; --accent:#E8737E; --ok:#5FB394; --wait:#D2A64B; --dead:#8B959D;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:960px;margin:0 auto;padding:clamp(24px,5vw,64px) clamp(18px,4vw,32px)}
h1,h2,h3{font-family:inherit;font-weight:600;text-wrap:balance;margin:0}
h1{font-size:clamp(28px,4.4vw,42px);letter-spacing:-.03em;line-height:1.15}
h2{font-size:20px;margin:0 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.eyebrow{font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
  letter-spacing:.16em;text-transform:uppercase;color:var(--accent)}
.sub{color:var(--muted);max-width:62ch;margin:10px 0 0}
header{display:flex;flex-direction:column;gap:8px;margin-bottom:36px}
section{margin-bottom:40px}
.metric{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 26px;
  background:var(--raised);border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:2px;padding:20px 24px;margin-bottom:36px}
.metric .big{font:600 clamp(38px,7vw,58px)/1 ui-monospace,Menlo,monospace;
  font-variant-numeric:tabular-nums;letter-spacing:-.03em}
.metric .label{font:600 11px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.14em;
  text-transform:uppercase;color:var(--muted)}
.metric dl{display:flex;gap:26px;flex-wrap:wrap;margin:0}
.metric dd{margin:2px 0 0;font:600 20px/1 ui-monospace,Menlo,monospace;
  font-variant-numeric:tabular-nums}
.metric dt{font:600 10px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.14em;
  text-transform:uppercase;color:var(--muted)}
.board{display:flex;flex-direction:column;gap:2px}
.piece{display:grid;grid-template-columns:4px 1fr auto;gap:0 16px;align-items:start;
  background:var(--raised);border:1px solid var(--line);border-radius:2px;padding:14px 16px}
.piece .stripe{grid-row:1/3;width:4px;height:100%;border-radius:2px;background:var(--dead)}
.piece.ok .stripe{background:var(--ok)} .piece.work .stripe{background:var(--accent)}
.piece.wait .stripe{background:var(--wait)}
.piece h3{font-size:16px}
.piece p{margin:4px 0 0;color:var(--muted);font-size:14px;max-width:60ch}
.tag{font:600 10px/1 ui-monospace,Menlo,monospace;letter-spacing:.12em;text-transform:uppercase;
  border:1px solid currentColor;border-radius:2px;padding:5px 7px;white-space:nowrap}
.piece.ok .tag{color:var(--ok)} .piece.work .tag{color:var(--accent)}
.piece.wait .tag{color:var(--wait)} .piece.todo .tag{color:var(--dead)}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:2px;background:var(--raised)}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:640px}
th,td{text-align:left;padding:9px 14px;border-bottom:1px solid var(--line);vertical-align:top}
th{font:600 10px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.13em;text-transform:uppercase;
  color:var(--muted)}
tr:last-child td{border-bottom:0}
code,td.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;
  word-break:break-all}
.v{font:600 10px/1 ui-monospace,Menlo,monospace;letter-spacing:.1em;text-transform:uppercase}
.v.y{color:var(--ok)} .v.n{color:var(--dead)}
footer{border-top:1px solid var(--line);padding-top:16px;color:var(--muted);font-size:13px;
  display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}
ul.notes{margin:0;padding-left:1.1em;color:var(--muted);font-size:14.5px}
ul.notes li{margin-bottom:6px;max-width:64ch}
ul.notes b{color:var(--ink);font-weight:600}
"""


def esc(s):
    return html.escape(str(s if s is not None else ""))


def render(cfg, cov, counts):
    pieces = "".join(
        f'<article class="piece {esc(p["state"])}"><span class="stripe"></span>'
        f'<div><h3>{esc(p["name"])}</h3><p>{esc(p["note"])}</p></div>'
        f'<span class="tag">{esc(p["state"])}</span></article>'
        for p in cfg["pieces"])
    sources = "".join(
        f'<tr><td>{esc(s["what"])}</td><td class="mono">{esc(s["url"])}</td>'
        f'<td><span class="v {"y" if s["verified"] else "n"}">'
        f'{"verified" if s["verified"] else "unverified"}</span></td>'
        f'<td>{esc(s["rows"])}</td></tr>' for s in cfg["sources"])
    notes = "".join(f"<li>{n}</li>" for n in cfg["notes"])
    verdicts = "".join(
        f'<article class="piece {"ok" if v.get("won") else "work"}">'
        f'<span class="stripe"></span><div><h3>{esc(v["page"])} '
        f'&middot; {esc(v["score"])}</h3><p>{v["gap"]}</p></div>'
        f'<span class="tag">{"gana" if v.get("won") else esc(v["state"])}</span>'
        f"</article>" for v in cfg.get("verdicts", []))
    verdicts = f'<div class="board">{verdicts}</div>' if verdicts else (
        "<p class='sub'>Scoring in progress.</p>")
    stats = "".join(
        f"<div><dt>{esc(k)}</dt><dd>{v:,}</dd></div>" for k, v in counts.items())
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<title>Hemiciclo Build Board</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<style>{CSS}</style>
<div class="wrap">
<header>
  <span class="eyebrow">Congreso del Per&uacute; &middot; 2026&ndash;2031</span>
  <h1>Tracking the bicameral Congress</h1>
  <p class="sub">{esc(cfg["subtitle"])}</p>
</header>

<div class="metric">
  <div><div class="label">Roll calls whose rows match the authoritative record</div>
       <div class="big">{cov["pct_validated"]}%</div>
       <div class="label">{cov["pct"]}% yield rows at all &middot;
         {cov["rows_linked"]}/{cov["vote_rows"]} rows tied to a named member</div></div>
  <dl>{stats}</dl>
</div>

<section><h2>Pieces</h2><div class="board">{pieces}</div></section>

<section><h2>Blind verdicts against GovTrack</h2>{verdicts}</section>

<section><h2>Sources</h2><div class="scroll"><table>
<thead><tr><th>What</th><th>Endpoint</th><th>Status</th><th>Yield</th></tr></thead>
<tbody>{sources}</tbody></table></div></section>

<section><h2>Where it stands</h2><ul class="notes">{notes}</ul></section>

<footer><span>Generated from the working database.</span>
<span>Updated {now}</span></footer>
</div>"""


def main():
    cfg = json.loads(STATUS.read_text())
    con = db.connect()
    cov = db.coverage(con)
    counts = {
        "bills": con.execute("SELECT count(*) FROM bill").fetchone()[0],
        "sponsorships": con.execute("SELECT count(*) FROM bill_sponsor").fetchone()[0],
        "actions": con.execute("SELECT count(*) FROM bill_action").fetchone()[0],
        "legislators": con.execute("SELECT count(*) FROM legislator").fetchone()[0],
        "motions": con.execute("SELECT count(*) FROM motion").fetchone()[0],
        "roll calls": cov["votes"],
        "vote rows": cov["vote_rows"],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(cfg, cov, counts))
    print(f"wrote {OUT}, coverage {cov['pct']}% ({cov['votes_parsed']}/{cov['votes']})")


if __name__ == "__main__":
    main()
