"""Measure test coverage of the lines this pull request changed.

Run as ``python -m scripts.coverage.run``.

Runs in the credential-free validation job, after the quality commands have
produced a coverage report. Method, which follows the same logic as diff-cover:

  1. take the added lines for each file from the PR diff
  2. keep only files that are source files in a measurable language
  3. for a file the coverage report knows about, an added line counts toward the
     denominator only if the report lists it — a blank line, a comment or a
     decorator is not executable, so counting it as uncovered would be wrong
  4. a source file the report does not mention at all is reported separately as
     unmeasured, because "0 of 40 new lines are tested" and "this file was never
     imported by the suite" are different problems with different fixes

The result file is always written. A repository with no coverage tooling gets
``not_run`` with the reason, never a silent pass and never a failure.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from ..common import summary
from ..common.commands import Command, run_commands, write_result
from ..common.config import (
    Target,
    load_defaults,
    load_repositories,
    resolve_target,
)
from ..common.errors import AutomationError
from ..common.globs import matches
from ..insights.base import as_bool, as_int, as_patterns, block
from ..insights.changes import parse_changes
from ..quality.run import build_runner
from . import parsers

#: Extensions whose lines a coverage tool can meaningfully measure.
DEFAULT_MEASURABLE = (
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rb", ".php",
    ".java", ".kt", ".kts", ".cs", ".scala", ".rs", ".swift", ".vue", ".svelte",
)

#: Never counted: tests, generated code, vendored trees, config.
DEFAULT_EXCLUDE = (
    "tests/**", "test/**", "**/tests/**", "**/test/**", "**/__tests__/**",
    "**/*_test.*", "**/test_*.*", "**/*.test.*", "**/*.spec.*", "**/conftest.py",
    "**/migrations/**", "**/node_modules/**", "vendor/**", "**/vendor/**",
    "**/dist/**", "**/build/**", "**/*.min.js", "**/*.pb.go", "**/*_pb2.py",
    "**/__init__.py", "**/setup.py", "**/manage.py",
)

DEFAULTS = {
    "enabled": True,
    "min_changed_line_coverage": 0,      # 0 = measure and report, never fail
    "fail_under": True,
    "treat_unmeasured_as_uncovered": False,
    "min_changed_lines": 10,             # below this, a percentage is noise
    "max_uncovered_reported": 200,
}

PASS, WARN, FAIL, NOT_RUN = "pass", "warn", "fail", "not_run"


def measurable(path: str, *, extensions: Sequence[str], exclude: Sequence[str]) -> bool:
    """True when a changed file is one whose lines coverage should account for."""
    if any(matches(pattern, path) for pattern in exclude):
        return False
    return PurePosixPath(path).suffix.lower() in set(extensions)


def match_report_path(changed: str, report_paths: Mapping[str, Any]) -> str | None:
    """Map a repo-relative changed path onto the path a coverage report used.

    Coverage tools write paths relative to wherever they ran, or absolute, or
    package-qualified. Matching on the longest shared path suffix handles all
    three without needing to know the source root.
    """
    if changed in report_paths:
        return changed

    changed_parts = changed.split("/")
    best: tuple[int, str] | None = None
    for candidate in report_paths:
        candidate_parts = candidate.split("/")
        shared = 0
        for left, right in zip(reversed(changed_parts), reversed(candidate_parts)):
            if left != right:
                break
            shared += 1
        # A shared filename alone is too weak — two "index.ts" files are common —
        # unless the changed path is itself a single segment.
        if shared < min(2, len(changed_parts)):
            continue
        if best is None or shared > best[0] or (shared == best[0] and candidate < best[1]):
            best = (shared, candidate)
    return best[1] if best else None


def analyse(
    diff: str,
    line_hits: parsers.LineHits,
    *,
    extensions: Sequence[str] = DEFAULT_MEASURABLE,
    exclude: Sequence[str] = DEFAULT_EXCLUDE,
    treat_unmeasured_as_uncovered: bool = False,
) -> dict[str, Any]:
    """Intersect the diff's added lines with the coverage report."""
    changes = parse_changes(diff)
    files: list[dict[str, Any]] = []
    unmeasured: list[dict[str, Any]] = []
    skipped: list[str] = []
    total_measured = total_covered = 0
    total_unmeasured_lines = 0

    for path, change in sorted(changes.items()):
        if change.is_deleted or change.is_binary or not change.added:
            continue
        if not measurable(path, extensions=extensions, exclude=exclude):
            skipped.append(path)
            continue

        added_numbers = sorted({line.line for line in change.added})
        report_path = match_report_path(path, line_hits)
        if report_path is None:
            unmeasured.append({
                "path": path,
                "added_lines": len(added_numbers),
                "reason": "No coverage report mentions this file.",
            })
            total_unmeasured_lines += len(added_numbers)
            continue

        hits = line_hits[report_path]
        measured = [number for number in added_numbers if number in hits]
        covered = [number for number in measured if hits[number] > 0]
        uncovered = [number for number in measured if hits[number] <= 0]
        total_measured += len(measured)
        total_covered += len(covered)

        files.append({
            "path": path,
            "report_path": report_path if report_path != path else "",
            "added_lines": len(added_numbers),
            "measured_lines": len(measured),
            "covered_lines": len(covered),
            "uncovered_lines": uncovered[:100],
            "uncovered_count": len(uncovered),
            "percent": round(100.0 * len(covered) / len(measured), 1) if measured else None,
            # Added lines the report does not list are not executable statements.
            "not_executable": len(added_numbers) - len(measured),
        })

    denominator = total_measured
    numerator = total_covered
    if treat_unmeasured_as_uncovered:
        denominator += total_unmeasured_lines

    percent = round(100.0 * numerator / denominator, 1) if denominator else None
    return {
        "changed_line_coverage": percent,
        "measured_lines": total_measured,
        "covered_lines": total_covered,
        "uncovered_lines": total_measured - total_covered,
        "unmeasured_lines": total_unmeasured_lines,
        "denominator": denominator,
        "files": sorted(files, key=lambda f: (f["percent"] if f["percent"] is not None else 101,
                                              -f["uncovered_count"], f["path"])),
        "unmeasured_files": unmeasured,
        "skipped_files": sorted(skipped)[:100],
        "treat_unmeasured_as_uncovered": treat_unmeasured_as_uncovered,
    }


