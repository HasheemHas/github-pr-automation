"""Glob-to-regex conversion, including regex metacharacters in paths."""
from __future__ import annotations

import pytest

from scripts.common.globs import any_match, glob_to_regex, matches, matching_paths


@pytest.mark.parametrize(
    "pattern,path",
    [
        ("backend/**", "backend/app/main.py"),
        ("backend/**", "backend/x"),
        ("backend/**", "backend/a/b/c/d.py"),
        ("frontend/**", "frontend/src/App.tsx"),
        ("**/*.tf", "main.tf"),
        ("**/*.tf", "terraform/main.tf"),
        ("**/*.tf", "a/b/c/net.tf"),
        (".github/workflows/**", ".github/workflows/ci.yml"),
        (".github/workflows/**", ".github/workflows/nested/ci.yml"),
        ("**/Dockerfile", "Dockerfile"),
        ("**/Dockerfile", "backend/Dockerfile"),
        ("**/Dockerfile", "a/b/Dockerfile"),
        ("**/*.test.*", "src/app.test.js"),
        ("**/*.test.*", "app.test.tsx"),
        ("tests/**", "tests/test_thing.py"),
        ("tests/**", "tests/unit/test_thing.py"),
        ("docker-compose.*", "docker-compose.yml"),
        ("**/auth/**", "backend/app/auth/jwt.py"),
        ("src/?.py", "src/a.py"),
    ],
)
def test_patterns_that_must_match(pattern, path):
    assert matches(pattern, path)


@pytest.mark.parametrize(
    "pattern,path",
    [
        ("backend/**", "backend"),
        ("backend/**", "backendx/main.py"),
        ("backend/**", "app/backend/main.py"),
        ("**/*.tf", "main.tfvars"),
        ("**/*.tf", "main.tf.json"),
        (".github/workflows/**", ".github/CODEOWNERS"),
        ("**/Dockerfile", "Dockerfile.dev"),
        ("**/Dockerfile", "MyDockerfile"),
        ("tests/**", "backend/tests/test_thing.py"),
        ("src/*.py", "src/nested/a.py"),
        ("src/?.py", "src/ab.py"),
        ("docker-compose.*", "sub/docker-compose.yml"),
    ],
)
def test_patterns_that_must_not_match(pattern, path):
    assert not matches(pattern, path)


@pytest.mark.parametrize(
    "path",
    [
        "src/a[1].js",
        "src/a].js",
        "src/a[.js",
        "src/(x).py",
        "src/a+b.py",
        "src/a.b.py",
        "src/back\\slash.py",
        "src/$dollar.py",
        "src/c^aret.py",
        "src/{brace}.py",
        "src/pi|pe.py",
    ],
)
def test_regex_metacharacters_in_a_path_are_matched_literally(path):
    # A pattern equal to the path must match it, and must not match a sibling.
    assert matches(path, path)
    assert not matches(path, path + "x")


def test_a_broken_escape_would_have_leaked_a_character_class():
    # Regression guard for the original `[.+^${}()|[\\]\\]` character class, which
    # terminated early and left `[` unescaped.
    assert matches("src/[abc].py", "src/[abc].py")
    assert not matches("src/[abc].py", "src/a.py")


def test_backslash_is_not_an_escape_character():
    assert matches("a\\*b", "a\\*b")
    assert not matches("a\\*b", "a*b")


def test_star_does_not_cross_a_directory_boundary():
    assert not matches("*.py", "pkg/mod.py")
    assert matches("*.py", "mod.py")


def test_double_star_crosses_directory_boundaries():
    assert matches("a/**/z.py", "a/b/c/z.py")
    assert matches("a/**/z.py", "a/z.py")


def test_leading_dot_slash_is_normalised():
    assert matches("backend/**", "./backend/app.py")


def test_any_match_and_matching_paths():
    paths = ["backend/a.py", "frontend/b.tsx", "README.md"]
    assert any_match(["backend/**"], paths)
    assert not any_match(["helm/**"], paths)
    assert matching_paths(["backend/**", "**/*.md"], paths) == ["backend/a.py", "README.md"]


def test_compiled_patterns_are_cached():
    assert glob_to_regex("backend/**") is glob_to_regex("backend/**")
