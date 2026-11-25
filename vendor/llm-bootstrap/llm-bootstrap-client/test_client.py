"""The client's suite, as the gate runs every suite: `python test_client.py`.

The rows run in node - the only thing this part needs that Python does not
bring, and the contract's own suite needs it too - on the kit and the page
the parts build now. Whether the committed llm-bootstrap-kit.js and
index.html are stale is the gate's check.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    env = dict(os.environ, PYTHON=sys.executable)
    run = subprocess.run(["node", "test_client.js"], cwd=HERE, env=env, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    sys.stdout.write(run.stdout)
    sys.stdout.write(run.stderr)
    return run.returncode


if __name__ == "__main__":
    sys.exit(main())
