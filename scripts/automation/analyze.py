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
from ..insights import describe
from ..insights import run as insights_run
from ..insights.changes import parse_changes

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
    # "Show generated metadata" and "Automatically detect conflicts" are features;
    # classifying them as chores understated every such pull request.
    if any(word in text for word in (
        "add", "feature", "implement", "support", "introduce", "show", "display",
        "detect", "expose", "enable", "generate", "report",
    )):
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


class _SafeFormat(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _template_text(template: Mapping[str, Any], key: str) -> str:
    value = template.get(key)
    return str(value if isinstance(value, str) else "")


def _render_template(template: Mapping[str, Any], key: str, values: Mapping[str, Any]) -> str:
    return _template_text(template, key).format_map(_SafeFormat(values)).strip()


def _risk_reason(template: Mapping[str, Any], risk: str) -> str:
    reasons = template.get("risk_reasons")
    if not isinstance(reasons, Mapping):
        return ""
    return str(reasons.get(risk) or "").strip()


def _render_list_template(
    template: Mapping[str, Any],
    key: str,
    values: Iterable[str],
    variable: str,
    base_values: Mapping[str, Any],
    *,
    limit: int,
) -> list[str]:
    pattern = _template_text(template, key)
    rendered: list[str] = []
    for value in list(values)[:limit]:
        data = dict(base_values)
        data[variable] = value
        text = pattern.format_map(_SafeFormat(data)).strip() if pattern else str(value).strip()
        if text:
            rendered.append(text)
    return rendered


def generate_metadata(
    context: Mapping[str, Any],
    template: Mapping[str, Any] | None = None,
    *,
    diff: str = "",
    insights: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the PR metadata, deriving the prose from the diff.

    ``diff`` is what makes the description say something: without it there is
    nothing to read but the path list, and the result degrades to the same
    file-name summary the old template produced. ``insights`` lets the description
    reuse what the other checks already established — routes, migrations,
    dependency bumps — instead of re-deriving it.

    ``template`` is an override layer, applied only where a key is set. It is
    never the source of the content.
    """
    template = template or {}
    changes = parse_changes(diff) if diff else {}
    paths = [str(p) for p in context.get("files") or []]
    components = _components(paths)
    # Build artefacts are not work and must not be described as changes.
    reviewable = [p for p in paths if not describe.is_noise(p)]
    reviewable_components = _components(reviewable) or components
    kind = _change_type(context)
    commits = [str(c).strip() for c in context.get("commits") or [] if str(c).strip()]
    subject = commits[0] if commits else str(context.get("title") or "Update repository")
    subject = re.sub(r"^(?:feat|fix|chore|docs|test|refactor)(?:\([^)]*\))?!?:\s*", "", subject,
                     flags=re.IGNORECASE).strip().rstrip(".")
    component = (
        reviewable_components[0] if len(reviewable_components) == 1 else "repository"
    )
    prefix = {"feature": "feat", "documentation": "docs"}.get(kind, kind)
    title = f"{prefix}({component}): {subject[:180]}"
    files = len(paths)
    additions = int(context.get("additions") or 0)
    deletions = int(context.get("deletions") or 0)
    # Risk is judged on files a human has to review, so a refreshed lock file or a
    # committed .pyc cannot inflate it.
    risk = "high" if any(
        p.startswith((".github/", "terraform/", "helm/", "migrations/"))
        or PurePosixPath(p).name.lower() in {"dockerfile", "docker-compose.yml"}
        for p in reviewable
    ) else "medium" if len(reviewable) > 10 or additions + deletions > 500 else "low"
    refs = sorted(set(_ISSUE.findall(" ".join([
        str(context.get("title") or ""), str(context.get("body") or ""), *commits
    ]))))
    component_text = (
        ", ".join(reviewable_components) if reviewable_components else "the repository"
    )
    template_values = {
        "subject": subject,
        "components": component_text,
        "component": component,
        "change_type": kind,
        "risk": risk.title(),
        "risk_lower": risk,
        "files": files,
        "additions": additions,
        "deletions": deletions,
        "commits": len(commits),
    }
    derived_risk_reason = describe.risk_reason(context, risk, insights=insights)
    # Details describe each file; At a Glance summarises the change as a whole. The
    # two must never be the same list, which is what a single `detail_item`
    # template rendered per path produced.
    #
    # A configured `detail_item` replaces the derived bullets outright — both keys
    # get the same list, or the override would be silently dead because `details`
    # takes precedence over `changes` when the block is rendered.
    templated_details = _render_list_template(
        template, "detail_item", reviewable, "path", template_values, limit=20
    ) if _template_text(template, "detail_item") else []
    details = templated_details or (
        describe.detail_bullets(changes, context) if changes
        else [f"`{path}` changed" for path in reviewable[:20]]
    )
    glance = describe.at_a_glance(
        changes, context, change_type=kind, risk=risk,
        risk_reason=derived_risk_reason, insights=insights,
    )

    return {
        "title": title,
        "summary": _render_template(template, "summary", template_values) or glance,
        "description": (
            _render_template(template, "description", template_values) or glance
        ),
        "at_glance": glance,
        "change_type": kind,
        "risk": risk,
        "risk_reason": _risk_reason(template, risk) or derived_risk_reason,
        "breaking_change": False,
        "breaking_change_details": "",
        "components": reviewable_components,
        # `changes` stays the per-file list for consumers that already read it;
        # `details` is the same content, and At a Glance no longer reuses it.
        "changes": details,
        "details": details,
        "release_notes": [note] if (note := _render_template(template, "release_note",
                                                             template_values)) else [],
        "recommended_testing": _render_list_template(
            template, "testing_item", reviewable_components, "component",
            template_values, limit=8,
        ) or describe.test_plan(context),
        "test_plan": describe.test_plan(context),
        "review_focus": _render_list_template(
            template, "review_focus_item", reviewable, "path", template_values, limit=10
        ),
        "migration_notes": [],
        "rollback": _render_template(template, "rollback", template_values),
        "user_impact": (
            _render_template(template, "user_impact", template_values)
            or describe.user_impact(context, insights=insights)
        ),
        "noise": describe.group_noise(paths),
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
    # A committed .pyc under tests/ is not a changed test, and reporting it as one
    # produced a test plan asking the reviewer to run compiled bytecode.
    changed = [str(p) for p in context.get("files") or [] if not describe.is_noise(str(p))]
    existing = [
        str(p) for p in context.get("repository_tests") or [] if not describe.is_noise(str(p))
    ]
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
    if plan.get("review") or plan.get("security"):
        security_only = bool(plan.get("security")) and not plan.get("review")
        result["review"] = {"status": "ok", "data": review_diff(context, diff,
                                                                    security_only=security_only)}
        result["review_scope"] = "security" if security_only else "full"
    if plan.get("tests"):
        result["tests"] = {"status": "ok", "data": analyse_tests(context)}
    # Diff-based insights: size, secrets, dependencies, migrations, API, notes.
    result.update(insights_run.run_all(plan, context, diff, plan.get("policies") or {}))

    # Metadata runs LAST, so the description is written from the diff and from what
    # the insights already established rather than from the path list alone.
    if plan.get("metadata"):
        options = plan.get("metadata_options") or {}
        template = options.get("template") if isinstance(options, Mapping) else {}
        if not isinstance(template, Mapping):
            template = {}
        result["metadata"] = {
            "status": "ok",
            "data": generate_metadata(context, template, diff=diff, insights=result),
        }
        # Test plan and migration notes need the test-gap and migration results.
        enrich_metadata(result, context)
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


def _section_data(result: Mapping[str, Any], key: str) -> dict[str, Any]:
    """The ``data`` payload of one analysis section, or an empty mapping."""
    entry = result.get(key)
    if not isinstance(entry, Mapping) or entry.get("status") != "ok":
        return {}
    data = entry.get("data")
    return dict(data) if isinstance(data, Mapping) else {}


def enrich_metadata(
    result: dict[str, Any], context: Mapping[str, Any] | None = None
) -> None:
    """Fold insight findings into the generated PR metadata, in place.

    The path-based heuristics in :func:`generate_metadata` are a fallback. Where
    an insight actually measured something — the real risk level, a genuinely
    breaking interface change, the migrations in this change — that measurement
    replaces the guess, so the PR description and the report cannot disagree.
    """
    entry = result.get("metadata")
    if not isinstance(entry, Mapping) or entry.get("status") != "ok":
        return
    metadata = entry.get("data")
    if not isinstance(metadata, dict):
        return

    size_data = _section_data(result, "size")
    if size_data.get("risk"):
        # The measured level replaces the path heuristic. The reason itself is
        # already derived in scripts/insights/describe.py, which names the files
        # that decided it, so it is not rewritten here.
        metadata["risk"] = size_data["risk"]

    api_data = _section_data(result, "api")
    if api_data.get("breaking_change"):
        metadata["breaking_change"] = True
        breaking = [
            f for f in api_data.get("findings") or []
            if f.get("severity") in ("critical", "high")
        ]
        metadata["breaking_change_details"] = " ".join(
            str(f.get("title", "")) + "." for f in breaking[:5]
        ) or "Interface changes were detected in this pull request."

    migration_data = _section_data(result, "migrations")
    records = migration_data.get("migrations") or []
    if records:
        notes_list = [
            f"`{record['path']}` ({record['framework']}"
            + (", not reversible" if not record.get("reversible") else "")
            + (f", {len(record.get('risky_operations') or [])} risky operation(s)"
               if record.get("risky_operations") else "")
            + ")"
            for record in records[:10]
        ]
        metadata["migration_notes"] = notes_list

    notes_data = _section_data(result, "release_notes")
    generated = [
        entry_text
        for section in notes_data.get("sections") or []
        for entry_text in section.get("entries") or []
    ]
    if generated:
        metadata["release_notes"] = generated[:20]

    secrets_data = _section_data(result, "secrets")
    if secrets_data.get("hits"):
        # A committed credential belongs at the top of the review focus list.
        focus = [
            f"Remove and rotate the credential at `{hit['path']}`:{hit['line']}."
            for hit in secrets_data["hits"][:5]
        ]
        metadata["review_focus"] = focus + list(metadata.get("review_focus") or [])[:8]

    # The test plan is derived from the test-gap analysis and the coverage result
    # where those ran, so it names real tests instead of a component name.
    tests_data = _section_data(result, "tests")
    if tests_data and context is not None:
        plan_steps = describe.test_plan(
            context, tests=tests_data, coverage=_section_data(result, "coverage"),
        )
        if plan_steps:
            metadata["test_plan"] = plan_steps
            metadata["recommended_testing"] = plan_steps


#: Section key -> label, in the order the job summary lists them.
SUMMARY_SECTIONS = (
    ("metadata", "Metadata"),
    ("review", "Rule review"),
    ("tests", "Test-gap analysis"),
    ("size", "PR size and risk"),
    ("secrets", "Secret scan"),
    ("dependencies", "Dependency risk"),
    ("migrations", "Migrations"),
    ("api", "API compatibility"),
    ("release_notes", "Release notes"),
    ("fix", "Automatic fixes"),
)


def _detail(key: str, entry: Mapping[str, Any]) -> str:
    """One line describing what a section found."""
    data = entry.get("data") or {}
    if entry.get("status") != "ok":
        return str(entry.get("error") or entry.get("reason") or "")
    if key == "review":
        return f"{len(data.get('findings') or [])} reproducible finding(s)"
    if key == "tests":
        return f"{len(data.get('gaps') or [])} potential gap(s)"
    if key == "metadata":
        return "Summary generated"
    # Every insight carries its own one-line summary.
    return str(data.get("summary") or entry.get("reason") or "")


def write_summary(result: Mapping[str, Any]) -> None:
    rows = []
    for key, label in SUMMARY_SECTIONS:
        entry = result.get(key)
        if not isinstance(entry, Mapping):
            continue
        data = entry.get("data") or {}
        status = str(data.get("status") or entry.get("status") or "not_run")
        rows.append([label, status.upper(), _detail(key, entry)])

    blocks = [
        summary.heading("Deterministic PR analysis"),
        summary.definitions({"Engine": result.get("engine", "unknown"),
                             "External AI calls": "none"}),
        summary.table(["Section", "Status", "Detail"], rows),
    ]

    # Findings from the insights, grouped so the run page explains each verdict
    # without anyone opening analysis.json.
    for key, label in SUMMARY_SECTIONS:
        entry = result.get(key)
        if not isinstance(entry, Mapping) or entry.get("status") != "ok":
            continue
        findings = ((entry.get("data") or {}).get("findings")) or []
        if not findings:
            continue
        blocks.append(summary.details(
            f"{label} — {len(findings)} finding(s)",
            summary.table(
                ["Severity", "File", "Finding", "Detail"],
                [
                    [
                        f.get("severity", ""),
                        f"`{f.get('path')}`" + (f":{f['line']}" if f.get("line") else "")
                        if f.get("path") else "—",
                        f.get("title", ""),
                        f.get("detail", ""),
                    ]
                    for f in findings[:25]
                ],
            ),
        ))

    notes_entry = result.get("release_notes")
    if isinstance(notes_entry, Mapping) and notes_entry.get("status") == "ok":
        markdown = ((notes_entry.get("data") or {}).get("markdown") or "").strip()
        if markdown:
            blocks.append(summary.details("Generated release notes", markdown))

    summary.write(*blocks)


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

    # Release notes also travel as a standalone file so a release job can consume
    # them without parsing analysis.json.
    markdown = (_section_data(result, "release_notes").get("markdown") or "").strip()
    if markdown:
        (out_dir / "release-notes.md").write_text(markdown + "\n", encoding="utf-8")

    write_summary(result)
    for key, label in SUMMARY_SECTIONS:
        entry = result.get(key)
        if isinstance(entry, Mapping):
            data = entry.get("data") or {}
            status = str(data.get("status") or entry.get("status") or "not_run")
            print(f"{label}: {status.upper()} — {_detail(key, entry)}")
    print("Deterministic analysis complete; no external AI service was called.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
