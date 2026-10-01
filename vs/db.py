"""SQLite store. Everything here can be rebuilt: opportunities by re-fetching,
user state from data/signals.jsonl, taste from data/examples/."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import numpy as np

from vs.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunity (
  id INTEGER PRIMARY KEY,
  dedup_key TEXT UNIQUE,
  canonical_url TEXT,
  source_urls TEXT DEFAULT '[]',
  source_id TEXT,
  from_example TEXT,
  type TEXT,
  title_original TEXT,
  title_en TEXT,
  org_name TEXT,
  org_domain TEXT,
  summary_en TEXT,
  field_tag TEXT,
  lang_original TEXT,
  application_languages TEXT DEFAULT '[]',
  location_city TEXT,
  location_country TEXT,
  region TEXT,
  remote_mode TEXT,
  deadline TEXT,
  deadline_kind TEXT,
  start_date TEXT,
  duration_text TEXT,
  conditions TEXT DEFAULT '{}',
  eligibility TEXT DEFAULT '{}',
  required_docs TEXT DEFAULT '[]',
  submission TEXT DEFAULT '{}',
  seniority TEXT,
  other_dates TEXT DEFAULT '[]',
  field_confidence TEXT DEFAULT '{}',
  raw_text TEXT,
  content_hash TEXT,
  extraction_status TEXT DEFAULT 'pending',
  status TEXT DEFAULT 'open',
  first_seen TEXT,
  last_seen TEXT
);
CREATE INDEX IF NOT EXISTS opp_status ON opportunity(status);

CREATE TABLE IF NOT EXISTS match (
  opportunity_id INTEGER PRIMARY KEY REFERENCES opportunity(id) ON DELETE CASCADE,
  computed_at TEXT,
  profile_hash TEXT,
  taste_hash TEXT,
  portfolio_sim REAL, taste REAL, keyword REAL, source_w REAL,
  prelim REAL, llm_fit REAL, final REAL,
  bucket TEXT,
  discovery_reason TEXT,
  cluster TEXT,
  reasons TEXT DEFAULT '[]',
  similar_examples TEXT DEFAULT '[]',
  red_flags TEXT DEFAULT '[]',
  suggested_projects TEXT DEFAULT '[]',
  eligibility_flag TEXT,
  eligibility_notes TEXT DEFAULT '[]',
  rerank_key TEXT
);

CREATE TABLE IF NOT EXISTS embedding (
  kind TEXT, key TEXT, text_hash TEXT, model_id TEXT, vec BLOB,
  PRIMARY KEY (kind, key, model_id)
);

CREATE TABLE IF NOT EXISTS user_state (
  opportunity_key TEXT PRIMARY KEY,
  favourite INTEGER DEFAULT 0,
  dismissed INTEGER DEFAULT 0,
  dismiss_reason TEXT,
  app_status TEXT,
  notes TEXT DEFAULT '',
  updated TEXT
);

CREATE TABLE IF NOT EXISTS source_state (
  source_id TEXT PRIMARY KEY,
  last_fetch TEXT,
  last_ok TEXT,
  items_last INTEGER,
  items_history TEXT DEFAULT '[]',
  error TEXT,
  blocked_runs INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS http_cache (
  url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, fetched_at TEXT, status INTEGER, body TEXT
);

CREATE TABLE IF NOT EXISTS page_snapshot (
  url TEXT PRIMARY KEY, text_hash TEXT, text TEXT, links TEXT DEFAULT '[]', checked_at TEXT, changed_at TEXT
);

CREATE TABLE IF NOT EXISTS run (
  id INTEGER PRIMARY KEY, started TEXT, ended TEXT, status TEXT,
  counts TEXT DEFAULT '{}', log TEXT DEFAULT '', report_path TEXT
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

JSON_COLUMNS = {
    "source_urls", "application_languages", "conditions", "eligibility", "required_docs",
    "submission", "other_dates", "field_confidence", "reasons", "similar_examples",
    "red_flags", "suggested_projects", "eligibility_notes", "counts", "items_history", "links",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    """Add columns introduced after a database was created."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(opportunity)")}
    if "extract_version" not in cols:
        conn.execute("ALTER TABLE opportunity ADD COLUMN extract_version TEXT")
    if "hidden_reason" not in cols:
        conn.execute("ALTER TABLE opportunity ADD COLUMN hidden_reason TEXT")


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for k, v in out.items():
        if k in JSON_COLUMNS and isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return out


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def kv_get(conn, key: str, default=None):
    row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return json.loads(row["v"]) if row else default


def kv_set(conn, key: str, value) -> None:
    conn.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, dumps(value)))


# ---- embeddings


def get_vec(conn, kind: str, key: str, model_id: str, text_hash: str) -> np.ndarray | None:
    row = conn.execute(
        "SELECT vec, text_hash FROM embedding WHERE kind=? AND key=? AND model_id=?", (kind, key, model_id)
    ).fetchone()
    if row is None or row["text_hash"] != text_hash:
        return None
    return np.frombuffer(row["vec"], dtype=np.float32)


def put_vec(conn, kind: str, key: str, model_id: str, text_hash: str, vec: np.ndarray) -> None:
    conn.execute(
        "INSERT INTO embedding(kind, key, text_hash, model_id, vec) VALUES(?,?,?,?,?) "
        "ON CONFLICT(kind, key, model_id) DO UPDATE SET text_hash=excluded.text_hash, vec=excluded.vec",
        (kind, key, text_hash, model_id, np.asarray(vec, dtype=np.float32).tobytes()),
    )


def all_vecs(conn, kind: str, model_id: str) -> dict[str, np.ndarray]:
    rows = conn.execute("SELECT key, vec FROM embedding WHERE kind=? AND model_id=?", (kind, model_id))
    return {r["key"]: np.frombuffer(r["vec"], dtype=np.float32) for r in rows}
