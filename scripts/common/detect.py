"""Detect a target repository's stack and derive its validation commands.

This is what lets the pipeline run against a repository nobody configured by
hand. Detection reads the repository's file tree (and a handful of manifest files)
over the API — it never checks out or executes target code.

Explicit ``commands`` in ``config/repositories.yml`` always win. Detection is the
fallback, so a repository can start on autopilot and be pinned down later.

Everything here is deliberately conservative:
  * a command is only emitted when the manifest that justifies it exists
  * an npm script is only run when ``package.json`` actually declares it, so we
    never produce a guaranteed-failing ``npm run lint``
  * dependency installs use ``--ignore-scripts`` where the ecosystem supports it
  * per-ecosystem and total caps stop a monorepo producing 200 commands
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

#: Directories whose contents are never treated as project sources.
IGNORED_PARTS = frozenset(
    {
        "node_modules", "vendor", ".git", "dist", "build", "out", "target",
        ".venv", "venv", "__pycache__", ".next", ".nuxt", "coverage",
        "site-packages", ".tox", ".mypy_cache", ".pytest_cache", "bower_components",
    }
)

#: Manifest directories considered per ecosystem, root first.
MAX_DIRS_PER_ECOSYSTEM = 4
#: Total commands emitted, across every ecosystem.
MAX_COMMANDS = 24
#: Manifest files read for their contents (npm scripts, etc.).
MAX_FILE_READS = 8

ReadFile = Callable[[str], "str | None"]


@dataclass(frozen=True)
class Detected:
    """One derived command."""

    name: str
    run: str
    group: str
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "run": self.run, "group": self.group, "reason": self.reason}


def _relevant(paths: Iterable[str]) -> list[str]:
    """Drop vendored and generated paths."""
    out = []
    for path in paths:
        if not isinstance(path, str) or not path.strip():
            continue
        parts = path.split("/")
        if any(part in IGNORED_PARTS for part in parts):
            continue
        out.append(path)
    return out


#: Populated by ``detect`` for the duration of one call; records coverage that
#: the per-ecosystem cap dropped so it is reported rather than hidden.
_NOTES: list[str] = []


def _dirs_with(paths: Sequence[str], filename: str) -> list[str]:
    """Directories containing ``filename``, root first, then shallowest.

    Capped at ``MAX_DIRS_PER_ECOSYSTEM``. Anything dropped is recorded in the
    detection notes: partial coverage must never look like full coverage.
    """
    found = []
    for path in paths:
        head, _, tail = path.rpartition("/")
        if tail == filename:
            found.append(head)
    unique = sorted(set(found), key=lambda d: (d.count("/") if d else -1, d))
    if len(unique) > MAX_DIRS_PER_ECOSYSTEM:
        dropped = unique[MAX_DIRS_PER_ECOSYSTEM:]
        _NOTES.append(
            f"{len(dropped)} of {len(unique)} directories containing {filename} were "
            f"not validated (cap is {MAX_DIRS_PER_ECOSYSTEM}): "
            + ", ".join(d or "root" for d in dropped[:6])
            + ("…" if len(dropped) > 6 else "")
        )
    return unique[:MAX_DIRS_PER_ECOSYSTEM]


def _has_any(paths: Sequence[str], *suffixes: str) -> bool:
    return any(path.endswith(suffixes) for path in paths)


def _label(directory: str, what: str) -> str:
    where = directory or "root"
    return f"{where}: {what}"


def _prefix(directory: str, command: str) -> str:
    """Run a command inside ``directory`` without relying on the caller's cwd."""
    if not directory:
        return command
    return f"sh -c 'cd {directory} && {command}'"


# -- ecosystems ------------------------------------------------------------

