"""Run centrally configured quality commands against a checked-out target tree.

Run as ``python -m scripts.quality.run``.

This module is designed to run in a job with NO credentials of any kind: no
GitHub App token, no NVIDIA key, no registry or cloud credentials. It executes
target-repository-controlled code (``npm ci`` runs lifecycle scripts), so job
isolation — not this script — is the security boundary.

The result file is always written, including when dependency installation fails.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ..common import sandbox as sandbox_module
from ..common.commands import run_commands, summarise, write_result
from ..common.config import Target, commands_for, load_repositories, resolve_target
from ..common.errors import AutomationError

GROUPS = ("install", "lint", "test", "build")


def collect(target: Target) -> list:
    """Flatten the configured command groups into a single ordered list."""
    commands: list = []
    for group in GROUPS:
        commands.extend(commands_for(target, group))
    return commands


def build_runner(target: Target, workdir: str, *, require_sandbox: bool = False):
    """Return ``(runner, sandbox_description)`` for executing target commands.

    When a sandbox is configured, target commands run inside a container with only
    the target tree mounted, so they cannot reach the central scripts or the
    artifact directory. ``require_sandbox`` makes an unusable sandbox fatal, which
    is what the patch pipelines need.
    """
    config = sandbox_module.load(target.config)
    if not config.usable:
        reason = "Sandbox is not enabled or no image is configured."
        if require_sandbox:
            raise AutomationError(
                "This action requires sandboxed execution of target commands. " + reason
            )
        return None, sandbox_module.describe(config, False, reason)

    ready, reason = sandbox_module.preflight(config)
    if not ready:
        if require_sandbox:
            raise AutomationError("Sandbox preflight failed: " + reason)
        print(f"::warning::{reason} Falling back to host execution.")
        return None, sandbox_module.describe(config, False, reason)
    return (
        sandbox_module.make_runner(config, workdir),
        sandbox_module.describe(config, True, reason),
    )


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    try:
        repository = env["TARGET_REPOSITORY"]
        config_file = env["CONFIG_FILE"]
        result_file = env["QUALITY_RESULT_FILE"]
    except KeyError as exc:
        print(f"::error::Missing required environment variable: {exc}", file=sys.stderr)
        return 2

    workdir = env.get("TARGET_WORKDIR") or "."
    try:
        # The org variable is irrelevant here; only the command config matters, so
        # allow this job to run without TARGET_ORG.
        repositories = load_repositories(config_file)
        target = resolve_target(
            repository,
            env={"TARGET_ORG": env.get("TARGET_ORG") or "validation-only"},
            repositories=repositories,
        )
        commands = collect(target)
    except AutomationError as exc:
        write_result(result_file, {"status": "error", "reason": str(exc), "results": []})
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if not commands:
        write_result(
            result_file,
            {"status": "not_run", "reason": "No quality commands are configured.", "results": []},
        )
        print("No quality commands configured; reporting NOT RUN.")
        return 0

    if not Path(workdir).is_dir():
        write_result(
            result_file,
            {"status": "error", "reason": f"Target working directory {workdir!r} is missing.",
             "results": []},
        )
        print(f"::error::Target working directory {workdir!r} is missing.", file=sys.stderr)
        return 1

    try:
        runner, sandbox_info = build_runner(target, workdir)
    except AutomationError as exc:
        write_result(result_file, {"status": "error", "reason": str(exc), "results": []})
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    results = run_commands(commands, cwd=workdir, runner=runner)
    payload = summarise(results)
    payload["sandbox"] = sandbox_info
    write_result(result_file, payload)

    for item in results:
        print(f"{item.name}: {item.status.upper()}")
    print(f"Sandbox: {'container ' + sandbox_info['image'] if sandbox_info['ready'] else 'host'}")
    print(f"Quality: {payload['status'].upper()}")
    # A non-zero exit records the failure for this job; the report job reads the
    # JSON, so a failure here never hides the detail.
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
