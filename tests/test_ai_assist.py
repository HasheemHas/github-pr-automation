"""The optional model layer over the deterministic analysis."""
from __future__ import annotations

from scripts.automation import ai_assist
from scripts.automation.analyze import analyse
from scripts.common.dispatch import plan as build_plan
from tests.conftest import FakeAIClient
from tests.test_deterministic_automation import CONTEXT, DIFF

ENV = {"NVIDIA_API_KEY": "test-key"}

AI_METADATA = {
    "title": "fix(auth): reject reused login tokens",
    "summary": "Login now rejects a token that was already used.",
    "description": "Adds a reuse check to the token validator.",
    "risk": "high",
    "review_focus": ["Token reuse check in `backend/auth.py`."],
}


class Boom(Exception):
    pass


def _plan(action):
    return build_plan(action)


def test_disabled_without_key_or_when_switched_off():
    assert not ai_assist.enabled(None)
    assert not ai_assist.enabled({})
    assert not ai_assist.enabled({"NVIDIA_API_KEY": "k", "AI_ENABLED": "false"})
    assert ai_assist.enabled({"NVIDIA_API_KEY": "k"})


def test_no_env_means_no_model_call():
    # A client that would fail if called proves the rules path makes no request.
    result = analyse(_plan("metadata"), CONTEXT, DIFF, client=FakeAIClient([]))
    assert result["engine"] == "deterministic-python-rules-v1"
    assert "ai" not in result


def test_model_prose_replaces_rule_prose_but_not_measured_fields():
    rules = analyse(_plan("metadata"), CONTEXT, DIFF)["metadata"]["data"]
    result = analyse(_plan("metadata"), CONTEXT, DIFF, env=ENV,
                     client=FakeAIClient([AI_METADATA]))
    data = result["metadata"]["data"]
    assert result["engine"].endswith("+ai")
    assert data["title"] == AI_METADATA["title"]
    assert data["summary"] == AI_METADATA["summary"]
    # Risk is never taken from the model.
    assert data["risk"] == rules["risk"]
    assert result["ai"]["sections"]["metadata"]["status"] == "ok"


def test_model_failure_keeps_rule_metadata():
    rules = analyse(_plan("metadata"), CONTEXT, DIFF)["metadata"]["data"]
    result = analyse(_plan("metadata"), CONTEXT, DIFF, env=ENV,
                     client=FakeAIClient([Boom()]))
    assert result["metadata"]["status"] == "ok"
    assert result["metadata"]["data"]["summary"] == rules["summary"]
    assert result["ai"]["sections"]["metadata"]["status"] == "error"


def test_model_findings_are_advisory_and_deduplicated():
    review = {"findings": [{
        "severity": "critical", "category": "security", "path": "backend/auth.py",
        "line": 3, "title": "Injected flaw", "body": "",
    }, {
        "severity": "low", "category": "style", "path": "backend/auth.py",
        "line": 2, "title": "Possible committed secret", "body": "",
    }]}
    result = analyse(_plan("review"), CONTEXT, DIFF, env=ENV, client=FakeAIClient([review]))
    findings = result["review"]["data"]["findings"]
    ai = [f for f in findings if f.get("source") == "ai"]
    assert len(ai) == 1  # the second duplicates a rule finding
    assert ai[0]["severity"] == "medium"
    assert ai[0]["model_severity"] == "critical"
    # Rule findings are untouched.
    assert any(f.get("rule") == "secret-literal" for f in findings)


def test_model_findings_can_block_when_opted_in():
    review = {"findings": [{"severity": "high", "path": "backend/auth.py", "line": 3,
                            "title": "Injected flaw"}]}
    env = dict(ENV, AI_FINDINGS_CAN_BLOCK="true")
    result = analyse(_plan("review"), CONTEXT, DIFF, env=env, client=FakeAIClient([review]))
    ai = [f for f in result["review"]["data"]["findings"] if f.get("source") == "ai"]
    assert ai[0]["severity"] == "high"


def test_model_review_failure_keeps_rule_findings():
    rules = analyse(_plan("review"), CONTEXT, DIFF)["review"]["data"]["findings"]
    result = analyse(_plan("review"), CONTEXT, DIFF, env=ENV,
                     client=FakeAIClient([Boom(), Boom(), Boom()]))
    assert result["review"]["data"]["findings"] == rules
    assert result["ai"]["sections"]["review"]["status"] == "error"
