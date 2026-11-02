"""HTTP bodies for the ledger endpoints (kept out of server.py, which routes and sends)."""
from __future__ import annotations

from . import ledger, ledger_metrics


def get_payload(db_path) -> dict:
    return {"entries": ledger.entries(db_path), "metrics": ledger_metrics.metric_list(),
            "kinds": list(ledger.KINDS), "window_days": ledger.WINDOW_DAYS,
            "threshold": ledger.CHANGE_THRESHOLD, "note": ledger.VERDICT_NOTE}


def post(db_path, pricing, path: str, body: dict, headers) -> tuple:
    """(status, JSON-able) for POST /api/ledger and /api/ledger/check.

    The X-Token-Dashboard header is required, like the findings Generate endpoint: a custom
    header forces a CORS preflight, so another web page cannot make the browser send it.
    """
    if headers.get("X-Token-Dashboard") != "1":
        return 403, {"error": "missing X-Token-Dashboard header"}
    if path == "/api/ledger/check":
        return 200, {"checked": ledger.check(db_path, pricing)}
    try:
        return 201, ledger.apply(db_path, pricing, body.get("kind"), body.get("description"), body.get("metric"))
    except ValueError as e:
        return 400, {"error": str(e)}
