"""Derive a pull request description from the diff, not from a template.

The problem this solves: a template like ``"Update {path}."`` rendered once per
changed path produces a description that says nothing a reviewer could not get
from the Files tab, lists build artefacts as if they were work, and repeats itself
across sections. A "Test Plan" reading *Run the configured checks for .github* is
worse than no test plan, because it looks like advice.

So every line of the description here is derived from what the diff actually
contains:

  * build artefacts and generated files are recognised and collapsed into one
    line that says what they are, never listed as individual changes
  * a code file is described by the symbols it added and removed; a workflow by
    the jobs and actions it changed; a manifest by the dependencies it moved
  * the test plan names the tests that actually cover the changed files, and the
    files nothing covers
  * user impact is stated only where a real signal supports it, and says what was
    checked when there is none

Templates remain available for wording that must match a house style, but they
are overrides layered on top of derived content, never the source of it.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from ..common.globs import matches
from .changes import FileChange, Line

# -- noise -------------------------------------------------------------------
#: ``(label, patterns, advice, singular noun)``. A file matching one of these is
#: never listed as an individual change: it is build output, a lock file, or
#: vendored code.
NOISE: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("compiled Python bytecode", ("**/__pycache__/**", "**/*.pyc", "**/*.pyo"),
     "These should not be tracked; add `__pycache__/` and `*.pyc` to `.gitignore` "
     "and untrack them.",
     "compiled Python bytecode file"),
    ("dependency lock file", (
        "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml", "**/poetry.lock",
        "**/Pipfile.lock", "**/composer.lock", "**/Gemfile.lock", "**/Cargo.lock",
        "**/go.sum", "**/npm-shrinkwrap.json", "**/uv.lock"),
     "Resolved dependency versions; review the manifest change instead.",
     "dependency lock file"),
    ("vendored dependency", ("vendor/**", "**/vendor/**", "**/node_modules/**"),
     "Third-party code checked into the repository.",
     "vendored dependency file"),
    ("build output", ("**/dist/**", "**/build/**", "**/*.min.js", "**/*.min.css",
                      "**/*.map", "**/.next/**", "**/out/**"),
     "Generated at build time.",
     "build output file"),
    ("generated code", ("**/*.pb.go", "**/*_pb2.py", "**/*_pb2_grpc.py",
                        "**/*.generated.*", "**/generated/**", "**/*.g.dart"),
     "Produced by a code generator; review the schema or generator input.",
     "generated source file"),
    ("test snapshot", ("**/__snapshots__/**", "**/*.snap"),
     "Recorded test output; confirm the change was intended, not accepted blindly.",
     "test snapshot"),
    ("compiled artefact", ("**/*.class", "**/*.o", "**/*.so", "**/*.dll", "**/*.exe",
                           "**/*.jar", "**/*.war"),
     "Binary build output.",
     "compiled artefact"),
)

#: Extension -> a human word for the kind of file, for files with no symbols.
KIND_BY_EXTENSION: dict[str, str] = {
    ".md": "documentation", ".rst": "documentation", ".txt": "text",
    ".json": "configuration", ".yml": "configuration", ".yaml": "configuration",
    ".toml": "configuration", ".ini": "configuration", ".cfg": "configuration",
    ".env": "environment configuration", ".sql": "SQL", ".css": "styles",
    ".scss": "styles", ".html": "markup", ".svg": "image", ".png": "image",
    ".jpg": "image", ".gif": "image", ".ico": "image", ".sh": "shell script",
    ".dockerfile": "container build", ".tf": "infrastructure definition",
}

# -- symbol extraction -------------------------------------------------------
#: ``(extensions, pattern, kind)``. ``name`` is the only required group.
SYMBOL_PATTERNS: tuple[tuple[tuple[str, ...], re.Pattern[str], str], ...] = (
    ((".py",), re.compile(r"^(?:async\s+)?def\s+(?P<name>\w+)\s*\("), "function"),
    ((".py",), re.compile(r"^class\s+(?P<name>\w+)"), "class"),
    ((".py",), re.compile(r"^(?P<name>[A-Z][A-Z0-9_]{2,})\s*[:=]"), "constant"),
    ((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
     re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>\w+)"),
     "function"),
    ((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
     re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*=\s*"
                r"(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"), "function"),
    ((".ts", ".tsx"),
     re.compile(r"^(?:export\s+)?(?:interface|type|enum)\s+(?P<name>\w+)"), "type"),
    ((".js", ".jsx", ".ts", ".tsx"), re.compile(r"^(?:export\s+)?class\s+(?P<name>\w+)"),
     "class"),
    ((".php",),
     re.compile(r"^\s*(?:public\s+|private\s+|protected\s+)?(?:static\s+)?function\s+"
                r"(?P<name>\w+)"), "function"),
    ((".php",), re.compile(r"^\s*(?:final\s+|abstract\s+)?class\s+(?P<name>\w+)"), "class"),
    ((".go",), re.compile(r"^func\s+(?:\([^)]*\)\s*)?(?P<name>\w+)\s*\("), "function"),
    ((".go",), re.compile(r"^type\s+(?P<name>\w+)\s+(?:struct|interface)"), "type"),
    ((".rb",), re.compile(r"^\s*def\s+(?P<name>[\w?!]+)"), "method"),
    ((".rb",), re.compile(r"^\s*class\s+(?P<name>\w+)"), "class"),
    ((".java", ".kt"),
     re.compile(r"^\s*(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],.]+\s+"
                r"(?P<name>\w+)\s*\("), "method"),
    ((".java", ".kt"),
     re.compile(r"^\s*(?:public\s+)?(?:final\s+|abstract\s+|data\s+)?class\s+(?P<name>\w+)"),
     "class"),
    ((".cs",),
     re.compile(r"^\s*(?:public|private|internal|protected)\s+(?:static\s+|async\s+)*"
                r"[\w<>\[\],.]+\s+(?P<name>\w+)\s*\("), "method"),
    ((".sql",),
     re.compile(r"(?i)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|INDEX|FUNCTION)\s+"
                r"(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(?P<name>[\w.]+)"), "object"),
)

#: A workflow job key: exactly two spaces of indent inside a `jobs:` mapping.
_WORKFLOW_JOB = re.compile(r"^  (?P<name>[A-Za-z_][\w-]*):\s*$")
_WORKFLOW_USES = re.compile(r"^\s*-?\s*uses:\s*(?P<name>[^\s#]+)")
_WORKFLOW_TRIGGER = re.compile(r"^  (?P<name>push|pull_request|schedule|workflow_call|"
                               r"workflow_dispatch|release|issue_comment):")
#: A top-level key in a flat config file.
_CONFIG_KEY = re.compile(r"^(?P<name>[A-Za-z_][\w.-]*)\s*[:=]")
#: A markdown heading, which is what a docs change actually adds.
_HEADING = re.compile(r"^#{1,4}\s+(?P<name>.+?)\s*$")

WORKFLOW_PATTERNS = (".github/workflows/**", ".gitlab-ci.yml", "**/azure-pipelines.yml")

#: Dependency manifests, described by the packages they move rather than by their
#: file type. "requirements.txt — text updated" says nothing.
MANIFEST_PATTERNS = (
    "**/requirements*.txt", "**/pyproject.toml", "**/Pipfile", "**/setup.py",
    "**/package.json", "**/composer.json", "**/go.mod", "**/Gemfile",
    "**/Cargo.toml", "**/pom.xml", "**/*.csproj", "**/build.gradle",
)

#: ORM migration operations, whose names are the description of the migration.
_MIGRATION_OPERATION = re.compile(
    r"""(?x)
    \b(?:migrations\.|op\.|Schema::|schema_editor\.)?
    (?P<name>AddField|RemoveField|RenameField|AlterField|AddIndex|RemoveIndex|
       CreateModel|DeleteModel|RenameModel|AlterUniqueTogether|RunPython|RunSQL|
       add_column|drop_column|rename_column|alter_column|create_table|drop_table|
       add_index|create_index|execute)\b
    """
)
MIGRATION_PATTERNS = (
    "**/migrations/**", "**/migrate/**", "**/versions/*.py", "**/db/migration/**",
)


def is_noise(path: str) -> tuple[str, str] | None:
    """Return ``(label, advice)`` when a path is build output or vendored code."""
    for label, patterns, advice, _noun in NOISE:
        if any(matches(pattern, path) for pattern in patterns):
            return label, advice
    return None


def _noise_noun(label: str, count: int) -> str:
    """The countable noun for a noise kind, correctly pluralised."""
    for entry_label, _patterns, _advice, noun in NOISE:
        if entry_label == label:
            head, _, last = noun.rpartition(" ")
            return noun if count == 1 else (
                f"{head} {_plural(last, count)}".strip()
            )
    return _plural("file", count)


def group_noise(paths: Iterable[str]) -> list[dict[str, Any]]:
    """Collapse noise paths into one entry per kind, with a directory hint."""
    buckets: dict[str, dict[str, Any]] = {}
    for path in paths:
        found = is_noise(path)
        if not found:
            continue
        label, advice = found
        bucket = buckets.setdefault(label, {"label": label, "advice": advice,
                                            "paths": [], "directories": set()})
        bucket["paths"].append(path)
        parent = str(PurePosixPath(path).parent)
        if parent and parent != ".":
            bucket["directories"].add(parent)
    return [
        {
            "label": bucket["label"],
            "advice": bucket["advice"],
            "count": len(bucket["paths"]),
            "paths": sorted(bucket["paths"])[:20],
            "directories": sorted(bucket["directories"])[:5],
        }
        for bucket in sorted(buckets.values(), key=lambda b: -len(b["paths"]))
    ]


def _symbols(lines: Sequence[Line], extension: str) -> list[tuple[str, str]]:
    """``[(kind, name)]`` for the declarations in a set of diff lines."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in lines:
        text = line.text
        for extensions, pattern, kind in SYMBOL_PATTERNS:
            if extension not in extensions:
                continue
            match = pattern.match(text)
            if not match:
                continue
            name = (match.group("name") or "").strip()
            if not name or name in seen or name.startswith("_") and kind != "constant":
                continue
            seen.add(name)
            found.append((kind, name))
            break
    return found


