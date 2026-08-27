"""Deterministic PR metadata, review, and test-gap analysis.

This module consumes the same sanitised PR artifacts as the former model-backed
stage and emits the same ``analysis.json`` contract.  It makes no network calls
and needs no API key.  Findings are deliberately limited to rules that can be
explained and reproduced from the unified diff.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from ..common import summary
from ..common.config import artifact_dir
from ..common.errors import AutomationError

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_ISSUE = re.compile(r"(?<![\w/])(?:[A-Z][A-Z0-9]+-\d+|#\d+)\b")
_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|secret|password|passwd|token|private[_-]?key)\s*[:=]\s*"
    r"['\"][^'\"]{8,}['\"]"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


@dataclass(frozen=True)
class AddedLine:
    path: str
    line: int
    text: str


def added_lines(diff: str) -> list[AddedLine]:
    """Return added right-side lines with their exact GitHub line numbers."""
    path = ""
    new_line: int | None = None
    result: list[AddedLine] = []
    for raw in (diff or "").splitlines():
        if raw.startswith("+++ "):
            value = raw[4:].strip()
            path = value[2:] if value.startswith("b/") else value
            if path == "/dev/null":
                path = ""
            continue
        match = _HUNK.match(raw)
        if match:
            new_line = int(match.group(1))
            continue
        if new_line is None or not path:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            result.append(AddedLine(path, new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        elif raw.startswith(" "):
            new_line += 1
    return result


def _change_type(context: Mapping[str, Any]) -> str:
    text = " ".join(
        [str(context.get("title") or ""), *[str(x) for x in context.get("commits") or []]]
    ).lower()
    paths = [str(p).lower() for p in context.get("files") or []]
    if any(word in text for word in ("security", "cve", "vulnerability")):
        return "security"
    if paths and all("test" in PurePosixPath(p).name.lower() or "/test" in p for p in paths):
        return "test"
    if paths and all(p.endswith((".md", ".rst", ".txt")) or p.startswith("docs/") for p in paths):
        return "documentation"
    if any(word in text for word in ("fix", "bug", "correct", "prevent", "repair")):
        return "fix"
    if any(word in text for word in ("refactor", "cleanup", "restructure")):
        return "refactor"
    if any(word in text for word in ("add", "feature", "implement", "support", "introduce")):
        return "feature"
    return "chore"


def _components(paths: Iterable[str]) -> list[str]:
    values: list[str] = []
    for path in paths:
        parts = PurePosixPath(path).parts
        value = parts[0] if len(parts) > 1 else "root"
        if value not in values:
            values.append(value)
    return values[:10]


def generate_metadata(context: Mapping[str, Any]) -> dict[str, Any]:
    paths = [str(p) for p in context.get("files") or []]
    components = _components(paths)
    kind = _change_type(context)
    commits = [str(c).strip() for c in context.get("commits") or [] if str(c).strip()]
    subject = commits[0] if commits else str(context.get("title") or "Update repository")
    subject = re.sub(r"^(?:feat|fix|chore|docs|test|refactor)(?:\([^)]*\))?!?:\s*", "", subject,
                     flags=re.IGNORECASE).strip().rstrip(".")
    component = components[0] if len(components) == 1 else "repository"
    prefix = {"feature": "feat", "documentation": "docs"}.get(kind, kind)
    title = f"{prefix}({component}): {subject[:180]}"
    files = len(paths)
    additions = int(context.get("additions") or 0)
    deletions = int(context.get("deletions") or 0)
    risk = "high" if any(
        p.startswith((".github/", "terraform/", "helm/", "migrations/"))
        or PurePosixPath(p).name.lower() in {"dockerfile", "docker-compose.yml"}
        for p in paths
    ) else "medium" if files > 10 or additions + deletions > 500 else "low"
    refs = sorted(set(_ISSUE.findall(" ".join([
        str(context.get("title") or ""), str(context.get("body") or ""), *commits
    ]))))
    component_text = ", ".join(components) if components else "the repository"
    return {
        "title": title,
        "summary": f"Updates {component_text} across {files} file(s) (+{additions}/-{deletions}).",
        "description": (
            f"This pull request updates {component_text}. The description is generated from "
            "changed paths, commit messages, and GitHub change statistics."
        ),
        "change_type": kind,
        "risk": risk,
        "risk_reason": (
            "Infrastructure, deployment, workflow, or migration files are affected."
            if risk == "high" else
            "The change is broad enough to require focused regression testing."
            if risk == "medium" else
            "The change is limited in file count and diff size."
        ),
        "breaking_change": False,
        "breaking_change_details": "",
        "components": components,
        "changes": [f"Changed `{p}`" for p in paths[:20]],
        "release_notes": [f"Changed {subject}."],
        "recommended_testing": [f"Run the configured checks for {c}." for c in components[:8]],
        "review_focus": [f"Review `{p}`." for p in paths[:10]],
        "migration_notes": [],
        "rollback": "Revert the pull request commit and redeploy the previous revision.",
        "related_issues": refs,
    }


def _finding(line: AddedLine, severity: str, category: str, title: str, body: str,
             rule: str) -> dict[str, Any]:
    return {"severity": severity, "category": category, "path": line.path,
            "line": line.line, "title": title, "body": body, "rule": rule}


def review_diff(context: Mapping[str, Any], diff: str, *, security_only: bool = False
                ) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for line in added_lines(diff):
        stripped = line.text.strip()
        candidates: list[dict[str, Any]] = []
        if _SECRET.search(line.text) or _PRIVATE_KEY.search(line.text):
            candidates.append(_finding(line, "critical", "security", "Possible committed secret",
                "A credential-shaped value was added. Remove it and rotate any exposed credential.",
                "secret-literal"))
        if re.search(r"\b(?:eval|exec)\s*\(", line.text):
            candidates.append(_finding(line, "high", "security", "Dynamic code execution",
                "Avoid eval/exec with data that can be influenced externally.", "dynamic-execution"))
        if re.search(r"\bsubprocess\.(?:run|Popen|call|check_output)\s*\(.*shell\s*=\s*True", line.text):
            candidates.append(_finding(line, "high", "security", "Shell command injection risk",
                "Pass an argument list and keep shell=False unless the input is fully constant.",
                "shell-true"))
        if re.search(r"(?i)verify\s*=\s*False|rejectUnauthorized\s*:\s*false", line.text):
            candidates.append(_finding(line, "high", "security", "TLS verification disabled",
                "Do not disable certificate verification in production code.", "tls-disabled"))
        if not security_only:
            if line.text.rstrip(" \t") != line.text:
                candidates.append(_finding(line, "low", "style", "Trailing whitespace",
                    "Remove trailing whitespace. This rule supports an automatic fix.",
                    "trailing-whitespace"))
            if re.match(r"^\s*except\s*:\s*(?:#.*)?$", line.text):
                candidates.append(_finding(line, "medium", "correctness", "Bare exception handler",
                    "Catch a specific exception so process-control and programming errors are not hidden.",
                    "bare-except"))
            if re.search(r"\b(?:print|console\.log)\s*\(", line.text) and not line.path.startswith("tests/"):
                candidates.append(_finding(line, "low", "maintainability", "Debug output added",
                    "Use the project's structured logger or remove debug output before merging.",
                    "debug-output"))
            if stripped and re.search(r"\b(?:TODO|FIXME|XXX)\b", stripped, re.IGNORECASE):
                candidates.append(_finding(line, "low", "maintainability", "Unresolved work marker",
                    "Track the remaining work explicitly or complete it before merging.", "work-marker"))
        for item in candidates:
            key = (item["path"], item["line"], item["rule"])
            if key not in seen:
                findings.append(item)
                seen.add(key)
    paths = [str(p) for p in context.get("files") or []]
    truncated = "[diff truncated by MAX_DIFF_BYTES]" in diff
    return {
        "findings": findings[:100], "dropped": max(0, len(findings) - 100),
        "batches": 1, "succeeded_batches": 1, "complete": not truncated,
        "reviewed_paths": paths if not truncated else sorted({line.path for line in added_lines(diff)}),
        "unreviewed_paths": [] if not truncated else sorted(set(paths) - {x.path for x in added_lines(diff)}),
        "note": "Deterministic rules; static analyzers run in the validation job.",
    }


def analyse_tests(context: Mapping[str, Any]) -> dict[str, Any]:
    changed = [str(p) for p in context.get("files") or []]
    existing = [str(p) for p in context.get("repository_tests") or []]
    changed_tests = [p for p in changed if "test" in PurePosixPath(p).name.lower()
                     or any(part in {"test", "tests", "__tests__", "spec"}
                            for part in PurePosixPath(p).parts)]
    source = [p for p in changed if p.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".go",
                                                ".rb", ".php", ".java", ".cs"))
              and p not in changed_tests]
    test_names = " ".join(PurePosixPath(p).stem.lower() for p in existing + changed_tests)
    gaps = []
    for path in source:
        stem = PurePosixPath(path).stem.lower()
        stem = stem.removeprefix("test_").removesuffix("_test").removesuffix(".spec")
        if stem and stem not in test_names:
            gaps.append(f"No matching test filename was found for `{path}`.")
    if source and not changed_tests:
        gaps.insert(0, "Production code changed without a test file changing in this pull request.")
    inventory = context.get("test_inventory") or {}
    if inventory.get("truncated"):
        gaps.append("The repository test inventory was truncated; filename matching is partial.")
    return {"changed": changed_tests, "existing_tests": existing[:300], "gaps": gaps[:50]}


def load_plan(env: Mapping[str, str]) -> dict[str, Any]:
    try:
        value = json.loads(env.get("PR_AUTOMATION_PLAN") or "{}")
    except json.JSONDecodeError as exc:
        raise AutomationError(f"PR_AUTOMATION_PLAN is invalid: {exc.msg}.") from None
    if not isinstance(value, dict):
        raise AutomationError("PR_AUTOMATION_PLAN must be a JSON object.")
    return value


def analyse(plan: Mapping[str, Any], context: Mapping[str, Any], diff: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "action": plan.get("action"), "head_sha": context.get("head_sha"),
        "engine": "deterministic-python-rules-v1",
    }
    if plan.get("metadata"):
        result["metadata"] = {"status": "ok", "data": generate_metadata(context)}
    if plan.get("review") or plan.get("security"):
        security_only = bool(plan.get("security")) and not plan.get("review")
        result["review"] = {"status": "ok", "data": review_diff(context, diff,
                                                                    security_only=security_only)}
        result["review_scope"] = "security" if security_only else "full"
    if plan.get("tests"):
        result["tests"] = {"status": "ok", "data": analyse_tests(context)}
    if plan.get("fix"):
        findings = ((result.get("review") or {}).get("data") or {}).get("findings") or []
        fixable = [f for f in findings if f.get("rule") in {
            "trailing-whitespace", "bare-except"
        }]
        result["fix"] = {
            "status": "pending" if fixable else "skipped",
            "reason": (f"{len(fixable)} deterministic fix(es) will be generated from the checkout."
                       if fixable else "No safely auto-fixable findings were detected."),
            "data": {"rules": ["trailing-whitespace", "bare-except"],
                     "count": len(fixable)},
        }
    return result


def write_summary(result: Mapping[str, Any]) -> None:
    rows = []
    for key, label in (("metadata", "Metadata"), ("review", "Rule review"),
                       ("tests", "Test-gap analysis"), ("fix", "Automatic fixes")):
        entry = result.get(key)
        if isinstance(entry, Mapping):
            data = entry.get("data") or {}
            detail = str(entry.get("reason") or "")
            if key == "review" and entry.get("status") == "ok":
                detail = f"{len(data.get('findings') or [])} reproducible finding(s)"
            elif key == "tests" and entry.get("status") == "ok":
                detail = f"{len(data.get('gaps') or [])} potential gap(s)"
            rows.append([label, str(entry.get("status", "not_run")).upper(), detail])
    summary.write(summary.heading("Deterministic PR analysis"),
                  summary.definitions({"Engine": result.get("engine", "unknown"),
                                       "External AI calls": "none"}),
                  summary.table(["Section", "Status", "Detail"], rows))


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    out_dir = artifact_dir(env)
    try:
        plan = load_plan(env)
        context = json.loads((out_dir / "pr-context.json").read_text(encoding="utf-8"))
        diff_file = out_dir / "pr.diff"
        diff = diff_file.read_text(encoding="utf-8", errors="replace") if diff_file.exists() else ""
    except (AutomationError, OSError, json.JSONDecodeError) as exc:
        print(f"::error::Could not load deterministic analysis inputs: {exc}", file=sys.stderr)
        return 1
    result = analyse(plan, context, diff)
    (out_dir / "analysis.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    write_summary(result)
    print("Deterministic analysis complete; no external AI service was called.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
