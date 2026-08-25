"""A scanner that cannot run must never read as a security failure.

Regression suite for the live run where `gitleaks dir` (unsupported by the pinned
8.18.4 binary) exited 1 and was reported as Security FAIL with a blank detail.
"""
from __future__ import annotations

import json

import pytest

from scripts.common.config import Command, Target
from scripts.common.dispatch import plan as build_plan
from scripts.github.report import ERROR, FAIL, PARTIAL, PASS, build_sections, overall
from scripts.security import run as security_run

GITLEAKS = Command(
    name="gitleaks", run="gitleaks dir . --no-banner --redact --exit-code 1", group="security"
)
SEMGREP = Command(name="semgrep", run="semgrep scan --error --quiet --config auto .",
                  group="security")


def runner_for(mapping):
    """Runner that matches on a substring of the command."""

    def run(command, cwd, env, timeout):
        for needle, result in mapping.items():
            if needle in command:
                return result
        return (0, "")

    return run


# -- probing ---------------------------------------------------------------

def test_a_working_cli_passes_the_probe():
    usable, broken = security_run.probe_scanners(
        [GITLEAKS], cwd=".", runner=runner_for({"--help": (0, "usage: gitleaks dir")})
    )
    assert [c.name for c in usable] == ["gitleaks"]
    assert broken == []


def test_an_unsupported_subcommand_is_an_error_not_a_finding():
    # This is the exact failure from the live run.
    usable, broken = security_run.probe_scanners(
        [GITLEAKS],
        cwd=".",
        runner=runner_for({"--help": (1, 'Error: unknown command "dir" for "gitleaks"')}),
    )
    assert usable == []
    assert len(broken) == 1
    assert broken[0].status == "error"
    assert broken[0].name == "gitleaks"
    assert 'unknown command "dir"' in broken[0].reason


def test_the_probe_never_runs_the_real_scan_for_a_broken_cli():
    calls = []

    def run(command, cwd, env, timeout):
        calls.append(command)
        return (1, "unknown command")

    security_run.probe_scanners([GITLEAKS], cwd=".", runner=run)
    # Only the probe ran; the scan itself was never attempted.
    assert calls == ["gitleaks dir --help"]


def test_one_broken_scanner_does_not_block_a_working_one():
    usable, broken = security_run.probe_scanners(
        [GITLEAKS, SEMGREP],
        cwd=".",
        runner=runner_for({"gitleaks dir --help": (1, "unknown command"),
                           "semgrep --version": (0, "1.2.3")}),
    )
    assert [c.name for c in usable] == ["semgrep"]
    assert [c.name for c in broken] == ["gitleaks"]


def test_an_extra_config_command_is_not_probed():
    extra = Command(name="pip-audit", run="pip-audit", group="security")
    usable, broken = security_run.probe_scanners([extra], cwd=".", runner=runner_for({}))
    assert [c.name for c in usable] == ["pip-audit"]
    assert broken == []


def test_gitleaks_runs_verbosely_but_redacted():
    _, _, command = security_run.SCANNERS["gitleaks"]
    # --verbose names the file and rule; --redact keeps the secret value out of
    # every log and artifact.
    assert "--verbose" in command
    assert "--redact" in command


# -- finding counts --------------------------------------------------------

@pytest.mark.parametrize(
    "output,expected",
    [
        ("INF scanned ~856030220 bytes in 9.63s\nWRN leaks found: 4", 4),
        ("WRN leaks found: 1", 1),
        ("leaks found: 0", 0),
        ("12 findings in 3 files", 12),
        ("Total detected issues: 7", 7),
        ("no leaks found", None),
        ("", None),
    ],
)
def test_a_stated_finding_count_is_extracted(output, expected):
    assert security_run.finding_count(output) == expected


def test_the_count_is_folded_into_the_reason():
    from scripts.common.commands import CommandResult

    results = [
        CommandResult("gitleaks", "security", "x", "fail", 1,
                      output_tail="WRN leaks found: 4")
    ]
    security_run.annotate_counts(results)
    assert "4 finding(s)" in results[0].reason
    assert "job summary" in results[0].reason


def test_an_existing_reason_is_not_overwritten():
    from scripts.common.commands import CommandResult

    results = [CommandResult("gitleaks", "security", "x", "error", reason="CLI mismatch")]
    security_run.annotate_counts(results)
    assert results[0].reason == "CLI mismatch"


def test_a_passing_scanner_gets_no_count_reason():
    from scripts.common.commands import CommandResult

    results = [CommandResult("gitleaks", "security", "x", "pass", 0,
                             output_tail="leaks found: 0")]
    security_run.annotate_counts(results)
    assert results[0].reason == ""


def test_the_report_detail_carries_the_finding_count():
    security = {
        "status": "fail",
        "results": [{"name": "gitleaks", "status": "fail",
                     "reason": "4 finding(s) — see the job summary for file and rule"}],
    }
    sections, _ = build_sections(dict(build_plan("all")), None, None, security)
    detail = {s.name: s for s in sections}["Security"].detail
    assert "gitleaks: 4 finding(s)" in detail


def test_every_declared_scanner_has_a_probe():
    for tool, entry in security_run.SCANNERS.items():
        binary, probe, command = entry
        assert probe.startswith(binary), tool
        assert command.startswith(binary), tool


