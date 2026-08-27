"""Central action dispatcher.

Every accepted action maps to an explicit set of pipeline steps. Nothing falls
through silently: an unknown action is a hard failure.
"""
from __future__ import annotations

from .errors import DispatchError

#: Every step the central pipeline knows how to run.
STEPS = (
    "metadata",
    "labels",
    "reviewers",
    "review",
    "security",
    "quality",
    "tests",
    # Deterministic insights over the diff. All credential-free.
    "size",
    "secrets",
    "deps",
    "migrations",
    "api",
    "notes",
    # Insights that need the checked-out tree and a test run.
    "coverage",
    "flaky",
    "fix",
    "resolve_conflicts",
)

#: The diff-only insights, grouped so ``insights`` can request them together.
INSIGHT_STEPS = ("size", "secrets", "deps", "migrations", "api", "notes")

#: ``all`` deliberately excludes ``fix`` and ``resolve_conflicts``: both write to
#: the target repository and must be requested explicitly. ``flaky`` is excluded
#: too, for a different reason: it re-runs the whole test suite, so the cost has
#: to be asked for.
ACTIONS: dict[str, tuple[str, ...]] = {
    "all": (
        "metadata", "labels", "reviewers", "review", "security", "quality", "tests",
        "size", "secrets", "deps", "migrations", "api", "notes", "coverage",
    ),
    "metadata": ("metadata",),
    "labels": ("labels",),
    "reviewers": ("reviewers",),
    "review": ("review",),
    "security": ("security",),
    "quality": ("quality",),
    "tests": ("tests",),
    # Every diff-only insight, with no checkout and no target code execution.
    "insights": INSIGHT_STEPS,
    "size": ("size",),
    "secrets": ("secrets",),
    "deps": ("deps",),
    "migrations": ("migrations",),
    "api": ("api",),
    "notes": ("notes",),
    # Coverage needs the test suite to have produced a report, so quality runs too.
    "coverage": ("quality", "coverage"),
    "flaky": ("flaky",),
    # A fix needs findings to fix, so review always runs first.
    "fix": ("review", "fix"),
    "resolve-conflicts": ("resolve_conflicts",),
}

#: Steps that need deterministic diff/context analysis.
ANALYSIS_STEPS = frozenset(
    {"metadata", "review", "security", "tests", "fix", "resolve_conflicts", *INSIGHT_STEPS}
)

#: Steps that need the target repository working tree checked out.
SOURCE_STEPS = frozenset(
    {"quality", "security", "fix", "resolve_conflicts", "coverage", "flaky"}
)

#: Steps that end in a write to the target repository's git refs.
WRITE_STEPS = frozenset({"fix", "resolve_conflicts"})


def plan(action: str) -> dict[str, object]:
    """Expand an action into a concrete execution plan.

    Raises:
        DispatchError: the action is not supported.
    """
    key = (action or "").strip()
    if key not in ACTIONS:
        supported = ", ".join(sorted(ACTIONS))
        raise DispatchError(f"Unsupported action {key!r}. Supported actions: {supported}.")

    steps = ACTIONS[key]
    enabled = {step: step in steps for step in STEPS}
    return {
        "action": key,
        "steps": list(steps),
        **enabled,
        # ``security`` shares the rule review pass, so review analysis is needed too.
        "needs_analysis": any(step in ANALYSIS_STEPS for step in steps),
        "needs_target_source": any(step in SOURCE_STEPS for step in steps),
        "needs_privileged_write": any(step in WRITE_STEPS for step in steps),
        # Conflict resolution needs full history and a merge attempt.
        "needs_merge_probe": "resolve_conflicts" in steps,
    }
