"""Stack detection: run against a repository nobody configured by hand."""
from __future__ import annotations

import json

import pytest

from scripts.common.config import (
    Target,
    commands_for,
    detected_commands,
    has_explicit_commands,
)
from scripts.common.detect import MAX_COMMANDS, detect


def reader(files: dict[str, str]):
    return lambda path: files.get(path)


def groups(result) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for command in result["commands"]:
        out.setdefault(command["group"], []).append(command["run"])
    return out


# -- node ------------------------------------------------------------------

def test_a_lockfile_gives_npm_ci_and_no_lockfile_gives_npm_install():
    with_lock = detect(["package.json", "package-lock.json"])
    without = detect(["package.json"])
    assert "npm ci --ignore-scripts" in groups(with_lock)["install"]
    assert any("npm install" in c for c in groups(without)["install"])


def test_dependency_installs_ignore_lifecycle_scripts():
    result = detect(["package.json", "package-lock.json"])
    assert all("--ignore-scripts" in c for c in groups(result)["install"])


def test_only_declared_npm_scripts_are_run():
    files = {"package.json": json.dumps({"scripts": {"lint": "eslint .", "build": "vite build"}})}
    result = detect(["package.json", "package-lock.json"], reader(files))
    ran = groups(result)
    assert any("npm run lint" in c for c in ran["lint"])
    assert any("npm run build" in c for c in ran["build"])
    # No 'test' script declared, so no test command is invented.
    assert "test" not in ran


def test_a_missing_package_json_body_yields_install_only():
    # Without file contents we cannot know which scripts exist, so none are run.
    result = detect(["package.json", "package-lock.json"])
    assert list(groups(result)) == ["install"]


def test_malformed_package_json_does_not_crash_detection():
    result = detect(["package.json"], reader({"package.json": "{not json"}))
    assert result["ecosystems"] == ["node"]
    assert list(groups(result)) == ["install"]


def test_a_monorepo_detects_each_workspace():
    files = {
        "frontend/package.json": json.dumps({"scripts": {"build": "vite build"}}),
        "admin/package.json": json.dumps({"scripts": {"lint": "eslint ."}}),
    }
    result = detect(
        ["frontend/package.json", "frontend/package-lock.json",
         "admin/package.json", "admin/package-lock.json"],
        reader(files),
    )
    installs = groups(result)["install"]
    assert len(installs) == 2
    assert any("cd frontend" in c for c in installs)
    assert any("cd admin" in c for c in installs)


def test_node_modules_and_vendor_are_ignored():
    result = detect(
        ["node_modules/left-pad/package.json", "backend/vendor/foo/composer.json",
         "package.json"]
    )
    assert len(groups(result)["install"]) == 1
    assert result["ecosystems"] == ["node"]


# -- python ----------------------------------------------------------------

def test_requirements_files_become_pip_installs():
    result = detect(["requirements.txt", "backend/requirements-dev.txt", "app.py"])
    installs = groups(result)["install"]
    assert any("-r requirements.txt" in c for c in installs)
    assert any("-r backend/requirements-dev.txt" in c for c in installs)


def test_python_files_get_a_syntax_check():
    result = detect(["app.py"])
    assert any("py_compile" in c for c in groups(result)["lint"])


def test_pytest_is_only_added_when_tests_exist():
    with_tests = detect(["app.py", "tests/test_app.py"])
    without = detect(["app.py"])
    assert any("pytest" in c for c in groups(with_tests)["test"])
    assert "test" not in groups(without)


def test_a_test_prefixed_file_counts_as_a_test():
    result = detect(["app.py", "backend/test_login.py"])
    assert "test" in groups(result)


# -- php -------------------------------------------------------------------

def test_composer_and_php_lint_are_detected():
    result = detect(["backend/composer.json", "index.php", "backend/api.php"])
    assert any("composer install" in c for c in groups(result)["install"])
    assert any("php -l" in c for c in groups(result)["lint"])
    assert "test" not in groups(result)


def test_phpunit_config_adds_a_test_command():
    result = detect(["composer.json", "phpunit.xml", "index.php"])
    assert any("phpunit" in c for c in groups(result)["test"])


def test_php_lint_prunes_vendor():
    result = detect(["index.php"])
    assert any("-path ./vendor -prune" in c for c in groups(result)["lint"])


# -- other ecosystems ------------------------------------------------------

@pytest.mark.parametrize(
    "paths,ecosystem,expected",
    [
        (["go.mod", "main.go"], "go", "go test ./..."),
        (["Cargo.toml", "src/main.rs"], "rust", "cargo test --locked"),
        (["Gemfile", "spec/a_spec.rb"], "ruby", "bundle exec rspec"),
        (["App.sln", "App/App.csproj"], "dotnet", "dotnet test --no-build"),
        (["pom.xml"], "java", "mvn -B -ntp verify"),
        (["build.gradle"], "java", "./gradlew build --no-daemon"),
    ],
)
def test_each_ecosystem_is_recognised(paths, ecosystem, expected):
    result = detect(paths)
    assert ecosystem in result["ecosystems"]
    assert any(expected in c["run"] for c in result["commands"])


