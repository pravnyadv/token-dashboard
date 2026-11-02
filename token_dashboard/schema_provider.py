"""``messages.provider`` / ``tool_calls.provider``: which CLI wrote a row.

Additive and non-destructive: existing rows take the default ``'claude'`` and
nothing is rebuilt or re-read. Codex rows are written by ``codex_scan``.

No index on ``provider``: the scope views filter on ``+provider`` (which the planner
cannot use an index for) because on a 500k-row table a ``(provider, timestamp)`` index
made the Claude-only views 3-4x slower than the raw table (random row lookups for a
filter that keeps nearly every row). A DB that already has it gets it dropped.
"""
from __future__ import annotations

_TABLES = ("messages", "tool_calls")  # internal constants, safe to interpolate


def ensure_provider(conn) -> None:
    for table in _TABLES:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "provider" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN provider TEXT NOT NULL DEFAULT 'claude'")
    conn.execute("DROP INDEX IF EXISTS idx_messages_provider")
