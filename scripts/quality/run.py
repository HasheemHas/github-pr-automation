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
from ..common import summary
from ..common.commands import run_commands, summarise, write_result
from ..common.config import (
    Target,
    commands_for,
    has_explicit_commands,
    load_defaults,
    load_repositories,
    resolve_target,
)
from ..common.errors import AutomationError

GROUPS = ("install", "lint", "test", "build")


def load_detection(env=None) -> dict | None:
    """Read ``detected-commands.json`` from the artifact directory, if present."""
    environ = os.environ if env is None else env
    path = Path(environ.get("DETECTED_COMMANDS_FILE") or "")
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def collect(target: Target, detection: dict | None = None) -> list:
    """Flatten the command groups into a single ordered list.

    Explicit config wins. When the repository declares no commands, the detected
    ones are used so an unconfigured repository is still validated.
    """
    commands: list = []
    for group in GROUPS:
        commands.extend(commands_for(target, group, detection=detection))
    return commands


def write_command_summary(title, results, payload, sandbox_info, *, groups=GROUPS) -> None:
    """Render a command-by-command result table into the Actions job summary.

    Shared by the quality and security runners so both read the same way.
    """
    status = str(payload.get("status", "not_run"))
    blocks = [
        summary.heading(f"{title} — {summary.icon(status)} {status.upper()}"),
        summary.table(
            ["Check", "Group", "Result", "Exit", "Note"],
            [
                [
                    item.name,
                    item.group,
                    f"{summary.icon(item.status)} {item.status}",
                    "—" if item.exit_code is None else item.exit_code,
                    item.reason or "",
                ]
                for item in results
            ],
        ),
    ]
    if payload.get("reason"):
        blocks.append(f"> [!NOTE]\n> {payload['reason']}")
    blocks.append(
        summary.definitions(
            {
                "Execution": (
                    f"container `{sandbox_info['image']}`" if sandbox_info.get("ready")
                    else f"host runner — {sandbox_info.get('reason', 'no sandbox')}"
                ),
                "Credentials in this job": "none",
            }
        )
    )
    # Failing commands get their output tail, so the run page explains the failure.
    for item in results:
        if item.status == "fail" and item.output_tail:
            blocks.append(
                summary.details(
                    f"Output — {item.name}", f"```text\n{item.output_tail[-3000:]}\n```"
                )
            )
    summary.write(*blocks)


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
            defaults=load_defaults(config_file),
        )
        detection = load_detection(env)
        commands = collect(target, detection)
        source = "explicit config" if has_explicit_commands(target) else "auto-detected"
    except AutomationError as exc:
        write_result(result_file, {"status": "error", "reason": str(exc), "results": []})
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if not commands:
        write_result(
            result_file,
            {
                "status": "not_run",
                "reason": (
                    "No quality commands are configured and none could be detected "
                    "from the repository's manifests."
                ),
                "results": [],
            },
        )
        print("No quality commands configured or detected; reporting NOT RUN.")
        return 0
    print(f"Commands from: {source} ({len(commands)} command(s))")

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

    write_command_summary(
        "🔍 Quality validation", results, payload, sandbox_info, groups=GROUPS
    )
    # A non-zero exit records the failure for this job; the report job reads the
    # JSON, so a failure here never hides the detail.
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