# -- end to end ------------------------------------------------------------

def _main(tmp_path, monkeypatch, *, security_config, runner_map):
    config = tmp_path / "r.yml"
    config.write_text(
        "repositories:\n  yshrm:\n    enabled: true\n"
        f"    security: {json.dumps(security_config)}\n",
        encoding="utf-8",
    )
    result_file = tmp_path / "security-result.json"
    workdir = tmp_path / "target"
    workdir.mkdir()
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("CONFIG_FILE", str(config))
    monkeypatch.setenv("SECURITY_RESULT_FILE", str(result_file))
    monkeypatch.setenv("TARGET_WORKDIR", str(workdir))
    monkeypatch.setattr(security_run.shutil, "which", lambda _: "/usr/bin/tool")
    monkeypatch.setattr(
        security_run, "run_commands",
        lambda commands, cwd=None, runner=None, **kw: _fake_run(commands, runner_map),
    )
    code = security_run.main()
    return code, json.loads(result_file.read_text(encoding="utf-8"))


def _fake_run(commands, mapping):
    from scripts.common.commands import CommandResult

    out = []
    for command in commands:
        code, output = next(
            (v for k, v in mapping.items() if k in command.run), (0, "")
        )
        out.append(
            CommandResult(
                name=command.name, group=command.group, command=command.run,
                status="pass" if code == 0 else "fail", exit_code=code, output_tail=output,
            )
        )
    return out


def test_a_broken_scanner_yields_partial_not_fail(tmp_path, monkeypatch):
    code, payload = _main(
        tmp_path, monkeypatch,
        security_config={"gitleaks": True},
        runner_map={"gitleaks dir --help": (1, 'unknown command "dir"')},
    )
    assert payload["status"] == "partial"
    assert code == 0  # partial is not a hard failure of the validation job
    entry = payload["results"][0]
    assert entry["status"] == "error"
    assert "unknown command" in entry["reason"]
    assert "unknown command" in payload["reason"]


def test_a_real_leak_still_fails(tmp_path, monkeypatch):
    code, payload = _main(
        tmp_path, monkeypatch,
        security_config={"gitleaks": True},
        runner_map={"gitleaks dir --help": (0, "usage"),
                    "gitleaks dir .": (1, "secret found in .env")},
    )
    assert payload["status"] == "fail"
    assert code == 1
    assert payload["results"][0]["status"] == "fail"


def test_a_clean_scan_passes(tmp_path, monkeypatch):
    code, payload = _main(
        tmp_path, monkeypatch,
        security_config={"gitleaks": True},
        runner_map={"gitleaks dir --help": (0, "usage"), "gitleaks dir .": (0, "no leaks")},
    )
    assert payload["status"] == "pass"
    assert code == 0


# -- reporting -------------------------------------------------------------

def test_the_security_detail_is_never_blank_on_failure():
    security = {
        "status": "fail",
        "results": [{"name": "gitleaks", "status": "fail"},
                    {"name": "semgrep", "status": "pass"}],
    }
    sections, _ = build_sections(dict(build_plan("all")), None, None, security)
    section = {s.name: s for s in sections}["Security"]
    assert section.status == FAIL
    assert "gitleaks: fail" in section.detail


def test_a_clean_scan_says_so():
    security = {"status": "pass", "results": [{"name": "gitleaks", "status": "pass"}]}
    sections, _ = build_sections(dict(build_plan("all")), None, None, security)
    section = {s.name: s for s in sections}["Security"]
    assert section.status == PASS
    assert "1 scanner(s) clean" in section.detail


def test_an_errored_scanner_reads_as_partial_and_names_itself():
    security = {
        "status": "partial",
        "results": [{"name": "gitleaks", "status": "error",
                     "reason": "gitleaks does not support this invocation"}],
    }
    sections, _ = build_sections(dict(build_plan("all")), None, None, security)
    section = {s.name: s for s in sections}["Security"]
    assert section.status == PARTIAL
    assert overall(sections) in (PARTIAL, FAIL)


def test_an_explicit_reason_wins_over_the_derived_detail():
    security = {"status": "partial", "reason": "trivy is not installed", "results": []}
    sections, _ = build_sections(dict(build_plan("all")), None, None, security)
    assert {s.name: s for s in sections}["Security"].detail == "trivy is not installed"


def test_a_broken_scanner_does_not_by_itself_fail_the_gate():
    # PARTIAL, so the run is not green, but a tool bug is not a security failure.
    quality = {
        "status": "pass",
        "results": [
            {"name": "Deps", "group": "install", "status": "pass"},
            {"name": "Lint", "group": "lint", "status": "pass"},
            {"name": "Tests", "group": "test", "status": "pass"},
            {"name": "Build", "group": "build", "status": "pass"},
        ],
    }
    analysis = {
        "metadata": {"status": "ok", "data": {"summary": "s"}},
        "review": {"status": "ok", "data": {"findings": [], "batches": 1, "complete": True}},
        "tests": {"status": "ok", "data": {"gaps": []}},
    }
    security = {"status": "partial", "reason": "gitleaks CLI mismatch", "results": []}
    sections, _ = build_sections(dict(build_plan("all")), analysis, quality, security)
    assert overall(sections) == PARTIAL
