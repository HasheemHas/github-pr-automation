"""Detect flaky tests by running the suite more than once.

Run as ``python -m scripts.flaky.run``.

A test is flaky when it produces more than one outcome for the same code. That is
the only definition this module uses, and it is established two ways:

  * across runs — the suite is executed N times against an unchanged tree, and any
    test that both passed and failed is flaky by observation
  * within a run — the runner's own rerun markers (Surefire ``flakyFailure``,
    pytest-rerunfailures / Jest ``rerunFailure``) are proof of a retry, so
    flakiness is detected even with ``runs: 1``

A test that failed in every run is reported separately as a consistent failure:
that is a real defect, and burying it among the flakes is how a genuine
regression gets ignored.

This is off by default. It multiplies test-suite time by ``runs``, which is a
cost a repository should opt into deliberately.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..common import summary
from ..common.commands import Command, CommandResult, run_commands, write_result
from ..common.config import (
    Target,
    commands_for,
    load_defaults,
    load_repositories,
    resolve_target,
)
from ..common.errors import AutomationError
from ..insights.base import as_bool, as_int, as_patterns, block
from ..quality.run import build_runner, load_detection
from . import junit

DEFAULTS = {
    "enabled": False,
    "runs": 3,
    "max_runs": 10,
    "fail_on_flaky": False,
    "min_failures": 1,
    "timeout": 1800,
}

PASS, WARN, FAIL, NOT_RUN = "pass", "warn", "fail", "not_run"


def commands_to_repeat(
    target: Target, detection: Mapping[str, Any] | None, override: str = ""
) -> list[Command]:
    """The command(s) to repeat: an explicit override, else the test group."""
    if override.strip():
        return [Command(name="Flaky probe", run=override.strip(), group="test")]
    return commands_for(target, "test", detection=detection)


def clear_reports(paths: Sequence[Path]) -> int:
    """Delete stale JUnit reports so a run's results cannot be double-counted.

    The target checkout is a throwaway artifact, so removing generated report
    files from it changes nothing that is ever pushed.
    """
    removed = 0
    for path in paths:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def classify(
    runs: Sequence[Sequence[junit.TestCase]],
    *,
    min_failures: int = 1,
) -> dict[str, Any]:
    """Split observed outcomes into flaky, consistently failing and stable."""
    outcomes: dict[str, dict[str, Any]] = {}

    for index, cases in enumerate(runs):
        for case in cases:
            entry = outcomes.setdefault(case.identifier, {
                "identifier": case.identifier,
                "name": case.name,
                "classname": case.classname,
                "file": case.file,
                "passes": 0, "failures": 0, "errors": 0, "skips": 0,
                "runs_seen": 0,
                "reran": False,
                "messages": [],
                "failing_runs": [],
            })
            entry["runs_seen"] += 1
            entry["reran"] = entry["reran"] or case.reran
            if case.status == junit.PASSED:
                entry["passes"] += 1
            elif case.status == junit.FAILED:
                entry["failures"] += 1
                entry["failing_runs"].append(index + 1)
            elif case.status == junit.ERROR:
                entry["errors"] += 1
                entry["failing_runs"].append(index + 1)
            else:
                entry["skips"] += 1
            if case.message and case.message not in entry["messages"]:
                entry["messages"].append(case.message)

    flaky: list[dict[str, Any]] = []
    consistent: list[dict[str, Any]] = []
    inconsistent: list[dict[str, Any]] = []
    total_runs = len(runs)

    for entry in outcomes.values():
        bad = entry["failures"] + entry["errors"]
        good = entry["passes"]
        # The framework's own retry marker is proof on its own.
        if entry["reran"] or (bad >= min_failures and good > 0):
            entry["reason"] = (
                "the test runner recorded a retry for this test"
                if entry["reran"] and not (bad and good)
                else f"passed {good} time(s) and failed {bad} time(s) on identical code"
            )
            flaky.append(entry)
        elif bad and not good:
            entry["reason"] = f"failed in all {bad} run(s) where it executed"
            consistent.append(entry)
        elif total_runs > 1 and entry["runs_seen"] < total_runs and not entry["skips"]:
            # A test that only appears in some runs is a collection-order or
            # test-selection problem, which behaves like flakiness in CI.
            entry["reason"] = (
                f"collected in only {entry['runs_seen']} of {total_runs} run(s)"
            )
            inconsistent.append(entry)

    order = (lambda item: (-(item["failures"] + item["errors"]), item["identifier"]))
    return {
        "flaky": sorted(flaky, key=order),
        "consistent_failures": sorted(consistent, key=order),
        "inconsistent_collection": sorted(inconsistent, key=order),
        "total_tests": len(outcomes),
        "runs": total_runs,
    }


def write_flaky_summary(payload: Mapping[str, Any]) -> None:
    status = str(payload.get("status", NOT_RUN))
    analysis = payload.get("analysis") or {}
    blocks = [
        summary.heading(f"🎲 Flaky test detection — {summary.icon(status)} {status.upper()}"),
        summary.definitions({
            "Runs": payload.get("runs", 0),
            "Tests observed": analysis.get("total_tests", 0),
            "Flaky": len(analysis.get("flaky") or []),
            "Consistent failures": len(analysis.get("consistent_failures") or []),
            "Inconsistently collected": len(analysis.get("inconsistent_collection") or []),
        }),
    ]
    if payload.get("reason"):
        blocks.append(f"> [!NOTE]\n> {payload['reason']}")

    for key, title in (
        ("flaky", "Flaky tests"),
        ("consistent_failures", "Consistent failures (real, not flaky)"),
        ("inconsistent_collection", "Tests not collected in every run"),
    ):
        entries = analysis.get(key) or []
        if not entries:
            continue
        blocks.append(summary.details(
            f"{title} ({len(entries)})",
            summary.table(
                ["Test", "Passed", "Failed", "Why", "First message"],
                [
                    [
                        f"`{entry['identifier']}`", entry["passes"],
                        entry["failures"] + entry["errors"], entry.get("reason", ""),
                        (entry.get("messages") or [""])[0][:160],
                    ]
                    for entry in entries[:40]
                ],
            ),
            open_by_default=key == "flaky",
        ))

    runs = payload.get("run_results") or []
    if runs:
        blocks.append(summary.details(
            f"Run results ({len(runs)})",
            summary.table(
                ["Run", "Command", "Result", "Exit", "Tests parsed"],
                [
                    [r["run"], f"`{r['command']}`",
                     f"{summary.icon(r['status'])} {r['status']}",
                     "—" if r.get("exit_code") is None else r["exit_code"],
                     r.get("tests", 0)]
                    for r in runs
                ],
            ),
        ))
    summary.write(*blocks)


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    try:
        repository = env["TARGET_REPOSITORY"]
        config_file = env["CONFIG_FILE"]
        result_file = env["FLAKY_RESULT_FILE"]
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

    policy = {**DEFAULTS, **block(target.config, "flaky")}
    if not as_bool(policy.get("enabled"), False):
        payload = {
            "status": NOT_RUN,
            "reason": ("Flaky detection is disabled. Set `flaky.enabled: true` for this "
                       "repository to enable it; it repeats the test suite, so enable it "
                       "deliberately."),
            "analysis": {}, "runs": 0, "run_results": [],
        }
        write_result(result_file, payload)
        write_flaky_summary(payload)
        print("Flaky detection: NOT RUN (disabled).")
        return 0

    root = Path(workdir)
    if not root.is_dir():
        payload = {"status": "error",
                   "reason": f"Target working directory {workdir!r} is missing.",
                   "analysis": {}, "runs": 0, "run_results": []}
        write_result(result_file, payload)
        print(f"::error::Target working directory {workdir!r} is missing.", file=sys.stderr)
        return 1

    detection = load_detection(env)
    try:
        commands = commands_to_repeat(target, detection, str(policy.get("command") or ""))
    except AutomationError as exc:
        write_result(result_file, {"status": "error", "reason": str(exc)})
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if not commands:
        payload = {
            "status": NOT_RUN,
            "reason": ("No test command is configured or detected, so there is nothing to "
                       "repeat. Set `flaky.command` for this repository."),
            "analysis": {}, "runs": 0, "run_results": [],
        }
        write_result(result_file, payload)
        write_flaky_summary(payload)
        print("Flaky detection: NOT RUN (no test command).")
        return 0

    runs = min(
        max(as_int(policy.get("runs"), DEFAULTS["runs"]), 1),
        as_int(policy.get("max_runs"), DEFAULTS["max_runs"]) or DEFAULTS["max_runs"],
    )
    timeout = as_int(policy.get("timeout"), DEFAULTS["timeout"]) or DEFAULTS["timeout"]
    patterns = as_patterns(policy.get("reports")) or list(junit.DEFAULT_PATTERNS)

    try:
        runner, sandbox_info = build_runner(target, workdir)
    except AutomationError as exc:
        runner, sandbox_info = None, {"ready": False, "reason": str(exc), "image": ""}

    observations: list[list[junit.TestCase]] = []
    run_results: list[dict[str, Any]] = []
    read_errors: list[str] = []

    for index in range(1, runs + 1):
        # Clear stale reports first, so run N's parse cannot pick up run N-1's file.
        clear_reports(junit.discover(root, patterns))
        results: list[CommandResult] = run_commands(
            commands, cwd=workdir, runner=runner, timeout=timeout
        )
        cases, errors = junit.load(junit.discover(root, patterns))
        read_errors.extend(errors)
        observations.append(cases)
        worst = "pass"
        for item in results:
            if item.status == "fail":
                worst = "fail"
                break
            if item.status in ("skipped", "not_run"):
                worst = item.status
        run_results.append({
            "run": index,
            "command": "; ".join(c.run for c in commands)[:200],
            "status": worst,
            "exit_code": next((r.exit_code for r in results if r.exit_code), 0),
            "tests": len(cases),
            "output_tail": "\n".join(r.output_tail[-800:] for r in results if r.status == "fail"),
        })
        print(f"Run {index}/{runs}: {worst.upper()} ({len(cases)} test result(s) parsed)")

    if not any(observations):
        payload = {
            "status": NOT_RUN,
            "reason": (
                "The test command ran but produced no JUnit XML, so per-test outcomes are "
                "unavailable. Configure the runner to emit JUnit XML (pytest "
                "`--junitxml=junit.xml`, Jest `jest-junit`, PHPUnit `--log-junit`, Go "
                "`gotestsum --junitfile`) and set `flaky.reports` if it lands somewhere "
                "unusual."
            ),
            "analysis": {}, "runs": runs, "run_results": run_results,
            "report_errors": read_errors[:20], "sandbox": sandbox_info,
        }
        write_result(result_file, payload)
        write_flaky_summary(payload)
        print("Flaky detection: NOT RUN (no JUnit XML produced).")
        return 0

    analysis = classify(
        observations, min_failures=as_int(policy.get("min_failures"), 1) or 1
    )
    flaky_count = len(analysis["flaky"])
    consistent_count = len(analysis["consistent_failures"])

    if flaky_count and as_bool(policy.get("fail_on_flaky"), False):
        status = FAIL
    elif flaky_count or analysis["inconsistent_collection"]:
        status = WARN
    else:
        status = PASS

    reason = (
        f"{flaky_count} flaky test(s) and {consistent_count} consistent failure(s) across "
        f"{runs} run(s) of an unchanged tree."
        if flaky_count or consistent_count else
        f"No test changed its outcome across {runs} run(s)."
    )
    if runs == 1:
        reason += (" Only one run was performed, so flakiness was detected solely from the "
                   "runner's own rerun markers.")

    payload = {
        "status": status,
        "reason": reason,
        "runs": runs,
        "analysis": analysis,
        "run_results": run_results,
        "report_errors": read_errors[:20],
        "sandbox": sandbox_info,
        "policy": {
            "runs": runs, "fail_on_flaky": as_bool(policy.get("fail_on_flaky"), False),
        },
    }
    write_result(result_file, payload)
    write_flaky_summary(payload)

    for entry in analysis["flaky"][:20]:
        print(f"FLAKY: {entry['identifier']} — {entry['reason']}")
    for entry in analysis["consistent_failures"][:20]:
        print(f"FAILING: {entry['identifier']} — {entry['reason']}")
    print(f"Flaky detection: {status.upper()} — {reason}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
