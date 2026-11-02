"""Which provider's rows a database connection sees.

Every analysis written before Codex support (cache, compactions, tips, skills,
findings, agents, what-if, costs...) assumes Claude's cache model and queries
``messages`` / ``tool_calls`` directly. Rather than patch each query,
``db.connect`` shadows those two tables with TEMP VIEWS that hold one provider's
rows, so such code is Claude-only by construction. The default scope is
``claude``; the server widens it for the few endpoints that take ``?provider=``,
and the scanners ask for ``all`` (raw tables).
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

PROVIDERS = ("claude", "codex", "all")
# endpoints that honour ?provider=; everything else is always Claude-only
PROVIDER_PATHS = ("/api/overview", "/api/daily", "/api/by-model", "/api/sessions",
                  "/api/projects", "/api/repos")

_CURRENT = contextvars.ContextVar("token_dashboard_provider", default="claude")


def current() -> str:
    return _CURRENT.get()


def parse(raw) -> str:
    """Validate a ``provider`` query value. Empty/None means claude. Raises ValueError."""
    if raw in (None, ""):
        return "claude"
    if raw not in PROVIDERS:
        raise ValueError("provider must be one of " + ", ".join(PROVIDERS))
    return raw


def for_request(raw_path: str) -> str:
    """Provider scope for a request URL (claude unless the path takes ?provider=)."""
    url = urlparse(raw_path)
    if url.path not in PROVIDER_PATHS:
        return "claude"
    return parse(parse_qs(url.query or "").get("provider", [None])[0])


@contextmanager
def scope(provider: str):
    token = _CURRENT.set(parse(provider))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def install_views(conn, provider: str) -> None:
    """Shadow messages/tool_calls with one provider's rows (no-op for ``all``)."""
    if provider == "all":
        return
    if provider not in PROVIDERS:
        raise ValueError(provider)
    # `provider` is whitelisted above; `rowid` is exposed because some queries order by it.
    conn.execute(f"CREATE TEMP VIEW IF NOT EXISTS messages AS "
                 f"SELECT rowid AS rowid, * FROM main.messages WHERE provider = '{provider}'")
    conn.execute(f"CREATE TEMP VIEW IF NOT EXISTS tool_calls AS "
                 f"SELECT * FROM main.tool_calls WHERE provider = '{provider}'")
