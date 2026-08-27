"""Database migration checks.

A migration is the one part of a pull request that cannot be rolled back by
reverting the merge, so it gets its own section with its own rules.

Checked here:

  * framework detection and naming convention per framework
  * a reverse/down path, or an explicit note that the migration is irreversible
  * destructive and locking operations, which are the ones that take production
    down rather than merely failing
  * an existing migration edited in place, which desynchronises every environment
    that already applied it
  * migration and application code moving together in the same change
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from ..common.globs import matches
from .base import Finding, as_bool, as_patterns, block, result, verdict
from .changes import FileChange

#: framework -> (path globs, filename convention, human description)
FRAMEWORKS: dict[str, tuple[tuple[str, ...], re.Pattern[str] | None, str]] = {
    "django": (
        ("**/migrations/[0-9]*.py", "**/migrations/*.py"),
        re.compile(r"\A\d{4}_[a-z0-9_]+\.py\Z"),
        "NNNN_description.py, e.g. 0042_add_invoice_status.py",
    ),
    "alembic": (
        ("**/alembic/versions/*.py", "**/versions/*.py", "**/migrations/versions/*.py"),
        re.compile(r"\A[0-9a-f]{8,}_[a-z0-9_]+\.py\Z"),
        "<revision>_description.py, e.g. 3f8a1c2b9d40_add_invoice_status.py",
    ),
    "laravel": (
        ("**/database/migrations/*.php",),
        re.compile(r"\A\d{4}_\d{2}_\d{2}_\d{6}_[a-z0-9_]+\.php\Z"),
        "YYYY_MM_DD_HHMMSS_description.php",
    ),
    "rails": (
        ("**/db/migrate/*.rb",),
        re.compile(r"\A\d{14}_[a-z0-9_]+\.rb\Z"),
        "YYYYMMDDHHMMSS_description.rb",
    ),
    "flyway": (
        ("**/db/migration/*.sql", "**/flyway/**/*.sql"),
        re.compile(r"\A[VU]\d+(?:[._]\d+)*__[A-Za-z0-9_]+\.sql\Z"),
        "V<version>__description.sql, e.g. V12__add_invoice_status.sql",
    ),
    "liquibase": (
        ("**/changelog/**/*.xml", "**/db/changelog*.xml", "**/db/changelog/**/*.yaml"),
        None,
        "a changelog file referenced from the master changelog",
    ),
    "prisma": (
        ("**/prisma/migrations/*/migration.sql",),
        None,
        "prisma/migrations/<timestamp>_<name>/migration.sql",
    ),
    "knex": (
        ("**/migrations/*.js", "**/migrations/*.ts", "**/knex/migrations/*.*"),
        re.compile(r"\A\d{8,14}_[A-Za-z0-9_-]+\.(?:js|ts)\Z"),
        "YYYYMMDDHHMMSS_description.js",
    ),
    "golang-migrate": (
        ("**/migrations/*.up.sql", "**/migrations/*.down.sql"),
        re.compile(r"\A\d{6,14}_[a-z0-9_]+\.(?:up|down)\.sql\Z"),
        "NNNNNN_description.up.sql plus a matching .down.sql",
    ),
    "sql": (
        ("**/migrations/*.sql", "**/migrate/*.sql", "**/schema/*.sql", "db/**/*.sql"),
        None,
        "an ordered, immutable SQL file",
    ),
}

#: Destructive or locking SQL, and the ORM operations that generate it.
#: severity -> (label, pattern, why it matters, what to do instead)
RISKY_OPERATIONS: tuple[tuple[str, str, str, str, str], ...] = (
    ("critical", "drop-table", r"(?i)\bDROP\s+TABLE\b",
     "Dropping a table destroys data irreversibly.",
     "Deploy in two steps: stop writing to the table, ship that, then drop it in a later "
     "release once you are certain nothing reads it."),
    ("critical", "drop-column", r"(?i)\bDROP\s+COLUMN\b|\bremove_column\b|\bRemoveField\b|"
     r"\bdropColumn\b|\bdrop_column\b",
     "Dropping a column destroys data and breaks any running instance of the previous "
     "release that still selects it.",
     "Ship the code that stops using the column first, then drop it in a later release."),
    ("critical", "truncate", r"(?i)\bTRUNCATE\b",
     "TRUNCATE empties the table with no undo.",
     "If this is intentional seed-data handling, say so in the description."),
    ("high", "delete-without-where", r"(?i)\bDELETE\s+FROM\s+[\w.\"`\[\]]+\s*(?:;|$)",
     "A DELETE with no WHERE clause removes every row.",
     "Add a WHERE clause, or confirm the table is meant to be emptied."),
    ("high", "update-without-where",
     r"(?i)\bUPDATE\s+[\w.\"`\[\]]+\s+SET\b(?![\s\S]{0,400}?\bWHERE\b)",
     "An UPDATE with no WHERE clause rewrites every row and can lock the table for the "
     "duration.",
     "Add a WHERE clause, or batch the update."),
    ("high", "not-null-without-default",
     r"(?i)\bADD\s+COLUMN\b(?![\s\S]*?\bDEFAULT\b)[\s\S]{0,200}?\bNOT\s+NULL\b",
     "Adding a NOT NULL column with no default fails outright on a non-empty table, and on "
     "older engines rewrites the whole table while holding a lock.",
     "Add the column as nullable, backfill in batches, then add the constraint."),
    ("high", "alter-column-type", r"(?i)\bALTER\s+(?:COLUMN\s+)?[\w.\"`\[\]]+\s+TYPE\b|"
     r"\bMODIFY\s+COLUMN\b|\bALTER\s+COLUMN\b[\s\S]{0,80}\b(?:TYPE|SET\s+DATA\s+TYPE)\b",
     "A type change rewrites the table and holds an exclusive lock; it can also silently "
     "truncate values.",
     "Add a new column, dual-write, backfill, then swap."),
    ("high", "rename", r"(?i)\bRENAME\s+(?:TABLE|COLUMN|TO)\b|\brename_column\b|"
     r"\bRenameField\b|\brenameColumn\b",
     "A rename breaks every running instance of the previous release at the moment it "
     "applies.",
     "Add the new name, dual-write, migrate readers, then drop the old name."),
    ("medium", "create-index-blocking", r"(?i)\bCREATE\s+(?:UNIQUE\s+)?INDEX\b"
     r"(?![\s\S]{0,60}\bCONCURRENTLY\b)",
     "On PostgreSQL a plain CREATE INDEX locks the table against writes for the whole build.",
     "Use CREATE INDEX CONCURRENTLY (and atomic = False in a Django migration)."),
    ("medium", "add-foreign-key", r"(?i)\bADD\s+(?:CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\b",
     "Adding a foreign key validates every existing row while holding a lock.",
     "Add the constraint as NOT VALID, then VALIDATE CONSTRAINT separately."),
    ("medium", "add-unique-constraint", r"(?i)\bADD\s+(?:CONSTRAINT\s+\S+\s+)?UNIQUE\b|"
     r"\bAlterUniqueTogether\b",
     "A unique constraint fails to apply if any duplicate exists, mid-deploy.",
     "Check for duplicates first, and build the supporting index concurrently."),
    ("medium", "raw-sql", r"(?i)\bRunSQL\b|\bexecute\s*\(\s*['\"]|\bDB::statement\b|"
     r"\braw\s*\(\s*['\"]",
     "Raw SQL inside a migration bypasses the framework's own safety checks.",
     "Confirm it is idempotent and that the reverse operation is written out."),
    ("low", "data-migration", r"(?i)\bRunPython\b|\bmodels\.\w+\.objects\.\w+\(|"
     r"\bDB::table\b|\bexecute\s*\(\s*['\"]\s*(?:INSERT|UPDATE)",
     "A data migration runs row by row inside the deploy and can time out on a large table.",
     "Batch it, or move it to a separate backfill job run outside the deploy."),
)

#: Evidence that a reverse path exists.
_REVERSIBLE = re.compile(
    r"""(?ix)
    \b(?:
        def\s+downgrade | def\s+backwards | reverse_code | reverse_sql |
        public\s+function\s+down | def\s+down | function\s+down | \bdown\s*: |
        exports\.down | \.down\s*=\s*
    )\b
    """
)
#: An explicit statement that there is no way back.
_IRREVERSIBLE_NOTE = re.compile(
    r"(?i)irreversible|not\s+reversible|cannot\s+be\s+(?:reversed|rolled\s+back)|"
    r"RunPython\.noop|no-?op\s+reverse|elidable"
)

#: Application code whose change usually implies a schema change and vice versa.
MODEL_PATTERNS = (
    "**/models.py", "**/models/*.py", "**/model.py", "**/entity/**", "**/entities/**",
    "**/schema.py", "**/schemas/*.py", "**/*Model.php", "**/Models/**", "**/models/**",
    "**/schema.prisma", "**/*.entity.ts", "**/entities/*.ts", "**/domain/**",
)

DEFAULTS = {
    "enabled": True,
    "fail_on": "",                    # advisory by default
    "require_rollback": True,
    "require_naming": True,
    "max_per_pr": 0,                  # 0 disables the count check
    "allow_editing_applied": False,
}


def detect_framework(path: str) -> str | None:
    """The migration framework a path belongs to, most specific first."""
    # Order matters: golang-migrate and alembic both live under */migrations.
    for name in ("golang-migrate", "alembic", "django", "laravel", "rails", "flyway",
                 "liquibase", "prisma", "knex", "sql"):
        globs, _, _ = FRAMEWORKS[name]
        if any(matches(pattern, path) for pattern in globs):
            if name == "django" and not _looks_like_django(path):
                continue
            return name
    return None


def _looks_like_django(path: str) -> bool:
    """Django migrations are numbered and sit directly under a migrations package."""
    name = PurePosixPath(path).name
    return name == "__init__.py" or bool(re.match(r"\A\d{4}_", name))


def migration_paths(paths: Sequence[str]) -> dict[str, str]:
    """``{path: framework}`` for every migration file in the change."""
    found: dict[str, str] = {}
    for path in paths:
        if PurePosixPath(path).name == "__init__.py":
            continue
        framework = detect_framework(path)
        if framework:
            found[path] = framework
    return found


def risky_operations(text: str) -> list[dict[str, str]]:
    """Every risky operation found in a block of migration text."""
    hits: list[dict[str, str]] = []
    for severity, rule, pattern, why, instead in RISKY_OPERATIONS:
        if re.search(pattern, text):
            hits.append({"severity": severity, "rule": rule, "why": why, "instead": instead})
    return hits


def evaluate(
    changes: Mapping[str, FileChange],
    context: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Check every migration file the pull request touches."""
    policy = {**DEFAULTS, **block(config, "migration_policy")}
    if not as_bool(policy.get("enabled"), True):
        return result("not_run", "Migration checks are disabled for this repository.")

    paths = [str(p) for p in (context or {}).get("files") or []] or sorted(changes)
    ignore = as_patterns(policy.get("ignore_paths"))
    candidates = [p for p in paths if not any(matches(g, p) for g in ignore)]
    found = migration_paths(candidates)

    if not found:
        # A model change with no migration is worth reporting even when no
        # migration file exists, so this is not an early return to "pass".
        findings = _model_without_migration(candidates, [])
        return result(
            verdict(findings, fail_at="", warn_at="low"),
            "No migration files changed." if not findings
            else "Model or entity code changed with no migration in this pull request.",
            findings, migrations=[], frameworks=[],
        )

    findings: list[Finding] = []
    records: list[dict[str, Any]] = []

    for path, framework in sorted(found.items()):
        change = changes.get(path)
        globs, convention, convention_text = FRAMEWORKS[framework]
        name = PurePosixPath(path).name
        added_text = change.added_text if change else ""
        full_text = added_text + "\n" + (change.removed_text if change else "")
        is_new = bool(change and change.is_new)
        edited_in_place = bool(change and change.is_modified and change.removed)

        record: dict[str, Any] = {
            "path": path,
            "framework": framework,
            "status": (
                "added" if is_new
                else "deleted" if change and change.is_deleted
                else "modified"
            ),
            "added_lines": len(change.added) if change else 0,
            "removed_lines": len(change.removed) if change else 0,
        }

        # -- naming convention -------------------------------------------------
        if as_bool(policy.get("require_naming"), True) and convention and is_new:
            if not convention.match(name):
                findings.append(Finding(
                    "low", "migration-naming",
                    f"`{name}` does not follow the {framework} naming convention",
                    f"Expected {convention_text}. Ordering and de-duplication both depend on "
                    "the filename, so a non-conforming name can apply out of order.",
                    path=path,
                ))
        record["naming_ok"] = bool(not convention or convention.match(name))

        # -- an already-applied migration edited in place ----------------------
        if edited_in_place and not as_bool(policy.get("allow_editing_applied"), False):
            findings.append(Finding(
                "high", "migration-edited-in-place",
                f"Existing migration `{name}` was modified",
                "Every environment that already applied this migration will never see the "
                "change, so schemas diverge silently and the checksum-based frameworks "
                "(Flyway, Liquibase) will refuse to start. Add a new migration instead.",
                path=path,
            ))
        record["edited_in_place"] = edited_in_place

        if change and change.is_deleted:
            findings.append(Finding(
                "high", "migration-deleted",
                f"Migration `{name}` was deleted",
                "Deleting a migration that has been applied leaves environments with schema "
                "state no migration accounts for, and breaks a fresh set-up from zero.",
                path=path,
            ))

        # -- reversibility -----------------------------------------------------
        needs_reverse = framework not in ("flyway", "prisma", "sql", "liquibase")
        reversible = bool(_REVERSIBLE.search(full_text))
        declared_irreversible = bool(_IRREVERSIBLE_NOTE.search(full_text))
        if framework == "golang-migrate":
            # The down file is a sibling, so look for it in the same change set.
            stem = re.sub(r"\.(?:up|down)\.sql\Z", "", path)
            reversible = any(
                candidate.startswith(stem) and candidate.endswith(".down.sql")
                for candidate in candidates
            )
        if (as_bool(policy.get("require_rollback"), True) and is_new and needs_reverse
                and not reversible and not declared_irreversible):
            findings.append(Finding(
                "medium", "migration-no-rollback",
                f"`{name}` has no reverse path",
                "Add the reverse operation, or state in the migration itself that it is "
                "irreversible and why. A deploy that cannot be rolled back needs to be a "
                "deliberate decision, not a discovery made during an incident.",
                path=path,
            ))
        record["reversible"] = reversible or declared_irreversible
        record["declared_irreversible"] = declared_irreversible

        # -- risky operations --------------------------------------------------
        operations = risky_operations(added_text)
        record["risky_operations"] = [op["rule"] for op in operations]
        for op in operations:
            findings.append(Finding(
                op["severity"], f"migration-{op['rule']}",
                f"{op['rule'].replace('-', ' ').capitalize()} in `{name}`",
                f"{op['why']} {op['instead']}",
                path=path,
            ))

        records.append(record)

    # -- cross-file checks -----------------------------------------------------
    new_migrations = [r for r in records if r["status"] == "added"]
    max_per_pr = int(policy.get("max_per_pr") or 0)
    if max_per_pr and len(new_migrations) > max_per_pr:
        findings.append(Finding(
            "medium", "migration-too-many",
            f"{len(new_migrations)} new migrations in one pull request",
            f"The configured limit is {max_per_pr}. Multiple schema steps in one deploy make "
            "a partial failure much harder to reason about; split them across releases.",
        ))

    frameworks = sorted({r["framework"] for r in records})
    if len(frameworks) > 1:
        findings.append(Finding(
            "low", "migration-mixed-frameworks",
            f"Migrations for {len(frameworks)} different frameworks: {', '.join(frameworks)}",
            "Confirm this is a genuinely multi-service change and not a misplaced file.",
        ))

    findings.extend(_model_without_migration(candidates, list(found)))
    findings.extend(_migration_without_code(candidates, list(found)))

    fail_on = str(policy.get("fail_on") or "").strip().lower()
    status = verdict(findings, fail_at=fail_on, warn_at="low")

    return result(
        status,
        f"{len(records)} migration file(s) across {', '.join(frameworks)}; "
        f"{sum(len(r['risky_operations']) for r in records)} risky operation(s)",
        findings,
        migrations=records,
        frameworks=frameworks,
        new_count=len(new_migrations),
        policy={
            "require_rollback": as_bool(policy.get("require_rollback"), True),
            "require_naming": as_bool(policy.get("require_naming"), True),
            "max_per_pr": max_per_pr,
            "fail_on": fail_on or "nothing (advisory)",
        },
    )


