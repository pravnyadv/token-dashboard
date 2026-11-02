"""SQL for the fix ledger (rows are plain dicts)."""
from __future__ import annotations

from typing import Optional

from .db import connect

_COLS = "id, applied_at, kind, description, metric, baseline, checked_at, after, verdict, days_before, days_after"


def _row(c, entry_id: int) -> dict:
    return dict(c.execute(f"SELECT {_COLS} FROM ledger WHERE id=?", (entry_id,)).fetchone())


def ledger_add(db_path, applied_at: str, kind: str, description: str, metric: str,
               baseline: Optional[float], days_before: int) -> dict:
    with connect(db_path) as c:
        cur = c.execute("INSERT INTO ledger (applied_at, kind, description, metric, baseline, days_before) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (applied_at, kind, description, metric, baseline, days_before))
        c.commit()
        return _row(c, cur.lastrowid)


def ledger_all(db_path) -> list:
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(f"SELECT {_COLS} FROM ledger ORDER BY applied_at, id")]


def ledger_set_result(db_path, entry_id: int, checked_at: str, after: Optional[float],
                      verdict: str, days_after: int) -> Optional[dict]:
    """Record the verdict once. Returns the row, or None if it was already checked (a race or a repeat)."""
    with connect(db_path) as c:
        cur = c.execute("UPDATE ledger SET checked_at=?, after=?, verdict=?, days_after=? "
                        "WHERE id=? AND checked_at IS NULL", (checked_at, after, verdict, days_after, entry_id))
        c.commit()
        return _row(c, entry_id) if cur.rowcount else None