def _workflow_facts(change: FileChange) -> dict[str, list[str]]:
    """Jobs, triggers and actions a workflow change touches."""
    def collect(lines: Sequence[Line]) -> dict[str, list[str]]:
        jobs, uses, triggers = [], [], []
        for line in lines:
            job = _WORKFLOW_JOB.match(line.text)
            trigger = _WORKFLOW_TRIGGER.match(line.text)
            action = _WORKFLOW_USES.match(line.text)
            if trigger and trigger.group("name") not in triggers:
                triggers.append(trigger.group("name"))
            elif job and job.group("name") not in jobs:
                jobs.append(job.group("name"))
            if action and action.group("name") not in uses:
                uses.append(action.group("name"))
        return {"jobs": jobs, "uses": uses, "triggers": triggers}

    added = collect(change.added)
    removed = collect(change.removed)
    return {
        "jobs_added": [j for j in added["jobs"] if j not in removed["jobs"]],
        "jobs_removed": [j for j in removed["jobs"] if j not in added["jobs"]],
        "triggers_added": [t for t in added["triggers"] if t not in removed["triggers"]],
        "actions_added": [u for u in added["uses"] if u not in removed["uses"]],
    }


def _config_keys(change: FileChange) -> dict[str, list[str]]:
    def keys(lines: Sequence[Line]) -> list[str]:
        found: list[str] = []
        for line in lines:
            match = _CONFIG_KEY.match(line.text)
            if match and match.group("name") not in found:
                found.append(match.group("name"))
        return found

    added, removed = keys(change.added), keys(change.removed)
    return {
        "added": [k for k in added if k not in removed],
        "removed": [k for k in removed if k not in added],
    }


