"""TARGET_ORG validation, the repository allowlist, YAML loading and imports."""
from __future__ import annotations

import importlib

import pytest

from scripts.common import config
from scripts.common.errors import ConfigError

REPOS = {
    "yshrm": {"enabled": True, "commands": {"install": ["npm ci"], "lint": []}},
    "disabled-repo": {"enabled": False, "commands": {}},
}


# -- TARGET_ORG ------------------------------------------------------------

def test_target_org_returns_configured_value():
    assert config.target_org({"TARGET_ORG": "mycompany"}) == "mycompany"


def test_target_org_strips_whitespace():
    assert config.target_org({"TARGET_ORG": "  mycompany  "}) == "mycompany"


@pytest.mark.parametrize("env", [{}, {"TARGET_ORG": ""}, {"TARGET_ORG": "   "}])
def test_target_org_missing_fails_with_the_documented_message(env):
    with pytest.raises(ConfigError) as excinfo:
        config.target_org(env)
    assert str(excinfo.value) == "TARGET_ORG repository/organization variable is required."


def test_the_reserved_github_org_variable_is_not_consulted():
    # GitHub cannot create a GITHUB_-prefixed variable, so it must never be read.
    with pytest.raises(ConfigError):
        config.target_org({"GITHUB_ORG": "mycompany"})


@pytest.mark.parametrize("value", ["my company", "my/company", "-bad", "bad-", "a" * 40])
def test_target_org_rejects_malformed_owners(value):
    with pytest.raises(ConfigError):
        config.target_org({"TARGET_ORG": value})


# -- allowlist -------------------------------------------------------------

def test_resolve_target_builds_owner_slash_repo():
    target = config.resolve_target("yshrm", env={"TARGET_ORG": "mycompany"}, repositories=REPOS)
    assert (target.owner, target.repo, target.full_name) == ("mycompany", "yshrm", "mycompany/yshrm")


def test_resolve_target_rejects_repository_not_in_allowlist():
    with pytest.raises(ConfigError, match="not listed in the central allowlist"):
        config.resolve_target("other", env={"TARGET_ORG": "mycompany"}, repositories=REPOS)


def test_resolve_target_rejects_disabled_repository():
    with pytest.raises(ConfigError, match="not enabled"):
        config.resolve_target("disabled-repo", env={"TARGET_ORG": "mycompany"}, repositories=REPOS)


def test_resolve_target_rejects_owner_prefixed_name():
    with pytest.raises(ConfigError, match="without the owner prefix"):
        config.resolve_target("mycompany/yshrm", env={"TARGET_ORG": "mycompany"}, repositories=REPOS)


def test_resolve_target_requires_target_org_before_the_allowlist():
    with pytest.raises(ConfigError, match="TARGET_ORG"):
        config.resolve_target("yshrm", env={}, repositories=REPOS)


@pytest.mark.parametrize("name", ["", "  ", "../etc", "a b"])
def test_resolve_target_rejects_malformed_repository_names(name):
    with pytest.raises(ConfigError):
        config.resolve_target(name, env={"TARGET_ORG": "mycompany"}, repositories=REPOS)


# -- YAML loading ----------------------------------------------------------

def test_load_yaml_reads_a_mapping(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text("a: 1\nb: [x, y]\n", encoding="utf-8")
    assert config.load_yaml(path) == {"a": 1, "b": ["x", "y"]}


def test_load_yaml_treats_an_empty_file_as_empty(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text("", encoding="utf-8")
    assert config.load_yaml(path) == {}


def test_load_yaml_rejects_a_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        config.load_yaml(tmp_path / "nope.yml")


def test_load_yaml_rejects_a_non_mapping(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="must contain a YAML mapping"):
        config.load_yaml(path)


def test_load_yaml_rejects_malformed_yaml(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text("a: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Could not parse"):
        config.load_yaml(path)


def test_repositories_config_in_this_repo_loads_and_lists_yshrm():
    repositories = config.load_repositories("config/repositories.yml")
    assert repositories["yshrm"]["enabled"] is True


def test_reviewers_config_in_this_repo_loads():
    groups = config.reviewer_groups("config/reviewers.yml")
    assert set(groups) == {"backend", "frontend", "devops", "security"}
    for group in groups.values():
        assert isinstance(group["paths"], list) and group["paths"]


def test_a_config_with_no_repositories_block_is_valid(tmp_path):
    # A defaults-only config is legitimate: every repository uses the profile.
    path = tmp_path / "r.yml"
    path.write_text("defaults: {enabled: true}\n", encoding="utf-8")
    assert config.load_repositories(path) == {}


def test_load_repositories_rejects_a_non_mapping_repositories_block(tmp_path):
    path = tmp_path / "r.yml"
    path.write_text("repositories:\n  - one\n  - two\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="'repositories' as a mapping"):
        config.load_repositories(path)


# -- command normalisation -------------------------------------------------

def test_commands_accepts_strings_and_named_mappings():
    target = config.Target(
        owner="o",
        repo="r",
        config={"commands": {"lint": ["npm run lint", {"name": "Admin lint", "run": "x"}]}},
    )
    commands = config.commands_for(target, "lint")
    assert [(c.name, c.run, c.group) for c in commands] == [
        ("Lint 1", "npm run lint", "lint"),
        ("Admin lint", "x", "lint"),
    ]


def test_commands_for_missing_group_is_empty():
    target = config.Target(owner="o", repo="r", config={})
    assert config.commands_for(target, "build") == []


def test_commands_rejects_a_malformed_entry():
    target = config.Target(owner="o", repo="r", config={"commands": {"lint": [42]}})
    with pytest.raises(ConfigError, match="must be a string or a mapping"):
        config.commands_for(target, "lint")


# -- module layout ---------------------------------------------------------

@pytest.mark.parametrize(
    "module",
    [
        "scripts.plan",
        "scripts.common.commands",
        "scripts.common.config",
        "scripts.common.dispatch",
        "scripts.common.globs",
        "scripts.common.labels",
        "scripts.common.reviewers",
        "scripts.ai.analyze",
        "scripts.ai.check_models",
        "scripts.ai.client",
        "scripts.ai.prompts",
        "scripts.ai.resolve",
        "scripts.ai.schemas",
        "scripts.github.api",
        "scripts.github.get_pr",
        "scripts.github.report",
        "scripts.gitops.patches",
        "scripts.gitops.probe",
        "scripts.gitops.push",
        "scripts.gitops.validate",
        "scripts.quality.run",
        "scripts.release.propose",
        "scripts.release.version",
        "scripts.security.run",
    ],
)
def test_every_module_imports_as_a_package_module(module):
    # This is what `python -m scripts.<module>` relies on; a missing __init__.py
    # or a sys.path hack would show up here.
    assert importlib.import_module(module) is not None


def test_analyze_resolves_its_sibling_import_without_sys_path_hacks():
    module = importlib.import_module("scripts.ai.analyze")
    assert module.metadata_model({"AI_METADATA_MODEL": "x/y"}) == "x/y"
