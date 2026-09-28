"""SQLite storage: schema, connection, and the text normalisation used for topic matching."""

from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source        TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    url           TEXT NOT NULL,
    lang          TEXT NOT NULL,
    country       TEXT NOT NULL,
    category      TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    -- polite-poller state (conditional GET) + health
    etag          TEXT,
    last_modified TEXT,
    last_fetch    TEXT,
    last_status   TEXT,
    last_ok       TEXT
);

-- Items are stored in their ORIGINAL language; the LLM translates at query time.
-- Keyed per source: several outlets covering one story is the cross-framing feature, not a dupe.
CREATE TABLE IF NOT EXISTS news (
    source        TEXT NOT NULL,
    guid          TEXT NOT NULL,
    lang          TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    link          TEXT,
    published_utc TEXT NOT NULL,   -- ISO-8601 UTC, sortable
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (source, guid)
);
CREATE INDEX IF NOT EXISTS news_published ON news (published_utc DESC);
"""


def fold(text: str | None) -> str:
    """Case- and accent-insensitive form: 'Elección' -> 'eleccion'. Safe for any script."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # refresh writes while the server reads
    conn.execute("PRAGMA busy_timeout=30000")
    conn.create_function("fold", 1, fold, deterministic=True)
    conn.executescript(SCHEMA)
    return conn
