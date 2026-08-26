"""AI request logging."""
from __future__ import annotations

import json

import pytest

from scripts.ai import client as ai_client
from scripts.ai.schemas import validate_metadata
from scripts.ai.telemetry import Recorder, usage_of
from scripts.common.errors import AIError
from tests.conftest import FakeAIClient

METADATA = {"summary": "Adds a thing.", "change_type": "feature", "risk": "low"}


class Clock:
    """Monotonic stub advancing 0.25s per read."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.25
        return self.t


def recorder():
    return Recorder(clock=Clock())


class Rejected(Exception):
    status_code = 400


# -- recording -------------------------------------------------------------

def test_a_successful_call_is_recorded():
    rec = recorder()
    ai_client.complete_json(
        model="vendor/m", system="sys", user="prompt text",
        validator=validate_metadata, client=FakeAIClient([METADATA]),
        recorder=rec, section="metadata",
    )
    assert len(rec.records) == 1
    entry = rec.records[0]
    assert entry.section == "metadata"
    assert entry.model == "vendor/m"
    assert entry.outcome == "ok"
    assert entry.attempt == 1 and entry.attempts_total == 3
    assert entry.structured is True
    assert entry.prompt_chars == len("prompt text")
    assert entry.response_chars > 0
    assert entry.duration_ms > 0


def test_every_attempt_is_recorded_with_its_outcome():
    rec = recorder()
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata,
        client=FakeAIClient(["prose, not json", METADATA]), recorder=rec, section="metadata",
    )
    assert [r.outcome for r in rec.records] == ["parse_error", "ok"]
    assert [r.attempt for r in rec.records] == [1, 2]
    # The retry drops structured mode.
    assert [r.structured for r in rec.records] == [True, False]


def test_a_provider_error_is_recorded_with_its_type():
    rec = recorder()
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata,
        client=FakeAIClient([Rejected("response_format unsupported"), METADATA]),
        recorder=rec,
    )
    assert rec.records[0].outcome == "provider_error"
    assert "Rejected" in rec.records[0].error


def test_a_schema_failure_is_distinguished_from_a_parse_failure():
    rec = recorder()
    with pytest.raises(AIError):
        ai_client.complete_json(
            model="m", system="s", user="u", validator=validate_metadata,
            client=FakeAIClient([{"change_type": "feature"}] * 3), recorder=rec,
        )
    assert {r.outcome for r in rec.records} == {"schema_error"}


def test_an_empty_response_is_recorded_as_empty():
    rec = recorder()
    with pytest.raises(AIError):
        ai_client.complete_json(
            model="m", system="s", user="u", validator=validate_metadata,
            client=FakeAIClient(["", "", ""]), recorder=rec,
        )
    assert {r.outcome for r in rec.records} == {"empty"}


def test_a_truncated_response_is_recorded_as_truncated():
    class Message:
        content = "Let me think about this"
        reasoning_content = ""

    class Choice:
        message = Message()
        finish_reason = "length"

    class Response:
        choices = [Choice()]
        usage = None

    class Completions:
        def create(self, **kwargs):
            return Response()

    class Client:
        chat = type("C", (), {"completions": Completions()})()

    rec = recorder()
    with pytest.raises(AIError, match="token output limit"):
        ai_client.complete_json(
            model="m", system="s", user="u", validator=validate_metadata,
            client=Client(), recorder=rec,
        )
    assert {r.outcome for r in rec.records} == {"truncated"}
    assert rec.records[0].finish_reason == "length"


def test_a_failed_response_is_quoted_but_a_successful_one_is_not():
    rec = recorder()
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata,
        client=FakeAIClient(["I refuse", METADATA]), recorder=rec,
    )
    assert "I refuse" in rec.records[0].response_preview
    assert rec.records[1].response_preview == ""


# -- prompt privacy --------------------------------------------------------

def test_prompts_are_not_logged_by_default():
    rec = recorder()
    ai_client.complete_json(
        model="m", system="sys text", user="secret-ish diff content",
        validator=validate_metadata, client=FakeAIClient([METADATA]), recorder=rec,
    )
    assert rec.records[0].prompt == ""
    assert rec.records[0].system == ""
    # The size is still recorded, which is what diagnosis needs.
    assert rec.records[0].prompt_chars == len("secret-ish diff content")


def test_prompts_are_logged_when_explicitly_enabled():
    rec = Recorder(log_prompts=True, clock=Clock())
    ai_client.complete_json(
        model="m", system="sys text", user="diff content",
        validator=validate_metadata, client=FakeAIClient([METADATA]), recorder=rec,
    )
    assert rec.records[0].prompt == "diff content"
    assert rec.records[0].system == "sys text"


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("", False), ("maybe", False),
])
def test_the_prompt_logging_flag_is_read_from_the_environment(value, expected):
    assert Recorder.from_env({"AI_LOG_PROMPTS": value}).log_prompts is expected


def test_no_flag_means_prompts_stay_out_of_the_log():
    assert Recorder.from_env({}).log_prompts is False


# -- token usage -----------------------------------------------------------

def test_token_usage_is_captured_when_the_provider_reports_it():
    class Usage:
        prompt_tokens = 1200
        completion_tokens = 340
        total_tokens = 1540

    class Response:
        usage = Usage()

    assert usage_of(Response()) == {
        "prompt_tokens": 1200, "completion_tokens": 340, "total_tokens": 1540
    }


def test_missing_usage_is_none_not_zero():
    class Response:
        usage = None

    assert usage_of(Response())["total_tokens"] is None
    assert usage_of(object())["prompt_tokens"] is None


def test_non_numeric_usage_is_ignored():
    class Usage:
        prompt_tokens = "lots"
        completion_tokens = None
        total_tokens = 5

    class Response:
        usage = Usage()

    counts = usage_of(Response())
    assert counts["prompt_tokens"] is None
    assert counts["total_tokens"] == 5


# -- reporting -------------------------------------------------------------

def test_totals_aggregate_across_requests():
    rec = recorder()
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata,
        client=FakeAIClient(["bad", METADATA]), recorder=rec,
    )
    totals = rec.totals
    assert totals["requests"] == 2
    assert totals["succeeded"] == 1
    assert totals["failed"] == 1
    assert totals["retries"] == 1
    assert totals["duration_ms"] > 0


def test_totals_on_an_empty_recorder():
    totals = Recorder().totals
    assert totals["requests"] == 0
    assert totals["completion_tokens"] is None


def test_the_console_line_names_the_essentials():
    rec = recorder()
    ai_client.complete_json(
        model="vendor/m", system="s", user="u", validator=validate_metadata,
        client=FakeAIClient([METADATA]), recorder=rec, section="review[2/4]",
    )
    line = rec.line(rec.records[0])
    for fragment in ("review[2/4]", "attempt 1/3", "model=vendor/m",
                     "mode=json_object", "outcome=ok", "ms"):
        assert fragment in line


def test_the_console_line_includes_the_error_when_there_is_one():
    rec = recorder()
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata,
        client=FakeAIClient(["nope", METADATA]), recorder=rec,
    )
    assert "error=" in rec.line(rec.records[0])


def test_rows_match_the_headers():
    rec = recorder()
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata,
        client=FakeAIClient([METADATA]), recorder=rec, section="metadata",
    )
    rows = rec.rows()
    assert len(rows) == 1
    assert len(rows[0]) == len(Recorder.HEADERS)


# -- the jsonl artifact ----------------------------------------------------

def test_the_log_is_written_as_one_json_object_per_line(tmp_path):
    rec = recorder()
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata,
        client=FakeAIClient(["bad", METADATA]), recorder=rec, section="metadata",
    )
    path = tmp_path / "ai-log.jsonl"
    rec.write(path)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["section"] == "metadata"
    assert first["outcome"] == "parse_error"
    assert "response_preview" in first


def test_empty_fields_are_omitted_from_the_log(tmp_path):
    rec = recorder()
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata,
        client=FakeAIClient([METADATA]), recorder=rec,
    )
    path = tmp_path / "ai-log.jsonl"
    rec.write(path)
    entry = json.loads(path.read_text(encoding="utf-8").strip())
    # No prompt logged, so the key is absent rather than an empty string.
    assert "prompt" not in entry
    assert "error" not in entry


def test_the_log_never_contains_the_api_key(tmp_path):
    rec = Recorder(log_prompts=True, clock=Clock())
    ai_client.complete_json(
        model="m", system="s", user="u", validator=validate_metadata,
        client=FakeAIClient([METADATA]), recorder=rec,
        env={"NVIDIA_API_KEY": "nvapi-super-secret"},
    )
    path = tmp_path / "ai-log.jsonl"
    rec.write(path)
    assert "nvapi-super-secret" not in path.read_text(encoding="utf-8")


def test_writing_an_empty_log_creates_an_empty_file(tmp_path):
    path = tmp_path / "nested" / "ai-log.jsonl"
    Recorder().write(path)
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == ""


# -- integration -----------------------------------------------------------

def test_analyse_records_a_request_per_batch():
    from scripts.ai.analyze import analyse
    from scripts.common.dispatch import plan as build_plan

    paths = ["a.py", "b.py"]
    diff = "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1,1 +1,3 @@\n+x\n+y\n" for p in paths
    )
    rec = recorder()
    analyse(
        build_plan("review"),
        {"files": paths, "head_sha": "a" * 40, "title": "t", "body": "", "author": "d",
         "commits": []},
        diff,
        client=FakeAIClient([{"findings": []}, {"findings": []}]),
        env={"AI_REVIEW_BATCH_CHARS": "80", "AI_REVIEW_BATCH_FILES": "1",
             "AI_REVIEW_MAX_BATCHES": "10"},
        recorder=rec,
    )
    assert [r.section for r in rec.records] == ["review[1/2]", "review[2/2]"]
    assert all(r.outcome == "ok" for r in rec.records)
