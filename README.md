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
python3 -m ingest.run comisiones   # nómina de la Cámara + mesas directivas del Senado
python3 -m ingest.run gastos       # planilla y viajes por despacho + semanas de rep.
python3 -m ingest.run status       # counts and coverage
python3 -m ingest.photos           # 190 retratos -> assets/photos/ (una vez)

python3 build.py                   # -> site/, ~17.5k pages in ~43s
python3 progress.py                # -> site/progress.html, the build board
```

`assets/` holds the two inputs that are not in the database and are tracked in
git: `assets/logos/` (six bench logos) and `assets/photos/` (190 portraits, two
widths each, ~5 MB). `build.py` copies both into `site/`; nothing under `site/`
is ever an input. `ingest.photos` downloads and downscales with ffmpeg and only
fetches what is missing, so it is a one-off unless the padrón changes.

Committee rosters come from the diario de debates rather than any API:
`python3 -c "from ingest import db,committees as c; c.ingest(db.connect(),'S',
db.DB.parent/'pdf'/'PLO-2026-3-SENADO.pdf')"`. Only the Senado has one. The
Cámara de Diputados has not approved a cuadro de comisiones, so `ingest.run
comisiones` stores its 19 committees by name and by the id the bills API uses,
and no members — the roster tables its own site serves under each committee URL
are the Senado's Comisión de Constitución pasted seven times, byte for byte, and
would have staffed the Cámara entirely with senators.

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
| Despacho payroll | `GET transparencia.gob.pe/personal/pte_transparencia_personal_genera.aspx?id_entidad=16&…&ch_tipo_descarga=3` |
| Viáticos y pasajes | `GET transparencia.gob.pe/contrataciones/pte_transparencia_contrataciones_genera.aspx?id_entidad=16&…&formato=xml` |
| Semanas de representación | `GET www3.congreso.gob.pe/semana-representacion/` |
| Committees, by id | `GET /spley-portal-service/comisiones` + `/periodo-parlamentario/2026/filtros?codTipoParl=D` |
| Mesas directivas | `GET senado.congreso.gob.pe/wp-json/wp/v2/pages?slug=mesas-directivas-y-horarios-de-sesiones-de-las-comisiones-parlamentarias` |

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

## What a congresista costs

The Congress publishes nothing about this. It never published gastos operativos
per member — the only instance in the whole Wayback record for the domain is a
JPEG a congresista put on his own page in 2012 — and the bicameral reglamentos in
force since 27/07/2026 deleted even the *obligation to liquidate* them. Old art.
22 f) required a monthly rendición to Tesorería with at least 30% backed by
receipts; RLC 004/005/006-2025-2026-CR do not contain the phrase at all.

What is public and machine-readable is the Portal de Transparencia Estándar,
entity 16 — plain GETs returning CSV/XML, no captcha, no session, no cookies. It
gives two of the four cost components, and `ingest/gastos.py` takes both:

- **planilla del despacho** — every staffer, monthly, with the member named in
  `VC_PERSONAL_DEPENDENCIA`. 6–8 people per despacho, S/35.5k–52.2k a month.
- **viáticos y pasajes** — per trip, per despacho. The one figure with real
  spread: S/430 to S/136,892 over 2025. Do not trust its `kind`: PTE labels it
  "Viajes nacionales / internacionales" and it was that through 2025, but from
  2026 the Congress flags most domestic trips internacional too. `route` is the
  only honest test. The source's exterior money columns are zero in all 67
  months — foreign trips file in the domestic ones.

It does **not** give the member's own pay. Régimen 7 "Altos Funcionarios" is
empty for entity 16 in every month ever sampled, back to 2015. That number, and
the asignación por función congresal, are uniform statutory amounts — and the
acuerdo that last set the asignación (118-2023-2024/MESA-CR) updates it "por el
Índice de Precios al Consumidor" without printing a figure, which lives in an
unpublished informe. They are a constant, so they belong in one explainer, not on
190 pages. A per-member page showing the same number 190 times is not data.

Three traps here too:

6. **Take the XML, not the CSV.** `VC_PERSONAL_DEPENDENCIA` holds raw newlines in
   an unquoted field: July 2026 is 4,138 lines for 4,051 records, so a naive
   parse shears ~2% of the payroll in half without erroring.
7. **The despacho join must be an exact token-multiset match.** A subset match
   maps the área `DESPACHO CONGRESAL` onto a member. 22 people sit in the padrón
   twice, once per period, so the month's `per_par` breaks the tie — otherwise a
   2021-2026 despacho resolves to a 2026-2031 seat. 127/127 resolve for 2026-07.
8. **~40% of both tables is institutional, not anybody's cost** — comisiones,
   Parlamento Andino, áreas administrativas, and the staff of three
   ex-presidents of the Republic. Those rows keep `legislator_id NULL`. Dividing
   them across the padrón to reach a rounder number would be an invention.
9. **One human is two rows in the padrón.** The Cámara publishes "Barbaran
   Reyes, Rosangela Andrea" for 2026-2031 and the 2021-2026 roster has
   "Barbarán Reyes, Rosangella Andrea"; PTE uses both, so her cost splits across
   two ids and one half reads S/14,774 for a year instead of ~S/600,000. Not
   patched by loosening the join — one letter of edit distance is precisely the
   licence that turns `DESPACHO CONGRESAL` into a member. `gastos.split_despachos`
   reports it and `demo()` fails on a second case.

`rep_week` is the denominator: the 191 semanas de representación, Oct 2009 to Jul
2026, so travel can be "went in 9 of 11 weeks" and not just a pile of soles. Two
of its quirks are load-bearing and are stored, not corrected: week 103 is used
twice (ABRIL and MAYO 2018), so the number is not a key; and week 187 reads "de
marzo de 2036" for a 2026 week, kept verbatim in `raw` with the fix recorded in
`note`. Expect a hole in travel for 1 Jan – 12 Apr 2026: Acuerdo
083-2025-2026/MESA-CR suspended 21 benefit acuerdos over the campaign. That is a
legal suspension, not thrift.

## Not available

No law, decree or treaty has been enacted under this Congress yet — those
services are live and empty. The 2021–2026 roll-call backfill (426 sessions) is
an OCR problem, not a scraping one: every one is a printed-then-rescanned image
with no text layer.

Committee membership is published nowhere machine-readable, and for the Cámara
de Diputados it is not published at all: no cuadro de comisiones, no roster, and
no mesas directivas. Its Junta de Portavoces put the "distribución de las
presidencias, vicepresidencias y secretarías" on the agenda of 11/08/2026 —
`AGENDA-JP-DIPUTADOS-11-08-2026-01.pdf` carries the heading and not the annex —
and its only diario de debates so far, `PLO-2026-1-IDIPUTADOS.pdf`, is the
sesión de instalación. So the Cámara's committee pages name the committee and
say who is in it only once the Cámara does.