def _node(paths: Sequence[str], read: ReadFile, budget: list[int]) -> list[Detected]:
    out: list[Detected] = []
    for directory in _dirs_with(paths, "package.json"):
        manifest = f"{directory}/package.json" if directory else "package.json"
        lock = any(
            (f"{directory}/{name}" if directory else name) in paths
            for name in ("package-lock.json", "npm-shrinkwrap.json")
        )
        install = "npm ci --ignore-scripts" if lock else "npm install --ignore-scripts --no-audit"
        out.append(
            Detected(_label(directory, "npm dependencies"), _prefix(directory, install),
                     "install", f"found {manifest}")
        )

        scripts: dict[str, object] = {}
        if budget[0] > 0:
            budget[0] -= 1
            raw = read(manifest)
            if raw:
                try:
                    scripts = (json.loads(raw) or {}).get("scripts") or {}
                except (json.JSONDecodeError, AttributeError):
                    scripts = {}
        if not isinstance(scripts, dict):
            scripts = {}

        # Only run scripts the manifest actually declares.
        for script, group in (("lint", "lint"), ("typecheck", "lint"),
                              ("test", "test"), ("build", "build")):
            if script in scripts:
                out.append(
                    Detected(
                        _label(directory, f"npm run {script}"),
                        _prefix(directory, f"npm run {script} --if-present"),
                        group,
                        f"{manifest} declares a '{script}' script",
                    )
                )
    return out


def _python(paths: Sequence[str], read: ReadFile, budget: list[int]) -> list[Detected]:
    out: list[Detected] = []
    requirement_files = [
        path for path in paths
        if path.rpartition("/")[2].startswith("requirements") and path.endswith(".txt")
    ][:MAX_DIRS_PER_ECOSYSTEM]
    for path in requirement_files:
        out.append(
            Detected(
                _label(path.rpartition("/")[0], "python dependencies"),
                f"python3 -m pip install --disable-pip-version-check -r {path}",
                "install",
                f"found {path}",
            )
        )

    python_files = [path for path in paths if path.endswith(".py")]
    if python_files:
        out.append(
            Detected(
                "python syntax",
                "python3 -c \"import pathlib,sys;"
                "bad=[str(p) for p in pathlib.Path('.').rglob('*.py')"
                " if not set(p.parts) & {'node_modules','vendor','.venv','venv','build','dist'}"
                " and not __import__('py_compile').compile(str(p), doraise=False)];"
                "sys.exit(1 if bad else 0)\"",
                "lint",
                f"{len(python_files)} python file(s) present",
            )
        )
        has_tests = any(
            part in ("tests", "test") for path in python_files for part in path.split("/")[:-1]
        ) or any(
            path.rpartition("/")[2].startswith("test_") or path.endswith("_test.py")
            for path in python_files
        )
        if has_tests:
            out.append(
                Detected("pytest", "python3 -m pytest -q", "test",
                         "python test files present")
            )
    return out


def _php(paths: Sequence[str], read: ReadFile, budget: list[int]) -> list[Detected]:
    out: list[Detected] = []
    for directory in _dirs_with(paths, "composer.json"):
        out.append(
            Detected(
                _label(directory, "composer dependencies"),
                _prefix(
                    directory,
                    "composer install --no-interaction --no-progress --prefer-dist --no-scripts",
                ),
                "install",
                "found composer.json",
            )
        )
    if _has_any(paths, ".php"):
        out.append(
            Detected(
                "php syntax",
                "sh -c 'find . -path ./vendor -prune -o -name \"*.php\" -print0 "
                "| xargs -0 -r -n1 -P4 php -l > /dev/null'",
                "lint",
                "php files present",
            )
        )
    for directory in _dirs_with(paths, "phpunit.xml") + _dirs_with(paths, "phpunit.xml.dist"):
        out.append(
            Detected(_label(directory, "phpunit"), _prefix(directory, "vendor/bin/phpunit"),
                     "test", "found a phpunit configuration")
        )
    return out


def _go(paths: Sequence[str], read: ReadFile, budget: list[int]) -> list[Detected]:
    out: list[Detected] = []
    for directory in _dirs_with(paths, "go.mod"):
        out.append(Detected(_label(directory, "go build"), _prefix(directory, "go build ./..."),
                            "build", "found go.mod"))
        out.append(Detected(_label(directory, "go vet"), _prefix(directory, "go vet ./..."),
                            "lint", "found go.mod"))
        out.append(Detected(_label(directory, "go test"), _prefix(directory, "go test ./..."),
                            "test", "found go.mod"))
    return out


