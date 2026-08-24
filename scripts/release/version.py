"""Semantic version selection for target repository releases.

Tag ordering is numeric on ``(major, minor, patch)``. The order the GitHub tags
API happens to return is never trusted.
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

from ..common.errors import AutomationError

TAG_PATTERN = re.compile(r"\Av?(\d+)\.(\d+)\.(\d+)\Z")

BUMP_LABELS = {
    "major": ("breaking-change", "breaking"),
    "minor": ("feature", "feat"),
}


def parse_tag(name: str) -> tuple[int, int, int] | None:
    """Parse ``v1.2.3`` or ``1.2.3`` into a comparable tuple, else None."""
    match = TAG_PATTERN.match((name or "").strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def sorted_versions(names: Iterable[str]) -> list[tuple[int, int, int]]:
    """Every parseable tag, numerically ascending, de-duplicated."""
    return sorted({parsed for name in names if (parsed := parse_tag(name)) is not None})


def latest_version(names: Iterable[str]) -> tuple[int, int, int]:
    """The highest semantic version, or ``(0, 0, 0)`` when none exist."""
    versions = sorted_versions(names)
    return versions[-1] if versions else (0, 0, 0)


def bump(current: tuple[int, int, int], labels: Sequence[str]) -> tuple[int, int, int]:
    """Apply the label-driven bump rule to a version tuple."""
    normalised = {str(label).strip().lower() for label in labels}
    major, minor, patch = current
    if normalised & set(BUMP_LABELS["major"]):
        return (major + 1, 0, 0)
    if normalised & set(BUMP_LABELS["minor"]):
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def next_version(tags: Iterable[str], labels: Sequence[str] = (), explicit: str = "") -> str:
    """Resolve the next version string.

    Raises:
        AutomationError: an explicit version was supplied but is not semver, or is
            not greater than the newest existing tag.
    """
    candidates = list(tags)
    current = latest_version(candidates)
    if explicit and explicit.strip():
        parsed = parse_tag(explicit)
        if parsed is None:
            raise AutomationError(
                f"Version {explicit!r} must use semantic versioning: MAJOR.MINOR.PATCH."
            )
        if parsed <= current:
            raise AutomationError(
                f"Version {format_version(parsed)} is not greater than the existing "
                f"latest tag {format_version(current)}."
            )
        return format_version(parsed)
    return format_version(bump(current, labels))
