"""Run every diff-based insight the plan asked for, in one pass.

Called from :mod:`scripts.automation.analyze`, so all of this lands in
``analysis.json`` alongside the metadata and rule review. One diff parse is
shared by every insight, which is what keeps this cheap enough to run on every
pull request.

Each entry in the returned mapping follows the same envelope the rest of the
pipeline already uses::

    {"status": "ok" | "error", "data": {...}}

so a single failing insight is reported as an error for its own section and never
takes the analysis job down with it.
"""
from __future__ import annotations

import traceback
from typing import Any, Callable, Mapping

from . import apicompat, deps, migrations, notes, secrets, size
from .changes import is_truncated, parse_changes

#: Plan flag -> (analysis key, human label). The order is the report's order.
INSIGHTS: tuple[tuple[str, str, str], ...] = (
    ("size", "size", "PR size"),
    ("secrets", "secrets", "Secret scan"),
    ("deps", "dependencies", "Dependency risk"),
    ("migrations", "migrations", "Migrations"),
    ("api", "api", "API compatibility"),
    ("notes", "release_notes", "Release notes"),
)


def run_all(
    plan: Mapping[str, Any],
    context: Mapping[str, Any],
    diff: str,
    policies: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run the requested insights and return them keyed for ``analysis.json``."""
    config = dict(policies or {})
    truncated = is_truncated(diff)
    changes = parse_changes(diff)
    results: dict[str, dict[str, Any]] = {}

    runners: dict[str, Callable[[], dict[str, Any]]] = {
        "size": lambda: size.evaluate(context, config),
        "secrets": lambda: secrets.evaluate(changes, config, diff_truncated=truncated),
        "deps": lambda: deps.evaluate(changes, context, config),
        "migrations": lambda: migrations.evaluate(changes, context, config),
        "api": lambda: apicompat.evaluate(changes, context, config),
    }

    for flag, key, label in INSIGHTS:
        if not plan.get(flag):
            continue
        if flag == "notes":
            # Release notes read the other insights, so they run last.
            continue
        try:
            results[key] = {"status": "ok", "data": runners[flag]()}
        except Exception as exc:  # noqa: BLE001 - one insight must not sink the job
            print(f"::warning::{label} insight failed: {exc.__class__.__name__}: {exc}")
            traceback.print_exc()
            results[key] = {
                "status": "error",
                "error": f"{label} could not be computed: {exc.__class__.__name__}: {exc}",
            }

    if plan.get("notes"):
        try:
            results["release_notes"] = {
                "status": "ok",
                "data": notes.generate(
                    context,
                    insights={
                        "api": results.get("api"),
                        "migrations": results.get("migrations"),
                        "dependencies": results.get("dependencies"),
                    },
                    config=config,
                    labels=context.get("labels") or [],
                ),
            }
        except Exception as exc:  # noqa: BLE001
            print(f"::warning::Release note generation failed: {exc}")
            traceback.print_exc()
            results["release_notes"] = {
                "status": "error",
                "error": f"Release notes could not be generated: {exc.__class__.__name__}: {exc}",
            }

    if truncated and results:
        for entry in results.values():
            data = entry.get("data")
            if isinstance(data, dict):
                data.setdefault("diff_truncated", True)

    return results


def requested(plan: Mapping[str, Any]) -> list[str]:
    """The insight labels this plan asked for, for the job summary."""
    return [label for flag, _key, label in INSIGHTS if plan.get(flag)]
