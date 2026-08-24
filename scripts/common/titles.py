"""Decide whether a human-written PR title is worth keeping.

The rule: a title that already tells a reviewer what changed is never touched. A
placeholder title ("update", "fix", "new changes", "Update auth.py") is replaced
with the AI's title. Ambiguity resolves in favour of keeping the human's text.
"""
from __future__ import annotations

import re

#: Titles that are placeholders no matter what else they contain.
WEAK_EXACT = frozenset(
    {
        "update", "updates", "updated", "update code", "update files",
        "change", "changes", "changed", "new changes", "some changes",
        "minor changes", "small changes", "misc", "misc changes",
        "fix", "fixes", "fixed", "fixing", "bug", "bugfix", "bug fix",
        "test", "tests", "testing", "test pr", "testing pr",
        "feature", "features", "feat", "new feature",
        "final", "final changes", "finalize", "final version",
        "wip", "work in progress", "draft", "temp", "tmp", "stuff",
        "cleanup", "clean up", "refactor", "refactoring", "chore",
        "patch", "hotfix", "quick fix", "asdf", ".", "..",
        "please review", "review", "ready", "done", "merge", "merge me",
        "initial commit", "first commit", "no message",
    }
)

#: A conventional-commit header with a real description is always strong.
CONVENTIONAL = re.compile(
    r"\A(build|chore|ci|docs|feat|feature|fix|perf|refactor|revert|style|test)"
    r"(\([^)]{1,40}\))?!?:\s*(?P<description>.+)\Z",
    re.IGNORECASE,
)

#: GitHub's own default titles for web edits and branch-name PRs.
GENERATED = (
    re.compile(r"\A(update|create|add|delete|rename)\s+[\w./-]+\Z", re.IGNORECASE),
    re.compile(r"\A[\w-]+/[\w./-]+\Z"),          # feature/some-branch
    re.compile(r"\A(patch|branch)-\d+\Z", re.IGNORECASE),
    re.compile(r"\A(revert\s+)?\"?[a-f0-9]{7,40}\"?\Z", re.IGNORECASE),  # a bare SHA
)

MIN_WORDS = 3
MIN_LENGTH = 12
#: A conventional-commit description shorter than this is still a placeholder.
MIN_CONVENTIONAL_DESCRIPTION_WORDS = 2


def _normalise(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip()).strip(" .!-–—:").lower()


def is_weak_title(title: str) -> bool:
    """True when ``title`` carries no information a reviewer could use."""
    text = (title or "").strip()
    if not text:
        return True
    normalised = _normalise(text)
    if not normalised:
        return True
    if normalised in WEAK_EXACT:
        return True

    conventional = CONVENTIONAL.match(text)
    if conventional:
        description = _normalise(conventional.group("description"))
        # "fix: bug" is still a placeholder; "fix(auth): prevent reuse" is not.
        if description in WEAK_EXACT:
            return True
        return len(description.split()) < MIN_CONVENTIONAL_DESCRIPTION_WORDS

    # A bare issue or ticket reference says nothing on its own.
    if re.fullmatch(r"(#\d+|[a-z]{2,10}-\d+)", normalised):
        return True
    if any(pattern.match(text) for pattern in GENERATED):
        return True
    if len(normalised) < MIN_LENGTH or len(normalised.split()) < MIN_WORDS:
        return True
    return False


def title_decision(
    current: str,
    proposed: str,
    *,
    policy: str = "weak",
) -> tuple[bool, str]:
    """Decide whether to replace the PR title.

    ``policy`` is one of:
      ``weak``   replace only a placeholder title (default)
      ``always`` replace whenever the AI proposed something different
      ``never``  never touch the title

    Returns ``(replace, reason)``; the reason is surfaced in the report so the
    decision is never silent.
    """
    proposed_text = (proposed or "").strip()
    current_text = (current or "").strip()
    mode = (policy or "weak").strip().lower()

    if mode not in {"weak", "always", "never"}:
        return False, f"Unknown title policy {policy!r}; title left unchanged."
    if mode == "never":
        return False, "Title policy is 'never'."
    if not proposed_text:
        return False, "AI proposed no title."
    if _normalise(proposed_text) == _normalise(current_text):
        return False, "AI title matches the existing title."
    if is_weak_title(proposed_text):
        # Never trade a human placeholder for a machine placeholder.
        return False, "AI title is itself too generic to be an improvement."
    if mode == "always":
        return True, "Title policy is 'always'."
    if is_weak_title(current_text):
        return True, f"Existing title {current_text!r} is a placeholder."
    return False, f"Existing title {current_text!r} is descriptive; kept."
