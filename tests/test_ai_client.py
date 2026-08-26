"""Model configuration, the response_format fallback and strict JSON parsing."""
from __future__ import annotations

import json

import pytest

from scripts.ai import client as ai_client
from scripts.ai.schemas import validate_metadata, validate_review
from scripts.common.errors import AIError, SchemaError
from tests.conftest import FakeAIClient

METADATA = {"summary": "Adds a thing.", "change_type": "feature", "risk": "low"}


class Rejected(Exception):
    """Stand-in for a provider 400 about response_format."""

    status_code = 400


# -- configuration ---------------------------------------------------------

def test_defaults_are_the_intended_production_values():
    assert ai_client.base_url({}) == "https://integrate.api.nvidia.com/v1"
    assert ai_client.metadata_model({}) == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert ai_client.review_model({}) == "nvidia/nemotron-3.5-lightning-30b-a3b"


def test_every_default_is_overridable():
    env = {
        "AI_BASE_URL": "https://example.invalid/v1",
        "AI_METADATA_MODEL": "vendor/meta",
        "AI_REVIEW_MODEL": "vendor/review",
    }
    assert ai_client.base_url(env) == "https://example.invalid/v1"
    assert ai_client.metadata_model(env) == "vendor/meta"
    assert ai_client.review_model(env) == "vendor/review"


def test_blank_overrides_fall_back_to_the_defaults():
    env = {"AI_BASE_URL": "  ", "AI_METADATA_MODEL": "", "AI_REVIEW_MODEL": " "}
    assert ai_client.base_url(env) == "https://integrate.api.nvidia.com/v1"
    assert ai_client.metadata_model(env) == "nvidia/nemotron-3.5-lightning-30b-a3b"


def test_a_missing_api_key_is_an_error_not_a_silent_empty_string():
    with pytest.raises(AIError, match="NVIDIA_API_KEY is not configured"):
        ai_client.api_key({})


def test_the_api_key_is_never_included_in_an_error_message():
    fake = FakeAIClient([Rejected("bad request"), Rejected("still bad")])
    with pytest.raises(AIError) as excinfo:
        ai_client.complete_json(
            model="m", system="s", user="u", validator=validate_metadata, client=fake,
            env={"NVIDIA_API_KEY": "nvapi-super-secret"},
        )
    assert "nvapi-super-secret" not in str(excinfo.value)


# -- structured-output fallback -------------------------------------------

def test_structured_mode_is_tried_first():
    fake = FakeAIClient([METADATA])
    result = ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata, client=fake
    )
    assert result["summary"] == "Adds a thing."
    assert fake.completions.calls[0]["response_format"] == {"type": "json_object"}


def test_a_response_format_rejection_retries_without_response_format():
    fake = FakeAIClient([Rejected("response_format is not supported"), METADATA])
    result = ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata, client=fake
    )
    assert result["change_type"] == "feature"
    assert len(fake.completions.calls) == 2
    assert "response_format" in fake.completions.calls[0]
    assert "response_format" not in fake.completions.calls[1]


def test_the_fallback_restates_the_json_contract_in_the_prompt():
    fake = FakeAIClient([Rejected("json mode unsupported"), METADATA])
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata, client=fake
    )
    retry_system = fake.completions.calls[1]["messages"][0]["content"]
    assert "single raw JSON object" in retry_system


def test_both_attempts_failing_raises_rather_than_returning_junk():
    fake = FakeAIClient([Rejected("nope"), Rejected("nope again")])
    with pytest.raises(AIError):
        ai_client.complete_json(
            model="m", system="s", user="u", validator=validate_metadata, client=fake
        )


def test_a_non_response_format_error_is_not_retried():
    class Unauthorized(Exception):
        status_code = 401

    fake = FakeAIClient([Unauthorized("bad credentials")])
    with pytest.raises(AIError):
        ai_client.complete_json(
            model="m", system="s", user="u", validator=validate_metadata, client=fake
        )
    assert len(fake.completions.calls) == 1


# -- strict JSON parsing ---------------------------------------------------

