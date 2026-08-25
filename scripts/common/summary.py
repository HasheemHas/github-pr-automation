"""Render GitHub Actions job summaries.

Every job writes a summary block to ``$GITHUB_STEP_SUMMARY`` so the run page
shows what happened without anyone opening the raw logs or the target PR.

Rules:
  * a missing ``GITHUB_STEP_SUMMARY`` is a no-op, so scripts stay runnable locally
  * nothing credential-shaped is ever written; callers pass rendered values only
  * a write failure is swallowed: a cosmetic summary must never fail a job
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Mapping, Sequence

#: GitHub truncates a job summary above 1 MiB; stay well clear.
MAX_BYTES = 900_000

ICONS = {
    "PASS": "✅",
    "FAIL": "❌",
    "PARTIAL": "⚠️",
    "ERROR": "🔥",
    "NOT RUN": "⚪",
    "pass": "✅",
    "fail": "❌",
    "skipped": "⏭️",
    "not_run": "⚪",
    "partial": "⚠️",
    "error": "🔥",
    "ok": "✅",
    "clean": "✅",
    "conflicted": "⚠️",
    "validated": "✅",
    "rejected": "❌",
    "refused": "🛑",
}


def icon(status: object) -> str:
    """An emoji for a status string, or a neutral dot when unknown."""
    return ICONS.get(str(status), "•")


def escape(value: object) -> str:
    """Make a value safe to drop into a markdown table cell."""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """Render a markdown table. Returns '' when there are no rows."""
    body = [f"| {' | '.join(escape(cell) for cell in row)} |" for row in rows]
    if not body:
        return ""
    head = f"| {' | '.join(escape(h) for h in headers)} |"
    rule = f"| {' | '.join('---' for _ in headers)} |"
    return "\n".join([head, rule, *body])


def definitions(pairs: Mapping[str, object] | Iterable[tuple[str, object]]) -> str:
    """Render a compact key/value list."""
    items = pairs.items() if isinstance(pairs, Mapping) else pairs
    return "\n".join(f"- **{escape(key)}:** {escape(value)}" for key, value in items)


def bullets(values: Iterable[object], *, limit: int = 20, empty: str = "_none_") -> str:
    """Render a bullet list, noting anything trimmed rather than hiding it."""
    items = [escape(value) for value in values]
    if not items:
        return empty
    shown = items[:limit]
    lines = [f"- {item}" for item in shown]
    if len(items) > limit:
        lines.append(f"- _…and {len(items) - limit} more_")
    return "\n".join(lines)


def details(title: str, body: str, *, open_by_default: bool = False) -> str:
    """Wrap a block in a collapsible section."""
    attr = " open" if open_by_default else ""
    return f"<details{attr}><summary>{escape(title)}</summary>\n\n{body}\n\n</details>"


def write(*blocks: str, env: Mapping[str, str] | None = None) -> bool:
    """Append blocks to the job summary. Returns True when something was written."""
    environ = os.environ if env is None else env
    path = environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return False
    content = "\n\n".join(block for block in blocks if block and block.strip())
    if not content.strip():
        return False
    if len(content.encode("utf-8")) > MAX_BYTES:
        content = content.encode("utf-8")[:MAX_BYTES].decode("utf-8", "ignore")
        content += "\n\n_Summary truncated._"
    try:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(content.rstrip() + "\n\n")
    except OSError:
        # A summary is a convenience; never fail a job over it.
        return False
    return True


def heading(text: str, level: int = 2) -> str:
    return f"{'#' * max(1, min(level, 6))} {text}"
