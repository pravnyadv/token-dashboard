"""Flags usage whose model has no entry (or tier fallback) in pricing.json as *unpriced*.

Only applied to non-Claude scopes, so the Claude-only JSON shape is unchanged. A price is
never invented: an unpriced model contributes $0 to totals and is reported separately.
"""
from __future__ import annotations

from .db import connect
from .pricer import Pricer


def is_unpriced(pricer: Pricer, model) -> bool:
    return not model or model == "unknown" or pricer.rates(model) is None


def mark_overview(totals: dict, models: list, pricing: dict) -> dict:
    """Add `unpriced_tokens` (all buckets) from `model_breakdown` rows."""
    pr = Pricer(pricing)
    totals["unpriced_tokens"] = sum(
        r["input_tokens"] + r["output_tokens"] + r["cache_read_tokens"]
        + r["cache_create_5m_tokens"] + r["cache_create_1h_tokens"]
        for r in models if is_unpriced(pr, r["model"]))
    return totals


def mark_models(rows: list, pricing: dict) -> list:
    """`by-model` rows: `unpriced` flag; an unpriced model has `cost_usd` None."""
    pr = Pricer(pricing)
    for r in rows:
        r["unpriced"] = is_unpriced(pr, r["model"])
        if r["unpriced"]:
            r["cost_usd"] = None
    return rows


def mark_sessions(db_path, pricing: dict, sessions: list) -> list:
    """Per session: `provider` (claude/codex) and `cost_unpriced`, true when any model it used is unpriced
    (its cost is then partial)."""
    ids = [s["session_id"] for s in sessions]
    for s in sessions:
        s["cost_unpriced"] = False
    if not ids:
        return sessions
    pr = Pricer(pricing)
    bad, prov = set(), {}
    with connect(db_path) as c:
        for r in c.execute("SELECT DISTINCT session_id, model, provider FROM messages "
                           f"WHERE session_id IN ({','.join('?' * len(ids))})", ids):
            prov[r["session_id"]] = r["provider"]
            if r["model"] is not None and is_unpriced(pr, r["model"]):
                bad.add(r["session_id"])
    for s in sessions:
        s["cost_unpriced"] = s["session_id"] in bad
        s["provider"] = prov.get(s["session_id"], "claude")
    return sessions
