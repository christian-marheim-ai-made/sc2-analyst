#!/usr/bin/env python3
"""Run the functional suite.

    .venv/Scripts/python.exe tests/run.py [-v]

Tests SKIP rather than fail when an input is missing, so a checkout without the gitignored
.SC2Replay / .SC2Map files still reports honestly instead of going red.

`mpyq` does not close the archive handles it opens, which produces ResourceWarning noise on
CPython. That is a property of the dependency, not of these tests, so it is filtered here and
nowhere else.
"""
import sys
import unittest
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

warnings.filterwarnings("ignore", category=ResourceWarning)

if __name__ == "__main__":
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.defaultTestLoader.discover(str(HERE), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    print()
    print("run=%d  ok=%d  failed=%d  errors=%d  skipped=%d"
          % (result.testsRun,
             result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
             len(result.failures), len(result.errors), len(result.skipped)))
    sys.exit(0 if result.wasSuccessful() else 1)
