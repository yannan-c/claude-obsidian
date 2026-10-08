#!/usr/bin/env python3
"""Local PDF text-extraction runner for the ``pdf-extract`` capability.

This runner is deliberately kept OUT of the ``claude_obsidian`` package so the
portable core stays zero-dependency and stdlib-only. It is the "separately
configured runner" that the ``pdf`` capture adapter
(``maturity: external-runner-required``) plans for. It performs no network
access: PDF text extraction is a purely local, offline operation.

Invocation honours the external-runner argv contract used by
``claude_obsidian.capture.plan_external_action`` --- an optional leading adapter
token followed by ``--source``::

    python3 scripts/runners/pdf_extract.py pdf --source /path/to/file.pdf
    python3 scripts/runners/pdf_extract.py --source /path/to/file.pdf

It also supports a self-check that proves the extractor actually works::

    python3 scripts/runners/pdf_extract.py --selfcheck

Output is a single JSON object on stdout. On any failure the process exits
non-zero with ``{"ok": false, "error": ...}`` and never pretends content was
read. The third-party ``pypdf`` library is required; when it is absent the
runner fails closed with ``PDF_EXTRACTOR_UNAVAILABLE`` so callers can preserve
the locator and report the unsupported extraction honestly.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

RUNNER_NAME = "claude-obsidian-pdf-extract"
RUNNER_VERSION = "1"
SCHEMA = "claude-obsidian.pdf-extract.v1"
ADAPTER_ID = "pdf"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # bound extracted UTF-8 text

# A minimal single-page PDF whose content stream draws the literal text
# "claude-obsidian", used by --selfcheck to exercise real extraction behaviour
# rather than merely importing the library. It is built at runtime with a
# correct cross-reference table and ``startxref`` so pypdf parses it strictly,
# instead of relying on fragile hand-computed byte offsets.
_SELFCHECK_MARKER = "claude-obsidian"


def _build_selfcheck_pdf() -> bytes:
    bodies = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        None,  # content stream, filled below
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    stream = b"BT /F1 24 Tf 20 100 Td (claude-obsidian) Tj ET"
    bodies[3] = b"<</Length %d>>stream\n%s\nendstream" % (len(stream), stream)

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj" % index + body + b"endobj\n"  # type: ignore[operator]
    xref_pos = len(out)
    count = len(bodies) + 1
    out += b"xref\n0 %d\n" % count
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (
        count,
        xref_pos,
    )
    return bytes(out)


class ExtractorError(Exception):
    """A fail-closed extraction error carrying a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")


def _load_reader_cls() -> Any:
    try:
        from pypdf import PdfReader  # type: ignore import-not-found
    except Exception as exc:  # noqa: BLE001 - any broken install must fail closed
        raise ExtractorError(
            "PDF_EXTRACTOR_UNAVAILABLE",
            "the pypdf library is not importable; install it to enable local "
            f"PDF text extraction ({exc})",
        ) from exc
    return PdfReader


def _library_version() -> str:
    try:
        import pypdf  # type: ignore import-not-found

        return str(getattr(pypdf, "__version__", "unknown"))
    except Exception:  # noqa: BLE001
        return "unknown"


def _extract_pages(reader: Any) -> list[str]:
    if getattr(reader, "is_encrypted", False):
        # Only an empty-password decrypt is attempted; a real password is a
        # separate, explicit decision and never guessed here.
        try:
            if reader.decrypt("") == 0:  # 0 == failed
                raise ExtractorError(
                    "PDF_ENCRYPTED",
                    "the PDF is encrypted and cannot be opened without a password",
                )
        except ExtractorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ExtractorError(
                "PDF_ENCRYPTED",
                f"the PDF is encrypted and could not be opened ({exc})",
            ) from exc
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001 - a single bad page must not lie
            raise ExtractorError(
                "PDF_PAGE_EXTRACTION_FAILED",
                f"failed to extract text from a page ({exc})",
            ) from exc
    return pages