def test_a_markdown_fence_is_tolerated():
    payload = "```json\n" + json.dumps(METADATA) + "\n```"
    assert ai_client.extract_json(payload)["summary"] == "Adds a thing."


def test_leading_prose_around_an_object_is_tolerated():
    payload = "Here you go:\n" + json.dumps(METADATA) + "\nHope that helps."
    assert ai_client.extract_json(payload)["risk"] == "low"


@pytest.mark.parametrize(
    "payload",
    ["", "   ", "not json at all", "{unclosed", '{"a": }', "[1, 2, 3]", '"a string"', "null"],
)
def test_malformed_output_is_never_accepted(payload):
    with pytest.raises(AIError):
        ai_client.extract_json(payload)


def test_an_empty_model_response_is_an_error():
    # Every attempt returns nothing, so the final failure surfaces.
    fake = FakeAIClient(["", "", ""])
    with pytest.raises(AIError, match="empty response"):
        ai_client.complete_json(
            model="m", system="s", user="u", validator=validate_metadata, client=fake
        )


def test_valid_json_that_fails_the_schema_is_rejected():
    fake = FakeAIClient([{"change_type": "feature"}] * 3)  # no summary, every time
    with pytest.raises(SchemaError):
        ai_client.complete_json(
            model="m", system="s", user="u", validator=validate_metadata, client=fake
        )


# -- retry on unusable output ---------------------------------------------

def test_prose_instead_of_json_is_retried_with_a_stricter_instruction():
    # This is the live failure: "The model response did not contain a JSON object."
    fake = FakeAIClient(["Sure! Here is my analysis of the pull request.", METADATA])
    result = ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata, client=fake
    )
    assert result["summary"] == "Adds a thing."
    assert len(fake.completions.calls) == 2


def test_the_final_attempt_demands_json_only():
    fake = FakeAIClient(["nope", "still nope", METADATA])
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata, client=fake
    )
    final_system = fake.completions.calls[2]["messages"][0]["content"]
    assert "Output ONLY a JSON object" in final_system
    assert "first character must be '{'" in final_system


def test_a_schema_failure_is_retried_then_succeeds():
    fake = FakeAIClient([{"change_type": "feature"}, METADATA])
    result = ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata, client=fake
    )
    assert result["summary"] == "Adds a thing."


def test_the_error_quotes_what_came_back():
    fake = FakeAIClient(["I cannot help with that request."] * 3)
    with pytest.raises(AIError) as excinfo:
        ai_client.complete_json(
            model="m", system="s", user="u", validator=validate_metadata, client=fake
        )
    assert "I cannot help with that request." in str(excinfo.value)


def test_at_most_three_attempts_are_made():
    fake = FakeAIClient(["bad"] * 10)
    with pytest.raises(AIError):
        ai_client.complete_json(
            model="m", system="s", user="u", validator=validate_metadata, client=fake
        )
    assert len(fake.completions.calls) == 3


# -- reasoning models -----------------------------------------------------

def test_a_reasoning_block_is_stripped_before_parsing():
    payload = "<think>Let me consider the diff carefully...</think>" + json.dumps(METADATA)
    assert ai_client.extract_json(payload)["summary"] == "Adds a thing."


@pytest.mark.parametrize("tag", ["think", "thinking", "reasoning"])
def test_every_reasoning_tag_is_recognised(tag):
    payload = f"<{tag}>pondering</{tag}>" + json.dumps(METADATA)
    assert ai_client.extract_json(payload)["risk"] == "low"


def test_a_reasoning_block_is_case_insensitive():
    payload = "<THINK>pondering</THINK>" + json.dumps(METADATA)
    assert ai_client.extract_json(payload)["risk"] == "low"


def test_an_unterminated_reasoning_block_reports_the_real_cause():
    with pytest.raises(AIError, match="only a reasoning block"):
        ai_client.extract_json("<think>I should start by looking at the auth module")


def test_the_answer_after_an_unterminated_block_is_still_found():
    payload = "<think>pondering\n" + json.dumps(METADATA)
    assert ai_client.extract_json(payload)["summary"] == "Adds a thing."


