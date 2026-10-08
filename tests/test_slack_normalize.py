#!/usr/bin/env python3
"""Behavioral tests for the deterministic slack-normalize runner.

The runner is stdlib-only and performs no network access, so every test runs
unconditionally. It exercises the fail-closed error paths, deterministic
rendering/hashing, and the argv contract.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER_PATH = ROOT / "scripts" / "runners" / "slack_normalize.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("co_slack_normalize_runner", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


class RunnerErrorPaths(unittest.TestCase):
    def test_runner_script_ships(self) -> None:
        self.assertTrue(RUNNER_PATH.is_file())

    def test_non_json_source_fails_closed(self) -> None:
        with self.assertRaises(runner.NormalizeError) as ctx:
            runner.normalize("not a dict or list")
        self.assertEqual("SLACK_SOURCE_NOT_JSON", ctx.exception.code)

    def test_empty_message_list_fails_closed(self) -> None:
        with self.assertRaises(runner.NormalizeError) as ctx:
            runner.normalize([])
        self.assertEqual("SLACK_NO_MESSAGES", ctx.exception.code)

    def test_object_without_message_list_fails_closed(self) -> None:
        with self.assertRaises(runner.NormalizeError) as ctx:
            runner.normalize({"ok": True})
        self.assertEqual("SLACK_NO_MESSAGES", ctx.exception.code)

    def test_message_missing_text_fails_closed(self) -> None:
        with self.assertRaises(runner.NormalizeError) as ctx:
            runner.normalize([{"ts": "1700000000.0", "channel": "c", "text": "  "}])
        self.assertEqual("SLACK_MESSAGE_MALFORMED", ctx.exception.code)

    def test_message_missing_ts_fails_closed(self) -> None:
        with self.assertRaises(runner.NormalizeError) as ctx:
            runner.normalize([{"channel": "c", "text": "hi"}])
        self.assertEqual("SLACK_MESSAGE_MALFORMED", ctx.exception.code)

    def test_cli_tolerates_leading_adapter_token(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(RUNNER_PATH), "slack"],
            input="not json",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("SLACK_SOURCE_NOT_JSON", payload["error"])


class RunnerNormalization(unittest.TestCase):
    def test_selfcheck_passes(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(RUNNER_PATH), "--selfcheck"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["marker_found"])

    def test_normalizes_sorts_and_hashes(self) -> None:
        result = runner.normalize(runner._SELFCHECK_MESSAGES)
        self.assertTrue(result["ok"])
        self.assertEqual(2, result["message_count"])
        self.assertEqual(["ai-news", "sf-platform"], result["channels"])
        self.assertEqual("2023-11-14", result["date_range"]["earliest"])
        self.assertEqual(64, len(result["source_sha256"]))
        self.assertIn("#ai-news", result["markdown"])

    def test_rendering_and_hash_are_deterministic(self) -> None:
        first = runner.normalize(runner._SELFCHECK_MESSAGES)
        # Reversed input order must not change the content-addressed output.
        second = runner.normalize(list(reversed(runner._SELFCHECK_MESSAGES)))
        self.assertEqual(first["markdown"], second["markdown"])
        self.assertEqual(first["source_sha256"], second["source_sha256"])

    def test_derives_utc_date_from_ts(self) -> None:
        result = runner.normalize(
            [{"ts": "1700000000.000200", "channel": "c", "user": "u", "text": "x"}]
        )
        self.assertEqual("2023-11-14", result["date_range"]["earliest"])

    def test_accepts_matches_wrapper_shape(self) -> None:
        document = {"matches": [{"ts": "1700000000.0", "channel": "c", "text": "x"}]}
        result = runner.normalize(document)
        self.assertEqual(1, result["message_count"])


if __name__ == "__main__":
    unittest.main()
