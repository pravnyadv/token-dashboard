"""Shared helpers: synthetic Codex rollouts (tests/fixtures/codex) in a temp dir, and a running server."""
import http.server
import json
import os
import shutil
import socket
import tempfile
import threading
import urllib.request

from token_dashboard.db import init_db, connect
from token_dashboard.server import build_handler

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CODEX = os.path.join(FIXTURES, "codex")
S1 = "01a198c7-0000-4000-8000-000000000001"
S2 = "01a198c7-0000-4000-8000-000000000002"
S4 = "01a198c7-0000-4000-8000-000000000004"


def copy_codex(dst):
    shutil.copytree(CODEX, dst)
    return dst


def rows(db, sql, args=()):
    with connect(db, provider="all") as c:
        return [dict(r) for r in c.execute(sql, args)]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self, db):
        self.port = free_port()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), build_handler(db, "/nonexistent"))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return json.loads(r.read())

    def post(self, path, body, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
