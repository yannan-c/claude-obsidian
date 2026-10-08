#!/usr/bin/env python3
"""Deterministic normalizer for Slack messages fetched via a Slack MCP transport.

This runner is deliberately kept OUT of the ``claude_obsidian`` package so the
portable core stays zero-dependency and stdlib-only. It performs NO network
access: the Slack read happens at the agent layer through a consent-gated Slack
MCP transport, and the already-fetched messages are handed to this runner as a
local JSON document. The runner's only job is to turn that untrusted message
payload into a stable, content-addressed Markdown capture plus provenance
metadata, so a ``slack-skills`` or ``wiki-ingest`` capture can land it as a
create-only ``.raw/captured/<sha256>.md`` payload.

Invocation reads one JSON document from ``--source`` (a local file) or stdin::

    python3 scripts/runners/slack_normalize.py --source messages.json
    cat messages.json | python3 scripts/runners/slack_normalize.py

It also supports a self-check that proves normalization works end to end::

    python3 scripts/runners/slack_normalize.py --selfcheck

Output is a single JSON object on stdout. On any failure the process exits
non-zero with ``{"ok": false, "error": ...}`` and never fabricates messages,
dates, authors, or permalinks. Message text is untrusted data: it is captured
verbatim as evidence and is never interpreted as an instruction.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
from typing import Any

RUNNER_NAME = "claude-obsidian-slack-normalize"
RUNNER_VERSION = "1"
SCHEMA = "claude-obsidian.slack-normalize.v1"
ADAPTER_ID = "slack"

# A tiny embedded message set used by --selfcheck to exercise real
# normalization behaviour (date derivation, sorting, grouping, hashing) rather
# than merely importing the module.
_SELFCHECK_MARKER = "claude-obsidian"
_SELFCHECK_MESSAGES = [
    {
        "ts": "1700000000.000200",
        "channel": "sf-platform",
        "user": "ada",
        "permalink": "https://example.slack.com/archives/C1/p1700000000000200",
        "text": "Flow HTTP callouts are GA this release — claude-obsidian",
    },
    {
        "ts": "1699990000.000100",
        "channel": "ai-news",
        "user": "lin",
        "permalink": "https://example.slack.com/archives/C2/p1699990000000100",
        "text": "New Agentforce model dropped",
    },
]


class NormalizeError(Exception):
    """A fail-closed normalization error carrying a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")


def _coerce_messages(document: Any) -> list[dict[str, Any]]:
    """Accept the common Slack MCP shapes and return a flat message list."""

    if isinstance(document, list):
        candidate = document
    elif isinstance(document, dict):
        for key in ("messages", "matches", "items", "results"):
            value = document.get(key)
            if isinstance(value, list):
                candidate = value
                break
        else:
            raise NormalizeError(
                "SLACK_NO_MESSAGES",
                "no message list found under messages/matches/items/results",
            )
    else:
        raise NormalizeError(
            "SLACK_SOURCE_NOT_JSON", "the Slack payload must be a JSON array or object"
        )
    if not candidate:
        raise NormalizeError("SLACK_NO_MESSAGES", "the Slack payload contains no messages")
    if not all(isinstance(item, dict) for item in candidate):
        raise NormalizeError(
            "SLACK_MESSAGE_MALFORMED", "every Slack message must be a JSON object"
        )
    return candidate


def _iso_date(ts: str) -> str:
    seconds = float(ts.split(".", 1)[0])
    return (
        _dt.datetime.fromtimestamp(seconds, tz=_dt.timezone.utc)
        .date()
        .isoformat()
    )


def _normalize_one(raw: dict[str, Any]) -> dict[str, str]:
    ts_value = raw.get("ts") or raw.get("timestamp")
    text = raw.get("text")
    if text is None or not str(text).strip():
        raise NormalizeError("SLACK_MESSAGE_MALFORMED", "a message is missing text")
    if ts_value is None:
        raise NormalizeError(
            "SLACK_MESSAGE_MALFORMED", "a message is missing a ts/timestamp"
        )
    ts = str(ts_value)
    try:
        iso = _iso_date(ts)
    except (ValueError, OverflowError, OSError) as exc:
        raise NormalizeError(
            "SLACK_MESSAGE_MALFORMED", f"a message has an unparseable ts ({exc})"
        ) from exc
    channel = str(
        raw.get("channel") or raw.get("channel_name") or raw.get("channel_id") or "unknown"
    )
    author = str(raw.get("user") or raw.get("username") or raw.get("user_id") or "unknown")
    permalink = str(raw.get("permalink") or raw.get("link") or "")
    return {
        "ts": ts,
        "iso": iso,
        "channel": channel,
        "author": author,
        "permalink": permalink,
        "text": str(text),
    }


