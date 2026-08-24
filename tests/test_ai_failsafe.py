"""Fail-safe AI behaviour: a failure never becomes a successful write."""
from __future__ import annotations

import pytest

from scripts.ai.analyze import analyse, load_plan
from scripts.ai.check_models import check
from scripts.ai.resolve import resolve
from scripts.ai.schemas import validate_patch
from scripts.common.dispatch import plan as build_plan
from scripts.common.errors import AIError, SchemaError
from tests.conftest import FakeAIClient

CONTEXT = {
    "repository": "org/repo",
    "number": 7,
    "title": "Add thing",
    "body": "",
    "author": "dev",
    "head_sha": "a" * 40,
    "files": ["backend/app/main.py"],
    "commits": ["Add thing"],
}


class Boom(Exception):
    status_code = 500


def test_a_review_failure_produces_no_findings():
    result = analyse(build_plan("review"), CONTEXT, "diff", client=FakeAIClient([Boom(), Boom()]))
    assert result["review"]["status"] == "error"
    assert result["review"]["data"] is None


def test_a_metadata_failure_leaves_metadata_absent_rather_than_empty():
    result = analyse(build_plan("metadata"), CONTEXT, "diff", client=FakeAIClient([Boom(), Boom()]))
    assert result["metadata"]["status"] == "error"
    assert result["metadata"]["data"] is None


def test_a_malformed_metadata_response_is_an_error_not_a_blank_summary():
    result = analyse(
        build_plan("metadata"), CONTEXT, "diff", client=FakeAIClient(["not json"])
    )
    assert result["metadata"]["status"] == "error"
    assert result["metadata"]["data"] is None


def test_one_failing_section_does_not_take_down_the_others():
    fake = FakeAIClient(
        [
            {"summary": "ok", "change_type": "fix", "risk": "low"},  # metadata
            Boom(),  # review: a 500 is not a response_format problem, so no retry
            {"changed": [], "existing_tests": [], "gaps": ["edge case"]},  # tests
        ]
    )
    result = analyse(build_plan("all"), CONTEXT, "diff", client=fake)
    assert result["metadata"]["status"] == "ok"
    assert result["review"]["status"] == "error"
    assert result["tests"]["status"] == "ok"


def test_no_patch_is_produced_when_there_are_no_findings():
    fake = FakeAIClient([{"findings": []}])
    execution = dict(build_plan("fix"))
    execution["fix_allowed_paths"] = ["backend/app/**/*.py"]
    result = analyse(execution, CONTEXT, "diff", client=fake)
    assert result["fix"]["status"] == "skipped"
    assert result["fix"]["data"] is None


def test_no_patch_is_produced_when_the_review_itself_failed():
    fake = FakeAIClient([Boom(), Boom()])
    execution = dict(build_plan("fix"))
    execution["fix_allowed_paths"] = ["backend/app/**/*.py"]
    result = analyse(execution, CONTEXT, "diff", client=fake)
    assert result["review"]["status"] == "error"
    assert result["fix"]["status"] == "skipped"


def test_no_patch_is_produced_without_a_configured_allowlist():
    fake = FakeAIClient([{"findings": [{"severity": "high", "title": "T",
                                       "path": "backend/app/main.py"}]}])
    result = analyse(dict(build_plan("fix")), CONTEXT, "diff", client=fake)
    assert result["fix"]["status"] == "skipped"
    assert "fix_allowed_paths" in result["fix"]["reason"]


def test_a_fix_failure_yields_no_patch():
    fake = FakeAIClient(
        [
            {"findings": [{"severity": "high", "title": "T", "path": "backend/app/main.py"}]},
            Boom(),
            Boom(),
        ]
    )
    execution = dict(build_plan("fix"))
    execution["fix_allowed_paths"] = ["backend/app/**/*.py"]
    result = analyse(execution, CONTEXT, "diff", client=fake)
    assert result["fix"]["status"] == "error"
    assert result["fix"]["data"] is None


def test_security_action_narrows_the_review_scope():
    fake = FakeAIClient([{"findings": []}])
    result = analyse(build_plan("security"), CONTEXT, "diff", client=fake)
    assert result["review_scope"] == "security"
    assert "security findings" in fake.completions.calls[0]["messages"][1]["content"]


def test_findings_outside_the_changed_files_are_dropped():
    fake = FakeAIClient(
        [
            {
                "findings": [
                    {"severity": "high", "title": "real", "path": "backend/app/main.py"},
                    {"severity": "high", "title": "invented", "path": "not/in/pr.py"},
                ]
            }
        ]
    )
    result = analyse(build_plan("review"), CONTEXT, "diff", client=fake)
    titles = [f["title"] for f in result["review"]["data"]["findings"]]
    assert titles == ["real"]
    assert result["review"]["data"]["dropped"] == 1


# -- conflict resolution ---------------------------------------------------

def test_no_conflicts_means_no_resolution():
    result = resolve(CONTEXT, [], ["backend/**"])
    assert result["status"] == "skipped"
    assert result["data"] is None


def test_conflicts_outside_the_allowlist_are_refused():
    result = resolve(CONTEXT, [{"path": ".github/workflows/ci.yml"}], ["backend/**"])
    assert result["status"] == "refused"
    assert result["data"] is None


def test_an_unconfident_resolution_is_not_applied():
    fake = FakeAIClient([{"patch": "--- a/x\n+++ b/x\n", "confident": False}])
    result = resolve(
        CONTEXT, [{"path": "backend/app/main.py"}], ["backend/**"], client=fake
    )
    assert result["status"] == "skipped"


def test_a_resolution_touching_disallowed_paths_is_refused():
    fake = FakeAIClient(
        [{"patch": "--- a/x\n+++ b/x\n", "confident": True, "files": [".github/workflows/ci.yml"]}]
    )
    result = resolve(
        CONTEXT, [{"path": "backend/app/main.py"}], ["backend/**"], client=fake
    )
    assert result["status"] == "refused"


def test_a_resolution_error_yields_no_patch():
    fake = FakeAIClient([Boom(), Boom()])
    result = resolve(CONTEXT, [{"path": "backend/app/main.py"}], ["backend/**"], client=fake)
    assert result["status"] == "error"
    assert result["data"] is None


# -- patch schema ----------------------------------------------------------

def test_a_patch_must_be_a_unified_diff():
    with pytest.raises(SchemaError, match="unified diff"):
        validate_patch({"patch": "just some prose"})


def test_an_empty_patch_is_a_valid_no_change_answer():
    assert validate_patch({"patch": ""})["patch"] == ""


def test_an_oversized_patch_is_rejected():
    with pytest.raises(SchemaError, match="exceeds"):
        validate_patch({"patch": "--- a/x\n" + "+" * 300_000})


# -- plan loading and model diagnostics -----------------------------------

def test_a_malformed_plan_is_rejected():
    with pytest.raises(AIError, match="not valid JSON"):
        load_plan({"PR_AUTOMATION_PLAN": "{oops"})


def test_a_missing_plan_is_an_empty_plan():
    assert load_plan({}) == {}


def test_model_diagnostic_flags_a_missing_model():
    verdict = check(["a/one"], {"AI_REVIEW_MODEL": "b/two", "AI_METADATA_MODEL": "a/one"})
    assert verdict == {"AI_REVIEW_MODEL": False, "AI_METADATA_MODEL": True}
