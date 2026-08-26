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
    if repositories is None:
        # A defaults-only config is valid: every repository is then handled by
        # the default profile.
        return {}
    if not isinstance(repositories, dict):
        raise ConfigError(f"{path} must define 'repositories' as a mapping.")
    return repositories


def load_defaults(path: str | Path = DEFAULT_REPOSITORIES_CONFIG) -> dict[str, Any]:
    """Load the ``defaults`` block applied to repositories with no explicit entry."""
    data = load_yaml(path)
    defaults = data.get("defaults")
    if defaults is None:
        return {}
    if not isinstance(defaults, dict):
        raise ConfigError(f"{path} must define 'defaults' as a mapping.")
    return defaults


def _merge(defaults: Mapping[str, Any], explicit: Mapping[str, Any]) -> dict[str, Any]:
    """Shallow-merge an explicit entry over the defaults, one level into dicts.

    An explicit key always wins. A nested mapping (``security``, ``metadata``,
    ``sandbox``, ``review``) merges key by key so a repository can override just
    ``title_policy`` without restating the whole block.
    """
    merged: dict[str, Any] = dict(defaults)
    for key, value in explicit.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def resolve_target(
    repository: str,
    *,
    env: Mapping[str, str] | None = None,
    path: str | Path = DEFAULT_REPOSITORIES_CONFIG,
    repositories: Mapping[str, Any] | None = None,
    defaults: Mapping[str, Any] | None = None,
) -> Target:
    """Resolve ``repository`` against ``TARGET_ORG`` and the central config.

    Resolution order:
      1. an explicit ``repositories`` entry, merged over ``defaults``
      2. otherwise ``defaults`` alone, when ``defaults.enabled`` is true
      3. otherwise a hard failure naming the known repositories

    ``defaults.deny`` always wins, so a repository can be blocked without being
    configured. The GitHub App installation remains the real authorisation
    boundary; this config decides what the pipeline is willing to attempt.
    """
    name = (repository or "").strip()
    if not name:
        raise ConfigError("A target repository name is required.")
    if "/" in name:
        raise ConfigError("Pass the repository name without the owner prefix.")
    if not _REPO_PATTERN.match(name):
        raise ConfigError(f"Target repository {name!r} is not a valid repository name.")

    owner = target_org(env)
    # Only fall back to disk when the caller did not supply the config itself, so
    # an in-memory allowlist is never silently widened by an on-disk defaults block.
    if repositories is not None:
        allowlist = dict(repositories)
        default_profile = dict(defaults) if defaults is not None else {}
    else:
        allowlist = load_repositories(path)
        default_profile = dict(defaults) if defaults is not None else load_defaults(path)

    denied = default_profile.get("deny") or []
    if isinstance(denied, (list, tuple)) and any(
        str(item).strip().lower() == name.lower() for item in denied
    ):
        raise ConfigError(
            f"Target repository {name!r} is explicitly denied in config/repositories.yml."
        )

    entry = allowlist.get(name)
    if entry is not None and not isinstance(entry, dict):
        raise ConfigError(f"Allowlist entry for {name!r} must be a mapping.")

    if entry is None:
        if not default_profile.get("enabled"):
            known = ", ".join(sorted(allowlist)) or "none"
            raise ConfigError(
                f"Target repository {name!r} is not listed in the central allowlist "
                f"(known repositories: {known}). Add it, or set 'defaults.enabled: true' "
                "to allow any repository in the organisation."
            )
        config = _merge(default_profile, {})
        config["_source"] = "defaults"
    else:
        config = _merge(default_profile, entry)
        config["_source"] = "explicit"
        if not config.get("enabled"):
            raise ConfigError(
                f"Target repository {name!r} is present but not enabled. Install the "
                "GitHub App there, then set 'enabled: true'."
            )

    config.pop("deny", None)
    return Target(owner=owner, repo=name, config=config)


def has_explicit_commands(target: Target) -> bool:
    """True when the repository declares any command itself."""
    declared = target.config.get("commands")
    if not isinstance(declared, Mapping):
        return False
    return any(declared.get(group) for group in ("install", "lint", "test", "build"))


def detected_commands(detection: Mapping[str, Any] | None, group: str) -> list[Command]:
    """Build commands for one group from a detection payload."""
    entries = (detection or {}).get("commands") or []
    result: list[Command] = []
    for item in entries:
        if not isinstance(item, Mapping) or item.get("group") != group:
            continue
        run = item.get("run")
        if not isinstance(run, str) or not run.strip():
            continue
        result.append(
            Command(name=str(item.get("name") or f"{group} command"), run=run, group=group)
        )
    return result


def commands_for(
    target: Target,
    group: str,
    *,
    detection: Mapping[str, Any] | None = None,
) -> list[Command]:
    """Normalise the commands for one group (install/lint/test/build).

    Accepts either a plain string or a ``{name, run}`` mapping per entry. When the
    repository declares no commands at all, the detected ones are used instead, so
    an unconfigured repository still gets validated.
    """
    if detection is not None and not has_explicit_commands(target):
        return detected_commands(detection, group)
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
