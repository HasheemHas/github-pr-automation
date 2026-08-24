"""Derive PR labels from changed paths, PR text and AI metadata."""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from .globs import any_match

#: label -> path patterns. Declared centrally so every target repo behaves alike.
PATH_LABELS: dict[str, tuple[str, ...]] = {
    "backend": ("backend/**",),
    "frontend": ("frontend/**", "admin/**"),
    "terraform": ("**/*.tf", "terraform/**"),
    "helm": ("helm/**", "**/Chart.yaml", "**/values.yaml"),
    "ci": (".github/workflows/**",),
    "docker": ("Dockerfile", "**/Dockerfile", "docker-compose.*", "**/docker-compose.*"),
    "tests": (
        "**/*.test.*",
        "**/*.spec.*",
        "**/*_test.py",
        "**/test_*.py",
        "tests/**",
        "test/**",
        "__tests__/**",
    ),
    "documentation": ("**/*.md", "docs/**"),
    "dependencies": (
        "**/package.json",
        "**/package-lock.json",
        "**/requirements*.txt",
        "**/pyproject.toml",
        "**/poetry.lock",
    ),
    "security": ("**/auth/**", "security/**"),
}

#: Labels implied by another label.
IMPLIED_LABELS: dict[str, tuple[str, ...]] = {
    "terraform": ("devops",),
    "helm": ("kubernetes", "devops"),
}

TITLE_LABELS: tuple[tuple[str, str], ...] = (
    (r"\bbreaking[ -]change\b", "breaking-change"),
    (r"\b(fix|bugfix|bug)\b", "fix"),
    (r"\b(feat|feature)\b", "feature"),
    (r"\brefactor\b", "refactor"),
)

RISK_LABELS = ("low-risk", "medium-risk", "high-risk")

#: Paths that force high risk when the AI supplied no risk assessment.
HIGH_RISK_PATHS = (".github/workflows/**", "terraform/**", "**/auth/**", "security/**")

MEDIUM_RISK_FILE_COUNT = 25


def derive_labels(
    paths: Sequence[str],
    *,
    title: str = "",
    body: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return the sorted label set for a pull request.

    ``metadata`` is trusted only for ``change_type`` and ``risk``, and only when
    those values are in the allowed enum. A missing or failed AI analysis simply
    falls back to path-based heuristics.
    """
    labels: set[str] = set()

    for label, patterns in PATH_LABELS.items():
        if any_match(patterns, paths):
            labels.add(label)
            labels.update(IMPLIED_LABELS.get(label, ()))

    text = f"{title} {body}".lower()
    for pattern, label in TITLE_LABELS:
        if re.search(pattern, text):
            labels.add(label)

    meta = metadata or {}
    change_type = str(meta.get("change_type") or "").strip().lower()
    if change_type in {"feature", "fix", "refactor", "documentation", "chore", "security"}:
        labels.add(change_type)
    risk = str(meta.get("risk") or "").strip().lower()
    if risk in {"low", "medium", "high"}:
        labels.add(f"{risk}-risk")
    if meta.get("breaking_change") is True:
        labels.add("breaking-change")

    if not labels & set(RISK_LABELS):
        if any_match(HIGH_RISK_PATHS, paths):
            labels.add("high-risk")
        elif len(paths) > MEDIUM_RISK_FILE_COUNT:
            labels.add("medium-risk")
        else:
            labels.add("low-risk")

    return sorted(labels)


def missing_labels(wanted: Iterable[str], existing: Iterable[str]) -> list[str]:
    """Labels that must be created before they can be applied."""
    have = {name.lower() for name in existing}
    return [label for label in wanted if label.lower() not in have]
