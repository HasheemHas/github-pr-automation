"""Run configured security scanners against a checked-out target tree.

Run as ``python -m scripts.security.run``.

Like the quality runner, this executes in a credential-free job. Scanners that
are enabled in config but whose binary is not provisioned are reported as
``not_run`` rather than silently passing.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Any, Mapping

from ..common.commands import CommandResult, run_commands, summarise, write_result
from ..common.config import Command, Target, load_repositories, resolve_target
from ..common.errors import AutomationError
from ..quality.run import build_runner, write_command_summary

#: tool -> (binary, command). Commands are central; target config only toggles them.
SCANNERS: dict[str, tuple[str, str]] = {
    "gitleaks": ("gitleaks", "gitleaks dir . --no-banner --redact --exit-code 1"),
    "semgrep": ("semgrep", "semgrep scan --error --quiet --config auto ."),
    "trivy": ("trivy", "trivy fs --quiet --exit-code 1 --scanners vuln,secret ."),
}


def planned_scanners(target: Target, *, which=shutil.which) -> tuple[list[Command], list[CommandResult]]:
    """Split enabled scanners into runnable commands and not-run placeholders."""
    config: Mapping[str, Any] = target.config.get("security") or {}
    runnable: list[Command] = []
    unavailable: list[CommandResult] = []
    for tool in sorted(SCANNERS):
        if not config.get(tool):
            continue
        binary, command = SCANNERS[tool]
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

    results = run_commands(runnable, cwd=workdir, runner=runner) + unavailable
    payload = summarise(results)
    payload["sandbox"] = sandbox_info
    # A scanner that could not run must not read as a pass.
    if payload["status"] == "pass" and any(r.status == "not_run" for r in results):
        payload["status"] = "partial"
        payload["reason"] = "Some enabled scanners were not provisioned on the runner."
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
