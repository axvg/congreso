"""Portraits, fetched once and kept in the repo.

The chamber portals serve 1.3 MB PNGs. 190 of them were hotlinked straight into
38 px thumbnails, so /parlamentarios.html pulled 37.7 MB — and one upstream path
change would delete the face from 330 fichas, the padrón and 6 bench pages at
the same time. So: download once, downscale, commit.

`assets/photos/` is the tracked source, like `assets/logos/`; `build.py` copies
it into the output. Inputs never live under `site/`.

    python3 -m ingest.photos          # fetch what is missing, then self-check

ponytail: ffmpeg is the only downscaler on this machine (no Pillow, no
ImageMagick) and it is already a dependency of nothing, so it is called as a
subprocess and its absence is reported, not worked around.
"""
import pathlib
import shutil
import subprocess
import sys
import urllib.request

from ingest import db

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "photos"
# Two widths. 400 covers the 132 px ficha portrait and the 190 px grid card at
# 2x; 160 covers the 48 px row avatar at 2x and is what a phone actually picks.
WIDTHS = (160, 400)
UA = {"User-Agent": "hemiciclo/1.0 (+https://github.com/axvg/congreso)"}


def _ffmpeg():
    return shutil.which("ffmpeg")


def scale(src, dst, w):
    """One JPEG, `w` wide, aspect preserved. Returns False if ffmpeg is absent."""
    if not _ffmpeg():
        return False
    subprocess.run(
        [_ffmpeg(), "-v", "error", "-y", "-i", str(src),
         "-vf", f"scale={w}:-1:flags=lanczos", "-q:v", "4", str(dst)],
        check=True)
    return True


def fetch(con=None, force=False):
    """Every portrait the padrón knows about, as assets/photos/<slug>-<w>.jpg."""
    con = con or db.connect()
    OUT.mkdir(parents=True, exist_ok=True)
    rows = con.execute("SELECT slug, photo_url FROM legislator "
                       "WHERE photo_url IS NOT NULL ORDER BY slug").fetchall()
    got = miss = 0
    for r in rows:
        outs = [OUT / f'{r["slug"]}-{w}.jpg' for w in WIDTHS]
        if not force and all(p.exists() for p in outs):
            got += 1
            continue
        tmp = OUT / f'.{r["slug"]}.orig'
        try:
            req = urllib.request.Request(r["photo_url"], headers=UA)
            with urllib.request.urlopen(req, timeout=60) as fh:
                tmp.write_bytes(fh.read())
            for w, p in zip(WIDTHS, outs):
                if not scale(tmp, p, w):
                    print("ffmpeg no está instalado: no se puede reescalar",
                          file=sys.stderr)
                    return got, len(rows) - got
            got += 1
        except Exception as e:                      # noqa: BLE001
            miss += 1
            print(f'{r["slug"]}: {e}', file=sys.stderr)
        finally:
            tmp.unlink(missing_ok=True)
    return got, miss


def demo():
    con = db.connect()
    got, miss = fetch(con)
    n = con.execute("SELECT count(*) FROM legislator "
                    "WHERE photo_url IS NOT NULL").fetchone()[0]
    sizes = [p.stat().st_size for p in OUT.glob("*-400.jpg")]
    assert sizes, "no se descargó ningún retrato"
    assert max(sizes) < 300_000, f"un retrato de {max(sizes)} bytes sigue sin reescalar"
    print(f"retratos: {got} de {n} en assets/photos/ ({miss} fallaron), "
          f"{sum(p.stat().st_size for p in OUT.iterdir()) / 1e6:.1f} MB en total")


if __name__ == "__main__":
    demo()
