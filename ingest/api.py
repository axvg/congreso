"""HTTP + JSON helpers for congreso.gob.pe. stdlib only, on purpose."""
import gzip
import json
import time
import urllib.error
import urllib.request

UA = "congreso-tracker/0.1 (+https://github.com/axvg/projects; open data mirror)"

SPLEY = "https://api.congreso.gob.pe/spley-portal-service"


def fetch(url, data=None, headers=None, tries=4, timeout=60, out=None):
    """GET, or POST when `data` is a dict (sent as JSON). Returns bytes.

    `out`, if given, is filled with the response headers (WordPress paginates
    with X-WP-TotalPages and nowhere else)."""
    body = None
    hdrs = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    def read(r):
        if out is not None:
            out.update(r.headers)
        raw = r.read()
        return gzip.decompress(raw) if r.headers.get("Content-Encoding") == "gzip" else raw

    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return read(r)
        except urllib.error.HTTPError as e:
            # The service encodes its own errors as JSON with a 4xx status
            # ("El Proyecto de Ley No Existe"). Those are answers, not failures --
            # returning the body keeps callers from retrying a settled question.
            if e.code < 500:
                return read(e)
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        # ponytail: fixed backoff, swap for jittered if they start rate-limiting
        time.sleep(2 * (n + 1))
    raise last


def get_json(url, data=None, **kw):
    return json.loads(fetch(url, data=data, **kw))


def spley(path, data=None, **kw):
    """Call the bills service and unwrap its {code,status,data} envelope."""
    r = get_json(f"{SPLEY}{path}", data=data, **kw)
    if r.get("status") != "success":
        raise RuntimeError(f"{path}: {r.get('code')} {r.get('status')}")
    return r["data"]


def paged(path, filt, page=200, cap=100_000):
    """Yield rows from a `lista-con-filtro` endpoint, walking rowStart."""
    start, seen = 0, 0
    while seen < cap:
        d = spley(path, {**filt, "pageSize": page, "rowStart": start})
        rows = d.get("proyectos") or d.get("dictamenes") or d.get("lista") or []
        if not rows:
            return
        yield from rows
        seen += len(rows)
        start += len(rows)
        if seen >= d.get("rowsTotal", 0):
            return