def test_content_in_a_reasoning_field_is_used_when_content_is_empty():
    class Message:
        content = ""
        reasoning_content = json.dumps(METADATA)

    class Choice:
        message = Message()
        finish_reason = "stop"

    class Response:
        choices = [Choice()]

    content, finish = ai_client._message_text(Response())
    assert json.loads(content)["summary"] == "Adds a thing."
    assert finish == "stop"


def test_a_present_content_field_wins_over_reasoning():
    class Message:
        content = json.dumps(METADATA)
        reasoning_content = "irrelevant"

    class Choice:
        message = Message()
        finish_reason = "stop"

    class Response:
        choices = [Choice()]

    content, _ = ai_client._message_text(Response())
    assert json.loads(content)["summary"] == "Adds a thing."


def test_no_choices_yields_empty_content():
    class Response:
        choices = []

    assert ai_client._message_text(Response()) == ("", "")


# -- token budget ---------------------------------------------------------

def test_running_out_of_output_tokens_says_so():
    class Message:
        content = "Let me work through the diff step by step. First"
        reasoning_content = ""

    class Choice:
        message = Message()
        finish_reason = "length"

    class Response:
        choices = [Choice()]

    class Completions:
        calls: list = []

        def create(self, **kwargs):
            Completions.calls.append(kwargs)
            return Response()

    class Client:
        chat = type("C", (), {"completions": Completions()})()

    with pytest.raises(AIError, match="token output limit"):
        ai_client.complete_json(
            model="m", system="s", user="u", validator=validate_metadata, client=Client()
        )


def test_the_default_token_budget_leaves_room_for_reasoning():
    assert ai_client.max_output_tokens({}) == 8192


def test_the_token_budget_is_overridable():
    assert ai_client.max_output_tokens({"AI_MAX_TOKENS": "16384"}) == 16384


@pytest.mark.parametrize("value", ["", "  ", "lots", "0", "-5"])
def test_a_malformed_token_budget_falls_back_to_the_default(value):
    assert ai_client.max_output_tokens({"AI_MAX_TOKENS": value}) == 8192


def test_the_budget_reaches_the_provider():
    fake = FakeAIClient([METADATA])
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata, client=fake,
        env={"AI_MAX_TOKENS": "12345"},
    )
    assert fake.completions.calls[0]["max_tokens"] == 12345


def test_an_explicit_max_tokens_wins_over_the_variable():
    fake = FakeAIClient([METADATA])
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata, client=fake,
        max_tokens=999, env={"AI_MAX_TOKENS": "12345"},
    )
    assert fake.completions.calls[0]["max_tokens"] == 999


# -- preview --------------------------------------------------------------

def test_preview_collapses_whitespace_and_truncates():
    assert ai_client.preview("a\n\n  b") == "a b"
    long = ai_client.preview("x" * 500)
    assert long.endswith("…")
    assert len(long) == ai_client.PREVIEW_CHARS + 1


def test_the_validator_is_applied_to_the_parsed_object():
    fake = FakeAIClient([{"findings": [{"severity": "high", "title": "T", "path": "a.py"}]}])
    result = ai_client.complete_json(
        model="m",
        system="s",
        user="u",
        validator=lambda d: validate_review(d, changed_paths={"a.py"}),
        client=fake,
    )
    assert result["findings"][0]["severity"] == "high"


# -- model listing ---------------------------------------------------------

def test_list_models_returns_sorted_ids():
    class Page:
        data = [{"id": "b/two"}, {"id": "a/one"}]

    class Client:
        models = type("M", (), {"list": staticmethod(lambda: Page())})()

    assert ai_client.list_models(client=Client()) == ["a/one", "b/two"]


def test_list_models_surfaces_a_provider_error_as_ai_error():
    class Client:
        models = type("M", (), {"list": staticmethod(lambda: (_ for _ in ()).throw(RuntimeError()))})()

    with pytest.raises(AIError, match="Could not list models"):
        ai_client.list_models(client=Client())