def _manifest_facts(change: FileChange) -> list[str]:
    """Describe a dependency manifest by the packages it moves."""
    from .deps import ECOSYSTEMS, diff_dependencies

    ecosystem = next(
        (name for name, (manifests, _locks) in ECOSYSTEMS.items()
         if any(matches(pattern, change.path) for pattern in manifests)),
        "",
    )
    if not ecosystem:
        return []
    packages = diff_dependencies(change, ecosystem)["packages"]
    parts: list[str] = []
    changed = [p for p in packages if p["kind"] == "changed"]
    added = [p for p in packages if p["kind"] == "added"]
    removed = [p for p in packages if p["kind"] == "removed"]
    if changed:
        # The comparison operator belongs in the manifest, not in prose:
        # "django 4.2.11 → 5.0.3" reads; "django ==4.2.11 → ==5.0.3" does not.
        def version(value: str) -> str:
            return value.lstrip("=~^><! ").strip() or "unspecified"

        parts.append(
            "updates " + _join(
                [f"{p['name']} {version(p['from'])} → {version(p['to'])}" for p in changed],
                limit=3,
            )
        )
    if added:
        parts.append(f"adds {_join([p['name'] for p in added], limit=4)}")
    if removed:
        parts.append(f"removes {_join([p['name'] for p in removed], limit=4)}")
    return parts


