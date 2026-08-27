"""Write results back to the target pull request.

Run as ``python -m scripts.github.report``.

This is the only module that holds a write-capable token, and it executes no
target repository code. It consumes only the explicitly named result artifacts.

Fail-safe rules enforced here:
  * a section whose deterministic analysis failed is rendered as ERROR/NOT RUN and its
    write is skipped entirely (metadata is never overwritten with nothing)
  * a missing optional artifact is NOT RUN, which is distinct from FAIL
  * the PR head SHA is re-checked immediately before any write; if the PR moved,
    nothing is written
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..common import readiness as readiness_module
from ..common import summary
from ..common.config import artifact_dir, reviewer_groups
from ..common.errors import AutomationError, GitHubError
from ..common.labels import derive_labels, missing_labels
from ..common.reviewers import DEFAULT_CODEOWNERS_MODE, resolve_reviewers
from ..common.titles import title_decision
from .api import GitHubClient
from .body import SUMMARY_END, SUMMARY_START
from .body import upsert as upsert_body
from .body import valid_headings, valid_sections
from .diffmap import parse_diff, split_anchored

REPORT_MARKER = "<!-- pr-automation-report -->"
REVIEW_MARKER = "<!-- ai-pr-review -->"
CHECK_NAME = "PR Automation / Quality Gate"

FINDING_KEY_PATTERN = re.compile(r"<!-- ai-finding:([0-9a-f]{16}) -->")

#: Inline comments per review. Beyond this, findings stay in the summary comment.
MAX_INLINE_COMMENTS = 25
#: Severities worth interrupting a reviewer inline. Lower ones stay in the summary.
INLINE_SEVERITIES = ("critical", "high", "medium")

SEVERITY_ICON = {
    "critical": "🛑", "high": "⚠️", "medium": "⚠️", "low": "ℹ️", "info": "ℹ️",
}

PASS, FAIL, NOT_RUN, ERROR, PARTIAL = "PASS", "FAIL", "NOT RUN", "ERROR", "PARTIAL"
#: An advisory row. Deliberately does NOT degrade the overall verdict: a 700-line
#: pull request or a widened dependency range is information for a reviewer, not a
#: reason to turn the branch-protection check amber on every second merge.
WARN = "WARN"

#: Insight status -> gate section status.
INSIGHT_STATUS = {
    "pass": PASS, "warn": WARN, "fail": FAIL, "not_run": NOT_RUN, "error": ERROR,
}

#: Analysis key -> (section name, plan flag). Order is the report's order.
INSIGHT_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("secrets", "Secret scan", "secrets"),
    ("size", "PR size", "size"),
    ("dependencies", "Dependency risk", "deps"),
    ("migrations", "Migrations", "migrations"),
    ("api", "API compatibility", "api"),
)


def finding_key(finding: Mapping[str, Any]) -> str:
    """Stable fingerprint so a re-run does not duplicate an inline comment."""
    raw = "|".join(
        str(finding.get(field) or "")
        for field in ("path", "line", "severity", "title")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def render_inline_comment(finding: Mapping[str, Any]) -> str:
    icon = SEVERITY_ICON.get(str(finding.get("severity")), "ℹ️")
    severity = str(finding.get("severity", "")).upper()
    category = finding.get("category") or "other"
    body = (finding.get("body") or "").strip()
    return (
        f"<!-- ai-finding:{finding_key(finding)} -->\n"
        f"{icon} **{severity}** · `{category}` — {finding.get('title')}\n"
        + (f"\n{body}\n" if body else "")
        + "\n_Automated rule finding. Advisory only; resolve or dismiss as you see fit._"
    )


@dataclass
class Section:
    name: str
    status: str
    detail: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == FAIL


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a result file. A missing or unreadable file is 'not produced'."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::warning::Ignoring unreadable artifact {path.name}: {exc}", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def _group_status(quality: Mapping[str, Any] | None, group: str) -> Section:
    """Derive one quality group's status, distinguishing 'not run' from 'failed'."""
    label = group.title()
    if quality is None:
        return Section(label, NOT_RUN, "Validation did not produce a result.")
    status = quality.get("status")
    if status in ("not_run", "error"):
        return Section(label, NOT_RUN if status == "not_run" else ERROR,
                       str(quality.get("reason") or ""))
    results = [r for r in (quality.get("results") or []) if r.get("group") == group]
    if not results:
        return Section(label, NOT_RUN, "No commands configured for this group.")
    failed = [r for r in results if r.get("status") == "fail"]
    skipped = [r for r in results if r.get("status") == "skipped"]
    if failed:
        return Section(label, FAIL, "; ".join(str(r.get("name")) for r in failed))
    if skipped:
        return Section(label, NOT_RUN, "; ".join(str(r.get("reason") or "skipped") for r in skipped))
    return Section(label, PASS, f"{len(results)} command(s) passed")


def _insight_section(name: str, entry: Mapping[str, Any] | None, requested: bool
                     ) -> tuple[Section, dict[str, Any]]:
    """Turn one insight payload into a gate section plus its data."""
    if not requested:
        return Section(name, NOT_RUN, "Not requested for this action."), {}
    if not isinstance(entry, Mapping):
        return Section(name, NOT_RUN, "Deterministic analysis produced no result."), {}
    if entry.get("status") != "ok":
        detail = str(entry.get("error") or entry.get("reason") or "")
        return Section(name, ERROR, detail), {}
    data = entry.get("data") or {}
    status = INSIGHT_STATUS.get(str(data.get("status")), ERROR)
    detail = str(data.get("summary") or "")
    counts = data.get("counts") or {}
    blocking = int(counts.get("critical") or 0) + int(counts.get("high") or 0)
    if blocking:
        detail += f"; {blocking} high/critical finding(s)"
    return Section(name, status, detail), dict(data)


