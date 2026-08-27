"""Parse CODEOWNERS and answer "who owns this file".

GitHub's own semantics, reproduced deliberately:

  * entries are gitignore-style patterns, not globs
  * the LAST matching entry wins outright — it does not merge with earlier ones
  * a pattern with no owners still matches, and clears ownership for those paths
  * a leading ``/`` anchors to the repository root; anything else matches at any
    depth
  * a trailing ``/`` matches everything beneath the directory
  * a bare directory name covers everything beneath it

Owners are classified as users, teams (``@org/team``) or email addresses, because
the three go to different places in GitHub's reviewer API.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .globs import matches

_EMAIL = re.compile(r"\A[^@\s]+@[^@\s]+\.[^@\s]+\Z")


@dataclass(frozen=True)
class Rule:
    """One CODEOWNERS line."""

    pattern: str
    owners: tuple[str, ...]
    line: int = 0

    @property
    def globs(self) -> tuple[str, ...]:
        return tuple(_expand(self.pattern))


@dataclass
class Ownership:
    """Who owns the changed files, and what could not be resolved."""

    by_path: dict[str, list[str]] = field(default_factory=dict)
    users: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    unowned: list[str] = field(default_factory=list)
    matched_rules: list[str] = field(default_factory=list)

    @property
    def covers_everything(self) -> bool:
        return bool(self.by_path) and not self.unowned

    @property
    def empty(self) -> bool:
        return not self.users and not self.teams

    def to_dict(self) -> dict[str, object]:
        return {
            "users": self.users,
            "teams": self.teams,
            "emails": self.emails,
            "unowned": self.unowned,
            "matched_rules": self.matched_rules,
            "covers_everything": self.covers_everything,
            # Capped: a 900-file PR must not push a megabyte into the artifact.
            "by_path": {path: owners for path, owners in list(self.by_path.items())[:200]},
        }


def _expand(pattern: str) -> list[str]:
    """Expand one CODEOWNERS pattern into equivalent glob patterns."""
    entry = (pattern or "").strip()
    if not entry:
        return []
    if entry in ("*", "/*", "**"):
        return ["**"]
    anchored = entry.startswith("/")
    body = entry.lstrip("/")
    if body.endswith("/"):
        body += "**"
    globs = [body] if anchored else [body, f"**/{body}"]
    # A bare directory name in CODEOWNERS covers everything beneath it, and
    # "no dot in the last segment" is the only signal available that it is one.
    extra: list[str] = []
    for candidate in globs:
        last = candidate.rsplit("/", 1)[-1]
        if not candidate.endswith("**") and "." not in last and "*" not in last:
            extra.append(f"{candidate}/**")
    return globs + extra


def parse(text: str) -> list[Rule]:
    """Every usable rule in a CODEOWNERS file, in file order."""
    rules: list[Rule] = []
    for number, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        pattern, owners = parts[0], tuple(parts[1:])
        # A section heading from the CODEOWNERS "sections" syntax is not a pattern.
        if pattern.startswith("[") or pattern.startswith("^["):
            continue
        rules.append(Rule(pattern=pattern, owners=owners, line=number))
    return rules


def owners_for(rules: Sequence[Rule], path: str) -> tuple[str, ...]:
    """Owners of one path. The last matching rule wins, as GitHub does it.

    An ownerless rule that matches last deliberately returns no owners: that is
    how CODEOWNERS excludes a subtree from an earlier broad rule.
    """
    winner: Rule | None = None
    for rule in rules:
        if any(matches(glob, path) for glob in rule.globs):
            winner = rule
    return winner.owners if winner else ()


def matching_rule(rules: Sequence[Rule], path: str) -> Rule | None:
    """The rule that decides ``path``, or None when nothing matches."""
    winner: Rule | None = None
    for rule in rules:
        if any(matches(glob, path) for glob in rule.globs):
            winner = rule
    return winner


def classify(owner: str) -> tuple[str, str]:
    """Split one owner token into ``(kind, name)`` where kind is user/team/email."""
    token = (owner or "").strip()
    if not token:
        return "", ""
    if _EMAIL.match(token):
        return "email", token
    name = token.lstrip("@")
    if "/" in name:
        # @org/team-slug -> the API wants just the slug.
        return "team", name.split("/", 1)[1]
    return "user", name


def resolve(text: str, paths: Iterable[str]) -> Ownership:
    """Map every changed path to its owners and split them by kind."""
    rules = parse(text)
    ownership = Ownership()
    if not rules:
        ownership.unowned = sorted(paths)
        return ownership

    seen_users: dict[str, None] = {}
    seen_teams: dict[str, None] = {}
    seen_emails: dict[str, None] = {}
    seen_rules: dict[str, None] = {}

    for path in paths:
        rule = matching_rule(rules, path)
        if rule is None or not rule.owners:
            ownership.unowned.append(path)
            if rule is not None:
                # A matching ownerless rule is a deliberate exclusion, worth naming.
                seen_rules.setdefault(f"{rule.pattern} (no owners)", None)
            continue
        seen_rules.setdefault(rule.pattern, None)
        resolved: list[str] = []
        for owner in rule.owners:
            kind, name = classify(owner)
            if not name:
                continue
            resolved.append(owner.lstrip("@") if kind != "email" else owner)
            if kind == "user":
                seen_users.setdefault(name, None)
            elif kind == "team":
                seen_teams.setdefault(name, None)
            else:
                seen_emails.setdefault(name, None)
        ownership.by_path[path] = resolved

    ownership.users = list(seen_users)
    ownership.teams = list(seen_teams)
    ownership.emails = list(seen_emails)
    ownership.matched_rules = list(seen_rules)
    ownership.unowned = sorted(ownership.unowned)
    return ownership


def covers(text: str, paths: Iterable[str]) -> bool:
    """True when every changed path is owned by someone."""
    path_list = list(paths)
    if not path_list:
        return False
    return resolve(text, path_list).covers_everything


def suggestions(
    ownership: Ownership,
    *,
    author: str = "",
    existing_users: Iterable[str] = (),
    existing_teams: Iterable[str] = (),
) -> dict[str, list[str]]:
    """Owners worth requesting: never the author, never an existing reviewer."""
    author_key = (author or "").strip().lstrip("@").lower()
    have_users = {name.strip().lstrip("@").lower() for name in existing_users}
    have_teams = {name.strip().lstrip("@").lower() for name in existing_teams}
    return {
        "users": [
            user for user in ownership.users
            if user.lower() != author_key and user.lower() not in have_users
        ],
        "teams": [team for team in ownership.teams if team.lower() not in have_teams],
    }


def summarise(ownership: Ownership, *, limit: int = 12) -> list[str]:
    """One line per owner, listing how many files they are on the hook for."""
    counts: dict[str, int] = {}
    for owners in ownership.by_path.values():
        for owner in owners:
            counts[owner] = counts.get(owner, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [f"@{owner} — {count} file(s)" for owner, count in ordered[:limit]]
