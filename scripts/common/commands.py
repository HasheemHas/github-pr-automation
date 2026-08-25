"""Run centrally declared validation commands and always produce a result file.

Design rules that this module exists to enforce:

* A dependency install failure is a reported result, never an aborted script. The
  result file is written even when the very first command fails.
* Once a required group fails, later groups are recorded as ``skipped`` with a
  reason instead of vanishing from the report.
* Command output is truncated and never echoed into the result wholesale, so a
  noisy build cannot bloat or poison the downstream report.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .config import Command

#: Groups whose failure makes every later group meaningless.
BLOCKING_GROUPS = frozenset({"install"})

OUTPUT_TAIL_BYTES = 4000


@dataclass
class CommandResult:
    name: str
    group: str
    command: str
    status: str  # pass | fail | skipped | not_run
    exit_code: int | None = None
    reason: str = ""
    output_tail: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "pass"


def _tail(text: str) -> str:
    if len(text) <= OUTPUT_TAIL_BYTES:
        return text
    return "...\n" + text[-OUTPUT_TAIL_BYTES:]


def _sanitised_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A minimal environment for untrusted target commands.

    Every credential-shaped variable is stripped. This is defence in depth: the
    validation job is not given any credentials in the first place.
    """
    blocked_exact = {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GH_APP_ID",
        "GH_APP_PRIVATE_KEY",
        "NVIDIA_API_KEY",
        "OPENAI_API_KEY",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_RESULTS_URL",
        "ACTIONS_CACHE_URL",
    }
    blocked_suffixes = ("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY", "_PRIVATE_KEY", "_CREDENTIALS")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in blocked_exact and not key.upper().endswith(blocked_suffixes)
    }
    env["CI"] = "true"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


def run_commands(
    commands: Sequence[Command],
    *,
    cwd: str | Path | None = None,
    timeout: int = 1800,
    runner=None,
    env_extra: Mapping[str, str] | None = None,
) -> list[CommandResult]:
    """Run commands in order, recording every one of them.

    ``runner`` is injectable for tests; it receives ``(command, cwd, env, timeout)``
    and returns ``(exit_code, combined_output)``.
    """
    execute = runner or _subprocess_runner
    env = _sanitised_env(env_extra)
    results: list[CommandResult] = []
    blocked_by: str | None = None

    for command in commands:
        if blocked_by:
            results.append(
                CommandResult(
                    name=command.name,
                    group=command.group,
                    command=command.run,
                    status="skipped",
                    reason=f"Skipped because {blocked_by} failed.",
                )
            )
            continue
        try:
            # Sandbox runners read this to apply a per-group network policy.
            try:
                setattr(execute, "group", command.group)
            except (AttributeError, TypeError):  # builtins and bound methods
                pass
            code, output = execute(command.run, cwd, env, timeout)
        except subprocess.TimeoutExpired:
            code, output = 124, f"Timed out after {timeout}s."
        results.append(
            CommandResult(
                name=command.name,
                group=command.group,
                command=command.run,
                status="pass" if code == 0 else "fail",
                exit_code=code,
                output_tail=_tail(output),
            )
        )
        if code != 0 and command.group in BLOCKING_GROUPS:
            blocked_by = command.name

    return results


def _subprocess_runner(command: str, cwd, env, timeout: int) -> tuple[int, str]:
    completed = subprocess.run(  # noqa: S602 - commands come from central config only
        command,
        shell=True,
        cwd=cwd,
        env=env,
        timeout=timeout,
        capture_output=True,
        text=True,
        errors="replace",
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def summarise(results: Iterable[CommandResult]) -> dict[str, object]:
    """Build the JSON payload the report job consumes."""
    items = list(results)
    if not items:
        return {"status": "not_run", "reason": "No commands are configured.", "results": []}
    failed = [item for item in items if item.status == "fail"]
    skipped = [item for item in items if item.status == "skipped"]
    # An 'error' result is a tool that could not run. It must not be silently
    # counted as a pass, but it is not a failing check either.
    errored = [item for item in items if item.status == "error"]
    if failed:
        status = "fail"
    elif errored or skipped:
        status = "partial"
    else:
        status = "pass"
    return {
        "status": status,
        "counts": {
            "total": len(items),
            "passed": sum(1 for item in items if item.status == "pass"),
            "failed": len(failed),
            "skipped": len(skipped),
            "errored": len(errored),
        },
        "results": [asdict(item) for item in items],
    }


def write_result(path: str | Path, payload: Mapping[str, object]) -> None:
    """Write a result file atomically enough for artifact upload."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
