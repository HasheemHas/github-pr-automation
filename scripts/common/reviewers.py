"""Resolve reviewer requests from central config, CODEOWNERS and changed files.

Two independent sources of truth are combined here:

  * ``config/reviewers.yml`` — central path-to-reviewer routing that applies to
    every target repository, including ones with no CODEOWNERS file of their own
  * the target repository's own CODEOWNERS — parsed properly, so ownership is
    reported per file and per owner rather than as a single yes/no

CODEOWNERS coverage still suppresses the *request* by default, because GitHub
already assigns owners itself and a second request produces duplicate
notifications. It no longer suppresses the *report*: knowing which owner is on
the hook for which file is the useful half, and it was previously thrown away.

Rules enforced here:
  * never request the PR author
  * never request a duplicate, in this run or against already-requested reviewers
  * existing reviewers are preserved (reviewers are only ever added)
  * a name GitHub cannot resolve is reported, never fatal
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import codeowners as codeowners_module
from .globs import matches

#: How CODEOWNERS ownership feeds into the reviewer request.
#:   defer   report the mapping, let GitHub do the requesting (default)
#:   request explicitly request the owners as well
#:   ignore  do not read CODEOWNERS at all
CODEOWNERS_MODES = ("defer", "request", "ignore")
DEFAULT_CODEOWNERS_MODE = "defer"


def _codeowners_patterns(text: str) -> list[str]:
    """The path patterns from a CODEOWNERS file, ignoring the owners.

    Retained for callers that only need the patterns; ownership questions should
    go through :mod:`scripts.common.codeowners`.
    """
    return [rule.pattern for rule in codeowners_module.parse(text) if rule.owners]


def _codeowners_globs(pattern: str) -> list[str]:
    """Expand one CODEOWNERS pattern into equivalent glob patterns."""
    return list(codeowners_module._expand(pattern))


def codeowners_covers(text: str, paths: Iterable[str]) -> bool:
    """True when every changed path is claimed by at least one CODEOWNERS rule."""
    return codeowners_module.covers(text, paths)


@dataclass
class ReviewerRequest:
    """The reviewers to add, who owns what, and why anything was left out."""

    users: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    matched_groups: list[str] = field(default_factory=list)
    deferred_to_codeowners: bool = False
    notes: list[str] = field(default_factory=list)
    #: CODEOWNERS-derived ownership, for the report. Empty when there is no file.
    ownership: dict[str, Any] = field(default_factory=dict)
    #: Owners worth a human's attention even when not requested via the API.
    suggested_users: list[str] = field(default_factory=list)
    suggested_teams: list[str] = field(default_factory=list)
    #: Changed paths no rule in either source claims.
    unowned_paths: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.users and not self.teams

    @property
    def has_suggestions(self) -> bool:
        return bool(self.suggested_users or self.suggested_teams)

    def to_dict(self) -> dict[str, Any]:
        return {
            "users": self.users,
            "teams": self.teams,
            "matched_groups": self.matched_groups,
            "deferred_to_codeowners": self.deferred_to_codeowners,
            "notes": self.notes,
            "ownership": self.ownership,
            "suggested_users": self.suggested_users,
            "suggested_teams": self.suggested_teams,
            "unowned_paths": self.unowned_paths,
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
    codeowners_mode: str = DEFAULT_CODEOWNERS_MODE,
) -> ReviewerRequest:
    """Work out which reviewers to request, and record who owns what."""
    request = ReviewerRequest()
    mode = (codeowners_mode or DEFAULT_CODEOWNERS_MODE).strip().lower()
    if mode not in CODEOWNERS_MODES:
        request.notes.append(
            f"Unknown reviewers.codeowners mode {mode!r}; using {DEFAULT_CODEOWNERS_MODE!r}."
        )
        mode = DEFAULT_CODEOWNERS_MODE

    author_key = (author or "").strip().lstrip("@").lower()
    already_users = {name.lower() for name in _clean(list(existing_users))}
    already_teams = {name.lower() for name in _clean(list(existing_teams))}

    # -- CODEOWNERS ownership, always reported when a file exists -------------
    ownership: codeowners_module.Ownership | None = None
    if codeowners and mode != "ignore":
        ownership = codeowners_module.resolve(codeowners, changed_paths)
        request.ownership = {
            **ownership.to_dict(),
            "per_owner": codeowners_module.summarise(ownership),
        }
        suggested = codeowners_module.suggestions(
            ownership, author=author, existing_users=already_users,
            existing_teams=already_teams,
        )
        request.suggested_users = suggested["users"]
        request.suggested_teams = suggested["teams"]
        request.unowned_paths = ownership.unowned[:50]

        if ownership.covers_everything and mode == "defer":
            request.deferred_to_codeowners = True
            request.notes.append(
                "CODEOWNERS covers every changed file; GitHub assigns the owners itself. "
                + (f"Owners: {', '.join('@' + o for o in ownership.users + ownership.teams)}."
                   if not ownership.empty else
                   "The matching rules name no assignable user or team.")
            )
            return request
        if ownership.unowned:
            request.notes.append(
                f"{len(ownership.unowned)} changed file(s) are not covered by CODEOWNERS: "
                + ", ".join(f"`{p}`" for p in ownership.unowned[:5])
                + ("…" if len(ownership.unowned) > 5 else "")
            )

    # -- central reviewer groups ---------------------------------------------
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
            if key == author_key or key in already_users or key in seen_users:
                continue
            seen_users.add(key)
            users.append(user)
        for team in _clean(group.get("teams")):
            key = team.lower()
            if key in already_teams or key in seen_teams:
                continue
            seen_teams.add(key)
            teams.append(team)

    # -- CODEOWNERS owners, requested only when explicitly asked for ---------
    if ownership is not None and mode == "request":
        for user in request.suggested_users:
            key = user.lower()
            if key == author_key or key in already_users or key in seen_users:
                continue
            seen_users.add(key)
            users.append(user)
        for team in request.suggested_teams:
            key = team.lower()
            if key in already_teams or key in seen_teams:
                continue
            seen_teams.add(key)
            teams.append(team)
        if request.has_suggestions:
            request.notes.append(
                "CODEOWNERS owners requested explicitly (reviewers.codeowners: request)."
            )

    request.users = users
    request.teams = teams
    if request.matched_groups and request.empty and not request.has_suggestions:
        request.notes.append(
            "Matched reviewer groups declare no assignable users or teams "
            f"({', '.join(request.matched_groups)})."
        )
    if not request.matched_groups:
        request.notes.append("No reviewer group matched the changed files.")
    return request