def _migration_facts(change: FileChange) -> list[str]:
    """Describe a migration by the schema operations it performs."""
    operations: list[str] = []
    for line in change.added:
        for match in _MIGRATION_OPERATION.finditer(line.text):
            name = match.group("name")
            if name not in operations:
                operations.append(name)
    if not operations:
        return []
    return [f"schema operations {_join(operations, limit=4)}"]


def _headings(change: FileChange) -> list[str]:
    found: list[str] = []
    for line in change.added:
        match = _HEADING.match(line.text)
        if match and match.group("name") not in found:
            found.append(match.group("name"))
    return found


def _join(values: Sequence[str], *, limit: int = 4, code: bool = True) -> str:
    """Render a short list as prose: ``a``, ``b`` and 3 more."""
    shown = [f"`{v}`" if code else str(v) for v in values[:limit]]
    extra = len(values) - len(shown)
    if not shown:
        return ""
    if extra > 0:
        return ", ".join(shown) + f" and {extra} more"
    if len(shown) == 1:
        return shown[0]
    return ", ".join(shown[:-1]) + f" and {shown[-1]}"


def _plural(word: str, count: int) -> str:
    """``job`` / ``jobs``, ``dependency`` / ``dependencies``.

    A description is prose a person reads; "1 job(s)" and "2 dependencys" both
    undermine the impression that anything thought about the output.
    """
    if count == 1:
        return word
    if word.endswith("y") and word[-2:-1] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "ch", "sh")):
        return word + "es"
    return word + "s"


def describe_file(change: FileChange, stats: Mapping[str, Any] | None = None) -> str:
    """One sentence about what a single file's change actually does.

    Reads the diff for declarations, workflow jobs, config keys or headings, and
    falls back to the line counts only when nothing more specific is available.
    """
    path = change.path
    extension = PurePosixPath(path).suffix.lower()
    counts = stats or {}
    added_lines = int(counts.get("additions") or len(change.added))
    removed_lines = int(counts.get("deletions") or len(change.removed))
    scale = f"+{added_lines}/-{removed_lines}"

    if change.is_binary:
        return f"`{path}` — binary file changed ({scale})"
    if change.is_rename and not change.added and not change.removed:
        return f"`{path}` — renamed from `{change.old_path}`"

    verb = "new file" if change.is_new else "deleted" if change.is_deleted else None
    parts: list[str] = []

    if any(matches(pattern, path) for pattern in WORKFLOW_PATTERNS):
        facts = _workflow_facts(change)
        if facts["jobs_added"]:
            count = len(facts["jobs_added"])
            parts.append(
                f"adds the {_join(facts['jobs_added'])} {_plural('job', count)}"
            )
        if facts["jobs_removed"]:
            count = len(facts["jobs_removed"])
            parts.append(
                f"removes the {_join(facts['jobs_removed'])} {_plural('job', count)}"
            )
        if facts["triggers_added"]:
            parts.append(f"triggers on {_join(facts['triggers_added'])}")
        if facts["actions_added"]:
            parts.append(f"uses {_join(facts['actions_added'], limit=3)}")
    elif any(matches(pattern, path) for pattern in MANIFEST_PATTERNS):
        parts.extend(_manifest_facts(change))
    elif any(matches(pattern, path) for pattern in MIGRATION_PATTERNS):
        parts.extend(_migration_facts(change))
        if not parts:
            parts.extend(
                f"adds {kind} `{name}`" for kind, name in
                _symbols(change.added, extension)[:2]
            )
    else:
        added_symbols = _symbols(change.added, extension)
        removed_symbols = _symbols(change.removed, extension)
        added_names = [name for _kind, name in added_symbols]
        removed_names = [
            name for kind, name in removed_symbols if name not in added_names
        ]
        kinds = {kind for kind, _name in added_symbols} or {
            kind for kind, _name in removed_symbols
        }
        noun = (kinds.pop() if len(kinds) == 1 else "declaration") if kinds else ""
        if added_names:
            parts.append(f"adds {noun or 'declaration'} {_join(added_names)}".replace(
                "adds declaration ", "adds "))
        if removed_names:
            parts.append(f"removes {_join(removed_names)}")

        if not parts and extension in (".yml", ".yaml", ".json", ".toml", ".ini", ".cfg",
                                       ".env", ".properties"):
            keys = _config_keys(change)
            if keys["added"]:
                parts.append(f"adds {_join(keys['added'])}")
            if keys["removed"]:
                parts.append(f"removes {_join(keys['removed'])}")
        if not parts and extension in (".md", ".rst"):
            headings = _headings(change)
            if headings:
                parts.append(f"documents {_join(headings, code=False)}")

    if not parts:
        kind = KIND_BY_EXTENSION.get(extension, "")
        parts.append(f"{kind} updated" if kind else "content changed")

    prefix = f"`{path}` — " + (f"{verb}, " if verb else "")
    return prefix + "; ".join(parts) + f" ({scale})"


