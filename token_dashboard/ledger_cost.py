"""Per-day cost split used by the ledger metrics (Claude only; the scope is set by the caller).

One query groups by day, model, speed, long-context band and a context-size flag, and prices each
group with the shared `WhatIf` / `Pricer`, so figures match the Overview's cost.
"""
from __future__ import annotations

from .db import connect, _range_clause
from .pricer import Pricer, PROMPT_SQL
from .whatif import WhatIf

BIG_CONTEXT_TOKENS = 200_000   # input + cache_read + cache_create of one turn


def cost_split(db_path, pricing, since, until) -> dict:
    """{day: {"total": usd, "opus": usd, "big": usd}}; `big` = turns whose context exceeded 200k tokens."""
    pricer = Pricer(pricing)
    band_sql, band_args = pricer.band_sql(PROMPT_SQL)
    rng, rng_args = _range_clause(since, until)
    # band_sql / PROMPT_SQL are internal constants; the threshold and the range are bound.
    sql = f"""
      SELECT substr(timestamp, 1, 10) AS day, model, speed, {band_sql} AS band,
             CASE WHEN {PROMPT_SQL} > ? THEN 1 ELSE 0 END AS big,
             COALESCE(SUM(input_tokens),0)           AS input_tokens,
             COALESCE(SUM(output_tokens),0)          AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)      AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0) AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' AND model IS NOT NULL {rng}
       GROUP BY day, model, speed, band, big
    """
    w = WhatIf(pricer, [])
    with connect(db_path) as c:
        for r in c.execute(sql, (*band_args, BIG_CONTEXT_TOKENS, *rng_args)):
            w.add(r["model"], r, r["band"], r["speed"],
                  key=(r["day"], pricer.class_of(r["model"]) == "opus", bool(r["big"])))
    out: dict = {}
    for day, opus, big in w.keys():
        usd = w.result((day, opus, big))["cost_usd"]
        d = out.setdefault(day, {"total": 0.0, "opus": 0.0, "big": 0.0})
        d["total"] += usd
        d["opus"] += usd if opus else 0.0
        d["big"] += usd if big else 0.0
    return out
