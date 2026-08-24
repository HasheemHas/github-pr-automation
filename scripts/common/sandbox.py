"""Run target-controlled commands inside a container.

The credential-free validation job already keeps secrets away from target code.
This adds integrity isolation: target commands cannot reach the central scripts,
the artifact directory, the runner's temp tree, or the Docker socket, so they
cannot tamper with the validation result that decides whether a patch is pushed.

Only the target working tree is mounted. The trusted host process collects exit
codes and writes the result artifact, so the artifact is never writable from
inside the sandbox.
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

WORKDIR = "/workspace"

#: Environment handed to the container. Nothing is inherited from the runner.
CONTAINER_ENV = {
    "CI": "true",
    "HOME": WORKDIR,
    "GIT_TERMINAL_PROMPT": "0",
    "NPM_CONFIG_FUND": "false",
    "NPM_CONFIG_AUDIT": "false",
    "NPM_CONFIG_UPDATE_NOTIFIER": "false",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}

#: Binaries a target image must provide before we trust its results.
DEFAULT_REQUIRED_BINARIES = ("sh",)

DEFAULT_MEMORY = "4g"
DEFAULT_PIDS = 512
#: Groups allowed network access when network isolation is enabled.
DEFAULT_NETWORK_GROUPS = ("install",)


@dataclass(frozen=True)
class SandboxConfig:
    enabled: bool = False
    image: str = ""
    required_binaries: tuple[str, ...] = DEFAULT_REQUIRED_BINARIES
    isolate_network: bool = False
    network_groups: tuple[str, ...] = DEFAULT_NETWORK_GROUPS
    memory: str = DEFAULT_MEMORY
    pids_limit: int = DEFAULT_PIDS
    runtime: str = "docker"

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.image)


def load(config: Mapping[str, Any] | None) -> SandboxConfig:
    """Read the ``sandbox`` block from a repository's central config entry."""
    raw = (config or {}).get("sandbox") or {}
    if not isinstance(raw, Mapping):
        raise ConfigError("sandbox must be a mapping.")
    image = str(raw.get("image") or "").strip()
    enabled = bool(raw.get("enabled", False))
    if enabled and not image:
        raise ConfigError("sandbox.enabled is true but sandbox.image is not set.")
    binaries = raw.get("required_binaries") or DEFAULT_REQUIRED_BINARIES
    if not isinstance(binaries, (list, tuple)):
        raise ConfigError("sandbox.required_binaries must be a list.")
    groups = raw.get("network_groups") or DEFAULT_NETWORK_GROUPS
    if not isinstance(groups, (list, tuple)):
        raise ConfigError("sandbox.network_groups must be a list.")
    return SandboxConfig(
        enabled=enabled,
        image=image,
        required_binaries=tuple(str(b) for b in binaries),
        isolate_network=bool(raw.get("isolate_network", False)),
        network_groups=tuple(str(g) for g in groups),
        memory=str(raw.get("memory") or DEFAULT_MEMORY),
        pids_limit=int(raw.get("pids_limit") or DEFAULT_PIDS),
        runtime=str(raw.get("runtime") or "docker"),
    )


def docker_args(config: SandboxConfig, host_dir: Path, *, group: str = "",
                network: bool | None = None) -> list[str]:
    """Build the ``docker run`` argument list for one command.

    Deliberately absent: any mount other than the target tree, the Docker socket,
    added capabilities, and privileged mode.
    """
    if network is None:
        network = (not config.isolate_network) or group in config.network_groups

    args = [
        config.runtime, "run", "--rm",
        # Only the target tree is visible.
        "--volume", f"{host_dir.resolve()}:{WORKDIR}",
        "--workdir", WORKDIR,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(config.pids_limit),
        "--memory", config.memory,
        # Never inherit the runner's environment.
        "--env-file", "/dev/null",
    ]
    if not network:
        args += ["--network", "none"]
    for key, value in CONTAINER_ENV.items():
        args += ["--env", f"{key}={value}"]
    args.append(config.image)
    return args


def preflight(config: SandboxConfig, *, runner=subprocess.run) -> tuple[bool, str]:
    """Verify the runtime works and the image provides the required binaries."""
    if not config.usable:
        return False, "Sandbox is not enabled or no image is configured."

    probe = runner(
        [config.runtime, "version", "--format", "{{.Server.Version}}"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        return False, f"{config.runtime} is not available on this runner."

    check = " && ".join(f"command -v {shlex.quote(b)} >/dev/null" for b in config.required_binaries)
    result = runner(
        [config.runtime, "run", "--rm", "--network", "none", config.image, "sh", "-lc", check],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        missing = ", ".join(config.required_binaries)
        return False, (
            f"Image {config.image} does not provide every required binary "
            f"({missing}); validation results from it would not be trustworthy."
        )
    return True, f"Sandbox ready: {config.image}"


def make_runner(config: SandboxConfig, host_dir: str | Path, *, runner=subprocess.run):
    """Build a runner compatible with :func:`scripts.common.commands.run_commands`.

    The ``cwd`` and ``env`` the caller passes are ignored on purpose: the command
    always runs at ``/workspace`` inside the container with a fixed environment.
    """
    directory = Path(host_dir)

    def run(command: str, _cwd, _env, timeout: int) -> tuple[int, str]:
        group = getattr(run, "group", "")
        args = docker_args(config, directory, group=group)
        args += ["sh", "-lc", command]
        completed = runner(args, capture_output=True, text=True, timeout=timeout)
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

    run.sandbox = config  # type: ignore[attr-defined]
    return run


def describe(config: SandboxConfig, ready: bool, reason: str) -> dict[str, Any]:
    """The sandbox block recorded in every validation result artifact."""
    return {
        "enabled": config.enabled,
        "image": config.image,
        "ready": ready,
        "reason": reason,
        "isolate_network": config.isolate_network,
        "network_groups": list(config.network_groups),
    }
