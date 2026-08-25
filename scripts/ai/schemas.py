"""Explicit schema validation for AI output.

Malformed or out-of-enum output raises :class:`SchemaError`, which the callers
convert into "not run" rather than writing anything to the target PR.
"""
from __future__ import annotations

from typing import Any, Mapping

from ..common.errors import SchemaError

CHANGE_TYPES = ("feature", "fix", "refactor", "documentation", "chore", "security", "test")
RISKS = ("low", "medium", "high")
SEVERITIES = ("critical", "high", "medium", "low", "info")
CATEGORIES = (
    "correctness",
    "security",
    "performance",
    "maintainability",
    "testing",
    "style",
    "other",
)

MAX_SUMMARY = 4000
MAX_DESCRIPTION = 8000
MAX_TITLE = 250
MAX_FINDINGS = 50
MAX_GAPS = 50
MAX_FIELD = 2000
MAX_PATCH_BYTES = 200_000


def _text(data: Mapping[str, Any], key: str, *, limit: int, required: bool = False,
          default: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        if required:
            raise SchemaError(f"Field '{key}' must be a string.")
        return default
    text = value.strip()
    if required and not text:
        raise SchemaError(f"Field '{key}' is required and must not be empty.")
    return text[:limit]


def _enum(data: Mapping[str, Any], key: str, allowed: tuple[str, ...], *,
          required: bool = False, default: str = "") -> str:
    raw = data.get(key)
    if raw is None or not isinstance(raw, str) or not raw.strip():
        if required:
            raise SchemaError(f"Field '{key}' is required.")
        return default
    value = raw.strip().lower()
    if value not in allowed:
        if required:
            raise SchemaError(f"Field '{key}' must be one of: {', '.join(allowed)}.")
        return default
    return value


def _string_list(data: Mapping[str, Any], key: str, *, limit: int) -> list[str]:
    raw = data.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise SchemaError(f"Field '{key}' must be a list of strings.")
    out: list[str] = []
    for item in raw[:limit]:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:MAX_FIELD])
    return out


def validate_metadata(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate PR metadata. ``summary`` is mandatory: no summary, no write.

    Every other field is optional; a section with no content is simply omitted
    from the rendered PR description rather than padded with placeholder text.
    """
    if not isinstance(data, Mapping):
        raise SchemaError("Metadata output must be a JSON object.")
    breaking = data.get("breaking_change")
    if isinstance(breaking, str):
        breaking = breaking.strip().lower() in {"true", "yes"}
    return {
        "title": _text(data, "title", limit=MAX_TITLE),
        "summary": _text(data, "summary", limit=MAX_SUMMARY, required=True),
        # A longer prose description; falls back to the summary when absent.
        "description": _text(data, "description", limit=MAX_DESCRIPTION),
        "change_type": _enum(data, "change_type", CHANGE_TYPES, default="chore"),
        "risk": _enum(data, "risk", RISKS, default="medium"),
        "risk_reason": _text(data, "risk_reason", limit=MAX_FIELD),
        "breaking_change": bool(breaking),
        "breaking_change_details": _text(data, "breaking_change_details", limit=MAX_FIELD),
        "components": _string_list(data, "components", limit=30),
        "changes": _string_list(data, "changes", limit=40),
        "release_notes": _string_list(data, "release_notes", limit=30),
        "recommended_testing": _string_list(data, "recommended_testing", limit=30),
        "review_focus": _string_list(data, "review_focus", limit=20),
        "migration_notes": _string_list(data, "migration_notes", limit=20),
        "rollback": _text(data, "rollback", limit=MAX_FIELD),
        "related_issues": _string_list(data, "related_issues", limit=20),
    }


def validate_review(data: Mapping[str, Any], *, changed_paths: set[str] | None = None
                    ) -> dict[str, Any]:
    """Validate review findings.

    A finding whose path is not in the PR's changed files is dropped: the model
    must not comment on code this PR did not touch.
    """
    if not isinstance(data, Mapping):
        raise SchemaError("Review output must be a JSON object.")
    raw_findings = data.get("findings")
    if raw_findings is None:
        raw_findings = []
    if not isinstance(raw_findings, list):
        raise SchemaError("Field 'findings' must be a list.")

    findings: list[dict[str, Any]] = []
    dropped = 0
    for item in raw_findings[:MAX_FINDINGS]:
        if not isinstance(item, Mapping):
            dropped += 1
            continue
        try:
            severity = _enum(item, "severity", SEVERITIES, required=True)
            title = _text(item, "title", limit=MAX_TITLE, required=True)
        except SchemaError:
            dropped += 1
            continue
        path = _text(item, "path", limit=MAX_FIELD)
        if changed_paths is not None and path and path not in changed_paths:
            dropped += 1
            continue
        line = item.get("line")
        if isinstance(line, str) and line.strip().isdigit():
            line = int(line.strip())
        if not isinstance(line, int) or line < 1:
            line = None
        findings.append(
            {
                "severity": severity,
                "category": _enum(item, "category", CATEGORIES, default="other"),
                "path": path,
                "line": line,
                "title": title,
                "body": _text(item, "body", limit=MAX_FIELD),
            }
        )
    return {"findings": findings, "dropped": dropped}


def validate_tests(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate test-gap analysis output."""
    if not isinstance(data, Mapping):
        raise SchemaError("Test analysis output must be a JSON object.")
    return {
        "changed": _string_list(data, "changed", limit=MAX_GAPS),
        "existing_tests": _string_list(data, "existing_tests", limit=MAX_GAPS),
        "gaps": _string_list(data, "gaps", limit=MAX_GAPS),
    }


def validate_patch(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a proposed patch. An empty patch is a valid "no change" answer."""
    if not isinstance(data, Mapping):
        raise SchemaError("Patch output must be a JSON object.")
    patch = data.get("patch")
    if patch is None:
        patch = ""
    if not isinstance(patch, str):
        raise SchemaError("Field 'patch' must be a string.")
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise SchemaError(f"Proposed patch exceeds {MAX_PATCH_BYTES} bytes.")
    if patch.strip() and not any(
        marker in patch for marker in ("--- ", "+++ ", "diff --git")
    ):
        raise SchemaError("Field 'patch' must be a unified diff.")
    return {
        "patch": patch,
        "explanation": _text(data, "explanation", limit=MAX_SUMMARY),
        "files": _string_list(data, "files", limit=50),
        "confident": bool(data.get("confident", False)),
    }
