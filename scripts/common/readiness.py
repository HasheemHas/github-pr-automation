"""The one answer a team actually wants: is this pull request ready to merge?

Every other part of this pipeline produces a status for its own slice. This
collapses all of them, plus the two things only GitHub knows — human approvals
and mergeability — into a single verdict with a per-dimension breakdown.

Three verdicts, because two would lie:

  READY                every blocking dimension passed
  READY WITH WARNINGS  nothing blocking failed, but something needs a human's
                       judgement: a partial scan, an unmeasured file, a large diff
  NOT READY            at least one blocking dimension failed or never ran

Which dimensions block is configuration, not opinion: ``readiness.block_on`` in
``config/repositories.yml``. A dimension that did not run is treated as blocking
only when it is named there, so enabling a check is what makes it mandatory —
never an accident of which job happened to be skipped.

Nothing here decides whether a merge happens. Branch protection does that, using
the published check run. This is the explanation a reviewer reads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

READY, WARN, BLOCKED, NOT_RUN = "ready", "warn", "blocked", "not_run"

VERDICT_READY = "READY"
VERDICT_WARNINGS = "READY WITH WARNINGS"
VERDICT_NOT_READY = "NOT READY"

#: How a gate section status maps onto a readiness status.
FROM_SECTION = {
    "PASS": READY,
    "FAIL": BLOCKED,
    "PARTIAL": WARN,
    "WARN": WARN,
    "ERROR": WARN,
    "NOT RUN": NOT_RUN,
}

#: How an insight status maps onto a readiness status.
FROM_INSIGHT = {
    "pass": READY,
    "warn": WARN,
    "fail": BLOCKED,
    "not_run": NOT_RUN,
    "error": WARN,
}

#: Dimension key -> the gate section name it is derived from.
SECTION_KEYS: dict[str, str] = {
    "metadata": "Metadata",
    "review_rules": "Review",
    "tests": "Test coverage",
    "security": "Security",
    "dependencies_install": "Dependencies",
    "lint": "Lint",
    "test": "Test",
    "build": "Build",
    "secrets": "Secret scan",
    "size": "PR size",
    "deps": "Dependency risk",
    "migrations": "Migrations",
    "api": "API compatibility",
    "coverage": "Changed-line coverage",
    "flaky": "Flaky tests",
}

#: Sensible default: the things that mean "do not merge this" in every repository.
DEFAULT_BLOCK_ON = ("lint", "test", "build", "security", "secrets", "conflicts", "review")

ICONS = {READY: "✅", WARN: "⚠️", BLOCKED: "❌", NOT_RUN: "⚪"}


@dataclass
class Dimension:
    """One line of the readiness table."""

    key: str
    name: str
    status: str
    detail: str = ""
    blocking: bool = False

    @property
    def icon(self) -> str:
        return ICONS.get(self.status, "•")

    @property
    def holds_the_merge(self) -> bool:
        return self.blocking and self.status in (BLOCKED, NOT_RUN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "name": self.name, "status": self.status,
            "detail": self.detail, "blocking": self.blocking,
            "holds_the_merge": self.holds_the_merge,
        }


@dataclass
class Readiness:
    """The merge-readiness verdict and its reasoning."""

    verdict: str = VERDICT_NOT_READY
    dimensions: list[Dimension] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.verdict in (VERDICT_READY, VERDICT_WARNINGS)

    @property
    def icon(self) -> str:
        return {"READY": "✅", "READY WITH WARNINGS": "⚠️"}.get(self.verdict, "🛑")

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "ready": self.ready,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "notes": self.notes,
            "dimensions": [d.to_dict() for d in self.dimensions],
        }


def _blocking_set(config: Mapping[str, Any] | None) -> set[str]:
    declared = (config or {}).get("block_on")
    if declared is None:
        return set(DEFAULT_BLOCK_ON)
    if not isinstance(declared, (list, tuple)):
        return set(DEFAULT_BLOCK_ON)
    if not declared:
        # An explicitly empty list means "advise only", which is a valid choice.
        return set()
    return {str(item).strip().lower() for item in declared if str(item).strip()}


def _section_status(sections: Sequence[Mapping[str, Any]], name: str
                    ) -> tuple[str, str] | None:
    for section in sections:
        if str(section.get("name")) == name:
            return (
                FROM_SECTION.get(str(section.get("status")), WARN),
                str(section.get("detail") or ""),
            )
    return None


def review_state(context: Mapping[str, Any], required: int) -> Dimension:
    """Human approval state, from the PR's own review list."""
    approvals = list(context.get("approvals") or [])
    changes_requested = list(context.get("changes_requested") or [])
    reviewed_by = list(context.get("reviewed_by") or [])

    if changes_requested:
        return Dimension(
            "review", "Human review", BLOCKED,
            f"Changes requested by {', '.join('@' + name for name in changes_requested[:5])}.",
        )
    if required <= 0:
        detail = (
            f"{len(approvals)} approval(s); none required by this configuration."
            if approvals else "No approval requirement configured."
        )
        return Dimension("review", "Human review", READY if approvals else WARN, detail)
    if len(approvals) >= required:
        return Dimension(
            "review", "Human review", READY,
            f"{len(approvals)} of {required} required approval(s): "
            + ", ".join("@" + name for name in approvals[:5]),
        )
    return Dimension(
        "review", "Human review", BLOCKED,
        f"{len(approvals)} of {required} required approval(s)."
        + (f" Reviewed by {', '.join('@' + n for n in reviewed_by[:5])} without approving."
           if reviewed_by and not approvals else ""),
    )


