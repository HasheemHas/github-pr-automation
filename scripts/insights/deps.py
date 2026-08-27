"""Dependency-change risk.

A dependency bump is a code change authored by someone outside the review, so it
is reported as its own section rather than being buried among the source diffs.

What this reports, all of it read straight out of the diff:

  * which ecosystems changed, and whether manifest and lock file agree
  * added, removed and upgraded packages, with the version transition
  * major-version bumps and range widening, which are the two transitions that
    actually break builds
  * install-time script hooks, VCS/URL dependencies and pre-release pins, which
    are the supply-chain shaped changes worth a second look
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from ..common.globs import matches
from .base import Finding, as_bool, as_patterns, block, result, verdict
from .changes import FileChange

#: ecosystem -> (manifest patterns, lock file patterns)
ECOSYSTEMS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "npm": (
        ("**/package.json", "package.json"),
        ("**/package-lock.json", "**/npm-shrinkwrap.json", "**/yarn.lock", "**/pnpm-lock.yaml"),
    ),
    "python": (
        ("**/requirements*.txt", "**/pyproject.toml", "**/setup.py", "**/setup.cfg",
         "**/Pipfile", "**/constraints*.txt"),
        ("**/poetry.lock", "**/Pipfile.lock", "**/requirements*.lock", "**/uv.lock",
         "**/pdm.lock"),
    ),
    "php": (("**/composer.json",), ("**/composer.lock",)),
    "go": (("**/go.mod",), ("**/go.sum",)),
    "ruby": (("**/Gemfile", "**/*.gemspec"), ("**/Gemfile.lock",)),
    "rust": (("**/Cargo.toml",), ("**/Cargo.lock",)),
    "java": (("**/pom.xml", "**/build.gradle", "**/build.gradle.kts"),
             ("**/gradle.lockfile",)),
    "dotnet": (("**/*.csproj", "**/*.fsproj", "**/Directory.Packages.props"),
               ("**/packages.lock.json",)),
    "container": (("**/Dockerfile", "Dockerfile"), ()),
    "actions": ((".github/workflows/**", ".github/actions/**"), ()),
}

#: Version-bearing dependency lines, per ecosystem syntax.
_JSON_DEP = re.compile(
    r"""^\s*"(?P<name>[@A-Za-z0-9._/-]+)"\s*:\s*"(?P<version>[^"]*)"\s*,?\s*$"""
)
_REQUIREMENTS_DEP = re.compile(
    r"""^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*
        (?P<extras>\[[^\]]*\])?\s*
        (?P<op>==|>=|<=|~=|!=|>|<|===)?\s*
        (?P<version>[A-Za-z0-9][A-Za-z0-9._*+!-]*)?\s*
        (?:;.*)?$""",
    re.VERBOSE,
)
_GO_DEP = re.compile(r"^\s*(?P<name>[^\s]+/[^\s]+)\s+v(?P<version>[^\s]+)")
_GEM_DEP = re.compile(
    r"""^\s*gem\s+['"](?P<name>[^'"]+)['"]\s*(?:,\s*['"](?P<version>[^'"]+)['"])?"""
)
_TOML_DEP = re.compile(
    r"""^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*=\s*
        (?:['"](?P<version>[^'"]+)['"]|\{[^}]*version\s*=\s*['"](?P<version2>[^'"]+)['"])""",
    re.VERBOSE,
)
_DOCKER_FROM = re.compile(r"(?i)^\s*FROM\s+(?P<name>[^\s:@]+)(?::(?P<version>[^\s@]+))?")
_ACTION_USES = re.compile(r"^\s*-?\s*uses\s*:\s*(?P<name>[^@\s]+)@(?P<version>[^\s#]+)")

#: Install-time hooks: code that runs on every developer machine and CI runner.
_LIFECYCLE_HOOK = re.compile(
    r'"(preinstall|install|postinstall|prepare|prepublish|prepublishOnly)"\s*:'
)
#: Dependencies fetched from a VCS or arbitrary URL rather than a registry.
_VCS_SOURCE = re.compile(
    r"(?i)(git\+|git@|https?://(?!registry\.)|file:|ssh://|github:|gitlab:|bitbucket:)"
)
_PRERELEASE = re.compile(r"(?i)-(?:alpha|beta|rc|dev|pre|next|canary|nightly|snapshot)")

