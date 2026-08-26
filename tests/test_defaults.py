"""The defaults profile: any repository in the org, without a hand-written entry."""
from __future__ import annotations

import pytest

from scripts.common import config
from scripts.common.errors import ConfigError

OPEN = {"enabled": True, "security": {"gitleaks": True}, "sandbox": {"enabled": False, "image": ""}}
STRICT = {"enabled": False}
REPOS = {
    "yshrm": {"enabled": True, "commands": {"install": ["npm ci"]}},
    "disabled-repo": {"enabled": False},
}
ENV = {"TARGET_ORG": "mycompany"}


def _resolve(name, *, repositories=REPOS, defaults=OPEN):
    return config.resolve_target(name, env=ENV, repositories=repositories, defaults=defaults)


# -- open mode -------------------------------------------------------------

def test_an_unlisted_repository_resolves_from_the_defaults():
    target = _resolve("brand-new-service")
    assert target.full_name == "mycompany/brand-new-service"
    assert target.config["_source"] == "defaults"


def test_the_defaults_profile_is_applied_to_an_unlisted_repository():
    target = _resolve("brand-new-service")
    assert target.config["security"]["gitleaks"] is True
    assert target.config["sandbox"]["enabled"] is False


def test_an_unlisted_repository_has_no_commands_so_detection_takes_over():
    target = _resolve("brand-new-service")
    assert not config.has_explicit_commands(target)


# -- strict mode -----------------------------------------------------------

def test_strict_mode_still_rejects_an_unlisted_repository():
    with pytest.raises(ConfigError, match="not listed in the central allowlist"):
        _resolve("brand-new-service", defaults=STRICT)


def test_the_rejection_message_explains_how_to_open_it_up():
    with pytest.raises(ConfigError) as excinfo:
        _resolve("brand-new-service", defaults=STRICT)
    assert "defaults.enabled: true" in str(excinfo.value)


def test_no_defaults_at_all_means_strict():
    with pytest.raises(ConfigError, match="not listed"):
        _resolve("brand-new-service", defaults={})


# -- deny list -------------------------------------------------------------

def test_a_denied_repository_is_refused_even_in_open_mode():
    defaults = {**OPEN, "deny": ["secrets-vault"]}
    with pytest.raises(ConfigError, match="explicitly denied"):
        _resolve("secrets-vault", defaults=defaults)


def test_the_deny_list_is_case_insensitive():
    defaults = {**OPEN, "deny": ["Secrets-Vault"]}
    with pytest.raises(ConfigError, match="explicitly denied"):
        _resolve("secrets-vault", defaults=defaults)


def test_deny_beats_an_explicit_enabled_entry():
    # A repository can be taken out of service without editing its own entry.
    defaults = {**OPEN, "deny": ["yshrm"]}
    with pytest.raises(ConfigError, match="explicitly denied"):
        _resolve("yshrm", defaults=defaults)


def test_the_deny_list_never_leaks_into_the_resolved_config():
    target = _resolve("anything", defaults={**OPEN, "deny": ["other"]})
    assert "deny" not in target.config


# -- merging ---------------------------------------------------------------

def test_an_explicit_entry_inherits_unset_defaults():
    target = _resolve("yshrm")
    assert target.config["security"]["gitleaks"] is True  # from defaults
    assert target.config["commands"]["install"] == ["npm ci"]  # from the entry
    assert target.config["_source"] == "explicit"


def test_an_explicit_entry_overrides_a_default():
    repos = {"yshrm": {"enabled": True, "security": {"gitleaks": False}}}
    target = _resolve("yshrm", repositories=repos)
    assert target.config["security"]["gitleaks"] is False


def test_nested_blocks_merge_key_by_key():
    defaults = {"enabled": True, "metadata": {"title_policy": "weak", "sections": ["description"]}}
    repos = {"yshrm": {"enabled": True, "metadata": {"title_policy": "never"}}}
    target = _resolve("yshrm", repositories=repos, defaults=defaults)
    # title_policy overridden, sections inherited.
    assert target.config["metadata"]["title_policy"] == "never"
    assert target.config["metadata"]["sections"] == ["description"]


def test_a_disabled_explicit_entry_is_still_refused_in_open_mode():
    # An explicit `enabled: false` is a deliberate opt-out, not an oversight.
    with pytest.raises(ConfigError, match="not enabled"):
        _resolve("disabled-repo")


def test_a_non_mapping_entry_is_rejected():
    with pytest.raises(ConfigError, match="must be a mapping"):
        _resolve("bad", repositories={"bad": "yes"})


# -- the guards that survive open mode ------------------------------------

def test_target_org_is_still_required():
    with pytest.raises(ConfigError, match="TARGET_ORG"):
        config.resolve_target("anything", env={}, repositories=REPOS, defaults=OPEN)


def test_a_malformed_repository_name_is_still_rejected():
    for name in ("", "  ", "../etc", "a b", "owner/repo"):
        with pytest.raises(ConfigError):
            _resolve(name)


def test_open_mode_does_not_enable_auto_fix():
    # fix_allowed_paths stays empty, so action=fix is refused for an unlisted repo.
    target = _resolve("brand-new-service")
    assert not target.config.get("fix_allowed_paths")


# -- the shipped config ----------------------------------------------------

def test_the_shipped_config_has_a_defaults_block():
    defaults = config.load_defaults("config/repositories.yml")
    assert defaults["enabled"] is True
    assert defaults["security"]["gitleaks"] is True
    assert defaults["fix_allowed_paths"] == []
    assert defaults["sandbox"]["enabled"] is False


def test_the_shipped_config_declares_no_default_commands():
    # Default commands would be wrong for most repositories; detection handles it.
    assert "commands" not in config.load_defaults("config/repositories.yml")


def test_an_arbitrary_repository_resolves_against_the_shipped_config():
    target = config.resolve_target(
        "some-new-repo", env=ENV, path="config/repositories.yml"
    )
    assert target.full_name == "mycompany/some-new-repo"
    assert target.config["_source"] == "defaults"


def test_the_explicitly_configured_repositories_still_resolve():
    for name in ("yshrm", "ecommerce-main"):
        target = config.resolve_target(name, env=ENV, path="config/repositories.yml")
        assert target.config["_source"] == "explicit"
        assert config.has_explicit_commands(target), name