def _result_section(name: str, payload: Mapping[str, Any] | None, requested: bool
                    ) -> Section:
    """A section from a validation-job result file (coverage, flaky)."""
    if not requested:
        return Section(name, NOT_RUN, "Not requested for this action.")
    if not isinstance(payload, Mapping):
        return Section(name, NOT_RUN, "The validation job produced no result.")
    status = INSIGHT_STATUS.get(str(payload.get("status")), ERROR)
    return Section(name, status, str(payload.get("reason") or ""))


def build_sections(
    plan: Mapping[str, Any],
    analysis: Mapping[str, Any] | None,
    quality: Mapping[str, Any] | None,
    security: Mapping[str, Any] | None,
    coverage: Mapping[str, Any] | None = None,
    flaky: Mapping[str, Any] | None = None,
) -> tuple[list[Section], dict[str, Any]]:
    """Build the gate sections and the counts the report needs."""
    sections: list[Section] = []
    analysis = analysis or {}

    def analysis_section(name: str, key: str, requested: bool) -> Mapping[str, Any] | None:
        if not requested:
            sections.append(Section(name, NOT_RUN, "Not requested for this action."))
            return None
        entry = analysis.get(key)
        if not isinstance(entry, Mapping):
            sections.append(Section(name, NOT_RUN, "Deterministic analysis produced no result."))
            return None
        if entry.get("status") == "ok":
            return entry.get("data") or {}
        detail = str(entry.get("error") or entry.get("reason") or "")
        sections.append(Section(name, ERROR if entry.get("status") == "error" else NOT_RUN, detail))
        return None

    metadata = analysis_section("Metadata", "metadata", bool(plan.get("metadata")))
    if metadata is not None:
        sections.append(Section("Metadata", PASS, "Summary generated"))

    review_requested = bool(plan.get("review") or plan.get("security"))
    review = analysis_section("Review", "review", review_requested)
    findings = list((review or {}).get("findings") or [])
    counts = {
        "critical": sum(1 for f in findings if f.get("severity") == "critical"),
        "high": sum(1 for f in findings if f.get("severity") == "high"),
        "medium": sum(1 for f in findings if f.get("severity") == "medium"),
        "low": sum(1 for f in findings if f.get("severity") in ("low", "info")),
    }
    if review is not None:
        blocking = counts["critical"] + counts["high"]
        detail = f"{len(findings)} finding(s); critical/high: {blocking}"
        batches = review.get("batches")
        if batches:
            detail += f"; {review.get('succeeded_batches', batches)}/{batches} batch(es)"
        if blocking:
            status = FAIL
        elif review.get("complete") is False:
            # Some files were never sent to the model: not a clean bill of health.
            status = PARTIAL
            unreviewed = review.get("unreviewed_paths") or []
            detail += f"; {len(unreviewed)} file(s) not reviewed"
        else:
            status = PASS
        sections.append(Section("Review", status, detail))

    tests = analysis_section("Test coverage", "tests", bool(plan.get("tests")))
    gaps = list((tests or {}).get("gaps") or [])
    if tests is not None:
        sections.append(Section("Test coverage", PASS, f"{len(gaps)} potential gap(s)"))

    # Deterministic insights. Each is its own row so a reviewer can see at a glance
    # which dimension is unhappy.
    insight_data: dict[str, Any] = {}
    for key, name, flag in INSIGHT_SECTIONS:
        section, data = _insight_section(name, analysis.get(key), bool(plan.get(flag)))
        sections.append(section)
        insight_data[flag] = data

    notes_entry = analysis.get("release_notes")
    release_notes = (
        dict(notes_entry.get("data") or {})
        if isinstance(notes_entry, Mapping) and notes_entry.get("status") == "ok"
        else {}
    )

    # Insights that needed the checked-out tree.
    sections.append(_result_section(
        "Changed-line coverage", coverage, bool(plan.get("coverage"))
    ))
    sections.append(_result_section("Flaky tests", flaky, bool(plan.get("flaky"))))

    # Scanners.
    if not plan.get("security"):
        sections.append(Section("Security", NOT_RUN, "Not requested for this action."))
    elif security is None:
        sections.append(Section("Security", NOT_RUN, "Scanners produced no result."))
    else:
        status_map = {"pass": PASS, "fail": FAIL, "not_run": NOT_RUN,
                      "partial": PARTIAL, "error": ERROR}
        scanners = [r for r in (security.get("results") or []) if isinstance(r, Mapping)]
        # Always name the scanners responsible; a bare "FAIL" with no detail is
        # unactionable, and was exactly what a mis-invoked scanner produced.
        detail = str(security.get("reason") or "")
        if not detail:
            notable = [r for r in scanners if r.get("status") in ("fail", "error", "not_run")]
            if notable:
                detail = "; ".join(
                    f"{r.get('name')}: {r.get('reason') or r.get('status')}" for r in notable
                )
            elif scanners:
                detail = f"{len(scanners)} scanner(s) clean"
        sections.append(
            Section("Security", status_map.get(str(security.get("status")), ERROR), detail)
        )

    # Quality groups.
    if not plan.get("quality"):
        for group in ("lint", "test", "build"):
            sections.append(Section(group.title(), NOT_RUN, "Not requested for this action."))
    else:
        install = _group_status(quality, "install")
        if install.status != PASS:
            sections.append(Section("Dependencies", install.status, install.detail))
        else:
            sections.append(Section("Dependencies", PASS, install.detail))
        for group in ("lint", "test", "build"):
            sections.append(_group_status(quality, group))

    return sections, {
        "findings": findings, "counts": counts, "gaps": gaps,
        "metadata": metadata, "review": review, "tests": tests,
        "insights": insight_data,
        "release_notes": release_notes,
        "coverage": dict(coverage) if isinstance(coverage, Mapping) else {},
        "flaky": dict(flaky) if isinstance(flaky, Mapping) else {},
    }


