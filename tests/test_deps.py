"""Dependency-change risk: version transitions, lock-file agreement, sources."""
from __future__ import annotations

from scripts.insights.changes import parse_changes
from scripts.insights.deps import ecosystems_touched, evaluate


def _diff(path, removed=(), added=()):
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(removed)} +1,{len(added)} @@\n{body}"
    )


def _evaluate(diff, paths=None, config=None):
    changes = parse_changes(diff)
    context = {"files": list(paths) if paths is not None else sorted(changes)}
    return evaluate(changes, context, config or {})


def _rules(result):
    return {f["rule"] for f in result["findings"]}


# -- ecosystem detection ---------------------------------------------------

def test_ecosystems_are_detected_and_split_by_kind():
    found = ecosystems_touched(["package.json", "package-lock.json", "backend/requirements.txt"])
    assert found["npm"]["manifests"] == ["package.json"]
    assert found["npm"]["locks"] == ["package-lock.json"]
    assert found["python"]["manifests"] == ["backend/requirements.txt"]


def test_no_dependency_file_passes_with_nothing_to_say():
    result = _evaluate(_diff("app/main.py", added=["x = 1"]))
    assert result["status"] == "pass"
    assert result["packages"] == []


# -- version transitions ---------------------------------------------------

def test_a_major_bump_is_reported_with_the_transition():
    result = _evaluate(_diff(
        "package.json",
        removed=['    "express": "^4.18.2",'],
        added=['    "express": "^5.0.1",'],
    ))
    assert "deps-major-bump" in _rules(result)
    package = next(p for p in result["packages"] if p["name"] == "express")
    assert package["from"] == "^4.18.2"
    assert package["to"] == "^5.0.1"
    assert package["major_bump"] is True


def test_a_patch_bump_is_not_a_major_bump():
    result = _evaluate(_diff(
        "package.json",
        removed=['    "express": "^4.18.2",'],
        added=['    "express": "^4.18.3",'],
    ))
    assert "deps-major-bump" not in _rules(result)
    assert result["totals"]["changed"] == 1


def test_a_downgrade_is_reported_separately():
    result = _evaluate(_diff(
        "requirements.txt",
        removed=["cryptography==42.0.5"],
        added=["cryptography==41.0.0"],
    ))
    assert "deps-downgrade" in _rules(result)


def test_widening_a_range_is_reported():
    result = _evaluate(_diff(
        "requirements.txt",
        removed=["requests==2.31.0"],
        added=["requests>=2.31.0"],
    ))
    assert "deps-range-widened" in _rules(result)


def test_a_prerelease_pin_is_reported():
    result = _evaluate(_diff(
        "package.json",
        removed=['    "vite": "5.0.0",'],
        added=['    "vite": "6.0.0-beta.3",'],
    ))
    assert "deps-prerelease" in _rules(result)


def test_added_and_removed_packages_are_reported():
    result = _evaluate(_diff(
        "requirements.txt",
        removed=["old-package==1.0.0"],
        added=["new-package==2.0.0"],
    ))
    assert {"deps-added", "deps-removed"} <= _rules(result)
    kinds = {p["name"]: p["kind"] for p in result["packages"]}
    assert kinds == {"new-package": "added", "old-package": "removed"}


# -- lock file agreement ---------------------------------------------------

def test_a_manifest_without_its_lock_file_is_reported():
    result = _evaluate(
        _diff("package.json", removed=['    "left-pad": "1.0.0",'],
              added=['    "left-pad": "1.3.0",']),
        paths=["package.json"],
    )
    assert "deps-manifest-without-lock" in _rules(result)


def test_a_manifest_with_its_lock_file_is_not_reported():
    diff = _diff("package.json", removed=['    "left-pad": "1.0.0",'],
                 added=['    "left-pad": "1.3.0",'])
    result = _evaluate(diff, paths=["package.json", "package-lock.json"])
    assert "deps-manifest-without-lock" not in _rules(result)


def test_a_lock_file_alone_is_only_a_low_severity_note():
    result = _evaluate(_diff("package-lock.json", added=['    "resolved": "x",']),
                       paths=["package-lock.json"])
    finding = next(f for f in result["findings"] if f["rule"] == "deps-lock-without-manifest")
    assert finding["severity"] == "low"