def evaluate(analysis: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the configured threshold to a coverage analysis."""
    threshold = as_int(policy.get("min_changed_line_coverage"), 0)
    min_lines = as_int(policy.get("min_changed_lines"), DEFAULTS["min_changed_lines"])
    fail_under = as_bool(policy.get("fail_under"), True)

    percent = analysis.get("changed_line_coverage")
    denominator = as_int(analysis.get("denominator"), 0)
    unmeasured_files = analysis.get("unmeasured_files") or []
    notes: list[str] = []

    if denominator == 0:
        status = NOT_RUN
        reason = (
            "No changed line was measurable: the coverage report covers none of the "
            "changed source files."
            if unmeasured_files else
            "This pull request changed no measurable source lines."
        )
        if unmeasured_files:
            status = WARN
            notes.append(
                f"{len(unmeasured_files)} changed source file(s) appear in no coverage report. "
                "Either the suite never imports them, or the report was written before these "
                "files were added."
            )
    elif threshold <= 0:
        status = PASS
        reason = f"{percent}% of {denominator} changed line(s) covered (no threshold configured)."
    elif denominator < min_lines:
        status = PASS
        reason = (
            f"{percent}% of {denominator} changed line(s) covered. Below the {min_lines}-line "
            "floor for enforcing a percentage, so the threshold was not applied."
        )
    elif percent is not None and percent + 1e-9 < threshold:
        status = FAIL if fail_under else WARN
        reason = (
            f"{percent}% of {denominator} changed line(s) covered, under the "
            f"{threshold}% threshold. {analysis['uncovered_lines']} changed line(s) are "
            "executed by no test."
        )
    else:
        status = PASS
        reason = (
            f"{percent}% of {denominator} changed line(s) covered, at or above the "
            f"{threshold}% threshold."
        )

    if unmeasured_files and status == PASS:
        status = WARN
        notes.append(
            f"{len(unmeasured_files)} changed source file(s) are not present in any coverage "
            "report and are excluded from the percentage."
        )

    return {"status": status, "reason": reason, "threshold": threshold, "notes": notes}


def _load_diff(env: Mapping[str, str]) -> str:
    path = Path(env.get("PR_DIFF_FILE") or "")
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _coverage_config(target: Target) -> dict[str, Any]:
    return {**DEFAULTS, **block(target.config, "coverage")}


def write_coverage_summary(payload: Mapping[str, Any]) -> None:
    """Render the coverage result into the Actions job summary."""
    status = str(payload.get("status", NOT_RUN))
    analysis = payload.get("analysis") or {}
    percent = analysis.get("changed_line_coverage")
    blocks = [
        summary.heading(
            f"🧪 Changed-line coverage — {summary.icon(status)} {status.upper()}"
        ),
        summary.definitions({
            "Changed-line coverage": f"{percent}%" if percent is not None else "not measurable",
            "Threshold": (
                f"{payload.get('threshold')}%" if payload.get("threshold") else "none configured"
            ),
            "Measured lines": analysis.get("measured_lines", 0),
            "Covered": analysis.get("covered_lines", 0),
            "Uncovered": analysis.get("uncovered_lines", 0),
            "Reports parsed": len(
                [r for r in payload.get("reports") or [] if r.get("status") == "ok"]
            ),
        }),
    ]
    if payload.get("reason"):
        blocks.append(f"> [!NOTE]\n> {payload['reason']}")

    files = [f for f in analysis.get("files") or [] if f.get("uncovered_count")]
    if files:
        blocks.append(summary.details(
            f"Files with uncovered changed lines ({len(files)})",
            summary.table(
                ["File", "Covered", "Measured", "%", "Uncovered lines"],
                [
                    [
                        f"`{f['path']}`", f["covered_lines"], f["measured_lines"],
                        "—" if f["percent"] is None else f"{f['percent']}%",
                        ", ".join(str(n) for n in f["uncovered_lines"][:15])
                        + ("…" if f["uncovered_count"] > 15 else ""),
                    ]
                    for f in files[:40]
                ],
            ),
            open_by_default=status == FAIL,
        ))

    unmeasured = analysis.get("unmeasured_files") or []
    if unmeasured:
        blocks.append(summary.details(
            f"Changed files absent from every coverage report ({len(unmeasured)})",
            summary.bullets(
                f"`{item['path']}` — {item['added_lines']} added line(s)" for item in unmeasured
            ),
        ))

    reports = payload.get("reports") or []
    if reports:
        blocks.append(summary.details(
            f"Coverage reports ({len(reports)})",
            summary.table(
                ["Report", "Format", "Status", "Files", "Note"],
                [
                    [f"`{r['path']}`", r["format"], f"{summary.icon(r['status'])} {r['status']}",
                     r.get("files", 0), r.get("reason", "")]
                    for r in reports
                ],
            ),
        ))
    summary.write(*blocks)


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    try:
        repository = env["TARGET_REPOSITORY"]
        config_file = env["CONFIG_FILE"]
        result_file = env["COVERAGE_RESULT_FILE"]
    except KeyError as exc:
        print(f"::error::Missing required environment variable: {exc}", file=sys.stderr)
        return 2

    workdir = env.get("TARGET_WORKDIR") or "."
    try:
        target = resolve_target(
            repository,
            env={"TARGET_ORG": env.get("TARGET_ORG") or "validation-only"},
            repositories=load_repositories(config_file),
            defaults=load_defaults(config_file),
        )
    except AutomationError as exc:
        write_result(result_file, {"status": "error", "reason": str(exc)})
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    policy = _coverage_config(target)
    if not as_bool(policy.get("enabled"), True):
        payload = {"status": NOT_RUN, "reason": "Coverage measurement is disabled for this "
                                                "repository.", "analysis": {}, "reports": []}
        write_result(result_file, payload)
        write_coverage_summary(payload)
        print("Changed-line coverage: NOT RUN (disabled).")
        return 0

    diff = _load_diff(env)
    if not diff.strip():
        payload = {"status": NOT_RUN, "reason": "The PR diff artifact was unavailable, so no "
                                                "changed lines could be identified.",
                   "analysis": {}, "reports": []}
        write_result(result_file, payload)
        write_coverage_summary(payload)
        print("Changed-line coverage: NOT RUN (no diff).")
        return 0

    root = Path(workdir)
    if not root.is_dir():
        payload = {"status": "error", "reason": f"Target working directory {workdir!r} is missing.",
                   "analysis": {}, "reports": []}
        write_result(result_file, payload)
        print(f"::error::Target working directory {workdir!r} is missing.", file=sys.stderr)
        return 1

    # An optional command that produces the report, for a repository whose test
    # command does not emit one by default. It runs under the same sandbox rules
    # as every other target command.
    command_output: dict[str, Any] = {}
    report_command = str(policy.get("command") or "").strip()
    if report_command:
        try:
            runner, sandbox_info = build_runner(target, workdir)
        except AutomationError as exc:
            runner, sandbox_info = None, {"ready": False, "reason": str(exc), "image": ""}
        results = run_commands(
            [Command(name="Coverage report", run=report_command, group="test")],
            cwd=workdir, runner=runner,
        )
        item = results[0] if results else None
        command_output = {
            "command": report_command,
            "status": item.status if item else "not_run",
            "exit_code": item.exit_code if item else None,
            "output_tail": (item.output_tail[-2000:] if item else ""),
            "sandbox": sandbox_info,
        }
        if item and item.status != "pass":
            print(f"::warning::The coverage command exited {item.exit_code}; parsing whatever "
                  "report it produced.")

    discovered = parsers.discover(root, as_patterns(policy.get("reports")))
    line_hits, reports = parsers.load(discovered)
    # Report paths are relative to the target checkout; the diff's are relative to
    # the repository root, so strip the checkout directory prefix.
    reports = [
        {**entry, "path": _relative_to(entry["path"], root)} for entry in reports
    ]

    if not line_hits:
        payload = {
            "status": NOT_RUN,
            "reason": (
                "No readable coverage report was found. Configure `coverage.reports` or a "
                "`coverage.command` for this repository, or have the test command emit one "
                "(for example `pytest --cov --cov-report=xml`)."
                if not reports else
                "Coverage report(s) were found but none contained per-line data."
            ),
            "analysis": {}, "reports": reports, "command": command_output,
        }
        write_result(result_file, payload)
        write_coverage_summary(payload)
        print(f"Changed-line coverage: NOT RUN ({payload['reason']})")
        return 0

    extensions = as_patterns(policy.get("include_extensions")) or list(DEFAULT_MEASURABLE)
    exclude = list(DEFAULT_EXCLUDE) + as_patterns(policy.get("exclude_paths"))
    analysis = analyse(
        diff, line_hits,
        extensions=extensions,
        exclude=exclude,
        treat_unmeasured_as_uncovered=as_bool(
            policy.get("treat_unmeasured_as_uncovered"), False
        ),
    )
    max_reported = as_int(policy.get("max_uncovered_reported"),
                          DEFAULTS["max_uncovered_reported"])
    analysis["files"] = analysis["files"][:max_reported]

    decision = evaluate(analysis, policy)
    payload = {
        "status": decision["status"],
        "reason": decision["reason"],
        "threshold": decision["threshold"],
        "notes": decision["notes"],
        "analysis": analysis,
        "reports": reports,
        "command": command_output,
    }
    write_result(result_file, payload)
    write_coverage_summary(payload)

    percent = analysis.get("changed_line_coverage")
    print(f"Changed-line coverage: {percent if percent is not None else 'n/a'}% "
          f"({analysis['covered_lines']}/{analysis['denominator']} line(s))")
    print(f"Status: {decision['status'].upper()} — {decision['reason']}")
    # The report job publishes the gate, so a threshold breach is not signalled by
    # this exit code; the result file carries it.
    return 0


def _relative_to(path: str, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return path


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
