"""Parse a unified diff into per-file added and removed lines.

:mod:`scripts.github.diffmap` answers "which right-side line numbers will GitHub
accept a comment on". This module answers a different question: "what text did
this pull request add, and what text did it remove". Several insights need the
removed side — an API endpoint that disappeared, a dependency that was dropped,
a migration file that was edited rather than added — which the comment-anchoring
map deliberately discards.

Nothing here interprets repository content beyond splitting lines, so a hostile
diff can at worst produce uninteresting findings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable, Iterator, Mapping

_DIFF_GIT = re.compile(r"\Adiff --git a/(?P<old>.+?) b/(?P<new>.+)\Z")
_MINUS_FILE = re.compile(r"\A--- (?:a/)?(?P<path>.+?)\s*\Z")
_PLUS_FILE = re.compile(r"\A\+\+\+ (?:b/)?(?P<path>.+?)\s*\Z")
_HUNK = re.compile(
    r"\A@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
_RENAME_FROM = re.compile(r"\Arename from (?P<path>.+)\Z")
_RENAME_TO = re.compile(r"\Arename to (?P<path>.+)\Z")

TRUNCATION_MARKER = "[diff truncated by MAX_DIFF_BYTES]"


@dataclass(frozen=True)
class Line:
    """One added or removed line, with the line number on its own side."""

    path: str
    line: int
    text: str

    @property
    def stripped(self) -> str:
        return self.text.strip()


@dataclass
class FileChange:
    """Everything one file contributed to the diff."""

    path: str
    old_path: str = ""
    added: list[Line] = field(default_factory=list)
    removed: list[Line] = field(default_factory=list)
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False

    @property
    def is_rename(self) -> bool:
        return bool(self.old_path) and self.old_path != self.path

    @property
    def is_modified(self) -> bool:
        """Existing file whose contents changed: not added, not deleted."""
        return not self.is_new and not self.is_deleted

    @property
    def added_text(self) -> str:
        return "\n".join(line.text for line in self.added)

    @property
    def removed_text(self) -> str:
        return "\n".join(line.text for line in self.removed)

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "old_path": self.old_path,
            "added": len(self.added),
            "removed": len(self.removed),
            "status": (
                "added" if self.is_new
                else "deleted" if self.is_deleted
                else "renamed" if self.is_rename
                else "modified"
            ),
        }


def parse_changes(diff: str) -> dict[str, FileChange]:
    """Build ``{path: FileChange}`` from a unified diff.

    A deleted file keeps its entry: "this endpoint was removed" is exactly the
    kind of finding the compatibility checks exist to report. Renames are keyed
    by the new path and remember the old one.
    """
    result: dict[str, FileChange] = {}
    current: FileChange | None = None
    old_line = new_line = 0
    pending_old = pending_new = ""
    rename_from = ""

    for raw in (diff or "").splitlines():
        header = _DIFF_GIT.match(raw)
        if header:
            pending_old = header.group("old").strip()
            pending_new = header.group("new").strip()
            current, old_line, new_line, rename_from = None, 0, 0, ""
            continue

        match = _RENAME_FROM.match(raw)
        if match:
            rename_from = match.group("path").strip()
            continue
        match = _RENAME_TO.match(raw)
        if match:
            # A pure rename carries no hunks, so materialise the entry here.
            path = match.group("path").strip()
            current = result.setdefault(path, FileChange(path=path))
            current.old_path = rename_from or pending_old
            continue

        if raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
            path = pending_new if pending_new != "/dev/null" else pending_old
            if path:
                entry = result.setdefault(path, FileChange(path=path))
                entry.is_binary = True
                current = entry
            continue

        if raw.startswith("--- "):
            match = _MINUS_FILE.match(raw)
            value = match.group("path").strip() if match else ""
            if value == "/dev/null":
                # No left side at all: the file is new.
                path = pending_new
                if path and path != "/dev/null":
                    entry = result.setdefault(path, FileChange(path=path))
                    entry.is_new = True
            elif value:
                pending_old = value
            continue

        if raw.startswith("+++ "):
            match = _PLUS_FILE.match(raw)
            value = match.group("path").strip() if match else ""
            if value == "/dev/null":
                # No right side: the file was deleted. Key it by its old path.
                path = pending_old
                if path and path != "/dev/null":
                    current = result.setdefault(path, FileChange(path=path))
                    current.is_deleted = True
                else:
                    current = None
            else:
                path = value or pending_new
                if path:
                    current = result.setdefault(path, FileChange(path=path))
                    if pending_old and pending_old not in ("/dev/null", path):
                        current.old_path = pending_old
                else:
                    current = None
            continue

        hunk = _HUNK.match(raw)
        if hunk:
            old_line = int(hunk.group("old_start"))
            new_line = int(hunk.group("new_start"))
            continue

        if current is None or not (old_line or new_line):
            continue

        if raw.startswith("+"):
            current.added.append(Line(current.path, new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            current.removed.append(Line(current.old_path or current.path, old_line, raw[1:]))
            old_line += 1
        elif raw.startswith("\\"):
            # "\ No newline at end of file" advances neither side.
            continue
        else:
            old_line += 1
            new_line += 1

    return result


def is_truncated(diff: str) -> bool:
    """True when the diff was cut short, so every insight is partial."""
    return TRUNCATION_MARKER in (diff or "")


def all_added(changes: Mapping[str, FileChange]) -> Iterator[Line]:
    """Every added line across every file, in file order."""
    for change in changes.values():
        yield from change.added


def all_removed(changes: Mapping[str, FileChange]) -> Iterator[Line]:
    """Every removed line across every file, in file order."""
    for change in changes.values():
        yield from change.removed


def file_stats(context: Mapping[str, object]) -> dict[str, dict[str, object]]:
    """Index ``file_stats`` from the PR context by path.

    GitHub's own per-file counts are authoritative for size policy: they cover
    files the diff may have truncated away.
    """
    stats: dict[str, dict[str, object]] = {}
    for entry in context.get("file_stats") or []:  # type: ignore[union-attr]
        if isinstance(entry, Mapping) and entry.get("path"):
            stats[str(entry["path"])] = dict(entry)
    return stats


def basename(path: str) -> str:
    return PurePosixPath(path).name


def extension(path: str) -> str:
    """Lower-case suffix including the dot, or '' when there is none."""
    return PurePosixPath(path).suffix.lower()


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    """Glob match against the shared glob implementation."""
    from ..common.globs import matches

    return any(matches(pattern, path) for pattern in patterns)
