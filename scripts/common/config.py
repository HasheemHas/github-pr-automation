"""Configuration loading, the target repository allowlist and org resolution."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .errors import ConfigError

TARGET_ORG_REQUIRED = "TARGET_ORG repository/organization variable is required."

# GitHub rejects custom secret and variable names beginning with GITHUB_, which is
# why this is TARGET_ORG and not GITHUB_ORG.
_ORG_PATTERN = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_REPO_PATTERN = re.compile(r"\A[A-Za-z0-9._-]{1,100}\Z")

DEFAULT_REPOSITORIES_CONFIG = "config/repositories.yml"
DEFAULT_REVIEWERS_CONFIG = "config/reviewers.yml"


@dataclass(frozen=True)
class Command:
    """One validation command declared centrally in ``config/repositories.yml``."""

    name: str
    run: str
    group: str


@dataclass(frozen=True)
class Target:
    """A resolved, allowlisted target repository."""

    owner: str
    repo: str
    config: Mapping[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Read a YAML mapping from disk."""
    file_path = Path(path)
    if not file_path.is_file():
        raise ConfigError(f"Configuration file not found: {file_path}")
    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - message passthrough
        raise ConfigError(f"Could not parse {file_path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{file_path} must contain a YAML mapping.")
    return data


def target_org(env: Mapping[str, str] | None = None) -> str:
    """Return the configured target organization.

    Raises:
        ConfigError: the ``TARGET_ORG`` variable is missing or malformed.
    """
    environ = os.environ if env is None else env
    value = (environ.get("TARGET_ORG") or "").strip()
    if not value:
        raise ConfigError(TARGET_ORG_REQUIRED)
    if not _ORG_PATTERN.match(value):
        raise ConfigError(f"TARGET_ORG {value!r} is not a valid GitHub owner name.")
    return value


def load_repositories(path: str | Path = DEFAULT_REPOSITORIES_CONFIG) -> dict[str, Any]:
    """Load the ``repositories`` mapping from the allowlist config."""
    data = load_yaml(path)
    repositories = data.get("repositories")
    if not isinstance(repositories, dict) or not repositories:
        raise ConfigError(f"{path} must define a non-empty 'repositories' mapping.")
    return repositories


def resolve_target(
    repository: str,
    *,
    env: Mapping[str, str] | None = None,
    path: str | Path = DEFAULT_REPOSITORIES_CONFIG,
    repositories: Mapping[str, Any] | None = None,
) -> Target:
    """Resolve ``repository`` against ``TARGET_ORG`` and the central allowlist."""
    name = (repository or "").strip()
    if not name:
        raise ConfigError("A target repository name is required.")
    if "/" in name:
        raise ConfigError("Pass the repository name without the owner prefix.")
    if not _REPO_PATTERN.match(name):
        raise ConfigError(f"Target repository {name!r} is not a valid repository name.")

    owner = target_org(env)
    allowlist = dict(repositories) if repositories is not None else load_repositories(path)
    entry = allowlist.get(name)
    if entry is None:
        known = ", ".join(sorted(allowlist)) or "none"
        raise ConfigError(
            f"Target repository {name!r} is not listed in the central allowlist "
            f"(known repositories: {known})."
        )
    if not isinstance(entry, dict):
        raise ConfigError(f"Allowlist entry for {name!r} must be a mapping.")
    if not entry.get("enabled"):
        raise ConfigError(
            f"Target repository {name!r} is present but not enabled. Install the "
            "GitHub App there, then set 'enabled: true'."
        )
    return Target(owner=owner, repo=name, config=entry)


def commands_for(target: Target, group: str) -> list[Command]:
    """Normalise the declared commands for one group (install/lint/test/build).

    Accepts either a plain string or a ``{name, run}`` mapping per entry.
    """
    declared = ((target.config.get("commands") or {}).get(group)) or []
    if not isinstance(declared, list):
        raise ConfigError(f"commands.{group} for {target.repo} must be a list.")
    result: list[Command] = []
    for index, item in enumerate(declared, start=1):
        if isinstance(item, str):
            result.append(Command(name=f"{group.title()} {index}", run=item, group=group))
        elif isinstance(item, dict) and isinstance(item.get("run"), str):
            name = str(item.get("name") or f"{group.title()} {index}")
            result.append(Command(name=name, run=item["run"], group=group))
        else:
            raise ConfigError(
                f"commands.{group}[{index}] for {target.repo} must be a string or a "
                "mapping with a 'run' key."
            )
    return result


def reviewer_groups(path: str | Path = DEFAULT_REVIEWERS_CONFIG) -> dict[str, Any]:
    """Load the ``reviewers`` mapping from the central reviewers config."""
    data = load_yaml(path)
    groups = data.get("reviewers")
    if groups is None:
        return {}
    if not isinstance(groups, dict):
        raise ConfigError(f"{path} must define a 'reviewers' mapping.")
    return groups


def artifact_dir(env: Mapping[str, str] | None = None) -> Path:
    """The single directory whose contents are allowed to become artifacts."""
    environ = os.environ if env is None else env
    base = environ.get("PR_AUTOMATION_ARTIFACT_DIR")
    if not base:
        runner_temp = environ.get("RUNNER_TEMP") or environ.get("TMPDIR") or "/tmp"
        base = str(Path(runner_temp) / "pr-automation-artifacts")
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path
