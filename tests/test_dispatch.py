"""Action dispatch: every action does something, unknown actions fail."""
from __future__ import annotations

import pytest

from scripts.common.dispatch import ACTIONS, STEPS, plan
from scripts.common.errors import DispatchError

SINGLE = {
    "metadata": "metadata",
    "labels": "labels",
    "reviewers": "reviewers",
    "review": "review",
    "security": "security",
    "quality": "quality",
    "tests": "tests",
}


@pytest.mark.parametrize("action,step", SINGLE.items())
def test_a_single_action_runs_only_its_own_step(action, step):
    result = plan(action)
    assert result["steps"] == [step]
    assert [name for name in STEPS if result[name]] == [step]


def test_all_runs_the_full_read_only_pipeline():
    result = plan("all")
    assert result["steps"] == [
        "metadata", "labels", "reviewers", "review", "security", "quality", "tests"
    ]


def test_all_never_writes_to_git():
    result = plan("all")
    assert result["fix"] is False
    assert result["resolve_conflicts"] is False
    assert result["needs_privileged_write"] is False


def test_fix_reviews_first_so_it_has_findings_to_fix():
    result = plan("fix")
    assert result["review"] is True
    assert result["fix"] is True
    assert result["needs_privileged_write"] is True
    assert result["needs_target_source"] is True


def test_resolve_conflicts_is_its_own_pipeline():
    result = plan("resolve-conflicts")
    assert result["resolve_conflicts"] is True
    assert result["needs_merge_probe"] is True
    assert result["needs_privileged_write"] is True
    assert result["review"] is False


@pytest.mark.parametrize("action", ["", "  ", "ALL", "everything", "fix-all", "labels;fix", None])
def test_an_unknown_action_fails_with_the_supported_list(action):
    with pytest.raises(DispatchError) as excinfo:
        plan(action)
    assert "Supported actions" in str(excinfo.value)
    for name in ACTIONS:
        assert name in str(excinfo.value)


def test_an_action_is_never_silently_ignored():
    # Every accepted action must expand to at least one step.
    for action in ACTIONS:
        assert plan(action)["steps"], f"{action} expanded to nothing"


def test_action_names_are_trimmed():
    assert plan("  labels  ")["action"] == "labels"


@pytest.mark.parametrize(
    "action,needs_analysis",
    [("all", True), ("metadata", True), ("review", True), ("tests", True),
     ("labels", False), ("reviewers", False), ("quality", False)],
)
def test_analysis_is_only_requested_when_a_step_needs_it(action, needs_analysis):
    assert plan(action)["needs_analysis"] is needs_analysis


@pytest.mark.parametrize(
    "action,needs_source",
    [("quality", True), ("security", True), ("fix", True), ("resolve-conflicts", True),
     ("metadata", False), ("labels", False), ("reviewers", False), ("tests", False)],
)
def test_target_source_is_only_checked_out_when_needed(action, needs_source):
    assert plan(action)["needs_target_source"] is needs_source


def test_every_declared_step_is_a_known_step():
    for steps in ACTIONS.values():
        for step in steps:
            assert step in STEPS
