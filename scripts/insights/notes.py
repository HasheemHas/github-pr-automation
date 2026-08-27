"""Release notes generated from commits, labels and changed files.

No model, no API key. Conventional Commit subjects are parsed when present; when
they are absent the commit is classified from its wording and the paths it
touched, so a repository that has never enforced a commit convention still gets
usable notes rather than an empty section.

Two audiences are served from the same data:

  * ``user_facing`` — what changed, for a changelog entry
  * ``operator`` — migrations, dependency and configuration changes, for whoever
    runs the deploy

Anything the other insights found that a release manager must know (a breaking
API change, a required migration) is folded in, so the notes and the report can
never disagree.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from ..common.globs import matches
from .base import as_bool, as_patterns, block, result

#: Conventional Commit subject: ``type(scope)!: description``
_CONVENTIONAL = re.compile(
    r"""\A(?P<type>[a-z]+)
        (?:\((?P<scope>[^)]*)\))?
        (?P<breaking>!)?
        :\s*(?P<description>.+)\Z""",
    re.VERBOSE | re.IGNORECASE,
)
_MERGE_COMMIT = re.compile(r"(?i)\A(?:merge|revert)\b")
_ISSUE_REF = re.compile(r"(?<![\w/])(?:[A-Z][A-Z0-9]+-\d+|#\d+)\b")
_TRAILING_REF = re.compile(r"\s*\((?:#\d+|[A-Z][A-Z0-9]+-\d+)\)\s*\Z")

#: section key -> (heading, commit types, keyword patterns)
SECTIONS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("breaking", "⚠️ Breaking Changes", ("breaking",), (r"\bbreaking\b", r"\bremoved?\b.*\bapi\b")),
    ("features", "Added", ("feat", "feature"),
     (r"\badd(?:s|ed|ing)?\b", r"\bintroduce[sd]?\b", r"\bimplement(?:s|ed)?\b",
      r"\bsupport\b", r"\bnew\b", r"\benable[sd]?\b")),
    ("fixes", "Fixed", ("fix", "bugfix", "hotfix"),
     (r"\bfix(?:e[sd])?\b", r"\bbug\b", r"\bcorrect(?:s|ed)?\b", r"\bresolve[sd]?\b",
      r"\bprevent(?:s|ed)?\b", r"\brepair(?:s|ed)?\b")),
    ("changed", "Changed", ("change", "refactor", "style", "revert"),
     (r"\bchange[sd]?\b", r"\brefactor(?:s|ed|ing)?\b", r"\brename[sd]?\b",
      r"\bupdate[sd]?\b", r"\bmove[sd]?\b", r"\bsimplif(?:y|ies|ied)\b")),
    ("performance", "Performance", ("perf",),
     (r"\bperf(?:ormance)?\b", r"\boptimi[sz]e[sd]?\b", r"\bspeed\b", r"\bfaster\b",
      r"\bcache\b")),
    ("security", "Security", ("security",),
     (r"\bsecurity\b", r"\bcve-\d", r"\bvulnerabilit", r"\bexploit\b", r"\bsanitis?z?e")),
    ("deprecated", "Deprecated", ("deprecate",), (r"\bdeprecate[sd]?\b",)),
    ("removed", "Removed", ("remove",), (r"\bremove[sd]?\b", r"\bdelete[sd]?\b", r"\bdrop(?:s|ped)?\b")),
    ("dependencies", "Dependencies", ("deps", "dependencies", "build"),
     (r"\bbump\b", r"\bupgrade[sd]?\b", r"\bdependenc(?:y|ies)\b")),
    ("docs", "Documentation", ("docs", "doc"), (r"\bdocument(?:s|ation|ed)?\b", r"\breadme\b")),
    ("tests", "Tests", ("test", "tests"), (r"\btests?\b", r"\bcoverage\b", r"\bspec\b")),
    ("internal", "Internal", ("chore", "ci", "build", "release"),
     (r"\bchore\b", r"\bci\b", r"\bbuild\b", r"\blint\b", r"\bformat", r"\btypo\b")),
)

#: Sections shown to end users by default. The rest are collapsed under Internal.
DEFAULT_SECTIONS = (
    "breaking", "features", "fixes", "changed", "performance", "security",
    "deprecated", "removed", "dependencies", "docs",
)

#: Path patterns implying an operator action, independent of any commit wording.
OPERATOR_SIGNALS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("Database migration", ("**/migrations/**", "**/migrate/**", "db/**", "**/*.sql"),
     "Run migrations as part of the deploy."),
    ("Dependency change", ("**/package.json", "**/package-lock.json", "**/requirements*.txt",
                           "**/poetry.lock", "**/composer.json", "**/composer.lock",
                           "**/go.mod", "**/Gemfile*", "**/Cargo.*", "**/pyproject.toml"),
     "Reinstall dependencies before starting the service."),
    ("Configuration change", ("**/*.env*", "**/config/**", "**/settings*.py", "**/*.ini",
                              "**/*.conf", "**/appsettings*.json"),
     "Review configuration and environment variables before deploying."),
    ("Container image change", ("**/Dockerfile", "docker-compose.*", "**/docker-compose.*"),
     "Rebuild and republish the image."),
    ("Infrastructure change", ("terraform/**", "**/*.tf", "helm/**", "k8s/**"),
     "Apply the infrastructure change before or with the release, as the plan requires."),
    ("CI/CD change", (".github/workflows/**", ".gitlab-ci.yml", "Jenkinsfile"),
     "Pipeline behaviour changes with this merge."),
    ("Scheduled job change", ("**/cron*", "**/celery*", "**/tasks.py", "**/jobs/**"),
     "Restart workers or schedulers so the new definitions load."),
)

DEFAULTS = {
    "enabled": True,
    "include_authors": False,
    "include_issue_refs": True,
    "include_internal": False,
    "max_per_section": 25,
}


def _clean_subject(subject: str) -> str:
    """Normalise a commit subject into a changelog line."""
    text = (subject or "").strip()
    text = _TRAILING_REF.sub("", text)
    text = text.rstrip(".").strip()
    return text[:1].upper() + text[1:] if text else text


def parse_commit(subject: str) -> dict[str, Any]:
    """Classify one commit subject into a section, scope and description."""
    raw = (subject or "").strip()
    match = _CONVENTIONAL.match(raw)
    if match:
        kind = (match.group("type") or "").lower()
        breaking = bool(match.group("breaking"))
        description = match.group("description").strip()
        section = "breaking" if breaking else _section_for_type(kind) or "changed"
        return {
            "section": section, "type": kind, "scope": (match.group("scope") or "").strip(),
            "description": _clean_subject(description), "breaking": breaking,
            "conventional": True, "subject": raw,
            "refs": sorted(set(_ISSUE_REF.findall(raw))),
        }

    lowered = raw.lower()
    section = "changed"
    for key, _heading, _types, patterns in SECTIONS:
        if any(re.search(pattern, lowered) for pattern in patterns):
            section = key
            break
    return {
        "section": section, "type": "", "scope": "",
        "description": _clean_subject(raw), "breaking": "breaking change" in lowered,
        "conventional": False, "subject": raw,
        "refs": sorted(set(_ISSUE_REF.findall(raw))),
    }


def _section_for_type(kind: str) -> str | None:
    for key, _heading, types, _patterns in SECTIONS:
        if kind in types:
            return key
    return None


def _heading(key: str) -> str:
    for section_key, heading, _types, _patterns in SECTIONS:
        if section_key == key:
            return heading
    return key.title()


def operator_actions(paths: Sequence[str]) -> list[dict[str, str]]:
    """Deploy-time actions implied by the changed paths."""
    actions: list[dict[str, str]] = []
    for label, patterns, action in OPERATOR_SIGNALS:
        touched = [p for p in paths if any(matches(pattern, p) for pattern in patterns)]
        if touched:
            actions.append({
                "label": label, "action": action,
                "files": ", ".join(f"`{p}`" for p in sorted(touched)[:5]),
                "count": str(len(touched)),
            })
    return actions


def generate(
    context: Mapping[str, Any],
    *,
    insights: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    labels: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the release notes payload for one pull request."""
    policy = {**DEFAULTS, **block(config, "release_notes")}
    if not as_bool(policy.get("enabled"), True):
        return result("not_run", "Release note generation is disabled for this repository.")

    commits = [str(c).strip() for c in context.get("commits") or [] if str(c).strip()]
    subjects = [c for c in commits if not _MERGE_COMMIT.match(c)]
    if not subjects:
        # A squash-merge repository may have one commit that is the PR title.
        subjects = [str(context.get("title") or "").strip()] if context.get("title") else []

    parsed = [parse_commit(subject) for subject in subjects]
    # Two commits saying the same thing produce one changelog line.
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for entry in parsed:
        key = (entry["section"], entry["description"].lower())
        if not entry["description"] or key in seen:
            continue
        seen.add(key)
        grouped.setdefault(entry["section"], []).append(entry)

    label_set = {str(label).lower() for label in labels}
    if "breaking-change" in label_set and "breaking" not in grouped:
        grouped.setdefault("breaking", []).append({
            "section": "breaking", "description": "This release contains a breaking change.",
            "scope": "", "refs": [], "breaking": True, "conventional": False,
            "subject": "", "type": "",
        })

    findings = _from_insights(insights or {})
    for entry in findings.get("breaking_entries", []):
        grouped.setdefault("breaking", []).append(entry)

    include_internal = as_bool(policy.get("include_internal"), False)
    configured = as_patterns(policy.get("sections")) or list(DEFAULT_SECTIONS)
    order = [key for key in configured if key in grouped]
    if include_internal:
        # Everything else — tests, chores, CI — appended in discovery order.
        order += [key for key in grouped if key not in order]

    max_per_section = int(policy.get("max_per_section") or DEFAULTS["max_per_section"])
    sections = [
        {
            "key": key,
            "heading": _heading(key),
            "entries": [_render_entry(entry, policy) for entry in grouped[key][:max_per_section]],
            "dropped": max(0, len(grouped[key]) - max_per_section),
        }
        for key in order
    ]

    paths = [str(p) for p in context.get("files") or []]
    actions = operator_actions(paths)
    actions.extend(findings.get("operator_actions", []))

    refs = sorted({ref for entry in parsed for ref in entry["refs"]})
    unconventional = [e for e in parsed if not e["conventional"]]

    payload_markdown = render_markdown(
        sections, actions,
        title=str(context.get("title") or ""),
        refs=refs if as_bool(policy.get("include_issue_refs"), True) else [],
        stats={
            "files": len(paths),
            "additions": context.get("additions") or 0,
            "deletions": context.get("deletions") or 0,
            "commits": len(commits),
        },
    )

    return result(
        "pass",
        f"{sum(len(s['entries']) for s in sections)} note(s) across {len(sections)} section(s)",
        (),
        sections=sections,
        markdown=payload_markdown,
        operator_actions=actions,
        related_issues=refs,
        breaking=bool(grouped.get("breaking")),
        commit_style=(
            "conventional" if parsed and not unconventional
            else "mixed" if parsed and len(unconventional) < len(parsed)
            else "freeform"
        ),
        note=(
            "Generated from commit subjects, labels and changed paths using templates. "
            "No model was used, so wording comes from the commits themselves."
        ),
    )