def test_an_unrecognised_repository_yields_nothing():
    result = detect(["README.md", "docs/guide.md", "LICENSE"])
    assert result["ecosystems"] == []
    assert result["commands"] == []


def test_an_empty_tree_is_safe():
    result = detect([])
    assert result["commands"] == []
    assert result["file_count"] == 0


def test_a_polyglot_repository_detects_every_stack():
    result = detect(
        ["package.json", "package-lock.json", "backend/composer.json", "index.php",
         "backend/ai/requirements.txt", "backend/ai/main.py"]
    )
    assert set(result["ecosystems"]) == {"node", "python", "php"}


# -- ordering and caps -----------------------------------------------------

def test_install_commands_come_first():
    result = detect(
        ["package.json", "package-lock.json", "app.py", "requirements.txt",
         "tests/test_app.py"],
        reader({"package.json": json.dumps({"scripts": {"build": "x"}})}),
    )
    order = [c["group"] for c in result["commands"]]
    assert order == sorted(order, key=lambda g: {"install": 0, "lint": 1,
                                                 "test": 2, "build": 3}[g])


def test_a_huge_monorepo_reports_the_workspaces_it_skipped():
    paths = []
    for i in range(40):
        paths += [f"pkg{i}/package.json", f"pkg{i}/package-lock.json"]
    result = detect(paths)
    # The per-ecosystem cap limits how many workspaces are validated...
    assert len(result["commands"]) == 4
    # ...and says so, rather than looking like full coverage.
    assert result["truncated"] is True
    assert any("were not validated" in note for note in result["notes"])
    assert any("36 of 40" in note for note in result["notes"])


def test_the_command_cap_is_also_reported():
    paths = ["app.py", "requirements.txt", "tests/test_app.py", "index.php",
             "composer.json", "go.mod"]
    result = detect(paths, max_commands=2)
    assert len(result["commands"]) == 2
    assert result["dropped"] > 0
    assert any("command cap" in note for note in result["notes"])


def test_full_coverage_reports_no_notes():
    result = detect(["package.json", "package-lock.json"])
    assert result["truncated"] is False
    assert result["notes"] == []


def test_notes_do_not_leak_between_calls():
    paths = [f"pkg{i}/package.json" for i in range(40)]
    detect(paths)
    assert detect(["package.json"])["notes"] == []


def test_the_default_cap_is_applied():
    paths = []
    for i in range(40):
        paths += [f"pkg{i}/package.json", f"pkg{i}/package-lock.json"]
    assert len(detect(paths)["commands"]) <= MAX_COMMANDS


def test_manifest_reads_are_budgeted():
    reads: list[str] = []

    def counting(path):
        reads.append(path)
        return json.dumps({"scripts": {"lint": "x"}})

    paths = []
    for i in range(20):
        paths += [f"pkg{i}/package.json", f"pkg{i}/package-lock.json"]
    detect(paths, counting)
    # Only a handful of manifests are fetched, whatever the repository size.
    assert len(reads) <= 8


# -- integration with config ----------------------------------------------

DETECTION = {
    "commands": [
        {"name": "npm dependencies", "run": "npm ci --ignore-scripts", "group": "install"},
        {"name": "npm run lint", "run": "npm run lint", "group": "lint"},
    ]
}


def test_detected_commands_are_used_when_config_declares_none():
    target = Target(owner="o", repo="r", config={})
    assert not has_explicit_commands(target)
    commands = commands_for(target, "install", detection=DETECTION)
    assert [c.run for c in commands] == ["npm ci --ignore-scripts"]


def test_explicit_config_always_wins_over_detection():
    target = Target(owner="o", repo="r", config={"commands": {"install": ["make deps"]}})
    assert has_explicit_commands(target)
    commands = commands_for(target, "install", detection=DETECTION)
    assert [c.run for c in commands] == ["make deps"]


def test_explicit_config_in_one_group_disables_detection_for_all_groups():
    # Half-detected, half-configured commands would be unpredictable, so declaring
    # any command opts the repository out of detection entirely.
    target = Target(owner="o", repo="r", config={"commands": {"install": ["make deps"]}})
    assert commands_for(target, "lint", detection=DETECTION) == []


def test_detection_is_ignored_when_not_supplied():
    target = Target(owner="o", repo="r", config={})
    assert commands_for(target, "install") == []


def test_an_empty_commands_block_is_not_explicit():
    target = Target(owner="o", repo="r",
                    config={"commands": {"install": [], "lint": [], "test": [], "build": []}})
    assert not has_explicit_commands(target)
    assert [c.run for c in commands_for(target, "install", detection=DETECTION)] == [
        "npm ci --ignore-scripts"
    ]


def test_detected_commands_filters_by_group():
    assert [c.name for c in detected_commands(DETECTION, "lint")] == ["npm run lint"]
    assert detected_commands(DETECTION, "build") == []


def test_a_malformed_detection_payload_is_ignored():
    assert detected_commands(None, "install") == []
    assert detected_commands({}, "install") == []
    assert detected_commands({"commands": [{"group": "install"}]}, "install") == []
    assert detected_commands({"commands": "nope"}, "install") == []