def _component_of(path: str) -> str:
    parts = PurePosixPath(path).parts
    return parts[0] if len(parts) > 1 else "root"


def detail_bullets(
    changes: Mapping[str, FileChange],
    context: Mapping[str, Any],
    *,
    limit: int = 20,
) -> list[str]:
    """Per-file bullets, grouped by component, with noise collapsed into one line."""
    from .changes import file_stats

    stats = file_stats(context)
    paths = [str(p) for p in context.get("files") or []] or sorted(changes)
    real = [p for p in paths if not is_noise(p)]
    noise = group_noise(paths)

    grouped: dict[str, list[str]] = {}
    for path in real:
        change = changes.get(path)
        sentence = (
            describe_file(change, stats.get(path)) if change is not None
            else f"`{path}` — changed"
        )
        grouped.setdefault(_component_of(path), []).append(sentence)

    bullets: list[str] = []
    multi = len(grouped) > 1
    remaining = limit
    for component, sentences in sorted(grouped.items()):
        if remaining <= 0:
            break
        head = sentences[:remaining]
        remaining -= len(head)
        if multi:
            bullets.append(
                f"**{component}** ({len(sentences)} {_plural('file', len(sentences))})"
            )
            bullets.extend(f"  - {line}" for line in head)
        else:
            bullets.extend(head)
        if len(sentences) > len(head):
            bullets.append(f"  - _…and {len(sentences) - len(head)} more in `{component}`_")

    for bucket in noise:
        location = (
            f" under {_join(bucket['directories'], limit=2)}" if bucket["directories"] else ""
        )
        bullets.append(
            f"{bucket['count']} {_noise_noun(bucket['label'], bucket['count'])}"
            f"{location} — {bucket['advice']}"
        )
    return bullets


def at_a_glance(
    changes: Mapping[str, FileChange],
    context: Mapping[str, Any],
    *,
    change_type: str = "",
    risk: str = "",
    risk_reason: str = "",
    insights: Mapping[str, Any] | None = None,
) -> str:
    """Two or three sentences that are not just the file list again."""
    from .changes import file_stats

    stats = file_stats(context)
    paths = [str(p) for p in context.get("files") or []] or sorted(changes)
    real = [p for p in paths if not is_noise(p)]
    noise_count = len(paths) - len(real)
    components = sorted({_component_of(p) for p in real})
    reviewable_lines = sum(
        int((stats.get(p) or {}).get("additions") or 0)
        + int((stats.get(p) or {}).get("deletions") or 0)
        for p in real
    ) or sum(len(changes[p].added) + len(changes[p].removed) for p in real if p in changes)

    kind_word = {
        "feature": "Adds functionality", "fix": "Fixes a defect",
        "refactor": "Restructures existing code without changing behaviour",
        "documentation": "Updates documentation", "test": "Changes tests only",
        "security": "Addresses a security concern", "chore": "Routine maintenance",
    }.get(change_type, "Changes the repository")

    scope = (
        f"{len(real)} {_plural('file', len(real))} across {_join(components, limit=3)}"
        if components else f"{len(real)} {_plural('file', len(real))}"
    )
    sentences = [f"{kind_word} in {scope} ({reviewable_lines} changed "
                 f"{_plural('line', reviewable_lines)})."]
    if noise_count:
        sentences.append(
            f"A further {noise_count} {_plural('file', noise_count)} "
            + ("is build output or a lock file and needs" if noise_count == 1
               else "are build output, lock files or vendored code and need")
            + " no review."
        )

    highlights = _highlights(insights or {}, changes, real)
    if highlights:
        sentences.append("Worth knowing: " + "; ".join(highlights) + ".")
    if risk:
        sentences.append(
            f"**Risk: {risk.title()}**" + (f" — {risk_reason}" if risk_reason else "") + ""
        )
    return " ".join(part for part in sentences if part)


