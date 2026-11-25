"""The data home from config (settings.py) and the runner settings handed to the bootstrap (llm.resolve)."""
from __future__ import annotations

import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from fixtures import REPO

sys.path.insert(0, str(REPO / "src"))
from sc2analyst import llm, settings  # noqa: E402


class DataHome(unittest.TestCase):
    def test_the_environment_wins(self):
        with mock.patch.dict(os.environ, {"SC2A_DATA_HOME": "C:/somewhere"}):
            self.assertEqual(settings.data_home(), Path("C:/somewhere"))

    def test_a_relative_home_is_taken_from_the_repository(self):
        with mock.patch.dict(os.environ, {"SC2A_DATA_HOME": "data-home"}):
            self.assertEqual(settings.data_home(), REPO / "data-home")

    def test_the_public_default(self):
        cfg = tomllib.loads((REPO / "config" / "analyst.toml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["data_home"], "data-home")


class Runner(unittest.TestCase):
    def resolve(self, text: str) -> tuple[Path, Path, str]:
        d = Path(self.tmp.name)
        src = d / "runner.toml"
        src.write_text(text, encoding="utf-8")
        with mock.patch.dict(os.environ, {"SC2A_DATA_HOME": str(d / "home")}):
            out = llm.resolve(src)
        return src, out, out.read_text(encoding="utf-8")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_paths_become_absolute_against_the_repository(self):
        src, out, text = self.resolve('[runner]\nllama_server = "llama/llama-server.exe"\nmodels_dir = "models"\nmax_loads = 1\n')
        self.assertNotEqual(out, src)
        r = tomllib.loads(text)["runner"]
        self.assertEqual(r["llama_server"], (REPO / "llama/llama-server.exe").as_posix())
        self.assertEqual(r["models_dir"], (REPO / "models").as_posix())
        self.assertEqual(r["max_loads"], 1)

    def test_absolute_paths_and_a_bare_command_pass_as_they_are(self):
        src, out, _ = self.resolve('[runner]\nllama_server = "llama-server"\nmodels_dir = "C:/models"\n')
        self.assertEqual(out, src)

    def test_the_public_runner_names_llama_and_models_folders(self):
        r = tomllib.loads((REPO / "config" / "runner.toml").read_text(encoding="utf-8"))["runner"]
        self.assertEqual((r["llama_server"], r["models_dir"]), ("llama/llama-server.exe", "models"))


if __name__ == "__main__":
    unittest.main()
