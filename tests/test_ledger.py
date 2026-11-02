import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone

from token_dashboard import ledger, ledger_metrics as lm
from token_dashboard.cache_insights import cache_report
from token_dashboard.compaction_insights import compactions_report
from token_dashboard.db import connect, init_db
from token_dashboard.pricing import cost_for, load_pricing
from token_dashboard.scanner import scan_dir
from tests.codex_fixture import FIXTURES, Server, copy_codex, rows

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICING = load_pricing(os.path.join(ROOT, "pricing.json"))
APPLIED = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
HAIKU, OPUS = "claude-haiku-4-5", "claude-opus-4-7"     # neither has a long-context price band
_n = [0]


def put(db, day, model=HAIKU, inp=0, out=0, cr=0, c5=0, sid="s", hhmm="10:00", provider="claude", agent=None):
    _n[0] += 1
    with connect(db, provider="all") as c:
        c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model, input_tokens, "
                  "output_tokens, cache_read_tokens, cache_create_5m_tokens, provider, agent_id) "
                  "VALUES (?, ?, 'p', 'assistant', ?, ?, ?, ?, ?, ?, ?, ?)",
                  (f"u{_n[0]}", sid, f"{day}T{hhmm}:00Z", model, inp, out, cr, c5, provider, agent))
        c.commit()


def day(offset):  # days relative to the apply day, 2026-10-20
    return (APPLIED.date() + timedelta(days=offset)).isoformat()


class LedgerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def week(self, start, per_day_tokens):
        """One haiku turn per day starting at day(start): input tokens from the list ($1 per Mtok)."""
        for i, t in enumerate(per_day_tokens):
            put(self.db, day(start + i), inp=t)

    def apply(self, metric="cost_per_day_usd", **kw):
        return ledger.apply(self.db, PRICING, "rule", "trim CLAUDE.md", metric, now=kw.get("now", APPLIED))


class BaselineTests(LedgerBase):
    def test_baseline_is_the_mean_of_the_seven_days_before(self):
        self.week(-7, [100_000 * (i + 1) for i in range(7)])          # $0.1 .. $0.7 -> mean $0.4
        put(self.db, day(0), inp=9_000_000)                            # the apply day itself is excluded
        put(self.db, day(-8), inp=9_000_000)                           # so is the day before the window
        e = self.apply()
        self.assertAlmostEqual(e["baseline"], 0.4, places=6)
        self.assertEqual((e["days_before"], e["after"], e["verdict"], e["checked_at"]), (7, None, None, None))
        self.assertEqual((e["kind"], e["metric"], e["description"]), ("rule", "cost_per_day_usd", "trim CLAUDE.md"))
        self.assertEqual(e["applied_at"], APPLIED.isoformat())

    def test_thin_sample_is_visible_in_days_before(self):
        for off, t in ((-6, 100_000), (-3, 300_000), (-1, 500_000)):
            put(self.db, day(off), inp=t)
        e = self.apply()
        self.assertEqual(e["days_before"], 3)
        self.assertAlmostEqual(e["baseline"], 0.3, places=6)           # mean of the 3 days with data, not /7

    def test_no_data_baseline_is_none(self):
        e = self.apply()
        self.assertEqual((e["baseline"], e["days_before"]), (None, 0))

    def test_unknown_metric_kind_and_description_are_rejected_and_not_stored(self):
        for args in (("rule", "x", "nope"), ("sparkles", "x", "cost_per_day_usd"), ("rule", "  ", "cost_per_day_usd"),
                     ("rule", "x" * 501, "cost_per_day_usd"), ("rule", None, "cost_per_day_usd")):
            with self.assertRaises(ValueError):
                ledger.apply(self.db, PRICING, *args, now=APPLIED)
        self.assertEqual(ledger.entries(self.db), [])