def _model_without_migration(paths: Sequence[str], migrations: Sequence[str]) -> list[Finding]:
    """Model or entity code changed, but no migration accompanies it."""
    if migrations:
        return []
    models = [p for p in paths if any(matches(g, p) for g in MODEL_PATTERNS)]
    if not models:
        return []
    return [Finding(
        "medium", "migration-missing-for-model-change",
        f"{len(models)} model/entity file(s) changed with no migration",
        "Changed: " + ", ".join(f"`{p}`" for p in sorted(models)[:5])
        + ". If the schema changed, generate the migration; if it did not, no action is "
        "needed — this check cannot tell the difference from the diff alone.",
    )]


def _migration_without_code(paths: Sequence[str], migrations: Sequence[str]) -> list[Finding]:
    """A migration with no accompanying application change at all."""
    if not migrations:
        return []
    others = [p for p in paths if p not in set(migrations)]
    substantive = [
        p for p in others
        if not any(matches(g, p) for g in ("**/*.md", "**/*.txt", "**/*.lock", "**/*.rst"))
    ]
    if substantive:
        return []
    return [Finding(
        "low", "migration-without-code",
        "Migration changed with no application code change",
        "The schema moves without the code that uses it. That is a valid two-phase deploy, "
        "and it is also what a forgotten model change looks like — confirm which this is.",
    )]
