"""``ledger`` table: one row per recorded fix, judged once 7 full days have passed.

Created with CREATE TABLE IF NOT EXISTS in the init path. Nothing here, and none of the
scanner's rebuild paths (the clear-and-replay migrations, the one-time backfills, a
``files`` reset), touches it: a ledger row is the user's own record, not derived data.
"""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  applied_at   TEXT NOT NULL,
  kind         TEXT NOT NULL,
  description  TEXT NOT NULL,
  metric       TEXT NOT NULL,
  baseline     REAL,
  checked_at   TEXT,
  after        REAL,
  verdict      TEXT,
  days_before  INTEGER,
  days_after   INTEGER
);
"""


def ensure_ledger(conn) -> None:
    conn.executescript(SCHEMA)
