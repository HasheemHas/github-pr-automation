"""Run configured security scanners against a checked-out target tree.

Run as ``python -m scripts.security.run``.

Like the quality runner, this executes in a credential-free job. Scanners that
are enabled in config but whose binary is not provisioned are reported as
``not_run`` rather than silently passing.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from typing import Any, Mapping, Sequence

from ..common.commands import CommandResult, run_commands, summarise, write_result
from ..common.config import Command, Target, load_repositories, resolve_target
from ..common.errors import AutomationError
from ..quality.run import build_runner, write_command_summary

#: tool -> (binary, probe, command). Commands are central; target config only
#: toggles them. The probe exists because a scanner exits non-zero both when it
#: finds secrets and when it is invoked wrongly — a CLI/version mismatch used to
#: be reported as a security failure. The probe separates the two.
SCANNERS: dict[str, tuple[str, str, str]] = {
    # --verbose prints each finding's file, line and rule. --redact keeps the
    # secret value itself out of the log and out of every artifact.
    "gitleaks": (
        "gitleaks",
        "gitleaks dir --help",
        "gitleaks dir . --no-banner --redact --verbose --exit-code 1",
    ),
    "semgrep": (
        "semgrep",
        "semgrep --version",
        "semgrep scan --error --quiet --config auto .",
    ),
    "trivy": (
        "trivy",
        "trivy --version",
        "trivy fs --quiet --exit-code 1 --scanners vuln,secret .",
    ),
}


#: Scanner output lines that state a finding count, so the report can say "4
#: leak(s)" instead of a bare "fail".
_COUNT_PATTERNS = (
    re.compile(r"leaks found:\s*(\d+)", re.IGNORECASE),
    re.compile(r"(\d+)\s+finding", re.IGNORECASE),
    re.compile(r"Total\s+detected\s+issues:\s*(\d+)", re.IGNORECASE),
)


def finding_count(output: str) -> int | None:
    """Extract a finding count from scanner output, or None when unstated."""
    for pattern in _COUNT_PATTERNS:
        match = pattern.search(output or "")
        if match:
            try:
                return int(match.group(1))
            except ValueError:  # pragma: no cover - guarded by the pattern
                continue
    return None


def annotate_counts(results: Sequence[CommandResult]) -> None:
    """Fold a scanner's finding count into its reason, in place."""
    for item in results:
        if item.status != "fail" or item.reason:
            continue
        count = finding_count(item.output_tail)
        if count is not None:
            item.reason = f"{count} finding(s) — see the job summary for file and rule"


def probe_scanners(
    commands: Sequence[Command], *, cwd: str, runner=None, timeout: int = 120
) -> tuple[list[Command], list[CommandResult]]:
    """Split scanners into ones whose CLI works and ones that are unusable.

    A scanner whose probe fails is recorded as ``error``, never as ``fail``: it
    found nothing, so it must not read as a security finding.
    """
    usable: list[Command] = []
    broken: list[CommandResult] = []
    for command in commands:
        entry = SCANNERS.get(command.name)
        if entry is None:
            usable.append(command)  # an extra command from config; nothing to probe
            continue
        _, probe, _ = entry
        results = run_commands(
            [Command(name=f"{command.name} probe", run=probe, group="security")],
            cwd=cwd,
            runner=runner,
            timeout=timeout,
        )
        if results and results[0].status == "pass":
            usable.append(command)
            continue
        tail = (results[0].output_tail if results else "").strip().splitlines()
        broken.append(
            CommandResult(
                name=command.name,
                group="security",
                command=command.run,
                status="error",
                reason=(
                    f"{command.name} does not support this invocation "
                    f"({probe!r} failed): {tail[0] if tail else 'no output'}"
                ),
            )
        )
    return usable, broken


def planned_scanners(target: Target, *, which=None) -> tuple[list[Command], list[CommandResult]]:
    """Split enabled scanners into runnable commands and not-run placeholders."""
    # Resolved at call time so the lookup can be substituted in tests.
    which = which or shutil.which
    config: Mapping[str, Any] = target.config.get("security") or {}
    runnable: list[Command] = []
    unavailable: list[CommandResult] = []
    for tool in sorted(SCANNERS):
        if not config.get(tool):
            continue
        binary, _probe, command = SCANNERS[tool]
        if which(binary) is None:
            unavailable.append(
                CommandResult(
                    name=tool,
                    group="security",
                    command=command,
                    status="not_run",
                    reason=f"{binary} is not installed on this runner.",
                )
            )
            continue
        runnable.append(Command(name=tool, run=command, group="security"))

    for index, extra in enumerate(config.get("commands") or [], start=1):
        if isinstance(extra, str):
            runnable.append(Command(name=f"Security {index}", run=extra, group="security"))
        elif isinstance(extra, dict) and isinstance(extra.get("run"), str):
            runnable.append(
                Command(name=str(extra.get("name") or f"Security {index}"),
                        run=extra["run"], group="security")
            )
    return runnable, unavailable


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    try:
        repository = env["TARGET_REPOSITORY"]
        config_file = env["CONFIG_FILE"]
        result_file = env["SECURITY_RESULT_FILE"]
    except KeyError as exc:
        print(f"::error::Missing required environment variable: {exc}", file=sys.stderr)
        return 2

    workdir = env.get("TARGET_WORKDIR") or "."
    try:
        target = resolve_target(
            repository,
            env={"TARGET_ORG": env.get("TARGET_ORG") or "validation-only"},
            repositories=load_repositories(config_file),
        )
        runnable, unavailable = planned_scanners(target)
    except AutomationError as exc:
        write_result(result_file, {"status": "error", "reason": str(exc), "results": []})
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if not runnable and not unavailable:
        write_result(
            result_file,
            {"status": "not_run", "reason": "No security scanners are enabled.", "results": []},
        )
        print("No security scanners enabled; reporting NOT RUN.")
        return 0

    # Scanners read the target tree, so they are sandboxed the same way.
    try:
        runner, sandbox_info = build_runner(target, workdir)
    except AutomationError as exc:
        write_result(result_file, {"status": "error", "reason": str(exc), "results": []})
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    # Verify each scanner's CLI before trusting its exit code.
    usable, broken = probe_scanners(runnable, cwd=workdir, runner=runner)
    results = run_commands(usable, cwd=workdir, runner=runner) + broken + unavailable
    annotate_counts(results)
    payload = summarise(results)
    payload["sandbox"] = sandbox_info
    # A scanner that could not run must not read as a pass.
    degraded = [r for r in results if r.status in ("not_run", "error")]
    if payload["status"] != "fail" and degraded:
        payload["status"] = "partial"
        payload["reason"] = "; ".join(
            r.reason or f"{r.name} did not run" for r in degraded
        )[:500]
    write_result(result_file, payload)

    for item in results:
        print(f"{item.name}: {item.status.upper()}" + (f" ({item.reason})" if item.reason else ""))
    print(f"Security: {payload['status'].upper()}")

    write_command_summary(
        "🔐 Security scanners", results, payload, sandbox_info, groups=("security",)
    )
    return 0 if payload["status"] in ("pass", "partial", "not_run") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