def test_an_ecosystem_with_no_lock_file_concept_is_not_nagged():
    # A Dockerfile has no lock file, so demanding one would be nonsense.
    result = _evaluate(_diff("Dockerfile", removed=["FROM node:20"],
                             added=["FROM node:22"]), paths=["Dockerfile"])
    assert "deps-manifest-without-lock" not in _rules(result)


# -- supply chain shaped changes -------------------------------------------

def test_an_install_hook_is_high_severity():
    result = _evaluate(_diff("package.json", added=['    "postinstall": "node setup.js",']))
    finding = next(f for f in result["findings"] if f["rule"] == "deps-install-hook")
    assert finding["severity"] == "high"


def test_a_git_dependency_source_is_reported():
    result = _evaluate(_diff(
        "package.json", added=['    "thing": "git+https://github.com/x/y.git#main",']
    ))
    assert "deps-vcs-source" in _rules(result)


def test_vcs_sources_can_be_allowed():
    result = _evaluate(
        _diff("package.json", added=['    "thing": "git+https://github.com/x/y.git",']),
        config={"dependency_policy": {"allow_vcs_sources": True}},
    )
    assert "deps-vcs-source" not in _rules(result)


# -- per-ecosystem parsing -------------------------------------------------

def test_go_module_versions_are_parsed():
    result = _evaluate(_diff(
        "go.mod",
        removed=["\tgithub.com/gin-gonic/gin v1.9.1"],
        added=["\tgithub.com/gin-gonic/gin v2.0.0"],
    ), paths=["go.mod", "go.sum"])
    assert "deps-major-bump" in _rules(result)


def test_composer_versions_are_parsed():
    result = _evaluate(_diff(
        "composer.json",
        removed=['        "laravel/framework": "^10.0",'],
        added=['        "laravel/framework": "^11.0",'],
    ), paths=["composer.json", "composer.lock"])
    assert "deps-major-bump" in _rules(result)


def test_a_github_action_version_is_parsed():
    result = _evaluate(_diff(
        ".github/workflows/ci.yml",
        removed=["      - uses: actions/checkout@v4"],
        added=["      - uses: actions/checkout@v5"],
    ))
    names = {p["name"] for p in result["packages"]}
    assert "actions/checkout" in names


def test_a_gemfile_version_is_parsed():
    result = _evaluate(_diff(
        "Gemfile",
        removed=["gem 'rails', '~> 7.0'"],
        added=["gem 'rails', '~> 8.0'"],
    ), paths=["Gemfile", "Gemfile.lock"])
    assert "deps-major-bump" in _rules(result)


def test_comments_are_not_parsed_as_dependencies():
    result = _evaluate(_diff("requirements.txt", added=[
        "# requests==1.0.0", "-r other.txt", "requests==2.31.0",
    ]), paths=["requirements.txt"])
    names = {p["name"] for p in result["packages"]}
    assert names == {"requests"}


# -- the envelope ----------------------------------------------------------

def test_dependency_review_is_advisory_by_default():
    result = _evaluate(_diff(
        "package.json", removed=['    "x": "1.0.0",'], added=['    "x": "2.0.0",'],
    ))
    assert result["status"] == "warn"


def test_failing_can_be_switched_on():
    result = _evaluate(
        _diff("package.json", added=['    "postinstall": "node setup.js",']),
        config={"dependency_policy": {"fail_on": "high"}},
    )
    assert result["status"] == "fail"


def test_the_check_can_be_disabled():
    result = _evaluate(
        _diff("package.json", added=['    "x": "1.0.0",']),
        config={"dependency_policy": {"enabled": False}},
    )
    assert result["status"] == "not_run"


def test_allowlisted_packages_are_not_reported():
    result = _evaluate(
        _diff("package.json", removed=['    "@types/node": "20.0.0",'],
              added=['    "@types/node": "22.0.0",']),
        config={"dependency_policy": {"allow_packages": ["@types/*"]}},
    )
    assert result["packages"] == []
