"""Write results back to the target pull request.

Run as ``python -m scripts.github.report``.

This is the only module that holds a write-capable token, and it executes no
target repository code. It consumes only the explicitly named result artifacts.

Fail-safe rules enforced here:
  * a section whose AI analysis failed is rendered as ERROR/NOT RUN and its
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

from ..common import summary
from ..common.config import artifact_dir, reviewer_groups
from ..common.errors import AutomationError, GitHubError
from ..common.labels import derive_labels, missing_labels
from ..common.reviewers import resolve_reviewers
from ..common.titles import title_decision
from .api import GitHubClient
from .body import SUMMARY_END, SUMMARY_START
from .body import upsert as upsert_body
from .body import valid_sections
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
        + "\n_AI review finding. Advisory only; resolve or dismiss as you see fit._"
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


def build_sections(
    plan: Mapping[str, Any],
    analysis: Mapping[str, Any] | None,
    quality: Mapping[str, Any] | None,
    security: Mapping[str, Any] | None,
) -> tuple[list[Section], dict[str, Any]]:
    """Build the gate sections and the counts the report needs."""
    sections: list[Section] = []
    analysis = analysis or {}

    def ai_section(name: str, key: str, requested: bool) -> Mapping[str, Any] | None:
        if not requested:
            sections.append(Section(name, NOT_RUN, "Not requested for this action."))
            return None
        entry = analysis.get(key)
        if not isinstance(entry, Mapping):
            sections.append(Section(name, NOT_RUN, "AI analysis produced no result."))
            return None
        if entry.get("status") == "ok":
            return entry.get("data") or {}
        detail = str(entry.get("error") or entry.get("reason") or "")
        sections.append(Section(name, ERROR if entry.get("status") == "error" else NOT_RUN, detail))
        return None

    metadata = ai_section("Metadata", "metadata", bool(plan.get("metadata")))
    if metadata is not None:
        sections.append(Section("Metadata", PASS, "Summary generated"))

    review_requested = bool(plan.get("review") or plan.get("security"))
    review = ai_section("Review", "review", review_requested)
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

    tests = ai_section("Test coverage", "tests", bool(plan.get("tests")))
    gaps = list((tests or {}).get("gaps") or [])
    if tests is not None:
        sections.append(Section("Test coverage", PASS, f"{len(gaps)} potential gap(s)"))

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

    return sections, {"findings": findings, "counts": counts, "gaps": gaps,
                      "metadata": metadata, "review": review, "tests": tests}


def overall(sections: Iterable[Section]) -> str:
    """Collapse the sections into one verdict.

    PARTIAL must propagate: a scanner that was enabled but could not run, or a
    review that could not cover every file, is not a pass.
    """
    items = list(sections)
    if any(section.status == FAIL for section in items):
        return FAIL
    if any(section.status in (ERROR, PARTIAL) for section in items):
        return PARTIAL
    if any(section.status == PASS for section in items):
        return PASS
    return NOT_RUN


def render_check_output(sections: list[Section], counts: Mapping[str, int], verdict: str) -> str:
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
    return "\n".join(lines)


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

    reviewer_line = "_None requested._"
    if reviewers.get("deferred_to_codeowners"):
        reviewer_line = "CODEOWNERS already covers every changed file."
    elif reviewers.get("users") or reviewers.get("teams"):
        names = [f"@{u}" for u in reviewers.get("users", [])]
        names += [f"@{full_name.split('/')[0]}/{t}" for t in reviewers.get("teams", [])]
        reviewer_line = ", ".join(names)
    elif reviewers.get("notes"):
        reviewer_line = "; ".join(reviewers["notes"])

    return (
        f"{REPORT_MARKER}\n"
        f"## 🤖 PR Automation Report\n\n"
        f"**Target:** `{full_name}` PR #{number}\n\n"
        f"| Check | Status | Detail |\n| --- | --- | --- |\n{rows}\n\n"
        f"**Overall:** **{verdict}**\n"
        f"{coverage_line}{title_line}{inline_line}\n"
        f"**Labels:** {', '.join(f'`{x}`' for x in labels) or '_none_'}\n\n"
        f"**Reviewers:** {reviewer_line}\n\n"
        f"<details><summary>Findings "
        f"(critical {counts['critical']}, high {counts['high']}, medium {counts['medium']}, "
        f"low {counts['low']})</summary>\n\n{findings_block}\n\n</details>\n\n"
        f"<details><summary>Potential test gaps ({len(data['gaps'])})</summary>\n\n"
        f"{gaps_block}\n\n</details>\n\n"
        f"Human approval is still required before merge."
    )


def upsert_summary(
    body: str,
    summary: str,
    metadata: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    sections: list[str] | None = None,
) -> str:
    """Replace the managed description block, preserving author-written text.

    Rendering lives in :mod:`scripts.github.body`; ``summary`` is used only as the
    fallback description when the model returned no long-form description.
    """
    payload = dict(metadata)
    if not (payload.get("description") or "").strip():
        payload["description"] = summary
    return upsert_body(body, payload, context, sections=sections)


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
    ) -> dict[str, Any]:
        """Update the PR description, and the title when the human one is a placeholder."""
        summary = (metadata.get("summary") or "").strip()
        if not summary:
            # Never overwrite PR metadata with an empty AI result.
            self.notes.append("AI produced no summary; PR body left unchanged.")
            return {"body_updated": False, "title_updated": False,
                    "title_reason": "No AI summary, so nothing was written."}

        pr = self.client.get(f"{self._repo_path}/pulls/{self.number}")
        current_title = pr.get("title") or ""
        proposed_title = (metadata.get("title") or "").strip()
        replace, reason = title_decision(current_title, proposed_title, policy=title_policy)

        payload: dict[str, Any] = {
            "body": upsert_summary(
                pr.get("body") or "", summary, metadata, context=context, sections=sections
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
                f"AI review: {len(batch)} inline finding(s). "
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

    def publish_check(self, head_sha: str, verdict: str, text: str, summary: str) -> None:
        """Publish the gate against the TARGET head SHA."""
        conclusion = {"PASS": "success", "PARTIAL": "neutral",
                      "NOT RUN": "neutral"}.get(verdict, "failure")
        self.client.post(
            f"{self._repo_path}/check-runs",
            {
                "name": CHECK_NAME,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": {"title": f"Overall: {verdict}", "summary": summary, "text": text},
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
) -> None:
    """Render the whole gate into the Actions job summary."""
    counts = data["counts"]
    pr_link = f"https://github.com/{full_name}/pull/{number}"
    blocks = [
        summary.heading(f"{summary.icon(verdict)} Quality Gate: {verdict}"),
        summary.definitions(
            {"Pull request": f"[{full_name}#{number}]({pr_link})", "Check run": f"`{CHECK_NAME}`"}
        ),
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
        "Body": "AI summary block written" if metadata_outcome.get("body_updated") else "unchanged",
        "Reviewers": (
            "deferred to CODEOWNERS" if reviewers.get("deferred_to_codeowners")
            else ", ".join(
                [f"@{u}" for u in reviewers.get("users") or []]
                + [f"@{t}" for t in reviewers.get("teams") or []]
            ) or "none requested"
        ),
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

    owner, repo = plan.get("owner", ""), plan.get("repo") or plan.get("repository", "")
    number = int(context["number"])
    head_sha = str(context["head_sha"])

    sections, data = build_sections(plan, analysis, quality, security)
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
        )
        reviewers = request.to_dict()
        if not request.deferred_to_codeowners and not request.empty:
            accepted = reporter.request_reviewers(request.users, request.teams)
            reviewers["users"], reviewers["teams"] = accepted["users"], accepted["teams"]

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
    )
    reporter.upsert_comment(comment)

    counts = data["counts"]
    summary = (
        f"{len(data['findings'])} finding(s); critical {counts['critical']}, "
        f"high {counts['high']}; test gaps {len(data['gaps'])}."
    )
    reporter.publish_check(
        head_sha, verdict, render_check_output(sections, counts, verdict), summary
    )

    gate = {
        "overall": verdict,
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
        "notes": reporter.notes,
    }
    (out_dir / "gate.json").write_text(json.dumps(gate, indent=2) + "\n", encoding="utf-8")

    write_gate_summary(
        f"{owner}/{repo}", number, sections, data, verdict, labels, reviewers,
        metadata_outcome, inline_outcome, unanchored, reporter.notes,
    )

    for section in sections:
        print(f"{section.name}: {section.status}")
    print(f"Overall: {verdict}")
    # Only a real FAIL reddens the run. A missing optional artifact is NOT RUN.
    return 1 if verdict == FAIL else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