def conflict_state(context: Mapping[str, Any], conflicts: Mapping[str, Any] | None
                   ) -> Dimension:
    """Mergeability, preferring the local merge probe over GitHub's cached flag."""
    if conflicts:
        status = str(conflicts.get("status") or "")
        if status == "clean":
            return Dimension("conflicts", "Merge conflicts", READY,
                             "The branch merges cleanly into the base.")
        if status == "conflicted":
            count = conflicts.get("conflict_count") or len(conflicts.get("conflicts") or [])
            return Dimension("conflicts", "Merge conflicts", BLOCKED,
                             f"{count} conflicted file(s) against the base branch.")
        if status == "error":
            return Dimension("conflicts", "Merge conflicts", WARN,
                             str(conflicts.get("reason") or "The merge probe could not run."))

    mergeable = context.get("mergeable")
    state = str(context.get("mergeable_state") or "").lower()
    if mergeable is False or state == "dirty":
        return Dimension("conflicts", "Merge conflicts", BLOCKED,
                         "GitHub reports this branch as conflicting with the base.")
    if mergeable is True and state in ("clean", "unstable", "has_hooks"):
        return Dimension(
            "conflicts", "Merge conflicts", READY,
            f"GitHub reports the branch as mergeable (state: {state or 'clean'})."
        )
    if state == "behind":
        return Dimension("conflicts", "Merge conflicts", WARN,
                         "The branch is behind the base and needs updating first.")
    if state == "blocked":
        return Dimension("conflicts", "Merge conflicts", WARN,
                         "GitHub reports the merge as blocked, usually by a required check "
                         "or a missing approval.")
    return Dimension("conflicts", "Merge conflicts", NOT_RUN,
                     "Mergeability was not determined; GitHub computes it asynchronously.")