def _build_result(
    *, source: str, data: bytes, pages: list[str], max_bytes: int
) -> dict[str, Any]:
    page_objects: list[dict[str, Any]] = []
    accumulated = 0
    truncated = False
    for index, text in enumerate(pages):
        if truncated:
            page_objects.append({"index": index, "chars": 0, "text": "", "omitted": True})
            continue
        encoded = text.encode("utf-8")
        if accumulated + len(encoded) > max_bytes:
            budget = max_bytes - accumulated
            # Trim on a UTF-8 boundary.
            clipped = encoded[: max(budget, 0)].decode("utf-8", "ignore")
            page_objects.append(
                {"index": index, "chars": len(clipped), "text": clipped, "truncated": True}
            )
            accumulated += len(clipped.encode("utf-8"))
            truncated = True
            continue
        accumulated += len(encoded)
        page_objects.append({"index": index, "chars": len(text), "text": text})
    full_text = "\f".join(
        obj["text"] for obj in page_objects if not obj.get("omitted")
    )
    return {
        "schema": SCHEMA,
        "ok": True,
        "adapter": ADAPTER_ID,
        "source": source,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "source_bytes": len(data),
        "extractor": {
            "name": RUNNER_NAME,
            "version": RUNNER_VERSION,
            "library": "pypdf",
            "library_version": _library_version(),
        },
        "page_count": len(pages),
        "pages": page_objects,
        "char_count": sum(len(obj["text"]) for obj in page_objects),
        "max_bytes": max_bytes,
        "truncated": truncated,
    }


def extract(path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Extract bounded per-page text from ``path``. Raises ExtractorError."""

    if path.is_symlink():
        raise ExtractorError("PDF_SOURCE_SYMLINK", "symlink sources are forbidden")
    if not path.is_file():
        raise ExtractorError("PDF_SOURCE_MISSING", f"not a readable file: {path}")
    data = path.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ExtractorError(
            "PDF_SOURCE_NOT_PDF", "source does not begin with a %PDF- header"
        )
    reader_cls = _load_reader_cls()
    try:
        reader = reader_cls(io.BytesIO(data))
    except ExtractorError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExtractorError("PDF_PARSE_FAILED", f"could not parse the PDF ({exc})") from exc
    pages = _extract_pages(reader)
    return _build_result(source=str(path), data=data, pages=pages, max_bytes=max_bytes)


def _selfcheck() -> int:
    try:
        reader_cls = _load_reader_cls()
        reader = reader_cls(io.BytesIO(_build_selfcheck_pdf()))
        text = "".join(_extract_pages(reader))
    except ExtractorError as exc:
        _emit({"schema": SCHEMA, "ok": False, "error": exc.code, "message": exc.message})
        return 1
    ok = _SELFCHECK_MARKER in text
    _emit(
        {
            "schema": SCHEMA,
            "ok": ok,
            "selfcheck": True,
            "library": "pypdf",
            "library_version": _library_version(),
            "marker_found": ok,
            **({} if ok else {"error": "PDF_SELFCHECK_NO_TEXT",
                              "message": "pypdf imported but did not extract the expected marker text"}),
        }
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Tolerate an optional leading adapter token from the runner argv contract
    # (`<runner...> pdf --source <path>`).
    if raw and raw[0] == ADAPTER_ID:
        raw = raw[1:]
    parser = argparse.ArgumentParser(
        prog="pdf_extract.py",
        description="Local, offline PDF text extraction for claude-obsidian.",
    )
    parser.add_argument("--source", help="path to the local .pdf file to extract")
    parser.add_argument(
        "--format", choices=("json", "text"), default="json", help="output format"
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"maximum extracted UTF-8 text bytes (default {DEFAULT_MAX_BYTES})",
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="verify the extractor works against an embedded sample PDF",
    )
    args = parser.parse_args(raw)

    if args.selfcheck:
        return _selfcheck()
    if not args.source:
        parser.error("--source is required unless --selfcheck is given")
    if args.max_bytes <= 0:
        parser.error("--max-bytes must be positive")

    try:
        result = extract(Path(args.source), max_bytes=args.max_bytes)
    except ExtractorError as exc:
        _emit({"schema": SCHEMA, "ok": False, "error": exc.code, "message": exc.message})
        return 1

    if args.format == "text":
        sys.stdout.write("\f".join(obj["text"] for obj in result["pages"]))
        sys.stdout.write("\n")
    else:
        _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
