"""Quality result generation, including after a dependency install failure."""
from __future__ import annotations

import json

from scripts.common.commands import (
    CommandResult,
    run_commands,
    summarise,
    write_result,
    _sanitised_env,
)
from scripts.common.config import Command

INSTALL = Command(name="Frontend dependencies", run="npm ci", group="install")
LINT = Command(name="Frontend lint", run="npm run lint", group="lint")
TEST = Command(name="Frontend test", run="npm test", group="test")
BUILD = Command(name="Frontend build", run="npm run build", group="build")
ALL = [INSTALL, LINT, TEST, BUILD]


def runner_for(exit_codes):
    """Return a runner that yields the given exit codes in order."""
    codes = list(exit_codes)

    def runner(command, cwd, env, timeout):
        return codes.pop(0), f"output of {command}"

    return runner


def test_every_command_appears_in_the_result_when_all_pass():
    results = run_commands(ALL, runner=runner_for([0, 0, 0, 0]))
    assert [(r.name, r.status) for r in results] == [
        ("Frontend dependencies", "pass"),
        ("Frontend lint", "pass"),
        ("Frontend test", "pass"),
        ("Frontend build", "pass"),
    ]
    assert summarise(results)["status"] == "pass"


def test_an_install_failure_is_recorded_and_does_not_abort_the_report():
    results = run_commands(ALL, runner=runner_for([1]))
    payload = summarise(results)
    assert results[0].status == "fail"
    assert payload["status"] == "fail"
    # The later groups are present and explained, not missing.
    assert [(r.name, r.status) for r in results[1:]] == [
        ("Frontend lint", "skipped"),
        ("Frontend test", "skipped"),
        ("Frontend build", "skipped"),
    ]
    assert "Frontend dependencies failed" in results[1].reason


def test_the_result_file_is_still_written_when_the_first_command_fails(tmp_path):
    results = run_commands(ALL, runner=runner_for([1]))
    path = tmp_path / "quality-result.json"
    write_result(path, summarise(results))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "fail"
    assert len(payload["results"]) == 4


def test_a_lint_failure_does_not_stop_the_test_and_build_groups():
    # Only install is blocking; a lint failure must not hide the build result.
    results = run_commands(ALL, runner=runner_for([0, 1, 0, 0]))
    assert [(r.name, r.status) for r in results] == [
        ("Frontend dependencies", "pass"),
        ("Frontend lint", "fail"),
        ("Frontend test", "pass"),
        ("Frontend build", "pass"),
    ]
    assert summarise(results)["status"] == "fail"


def test_counts_are_reported():
    results = run_commands(ALL, runner=runner_for([0, 1, 0, 0]))
    counts = summarise(results)["counts"]
    assert counts == {"total": 4, "passed": 3, "failed": 1, "skipped": 0}


def test_no_configured_commands_is_not_run_rather_than_pass():
    assert summarise([])["status"] == "not_run"


def test_a_timeout_is_recorded_as_a_failure():
    import subprocess

    def timing_out(command, cwd, env, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    results = run_commands([LINT], runner=timing_out, timeout=5)
    assert results[0].status == "fail"
    assert results[0].exit_code == 124
    assert "Timed out" in results[0].output_tail


def test_command_output_is_truncated():
    def noisy(command, cwd, env, timeout):
        return 0, "x" * 50_000

    results = run_commands([LINT], runner=noisy)
    assert len(results[0].output_tail) < 5000
    assert results[0].output_tail.startswith("...")


def test_credential_shaped_variables_are_stripped_from_the_command_environment(monkeypatch):
    monkeypatch.setenv("GH_APP_PRIVATE_KEY", "secret")
    monkeypatch.setenv("NVIDIA_API_KEY", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("DOCKER_PASSWORD", "secret")
    monkeypatch.setenv("AWS_SECRET", "secret")
    monkeypatch.setenv("MY_API_KEY", "secret")
    monkeypatch.setenv("HARMLESS_VAR", "keep")

    env = _sanitised_env()
    for blocked in (
        "GH_APP_PRIVATE_KEY", "NVIDIA_API_KEY", "GITHUB_TOKEN",
        "DOCKER_PASSWORD", "AWS_SECRET", "MY_API_KEY",
    ):
        assert blocked not in env, blocked
    assert env["HARMLESS_VAR"] == "keep"
    assert env["CI"] == "true"


def test_the_sanitised_environment_reaches_the_runner(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "secret")
    seen = {}

    def capture(command, cwd, env, timeout):
        seen.update(env)
        return 0, ""

    run_commands([LINT], runner=capture)
    assert "NVIDIA_API_KEY" not in seen


def test_command_result_passed_property():
    assert CommandResult("a", "lint", "x", "pass").passed
    assert not CommandResult("a", "lint", "x", "skipped").passed