def overall(sections: Iterable[Section]) -> str:
    """Collapse the sections into one verdict.

    PARTIAL must propagate: a scanner that was enabled but could not run, or a
    review that could not cover every file, is not a pass.

    WARN does not propagate. An advisory insight — a large diff, a widened
    dependency range — must not turn the branch-protection check amber, or the
    check stops meaning anything. Those surface in the merge-readiness summary
    instead, where a human reads them.
    """
    items = list(sections)
    if any(section.status == FAIL for section in items):
        return FAIL
    if any(section.status in (ERROR, PARTIAL) for section in items):
        return PARTIAL
    if any(section.status in (PASS, WARN) for section in items):
        return PASS
    return NOT_RUN


def render_check_output(
    sections: list[Section],
    counts: Mapping[str, int],
    verdict: str,
    readiness: readiness_module.Readiness | None = None,
) -> str:
    lines = [f"{CHECK_NAME}", ""]
    for section in sections:
        lines.append(f"{section.name}")
        lines.append(f"{section.status}" + (f" — {section.detail}" if section.detail else ""))
        lines.append("")
    lines.append(f"Critical findings\n{counts.get('critical', 0)}")
    lines.append("")
    lines.append(f"High findings\n{counts.get('high', 0)}")
    lines.append("")
    lines.append(f"Overall\n{verdict}")
    if readiness is not None:
        lines.extend(["", readiness_module.render_check_text(readiness)])
    return "\n".join(lines)


def render_reviewers(full_name: str, reviewers: Mapping[str, Any]) -> str:
    """The reviewer line, including CODEOWNERS-derived suggestions."""
    owner = full_name.split("/")[0]
    requested = [f"@{u}" for u in reviewers.get("users") or []]
    requested += [f"@{owner}/{t}" for t in reviewers.get("teams") or []]
    suggested = [f"@{u}" for u in reviewers.get("suggested_users") or []]
    suggested += [f"@{owner}/{t}" for t in reviewers.get("suggested_teams") or []]

    parts: list[str] = []
    if requested:
        parts.append("requested " + ", ".join(requested))
    if reviewers.get("deferred_to_codeowners"):
        parts.append("CODEOWNERS covers every changed file, so GitHub assigns the owners")
    if suggested and not requested:
        parts.append("CODEOWNERS owners: " + ", ".join(suggested))
    elif suggested:
        parts.append("also owned by " + ", ".join(suggested))
    if not parts:
        notes = reviewers.get("notes") or []
        return "; ".join(str(note) for note in notes[:2]) or "_None requested._"
    return "; ".join(parts)


def render_ownership(reviewers: Mapping[str, Any]) -> str:
    """A per-owner file breakdown, collapsed."""
    ownership = reviewers.get("ownership") or {}
    per_owner = ownership.get("per_owner") or []
    unowned = ownership.get("unowned") or []
    if not per_owner and not unowned:
        return ""
    body = "\n".join(f"- {line}" for line in per_owner) or "_No owner matched._"
    if unowned:
        body += (
            f"\n\n**Not covered by CODEOWNERS ({len(unowned)}):** "
            + ", ".join(f"`{path}`" for path in unowned[:15])
            + ("…" if len(unowned) > 15 else "")
        )
    rules = ownership.get("matched_rules") or []
    if rules:
        body += "\n\n**Matched rules:** " + ", ".join(f"`{rule}`" for rule in rules[:15])
    return f"<details><summary>File ownership ({len(per_owner)} owner(s))</summary>\n\n{body}\n\n</details>"


def render_insight_details(data: Mapping[str, Any]) -> str:
    """Collapsible blocks for each insight that found something."""
    blocks: list[str] = []
    insights = data.get("insights") or {}

    for flag, title in (
        ("secrets", "Detected credentials"),
        ("api", "Interface changes"),
        ("migrations", "Migration checks"),
        ("deps", "Dependency changes"),
        ("size", "Size and risk"),
    ):
        payload = insights.get(flag) or {}
        findings = payload.get("findings") or []
        if not findings:
            continue
        rows = "\n".join(
            f"- **{f.get('severity')}** "
            + (f"`{f.get('path')}`" + (f":{f['line']}" if f.get("line") else "") + " — "
               if f.get("path") else "")
            + f"{f.get('title')}"
            + (f"\n  {f.get('detail')}" if f.get("detail") else "")
            for f in findings[:20]
        )
        extra = ""
        if flag == "secrets":
            extra = (
                "\n\n_Values are masked and never stored by this automation. Rotate every "
                "credential listed above: removing the line does not remove it from git "
                "history._"
            )
        if flag == "deps":
            packages = payload.get("packages") or []
            if packages:
                extra = "\n\n" + summary.table(
                    ["Package", "Change", "From", "To", "Ecosystem"],
                    [
                        [f"`{p['name']}`", p["kind"], p["from"] or "—", p["to"] or "—",
                         p.get("ecosystem", "")]
                        for p in packages[:30]
                    ],
                )
        blocks.append(
            f"<details><summary>{title} ({len(findings)})</summary>\n\n{rows}{extra}\n\n</details>"
        )

    coverage = data.get("coverage") or {}
    analysis = coverage.get("analysis") or {}
    files = [f for f in analysis.get("files") or [] if f.get("uncovered_count")]
    if files:
        rows = summary.table(
            ["File", "Covered", "Measured", "%", "Uncovered lines"],
            [
                [f"`{f['path']}`", f["covered_lines"], f["measured_lines"],
                 "—" if f["percent"] is None else f"{f['percent']}%",
                 ", ".join(str(n) for n in f["uncovered_lines"][:12])
                 + ("…" if f["uncovered_count"] > 12 else "")]
                for f in files[:25]
            ],
        )
        blocks.append(
            f"<details><summary>Changed lines with no test coverage "
            f"({analysis.get('uncovered_lines', 0)})</summary>\n\n{rows}\n\n</details>"
        )
    unmeasured = analysis.get("unmeasured_files") or []
    if unmeasured:
        rows = "\n".join(
            f"- `{item['path']}` — {item['added_lines']} added line(s)"
            for item in unmeasured[:20]
        )
        blocks.append(
            f"<details><summary>Changed files absent from every coverage report "
            f"({len(unmeasured)})</summary>\n\n{rows}\n\n</details>"
        )

    flaky = (data.get("flaky") or {}).get("analysis") or {}
    for key, title in (
        ("flaky", "Flaky tests"),
        ("consistent_failures", "Consistent test failures (not flaky)"),
    ):
        entries = flaky.get(key) or []
        if not entries:
            continue
        rows = "\n".join(
            f"- `{entry['identifier']}` — {entry.get('reason', '')}"
            for entry in entries[:25]
        )
        blocks.append(
            f"<details><summary>{title} ({len(entries)})</summary>\n\n{rows}\n\n</details>"
        )

    notes = data.get("release_notes") or {}
    markdown = (notes.get("markdown") or "").strip()
    if markdown:
        blocks.append(
            f"<details><summary>Draft release notes</summary>\n\n{markdown}\n\n</details>"
        )

    return "\n\n".join(blocks)


