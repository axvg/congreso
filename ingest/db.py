"""SQLite schema. One file, no ORM."""
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "congreso.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 130 diputados + 60 senadores (2026-2031); also holds 2021-2026 congresistas.
CREATE TABLE IF NOT EXISTS legislator (
  id          TEXT PRIMARY KEY,      -- '<per_par>-<chamber>-<codigo>'
  per_par     INTEGER NOT NULL,
  chamber     TEXT NOT NULL,         -- 'D' diputados | 'S' senado | 'C' unicameral (2021-26)
  codigo      TEXT,                  -- spley congresistaId, for lista-con-filtro
  full_name   TEXT NOT NULL,
  slug        TEXT NOT NULL,
  last_name   TEXT,
  first_name  TEXT,
  party       TEXT,                  -- grupo parlamentario
  district    TEXT,                  -- circunscripcion / departamento
  photo_url   TEXT,
  source_url  TEXT,
  email       TEXT,
  votes_received TEXT,               -- votes that elected them, from the profile page
  bio         TEXT,
  active      INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS legislator_slug ON legislator(slug);

CREATE TABLE IF NOT EXISTS bill (
  id            TEXT PRIMARY KEY,    -- '<per_par>-<ply_num>'
  per_par       INTEGER NOT NULL,
  per_leg       INTEGER,
  ply_num       INTEGER NOT NULL,
  code          TEXT NOT NULL,       -- '14864/2025-CR'
  chamber       TEXT,                -- codTipoParl
  title         TEXT,
  summary       TEXT,
  status        TEXT,
  proponent     TEXT,
  presented_on  TEXT,
  authors_raw   TEXT,
  source_url    TEXT,
  fetched_at    TEXT
);
CREATE INDEX IF NOT EXISTS bill_presented ON bill(presented_on DESC);
CREATE INDEX IF NOT EXISTS bill_perpar ON bill(per_par);

CREATE TABLE IF NOT EXISTS bill_sponsor (
  bill_id       TEXT NOT NULL,
  legislator_id TEXT,
  name_raw      TEXT NOT NULL,
  rank          INTEGER,             -- 0 = primary author
  PRIMARY KEY (bill_id, name_raw)
);

-- 37% of bills sit at EN COMISION, so "which committee" is the whole status.
CREATE TABLE IF NOT EXISTS committee (
  id      INTEGER PRIMARY KEY,
  per_par INTEGER,
  chamber TEXT,
  name    TEXT NOT NULL,
  slug    TEXT NOT NULL,
  url     TEXT
);

-- Published nowhere as data; parsed out of the roster-approval session diario.
CREATE TABLE IF NOT EXISTS committee_member (
  committee_id  INTEGER NOT NULL,
  legislator_id TEXT,
  name_raw      TEXT NOT NULL,
  bench         TEXT,
  role          TEXT,                -- titular | suplente
  amendment     INTEGER DEFAULT 0,   -- 1 = a later change filed by oficio
  source_url    TEXT,
  PRIMARY KEY (committee_id, name_raw, role)
);
CREATE INDEX IF NOT EXISTS cm_leg ON committee_member(legislator_id);

CREATE TABLE IF NOT EXISTS bill_committee (
  bill_id      TEXT NOT NULL,
  committee_id INTEGER NOT NULL,
  PRIMARY KEY (bill_id, committee_id)
);

CREATE TABLE IF NOT EXISTS bill_action (
  bill_id  TEXT NOT NULL,
  acted_on TEXT,
  text     TEXT,
  doc_url  TEXT,
  PRIMARY KEY (bill_id, acted_on, text)
);

-- Motions carry most of the new Congress's early activity.
CREATE TABLE IF NOT EXISTS motion (
  id           TEXT PRIMARY KEY,     -- '<per_par>-<chamber>-<num>'
  per_par      INTEGER NOT NULL,
  chamber      TEXT NOT NULL,
  num          INTEGER NOT NULL,
  code         TEXT,                 -- '00014-2026-2031-S'
  kind         TEXT,                 -- MOCION DE SALUDO, etc.
  summary      TEXT,
  status       TEXT,
  party        TEXT,
  presented_on TEXT,
  authors_raw  TEXT,
  fetched_at   TEXT
);
CREATE INDEX IF NOT EXISTS motion_presented ON motion(presented_on DESC);

CREATE TABLE IF NOT EXISTS motion_signer (
  motion_id     TEXT NOT NULL,
  legislator_id TEXT,
  name_raw      TEXT NOT NULL,
  rank          INTEGER,             -- 0 = primary signer, marked (P) upstream
  PRIMARY KEY (motion_id, name_raw)
);

-- A published roll call. `source_url` is the artifact we parsed.
-- The electronic tally boards are stamped "INFORMACIÓN PROVISIONAL / SIN LOS VOTOS
-- ORALES"; the diario de debates carries the corrected result read out on the floor.
-- n_* is what the board printed, n_*_final what the floor settled on.
CREATE TABLE IF NOT EXISTS vote (
  id          TEXT PRIMARY KEY,      -- '<chamber>-<held_on>-<ordinal in the day>'
  per_par     INTEGER,
  chamber     TEXT NOT NULL,
  held_on     TEXT,
  session     TEXT,
  subject     TEXT,
  bill_id     TEXT,
  result      TEXT,
  n_yes       INTEGER, n_no INTEGER, n_abstain INTEGER, n_absent INTEGER,
  n_yes_final INTEGER, n_no_final INTEGER, n_abstain_final INTEGER,
  provisional INTEGER DEFAULT 0,     -- the parsed source says so about itself
  final_source_url TEXT,             -- diario de debates that superseded the board
  source_url  TEXT NOT NULL,
  source_kind TEXT,                  -- 'pdf' | 'html' | 'json'
  parsed      INTEGER DEFAULT 0,     -- 1 when rows agree with the best tally we have
  parse_note  TEXT,
  fetched_at  TEXT
);
CREATE INDEX IF NOT EXISTS vote_held ON vote(held_on DESC);

-- The deliverable: one row per legislator per roll call.
CREATE TABLE IF NOT EXISTS vote_row (
  vote_id       TEXT NOT NULL,
  legislator_id TEXT,
  name_raw      TEXT NOT NULL,
  party_raw     TEXT,
  -- SI | NO | ABST | AUSENTE | LICENCIA | SINRES (present, declined to register)
  -- | PRESIDENCIA (the chair, excluded by rule) | BLANCO (cell left empty)
  position      TEXT NOT NULL,
  source        TEXT,                -- 'grid' | 'constancia' | 'diario'
  PRIMARY KEY (vote_id, name_raw)
);
CREATE INDEX IF NOT EXISTS vote_row_leg ON vote_row(legislator_id);

-- Roll-call attendance is only half the story; every session also publishes one or
-- more plain attendance takings. `chamber` comes from the roster the names resolve
-- against, not the host: senado.congreso.gob.pe serves the Diputados roster under
-- the name Asistencia_Congreso_*.
CREATE TABLE IF NOT EXISTS attendance (
  chamber       TEXT NOT NULL,
  held_on       TEXT NOT NULL,
  taken_at      TEXT NOT NULL,       -- 'Hora:' -- a session takes several
  legislator_id TEXT,
  name_raw      TEXT NOT NULL,
  party_raw     TEXT,
  status        TEXT NOT NULL,       -- PRE | AUS | LO | LE | LP | L
  source_url    TEXT,
  PRIMARY KEY (chamber, held_on, taken_at, name_raw)
);
CREATE INDEX IF NOT EXISTS attendance_leg ON attendance(legislator_id);
"""

# Columns added after the first tables shipped. CREATE TABLE IF NOT EXISTS will not
# backfill them and the bill data behind them costs an hour to refetch.
LATE = [("vote", "n_yes_final INTEGER"), ("vote", "n_no_final INTEGER"),
        ("vote", "n_abstain_final INTEGER"), ("vote", "provisional INTEGER DEFAULT 0"),
        ("vote", "final_source_url TEXT"), ("vote_row", "source TEXT"),
        ("committee_member", "amendment INTEGER DEFAULT 0")]


def connect(path=DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60)  # WAL still serialises writers
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for table, col in LATE:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # already there
    return con


def upsert(con, table, row):
    cols = ",".join(row)
    marks = ",".join("?" * len(row))
    con.execute(
        f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", list(row.values())
    )


def coverage(con):
    """The headline metric: published roll calls -> structured per-legislator rows.

    An earlier version of this divided the roll calls that produced rows by the
    roll calls in the `vote` table. Both counts came from the same loop, which
    only inserted on success, so it read 100% by construction and could not read
    anything else. The denominator has to be every roll call we know was HELD,
    including the ones we failed to extract and the ones no nominal list was ever
    published for -- otherwise the number measures the parser's opinion of itself.

    Hence three states, and the strictest one leads:
      held      - a roll call took place (row exists whether or not we parsed it)
      extracted - per-legislator rows exist
      validated - those rows agree with the most authoritative record available,
                  which is the diario de debates where there is one, NOT a table
                  the chamber stamped INFORMACION PROVISIONAL / SIN LOS VOTOS ORALES.
    """
    q = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
    held = q("SELECT count(*) FROM vote")
    extracted = q("SELECT count(DISTINCT vote_id) FROM vote_row")
    validated = q("SELECT count(*) FROM vote WHERE parsed=1")
    pct = lambda n: round(100 * n / held, 1) if held else 0.0  # noqa: E731
    return {
        "votes": held, "votes_parsed": extracted, "votes_validated": validated,
        "votes_unextracted": held - extracted,
        "vote_rows": q("SELECT count(*) FROM vote_row"),
        "rows_linked": q("SELECT count(*) FROM vote_row "
                         "WHERE legislator_id IS NOT NULL"),
        "pct": pct(extracted), "pct_validated": pct(validated),
    }