def _render_entry(entry: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    """One changelog bullet."""
    text = str(entry.get("description") or "")
    scope = str(entry.get("scope") or "")
    if scope:
        text = f"**{scope}:** {text}"
    refs = entry.get("refs") or []
    if refs and as_bool(policy.get("include_issue_refs"), True):
        text += " (" + ", ".join(str(ref) for ref in refs) + ")"
    return text


def _from_insights(insights: Mapping[str, Any]) -> dict[str, list[Any]]:
    """Fold the other insights into the notes so the two cannot disagree."""
    breaking_entries: list[dict[str, Any]] = []
    actions: list[dict[str, str]] = []

    api = _data(insights.get("api"))
    if api.get("breaking_change"):
        removed = (api.get("routes") or {}).get("removed") or []
        symbols = (api.get("symbols") or {}).get("removed") or []
        detail = ", ".join([f"`{r}`" for r in removed[:5]] + [f"`{s}`" for s in symbols[:5]])
        breaking_entries.append({
            "section": "breaking", "scope": "api",
            "description": (
                "Interface changes detected"
                + (f": {detail}" if detail else "")
                + ". Consumers may need to be updated."
            ),
            "refs": [], "breaking": True, "conventional": False, "subject": "", "type": "",
        })

    migrations = _data(insights.get("migrations"))
    records = migrations.get("migrations") or []
    if records:
        risky = [r for r in records if r.get("risky_operations")]
        actions.append({
            "label": "Database migration",
            "action": (
                f"{len(records)} migration file(s) in this release"
                + (f", {len(risky)} containing destructive or locking operations"
                   if risky else "")
                + ". Take a backup before applying."
            ),
            "files": ", ".join(f"`{r['path']}`" for r in records[:5]),
            "count": str(len(records)),
        })

    deps = _data(insights.get("dependencies"))
    totals = deps.get("totals") or {}
    if totals.get("major_bumps"):
        actions.append({
            "label": "Major dependency upgrade",
            "action": f"{totals['major_bumps']} dependency(ies) crossed a major version. "
                      "Review the upgrade notes before deploying.",
            "files": "", "count": str(totals["major_bumps"]),
        })
    return {"breaking_entries": breaking_entries, "operator_actions": actions}


def _data(entry: Any) -> dict[str, Any]:
    """Unwrap an analysis section, which may be ``{"status", "data"}`` or raw."""
    if isinstance(entry, Mapping):
        inner = entry.get("data")
        if isinstance(inner, Mapping):
            return dict(inner)
        return dict(entry)
    return {}


def render_markdown(
    sections: Sequence[Mapping[str, Any]],
    actions: Sequence[Mapping[str, str]],
    *,
    title: str = "",
    refs: Iterable[str] = (),
    stats: Mapping[str, Any] | None = None,
) -> str:
    """Render the notes as markdown suitable for a changelog or a release body."""
    lines: list[str] = []
    if title:
        lines.append(f"## {_clean_subject(title)}")
        lines.append("")

    for section in sections:
        entries = section.get("entries") or []
        if not entries:
            continue
        lines.append(f"### {section['heading']}")
        lines.append("")
        lines.extend(f"- {entry}" for entry in entries)
        dropped = int(section.get("dropped") or 0)
        if dropped:
            lines.append(f"- _…and {dropped} more_")
        lines.append("")

    if actions:
        lines.append("### Deployment Notes")
        lines.append("")
        for action in actions:
            detail = f"**{action['label']}** — {action['action']}"
            if action.get("files"):
                detail += f" ({action['files']})"
            lines.append(f"- {detail}")
        lines.append("")

    reference_list = list(refs)
    if reference_list:
        lines.append("### References")
        lines.append("")
        lines.append(", ".join(reference_list))
        lines.append("")

    if stats:
        lines.append(
            f"_{stats.get('commits', 0)} commit(s), {stats.get('files', 0)} file(s), "
            f"+{stats.get('additions', 0)}/-{stats.get('deletions', 0)}._"
        )

    return "\n".join(lines).strip() + "\n" if lines else ""
