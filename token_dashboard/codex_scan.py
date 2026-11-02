"""Scan OpenAI Codex CLI rollouts (``$CODEX_SESSIONS_DIR``, default ``~/.codex/sessions``).

Same shape as ``scanner.scan_dir``: the ``files`` table holds the byte high-water
mark per rollout and rows are INSERT OR REPLACE'd on a synthetic key, so rescans
are idempotent. Parser state (session id, cwd, model...) is saved in ``meta`` in
the same transaction, so appended lines are parsed without re-reading the file.
A missing directory is normal (Codex not installed): nothing is scanned.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Union

from .codex_parse import State, parse_record
from .db import connect

ENV_VAR = "CODEX_SESSIONS_DIR"
STATE_PREFIX = "codex_state:"

_MSG_COLS = ("uuid", "parent_uuid", "session_id", "project_slug", "cwd", "git_branch", "cc_version", "entrypoint",
             "type", "is_sidechain", "agent_id", "timestamp", "model", "stop_reason", "prompt_id", "message_id",
             "input_tokens", "output_tokens", "cache_read_tokens", "cache_create_5m_tokens", "cache_create_1h_tokens",
             "prompt_text", "prompt_chars", "tool_calls_json", "speed", "provider")
INSERT_MSG = (f"INSERT OR REPLACE INTO messages ({', '.join(_MSG_COLS)}) "
              f"VALUES ({', '.join(':' + c for c in _MSG_COLS)})")
INSERT_TOOL = ("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, "
               "is_error, timestamp, tool_use_id, result_chars, provider) VALUES (:message_uuid, :session_id, "
               ":project_slug, :tool_name, :target, :result_tokens, :is_error, :timestamp, :tool_use_id, "
               ":result_chars, :provider)")


def sessions_dir() -> Path:
    return Path(os.environ.get(ENV_VAR) or Path.home() / ".codex" / "sessions")


def _scan_file(path: Path, conn, start: int, state: State) -> dict:
    msgs = tools = 0
    end = start
    with open(path, "rb") as fb:
        fb.seek(start)
        for raw in iter(fb.readline, b""):
            if not raw.endswith(b"\n"):
                break    # partial line: the writer is mid-flush; re-read it next time
            end += len(raw)
            try:
                rec = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                rec = None
            if not isinstance(rec, dict):
                state.next_line += 1     # junk/blank lines still count towards the line number
                continue
            mrows, trows = parse_record(rec, state)
            for m in mrows:
                conn.execute(INSERT_MSG, m)
                msgs += 1
            for t in trows:
                # a call and its output share (session, tool_use_id) but differ in tool_name
                conn.execute("DELETE FROM tool_calls WHERE provider='codex' AND session_id=? AND tool_use_id=? "
                             "AND tool_name=?", (t["session_id"], t["tool_use_id"], t["tool_name"]))
                conn.execute(INSERT_TOOL, t)
                tools += 1
    return {"messages": msgs, "tools": tools, "end_offset": end}


def scan_codex(root: Union[str, Path, None] = None, db_path=None) -> dict:
    root = Path(root) if root else sessions_dir()
    totals = {"messages": 0, "tools": 0, "files": 0}
    if not root.is_dir():
        return totals
    entries = []
    for p in root.rglob("rollout-*.jsonl"):
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, p))
    entries.sort(key=lambda e: e[0], reverse=True)
    with connect(db_path, provider="all") as conn:
        known = {r["path"]: (r["mtime"], r["bytes_read"])
                 for r in conn.execute("SELECT path, mtime, bytes_read FROM files")}
        for mtime, size, p in entries:
            row = known.get(str(p))
            if row and row[0] == mtime and row[1] == size:
                continue
            offset = row[1] if row and size > row[1] else 0
            saved = conn.execute("SELECT v FROM meta WHERE k=?", (STATE_PREFIX + str(p),)).fetchone() if offset else None
            if offset and saved:
                state = State.loads(saved["v"], p.stem)
            else:
                offset, state = 0, State(p.stem)
            try:
                sub = _scan_file(p, conn, offset, state)
            except OSError:
                conn.rollback()
                continue
            conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (STATE_PREFIX + str(p), state.dumps()))
            conn.execute("INSERT OR REPLACE INTO files (path, mtime, bytes_read, scanned_at) VALUES (?, ?, ?, ?)",
                         (str(p), mtime, sub["end_offset"], time.time()))
            conn.commit()
            totals["messages"] += sub["messages"]
            totals["tools"] += sub["tools"]
            totals["files"] += 1
    return totals
