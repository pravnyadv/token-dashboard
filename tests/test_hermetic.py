"""`unittest discover tests` imports test modules as top-level modules, so tests/__init__.py does not run.
Importing hermetic here (discovery imports every module before running any test) covers that case."""
import os
import unittest

try:
    from . import hermetic  # noqa: F401  (as tests.test_hermetic)
except ImportError:
    import hermetic  # noqa: F401  (top-level module under `discover tests`)


class HermeticTests(unittest.TestCase):
    def test_codex_dir_is_not_the_real_one(self):
        real = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
        self.assertNotEqual(os.path.abspath(os.environ["CODEX_SESSIONS_DIR"]), os.path.abspath(real))
        self.assertFalse(os.path.isdir(os.environ["CODEX_SESSIONS_DIR"]))


if __name__ == "__main__":
    unittest.main()
