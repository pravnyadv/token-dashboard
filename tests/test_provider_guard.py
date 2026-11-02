"""Writers of messages/tool_calls must connect with provider="all": the default connection sees
a read-only per-provider VIEW, so a write through it fails at runtime. Catch that statically."""
import re
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "token_dashboard"
WRITE = re.compile(r"(INSERT[^\"']*INTO|UPDATE|DELETE\s+FROM)\s+(messages|tool_calls)\b", re.I)
ALLOWED = {"scanner.py", "codex_scan.py", "db.py"}   # these connect with provider="all" (or use a passed-in raw conn)


class ProviderGuardTests(unittest.TestCase):
    def test_only_scanners_write_messages_and_tool_calls(self):
        offenders = [f"{p.name}:{n}" for p in sorted(PKG.glob("*.py")) if p.name not in ALLOWED
                     for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if WRITE.search(line)]
        self.assertEqual(offenders, [])

    def test_scanners_open_the_raw_tables(self):
        for name in ("scanner.py", "codex_scan.py"):
            src = (PKG / name).read_text(encoding="utf-8")
            self.assertIn('connect(db_path, provider="all")', src, name)

    def test_a_default_connection_cannot_write(self):
        import os, sqlite3, tempfile
        from token_dashboard.db import connect, init_db
        db = os.path.join(tempfile.mkdtemp(), "g.db"); init_db(db)
        with connect(db) as c:
            with self.assertRaises(sqlite3.OperationalError):
                c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp) "
                          "VALUES ('x','s','p','user','2026-01-01T00:00:00Z')")

    def test_thread_does_not_inherit_the_scope(self):
        import threading
        from token_dashboard import provider
        seen = []
        with provider.scope("codex"):
            t = threading.Thread(target=lambda: seen.append(provider.current())); t.start(); t.join()
            self.assertEqual(provider.current(), "codex")
        self.assertEqual(seen, ["claude"])      # documented: a thread must enter scope() itself


if __name__ == "__main__":
    unittest.main()
