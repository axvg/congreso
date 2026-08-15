# Hemiciclo

A public record of Peru's bicameral Congress — the 130 deputies and 60 senators
of the 2026–2031 period. Bills, motions, roll-call votes and attendance, scraped
from the Congress's own APIs and PDFs and rendered as a static site.

Held to GovTrack.us as the bar. `docs/benchmark.md` is the page-by-page
reverse-engineering of GovTrack that the work is judged against, including the
12-criterion rubric used for blind comparison.

## Running it

Python 3.12, no dependencies beyond `cryptography` (stdlib otherwise) and
`pdftotext` from poppler.

```
python3 -m ingest.run members      # 130 + 60 from the chamber WordPress APIs
python3 -m ingest.run bills        # bill lists, both periods
python3 -m ingest.run expedientes  # dossiers: timeline, committees, documents
python3 -m ingest.run votes        # roll-call PDFs + diario de debates + attendance
python3 -m ingest.run textos       # the filed text of each bill, from its PDF
python3 -m ingest.run status       # counts and coverage

python3 build.py                   # -> site/, ~15k pages in ~5s
python3 progress.py                # -> site/progress.html, the build board
```

Committee rosters come from the diario de debates rather than any API:
`python3 -c "from ingest import db,committees as c; c.ingest(db.connect(),'S',
db.DB.parent/'pdf'/'PLO-2026-3-SENADO.pdf')"`.

Each ingest module has a `demo()` self-check that hits the live sources:
`python3 -m ingest.spley`, `python3 -m ingest.legislators`, and so on. They are
the test suite; `.github/workflows/actualizar.yml` runs all of them daily,
re-ingests incrementally and deploys.

The 2021-2026 roll-call backlog needs OCR, which is not installed:

```
sudo apt install -y tesseract-ocr tesseract-ocr-spa
python3 -m ingest.ocr              # pilot over 5 actas; it measures, does not ingest
```

## The measurable half

Coverage is the share of published roll calls whose per-legislator rows agree
with the most authoritative record available. It is deliberately strict:

- **held** — a roll call took place, whether or not we could extract it. This is
  the denominator, and it includes votes taken by show of hands for which no
  nominal list was ever published.
- **extracted** — per-legislator rows exist.
- **validated** — those rows match the Diario de los Debates where one exists,
  *not* a tally the chamber stamped `INFORMACIÓN PROVISIONAL · SIN LOS VOTOS
  ORALES`.

An earlier version divided roll calls that produced rows by roll calls in the
table. The same loop wrote both, so it read 100% by construction. A confidently
wrong vote row is worse than a missing one.

## Sources, and what each one cost to find

| What | Endpoint |
|---|---|
| Bills | `POST api.congreso.gob.pe/spley-portal-service/proyecto-ley/lista-con-filtro` |
| Bill dossier | `GET /spley-portal-service/expediente/{aes(perParId)}/{aes(num)}?codTipoParl=` |
| Author roster | `GET /spley-portal-service/periodo-parlamentario/{id}/filtros?codTipoParl=` |
| Motions | `POST api.congreso.gob.pe/smociones-portal-service/mocion/lista-con-filtros` |
| Legislators | `GET {diputados,senado}.congreso.gob.pe/wp-json/wp/v2/{diputado,senador}` |
| Roll calls, diarios | `GET /wp-json/wp/v2/media?per_page=100` on both chamber hosts |
| Enacted law | `GET api.congreso.gob.pe/adlp-visor-service/ley/leyes?nroley1=&nroley2=` |

Five things are not obvious and cost real time:

1. **`codTipoParl` is mandatory from 2026 on.** Omit it and the bicameral
   periods return an empty list rather than an error — which reads exactly like
   a congress that has filed nothing.
2. **The expediente path segments are AES-128-ECB encrypted** with a key shipped
   in the Angular bundle. Two traps: the service mangles `+` and `/` inside a
   path segment, so a ciphertext containing either 400s — zero-padding the
   plaintext rerolls it clean, since the value is parsed as an int server-side.
   And the route calls its first segment `:anio` while wanting the `perParId`.
3. **Party and district are in taxonomies registered `show_in_rest=false`.**
   `?grupo_parlamentario=x` is accepted and silently ignored. They leak through
   `get_post_class()` into `class_list`, which is the only structured source.
4. **In the nominal-vote layout the mark is the literal `P` in all three of the
   SI, NO and ABST columns.** Only its horizontal coordinate distinguishes a yes
   from a no, so the parser reads geometry, not text.
5. **Two routes serve the same attached document and only one is open.**
   `/archivo/uuid/{uuid}` answers `Token de captcha no proporcionado`; the
   numeric-id route the viewer uses for inline rendering,
   `/archivo/{btoa(id)}/pdf`, asks for nothing. Keep the id, not the uuid.

The roll-call tallies published during a session are stamped `INFORMACIÓN
PROVISIONAL · SIN LOS VOTOS ORALES`. The corrected result — including the votes
the chair reads into the record afterwards — appears only in the diario de
debates. Validating a provisional document against itself proves only that it is
self-consistent, so `parsed=1` requires the diario where one exists.

There is no shared id between the chamber websites and the bills API; the join
is normalised names, 190/190 for the current period.

## Not available

No law, decree or treaty has been enacted under this Congress yet — those
services are live and empty. The 2021–2026 roll-call backfill (426 sessions) is
an OCR problem, not a scraping one: every one is a printed-then-rescanned image
with no text layer. Committee membership is published nowhere machine-readable.
