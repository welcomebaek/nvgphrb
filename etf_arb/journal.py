"""JSONL journal for the ETF NAV-disparity system.

One append-only file (logs/etf_arb_journal.jsonl), one JSON object per line.
`append(event_type, payload)` stamps the current local time automatically and
does a single write+flush per line, which is atomic enough for line-oriented
tailing/grepping (a single small write to an O_APPEND fd).

Secret discipline: callers should never put secrets in payloads, but as a
second line of defense any secret values registered via register_secrets()
are scrubbed from the serialized line before it is written.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from etf_arb import paths

DEFAULT_JOURNAL_PATH = paths.JOURNAL_PATH

_registered_secrets: list[str] = []


def register_secrets(secrets: list[str]) -> None:
    """Register secret values to be scrubbed from every journaled line."""
    for s in secrets:
        if s and s not in _registered_secrets:
            _registered_secrets.append(s)


def _scrub(line: str) -> str:
    for secret in _registered_secrets:
        if secret in line:
            line = line.replace(secret, "[REDACTED]")
    return line


def append(
    event_type: str, payload: dict[str, Any], path: Path | None = None
) -> None:
    """Append one event line: {"ts": ..., "event": event_type, ...payload}."""
    journal_path = path or DEFAULT_JOURNAL_PATH
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    record: dict[str, Any] = {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "event": event_type,
    }
    record.update(payload)

    line = _scrub(json.dumps(record, ensure_ascii=False, default=str))

    fd = os.open(
        journal_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644
    )
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)