def _highlights(
    insights: Mapping[str, Any],
    changes: Mapping[str, FileChange],
    paths: Sequence[str],
) -> list[str]:
    """The handful of facts a reviewer needs before opening the diff."""
    out: list[str] = []

    def data(key: str) -> dict[str, Any]:
        entry = insights.get(key)
        if isinstance(entry, Mapping):
            inner = entry.get("data")
            return dict(inner) if isinstance(inner, Mapping) else dict(entry)
        return {}

    secrets = data("secrets")
    if secrets.get("hits"):
        count = len(secrets["hits"])
        out.append(
            f"{count} credential-shaped {_plural('value', count)} "
            + ("was" if count == 1 else "were") + " added"
        )

    api = data("api")
    removed_routes = (api.get("routes") or {}).get("removed") or []
    if removed_routes:
        count = len(removed_routes)
        out.append(f"{count} {_plural('route', count)} no longer declared")
    elif (api.get("routes") or {}).get("added"):
        count = len((api["routes"])["added"])
        out.append(f"{count} new {_plural('route', count)}")

    migrations = data("migrations").get("migrations") or []
    if migrations:
        risky = [m for m in migrations if m.get("risky_operations")]
        out.append(
            f"{len(migrations)} database {_plural('migration', len(migrations))}"
            + (f", {len(risky)} with destructive or locking operations" if risky else "")
        )

    dependencies = data("dependencies")
    totals = dependencies.get("totals") or {}
    ecosystems = set(dependencies.get("ecosystems") or {})
    # A pinned action version is a dependency, but announcing "2 new dependencies"
    # for a workflow edit reads as a package install. The workflow line below
    # already says what changed, so only real package ecosystems are called out.
    package_ecosystems = ecosystems - {"actions"}
    if totals.get("major_bumps") and package_ecosystems:
        count = totals["major_bumps"]
        out.append(f"{count} major dependency {_plural('upgrade', count)}")
    elif totals.get("added") and package_ecosystems:
        count = totals["added"]
        out.append(f"{count} new {_plural('dependency', count)}")

    workflows = [p for p in paths if any(matches(g, p) for g in WORKFLOW_PATTERNS)]
    if workflows:
        out.append(
            f"{len(workflows)} CI workflow {_plural('file', len(workflows))} changed"
        )
    return out[:4]


