"""Keep tests off the developer's real data: scans must not read ~/.codex.

`scan_dir` also scans CODEX_SESSIONS_DIR (default ~/.codex/sessions), which on a real machine holds
real transcripts. Point it at a directory that does not exist unless a test sets its own.
"""
import os
import tempfile

os.environ.setdefault("CODEX_SESSIONS_DIR", os.path.join(tempfile.gettempdir(), "token-dashboard-tests-no-codex"))
