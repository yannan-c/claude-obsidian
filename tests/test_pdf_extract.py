#!/usr/bin/env python3
"""Behavioral tests for the local pdf-extract runner.

The runner lives outside the stdlib-only core and depends on the optional
third-party ``pypdf`` library, so extraction tests skip when it is unavailable.
Error-path and argv-contract tests run regardless.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER_PATH = ROOT / "scripts" / "runners" / "pdf_extract.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("co_pdf_extract_runner", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pypdf_available() -> bool:
    try:
        import pypdf  # type: ignore  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


runner = _load_runner()


class RunnerErrorPaths(unittest.TestCase):
    """These exercise fail-closed behaviour before pypdf is ever loaded."""

    def test_runner_script_ships(self) -> None:
        self.assertTrue(RUNNER_PATH.is_file())

    def test_non_pdf_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "note.txt"
            path.write_bytes(b"not a pdf")
            with self.assertRaises(runner.ExtractorError) as ctx:
                runner.extract(path)
            self.assertEqual("PDF_SOURCE_NOT_PDF", ctx.exception.code)

    def test_missing_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(runner.ExtractorError) as ctx:
                runner.extract(Path(td) / "absent.pdf")
            self.assertEqual("PDF_SOURCE_MISSING", ctx.exception.code)

    def test_symlink_source_is_forbidden(self) -> None:
        if os.name == "nt":
            self.skipTest("symlink creation is unreliable on native Windows")
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "real.pdf"
            target.write_bytes(b"%PDF-1.4\n")
            link = Path(td) / "link.pdf"
            link.symlink_to(target)
            with self.assertRaises(runner.ExtractorError) as ctx:
                runner.extract(link)
            self.assertEqual("PDF_SOURCE_SYMLINK", ctx.exception.code)

    def test_cli_tolerates_leading_adapter_token(self) -> None:
        # `pdf` prefix (from the external-runner argv contract) plus a bogus
        # source still parses to a structured, non-crashing JSON error.
        with tempfile.TemporaryDirectory() as td:
            bogus = Path(td) / "x.pdf"
            bogus.write_bytes(b"not a pdf")
            completed = subprocess.run(
                [sys.executable, str(RUNNER_PATH), "pdf", "--source", str(bogus)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn("error", payload)


class RunnerExtraction(unittest.TestCase):
    """These require the optional pypdf library and skip otherwise."""

    def setUp(self) -> None:
        if not _pypdf_available():
            self.skipTest("pypdf is not installed; extraction behaviour not exercised")

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

    def test_extracts_marker_text_with_page_structure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sample.pdf"
            path.write_bytes(runner._build_selfcheck_pdf())
            result = runner.extract(path)
            self.assertTrue(result["ok"])
            self.assertEqual(1, result["page_count"])
            self.assertFalse(result["truncated"])
            self.assertIn(runner._SELFCHECK_MARKER, result["pages"][0]["text"])
            self.assertEqual(64, len(result["source_sha256"]))

    def test_bounded_output_marks_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sample.pdf"
            path.write_bytes(runner._build_selfcheck_pdf())
            result = runner.extract(path, max_bytes=4)
            self.assertTrue(result["truncated"])


class RunnerWithoutPypdf(unittest.TestCase):
    def test_reports_unavailable_when_pypdf_missing(self) -> None:
        if _pypdf_available():
            self.skipTest("pypdf is installed; the unavailable path cannot be observed")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sample.pdf"
            path.write_bytes(b"%PDF-1.4\n")
            with self.assertRaises(runner.ExtractorError) as ctx:
                runner.extract(path)
            self.assertEqual("PDF_EXTRACTOR_UNAVAILABLE", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