def test_plan(
    context: Mapping[str, Any],
    *,
    tests: Mapping[str, Any] | None = None,
    commands: Sequence[str] = (),
    coverage: Mapping[str, Any] | None = None,
) -> list[str]:
    """Numbered steps derived from what changed and what already covers it."""
    paths = [str(p) for p in context.get("files") or []]
    inventory = [str(p) for p in context.get("repository_tests") or []]
    gaps = list((tests or {}).get("gaps") or [])
    changed_tests = list((tests or {}).get("changed") or [])

    source = [
        p for p in paths
        if not is_noise(p)
        and PurePosixPath(p).suffix.lower() in {
            ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rb", ".php", ".java", ".cs"
        }
        and p not in changed_tests
        # A migration is verified by running it, not by a unit test named after it.
        and not any(matches(pattern, p) for pattern in MIGRATION_PATTERNS)
    ]
    migrations = [
        p for p in paths
        if not is_noise(p) and any(matches(g, p) for g in MIGRATION_PATTERNS)
    ]

    steps: list[str] = []

    # Tests that already exist for the changed files, matched on filename stem.
    related = _related_tests(source, inventory)
    if related:
        steps.append(
            "Run the tests that cover the changed files: "
            + _join(sorted(set(related.values())), limit=6) + "."
        )
    if changed_tests:
        steps.append(
            f"Confirm the {len(changed_tests)} changed test "
            f"{_plural('file', len(changed_tests))} "
            + ("fails" if len(changed_tests) == 1 else "fail")
            + " without the production change and passes with it: "
            + _join(changed_tests, limit=4) + "."
        )

    uncovered = [p for p in source if p not in related and not _has_test(p, inventory)]
    if uncovered:
        steps.append(
            "No test file matches " + _join(sorted(uncovered), limit=5)
            + " — add coverage or state in the PR why it is not needed."
        )

    if migrations:
        steps.append(
            f"Apply the {len(migrations)} {_plural('migration', len(migrations))} against a "
            "copy of production data and confirm the reverse path works: "
            + _join(sorted(migrations), limit=3) + "."
        )

    analysis = (coverage or {}).get("analysis") or {}
    if analysis.get("denominator"):
        percent = analysis.get("changed_line_coverage")
        worst = [f for f in analysis.get("files") or [] if f.get("uncovered_count")]
        step = f"Changed-line coverage is {percent}%."
        if worst:
            step += (
                " Exercise the uncovered lines in "
                + _join([f["path"] for f in worst], limit=3) + "."
            )
        steps.append(step)

    for command in list(commands)[:4]:
        steps.append(f"Run `{command}`.")

    workflows = [p for p in paths if any(matches(g, p) for g in WORKFLOW_PATTERNS)]
    if workflows:
        steps.append(
            "A CI workflow changed, so confirm the pipeline itself succeeds on this "
            "branch before merging — a workflow defect only shows up when it runs."
        )

    # Gap notes the filename analysis produced that are not already covered above.
    if not source and not changed_tests:
        steps.append(
            "No application code changed, so no functional testing is implied; confirm the "
            "affected files render or load as expected."
        )
    elif not steps:
        steps.append("Run the repository's full test suite.")
    for gap in gaps:
        if gap.startswith("The repository test inventory was truncated"):
            steps.append(
                "The repository test list was truncated, so this plan may miss an existing "
                "test; check manually."
            )
    return steps[:10]


def _related_tests(source: Sequence[str], inventory: Sequence[str]) -> dict[str, str]:
    """``{source path: test path}`` where a test filename matches a source stem.

    Generic stems are skipped: every repository has an ``index`` and a ``main``,
    so matching on them would claim coverage that does not exist.
    """
    found: dict[str, str] = {}
    for path in source:
        stem = PurePosixPath(path).stem.lower()
        if not stem or stem in {"index", "main", "__init__", "app", "utils", "types"}:
            continue
        for test in inventory:
            test_stem = PurePosixPath(test).stem.lower()
            normalised = (
                test_stem.removeprefix("test_").removesuffix("_test")
                .removesuffix(".test").removesuffix(".spec").removesuffix("_spec")
            )
            if normalised == stem:
                found[path] = test
                break
    return found


def _has_test(path: str, inventory: Sequence[str]) -> bool:
    stem = PurePosixPath(path).stem.lower()
    if not stem:
        return False
    return any(stem in PurePosixPath(test).stem.lower() for test in inventory)


#: ``(label, patterns, sentence)`` — a real signal that end users are affected.
IMPACT_SIGNALS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("user interface", ("**/*.tsx", "**/*.jsx", "**/*.vue", "**/*.svelte", "**/*.css",
                        "**/*.scss", "**/templates/**", "**/views/**"),
     "The user interface changed, so the visible behaviour of affected screens may differ."),
    ("public API", ("**/api/**", "**/routes/**", "**/controllers/**", "**/*.proto",
                    "**/openapi*.y*ml", "**/schema.graphql"),
     "API surface changed, so API consumers may need to be updated."),
    ("authentication", ("**/auth/**", "**/authentication/**", "**/permissions/**"),
     "Authentication or authorisation logic changed, so who can access what may differ."),
    ("configuration", ("**/.env*", "**/config/**", "**/settings*.py", "**/appsettings*.json"),
     "Configuration changed, so a deploy may need new or updated settings."),
    ("command line", ("**/cli.py", "**/cli/**", "**/bin/**", "**/*.sh", "**/Makefile"),
     "Command-line entry points changed, so scripts and runbooks calling them may need updating."),
    ("CI pipeline", WORKFLOW_PATTERNS,
     "CI pipeline behaviour changed. This affects contributors and the release process, "
     "not end users of the product."),
    ("infrastructure", ("terraform/**", "**/*.tf", "helm/**", "k8s/**"),
     "Infrastructure definitions changed, so deployed resources may change on apply."),
)