def render_comment(
    full_name: str,
    number: int,
    sections: list[Section],
    data: Mapping[str, Any],
    labels: list[str],
    reviewers: Mapping[str, Any],
    verdict: str,
    *,
    metadata_outcome: Mapping[str, Any] | None = None,
    inline_outcome: Mapping[str, Any] | None = None,
    unanchored: list[Mapping[str, Any]] | None = None,
    readiness: readiness_module.Readiness | None = None,
) -> str:
    counts = data["counts"]
    rows = "\n".join(
        f"| {s.name} | **{s.status}** | {s.detail or ''} |" for s in sections
    )
    findings = data["findings"][:20]
    findings_block = (
        "\n".join(
            f"- **{f['severity']}** `{f.get('path') or 'n/a'}`"
            + (f":{f['line']}" if f.get("line") else "")
            + f" — {f['title']}"
            for f in findings
        )
        or "_No findings reported._"
    )
    gaps_block = "\n".join(f"- {g}" for g in data["gaps"][:20]) or "_No obvious gaps._"

    title_line = ""
    outcome = metadata_outcome or {}
    if outcome.get("title_updated"):
        return_title = outcome.get("new_title")
        title_line = (
            f"\n**Title:** replaced with `{return_title}` "
            f"({outcome.get('title_reason')})\n"
        )
    elif outcome.get("title_reason"):
        title_line = f"\n**Title:** unchanged — {outcome.get('title_reason')}\n"

    inline = inline_outcome or {}
    inline_bits = []
    if inline.get("posted"):
        inline_bits.append(f"{inline['posted']} posted inline")
    if inline.get("skipped_existing"):
        inline_bits.append(f"{inline['skipped_existing']} already commented")
    if inline.get("rejected"):
        inline_bits.append(f"{inline['rejected']} rejected by GitHub")
    if inline.get("overflow"):
        inline_bits.append(f"{len(inline['overflow'])} over the inline cap")
    if unanchored:
        inline_bits.append(f"{len(unanchored)} not anchorable to a diff line")
    inline_line = (
        f"\n**Inline comments:** {', '.join(inline_bits)}\n" if inline_bits else ""
    )

    review = data.get("review") or {}
    coverage_line = ""
    if review.get("batches"):
        unreviewed = review.get("unreviewed_paths") or []
        coverage_line = (
            f"\n**Review coverage:** {len(review.get('reviewed_paths') or [])} file(s) across "
            f"{review.get('succeeded_batches', review['batches'])}/{review['batches']} batch(es)"
            + (
                f". ⚠️ Not reviewed: {', '.join(f'`{p}`' for p in unreviewed[:10])}"
                + (f" and {len(unreviewed) - 10} more" if len(unreviewed) > 10 else "")
                if unreviewed
                else "."
            )
            + "\n"
        )

    reviewer_line = render_reviewers(full_name, reviewers)
    ownership_block = render_ownership(reviewers)
    insight_blocks = render_insight_details(data)

    # The readiness verdict goes first: it is the one line most readers need.
    readiness_block = (
        readiness_module.render_markdown(readiness) + "\n\n" if readiness is not None else ""
    )

    coverage_summary = ""
    coverage_payload = (data.get("coverage") or {}).get("analysis") or {}
    if coverage_payload.get("denominator"):
        percent = coverage_payload.get("changed_line_coverage")
        coverage_summary = (
            f"\n**Changed-line coverage:** {percent}% "
            f"({coverage_payload.get('covered_lines', 0)}/"
            f"{coverage_payload.get('denominator', 0)} line(s))\n"
        )

    return (
        f"{REPORT_MARKER}\n"
        f"## 🤖 PR Automation Report\n\n"
        f"**Target:** `{full_name}` PR #{number}\n\n"
        f"{readiness_block}"
        f"<details><summary>All checks ({len(sections)})</summary>\n\n"
        f"| Check | Status | Detail |\n| --- | --- | --- |\n{rows}\n\n</details>\n\n"
        f"**Quality gate:** **{verdict}**\n"
        f"{coverage_summary}{coverage_line}{title_line}{inline_line}\n"
        f"**Labels:** {', '.join(f'`{x}`' for x in labels) or '_none_'}\n\n"
        f"**Reviewers:** {reviewer_line}\n\n"
        + (f"{ownership_block}\n\n" if ownership_block else "")
        + f"<details><summary>Rule findings "
        f"(critical {counts['critical']}, high {counts['high']}, medium {counts['medium']}, "
        f"low {counts['low']})</summary>\n\n{findings_block}\n\n</details>\n\n"
        f"<details><summary>Potential test gaps ({len(data['gaps'])})</summary>\n\n"
        f"{gaps_block}\n\n</details>\n\n"
        + (f"{insight_blocks}\n\n" if insight_blocks else "")
        + "Human approval is still required before merge."
    )