class VerdictTests(unittest.TestCase):
    def v(self, metric, base, after):
        return ledger.verdict_for(metric, base, after)

    def test_lower_is_better(self):
        self.assertEqual(self.v("cost_per_day_usd", 100, 80), "improved")
        self.assertEqual(self.v("cost_per_day_usd", 100, 120), "worse")
        self.assertEqual(self.v("cost_per_day_usd", 100, 100), "no_change")
        self.assertEqual(self.v("cost_per_day_usd", 100, 95), "no_change")
        self.assertEqual(self.v("compactions_per_day", 2, 3), "worse")

    def test_higher_is_better(self):
        self.assertEqual(self.v("cache_hit_ratio", 0.5, 0.9), "improved")
        self.assertEqual(self.v("cache_hit_ratio", 0.5, 0.2), "worse")
        self.assertEqual(self.v("cache_hit_ratio", 0.5, 0.52), "no_change")

    def test_exact_ten_percent_boundary_counts_as_a_change(self):
        self.assertEqual(self.v("cost_per_day_usd", 100, 90), "improved")
        self.assertEqual(self.v("cost_per_day_usd", 100, 110), "worse")
        self.assertEqual(self.v("cost_per_day_usd", 100, 90.01), "no_change")
        self.assertEqual(self.v("cost_per_day_usd", 100, 109.99), "no_change")
        self.assertEqual(self.v("cache_hit_ratio", 0.5, 0.55), "improved")
        self.assertEqual(self.v("cache_hit_ratio", 0.5, 0.45), "worse")
        self.assertEqual(self.v("cost_per_day_usd", 0.3, 0.33), "worse")     # 0.03/0.3 is 0.0999.. in floats
        self.assertEqual(self.v("cost_per_day_usd", 0.3, 0.27), "improved")

    def test_zero_baseline_and_missing_data(self):
        self.assertEqual(self.v("cost_per_day_usd", 0, 0), "no_change")
        self.assertEqual(self.v("cost_per_day_usd", 0, 1), "worse")
        self.assertEqual(self.v("cache_hit_ratio", 0, 0.1), "improved")
        self.assertEqual(self.v("cost_per_day_usd", None, 5), "no_change")
        self.assertEqual(self.v("cost_per_day_usd", 5, None), "no_change")


class CheckTests(LedgerBase):
    def test_improved_with_after_window_excluding_the_apply_day_and_day_eight(self):
        self.week(-7, [400_000] * 7)                                   # baseline $0.4
        e = self.apply()
        self.week(1, [200_000] * 7)                                    # days +1..+7 -> $0.2
        put(self.db, day(0), inp=9_000_000)                            # apply day: in neither window
        put(self.db, day(8), inp=9_000_000)                            # day 8: not yet part of the window
        done = ledger.check(self.db, PRICING, today=date.fromisoformat(day(8)), now=APPLIED + timedelta(days=8))
        self.assertEqual([x["id"] for x in done], [e["id"]])
        r = done[0]
        self.assertEqual((r["verdict"], r["days_after"], r["days_before"]), ("improved", 7, 7))
        self.assertAlmostEqual(r["after"], 0.2, places=6)
        self.assertIsNotNone(r["checked_at"])

    def test_worse_and_no_change(self):
        self.week(-7, [400_000] * 7)
        a = self.apply()
        b = self.apply(now=APPLIED + timedelta(minutes=1))
        self.week(1, [600_000] * 7)                                    # +50% -> worse
        for x in ledger.check(self.db, PRICING, today=date.fromisoformat(day(8))):
            self.assertEqual(x["verdict"], "worse")
        c = self.apply(now=APPLIED + timedelta(days=10))               # another entry; judged later
        self.assertEqual(len(ledger.entries(self.db)), 3)
        # a flat week: same level before and after
        db2 = os.path.join(self.tmp, "flat.db"); init_db(db2)
        for off in list(range(-7, 0)) + list(range(1, 8)):
            put(db2, day(off), inp=400_000)
        ledger.apply(db2, PRICING, "habit", "flat", "cost_per_day_usd", now=APPLIED)
        self.assertEqual(ledger.check(db2, PRICING, today=date.fromisoformat(day(8)))[0]["verdict"], "no_change")

    def test_higher_is_better_metric_end_to_end(self):
        for off in range(-7, 0):
            put(self.db, day(off), inp=500, cr=500)                    # hit ratio 0.5
        self.apply("cache_hit_ratio")
        for off in range(1, 8):
            put(self.db, day(off), inp=100, cr=900)                    # hit ratio 0.9
        r = ledger.check(self.db, PRICING, today=date.fromisoformat(day(8)))[0]
        self.assertEqual((r["verdict"], round(r["baseline"], 3), round(r["after"], 3)), ("improved", 0.5, 0.9))

    def test_no_data_in_either_window_is_no_change(self):
        e = self.apply()
        r = ledger.check(self.db, PRICING, today=date.fromisoformat(day(30)))[0]
        self.assertEqual((r["id"], r["verdict"], r["baseline"], r["after"], r["days_before"], r["days_after"]),
                         (e["id"], "no_change", None, None, 0, 0))
        self.assertIsNotNone(r["checked_at"])                          # still judged once

    def test_baseline_only_or_after_only_is_no_change(self):
        self.week(-7, [400_000] * 7)
        self.apply()
        r = ledger.check(self.db, PRICING, today=date.fromisoformat(day(9)))[0]   # no data after
        self.assertEqual((r["verdict"], r["after"], r["days_after"]), ("no_change", None, 0))

    def test_younger_than_a_full_week_stays_pending(self):
        self.week(-7, [400_000] * 7)
        e = self.apply()
        self.week(1, [100_000] * 7)
        for off in (0, 3, 6, 7):                                       # day +7 is not over yet
            self.assertEqual(ledger.check(self.db, PRICING, today=date.fromisoformat(day(off))), [], off)
        row = ledger.entries(self.db)[0]
        self.assertEqual((row["status"], row["verdict"], row["after"], row["due_on"]), ("pending", None, None, day(8)))
        self.assertEqual(len(ledger.check(self.db, PRICING, today=date.fromisoformat(day(8)))), 1)

    def test_checking_twice_changes_nothing(self):
        self.week(-7, [400_000] * 7)
        self.apply()
        self.week(1, [100_000] * 7)
        today = date.fromisoformat(day(8))
        first = ledger.check(self.db, PRICING, today=today, now=APPLIED + timedelta(days=8))
        snap = ledger.entries(self.db)
        put(self.db, day(3), inp=50_000_000)                           # late data must not re-judge it
        self.assertEqual(ledger.check(self.db, PRICING, today=today + timedelta(days=5)), [])
        self.assertEqual(ledger.entries(self.db), snap)
        self.assertEqual(len(first), 1)

    def test_set_result_is_once_only(self):
        e = self.apply()
        from token_dashboard import ledger_store as s
        self.assertIsNotNone(s.ledger_set_result(self.db, e["id"], "t1", 1.0, "worse", 3))
        self.assertIsNone(s.ledger_set_result(self.db, e["id"], "t2", 9.0, "improved", 7))
        self.assertEqual(ledger.entries(self.db)[0]["verdict"], "worse")