def _rust(paths: Sequence[str], read: ReadFile, budget: list[int]) -> list[Detected]:
    out: list[Detected] = []
    for directory in _dirs_with(paths, "Cargo.toml"):
        out.append(Detected(_label(directory, "cargo check"),
                            _prefix(directory, "cargo check --locked"), "lint", "found Cargo.toml"))
        out.append(Detected(_label(directory, "cargo test"),
                            _prefix(directory, "cargo test --locked"), "test", "found Cargo.toml"))
    return out


def _ruby(paths: Sequence[str], read: ReadFile, budget: list[int]) -> list[Detected]:
    out: list[Detected] = []
    for directory in _dirs_with(paths, "Gemfile"):
        out.append(Detected(_label(directory, "bundle install"),
                            _prefix(directory, "bundle install --jobs 4"), "install",
                            "found Gemfile"))
        if any(path.startswith((f"{directory}/spec/" if directory else "spec/")) for path in paths):
            out.append(Detected(_label(directory, "rspec"),
                                _prefix(directory, "bundle exec rspec"), "test",
                                "found a spec directory"))
    return out


def _dotnet(paths: Sequence[str], read: ReadFile, budget: list[int]) -> list[Detected]:
    if not _has_any(paths, ".sln", ".csproj", ".fsproj"):
        return []
    return [
        Detected("dotnet restore", "dotnet restore", "install", "found a .NET project"),
        Detected("dotnet build", "dotnet build --no-restore", "build", "found a .NET project"),
        Detected("dotnet test", "dotnet test --no-build", "test", "found a .NET project"),
    ]


def _java(paths: Sequence[str], read: ReadFile, budget: list[int]) -> list[Detected]:
    out: list[Detected] = []
    for directory in _dirs_with(paths, "pom.xml"):
        out.append(Detected(_label(directory, "maven verify"),
                            _prefix(directory, "mvn -B -ntp verify"), "test", "found pom.xml"))
    for name in ("build.gradle", "build.gradle.kts"):
        for directory in _dirs_with(paths, name):
            out.append(Detected(_label(directory, "gradle build"),
                                _prefix(directory, "./gradlew build --no-daemon"),
                                "test", f"found {name}"))
    return out


DETECTORS = (
    ("node", _node),
    ("python", _python),
    ("php", _php),
    ("go", _go),
    ("rust", _rust),
    ("ruby", _ruby),
    ("dotnet", _dotnet),
    ("java", _java),
)

GROUP_ORDER = {"install": 0, "lint": 1, "test": 2, "build": 3}


def detect(
    paths: Sequence[str],
    read_file: ReadFile | None = None,
    *,
    max_commands: int = MAX_COMMANDS,
) -> dict[str, object]:
    """Derive validation commands from a repository's file list.

    Returns ``{"ecosystems": [...], "commands": [...], "truncated": bool}``.
    ``read_file`` is used only to read declared npm scripts; it may be omitted,
    in which case no npm script commands are produced.
    """
    read: ReadFile = read_file or (lambda _path: None)
    budget = [MAX_FILE_READS]
    relevant = _relevant(paths)

    _NOTES.clear()
    ecosystems: list[str] = []
    commands: list[Detected] = []
    for name, detector in DETECTORS:
        found = detector(relevant, read, budget)
        if found:
            ecosystems.append(name)
            commands.extend(found)

    # Install first, then lint, test, build — the order run_commands relies on.
    commands.sort(key=lambda c: (GROUP_ORDER.get(c.group, 9), c.name))
    dropped = max(0, len(commands) - max_commands)
    notes = list(_NOTES)
    if dropped:
        notes.append(f"{dropped} detected command(s) dropped by the {max_commands} command cap.")
    _NOTES.clear()
    return {
        "ecosystems": ecosystems,
        "commands": [c.as_dict() for c in commands[:max_commands]],
        # True when coverage is partial for any reason, not only the command cap.
        "truncated": bool(dropped or notes),
        "dropped": dropped,
        "notes": notes,
        "file_count": len(relevant),
    }
