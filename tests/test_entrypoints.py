"""Entry-point behaviour: `python -m scripts.*` wiring, outputs and artifacts."""
from __future__ import annotations

import json

import pytest
import yaml

from scripts import plan as plan_module
from scripts.github import report as report_module
from scripts.quality import run as quality_run
from scripts.security import run as security_run

REPOS_YML = """
repositories:
  yshrm:
    enabled: true
    metadata:
      sections: [at_glance, details, test_plan, type, user_impact, release_notes]
      headings:
        at_glance: Overview
      template:
        summary: "{subject}"
        detail_item: "Update `{path}`."
    commands:
      install:
        - name: Frontend dependencies
          run: exit 1
      lint:
        - name: Frontend lint
          run: exit 0
    security:
      gitleaks: true
      trivy: true
    fix_allowed_paths: ["backend/**"]
"""


@pytest.fixture
def repos_config(tmp_path):
    path = tmp_path / "repositories.yml"
    path.write_text(REPOS_YML, encoding="utf-8")
    return path


def outputs(path):
    """Parse a GITHUB_OUTPUT file into a dict."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


# -- scripts.plan ----------------------------------------------------------

def test_plan_writes_outputs_and_the_plan_artifact(tmp_path, artifacts, monkeypatch, repos_config):
    output_file = tmp_path / "gh-output"
    output_file.touch()
    for key, value in {
        "TARGET_REPOSITORY": "yshrm",
        "TARGET_ACTION": "all",
        "TARGET_ORG": "mycompany",
        "REPOSITORIES_CONFIG": str(repos_config),
        "GITHUB_OUTPUT": str(output_file),
    }.items():
        monkeypatch.setenv(key, value)

    assert plan_module.main() == 0
    values = outputs(output_file)
    assert values["owner"] == "mycompany"
    assert values["repo"] == "yshrm"
    assert values["full_name"] == "mycompany/yshrm"
    assert values["run_reviewers"] == "true"
    assert values["needs_privileged_write"] == "false"

    written = json.loads((artifacts / "plan.json").read_text(encoding="utf-8"))
    assert written["action"] == "all"
    assert written["fix_allowed_paths"] == ["backend/**"]
    assert written["metadata_options"]["title_policy"] == "weak"
    assert written["metadata_options"]["sections"][0] == "at_glance"
    assert written["metadata_options"]["headings"]["at_glance"] == "Overview"
    assert written["metadata_options"]["template"]["summary"] == "{subject}"
    assert written["inline_comments"] is True
    # The plan output must be parseable by fromJSON() in the workflow.
    assert json.loads(values["plan"])["owner"] == "mycompany"


def test_plan_carries_the_configured_title_policy(tmp_path, artifacts, monkeypatch):
    config = tmp_path / "r.yml"
    config.write_text(
        "repositories:\n  yshrm:\n    enabled: true\n"
        "    metadata: {title_policy: always}\n    review: {inline_comments: false}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("TARGET_ACTION", "metadata")
    monkeypatch.setenv("TARGET_ORG", "mycompany")
    monkeypatch.setenv("REPOSITORIES_CONFIG", str(config))
    assert plan_module.main() == 0
    written = json.loads((artifacts / "plan.json").read_text(encoding="utf-8"))
    assert written["metadata_options"]["title_policy"] == "always"
    assert written["inline_comments"] is False


def test_an_unknown_title_policy_falls_back_to_weak(tmp_path, artifacts, monkeypatch, capsys):
    config = tmp_path / "r.yml"
    config.write_text(
        "repositories:\n  yshrm:\n    enabled: true\n"
        "    metadata: {title_policy: aggressive}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("TARGET_ACTION", "metadata")
    monkeypatch.setenv("TARGET_ORG", "mycompany")
    monkeypatch.setenv("REPOSITORIES_CONFIG", str(config))
    assert plan_module.main() == 0
    written = json.loads((artifacts / "plan.json").read_text(encoding="utf-8"))
    assert written["metadata_options"]["title_policy"] == "weak"
    assert "Unknown metadata.title_policy" in capsys.readouterr().out


def test_a_read_only_action_does_not_require_a_sandbox(
    tmp_path, artifacts, monkeypatch, repos_config
):
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("TARGET_ACTION", "all")
    monkeypatch.setenv("TARGET_ORG", "mycompany")
    monkeypatch.setenv("REPOSITORIES_CONFIG", str(repos_config))
    assert plan_module.main() == 0


def test_fix_is_refused_when_no_sandbox_is_configured(
    tmp_path, artifacts, monkeypatch, capsys
):
    config = tmp_path / "r.yml"
    config.write_text(
        "repositories:\n  yshrm:\n    enabled: true\n"
        "    fix_allowed_paths: ['backend/**']\n"
        "    sandbox: {enabled: false, image: ''}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("TARGET_ACTION", "fix")
    monkeypatch.setenv("TARGET_ORG", "mycompany")
    monkeypatch.setenv("REPOSITORIES_CONFIG", str(config))
    assert plan_module.main() == 1
    assert "must run in a sandbox" in capsys.readouterr().err


def test_resolve_conflicts_is_refused_without_a_sandbox(tmp_path, artifacts, monkeypatch, capsys):
    config = tmp_path / "r.yml"
    config.write_text("repositories:\n  yshrm:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("TARGET_ACTION", "resolve-conflicts")
    monkeypatch.setenv("TARGET_ORG", "mycompany")
    monkeypatch.setenv("REPOSITORIES_CONFIG", str(config))
    assert plan_module.main() == 1
    assert "must run in a sandbox" in capsys.readouterr().err


def test_fix_is_allowed_once_a_sandbox_image_is_configured(tmp_path, artifacts, monkeypatch):
    config = tmp_path / "r.yml"
    config.write_text(
        "repositories:\n  yshrm:\n    enabled: true\n"
        "    fix_allowed_paths: ['backend/**']\n"
        "    sandbox: {enabled: true, image: 'example/img:pinned'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("TARGET_ACTION", "fix")
    monkeypatch.setenv("TARGET_ORG", "mycompany")
    monkeypatch.setenv("REPOSITORIES_CONFIG", str(config))
    assert plan_module.main() == 0
    written = json.loads((artifacts / "plan.json").read_text(encoding="utf-8"))
    assert written["sandbox"] == {"enabled": True, "image": "example/img:pinned"}


def test_plan_fails_before_any_token_exists_on_a_missing_target_org(
    tmp_path, artifacts, monkeypatch, repos_config, capsys
):
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("TARGET_ACTION", "all")
    monkeypatch.setenv("REPOSITORIES_CONFIG", str(repos_config))
    monkeypatch.delenv("TARGET_ORG", raising=False)
    assert plan_module.main() == 1
    assert "TARGET_ORG repository/organization variable is required." in capsys.readouterr().err


def test_plan_fails_on_an_unknown_action(tmp_path, artifacts, monkeypatch, repos_config, capsys):
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("TARGET_ACTION", "deploy-to-prod")
    monkeypatch.setenv("TARGET_ORG", "mycompany")
    monkeypatch.setenv("REPOSITORIES_CONFIG", str(repos_config))
    assert plan_module.main() == 1
    assert "Unsupported action" in capsys.readouterr().err


def test_plan_fails_on_a_repository_outside_the_allowlist(
    tmp_path, artifacts, monkeypatch, repos_config, capsys
):
    monkeypatch.setenv("TARGET_REPOSITORY", "someone-elses-repo")
    monkeypatch.setenv("TARGET_ACTION", "all")
    monkeypatch.setenv("TARGET_ORG", "mycompany")
    monkeypatch.setenv("REPOSITORIES_CONFIG", str(repos_config))
    assert plan_module.main() == 1
    assert "not listed in the central allowlist" in capsys.readouterr().err


# -- scripts.quality.run ---------------------------------------------------

def test_quality_run_writes_a_result_even_when_install_fails(
    tmp_path, monkeypatch, repos_config
):
    result_file = tmp_path / "quality-result.json"
    workdir = tmp_path / "target"
    workdir.mkdir()
    for key, value in {
        "TARGET_REPOSITORY": "yshrm",
        "CONFIG_FILE": str(repos_config),
        "QUALITY_RESULT_FILE": str(result_file),
        "TARGET_WORKDIR": str(workdir),
    }.items():
        monkeypatch.setenv(key, value)

    assert quality_run.main() == 1  # failure is reported...
    payload = json.loads(result_file.read_text(encoding="utf-8"))  # ...and recorded
    assert payload["status"] == "fail"
    names = {r["name"]: r["status"] for r in payload["results"]}
    assert names == {"Frontend dependencies": "fail", "Frontend lint": "skipped"}


def test_quality_run_does_not_need_target_org(tmp_path, monkeypatch, repos_config):
    # The validation job is credential-free and must not depend on org config.
    monkeypatch.delenv("TARGET_ORG", raising=False)
    workdir = tmp_path / "target"
    workdir.mkdir()
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("CONFIG_FILE", str(repos_config))
    monkeypatch.setenv("QUALITY_RESULT_FILE", str(tmp_path / "q.json"))
    monkeypatch.setenv("TARGET_WORKDIR", str(workdir))
    assert quality_run.main() == 1


def test_quality_run_reports_a_missing_working_directory(tmp_path, monkeypatch, repos_config):
    result_file = tmp_path / "q.json"
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("CONFIG_FILE", str(repos_config))
    monkeypatch.setenv("QUALITY_RESULT_FILE", str(result_file))
    monkeypatch.setenv("TARGET_WORKDIR", str(tmp_path / "absent"))
    assert quality_run.main() == 1
    assert json.loads(result_file.read_text(encoding="utf-8"))["status"] == "error"


def test_quality_run_reports_missing_environment(monkeypatch, capsys):
    for key in ("TARGET_REPOSITORY", "CONFIG_FILE", "QUALITY_RESULT_FILE"):
        monkeypatch.delenv(key, raising=False)
    assert quality_run.main() == 2
    assert "Missing required environment variable" in capsys.readouterr().err


# -- scripts.security.run --------------------------------------------------

def test_an_unprovisioned_scanner_is_not_run_rather_than_passing(
    tmp_path, monkeypatch, repos_config
):
    result_file = tmp_path / "security-result.json"
    workdir = tmp_path / "target"
    workdir.mkdir()
    monkeypatch.setenv("TARGET_REPOSITORY", "yshrm")
    monkeypatch.setenv("CONFIG_FILE", str(repos_config))
    monkeypatch.setenv("SECURITY_RESULT_FILE", str(result_file))
    monkeypatch.setenv("TARGET_WORKDIR", str(workdir))
    monkeypatch.setattr(security_run.shutil, "which", lambda _: None)

    assert security_run.main() == 0
    payload = json.loads(result_file.read_text(encoding="utf-8"))
    assert payload["status"] == "partial"
    statuses = {r["name"]: r["status"] for r in payload["results"]}
    assert statuses == {"gitleaks": "not_run", "trivy": "not_run"}


def test_disabled_scanners_are_not_run():
    from scripts.common.config import Target

    target = Target(owner="o", repo="r", config={"security": {"gitleaks": False}})
    runnable, unavailable = security_run.planned_scanners(target, which=lambda _: "/usr/bin/x")
    assert runnable == [] and unavailable == []


def test_enabled_and_provisioned_scanners_become_commands():
    from scripts.common.config import Target

    target = Target(owner="o", repo="r", config={"security": {"semgrep": True}})
    runnable, unavailable = security_run.planned_scanners(target, which=lambda _: "/usr/bin/semgrep")
    assert [c.name for c in runnable] == ["semgrep"]
    assert unavailable == []


def test_extra_security_commands_from_config_are_included():
    from scripts.common.config import Target

    target = Target(
        owner="o",
        repo="r",
        config={"security": {"commands": [{"name": "pip-audit", "run": "pip-audit"}]}},
    )
    runnable, _ = security_run.planned_scanners(target, which=lambda _: None)
    assert [c.name for c in runnable] == ["pip-audit"]


# -- report artifact handling ---------------------------------------------

def test_report_treats_a_missing_artifact_as_absent(tmp_path):
    assert report_module._read_json(tmp_path / "nope.json") is None


def test_report_treats_a_corrupt_artifact_as_absent(tmp_path, capsys):
    path = tmp_path / "quality-result.json"
    path.write_text("{not json", encoding="utf-8")
    assert report_module._read_json(path) is None
    assert "Ignoring unreadable artifact" in capsys.readouterr().err


def test_report_rejects_a_json_array_artifact(tmp_path):
    path = tmp_path / "quality-result.json"
    path.write_text("[1, 2]", encoding="utf-8")
    assert report_module._read_json(path) is None


def test_report_fails_cleanly_without_a_pr_context(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RESULT_DIR", str(tmp_path))
    monkeypatch.setenv("TARGET_GITHUB_TOKEN", "t")
    monkeypatch.setenv("PR_AUTOMATION_PLAN", "{}")
    assert report_module.main() == 2
    assert "pr-context.json is missing" in capsys.readouterr().err


# -- workflow and config files parse --------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/central-pr-automation.yml",
        ".github/workflows/release-central.yml",
        ".github/workflows/ci.yml",
        "config/repositories.yml",
        "config/reviewers.yml",
    ],
)
def test_every_yaml_file_parses(path):
    with open(path, encoding="utf-8") as handle:
        assert yaml.safe_load(handle) is not None


def test_the_central_repo_holds_no_legacy_same_repository_workflows():
    import pathlib

    present = {p.name for p in pathlib.Path(".github/workflows").glob("*.yml")}
    # The old per-repository implementation and its thin dispatchers are gone;
    # central-pr-automation.yml's own workflow_dispatch covers every action.
    assert present == {
        "central-pr-automation.yml",
        "ci.yml",
        "release-central.yml",
    }


def test_the_central_workflow_runs_target_code_in_exactly_one_job():
    with open(".github/workflows/central-pr-automation.yml", encoding="utf-8") as handle:
        workflow = yaml.safe_load(handle)
    jobs = workflow["jobs"]

    def job_text(name):
        return yaml.safe_dump(jobs[name])

    # Only validate-target may reference the quality or security runners.
    runners = [name for name in jobs if "scripts.quality.run" in job_text(name)
               or "scripts.security.run" in job_text(name)]
    assert runners == ["validate-target"]

    # And that job must receive no credentials at all.
    validation = job_text("validate-target")
    for secret in ("GH_APP_ID", "GH_APP_PRIVATE_KEY", "NVIDIA_API_KEY",
                   "create-github-app-token", "app-token"):
        assert secret not in validation, secret


def test_no_job_uploads_the_whole_runner_temp():
    with open(".github/workflows/central-pr-automation.yml", encoding="utf-8") as handle:
        text = handle.read()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("path:") or stripped.startswith("- ${{ runner.temp }}"):
            assert stripped.rstrip("/") not in (
                "path: ${{ runner.temp }}",
                "- ${{ runner.temp }}",
            ), line


def test_the_deterministic_analysis_jobs_never_check_out_the_target_repository():
    with open(".github/workflows/central-pr-automation.yml", encoding="utf-8") as handle:
        jobs = yaml.safe_load(handle)["jobs"]
    for name in ("deterministic-analysis", "deterministic-resolve"):
        text = yaml.safe_dump(jobs[name])
        assert "repository:" not in text, name
        assert "full_name" not in text, name


def test_active_workflows_have_no_ai_provider_or_model_dependency():
    import pathlib

    workflow_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in pathlib.Path(".github/workflows").glob("*.yml")
    )
    for value in ("NVIDIA_API_KEY", "AI_BASE_URL", "AI_METADATA_MODEL",
                  "AI_REVIEW_MODEL", "AI_MAX_TOKENS", "scripts.ai"):
        assert value not in workflow_text
    assert "openai" not in pathlib.Path("requirements.txt").read_text(encoding="utf-8").lower()