def user_impact(
    context: Mapping[str, Any],
    *,
    insights: Mapping[str, Any] | None = None,
) -> str:
    """State impact only where a signal supports it; otherwise say what was checked."""
    paths = [str(p) for p in context.get("files") or [] if not is_noise(str(p))]
    given = insights or {}

    def data(key: str) -> dict[str, Any]:
        entry = given.get(key)
        if isinstance(entry, Mapping):
            inner = entry.get("data")
            return dict(inner) if isinstance(inner, Mapping) else dict(entry)
        return {}

    statements: list[str] = []

    api = data("api")
    if api.get("breaking_change"):
        removed = (api.get("routes") or {}).get("removed") or []
        symbols = (api.get("symbols") or {}).get("removed") or []
        detail = _join([*removed, *symbols], limit=3)
        statements.append(
            "**Breaking:** interface changes were detected"
            + (f" ({detail})" if detail else "")
            + ", so existing callers may fail until updated."
        )

    migrations = data("migrations").get("migrations") or []
    if migrations:
        risky = [m for m in migrations if m.get("risky_operations")]
        statements.append(
            f"{len(migrations)} database {_plural('migration', len(migrations))} "
            + ("runs" if len(migrations) == 1 else "run") + " on deploy"
            + (", including destructive or locking operations — take a backup and plan a "
               "maintenance window" if risky else "")
            + "."
        )

    for label, patterns, sentence in IMPACT_SIGNALS:
        if any(matches(pattern, path) for pattern in patterns for path in paths):
            statements.append(sentence)

    if not statements:
        checked = (
            "changed paths, interface declarations, migrations and dependency manifests"
        )
        return (
            "No user-facing impact was identified from the "
            f"{len(paths)} reviewable {_plural('file', len(paths))}: {checked} show no "
            "API, schema, interface or "
            "configuration change. This is derived from the diff, so confirm anything the "
            "diff cannot show, such as altered runtime behaviour inside an existing function."
        )
    # De-duplicate while preserving order; several signals can share a sentence.
    seen: set[str] = set()
    unique = [s for s in statements if not (s in seen or seen.add(s))]
    return " ".join(unique[:4])


def risk_reason(
    context: Mapping[str, Any],
    risk: str,
    *,
    insights: Mapping[str, Any] | None = None,
) -> str:
    """Say *why* the risk level is what it is, naming the deciding files."""
    paths = [str(p) for p in context.get("files") or [] if not is_noise(str(p))]
    reasons: list[str] = []

    size_data: dict[str, Any] = {}
    entry = (insights or {}).get("size")
    if isinstance(entry, Mapping):
        inner = entry.get("data")
        size_data = dict(inner) if isinstance(inner, Mapping) else dict(entry)

    areas = size_data.get("sensitive_areas") or {}
    if areas:
        named = []
        for area, files in list(areas.items())[:3]:
            first = files[0] if isinstance(files, list) and files else ""
            named.append(f"{area}" + (f" (`{first}`)" if first else ""))
        reasons.append("touches " + _join(named, limit=3, code=False))

    metrics = size_data.get("metrics") or {}
    files_count = metrics.get("files_reviewable", len(paths))
    lines_count = metrics.get("lines_reviewable")
    if lines_count is None:
        lines_count = int(context.get("additions") or 0) + int(context.get("deletions") or 0)
    reasons.append(
        f"{files_count} reviewable {_plural('file', files_count)}, "
        f"{lines_count} changed {_plural('line', lines_count)}"
    )

    concerns = size_data.get("substantive_concerns") or []
    if len(concerns) > 1:
        reasons.append(f"spans {_join(concerns, limit=3, code=False)}")

    return "; ".join(reasons) + "."