def _render_markdown(messages: list[dict[str, str]]) -> str:
    channels = sorted({m["channel"] for m in messages})
    isos = sorted({m["iso"] for m in messages})
    lines: list[str] = []
    lines.append("# Slack capture")
    lines.append("")
    lines.append(f"- source: slack-mcp")
    lines.append(f"- messages: {len(messages)}")
    lines.append(f"- date_range: {isos[0]} .. {isos[-1]}")
    lines.append(f"- channels: {', '.join(channels)}")
    lines.append("")
    for channel in channels:
        lines.append(f"## #{channel}")
        lines.append("")
        in_channel = sorted(
            (m for m in messages if m["channel"] == channel),
            key=lambda m: (m["ts"], m["author"]),
        )
        for message in in_channel:
            locator = message["permalink"] or "(no permalink)"
            lines.append(f"- **{message['iso']}** · {message['author']} · {locator}")
            for text_line in message["text"].splitlines() or [""]:
                lines.append(f"  > {text_line}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def normalize(document: Any) -> dict[str, Any]:
    """Normalize a Slack message document. Raises NormalizeError on bad input."""

    raw_messages = _coerce_messages(document)
    messages = [_normalize_one(item) for item in raw_messages]
    messages.sort(key=lambda m: (m["ts"], m["channel"], m["author"]))
    markdown = _render_markdown(messages)
    markdown_bytes = markdown.encode("utf-8")
    isos = sorted({m["iso"] for m in messages})
    channels = sorted({m["channel"] for m in messages})
    return {
        "schema": SCHEMA,
        "ok": True,
        "adapter": ADAPTER_ID,
        "normalizer": {"name": RUNNER_NAME, "version": RUNNER_VERSION},
        "message_count": len(messages),
        "date_range": {"earliest": isos[0], "latest": isos[-1]},
        "channels": channels,
        "source_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
        "char_count": len(markdown),
        "markdown": markdown,
    }


def _load_document(source: str | None) -> Any:
    if source:
        try:
            with open(source, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise NormalizeError("SLACK_SOURCE_MISSING", f"cannot read source ({exc})") from exc
    else:
        text = sys.stdin.read()
    if not text.strip():
        raise NormalizeError("SLACK_SOURCE_MISSING", "the Slack payload is empty")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise NormalizeError("SLACK_SOURCE_NOT_JSON", f"invalid JSON ({exc})") from exc


def _selfcheck() -> int:
    try:
        result = normalize(_SELFCHECK_MESSAGES)
    except NormalizeError as exc:
        _emit({"schema": SCHEMA, "ok": False, "error": exc.code, "message": exc.message})
        return 1
    ok = (
        _SELFCHECK_MARKER in result["markdown"]
        and result["message_count"] == 2
        and result["channels"] == ["ai-news", "sf-platform"]
        and len(result["source_sha256"]) == 64
    )
    _emit(
        {
            "schema": SCHEMA,
            "ok": ok,
            "selfcheck": True,
            "marker_found": _SELFCHECK_MARKER in result["markdown"],
            **(
                {}
                if ok
                else {
                    "error": "SLACK_SELFCHECK_FAILED",
                    "message": "normalization did not produce the expected sample output",
                }
            ),
        }
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == ADAPTER_ID:
        raw = raw[1:]
    parser = argparse.ArgumentParser(
        prog="slack_normalize.py",
        description="Deterministic, offline normalization of fetched Slack messages.",
    )
    parser.add_argument(
        "--source", help="path to a local JSON file of Slack messages (default: stdin)"
    )
    parser.add_argument(
        "--format", choices=("json", "text"), default="json", help="output format"
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="verify normalization against an embedded sample payload",
    )
    args = parser.parse_args(raw)

    if args.selfcheck:
        return _selfcheck()

    try:
        document = _load_document(args.source)
        result = normalize(document)
    except NormalizeError as exc:
        _emit({"schema": SCHEMA, "ok": False, "error": exc.code, "message": exc.message})
        return 1

    if args.format == "text":
        sys.stdout.write(result["markdown"])
    else:
        _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