_SEMVER = re.compile(r"(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?")
#: Range operators ordered loosest first; a move down this list widens the range.
_RANGE_WIDTH = {"==": 0, "===": 0, "": 1, "=": 1, "~=": 2, "~": 2, "^": 3, ">=": 4, ">": 4, "*": 5}

DEFAULTS = {
    "enabled": True,
    "fail_on": "",              # advisory by default; set to high/critical to block
    "allow_major_bumps": True,
    "allow_vcs_sources": False,
    "max_reported": 60,
}


def ecosystems_touched(paths: Sequence[str]) -> dict[str, dict[str, list[str]]]:
    """Which ecosystems this change touches, split into manifests and lock files."""
    found: dict[str, dict[str, list[str]]] = {}
    for name, (manifests, locks) in ECOSYSTEMS.items():
        touched_manifests = [p for p in paths if any(matches(g, p) for g in manifests)]
        touched_locks = [p for p in paths if any(matches(g, p) for g in locks)]
        if touched_manifests or touched_locks:
            found[name] = {"manifests": sorted(touched_manifests), "locks": sorted(touched_locks)}
    return found


def _parse_dep_line(text: str, ecosystem: str) -> tuple[str, str, str] | None:
    """Return ``(name, operator, version)`` for a dependency line, or None.

    The operator is kept separate from the version so range widening — a real
    source of "worked yesterday, broken today" — can be detected.
    """
    line = text.strip()
    if not line or line.startswith(("#", "//", "*")):
        return None

    if ecosystem == "npm":
        match = _JSON_DEP.match(text)
        if not match:
            return None
        version = match.group("version").strip()
        operator = ""
        for prefix in ("^", "~", ">=", "<=", ">", "<", "="):
            if version.startswith(prefix):
                operator, version = prefix, version[len(prefix):].strip()
                break
        if version in ("*", "latest", "x"):
            operator = "*"
        return match.group("name"), operator, version

    if ecosystem == "python":
        if line.startswith("-"):
            return None
        match = _REQUIREMENTS_DEP.match(line.split("#", 1)[0])
        if match and match.group("name"):
            return (match.group("name"), match.group("op") or "",
                    (match.group("version") or "").strip())
        match = _TOML_DEP.match(text)
        if match:
            raw = match.group("version") or match.group("version2") or ""
            operator = ""
            for prefix in ("^", "~=", "~", ">=", "<=", ">", "<", "=="):
                if raw.startswith(prefix):
                    operator, raw = prefix, raw[len(prefix):].strip()
                    break
            return match.group("name"), operator, raw
        return None

    if ecosystem in ("php", "dotnet", "java"):
        match = _JSON_DEP.match(text)
        if match:
            version = match.group("version").strip()
            operator = ""
            for prefix in ("^", "~", ">=", "<=", ">", "<"):
                if version.startswith(prefix):
                    operator, version = prefix, version[len(prefix):].strip()
                    break
            return match.group("name"), operator, version
        # Maven / MSBuild attribute or element forms.
        attr = re.search(r'(?i)(?:Include|id)="(?P<name>[^"]+)"[^>]*Version="(?P<v>[^"]+)"', text)
        if attr:
            return attr.group("name"), "", attr.group("v")
        return None

    if ecosystem == "go":
        match = _GO_DEP.match(line)
        return (match.group("name"), "", match.group("version")) if match else None

    if ecosystem == "ruby":
        match = _GEM_DEP.match(line)
        if not match:
            return None
        raw = (match.group("version") or "").strip()
        operator = ""
        for prefix in ("~>", ">=", "<=", ">", "<", "="):
            if raw.startswith(prefix):
                operator, raw = prefix, raw[len(prefix):].strip()
                break
        return match.group("name"), operator, raw

    if ecosystem == "rust":
        match = _TOML_DEP.match(text)
        if not match:
            return None
        raw = (match.group("version") or match.group("version2") or "").strip()
        operator = "^" if raw and raw[0].isdigit() else ""
        return match.group("name"), operator, raw.lstrip("^~>=< ")

    if ecosystem == "container":
        match = _DOCKER_FROM.match(line)
        if not match:
            return None
        return match.group("name"), "", (match.group("version") or "").strip()

    if ecosystem == "actions":
        match = _ACTION_USES.match(text)
        return (match.group("name"), "", match.group("version")) if match else None

    return None


def _major(version: str) -> int | None:
    match = _SEMVER.search(version or "")
    if not match:
        return None
    try:
        return int(match.group("major"))
    except (TypeError, ValueError):  # pragma: no cover - guarded by the pattern
        return None