def assess(
    *,
    sections: Sequence[Mapping[str, Any]] = (),
    context: Mapping[str, Any] | None = None,
    conflicts: Mapping[str, Any] | None = None,
    insights: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> Readiness:
    """Build the readiness verdict from every available result.

    ``sections`` are the gate sections the report already computed, so the two
    can never disagree. ``insights`` carries the deterministic insight payloads
    keyed by dimension (``secrets``, ``size``, ``coverage`` …) for the dimensions
    that have no gate section of their own.
    """
    settings = dict(config or {})
    blocking = _blocking_set(settings)
    ctx = dict(context or {})
    given = dict(insights or {})
    readiness = Readiness()
    dimensions: list[Dimension] = []

    # -- dimensions derived from the gate sections ---------------------------
    for key, section_name in SECTION_KEYS.items():
        found = _section_status(sections, section_name)
        if found is None:
            # Not a section this run produced; fall back to a supplied insight.
            payload = given.get(key)
            if not isinstance(payload, Mapping):
                continue
            status = FROM_INSIGHT.get(str(payload.get("status")), WARN)
            detail = str(payload.get("summary") or payload.get("reason") or "")
        else:
            status, detail = found
        dimensions.append(Dimension(key, section_name, status, detail,
                                    blocking=key in blocking))

    # -- dimensions only GitHub can answer ----------------------------------
    required = settings.get("require_approvals")
    try:
        required_approvals = int(required) if required is not None else 1
    except (TypeError, ValueError):
        required_approvals = 1
    review = review_state(ctx, required_approvals)
    review.blocking = "review" in blocking
    dimensions.append(review)

    conflict = conflict_state(ctx, conflicts)
    conflict.blocking = "conflicts" in blocking
    dimensions.append(conflict)

    if ctx.get("draft"):
        allow_draft = bool(settings.get("allow_draft", False))
        dimensions.append(Dimension(
            "draft", "Draft status", WARN if allow_draft else BLOCKED,
            "This pull request is a draft." if not allow_draft
            else "This pull request is a draft; drafts are allowed by configuration.",
            blocking=not allow_draft,
        ))

    # -- collapse ------------------------------------------------------------
    ordered = _order(dimensions)
    readiness.dimensions = ordered
    readiness.blockers = [
        f"{d.name}: {d.detail or d.status}" for d in ordered if d.holds_the_merge
    ]
    # A non-blocking dimension that simply did not run is not a warning: nobody
    # asked for it, and listing every unrequested check would drown the ones that
    # matter. A non-blocking dimension that actually FAILED is a warning, because
    # something real was found and the repository chose not to block on it.
    readiness.warnings = [
        f"{d.name}: {d.detail or d.status}"
        for d in ordered
        if d.status == WARN or (d.status == BLOCKED and not d.blocking)
    ]
    not_run_blocking = [d.name for d in ordered if d.blocking and d.status == NOT_RUN]
    if not_run_blocking:
        readiness.notes.append(
            "Required check(s) did not run: " + ", ".join(not_run_blocking)
            + ". A check that never ran is not a pass, so it holds the merge."
        )

    if readiness.blockers:
        readiness.verdict = VERDICT_NOT_READY
    elif readiness.warnings:
        readiness.verdict = VERDICT_WARNINGS
    else:
        readiness.verdict = VERDICT_READY
    readiness.notes.append(
        "Branch protection, not this summary, decides whether the merge button works."
    )
    return readiness


#: Presentation order: the blocking gates first, then advisory insights.
_ORDER = (
    "review", "conflicts", "draft", "lint", "test", "build", "dependencies_install",
    "security", "secrets", "coverage", "flaky", "tests", "review_rules",
    "api", "migrations", "deps", "size", "metadata",
)


def _order(dimensions: Iterable[Dimension]) -> list[Dimension]:
    index = {key: position for position, key in enumerate(_ORDER)}
    return sorted(dimensions, key=lambda d: (index.get(d.key, len(_ORDER)), d.name))


def _cell(value: str) -> str:
    """Make a detail string safe for a markdown table cell."""
    text = str(value or "").replace("|", "\\|")
    return " ".join(text.split())


def render_markdown(readiness: Readiness) -> str:
    """The readiness block for the PR comment."""
    rows = "\n".join(
        f"| {d.icon} {d.name} | **{d.status.replace('_', ' ').upper()}** | "
        f"{'yes' if d.blocking else 'no'} | {_cell(d.detail)} |"
        for d in readiness.dimensions
    )
    blockers = (
        "\n".join(f"- 🛑 {item}" for item in readiness.blockers)
        or "_Nothing is blocking this merge._"
    )
    warnings = "\n".join(f"- ⚠️ {item}" for item in readiness.warnings)
    return (
        f"### {readiness.icon} Merge readiness: {readiness.verdict}\n\n"
        f"| Dimension | Status | Blocking | Detail |\n| --- | --- | --- | --- |\n{rows}\n\n"
        f"**Blocking:**\n{blockers}\n"
        + (f"\n<details><summary>Needs a look ({len(readiness.warnings)})</summary>\n\n"
           f"{warnings}\n\n</details>\n" if warnings else "")
    )


def render_check_text(readiness: Readiness) -> str:
    """Plain text for the check run output, which does not render tables well."""
    lines = [f"Merge readiness: {readiness.verdict}", ""]
    for dimension in readiness.dimensions:
        marker = "BLOCKING" if dimension.holds_the_merge else dimension.status.upper()
        lines.append(f"{dimension.name}: {marker}"
                     + (f" — {dimension.detail}" if dimension.detail else ""))
    if readiness.blockers:
        lines.extend(["", "Blocking:"])
        lines.extend(f"- {item}" for item in readiness.blockers)
    return "\n".join(lines)
