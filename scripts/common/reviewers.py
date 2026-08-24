"""Resolve reviewer requests from central config and the PR's changed files."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .globs import matches

# CODEOWNERS entries are gitignore-style, not glob-style. These translations cover
# the forms GitHub actually resolves.
def _codeowners_patterns(text: str) -> list[str]:
    """Extract the path patterns from a CODEOWNERS file, ignoring the owners."""
    patterns: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            # A pattern with no owners does not assign anyone.
            continue
        patterns.append(parts[0])
    return patterns


def _codeowners_globs(pattern: str) -> list[str]:
    """Expand one CODEOWNERS pattern into equivalent glob patterns."""
    entry = pattern.strip()
    if entry in ("*", "/*"):
        return ["**"]
    anchored = entry.startswith("/")
    body = entry.lstrip("/")
    if body.endswith("/"):
        body += "**"
    globs = [body] if anchored else [body, f"**/{body}"]
    # A bare directory name in CODEOWNERS covers everything beneath it.
    extra: list[str] = []
    for candidate in globs:
        if not candidate.endswith("**") and "." not in candidate.rsplit("/", 1)[-1]:
            extra.append(f"{candidate}/**")
    return globs + extra


def codeowners_covers(text: str, paths: Iterable[str]) -> bool:
    """True when every changed path is claimed by at least one CODEOWNERS rule."""
    path_list = list(paths)
    if not path_list:
        return False
    globs = [g for pattern in _codeowners_patterns(text) for g in _codeowners_globs(pattern)]
    if not globs:
        return False
    return all(any(matches(g, path) for g in globs) for path in path_list)


@dataclass
class ReviewerRequest:
    """The reviewers to add, plus why anything was left out."""

    users: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    matched_groups: list[str] = field(default_factory=list)
    deferred_to_codeowners: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.users and not self.teams

    def to_dict(self) -> dict[str, Any]:
        return {
            "users": self.users,
            "teams": self.teams,
            "matched_groups": self.matched_groups,
            "deferred_to_codeowners": self.deferred_to_codeowners,
            "notes": self.notes,
        }


def _clean(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        name = value.strip().lstrip("@")
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def resolve_reviewers(
    groups: Mapping[str, Any],
    changed_paths: Sequence[str],
    *,
    author: str = "",
    existing_users: Iterable[str] = (),
    existing_teams: Iterable[str] = (),
    codeowners: str | None = None,
) -> ReviewerRequest:
    """Work out which reviewers to request.

    Rules enforced here:
      * never request the PR author
      * never request a duplicate, in this run or against already-requested reviewers
      * existing reviewers are preserved (we only ever add)
      * defer entirely to CODEOWNERS when it already covers every changed file
    """
    request = ReviewerRequest()

    if codeowners and codeowners_covers(codeowners, changed_paths):
        request.deferred_to_codeowners = True
        request.notes.append("CODEOWNERS already covers every changed file.")
        return request

    author_key = (author or "").strip().lstrip("@").lower()
    already_users = {name.lower() for name in _clean(list(existing_users))}
    already_teams = {name.lower() for name in _clean(list(existing_teams))}

    users: list[str] = []
    teams: list[str] = []
    seen_users: set[str] = set()
    seen_teams: set[str] = set()

    for name, group in sorted(groups.items()):
        if not isinstance(group, dict):
            request.notes.append(f"Reviewer group '{name}' is not a mapping; ignored.")
            continue
        patterns = [p for p in (group.get("paths") or []) if isinstance(p, str)]
        if not patterns:
            request.notes.append(f"Reviewer group '{name}' declares no paths; ignored.")
            continue
        if not any(matches(pattern, path) for pattern in patterns for path in changed_paths):
            continue
        request.matched_groups.append(name)
        for user in _clean(group.get("users")):
            key = user.lower()
            if key == author_key:
                continue
            if key in already_users or key in seen_users:
                continue
            seen_users.add(key)
            users.append(user)
        for team in _clean(group.get("teams")):
            key = team.lower()
            if key in already_teams or key in seen_teams:
                continue
            seen_teams.add(key)
            teams.append(team)

    request.users = users
    request.teams = teams
    if request.matched_groups and request.empty:
        request.notes.append(
            "Matched reviewer groups declare no assignable users or teams "
            f"({', '.join(request.matched_groups)})."
        )
    if not request.matched_groups:
        request.notes.append("No reviewer group matched the changed files.")
    return request
