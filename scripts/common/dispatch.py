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
    "fix",
    "resolve_conflicts",
)

#: ``all`` deliberately excludes ``fix`` and ``resolve_conflicts``: both write to
#: the target repository and must be requested explicitly.
ACTIONS: dict[str, tuple[str, ...]] = {
    "all": ("metadata", "labels", "reviewers", "review", "security", "quality", "tests"),
    "metadata": ("metadata",),
    "labels": ("labels",),
    "reviewers": ("reviewers",),
    "review": ("review",),
    "security": ("security",),
    "quality": ("quality",),
    "tests": ("tests",),
    # A fix needs findings to fix, so review always runs first.
    "fix": ("review", "fix"),
    "resolve-conflicts": ("resolve_conflicts",),
}

#: Steps that call the AI provider.
AI_STEPS = frozenset({"metadata", "review", "security", "tests", "fix", "resolve_conflicts"})

#: Steps that need the target repository working tree checked out.
SOURCE_STEPS = frozenset({"quality", "security", "fix", "resolve_conflicts"})

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
        # ``security`` shares the AI review call, so review analysis is needed too.
        "needs_ai": any(step in AI_STEPS for step in steps),
        "needs_target_source": any(step in SOURCE_STEPS for step in steps),
        "needs_privileged_write": any(step in WRITE_STEPS for step in steps),
        # Conflict resolution needs full history and a merge attempt.
        "needs_merge_probe": "resolve_conflicts" in steps,
    }
