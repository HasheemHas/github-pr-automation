"""Pull request size and risk policy.

Review quality collapses well before a pull request becomes unreviewable, so this
reports on three separate things a reviewer cares about:

  * volume — how many files and lines, excluding generated ones
  * spread — how many distinct concerns the change touches at once
  * risk — whether high-blast-radius paths are in the same change as ordinary code

Generated files (lock files, vendored trees, snapshots, minified bundles) are
counted separately rather than dropped, because "1200 lines, 1150 of them a lock
file" and "1200 lines of hand-written code" deserve opposite reactions.

Configure per repository under ``size_policy`` in ``config/repositories.yml``.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..common.globs import matches
from .base import Finding, as_bool, as_int, as_patterns, block, result, verdict
from .changes import file_stats

#: Files whose size says nothing about review effort.
DEFAULT_GENERATED = (
    "**/package-lock.json",
    "**/yarn.lock",
    "**/pnpm-lock.yaml",
    "**/npm-shrinkwrap.json",
    "**/poetry.lock",
    "**/Pipfile.lock",
    "**/composer.lock",
    "**/Gemfile.lock",
    "**/Cargo.lock",
    "**/go.sum",
    "**/*.lock",
    "vendor/**",
    "**/vendor/**",
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
    "**/__snapshots__/**",
    "**/*.snap",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
    "**/*.pb.go",
    "**/*_pb2.py",
    "**/*.generated.*",
    "**/migrations/**",
)

#: Paths whose change carries blast radius beyond the diff itself.
DEFAULT_SENSITIVE: dict[str, tuple[str, ...]] = {
    "ci/cd workflows": (".github/workflows/**", ".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml"),
    "infrastructure": ("terraform/**", "**/*.tf", "helm/**", "**/Chart.yaml", "k8s/**", "**/*.tfvars"),
    "container images": ("**/Dockerfile", "Dockerfile", "docker-compose.*", "**/docker-compose.*"),
    "authentication": ("**/auth/**", "**/authentication/**", "**/permissions/**", "security/**"),
    "database migrations": ("**/migrations/**", "**/migrate/**", "db/**"),
    "dependency manifests": (
        "**/package.json", "**/requirements*.txt", "**/pyproject.toml",
        "**/go.mod", "**/composer.json", "**/Gemfile", "**/pom.xml", "**/Cargo.toml",
    ),
    "secrets and keys": ("**/*.pem", "**/*.key", "**/.env*", "**/secrets/**"),
}

#: Concern -> patterns. Used to measure how many unrelated things one PR mixes.
CONCERNS: dict[str, tuple[str, ...]] = {
    "backend": ("backend/**", "server/**", "api/**", "app/**", "src/main/**"),
    "frontend": ("frontend/**", "admin/**", "web/**", "client/**", "ui/**"),
    "mobile": ("android/**", "ios/**", "mobile/**"),
    "infrastructure": ("terraform/**", "**/*.tf", "helm/**", "k8s/**", "deploy/**"),
    "ci": (".github/**", ".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml"),
    "database": ("**/migrations/**", "**/migrate/**", "db/**", "**/*.sql"),
    "dependencies": (
        "**/package.json", "**/package-lock.json", "**/requirements*.txt",
        "**/pyproject.toml", "**/poetry.lock", "**/go.mod", "**/go.sum",
        "**/composer.json", "**/composer.lock", "**/Gemfile*", "**/Cargo.*",
    ),
    "documentation": ("**/*.md", "**/*.rst", "docs/**", "**/*.txt"),
    "tests": (
        "tests/**", "test/**", "**/tests/**", "**/test/**", "__tests__/**",
        "**/*_test.*", "**/test_*.*", "**/*.test.*", "**/*.spec.*",
    ),
    "configuration": ("**/*.yml", "**/*.yaml", "**/*.toml", "**/*.ini", "**/*.cfg"),
}

#: Concerns that legitimately accompany any change, so they never count as "mixed".
INCIDENTAL_CONCERNS = frozenset({"tests", "documentation", "dependencies", "configuration"})

DEFAULTS = {
    "max_files": 60,
    "max_lines": 1500,
    "warn_files": 20,
    "warn_lines": 500,
    "max_commits": 0,          # 0 disables the check
    "max_concerns": 2,         # substantive concerns before "mixed change" is reported
    "fail_on_violation": False,
    "count_generated": False,
}


def classify_concerns(paths: Sequence[str]) -> dict[str, list[str]]:
    """Group changed paths by concern. A path may belong to more than one."""
    grouped: dict[str, list[str]] = {}
    for path in paths:
        for concern, patterns in CONCERNS.items():
            if any(matches(pattern, path) for pattern in patterns):
                grouped.setdefault(concern, []).append(path)
    unclassified = [
        path for path in paths
        if not any(path in members for members in grouped.values())
    ]
    if unclassified:
        grouped["other"] = unclassified
    return grouped


def sensitive_areas(paths: Sequence[str], extra: Mapping[str, Sequence[str]] | None = None
                    ) -> dict[str, list[str]]:
    """Which high-blast-radius areas this change touches, and via which files."""
    catalogue: dict[str, Sequence[str]] = {**DEFAULT_SENSITIVE, **(extra or {})}
    hits: dict[str, list[str]] = {}
    for area, patterns in catalogue.items():
        touched = [p for p in paths if any(matches(pattern, p) for pattern in patterns)]
        if touched:
            hits[area] = touched
    return hits


def _generated(path: str, patterns: Sequence[str]) -> bool:
    return any(matches(pattern, path) for pattern in patterns)


def measure(
    context: Mapping[str, Any],
    *,
    generated_patterns: Sequence[str] = DEFAULT_GENERATED,
    ignore_patterns: Sequence[str] = (),
) -> dict[str, Any]:
    """Compute the volume metrics, splitting generated from hand-written files.

    GitHub's own per-file counts are used when present so a truncated diff does
    not understate the size of the change.
    """
    paths = [str(p) for p in context.get("files") or []]
    stats = file_stats(context)
    kept: list[str] = []
    ignored: list[str] = []
    generated: list[str] = []
    reviewable_lines = generated_lines = 0

    for path in paths:
        entry = stats.get(path) or {}
        lines = as_int(entry.get("additions"), 0) + as_int(entry.get("deletions"), 0)
        if ignore_patterns and _generated(path, ignore_patterns):
            ignored.append(path)
            continue
        if _generated(path, generated_patterns):
            generated.append(path)
            generated_lines += lines
            continue
        kept.append(path)
        reviewable_lines += lines

    # Fall back to the PR-level totals when per-file stats are unavailable.
    if not stats:
        reviewable_lines = as_int(context.get("additions"), 0) + as_int(context.get("deletions"), 0)

    return {
        "files_total": len(paths),
        "files_reviewable": len(kept),
        "files_generated": len(generated),
        "files_ignored": len(ignored),
        "lines_reviewable": reviewable_lines,
        "lines_generated": generated_lines,
        "lines_total": reviewable_lines + generated_lines,
        "additions": as_int(context.get("additions"), 0),
        "deletions": as_int(context.get("deletions"), 0),
        "commits": len(context.get("commits") or []),
        "generated_paths": sorted(generated)[:50],
        "ignored_paths": sorted(ignored)[:50],
        "largest_files": [
            {
                "path": path,
                "lines": as_int((stats.get(path) or {}).get("additions"), 0)
                + as_int((stats.get(path) or {}).get("deletions"), 0),
            }
            for path in sorted(
                kept,
                key=lambda p: as_int((stats.get(p) or {}).get("additions"), 0)
                + as_int((stats.get(p) or {}).get("deletions"), 0),
                reverse=True,
            )[:10]
        ],
    }


def evaluate(context: Mapping[str, Any], config: Mapping[str, Any] | None = None
             ) -> dict[str, Any]:
    """Apply the size and risk policy to one pull request."""
    policy = {**DEFAULTS, **block(config, "size_policy")}
    generated_patterns = as_patterns(policy.get("generated_paths")) or list(DEFAULT_GENERATED)
    ignore_patterns = as_patterns(policy.get("ignore_paths"))

    metrics = measure(
        context, generated_patterns=generated_patterns, ignore_patterns=ignore_patterns
    )
    count_generated = as_bool(policy.get("count_generated"), False)
    files = metrics["files_total"] if count_generated else metrics["files_reviewable"]
    lines = metrics["lines_total"] if count_generated else metrics["lines_reviewable"]

    max_files = as_int(policy.get("max_files"), DEFAULTS["max_files"])
    max_lines = as_int(policy.get("max_lines"), DEFAULTS["max_lines"])
    warn_files = as_int(policy.get("warn_files"), DEFAULTS["warn_files"])
    warn_lines = as_int(policy.get("warn_lines"), DEFAULTS["warn_lines"])
    max_commits = as_int(policy.get("max_commits"), DEFAULTS["max_commits"])
    max_concerns = as_int(policy.get("max_concerns"), DEFAULTS["max_concerns"])

    findings: list[Finding] = []

    if max_files and files > max_files:
        findings.append(Finding(
            "high", "size-files-over-limit",
            f"{files} files changed, over the limit of {max_files}",
            "Split the pull request so each part can be reviewed on its own. "
            f"{metrics['files_generated']} generated file(s) are already excluded from this count.",
        ))
    elif warn_files and files > warn_files:
        findings.append(Finding(
            "medium", "size-files-large",
            f"{files} files changed",
            f"Above the {warn_files}-file review comfort threshold. Reviewable, but expect "
            "reviewers to need more time.",
        ))

    if max_lines and lines > max_lines:
        findings.append(Finding(
            "high", "size-lines-over-limit",
            f"{lines} changed lines, over the limit of {max_lines}",
            "Defect detection drops sharply past a few hundred lines per review. Split the "
            "change, or say in the description why it cannot be split.",
        ))
    elif warn_lines and lines > warn_lines:
        findings.append(Finding(
            "medium", "size-lines-large",
            f"{lines} changed lines",
            f"Above the {warn_lines}-line review comfort threshold.",
        ))

    if max_commits and metrics["commits"] > max_commits:
        findings.append(Finding(
            "low", "size-commits-many",
            f"{metrics['commits']} commits, over the limit of {max_commits}",
            "Consider squashing so the history reads as deliberate steps.",
        ))

    paths = [str(p) for p in context.get("files") or []]
    concerns = classify_concerns(paths)
    substantive = sorted(set(concerns) - INCIDENTAL_CONCERNS)
    if max_concerns and len(substantive) > max_concerns:
        findings.append(Finding(
            "medium", "size-mixed-concerns",
            f"Change spans {len(substantive)} unrelated areas: {', '.join(substantive)}",
            "Unrelated changes in one pull request force every reviewer to read every part. "
            "Split by area unless the change genuinely has to land atomically.",
        ))

    areas = sensitive_areas(paths)
    # A sensitive path inside an otherwise ordinary change is the case that gets
    # merged without the right eyes on it, so it is called out by name.
    for area, touched in sorted(areas.items()):
        findings.append(Finding(
            "medium" if len(touched) < 5 else "high",
            "size-sensitive-area",
            f"Touches {area} ({len(touched)} file(s))",
            "Highest-blast-radius file(s): "
            + ", ".join(f"`{p}`" for p in sorted(touched)[:5])
            + ("…" if len(touched) > 5 else ""),
        ))

    risk = _risk_level(files, lines, len(substantive), areas)
    fail_at = "high" if as_bool(policy.get("fail_on_violation"), False) else ""
    status = verdict(findings, fail_at=fail_at, warn_at="medium")

    return result(
        status,
        f"{files} reviewable file(s), {lines} changed line(s); risk {risk}",
        findings,
        metrics=metrics,
        risk=risk,
        concerns={name: len(members) for name, members in sorted(concerns.items())},
        substantive_concerns=substantive,
        sensitive_areas={name: sorted(members)[:20] for name, members in sorted(areas.items())},
        policy={
            "max_files": max_files, "max_lines": max_lines,
            "warn_files": warn_files, "warn_lines": warn_lines,
            "max_commits": max_commits, "max_concerns": max_concerns,
            "fail_on_violation": bool(fail_at), "count_generated": count_generated,
        },
    )


def _risk_level(files: int, lines: int, concerns: int, areas: Mapping[str, Any]) -> str:
    """A single low/medium/high label, so labels and the report agree."""
    if areas and any(
        area in areas for area in ("ci/cd workflows", "infrastructure", "authentication",
                                   "secrets and keys", "database migrations")
    ):
        return "high"
    if files > 40 or lines > 1000 or concerns > 3:
        return "high"
    if files > 15 or lines > 400 or concerns > 2:
        return "medium"
    return "low"
