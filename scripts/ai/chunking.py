"""Split a large PR diff into review batches instead of truncating it.

Pipeline: diff -> per-file diffs -> oversized files split by hunk -> batches
packed to a character budget. Whatever cannot be covered is reported explicitly;
nothing is dropped silently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_FILE_SPLIT = re.compile(r"^(?=diff --git )", re.MULTILINE)
_HUNK_SPLIT = re.compile(r"^(?=@@ )", re.MULTILINE)
_PATH = re.compile(r"\Adiff --git a/(?P<old>.+?) b/(?P<new>.+?)\s*$", re.MULTILINE)

#: Characters of diff per AI request. Well inside a 4k-token completion budget
#: while leaving room for the context blob and the system prompt.
DEFAULT_BATCH_CHARS = 30000
#: Files per batch, so one request never spans an unreviewable amount of context.
DEFAULT_BATCH_FILES = 12
#: Hard ceiling on requests per review, to bound cost and wall-clock.
DEFAULT_MAX_BATCHES = 12


@dataclass
class Piece:
    """One reviewable unit: a whole file diff, or one slice of a large file."""

    path: str
    text: str
    part: int = 1
    parts: int = 1

    @property
    def label(self) -> str:
        return self.path if self.parts == 1 else f"{self.path} (part {self.part}/{self.parts})"


@dataclass
class Batch:
    pieces: list[Piece] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(piece.text for piece in self.pieces)

    @property
    def paths(self) -> list[str]:
        seen: list[str] = []
        for piece in self.pieces:
            if piece.path not in seen:
                seen.append(piece.path)
        return seen

    @property
    def size(self) -> int:
        return sum(len(piece.text) for piece in self.pieces)


@dataclass
class Plan:
    """The batching outcome, including what could not be covered."""

    batches: list[Batch] = field(default_factory=list)
    covered_paths: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def complete(self) -> bool:
        return not self.skipped_paths


def split_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified diff into ``(path, text)`` per file, order preserved."""
    text = diff or ""
    if not text.strip():
        return []
    sections = [section for section in _FILE_SPLIT.split(text) if section.strip()]
    if len(sections) == 1 and not sections[0].lstrip().startswith("diff --git"):
        # A diff without git headers; treat it as one anonymous unit.
        return [("", sections[0])]
    result: list[tuple[str, str]] = []
    for section in sections:
        match = _PATH.search(section)
        path = (match.group("new").strip() if match else "").strip()
        result.append((path, section))
    return result


def split_by_hunk(path: str, text: str, budget: int) -> list[Piece]:
    """Split one file's diff into pieces that each fit inside ``budget``."""
    if len(text) <= budget:
        return [Piece(path=path, text=text)]

    parts = _HUNK_SPLIT.split(text)
    header = parts[0] if parts and not parts[0].lstrip().startswith("@@") else ""
    hunks = [part for part in parts[1:] if part.strip()] if header else [
        part for part in parts if part.strip()
    ]
    if not hunks:
        # One enormous hunk; slice it on line boundaries rather than mid-line.
        return _slice_lines(path, text, budget)

    pieces: list[Piece] = []
    current = header
    for hunk in hunks:
        if len(hunk) + len(header) > budget:
            if current.strip() and current != header:
                pieces.append(Piece(path=path, text=current))
                current = header
            pieces.extend(_slice_lines(path, header + hunk, budget))
            continue
        if len(current) + len(hunk) > budget and current.strip() and current != header:
            pieces.append(Piece(path=path, text=current))
            current = header
        current += hunk
    if current.strip() and current != header:
        pieces.append(Piece(path=path, text=current))

    total = len(pieces)
    for index, piece in enumerate(pieces, start=1):
        piece.part, piece.parts = index, total
    return pieces


def _slice_lines(path: str, text: str, budget: int) -> list[Piece]:
    """Last resort: cut on line boundaries so no diff line is ever halved."""
    pieces: list[Piece] = []
    current: list[str] = []
    length = 0
    for line in text.splitlines(keepends=True):
        if length + len(line) > budget and current:
            pieces.append(Piece(path=path, text="".join(current)))
            current, length = [], 0
        current.append(line)
        length += len(line)
    if current:
        pieces.append(Piece(path=path, text="".join(current)))
    total = len(pieces)
    for index, piece in enumerate(pieces, start=1):
        piece.part, piece.parts = index, total
    return pieces


def plan_batches(
    diff: str,
    *,
    batch_chars: int = DEFAULT_BATCH_CHARS,
    batch_files: int = DEFAULT_BATCH_FILES,
    max_batches: int = DEFAULT_MAX_BATCHES,
) -> Plan:
    """Pack a diff into review batches, reporting anything left uncovered."""
    plan = Plan()
    files = split_by_file(diff)
    if not files:
        return plan

    pieces: list[Piece] = []
    for path, text in files:
        pieces.extend(split_by_hunk(path, text, batch_chars))

    current = Batch()
    for piece in pieces:
        would_overflow = current.pieces and (
            current.size + len(piece.text) > batch_chars
            or len(current.paths) >= batch_files
        )
        if would_overflow:
            plan.batches.append(current)
            current = Batch()
        current.pieces.append(piece)
    if current.pieces:
        plan.batches.append(current)

    if len(plan.batches) > max_batches:
        dropped = plan.batches[max_batches:]
        plan.batches = plan.batches[:max_batches]
        kept = {path for batch in plan.batches for path in batch.paths}
        plan.skipped_paths = sorted(
            {path for batch in dropped for path in batch.paths if path and path not in kept}
        )
        plan.skipped_reason = (
            f"Diff needed {len(dropped) + max_batches} review batches but the cap is "
            f"{max_batches}. {len(plan.skipped_paths)} file(s) were not reviewed."
        )

    plan.covered_paths = sorted({p for batch in plan.batches for p in batch.paths if p})
    return plan


def merge_findings(groups: list[list[dict]]) -> list[dict]:
    """Merge per-batch findings, de-duplicated on (path, line, lowercased title)."""
    seen: set[tuple[str, object, str]] = set()
    merged: list[dict] = []
    for group in groups:
        for finding in group or []:
            key = (
                str(finding.get("path") or ""),
                finding.get("line"),
                str(finding.get("title") or "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(finding)
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    merged.sort(key=lambda f: (severity_order.get(str(f.get("severity")), 9),
                               str(f.get("path") or ""), f.get("line") or 0))
    return merged
