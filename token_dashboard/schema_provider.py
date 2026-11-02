"""``messages.provider`` / ``tool_calls.provider``: which CLI wrote a row.

Additive and non-destructive: existing rows take the default ``'claude'`` and
nothing is rebuilt or re-read. Codex rows are written by ``codex_scan``.
"""
from __future__ import annotations

_TABLES = ("messages", "tool_calls")  # internal constants, safe to interpolate


def ensure_provider(conn) -> None:
    for table in _TABLES:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "provider" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN provider TEXT NOT NULL DEFAULT 'claude'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_provider ON messages(provider, timestamp)")
