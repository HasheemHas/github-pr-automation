"""Shared result shape for every deterministic insight.

Each insight returns the same envelope so the report job can render any of them
without special-casing:

    {"status": "pass|warn|fail|not_run", "summary": str,
     "findings": [{"severity", "rule", "title", "detail", "path", "line"}], ...}

``warn`` exists deliberately. A 900-line pull request, a widened dependency range
or a missing rollback note is worth telling a reviewer about, and is not grounds
for blocking a merge. Only a configured policy breach produces ``fail``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: Highest severity last, so a numeric comparison ranks them.
SEVERITY_ORDER: dict[str, int] = {
    "info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4,
}

PASS, WARN, FAIL, NOT_RUN = "pass", "warn", "fail", "not_run"


@dataclass(frozen=True)
class Finding:
    """One reproducible observation about the pull request."""

    severity: str
    rule: str
    title: str
    detail: str = ""
    path: str = ""
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "severity": self.severity,
            "rule": self.rule,
            "title": self.title,
            "detail": self.detail,
        }
        if self.path:
            payload["path"] = self.path
        if self.line is not None:
            payload["line"] = self.line
        return payload


def rank(severity: str) -> int:
    return SEVERITY_ORDER.get(str(severity).lower(), 0)


def worst(findings: Iterable[Finding]) -> str:
    """The highest severity present, or 'info' when there is nothing."""
    items = list(findings)
    if not items:
        return "info"
    return max((f.severity for f in items), key=rank)


def verdict(findings: Sequence[Finding], *, fail_at: str = "high", warn_at: str = "low") -> str:
    """Collapse findings into a status using two severity thresholds.

    ``fail_at`` of an unknown or empty value disables failing entirely, which is
    how a policy configured as advisory-only stays advisory.
    """
    if not findings:
        return PASS
    top = rank(worst(findings))
    if fail_at and fail_at in SEVERITY_ORDER and top >= rank(fail_at):
        return FAIL
    if warn_at and warn_at in SEVERITY_ORDER and top >= rank(warn_at):
        return WARN
    return PASS


def result(
    status: str,
    summary: str,
    findings: Sequence[Finding] = (),
    **extra: Any,
) -> dict[str, Any]:
    """Build the insight envelope, sorted most-severe first."""
    ordered = sorted(findings, key=lambda f: (-rank(f.severity), f.path, f.rule))
    payload: dict[str, Any] = {
        "status": status,
        "summary": summary,
        "findings": [f.to_dict() for f in ordered],
        "counts": {
            name: sum(1 for f in ordered if f.severity == name) for name in SEVERITY_ORDER
        },
    }
    payload.update(extra)
    return payload


def not_run(reason: str, **extra: Any) -> dict[str, Any]:
    """An insight that had nothing to work with. Distinct from a clean pass."""
    return result(NOT_RUN, reason, (), **extra)


def block(config: Mapping[str, Any] | None, name: str) -> dict[str, Any]:
    """Read one nested config mapping, tolerating a missing or malformed block."""
    value = (config or {}).get(name)
    return dict(value) if isinstance(value, Mapping) else {}


def as_int(value: Any, default: int) -> int:
    """Coerce a config value to a non-negative int, falling back on nonsense."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
    return default


def as_patterns(value: Any) -> list[str]:
    """A list of glob strings from config, dropping anything unusable."""
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]