def _range_width(operator: str) -> int:
    return _RANGE_WIDTH.get(operator, 1)


def diff_dependencies(change: FileChange, ecosystem: str) -> dict[str, Any]:
    """Compare the added and removed dependency lines of one manifest."""
    added: dict[str, tuple[str, str]] = {}
    removed: dict[str, tuple[str, str]] = {}
    for line in change.added:
        parsed = _parse_dep_line(line.text, ecosystem)
        if parsed:
            added[parsed[0]] = (parsed[1], parsed[2])
    for line in change.removed:
        parsed = _parse_dep_line(line.text, ecosystem)
        if parsed:
            removed[parsed[0]] = (parsed[1], parsed[2])

    changed: list[dict[str, Any]] = []
    for name, (operator, version) in sorted(added.items()):
        if name in removed:
            old_operator, old_version = removed[name]
            if (old_operator, old_version) == (operator, version):
                continue
            old_major, new_major = _major(old_version), _major(version)
            changed.append({
                "name": name,
                "kind": "changed",
                "from": f"{old_operator}{old_version}".strip(),
                "to": f"{operator}{version}".strip(),
                "major_bump": (
                    old_major is not None and new_major is not None and new_major > old_major
                ),
                "downgrade": (
                    old_major is not None and new_major is not None and new_major < old_major
                ),
                "range_widened": _range_width(operator) > _range_width(old_operator),
                "prerelease": bool(_PRERELEASE.search(version)),
            })
        else:
            changed.append({
                "name": name, "kind": "added", "from": "",
                "to": f"{operator}{version}".strip(),
                "major_bump": False, "downgrade": False,
                "range_widened": _range_width(operator) >= 4,
                "prerelease": bool(_PRERELEASE.search(version)),
            })
    for name, (operator, version) in sorted(removed.items()):
        if name not in added:
            changed.append({
                "name": name, "kind": "removed",
                "from": f"{operator}{version}".strip(), "to": "",
                "major_bump": False, "downgrade": False,
                "range_widened": False, "prerelease": False,
            })
    return {"path": change.path, "ecosystem": ecosystem, "packages": changed}


