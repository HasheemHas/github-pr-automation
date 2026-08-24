"""Weak-title detection and the title replacement decision."""
from __future__ import annotations

import pytest

from scripts.common.titles import is_weak_title, title_decision

WEAK = [
    "update",
    "Update",
    "  update  ",
    "updates",
    "changes",
    "new changes",
    "some changes",
    "minor changes",
    "fix",
    "fixes",
    "bugfix",
    "test",
    "tests",
    "testing",
    "feature",
    "feat",
    "new feature",
    "final",
    "final changes",
    "wip",
    "WIP",
    "work in progress",
    "misc",
    "cleanup",
    "refactor",
    "chore",
    "patch",
    "hotfix",
    "quick fix",
    "please review",
    "done",
    "",
    "   ",
    ".",
    "update.",
    "Update!",
]

STRONG = [
    "fix(auth): prevent refresh token reuse",
    "feat(auth): add refresh-token validation",
    "feat(auth): improve login authentication",
    "fix: correct token expiry comparison in the auth middleware",
    "Add rate limiting to the login endpoint",
    "Remove the deprecated payroll export job",
    "refactor(chat): split the websocket handler into two services",
    "docs: document the tenant isolation model",
    "perf(search): cache the employee directory lookup",
    "revert: restore the previous session timeout",
]


@pytest.mark.parametrize("title", WEAK)
def test_placeholder_titles_are_weak(title):
    assert is_weak_title(title)


@pytest.mark.parametrize("title", STRONG)
def test_descriptive_titles_are_strong(title):
    assert not is_weak_title(title)


@pytest.mark.parametrize(
    "title",
    ["Update auth.py", "Update README.md", "Create config.yml", "Delete old_file.py",
     "feature/refresh-tokens", "patch-1", "branch-42", "a1b2c3d4e5f6"],
)
def test_machine_generated_titles_are_weak(title):
    assert is_weak_title(title)


@pytest.mark.parametrize("title", ["#123", "JIRA-456", "abc-12"])
def test_a_bare_ticket_reference_is_weak(title):
    assert is_weak_title(title)


def test_a_conventional_header_with_a_placeholder_description_is_still_weak():
    assert is_weak_title("fix: bug")
    assert is_weak_title("feat: feature")
    assert is_weak_title("chore: update")


def test_a_conventional_header_needs_more_than_one_word():
    assert is_weak_title("fix: typo")
    assert not is_weak_title("fix: correct typo in tenant name")


def test_a_short_but_specific_conventional_title_is_kept():
    # Two description words is the floor, and scoped headers pass it.
    assert not is_weak_title("fix(auth): token expiry")


# -- decision --------------------------------------------------------------

def test_a_weak_human_title_is_replaced():
    replace, reason = title_decision("update login", "feat(auth): improve login authentication")
    assert replace
    assert "placeholder" in reason


def test_a_good_human_title_is_kept():
    replace, reason = title_decision(
        "fix(auth): prevent refresh token reuse", "fix(auth): correct token handling"
    )
    assert not replace
    assert "descriptive" in reason


def test_an_empty_title_is_replaced():
    replace, _ = title_decision("", "feat(auth): add refresh-token validation")
    assert replace


def test_a_generic_ai_title_never_replaces_a_human_one():
    # Trading one placeholder for another is not an improvement.
    replace, reason = title_decision("update", "changes")
    assert not replace
    assert "itself too generic" in reason


def test_no_ai_title_means_no_change():
    replace, reason = title_decision("update", "")
    assert not replace
    assert "no title" in reason


def test_an_identical_title_is_not_rewritten():
    replace, reason = title_decision(
        "fix(auth): prevent reuse", "  fix(auth): prevent reuse  "
    )
    assert not replace
    assert "matches" in reason


def test_policy_always_replaces_a_good_title():
    replace, reason = title_decision(
        "fix(auth): prevent refresh token reuse",
        "fix(auth): correct refresh token comparison",
        policy="always",
    )
    assert replace
    assert "always" in reason


def test_policy_never_keeps_even_a_placeholder():
    replace, reason = title_decision("update", "feat(auth): add validation", policy="never")
    assert not replace
    assert "never" in reason


def test_policy_always_still_refuses_a_generic_ai_title():
    replace, _ = title_decision("update login flow properly", "changes", policy="always")
    assert not replace


def test_an_unknown_policy_is_conservative():
    replace, reason = title_decision("update", "feat: add thing here", policy="aggressive")
    assert not replace
    assert "Unknown title policy" in reason


def test_the_documented_example_end_to_end():
    # "update login" -> replaced; "fix(auth): prevent refresh token reuse" -> kept.
    assert title_decision("update login", "feat(auth): improve login authentication")[0]
    assert not title_decision(
        "fix(auth): prevent refresh token reuse", "feat(auth): improve login authentication"
    )[0]