class MetricTests(LedgerBase):
    def win(self, end=0, n=1):
        return lm.window(self.db, PRICING, date.fromisoformat(day(end)), n)

    def test_the_six_metrics_and_their_directions(self):
        self.assertEqual({m["name"]: m["direction"] for m in lm.metric_list()}, {
            "cost_per_day_usd": "lower", "opus_cost_share": "lower", "cache_wasted_usd_per_day": "lower",
            "cache_hit_ratio": "higher", "compactions_per_day": "lower", "cost_share_over_200k_context": "lower"})

    def test_cost_opus_share_and_big_context_share(self):
        put(self.db, day(0), model=OPUS, inp=1_000_000)                # $5
        put(self.db, day(0), model=HAIKU, inp=1_000_000)               # $1, context 1M > 200k
        put(self.db, day(0), model=HAIKU, inp=100_000, hhmm="11:00")   # $0.1, small
        v = self.win()[day(0)]
        self.assertAlmostEqual(v["cost_per_day_usd"], 6.1, places=6)
        self.assertAlmostEqual(v["opus_cost_share"], 5 / 6.1, places=6)
        self.assertAlmostEqual(v["cost_share_over_200k_context"], 6 / 6.1, places=6)  # opus 1M and haiku 1M turns

    def test_context_counts_cache_tokens_and_the_threshold_is_exclusive(self):
        put(self.db, day(0), inp=100, cr=199_900)                      # exactly 200,000: not over
        put(self.db, day(0), inp=100, cr=200_000, hhmm="11:00")        # 200,100: over
        v = self.win()[day(0)]
        small = cost_for(HAIKU, {"input_tokens": 100, "output_tokens": 0, "cache_read_tokens": 199_900,
                                 "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0}, PRICING)["usd"]
        big = cost_for(HAIKU, {"input_tokens": 100, "output_tokens": 0, "cache_read_tokens": 200_000,
                               "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0}, PRICING)["usd"]
        self.assertAlmostEqual(v["cost_share_over_200k_context"], big / (big + small), places=6)

    def test_cache_metrics_reuse_the_cache_report(self):
        put(self.db, day(0), inp=0, c5=100_000, hhmm="10:00")                          # cold start
        put(self.db, day(0), inp=0, c5=100_000, hhmm="10:30")                          # 30 min later: 5m cache expired, re-written
        put(self.db, day(0), inp=1000, cr=99_000, hhmm="10:31")                        # healthy turn
        rep = {r["day"]: r for r in cache_report(self.db, PRICING)["by_day"]}[day(0)]
        v = self.win()[day(0)]
        self.assertGreater(rep["wasted_usd"], 0)
        self.assertEqual(v["cache_wasted_usd_per_day"], rep["wasted_usd"])
        self.assertEqual(v["cache_hit_ratio"], rep["hit_ratio"])
        self.assertAlmostEqual(v["cache_hit_ratio"], 99_000 / (99_000 + 200_000 + 1000), places=9)

    def test_compactions_per_day_counts_zero_for_active_days(self):
        put(self.db, day(0), inp=10)
        put(self.db, day(1), inp=10)
        with connect(self.db, provider="all") as c:
            for i in range(2):
                c.execute("INSERT INTO compactions (uuid, session_id, project_slug, timestamp, trigger) "
                          "VALUES (?, 's', 'p', ?, 'auto')", (f"c{i}", f"{day(0)}T12:0{i}:00Z"))
            c.commit()
        w = self.win(end=1, n=2)
        self.assertEqual((w[day(0)]["compactions_per_day"], w[day(1)]["compactions_per_day"]), (2, 0))
        self.assertEqual({r["day"]: r["count"] for r in compactions_report(self.db, PRICING)["by_day"]}, {day(0): 2})
        self.assertEqual(lm.daily_mean(w, "compactions_per_day"), 1.0)

    def test_days_without_activity_have_no_data_and_ratios_need_cost(self):
        put(self.db, day(0), model="not-a-priced-model", inp=1000)     # activity, but nothing priced
        v = self.win(end=1, n=2)
        self.assertEqual(set(v), {day(0)})
        self.assertEqual(v[day(0)]["cost_per_day_usd"], 0.0)
        self.assertNotIn("opus_cost_share", v[day(0)])
        self.assertEqual(lm.days_with_data(v, "opus_cost_share"), 0)
        self.assertIsNone(lm.daily_mean(v, "opus_cost_share"))

    def test_codex_rows_never_count(self):
        put(self.db, day(0), inp=100_000)
        before = self.win()
        put(self.db, day(0), model="gpt-5.5", inp=9_000_000, out=1_000_000, provider="codex", sid="cx")
        put(self.db, day(2), model="gpt-5.5", inp=9_000_000, provider="codex", sid="cx")
        self.assertEqual(self.win(), before)
        self.assertEqual(self.win(end=2, n=3).keys(), {day(0)})


class SurvivalTests(LedgerBase):
    def test_entry_survives_rescans_and_every_rebuild_path(self):
        proj = os.path.join(self.tmp, "projects", "C--work"); os.makedirs(proj)
        shutil.copy(os.path.join(FIXTURES, "sample_session.jsonl"), os.path.join(proj, "s1.jsonl"))
        root = os.path.join(self.tmp, "projects")
        none = os.path.join(self.tmp, "no-codex")
        scan_dir(root, self.db, codex_dir=none)
        e = ledger.apply(self.db, PRICING, "rule", "recent fix", "cost_per_day_usd")   # applied now: stays pending
        before = ledger.entries(self.db)
        with connect(self.db, provider="all") as c:                    # a full rescan: what the backfills do
            c.execute("UPDATE files SET bytes_read = 0, mtime = 0"); c.commit()
        self.assertEqual(scan_dir(root, self.db, codex_dir=none)["files"], 1)
        self.assertEqual(ledger.entries(self.db), before)
        with connect(self.db, provider="all") as c:                    # the destructive migrations: clear + replay
            for t in ("messages", "tool_calls", "files"):
                c.execute(f"DELETE FROM {t}")
            c.commit()
        scan_dir(root, self.db, codex_dir=none)
        init_db(self.db)                                               # re-running init is a no-op for the ledger
        self.assertEqual(ledger.entries(self.db), before)
        self.assertEqual(before[0]["id"], e["id"])

    def test_scan_runs_the_check_automatically_and_idempotently(self):
        e = ledger.apply(self.db, PRICING, "habit", "old fix", "cost_per_day_usd",
                         now=datetime.now(timezone.utc) - timedelta(days=20))
        none = os.path.join(self.tmp, "none")
        scan_dir(none, self.db, codex_dir=none)
        row = ledger.entries(self.db)[0]
        self.assertEqual((row["id"], row["status"], row["verdict"]), (e["id"], "checked", "no_change"))
        scan_dir(none, self.db, codex_dir=none)
        self.assertEqual(ledger.entries(self.db), [row])


class ApiTests(LedgerBase):
    H = {"X-Token-Dashboard": "1"}

    def setUp(self):
        super().setUp()
        self.srv = Server(self.db)
        self.addCleanup(self.srv.close)

    def post(self, path, body, headers=None):
        try:
            return self.srv.post(path, body, headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_get_lists_entries_and_metrics_with_directions(self):
        d = self.srv.get("/api/ledger")
        self.assertEqual(d["entries"], [])
        self.assertEqual({m["name"]: m["direction"] for m in d["metrics"]}["cache_hit_ratio"], "higher")
        self.assertEqual(d["kinds"], ["rule", "model", "habit", "other"])

    def test_post_computes_the_baseline_on_the_server(self):
        today = datetime.now(timezone.utc).date()
        for i in range(1, 4):
            put(self.db, (today - timedelta(days=i)).isoformat(), inp=200_000)
        code, e = self.post("/api/ledger", {"kind": "rule", "description": "shorter CLAUDE.md",
                                            "metric": "cost_per_day_usd", "baseline": 999}, self.H)
        self.assertEqual(code, 201)
        self.assertAlmostEqual(e["baseline"], 0.2, places=6)           # a client-supplied baseline is ignored
        self.assertEqual(e["days_before"], 3)
        self.assertEqual(len(self.srv.get("/api/ledger")["entries"]), 1)

    def test_post_validation_and_origin_header(self):
        body = {"kind": "rule", "description": "x", "metric": "cost_per_day_usd"}
        self.assertEqual(self.post("/api/ledger", body)[0], 403)       # no X-Token-Dashboard header
        self.assertEqual(self.post("/api/ledger/check", {})[0], 403)
        self.assertEqual(self.post("/api/ledger", {**body, "metric": "nope"}, self.H)[0], 400)
        self.assertEqual(self.post("/api/ledger", {**body, "kind": "x"}, self.H)[0], 400)
        self.assertEqual(self.post("/api/ledger", {**body, "description": ""}, self.H)[0], 400)
        self.assertEqual(self.srv.get("/api/ledger")["entries"], [])

    def test_backdated_entry_is_judged_by_the_check_endpoint_once(self):
        today = datetime.now(timezone.utc).date()
        applied = today - timedelta(days=10)
        code, e = self.post("/api/ledger", {"kind": "model", "description": "switch to haiku",
                                            "metric": "cost_per_day_usd"}, self.H)
        self.assertEqual(code, 201)
        # Backdate it in the test DB, as if it had been recorded 10 days ago with a $0.40/day baseline.
        with connect(self.db) as c:
            c.execute("UPDATE ledger SET applied_at=?, baseline=0.4, days_before=7 WHERE id=?",
                      (datetime.combine(applied, datetime.min.time(), tzinfo=timezone.utc).isoformat(), e["id"]))
            c.commit()
        for off in range(1, 8):
            put(self.db, (applied + timedelta(days=off)).isoformat(), inp=200_000)    # $0.20/day afterwards
        code, res = self.post("/api/ledger/check", {}, self.H)
        self.assertEqual(code, 200)
        (r,) = res["checked"]
        self.assertEqual((r["id"], r["verdict"], r["days_before"], r["days_after"]), (e["id"], "improved", 7, 7))
        self.assertAlmostEqual(r["after"], 0.2, places=6)
        self.assertEqual(self.post("/api/ledger/check", {}, self.H)[1], {"checked": []})   # idempotent
        row = self.srv.get("/api/ledger")["entries"][0]
        self.assertEqual((row["status"], row["verdict"]), ("checked", "improved"))


class CliTests(LedgerBase):
    def run_cli(self, *args):
        env = {**os.environ, "TOKEN_DASHBOARD_DB": self.db, "CODEX_SESSIONS_DIR": os.path.join(self.tmp, "none")}
        return subprocess.run([sys.executable, "cli.py", *args], cwd=ROOT, env=env, capture_output=True, text=True)

    def test_apply_list_check(self):
        r = self.run_cli("ledger", "list")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ledger is empty"))
        r = self.run_cli("ledger", "apply", "--kind", "habit", "--metric", "cache_hit_ratio", "start fresh sessions")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("recorded fix #1", r.stdout)
        r = self.run_cli("ledger", "list")
        self.assertIn("start fresh sessions", r.stdout)
        self.assertIn("pending", r.stdout)
        r = self.run_cli("ledger", "check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("judged 0", r.stdout)
        bad = self.run_cli("ledger", "apply", "--kind", "habit", "--metric", "nope", "x")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("unknown metric", bad.stderr)


if __name__ == "__main__":
    unittest.main()
