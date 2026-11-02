import json
import os
import shutil
import tempfile
import unittest
import urllib.error

from token_dashboard.codex_scan import scan_codex
from token_dashboard.db import init_db
from token_dashboard.scanner import scan_dir
from tests.codex_fixture import FIXTURES, S1, S2, S4, Server, copy_codex, rows

ROLLOUT_S1 = lambda root: os.path.join(root, "2026/10/22", f"rollout-2026-10-22T10-00-00-{S1}.jsonl")
ROLLOUT_S4 = lambda root: os.path.join(root, "2026/10/24", f"rollout-2026-10-24T08-00-00-{S4}.jsonl")
DUMP = "SELECT * FROM messages ORDER BY uuid"
TOOLS = "SELECT message_uuid, tool_name, target, result_chars, tool_use_id FROM tool_calls ORDER BY tool_use_id, tool_name"


class CodexBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, "t.db")
        self.root = copy_codex(os.path.join(self.tmp, "sessions"))
        init_db(self.db)


class ScanTests(CodexBase):
    def test_token_subtraction_on_sample(self):
        scan_codex(self.root, self.db)
        r = rows(self.db, "SELECT * FROM messages WHERE session_id=? AND type='assistant' "
                          "ORDER BY timestamp LIMIT 2", (S1,))
        self.assertEqual([x["input_tokens"] for x in r], [7000, 1500])       # 9000-2000, 9500-8000
        self.assertEqual([x["cache_read_tokens"] for x in r], [2000, 8000])
        self.assertEqual([x["output_tokens"] for x in r], [120, 300])
        self.assertEqual([(x["cache_create_5m_tokens"], x["cache_create_1h_tokens"]) for x in r], [(0, 0)] * 2)
        self.assertEqual({x["provider"] for x in r}, {"codex"})
        self.assertEqual({x["model"] for x in r}, {"gpt-5.5"})

    def test_cache_write_is_subtracted_from_input_and_input_never_negative(self):
        from token_dashboard.codex_parse import usage_buckets
        self.assertEqual(usage_buckets({"input_tokens": 100, "cached_input_tokens": 30,
                                        "cache_write_input_tokens": 20, "output_tokens": 5})["input_tokens"], 50)
        b = usage_buckets({"input_tokens": 10, "cached_input_tokens": 30, "output_tokens": 1})
        self.assertEqual((b["input_tokens"], b["cache_read_tokens"]), (0, 30))

    def test_keys_slug_model_and_prompt(self):
        scan_codex(self.root, self.db)
        r = rows(self.db, "SELECT * FROM messages WHERE session_id=? ORDER BY uuid", (S2,))
        self.assertTrue(all(x["uuid"] == x["message_id"] == f"{S2}:" + x["uuid"].split(":")[1] for x in r))
        self.assertEqual({x["project_slug"] for x in r}, {"-work-other-app"})
        by = {x["uuid"].split(":")[1]: x for x in r}
        self.assertEqual(by["2"]["prompt_text"], "list the files")
        self.assertEqual(by["4"]["model"], "gpt-5.5")                  # latest preceding turn_context
        self.assertEqual(by["8"]["model"], "codex-unpriced-model")
        self.assertNotIn("3", by)                                      # token_count with info=null is not a turn

    def test_line_number_when_no_ordinal(self):
        d = os.path.join(self.tmp, "noord"); os.makedirs(d)
        with open(os.path.join(d, "rollout-x.jsonl"), "w") as f:
            f.write(json.dumps({"timestamp": "2026-10-22T00:00:00Z", "type": "session_meta", "payload": {"id": "S", "cwd": "/a"}}) + "\n")
            f.write(json.dumps({"timestamp": "2026-10-22T00:00:01Z", "type": "event_msg", "payload": {"type": "token_count",
                   "info": {"last_token_usage": {"input_tokens": 5, "output_tokens": 1}}}}) + "\n")
        scan_codex(d, self.db)
        self.assertEqual(rows(self.db, "SELECT uuid FROM messages")[0]["uuid"], "S:1")

    def test_tool_calls(self):
        scan_codex(self.root, self.db)
        t = {(x["tool_use_id"], x["tool_name"]): x for x in rows(self.db, TOOLS)}
        self.assertEqual(t[("c1", "exec")]["target"], "sqlite3 db 'select 1'")
        self.assertEqual(t[("c2", "shell")]["target"], "bash -lc ls")  # JSON arguments unwrapped
        self.assertEqual(t[("c1", "_tool_result")]["result_chars"], 1)
        self.assertEqual(t[("c2", "_tool_result")]["result_chars"], len("a.txt\nb.txt"))
        # nothing leaks the result text itself
        self.assertNotIn("a.txt", json.dumps(rows(self.db, "SELECT * FROM tool_calls")))

    def test_resumed_session_is_not_double_counted(self):
        scan_codex(self.root, self.db)
        r = rows(self.db, "SELECT COUNT(*) n, SUM(input_tokens+cache_read_tokens) t FROM messages "
                          "WHERE session_id=? AND type='assistant'", (S1,))[0]
        self.assertEqual(r["n"], 3)                                    # 2 original + 1 new, replayed rows overwritten
        self.assertEqual(r["t"], 9000 + 9500 + 12000)
        self.assertEqual(len(rows(self.db, "SELECT 1 FROM tool_calls WHERE session_id=?", (S1,))), 2)

    def test_rescan_changes_nothing(self):
        first = scan_codex(self.root, self.db)
        self.assertEqual(first["files"], 4)
        before = (rows(self.db, DUMP), rows(self.db, TOOLS), rows(self.db, "SELECT path, mtime, bytes_read FROM files ORDER BY path"))
        again = scan_codex(self.root, self.db)
        # (the truncated S4 file is legitimately re-read: its mark is behind EOF; it yields no rows)
        self.assertEqual((again["messages"], again["tools"]), (0, 0))
        self.assertEqual(before, (rows(self.db, DUMP), rows(self.db, TOOLS), rows(self.db, "SELECT path, mtime, bytes_read FROM files ORDER BY path")))

    def test_full_replay_is_idempotent(self):
        scan_codex(self.root, self.db)
        before = (rows(self.db, DUMP), rows(self.db, TOOLS))
        rows(self.db, "SELECT 1")  # (connection helper smoke)
        from token_dashboard.db import connect
        with connect(self.db, provider="all") as c:   # what the one-time backfills do
            c.execute("UPDATE files SET bytes_read = 0, mtime = 0"); c.commit()
        self.assertEqual(scan_codex(self.root, self.db)["files"], 4)
        self.assertEqual(before, (rows(self.db, DUMP), rows(self.db, TOOLS)))

    def test_appended_lines_are_read_incrementally_with_carried_state(self):
        scan_codex(self.root, self.db)
        p = ROLLOUT_S1(self.root)
        size = os.path.getsize(p)
        with open(p, "a") as f:
            f.write(json.dumps({"timestamp": "2026-10-22T10:05:00.000Z", "ordinal": 20, "type": "event_msg",
                                "payload": {"type": "token_count", "info": {"last_token_usage": {
                                    "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}}}}) + "\n")
            f.write(json.dumps({"timestamp": "2026-10-22T10:05:01.000Z", "ordinal": 21, "type": "response_item",
                                "payload": {"type": "function_call", "call_id": "c9", "name": "exec", "arguments": "ls"}}) + "\n")
        n = scan_codex(self.root, self.db)
        self.assertEqual((n["messages"], n["tools"]), (1, 1))   # only the new lines
        new = rows(self.db, "SELECT * FROM messages WHERE uuid=?", (f"{S1}:20",))[0]
        self.assertEqual((new["input_tokens"], new["cache_read_tokens"], new["model"], new["cwd"]),
                         (60, 40, "gpt-5.5", "/work/demo-app"))             # model/cwd carried from saved state
        self.assertEqual(rows(self.db, "SELECT message_uuid FROM tool_calls WHERE tool_use_id='c9'")[0]["message_uuid"], f"{S1}:20")
        self.assertEqual(rows(self.db, "SELECT bytes_read FROM files WHERE path=?", (p,))[0]["bytes_read"], os.path.getsize(p))
        self.assertGreater(os.path.getsize(p), size)

    def test_truncated_last_line_is_retried_when_completed(self):
        n = scan_codex(self.root, self.db)
        p = ROLLOUT_S4(self.root)
        self.assertEqual(len(rows(self.db, "SELECT 1 FROM messages WHERE session_id=?", (S4,))), 2)  # prompt + 1 turn
        read = rows(self.db, "SELECT bytes_read FROM files WHERE path=?", (p,))[0]["bytes_read"]
        self.assertLess(read, os.path.getsize(p))                       # high-water mark stops before the partial line
        with open(p, "a") as f:
            f.write('unt", "info": {"last_token_usage": {"input_tokens": 50, "output_tokens": 5}}}}\n')
        scan_codex(self.root, self.db)
        r = rows(self.db, "SELECT input_tokens FROM messages WHERE uuid=?", (f"{S4}:4",))
        self.assertEqual([x["input_tokens"] for x in r], [50])

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(scan_codex(os.path.join(self.tmp, "nope"), self.db), {"messages": 0, "tools": 0, "files": 0})
        os.environ["CODEX_SESSIONS_DIR"] = os.path.join(self.tmp, "also-nope")
        self.addCleanup(os.environ.pop, "CODEX_SESSIONS_DIR", None)
        r = scan_dir(os.path.join(self.tmp, "no-claude"), self.db)
        self.assertEqual(r["codex"]["files"], 0)
        self.assertEqual(r["files"], 0)

    def test_scan_dir_entry_point_covers_both(self):
        proj = os.path.join(self.tmp, "projects", "C--work"); os.makedirs(proj)
        shutil.copy(os.path.join(FIXTURES, "sample_session.jsonl"), os.path.join(proj, "s1.jsonl"))
        r = scan_dir(os.path.join(self.tmp, "projects"), self.db, codex_dir=self.root)
        self.assertEqual((r["files"], r["codex"]["files"]), (1, 4))


CLAUDE_ONLY = ["/api/cache", "/api/compactions", "/api/tips", "/api/skills", "/api/findings", "/api/agent-types",
               "/api/whatif", "/api/whatif?group=model", "/api/tool-costs", "/api/skill-costs", "/api/tool-costs/calls",
               "/api/prompts", "/api/recent", "/api/recent/buckets", "/api/activity", "/api/tools",
               "/api/session-timeline?id=s1", "/api/sessions/s1", "/api/session-subagents?id=s1",
               "/api/findings/preview", "/api/tool-costs/params?tool=Read"]


class ProviderApiTests(CodexBase):
    def setUp(self):
        super().setUp()
        proj = os.path.join(self.tmp, "projects", "C--work"); os.makedirs(proj)
        shutil.copy(os.path.join(FIXTURES, "sample_session.jsonl"), os.path.join(proj, "s1.jsonl"))
        scan_dir(os.path.join(self.tmp, "projects"), self.db, codex_dir=os.path.join(self.tmp, "none"))
        self.srv = Server(self.db)
        self.addCleanup(self.srv.close)

    def snapshot(self):
        out = {}
        for p in CLAUDE_ONLY:
            for suffix in ("", ("&" if "?" in p else "?") + "provider=codex", ("&" if "?" in p else "?") + "provider=all"):
                try:
                    out[p + suffix] = self.srv.get(p + suffix)
                except urllib.error.HTTPError as e:
                    out[p + suffix] = e.code
        return out

    def test_claude_only_endpoints_identical_with_and_without_codex(self):
        before = self.snapshot()
        self.assertTrue(any(isinstance(v, (list, dict)) and v for v in before.values()))
        scan_codex(self.root, self.db)
        self.assertGreater(rows(self.db, "SELECT COUNT(*) n FROM messages WHERE provider='codex'")[0]["n"], 0)
        after = self.snapshot()
        self.assertEqual(before, after)
        # ?provider= is ignored on them: codex/all give the same answer as no parameter
        for p in CLAUDE_ONLY:
            sep = "&" if "?" in p else "?"
            self.assertEqual(after[p], after[p + sep + "provider=codex"])

    def test_default_provider_is_claude_and_all_is_the_sum(self):
        before = self.srv.get("/api/overview")
        scan_codex(self.root, self.db)
        claude, codex, both = (self.srv.get("/api/overview?provider=" + p) for p in ("claude", "codex", "all"))
        self.assertEqual(self.srv.get("/api/overview"), claude)
        self.assertEqual(before, claude)
        for k in ("sessions", "turns", "input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_create_5m_tokens", "cache_create_1h_tokens"):
            self.assertEqual(both[k], claude[k] + codex[k], k)
        self.assertAlmostEqual(both["cost_usd"], claude["cost_usd"] + codex["cost_usd"], places=3)
        self.assertEqual((codex["sessions"], codex["turns"]), (3, 4))        # S1, S2, S4; turns = prompts (S1 has 2)
        self.assertEqual(codex["input_tokens"], 7000 + 1500 + 3000 + 5000 + 500 + 2000)   # replayed rows not doubled

    def test_daily_models_sessions_projects_split_by_provider(self):
        scan_codex(self.root, self.db)
        for path, key in (("/api/daily", "input_tokens"), ("/api/projects", "input_tokens")):
            tot = lambda p: sum(r[key] for r in self.srv.get(f"{path}?provider={p}"))
            self.assertEqual(tot("all"), tot("claude") + tot("codex"), path)
        self.assertEqual({r["day"] for r in self.srv.get("/api/daily?provider=codex")}, {"2026-10-22", "2026-10-23", "2026-10-24"})
        self.assertEqual({r["session_id"] for r in self.srv.get("/api/sessions?provider=claude")}, {"s1"})
        self.assertEqual({r["session_id"] for r in self.srv.get("/api/sessions?provider=codex")}, {S1, S2, S4})
        self.assertEqual({r["project_slug"] for r in self.srv.get("/api/projects?provider=codex")}, {"-work-demo-app", "-work-other-app"})
        self.assertTrue(self.srv.get("/api/repos?provider=codex"))
        self.assertNotEqual(self.srv.get("/api/repos?provider=codex"), self.srv.get("/api/repos?provider=claude"))

    def test_unpriced_models_are_flagged_not_priced(self):
        scan_codex(self.root, self.db)
        models = {r["model"]: r for r in self.srv.get("/api/by-model?provider=codex")}
        self.assertFalse(models["gpt-5.5"]["unpriced"])
        self.assertGreater(models["gpt-5.5"]["cost_usd"], 0)
        self.assertTrue(models["codex-unpriced-model"]["unpriced"])
        self.assertIsNone(models["codex-unpriced-model"]["cost_usd"])
        self.assertEqual(self.srv.get("/api/overview?provider=codex")["unpriced_tokens"], 6000 + 400)
        s = {r["session_id"]: r for r in self.srv.get("/api/sessions?provider=codex")}
        self.assertTrue(s[S2]["cost_unpriced"])
        self.assertFalse(s[S1]["cost_unpriced"])
        # the Claude shape has none of these fields
        self.assertNotIn("unpriced", self.srv.get("/api/by-model")[0])
        self.assertNotIn("unpriced_tokens", self.srv.get("/api/overview"))

    def test_invalid_provider_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.srv.get("/api/overview?provider=gemini")
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
