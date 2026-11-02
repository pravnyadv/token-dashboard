"""Fix ledger: record a change with its metric baseline, later judge whether the metric moved.

Baseline = mean of the 7 days before the day the entry was applied. After = mean of the 7 days
after that day, filled in once those 7 days are over (so on the 8th day, never on a partial
day). The verdict compares them with a 10 percent relative threshold, in the direction the
metric prefers. It is *indicative*: one user, one week, and other things change too.
No SQL here (see ledger_store); the metrics come from ledger_metrics.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from . import ledger_metrics as metrics
from . import ledger_store as store

WINDOW_DAYS = 7
CHANGE_THRESHOLD = 0.10
KINDS = ("rule", "model", "habit", "other")
MAX_DESCRIPTION = 500
VERDICT_NOTE = ("Indicative only: one user, one week, and other things changed too. "
                "It says whether the number moved, not that the change caused it.")
_PRICING = Path(__file__).resolve().parent.parent / "pricing.json"


def _day(iso: str) -> date:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).date()


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def due_day(applied_at: str) -> date:
    """First UTC day on which an entry applied at ``applied_at`` can be judged (its 7th day after is over)."""
    return _day(applied_at) + timedelta(days=WINDOW_DAYS + 1)


def verdict_for(metric: str, baseline: Optional[float], after: Optional[float]) -> str:
    if baseline is None or after is None:
        return "no_change"
    delta = after - baseline
    if delta == 0:
        return "no_change"
    # rounded so an exact 10% move (0.3 -> 0.33) is not lost to float error; exactly 10% counts as a change
    rel = round(abs(delta) / abs(baseline), 9) if baseline else float("inf")
    if rel < CHANGE_THRESHOLD:
        return "no_change"
    better = delta > 0 if metric in metrics.HIGHER_IS_BETTER else delta < 0
    return "improved" if better else "worse"


def apply(db_path, pricing, kind: str, description: str, metric: str, now: Optional[datetime] = None) -> dict:
    """Record a change. Raises ValueError for an unknown kind or metric, or a bad description."""
    if kind not in KINDS:
        raise ValueError("kind must be one of " + ", ".join(KINDS))
    if metric not in metrics.METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    description = (description or "").strip() if isinstance(description, str) else ""
    if not description or len(description) > MAX_DESCRIPTION:
        raise ValueError(f"description is required (at most {MAX_DESCRIPTION} characters)")
    now = _now(now)
    before = metrics.window(db_path, pricing, now.date() - timedelta(days=1), WINDOW_DAYS)
    return store.ledger_add(db_path, now.isoformat(), kind, description, metric,
                            metrics.daily_mean(before, metric), metrics.days_with_data(before, metric))


def check(db_path, pricing, today: Optional[date] = None, now: Optional[datetime] = None) -> List[dict]:
    """Judge every entry that is due and not yet judged; return the ones judged now.

    Idempotent: a judged entry is never touched again, and a younger one stays pending.
    """
    now = _now(now)
    today = today or now.date()
    done = []
    for e in store.ledger_all(db_path):
        if e["checked_at"] is not None or today < due_day(e["applied_at"]):
            continue
        applied = _day(e["applied_at"])
        days = metrics.window(db_path, pricing, applied + timedelta(days=WINDOW_DAYS), WINDOW_DAYS)
        after = metrics.daily_mean(days, e["metric"])
        res = store.ledger_set_result(db_path, e["id"], now.isoformat(), after,
                                      verdict_for(e["metric"], e["baseline"], after),
                                      metrics.days_with_data(days, e["metric"]))
        if res:
            done.append(res)
    return done


def entries(db_path) -> List[dict]:
    """All entries, newest first, each with `status` (pending/checked) and `due_on`."""
    rows = store.ledger_all(db_path)
    for e in rows:
        e["status"] = "checked" if e["checked_at"] else "pending"
        e["due_on"] = due_day(e["applied_at"]).isoformat()
    return rows[::-1]


def auto_check(db_path) -> None:
    """Run after each scan. Never lets a ledger problem break the scan."""
    try:
        if any(e["checked_at"] is None and datetime.now(timezone.utc).date() >= due_day(e["applied_at"])
               for e in store.ledger_all(db_path)):
            from .rates import load_with_override
            check(db_path, load_with_override(_PRICING))
    except Exception as e:  # noqa: BLE001
        print(f"Token Dashboard: ledger check skipped: {e}", file=sys.stderr)


def _fmt(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.3g}"


def format_table(rows: List[dict]) -> str:
    if not rows:
        return "ledger is empty"
    head = ("id", "applied", "kind", "metric", "baseline", "after", "days", "verdict", "description")
    body = [head] + [(str(e["id"]), e["applied_at"][:10], e["kind"], e["metric"], _fmt(e["baseline"]), _fmt(e["after"]),
                      f"{e['days_before'] or 0}/{'-' if e['days_after'] is None else e['days_after']}",
                      e["verdict"] or "pending", e["description"]) for e in rows]
    widths = [max(len(r[i]) for r in body) for i in range(len(head) - 1)]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)) + "  " + r[-1] for r in body)
