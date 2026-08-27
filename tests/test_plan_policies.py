"""The plan job carries the insight policies and the new step outputs."""
from __future__ import annotations

import json

import pytest

from scripts import plan as plan_module
from scripts.plan import POLICY_BLOCKS, collect_policies

CONFIG = """
repositories:
  yshrm:
    enabled: true
    reviewers:
      codeowners: request
    size_policy:
      max_files: 30
      fail_on_violation: true
    secret_scan:
      fail_on: critical
    coverage:
      enabled: true
      min_changed_line_coverage: 75
    readiness:
      require_approvals: 2
      block_on: [test, secrets]
    flaky:
      enabled: true
      runs: 5
"""


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "repositories.yml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def _run(monkeypatch, config, action="all", output_file=None):
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("TARGET_ACTION", action)
    monkeypatch.setenv("TARGET_ORG", "mycompany")
    monkeypatch.setenv("REPOSITORIES_CONFIG", str(config))
    if output_file is not None:
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    return plan_module.main()


def _outputs(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


# -- policy collection -----------------------------------------------------

def test_only_mapping_blocks_are_carried(capsys):
    policies = collect_policies({
        "size_policy": {"max_files": 10},
        "secret_scan": "not a mapping",
        "unrelated": {"x": 1},
    })
    assert policies == {"size_policy": {"max_files": 10}}
    assert "not a mapping" in capsys.readouterr().out


def test_an_absent_block_is_simply_absent():
    assert collect_policies({}) == {}


def test_every_documented_block_is_collectable():
    config = {name: {"key": "value"} for name in POLICY_BLOCKS}
    assert set(collect_policies(config)) == set(POLICY_BLOCKS)


# -- the plan artifact -----------------------------------------------------

def test_the_plan_carries_the_configured_policies(artifacts, monkeypatch, config):
    assert _run(monkeypatch, config) == 0
    written = json.loads((artifacts / "plan.json").read_text(encoding="utf-8"))
    policies = written["policies"]
    assert policies["size_policy"]["max_files"] == 30
    assert policies["secret_scan"]["fail_on"] == "critical"
    assert policies["coverage"]["min_changed_line_coverage"] == 75
    assert policies["readiness"]["require_approvals"] == 2
    assert policies["flaky"]["runs"] == 5


def test_the_plan_carries_the_codeowners_mode(artifacts, monkeypatch, config):
    _run(monkeypatch, config)
    written = json.loads((artifacts / "plan.json").read_text(encoding="utf-8"))
    assert written["reviewer_options"]["codeowners"] == "request"


def test_an_unknown_codeowners_mode_falls_back_to_defer(tmp_path, artifacts, monkeypatch,
                                                       capsys):
    path = tmp_path / "r.yml"
    path.write_text(
        "repositories:\n  yshrm:\n    enabled: true\n    reviewers: {codeowners: shout}\n",
        encoding="utf-8",
    )
    assert _run(monkeypatch, path) == 0
    written = json.loads((artifacts / "plan.json").read_text(encoding="utf-8"))
    assert written["reviewer_options"]["codeowners"] == "defer"
    assert "Unknown reviewers.codeowners mode" in capsys.readouterr().out


def test_a_repository_with_no_policy_blocks_gets_an_empty_mapping(tmp_path, artifacts,
                                                                  monkeypatch):
    path = tmp_path / "r.yml"
    path.write_text("repositories:\n  yshrm:\n    enabled: true\n", encoding="utf-8")
    assert _run(monkeypatch, path) == 0
    written = json.loads((artifacts / "plan.json").read_text(encoding="utf-8"))
    assert written["policies"] == {}
    assert written["reviewer_options"]["codeowners"] == "defer"


def test_the_plan_json_stays_parseable_by_the_workflow(tmp_path, artifacts, monkeypatch,
                                                      config):
    output_file = tmp_path / "gh-output"
    output_file.touch()
    _run(monkeypatch, config, output_file=output_file)
    values = _outputs(output_file)
    # fromJSON() in the workflow parses this, so it must survive a round trip on
    # one line.
    parsed = json.loads(values["plan"])
    assert parsed["policies"]["size_policy"]["max_files"] == 30
    assert "\n" not in values["plan"]


# -- the new step outputs --------------------------------------------------

def test_the_new_run_flags_are_exported(tmp_path, artifacts, monkeypatch, config):
    output_file = tmp_path / "gh-output"
    output_file.touch()
    _run(monkeypatch, config, output_file=output_file)
    values = _outputs(output_file)
    assert values["run_coverage"] == "true"
    assert values["run_insights"] == "true"
    # `all` excludes flaky: repeating the suite has to be asked for.
    assert values["run_flaky"] == "false"


def test_requesting_flaky_sets_its_flag(tmp_path, artifacts, monkeypatch, config):
    output_file = tmp_path / "gh-output"
    output_file.touch()
    _run(monkeypatch, config, action="flaky", output_file=output_file)
    values = _outputs(output_file)
    assert values["run_flaky"] == "true"
    assert values["needs_target_source"] == "true"
    assert values["run_insights"] == "false"


def test_the_insights_action_needs_no_checkout(tmp_path, artifacts, monkeypatch, config):
    output_file = tmp_path / "gh-output"
    output_file.touch()
    _run(monkeypatch, config, action="insights", output_file=output_file)
    values = _outputs(output_file)
    assert values["run_insights"] == "true"
    assert values["needs_target_source"] == "false"
    assert values["needs_analysis"] == "true"
    assert values["needs_privileged_write"] == "false"
