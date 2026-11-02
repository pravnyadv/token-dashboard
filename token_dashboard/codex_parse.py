"""Codex CLI rollout records -> `messages` / `tool_calls` rows (pure, no I/O).

Format relied on (one JSON object per line of ``rollout-*.jsonl``):

* ``session_meta``   payload ``id``, ``cwd``, ``git.branch``, ``cli_version``, ``originator``
* ``turn_context``   payload ``model`` (the model for token_count events that follow)
* ``event_msg`` / ``token_count``  payload ``info.last_token_usage`` (one assistant turn)
* ``response_item`` / ``message`` with ``role: user``  (a prompt)
* ``response_item`` / ``function_call`` | ``custom_tool_call`` (+ ``*_output``)  keyed by ``call_id``

Token semantics: in ``last_token_usage`` ``cached_input_tokens`` is a SUBSET of
``input_tokens`` and ``reasoning_output_tokens`` is already inside ``output_tokens``.
We store Claude-style disjoint buckets: input = input - cached - cache_write.
``cache_write_input_tokens`` was 0 in every row examined, so treating it as a
subset of input is an assumption (see docs/KNOWN_LIMITATIONS.md).

Keys: uuid and message_id are ``<session_id>:<ordinal>`` (the line number when a
record has no ``ordinal``), so replaying a file overwrites instead of duplicating.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple

from .db import _encode_slug

PROMPT_CLIP = 2000      # prompt_text is clipped; prompt_chars keeps the real length
TARGET_CLIP = 500


@dataclass
class State:
    """Carried between lines (and, persisted, between incremental scans of one file)."""
    session_id: str
    cwd: Optional[str] = None
    branch: Optional[str] = None
    version: Optional[str] = None
    originator: Optional[str] = None
    model: Optional[str] = None
    last_uuid: Optional[str] = None   # message the next tool call is attached to
    next_line: int = 0                # 0-based number of the next line to read
    meta_seen: bool = False           # only the file's first session_meta names the session

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, raw: str, fallback_id: str) -> "State":
        try:
            d = json.loads(raw)
            return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d}) if isinstance(d, dict) \
                else cls(fallback_id)
        except (ValueError, TypeError):
            return cls(fallback_id)


def _n(v) -> int:
    """A token count: non-negative int/float, or a numeric string; anything else is 0."""
    if isinstance(v, str):
        try:
            v = float(v.strip())
        except ValueError:
            return 0
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0


def usage_buckets(u: dict) -> dict:
    inp, cached, cw = _n(u.get("input_tokens")), _n(u.get("cached_input_tokens")), _n(u.get("cache_write_input_tokens"))
    return {
        "input_tokens":           max(0, inp - cached - cw),
        "output_tokens":          _n(u.get("output_tokens")),
        "cache_read_tokens":      cached,
        "cache_create_5m_tokens": cw,
        "cache_create_1h_tokens": 0,
    }


def blocks_text(content) -> str:
    """Flatten str | list of text blocks into one string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b)
                         for b in content if not isinstance(b, dict) or "text" in b)
    return ""


def _target(payload: dict) -> Optional[str]:
    raw = payload.get("input") if isinstance(payload.get("input"), str) else payload.get("arguments")
    if not isinstance(raw, str):
        return None
    try:
        args = json.loads(raw)
    except ValueError:
        args = None
    if isinstance(args, dict):
        cmd = args.get("cmd", args.get("command"))
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        if isinstance(cmd, str):
            raw = cmd
    return raw[:TARGET_CLIP]


def _slug(st: State) -> str:
    return _encode_slug(st.cwd) if st.cwd else "unknown"


def _base(st: State, key: str, ts: str) -> dict:
    return {
        "uuid": key, "parent_uuid": None, "session_id": st.session_id,
        "project_slug": _slug(st),
        "cwd": st.cwd, "git_branch": st.branch, "cc_version": st.version, "entrypoint": st.originator,
        "is_sidechain": 0, "agent_id": None, "timestamp": ts, "model": None, "stop_reason": None,
        "prompt_id": None, "message_id": key,
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
        "prompt_text": None, "prompt_chars": None, "tool_calls_json": None, "speed": None,
        "provider": "codex",
    }


def parse_record(rec: dict, st: State) -> Tuple[List[dict], List[dict]]:
    """Update ``st`` from one record; return (message rows, tool_call rows). Advances ``st.next_line``."""
    line_no = st.next_line
    st.next_line += 1
    p, typ = rec.get("payload"), rec.get("type")
    if not isinstance(p, dict):
        return [], []
    ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else ""
    ordinal = rec.get("ordinal") if isinstance(rec.get("ordinal"), int) and not isinstance(rec.get("ordinal"), bool) else line_no
    key, pt = f"{st.session_id}:{ordinal}", p.get("type")
    if typ == "session_meta":
        if st.meta_seen:
            return [], []       # a resumed/forked rollout may embed its parent's meta: keep this session's id
        st.meta_seen = True
        st.session_id = p.get("id") or st.session_id
        st.cwd = p.get("cwd") or st.cwd
        git = p.get("git") if isinstance(p.get("git"), dict) else {}
        st.branch = git.get("branch") or st.branch
        st.version = p.get("cli_version") or st.version
        st.originator = p.get("originator") or st.originator
        return [], []
    if typ == "turn_context":
        st.model = p.get("model") or st.model
        return [], []
    if not ts:
        return [], []
    if typ == "event_msg" and pt == "token_count":
        u = (p.get("info") or {}).get("last_token_usage") if isinstance(p.get("info"), dict) else None
        if not isinstance(u, dict):
            return [], []
        m = _base(st, key, ts)
        m.update(type="assistant", model=st.model, **usage_buckets(u))
        st.last_uuid = key
        return [m], []
    if typ != "response_item":
        return [], []
    if pt == "message" and p.get("role") == "user":
        text = blocks_text(p.get("content"))
        m = _base(st, key, ts)
        m.update(type="user", prompt_text=text[:PROMPT_CLIP], prompt_chars=len(text))
        st.last_uuid = key
        return [m], []
    if pt in ("function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output"):
        if not st.last_uuid:
            return [], []   # nothing to attach to yet (no prompt or turn seen)
        call_id = p.get("call_id") if isinstance(p.get("call_id"), str) else key
        t = {"message_uuid": st.last_uuid, "session_id": st.session_id, "project_slug": _slug(st),
             "timestamp": ts, "tool_use_id": call_id, "provider": "codex", "is_error": 0}
        if pt.endswith("_output"):
            out = p.get("output")
            text = out if isinstance(out, str) else blocks_text(out)
            t.update(tool_name="_tool_result", target=call_id, result_tokens=len(text) // 4, result_chars=len(text))
        else:
            t.update(tool_name=p.get("name") or "unknown", target=_target(p), result_tokens=None, result_chars=None)
        return [], [t]
    return [], []