def evaluate(
    changes: Mapping[str, FileChange],
    context: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Report every dependency change in the pull request, with risk notes."""
    policy = {**DEFAULTS, **block(config, "dependency_policy")}
    if not as_bool(policy.get("enabled"), True):
        return result("not_run", "Dependency review is disabled for this repository.")

    paths = [str(p) for p in (context or {}).get("files") or []] or sorted(changes)
    touched = ecosystems_touched(paths)
    if not touched:
        return result("pass", "No dependency manifests or lock files changed.", (),
                      ecosystems={}, packages=[])

    allow_list = as_patterns(policy.get("allow_packages"))
    findings: list[Finding] = []
    reports: list[dict[str, Any]] = []

    for ecosystem, files in sorted(touched.items()):
        manifest_changes = [changes[p] for p in files["manifests"] if p in changes]
        for change in manifest_changes:
            report = diff_dependencies(change, ecosystem)
            if report["packages"]:
                reports.append(report)

        # Manifest and lock file must move together, or the install is not
        # reproducible: one machine resolves the new version, another the old.
        has_locks = bool(ECOSYSTEMS[ecosystem][1])
        if has_locks and files["manifests"] and not files["locks"]:
            findings.append(Finding(
                "medium", "deps-manifest-without-lock",
                f"{ecosystem}: manifest changed without its lock file",
                f"Changed {', '.join(f'`{p}`' for p in files['manifests'][:5])} but no lock file. "
                "Run the install command and commit the lock file so every environment "
                "resolves the same versions.",
                path=files["manifests"][0],
            ))
        if has_locks and files["locks"] and not files["manifests"]:
            findings.append(Finding(
                "low", "deps-lock-without-manifest",
                f"{ecosystem}: lock file changed without its manifest",
                f"Changed {', '.join(f'`{p}`' for p in files['locks'][:5])} only. This is normal "
                "for a transitive security update; confirm it was intentional and not a stray "
                "local install.",
                path=files["locks"][0],
            ))

        for change in manifest_changes:
            hooks = [line for line in change.added if _LIFECYCLE_HOOK.search(line.text)]
            for line in hooks[:5]:
                findings.append(Finding(
                    "high", "deps-install-hook",
                    "Install-time script hook added",
                    "A lifecycle hook runs automatically on every install, on developer "
                    "machines and on CI. Confirm what it executes.",
                    path=change.path, line=line.line,
                ))
            if not as_bool(policy.get("allow_vcs_sources"), False):
                for line in change.added:
                    parsed = _parse_dep_line(line.text, ecosystem)
                    if parsed and _VCS_SOURCE.search(parsed[2]):
                        findings.append(Finding(
                            "high", "deps-vcs-source",
                            f"`{parsed[0]}` is installed from a VCS or URL",
                            "A non-registry source is not immutable: the referenced ref can be "
                            "moved or deleted. Pin to a published release, or to a full commit "
                            "SHA if the source must stay.",
                            path=change.path, line=line.line,
                        ))

    packages = [
        {**pkg, "path": report["path"], "ecosystem": report["ecosystem"]}
        for report in reports
        for pkg in report["packages"]
    ]
    reportable = [
        pkg for pkg in packages
        if not (allow_list and any(matches(pattern, pkg["name"]) for pattern in allow_list))
    ]

    major = [p for p in reportable if p["major_bump"]]
    downgrades = [p for p in reportable if p["downgrade"]]
    widened = [p for p in reportable if p["range_widened"]]
    prereleases = [p for p in reportable if p["prerelease"]]
    added_pkgs = [p for p in reportable if p["kind"] == "added"]
    removed_pkgs = [p for p in reportable if p["kind"] == "removed"]

    if major:
        findings.append(Finding(
            "high" if not as_bool(policy.get("allow_major_bumps"), True) else "medium",
            "deps-major-bump",
            f"{len(major)} major version bump(s)",
            "; ".join(f"`{p['name']}` {p['from'] or '?'} → {p['to']}" for p in major[:10])
            + ". A major bump is a breaking change by definition; check the upgrade notes and "
            "confirm the test suite exercises the affected paths.",
        ))
    if downgrades:
        findings.append(Finding(
            "medium", "deps-downgrade",
            f"{len(downgrades)} package(s) downgraded",
            "; ".join(f"`{p['name']}` {p['from']} → {p['to']}" for p in downgrades[:10])
            + ". A downgrade often reintroduces a fixed vulnerability; confirm it is deliberate.",
        ))
    if widened:
        findings.append(Finding(
            "medium", "deps-range-widened",
            f"{len(widened)} dependency range(s) widened or unpinned",
            "; ".join(f"`{p['name']}` → {p['to'] or 'any'}" for p in widened[:10])
            + ". A looser range means the next install can pull a version nobody reviewed.",
        ))
    if prereleases:
        findings.append(Finding(
            "medium", "deps-prerelease",
            f"{len(prereleases)} pre-release version(s) pinned",
            "; ".join(f"`{p['name']}` {p['to']}" for p in prereleases[:10])
            + ". Pre-releases can be republished or withdrawn; avoid them on a release branch.",
        ))
    if added_pkgs:
        findings.append(Finding(
            "low", "deps-added",
            f"{len(added_pkgs)} new dependency(ies)",
            "; ".join(f"`{p['name']}` {p['to']}" for p in added_pkgs[:15])
            + ". Each new dependency adds maintenance surface, transitive dependencies and a "
            "licence obligation.",
        ))
    if removed_pkgs:
        findings.append(Finding(
            "low", "deps-removed",
            f"{len(removed_pkgs)} dependency(ies) removed",
            "; ".join(f"`{p['name']}`" for p in removed_pkgs[:15])
            + ". Confirm nothing still imports them.",
        ))

    fail_on = str(policy.get("fail_on") or "").strip().lower()
    status = verdict(findings, fail_at=fail_on, warn_at="low")
    max_reported = int(policy.get("max_reported") or DEFAULTS["max_reported"])

    return result(
        status,
        (f"{len(reportable)} dependency change(s) across "
         f"{', '.join(sorted(touched))}" if reportable
         else f"Dependency files changed in {', '.join(sorted(touched))}; no version changes parsed"),
        findings,
        ecosystems=touched,
        packages=reportable[:max_reported],
        dropped=max(0, len(reportable) - max_reported),
        totals={
            "added": len(added_pkgs), "removed": len(removed_pkgs),
            "changed": len([p for p in reportable if p["kind"] == "changed"]),
            "major_bumps": len(major), "downgrades": len(downgrades),
            "range_widened": len(widened), "prereleases": len(prereleases),
        },
    )
