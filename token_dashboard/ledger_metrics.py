"""Named daily metrics for the fix ledger, computed from this dashboard's own Claude data.

Codex is excluded on purpose (its cache semantics differ), whatever provider the caller
has selected. Days are the UTC days `/api/daily` uses (`substr(timestamp, 1, 10)`).
A day *has data* for a metric when the metric is defined for it; ratios need a non-zero
denominator, `compactions_per_day` counts a day with activity and no compaction as 0.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Optional

from . import provider
from .cache_insights import cache_report
from .compaction_insights import compactions_report
from .db import daily_token_breakdown
from .ledger_cost import cost_split

# name -> (label, direction, unit)
METRICS = {
    "cost_per_day_usd":            ("Cost per day", "lower", "usd"),
    "opus_cost_share":             ("Opus share of cost", "lower", "ratio"),
    "cache_wasted_usd_per_day":    ("Cache waste per day", "lower", "usd"),
    "cache_hit_ratio":             ("Cache hit ratio", "higher", "ratio"),
    "compactions_per_day":         ("Compactions per day", "lower", "count"),
    "cost_share_over_200k_context": ("Cost share of turns over 200k context", "lower", "ratio"),
}
HIGHER_IS_BETTER = frozenset(k for k, v in METRICS.items() if v[1] == "higher")


def metric_list() -> list:
    return [{"name": k, "label": v[0], "direction": v[1], "unit": v[2]} for k, v in METRICS.items()]


def window(db_path, pricing, end_day: date, n: int) -> Dict[str, dict]:
    """{day: {metric: value}} for the ``n`` days ending at ``end_day`` (inclusive); days without activity are absent."""
    since, until = (end_day - timedelta(days=n - 1)).isoformat(), (end_day + timedelta(days=1)).isoformat()
    with provider.scope("claude"):
        active = {r["day"] for r in daily_token_breakdown(db_path, since, until)}
        cost = cost_split(db_path, pricing, since, until)
        cache = {r["day"]: r for r in cache_report(db_path, pricing, since, until)["by_day"]}
        comp = {r["day"]: r["count"] for r in compactions_report(db_path, pricing, since, until)["by_day"]}
    out = {}
    for day in sorted(active):
        c = cost.get(day, {"total": 0.0, "opus": 0.0, "big": 0.0})
        v = {"cost_per_day_usd": c["total"], "compactions_per_day": comp.get(day, 0)}
        if c["total"] > 0:
            v["opus_cost_share"] = c["opus"] / c["total"]
            v["cost_share_over_200k_context"] = c["big"] / c["total"]
        if day in cache:
            v["cache_wasted_usd_per_day"] = cache[day]["wasted_usd"]
            if cache[day]["hit_ratio"] is not None:
                v["cache_hit_ratio"] = cache[day]["hit_ratio"]
        out[day] = v
    return out


def daily_mean(days: Dict[str, dict], metric: str) -> Optional[float]:
    """Mean over the days that have a value for ``metric``; None when none do."""
    vals = [d[metric] for d in days.values() if metric in d]
    return sum(vals) / len(vals) if vals else None


def days_with_data(days: Dict[str, dict], metric: str) -> int:
    return sum(1 for d in days.values() if metric in d)