def upsert_summary(
    body: str,
    summary: str,
    metadata: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    sections: list[str] | None = None,
    headings: Mapping[str, str] | None = None,
) -> str:
    """Replace the managed description block, preserving author-written text.

    Rendering lives in :mod:`scripts.github.body`; ``summary`` is used only as the
    fallback description when analysis returned no long-form description.
    """
    payload = dict(metadata)
    if not (payload.get("description") or "").strip():
        payload["description"] = summary
    return upsert_body(body, payload, context, sections=sections, headings=headings)


class Reporter:
    def __init__(self, client: GitHubClient, owner: str, repo: str, number: int) -> None:
        self.client = client
        self.owner = owner
        self.repo = repo
        self.number = number
        self.notes: list[str] = []

    @property
    def _repo_path(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"

    def verify_head(self, expected_sha: str) -> None:
        """Refuse to write if the PR moved since analysis started."""
        pr = self.client.get(f"{self._repo_path}/pulls/{self.number}")
        current = ((pr or {}).get("head") or {}).get("sha") or ""
        if pr.get("state") != "open":
            raise AutomationError(f"PR #{self.number} is no longer open; nothing was written.")
        if expected_sha and current != expected_sha:
            raise AutomationError(
                f"PR #{self.number} head moved from {expected_sha[:12]} to {current[:12]} "
                "during analysis; nothing was written. Re-run the workflow."
            )

    def apply_labels(self, labels: list[str]) -> list[str]:
        if not labels:
            return []
        existing = [
            item.get("name", "")
            for item in self.client.paginate(f"{self._repo_path}/labels")
            if isinstance(item, dict)
        ]
        for label in missing_labels(labels, existing):
            try:
                self.client.post(
                    f"{self._repo_path}/labels",
                    {"name": label, "color": "0e8a16", "description": "Central PR automation"},
                )
            except GitHubError as exc:
                # 422 means it already exists (race with another run); anything else is a note.
                if exc.status != 422:
                    self.notes.append(f"Could not create label '{label}'.")
        self.client.post(f"{self._repo_path}/issues/{self.number}/labels", {"labels": labels})
        return labels

    def update_metadata(
        self,
        metadata: Mapping[str, Any],
        *,
        title_policy: str = "weak",
        context: Mapping[str, Any] | None = None,
        sections: list[str] | None = None,
        headings: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Update the PR description, and the title when the human one is a placeholder."""
        summary = (metadata.get("summary") or "").strip()
        if not summary:
            # Never overwrite PR metadata with an empty analysis result.
            self.notes.append("Automation produced no summary; PR body left unchanged.")
            return {"body_updated": False, "title_updated": False,
                    "title_reason": "No generated summary, so nothing was written."}

        pr = self.client.get(f"{self._repo_path}/pulls/{self.number}")
        current_title = pr.get("title") or ""
        proposed_title = (metadata.get("title") or "").strip()
        replace, reason = title_decision(current_title, proposed_title, policy=title_policy)

        payload: dict[str, Any] = {
            "body": upsert_summary(
                pr.get("body") or "", summary, metadata, context=context, sections=sections,
                headings=headings
            )
        }
        if replace:
            payload["title"] = proposed_title
        self.client.patch(f"{self._repo_path}/pulls/{self.number}", payload)

        if replace:
            self.notes.append(f"Title replaced: {reason}")
        return {
            "body_updated": True,
            "title_updated": replace,
            "title_reason": reason,
            "previous_title": current_title,
            "new_title": proposed_title if replace else current_title,
        }

    def request_reviewers(self, users: list[str], teams: list[str]) -> dict[str, list[str]]:
        """Request reviewers, degrading gracefully when a name does not resolve."""
        if not users and not teams:
            return {"users": [], "teams": []}
        path = f"{self._repo_path}/pulls/{self.number}/requested_reviewers"
        try:
            self.client.post(path, {"reviewers": users, "team_reviewers": teams})
            return {"users": users, "teams": teams}
        except GitHubError as exc:
            if exc.status not in (403, 422):
                raise
        # One name is bad; find out which by requesting them one at a time.
        accepted_users: list[str] = []
        accepted_teams: list[str] = []
        for user in users:
            try:
                self.client.post(path, {"reviewers": [user]})
                accepted_users.append(user)
            except GitHubError:
                self.notes.append(f"Reviewer '{user}' could not be requested.")
        for team in teams:
            try:
                self.client.post(path, {"team_reviewers": [team]})
                accepted_teams.append(team)
            except GitHubError:
                self.notes.append(f"Reviewer team '{team}' could not be requested.")
        return {"users": accepted_users, "teams": accepted_teams}

    def post_inline_review(
        self,
        findings: list[dict[str, Any]],
        head_sha: str,
        *,
        max_comments: int = MAX_INLINE_COMMENTS,
    ) -> dict[str, Any]:
        """Post verified findings as inline review comments on the PR diff.

        Every ``line`` here has already been checked against the right-side diff
        map, so no comment is anchored to a line GitHub does not have. Findings
        already commented on in an earlier run are skipped.
        """
        if not findings:
            return {"posted": 0, "skipped_existing": 0, "rejected": 0, "overflow": []}

        existing = self._existing_finding_keys()
        fresh = [f for f in findings if finding_key(f) not in existing]
        skipped = len(findings) - len(fresh)
        overflow = fresh[max_comments:]
        batch = fresh[:max_comments]
        if not batch:
            return {"posted": 0, "skipped_existing": skipped, "rejected": 0,
                    "overflow": [f["title"] for f in overflow]}

        comments = [
            {
                "path": f["path"],
                "line": f["line"],
                "side": "RIGHT",
                "body": render_inline_comment(f),
            }
            for f in batch
        ]
        payload = {
            "commit_id": head_sha,
            "event": "COMMENT",
            "body": (
                f"{REVIEW_MARKER}\n"
                f"Automated review: {len(batch)} inline finding(s). "
                "See the PR Automation Report comment for the full picture. "
                "This is advisory; human approval is still required."
            ),
            "comments": comments,
        }
        try:
            self.client.post(f"{self._repo_path}/pulls/{self.number}/reviews", payload)
            return {"posted": len(batch), "skipped_existing": skipped, "rejected": 0,
                    "overflow": [f["title"] for f in overflow]}
        except GitHubError as exc:
            if exc.status not in (403, 422):
                raise
        # One comment was rejected; find out which by posting them one at a time
        # rather than losing the whole review.
        posted, rejected = 0, 0
        for comment, finding in zip(comments, batch):
            single = {**payload, "comments": [comment], "body": REVIEW_MARKER}
            try:
                self.client.post(f"{self._repo_path}/pulls/{self.number}/reviews", single)
                posted += 1
            except GitHubError:
                rejected += 1
                self.notes.append(
                    f"GitHub rejected an inline comment on {comment['path']}:{comment['line']}."
                )
        return {"posted": posted, "skipped_existing": skipped, "rejected": rejected,
                "overflow": [f["title"] for f in overflow]}

    def _existing_finding_keys(self) -> set[str]:
        """Fingerprints of findings this automation already commented on."""
        try:
            comments = self.client.paginate(f"{self._repo_path}/pulls/{self.number}/comments")
        except GitHubError:
            # Better to risk a duplicate than to drop a finding entirely.
            self.notes.append("Could not list existing review comments; skipped de-duplication.")
            return set()
        keys: set[str] = set()
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            match = FINDING_KEY_PATTERN.search(comment.get("body") or "")
            if match:
                keys.add(match.group(1))
        return keys

    def upsert_comment(self, body: str) -> None:
        comments = self.client.paginate(f"{self._repo_path}/issues/{self.number}/comments")
        previous = next(
            (
                c
                for c in comments
                if isinstance(c, dict) and REPORT_MARKER in (c.get("body") or "")
            ),
            None,
        )
        if previous:
            self.client.patch(f"{self._repo_path}/issues/comments/{previous['id']}", {"body": body})
        else:
            self.client.post(f"{self._repo_path}/issues/{self.number}/comments", {"body": body})

    def publish_check(self, head_sha: str, verdict: str, text: str, summary: str,
                      *, readiness_verdict: str = "") -> None:
        """Publish the gate against the TARGET head SHA.

        The conclusion still comes from the quality gate alone: readiness includes
        human approvals and mergeability, which branch protection already enforces
        on its own, and a check run that fails for a missing approval would
        deadlock against it. Readiness appears in the title and body, where it
        informs without gating twice.
        """
        conclusion = {"PASS": "success", "PARTIAL": "neutral",
                      "NOT RUN": "neutral"}.get(verdict, "failure")
        title = f"Overall: {verdict}"
        if readiness_verdict:
            title = f"{readiness_verdict} · gate {verdict}"
        self.client.post(
            f"{self._repo_path}/check-runs",
            {
                "name": CHECK_NAME,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {"title": title[:255], "summary": summary, "text": text},
            },
        )


def write_gate_summary(
    full_name: str,
    number: int,
    sections: list[Section],
    data: Mapping[str, Any],
    verdict: str,
    labels: list[str],
    reviewers: Mapping[str, Any],
    metadata_outcome: Mapping[str, Any],
    inline_outcome: Mapping[str, Any],
    unanchored: list[Mapping[str, Any]],
    notes: list[str],
    readiness: readiness_module.Readiness | None = None,
) -> None:
    """Render the whole gate into the Actions job summary."""
    counts = data["counts"]
    pr_link = f"https://github.com/{full_name}/pull/{number}"
    blocks = [
        summary.heading(
            f"{readiness.icon} Merge readiness: {readiness.verdict}" if readiness
            else f"{summary.icon(verdict)} Quality Gate: {verdict}"
        ),
        summary.definitions(
            {"Pull request": f"[{full_name}#{number}]({pr_link})", "Check run": f"`{CHECK_NAME}`",
             "Quality gate": f"{summary.icon(verdict)} {verdict}"}
        ),
    ]
    if readiness is not None:
        blocks.append(summary.table(
            ["Dimension", "Status", "Blocking", "Detail"],
            [
                [d.name, f"{d.icon} {d.status.replace('_', ' ').upper()}",
                 "yes" if d.blocking else "no", d.detail]
                for d in readiness.dimensions
            ],
        ))
        if readiness.blockers:
            blocks.append(
                "> [!CAUTION]\n> Blocking this merge:\n"
                + "\n".join(f"> - {item}" for item in readiness.blockers)
            )
        if readiness.warnings:
            blocks.append(summary.details(
                f"Needs a look ({len(readiness.warnings)})",
                summary.bullets(readiness.warnings, limit=30),
            ))
    blocks += [
        summary.heading("Checks", 3),
        summary.table(
            ["Check", "Status", "Detail"],
            [[s.name, f"{summary.icon(s.status)} {s.status}", s.detail] for s in sections],
        ),
        summary.table(
            ["Critical", "High", "Medium", "Low/Info", "Test gaps"],
            [[counts["critical"], counts["high"], counts["medium"], counts["low"],
              len(data["gaps"])]],
        ),
    ]

    written = {
        "Labels": ", ".join(f"`{x}`" for x in labels) or "none",
        "Title": (
            f"replaced → `{metadata_outcome.get('new_title')}`"
            if metadata_outcome.get("title_updated")
            else f"unchanged — {metadata_outcome.get('title_reason') or 'not requested'}"
        ),
        "Body": "generated summary block written" if metadata_outcome.get("body_updated") else "unchanged",
        "Reviewers": (
            "deferred to CODEOWNERS" if reviewers.get("deferred_to_codeowners")
            else ", ".join(
                [f"@{u}" for u in reviewers.get("users") or []]
                + [f"@{t}" for t in reviewers.get("teams") or []]
            ) or "none requested"
        ),
        "CODEOWNERS owners": ", ".join(
            [f"@{u}" for u in reviewers.get("suggested_users") or []]
            + [f"@{t}" for t in reviewers.get("suggested_teams") or []]
        ) or "none identified",
        "Inline comments": (
            f"{inline_outcome.get('posted', 0)} posted"
            + (f", {inline_outcome.get('skipped_existing', 0)} already present"
               if inline_outcome.get("skipped_existing") else "")
            + (f", {inline_outcome.get('rejected', 0)} rejected"
               if inline_outcome.get("rejected") else "")
            + (f", {len(unanchored)} not anchorable" if unanchored else "")
        ),
    }
    blocks.append(summary.heading("Written to the pull request", 3))
    blocks.append(summary.definitions(written))

    findings = data["findings"]
    if findings:
        blocks.append(
            summary.details(
                f"Findings ({len(findings)})",
                summary.table(
                    ["Severity", "File", "Line", "Finding"],
                    [
                        [f["severity"], f"`{f.get('path') or '—'}`", f.get("line") or "—",
                         f["title"]]
                        for f in findings[:40]
                    ],
                ),
                open_by_default=verdict == FAIL,
            )
        )
    if unanchored:
        blocks.append(
            summary.details(
                f"Findings not anchorable to a diff line ({len(unanchored)})",
                summary.bullets(
                    f"`{f.get('path') or '—'}`:{f.get('line') or '—'} — {f.get('title')} "
                    f"({f.get('unanchored_reason')})"
                    for f in unanchored
                ),
            )
        )
    if data["gaps"]:
        blocks.append(
            summary.details(f"Potential test gaps ({len(data['gaps'])})",
                            summary.bullets(data["gaps"]))
        )

    # Each insight's own findings, so the run page explains every advisory row.
    for flag, label in (
        ("secrets", "Detected credentials"),
        ("api", "Interface changes"),
        ("migrations", "Migration checks"),
        ("deps", "Dependency changes"),
        ("size", "Size and risk"),
    ):
        payload = (data.get("insights") or {}).get(flag) or {}
        insight_findings = payload.get("findings") or []
        if not insight_findings:
            continue
        blocks.append(summary.details(
            f"{label} ({len(insight_findings)})",
            summary.table(
                ["Severity", "File", "Finding", "Detail"],
                [
                    [
                        f.get("severity", ""),
                        (f"`{f['path']}`" + (f":{f['line']}" if f.get("line") else ""))
                        if f.get("path") else "—",
                        f.get("title", ""),
                        f.get("detail", ""),
                    ]
                    for f in insight_findings[:30]
                ],
            ),
            open_by_default=flag == "secrets",
        ))

    coverage_analysis = (data.get("coverage") or {}).get("analysis") or {}
    if coverage_analysis.get("denominator"):
        blocks.append(summary.details(
            f"Changed-line coverage "
            f"({coverage_analysis.get('changed_line_coverage')}%)",
            summary.table(
                ["File", "Covered", "Measured", "%", "Uncovered lines"],
                [
                    [f"`{f['path']}`", f["covered_lines"], f["measured_lines"],
                     "—" if f["percent"] is None else f"{f['percent']}%",
                     ", ".join(str(n) for n in f["uncovered_lines"][:12])]
                    for f in coverage_analysis.get("files") or []
                ][:30],
            ),
        ))

    flaky_analysis = (data.get("flaky") or {}).get("analysis") or {}
    if flaky_analysis.get("flaky") or flaky_analysis.get("consistent_failures"):
        blocks.append(summary.details(
            f"Flaky tests ({len(flaky_analysis.get('flaky') or [])}) and consistent "
            f"failures ({len(flaky_analysis.get('consistent_failures') or [])})",
            summary.table(
                ["Test", "Kind", "Why"],
                [
                    [f"`{entry['identifier']}`", kind, entry.get("reason", "")]
                    for kind, key in (("flaky", "flaky"),
                                      ("failing", "consistent_failures"))
                    for entry in (flaky_analysis.get(key) or [])[:20]
                ],
            ),
        ))

    release_notes = (data.get("release_notes") or {}).get("markdown") or ""
    if release_notes.strip():
        blocks.append(summary.details("Draft release notes", release_notes.strip()))

    if notes:
        blocks.append(summary.details(f"Notes ({len(notes)})", summary.bullets(notes, limit=30)))

    blocks.append("_Human approval is still required before merge._")
    summary.write(*blocks)


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    out_dir = Path(env.get("RESULT_DIR") or artifact_dir(env))

    try:
        plan = json.loads(env.get("PR_AUTOMATION_PLAN") or "{}")
        token = env["TARGET_GITHUB_TOKEN"]
    except (KeyError, json.JSONDecodeError) as exc:
        print(f"::error::Missing or invalid report inputs: {exc}", file=sys.stderr)
        return 2

    context = _read_json(out_dir / "pr-context.json")
    if context is None:
        print("::error::pr-context.json is missing; cannot report.", file=sys.stderr)
        return 2

    analysis = _read_json(out_dir / "analysis.json")
    quality = _read_json(out_dir / "quality-result.json")
    security = _read_json(out_dir / "security-result.json")
    coverage = _read_json(out_dir / "coverage-result.json")
    flaky = _read_json(out_dir / "flaky-result.json")
    conflicts = _read_json(out_dir / "conflicts.json")

    owner, repo = plan.get("owner", ""), plan.get("repo") or plan.get("repository", "")
    number = int(context["number"])
    head_sha = str(env.get("TARGET_REPORT_HEAD_SHA") or context["head_sha"])

    sections, data = build_sections(plan, analysis, quality, security, coverage, flaky)
    verdict = overall(sections)

    client = GitHubClient(token)
    reporter = Reporter(client, owner, repo, number)
    try:
        reporter.verify_head(head_sha)
    except AutomationError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 4

    labels: list[str] = []
    if plan.get("labels"):
        labels = derive_labels(
            context.get("files") or [],
            title=context.get("title") or "",
            body=context.get("body") or "",
            metadata=data["metadata"] or {},
        )
        labels = reporter.apply_labels(labels)

    metadata_outcome: dict[str, Any] = {}
    if plan.get("metadata") and data["metadata"]:
        options = plan.get("metadata_options") or {}
        metadata_outcome = reporter.update_metadata(
            data["metadata"],
            title_policy=str(options.get("title_policy") or "weak"),
            context=context,
            sections=valid_sections(options.get("sections")),
            headings=valid_headings(options.get("headings")),
        )

    # Inline review comments, anchored only to lines that are genuinely in the diff.
    inline_outcome: dict[str, Any] = {"posted": 0}
    unanchored: list[dict[str, Any]] = []
    if data["review"] is not None and plan.get("inline_comments", True):
        diff_path = out_dir / "pr.diff"
        diff_map = parse_diff(diff_path.read_text(encoding="utf-8", errors="replace")) \
            if diff_path.is_file() else {}
        candidates = [
            f for f in data["findings"] if str(f.get("severity")) in INLINE_SEVERITIES
        ]
        anchored, unanchored = split_anchored(diff_map, candidates)
        if not diff_map:
            reporter.notes.append("No diff artifact available; inline comments skipped.")
        else:
            inline_outcome = reporter.post_inline_review(anchored, head_sha)
            if unanchored:
                reporter.notes.append(
                    f"{len(unanchored)} finding(s) could not be anchored to a diff line "
                    "and appear in the report only."
                )

    reviewers: dict[str, Any] = {"users": [], "teams": [], "notes": []}
    if plan.get("reviewers"):
        codeowners_file = out_dir / "codeowners.txt"
        request = resolve_reviewers(
            reviewer_groups(env.get("REVIEWERS_CONFIG") or "config/reviewers.yml"),
            context.get("files") or [],
            author=context.get("author") or "",
            existing_users=context.get("existing_reviewers") or [],
            existing_teams=context.get("existing_reviewer_teams") or [],
            codeowners=codeowners_file.read_text(encoding="utf-8")
            if codeowners_file.is_file()
            else None,
            codeowners_mode=str(
                (plan.get("reviewer_options") or {}).get("codeowners")
                or DEFAULT_CODEOWNERS_MODE
            ),
        )
        reviewers = request.to_dict()
        if not request.deferred_to_codeowners and not request.empty:
            accepted = reporter.request_reviewers(request.users, request.teams)
            reviewers["users"], reviewers["teams"] = accepted["users"], accepted["teams"]

    # The merge-readiness verdict: one answer, assembled from every section plus
    # the two things only GitHub knows — approvals and mergeability.
    readiness = readiness_module.assess(
        sections=[{"name": s.name, "status": s.status, "detail": s.detail} for s in sections],
        context=context,
        conflicts=conflicts,
        config=(plan.get("policies") or {}).get("readiness") or {},
    )

    reviewers["notes"] = list(reviewers.get("notes") or []) + reporter.notes
    comment = render_comment(
        f"{owner}/{repo}",
        number,
        sections,
        data,
        labels,
        reviewers,
        verdict,
        metadata_outcome=metadata_outcome,
        inline_outcome=inline_outcome,
        unanchored=unanchored,
        readiness=readiness,
    )
    reporter.upsert_comment(comment)

    counts = data["counts"]
    summary = (
        f"Merge readiness: {readiness.verdict}. "
        f"{len(data['findings'])} rule finding(s); critical {counts['critical']}, "
        f"high {counts['high']}; test gaps {len(data['gaps'])}."
    )
    if readiness.blockers:
        summary += " Blocking: " + "; ".join(readiness.blockers[:5])
    reporter.publish_check(
        head_sha, verdict,
        render_check_output(sections, counts, verdict, readiness), summary,
        readiness_verdict=readiness.verdict,
    )

    gate = {
        "overall": verdict,
        "readiness": readiness.to_dict(),
        "sections": [{"name": s.name, "status": s.status, "detail": s.detail} for s in sections],
        "counts": counts,
        "metadata": metadata_outcome,
        "inline_comments": inline_outcome,
        "unanchored_findings": len(unanchored),
        "review_coverage": {
            "batches": (data.get("review") or {}).get("batches"),
            "complete": (data.get("review") or {}).get("complete"),
            "unreviewed_paths": (data.get("review") or {}).get("unreviewed_paths") or [],
        },
        "changed_line_coverage": (
            (data.get("coverage") or {}).get("analysis") or {}
        ).get("changed_line_coverage"),
        "secrets_found": len(((data.get("insights") or {}).get("secrets") or {}).get("hits") or []),
        "reviewers": {
            "requested_users": reviewers.get("users") or [],
            "requested_teams": reviewers.get("teams") or [],
            "codeowners_users": reviewers.get("suggested_users") or [],
            "codeowners_teams": reviewers.get("suggested_teams") or [],
            "unowned_paths": reviewers.get("unowned_paths") or [],
        },
        "notes": reporter.notes,
    }
    (out_dir / "gate.json").write_text(json.dumps(gate, indent=2) + "\n", encoding="utf-8")

    write_gate_summary(
        f"{owner}/{repo}", number, sections, data, verdict, labels, reviewers,
        metadata_outcome, inline_outcome, unanchored, reporter.notes, readiness,
    )

    for section in sections:
        print(f"{section.name}: {section.status}")
    print(f"Quality gate: {verdict}")
    print(f"Merge readiness: {readiness.verdict}")
    for blocker in readiness.blockers:
        print(f"  blocking: {blocker}")
    # Publishing a failed target gate is a successful reporting operation. The
    # target check run already carries conclusion=failure and is what branch
    # protection should enforce. Returning 1 here incorrectly makes the reporter
    # look broken and obscures the upstream validation failures it successfully
    # published.
    if verdict == FAIL:
        print("::warning::Target quality gate failed; the failure was published to the PR check.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
