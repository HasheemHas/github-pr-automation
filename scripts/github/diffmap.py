"""Map a unified diff to the exact right-side line numbers GitHub will accept.

An inline review comment is only valid on a line that appears in the PR's diff.
The model's line integer is a guess; this module is the authority. A finding
whose line is not in the map is demoted to the summary comment rather than
anchored to a fabricated position.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

_DIFF_GIT = re.compile(r"\Adiff --git a/(?P<old>.+?) b/(?P<new>.+)\Z")
_PLUS_FILE = re.compile(r"\A\+\+\+ (?:b/)?(?P<path>.+?)\s*\Z")
_MINUS_FILE = re.compile(r"\A--- (?:a/)?(?P<path>.+?)\s*\Z")
_HUNK = re.compile(
    r"\A@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass
class FileLines:
    """Right-side line numbers for one file, split by kind."""

    added: set[int] = field(default_factory=set)
    context: set[int] = field(default_factory=set)

    @property
    def commentable(self) -> set[int]:
        """Every line GitHub will accept a RIGHT-side comment on."""
        return self.added | self.context


def parse_diff(diff: str) -> dict[str, FileLines]:
    """Build ``{path: FileLines}`` from a unified diff.

    Deleted files produce no entry: there is no right side to comment on.
    """
    result: dict[str, FileLines] = {}
    current: FileLines | None = None
    path: str | None = None
    new_line = 0
    pending_path: str | None = None

    for raw in (diff or "").splitlines():
        header = _DIFF_GIT.match(raw)
        if header:
            pending_path = header.group("new").strip()
            current, path, new_line = None, None, 0
            continue

        if raw.startswith("+++ "):
            match = _PLUS_FILE.match(raw)
            target = match.group("path").strip() if match else ""
            if target == "/dev/null":
                # File deleted: nothing on the right side.
                current, path = None, None
            else:
                path = target or pending_path
                if path:
                    current = result.setdefault(path, FileLines())
            continue

        if raw.startswith("--- "):
            # Only used to keep pending_path fresh for diffs without a git header.
            match = _MINUS_FILE.match(raw)
            if match and pending_path is None:
                pending_path = match.group("path").strip()
            continue

        hunk = _HUNK.match(raw)
        if hunk:
            new_line = int(hunk.group("new_start"))
            continue

        if current is None or not new_line:
            continue

        if raw.startswith("+"):
            current.added.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            # Left side only; the right-side counter does not advance.
            continue
        elif raw.startswith("\\"):
            # "\ No newline at end of file"
            continue
        else:
            # A context line (leading space, or an empty line in a loose diff).
            current.context.add(new_line)
            new_line += 1

    return result


def anchor(diff_map: dict[str, FileLines], path: str, line: object) -> int | None:
    """Return ``line`` when it is genuinely commentable, else ``None``.

    No snapping, no nearest-neighbour guessing: an inline comment on the wrong
    line is worse than the same finding in the summary.
    """
    entry = diff_map.get((path or "").strip())
    if entry is None:
        return None
    if isinstance(line, str) and line.strip().isdigit():
        line = int(line.strip())
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return None
    return line if line in entry.commentable else None


def added_line_count(diff_map: dict[str, FileLines]) -> int:
    return sum(len(entry.added) for entry in diff_map.values())


def split_anchored(
    diff_map: dict[str, FileLines], findings: Iterable[dict]
) -> tuple[list[dict], list[dict]]:
    """Split findings into inline-anchorable and summary-only.

    Every returned inline finding carries a verified ``line``; the reason a
    finding could not be anchored is recorded on it so the report can say so.
    """
    inline: list[dict] = []
    summary: list[dict] = []
    for finding in findings:
        path = (finding.get("path") or "").strip()
        verified = anchor(diff_map, path, finding.get("line"))
        if verified is None:
            reason = (
                "no path" if not path
                else "path not in this diff" if path not in diff_map
                else "line %s is not part of the diff" % finding.get("line")
            )
            summary.append({**finding, "unanchored_reason": reason})
        else:
            inline.append({**finding, "line": verified})
    return inline, summary
