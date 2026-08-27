"""Detect credentials in the lines this pull request adds.

This complements gitleaks rather than replacing it. gitleaks runs in the
validation job, needs a binary on the runner, and scans the whole tree; this runs
in the credential-free analysis job with no dependencies at all, scans only the
added lines, and always produces a result. When gitleaks is unavailable — a
network hiccup on the download step, a repository with scanners disabled — this
is the difference between "no secret scanning happened" and a real answer.

Non-negotiable rules enforced here:

  * a matched secret value is NEVER copied into a finding, a result file, a log
    line or a PR comment; only a masked fingerprint is kept
  * a finding names the file, line and rule so the value can be found and rotated
    without this tool ever repeating it
  * placeholders are recognised and excluded, because a scanner that cries wolf
    on ``password = "changeme"`` gets switched off within a week
"""
from __future__ import annotations

import base64
import math
import re
from typing import Any, Iterable, Mapping, Sequence

from ..common.globs import matches
from .base import Finding, as_bool, as_int, as_patterns, block, result, verdict
from .changes import FileChange, Line

#: Files where a credential-shaped string is the documented, intended content.
DEFAULT_ALLOW_PATHS = (
    "**/*.example",
    "**/*.sample",
    "**/*.template",
    "**/.env.example",
    "**/.env.sample",
    "**/.env.template",
    "**/*.md",
    "**/*.rst",
    "**/fixtures/**",
    "**/testdata/**",
    "**/__fixtures__/**",
    "**/*.lock",
    "**/package-lock.json",
    "**/poetry.lock",
    "**/composer.lock",
    "**/Gemfile.lock",
    "**/go.sum",
    "**/*.snap",
)

#: Substrings that mark a value as a placeholder rather than a live credential.
PLACEHOLDER_WORDS = (
    "example", "changeme", "change-me", "change_me", "placeholder", "your-", "your_",
    "yourkey", "yourtoken", "dummy", "sample", "insert", "redacted", "xxxxx", "aaaaa",
    "todo", "fixme", "notreal", "fake", "test-token", "testtoken", "dev-secret",
    "secretsecret", "password123", "hunter2", "s3cr3t", "abc123", "foobar", "lorem",
    "0000000", "1234567", "<your", "replace", "somepassword", "somesecret",
)

#: Value forms that are references to a secret, not the secret itself.
_REFERENCE = re.compile(
    r"""(?x)
    ^\s*(?:
        \$\{?[A-Za-z_] |                # ${VAR} / $VAR
        %[A-Za-z_][A-Za-z0-9_]*% |      # %VAR% (Windows)
        \{\{ |                          # {{ template }}
        <% |                            # <%= erb %>
        os\.environ | os\.getenv |
        process\.env |
        System\.getenv |
        ENV\[ | getenv\( |
        config\( | env\( | secrets?\. |
        vault: | sops: | aws-vault |
        \*{3,} | x{5,} | \.{3,}
    )
    """,
    re.IGNORECASE,
)

#: Lines that are obviously not an assignment of a live value.
_COMMENTED_OUT = re.compile(r"^\s*(?:#|//|;|--|\*|/\*)\s*(?:[A-Z_]+\s*[:=])?", re.IGNORECASE)


class Detector:
    """One named credential pattern.

    ``group`` names the regex group holding the sensitive value, so masking
    always covers the value and never the surrounding key name.
    """

    def __init__(self, rule: str, title: str, severity: str, pattern: str,
                 *, group: int = 0, min_entropy: float = 0.0,
                 provider: str = "", flags: int = 0) -> None:
        self.rule = rule
        self.title = title
        self.severity = severity
        self.regex = re.compile(pattern, flags)
        self.group = group
        self.min_entropy = min_entropy
        self.provider = provider or title


#: Provider-specific patterns first: they are precise, so they are high confidence.
DETECTORS: tuple[Detector, ...] = (
    Detector("aws-access-key-id", "AWS access key ID", "critical",
             r"\b((?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16})\b", group=1),
    Detector("aws-secret-access-key", "AWS secret access key", "critical",
             r"(?i)aws.{0,20}?(?:secret|access).{0,20}?key.{0,5}['\"]?\s*[:=]\s*"
             r"['\"]?([A-Za-z0-9/+=]{40})", group=1, min_entropy=3.5),
    Detector("github-token", "GitHub token", "critical",
             r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,255})\b", group=1),
    Detector("github-fine-grained-pat", "GitHub fine-grained token", "critical",
             r"\b(github_pat_[A-Za-z0-9_]{22,255})\b", group=1),
    Detector("gitlab-token", "GitLab token", "critical",
             r"\b(glpat-[A-Za-z0-9\-_]{20,})\b", group=1),
    Detector("slack-token", "Slack token", "critical",
             r"\b(xox[abposr]-[A-Za-z0-9-]{10,})\b", group=1),
    Detector("slack-webhook", "Slack webhook URL", "high",
             r"(https://hooks\.slack\.com/services/[A-Za-z0-9/+]{20,})", group=1),
    Detector("google-api-key", "Google API key", "critical",
             r"\b(AIza[0-9A-Za-z\-_]{35})\b", group=1),
    Detector("gcp-service-account", "Google service-account private key", "critical",
             r"\"type\"\s*:\s*\"(service_account)\"", group=1),
    Detector("stripe-secret-key", "Stripe secret key", "critical",
             r"\b((?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,})\b", group=1),
    Detector("sendgrid-key", "SendGrid API key", "critical",
             r"\b(SG\.[A-Za-z0-9\-_]{16,}\.[A-Za-z0-9\-_]{16,})\b", group=1),
    Detector("twilio-key", "Twilio API key", "critical",
             r"\b(SK[0-9a-fA-F]{32})\b", group=1),
    Detector("openai-key", "OpenAI API key", "critical",
             r"\b(sk-(?:proj-)?[A-Za-z0-9\-_]{32,})\b", group=1),
    Detector("anthropic-key", "Anthropic API key", "critical",
             r"\b(sk-ant-[A-Za-z0-9\-_]{24,})\b", group=1),
    Detector("nvidia-key", "NVIDIA API key", "critical",
             r"\b(nvapi-[A-Za-z0-9\-_]{32,})\b", group=1),
    Detector("hugging-face-token", "Hugging Face token", "high",
             r"\b(hf_[A-Za-z0-9]{30,})\b", group=1),
    Detector("npm-token", "npm access token", "critical",
             r"\b(npm_[A-Za-z0-9]{36})\b", group=1),
    Detector("pypi-token", "PyPI upload token", "critical",
             r"\b(pypi-[A-Za-z0-9\-_]{50,})\b", group=1),
    Detector("digitalocean-token", "DigitalOcean token", "critical",
             r"\b(dop_v1_[0-9a-f]{64})\b", group=1),
    Detector("square-token", "Square access token", "critical",
             r"\b((?:sq0atp|sq0csp|EAAA)[A-Za-z0-9\-_]{20,})\b", group=1),
    Detector("mailgun-key", "Mailgun API key", "critical",
             r"\b(key-[0-9a-f]{32})\b", group=1),
    Detector("private-key-block", "Private key block", "critical",
             r"(-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----)",
             group=1),
    Detector("ssh-private-key-path", "SSH private key committed", "high",
             r"(-----BEGIN OPENSSH PRIVATE KEY-----)", group=1),
    Detector("jwt", "JSON Web Token", "high",
             r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b",
             group=1),
    Detector("basic-auth-url", "Credential embedded in a URL", "critical",
             r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:([^\s/@]{4,})@", group=1),
    Detector("connection-string-password", "Database connection string with a password",
             "critical",
             r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql)"
             r"://[^\s:/@]+:([^\s/@]{4,})@", group=1),
    # Generic assignments last: broadest, so entropy-gated to keep the noise down.
    Detector("generic-secret-assignment", "Hard-coded credential", "high",
             r"""(?ix)
             \b(?:api[_-]?key|apikey|secret[_-]?key|secret|password|passwd|pwd|
                 access[_-]?token|auth[_-]?token|bearer[_-]?token|private[_-]?key|
                 client[_-]?secret|encryption[_-]?key|signing[_-]?key|
                 credential|passphrase)
             \s*(?:=>|:=|[:=])\s*
             (?:r|b|f|u)?['"]([^'"\n]{8,120})['"]
             """, group=1, min_entropy=3.0),
    Detector("env-file-assignment", "Credential in an environment file", "critical",
             r"""(?ix)
             ^\s*(?:export\s+)?
             [A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PASSPHRASE|DSN)
             \s*=\s*
             ['"]?([^\s'"#]{8,200})['"]?\s*$
             """, group=1, min_entropy=2.6),
)

#: Detectors that only apply to files that are actually environment files.
_ENV_ONLY = frozenset({"env-file-assignment"})

ENV_FILE_PATTERNS = (
    "**/.env", "**/.env.*", "**/*.env", "**/env", "**/environment",
    "**/*.envrc", "**/.envrc",
)

DEFAULTS = {
    "enabled": True,
    "min_entropy": 3.0,
    "fail_on": "high",
    "max_findings": 100,
    "scan_deleted": False,
}


def shannon_entropy(value: str) -> float:
    """Bits of entropy per character. 0.0 for an empty string."""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def mask(value: str) -> str:
    """A non-reversible preview of a secret.

    Keeps the first two characters — enough to identify which credential this is
    when several are in flight — plus the length. The rest never leaves this
    function. Values of six characters or fewer are masked entirely.
    """
    text = str(value or "")
    if len(text) <= 6:
        return "*" * len(text)
    return f"{text[:2]}{'*' * min(len(text) - 2, 12)} ({len(text)} chars)"


def looks_like_placeholder(value: str) -> bool:
    """True when a matched value is documentation, not a live credential."""
    text = str(value or "")
    lowered = text.lower()
    if _REFERENCE.match(text):
        return True
    if any(word in lowered for word in PLACEHOLDER_WORDS):
        return True
    # A single repeated character, or a strictly sequential run, is filler.
    if len(set(lowered)) <= 2:
        return True
    if lowered in {"true", "false", "none", "null", "nil", "undefined", "localhost"}:
        return True
    return False


def _decodes_to_text(value: str) -> bool:
    """True when a base64-looking value decodes to plain readable text.

    A base64 blob of prose is a config string; a base64 blob of bytes may be a
    key. This keeps encoded English out of the results.
    """
    if len(value) < 16 or len(value) % 4:
        return False
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for char in text if char.isprintable() or char in "\r\n\t")
    return printable / max(len(text), 1) > 0.9


def is_env_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if name.startswith(".env") or name.endswith(".env"):
        return True
    return any(matches(pattern, path) for pattern in ENV_FILE_PATTERNS)


def scan_line(line: Line, *, min_entropy: float = 3.0,
              detectors: Sequence[Detector] = DETECTORS) -> list[dict[str, Any]]:
    """Every detector hit on one added line, with the value already masked."""
    text = line.text
    if not text.strip():
        return []
    env_file = is_env_file(line.path)
    commented = bool(_COMMENTED_OUT.match(text))
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()

    for detector in detectors:
        if detector.rule in _ENV_ONLY and not env_file:
            continue
        for match in detector.regex.finditer(text):
            try:
                value = match.group(detector.group)
            except IndexError:  # pragma: no cover - a detector with a bad group index
                continue
            if not value:
                continue
            if looks_like_placeholder(value):
                continue
            # The caller's entropy floor applies only to detectors fuzzy enough to
            # declare a requirement of their own. Provider patterns are precise, so
            # a low-entropy match against one is still a real key.
            threshold = max(detector.min_entropy, min_entropy) if detector.min_entropy else 0.0
            if threshold and shannon_entropy(value) < threshold:
                continue
            if detector.min_entropy and _decodes_to_text(value):
                continue
            if detector.rule in seen:
                continue
            seen.add(detector.rule)
            hits.append({
                "rule": detector.rule,
                "title": detector.title,
                "severity": "medium" if commented else detector.severity,
                "provider": detector.provider,
                "path": line.path,
                "line": line.line,
                # The value itself is never stored. Only this masked preview is.
                "masked": mask(value),
                "entropy": round(shannon_entropy(value), 2),
                "commented_out": commented,
            })
    return hits


def evaluate(
    changes: Mapping[str, FileChange],
    config: Mapping[str, Any] | None = None,
    *,
    diff_truncated: bool = False,
) -> dict[str, Any]:
    """Scan every added line in the diff for credential material."""
    policy = {**DEFAULTS, **block(config, "secret_scan")}
    if not as_bool(policy.get("enabled"), True):
        return result("not_run", "Secret scanning is disabled for this repository.", (),
                      scanned_files=0, scanned_lines=0, hits=[])

    allow_paths = list(DEFAULT_ALLOW_PATHS) + as_patterns(policy.get("allow_paths"))
    min_entropy = float(policy.get("min_entropy") or DEFAULTS["min_entropy"])
    max_findings = as_int(policy.get("max_findings"), DEFAULTS["max_findings"])

    hits: list[dict[str, Any]] = []
    scanned_files = scanned_lines = 0
    skipped: list[str] = []

    for path, change in sorted(changes.items()):
        if change.is_binary or change.is_deleted:
            continue
        # An allowlisted path is still checked for a real private key block: a key
        # committed as "example.pem" is a committed key.
        allowed = any(matches(pattern, path) for pattern in allow_paths)
        if allowed:
            skipped.append(path)
        scanned_files += 1
        for line in change.added:
            scanned_lines += 1
            for hit in scan_line(line, min_entropy=min_entropy):
                if allowed and hit["rule"] not in {"private-key-block", "ssh-private-key-path"}:
                    continue
                hits.append(hit)

    findings = [
        Finding(
            severity=hit["severity"],
            rule=f"secret:{hit['rule']}",
            title=f"{hit['title']} added",
            detail=(
                f"Added at `{hit['path']}`:{hit['line']} (masked: `{hit['masked']}`). "
                + ("The line is commented out, which is why this is not critical, but the "
                   "value is in the commit either way. " if hit["commented_out"] else "")
                + "Rotate the credential first, then remove the value and load it from a "
                "secret store or a CI secret. Deleting the line does not remove it from git "
                "history, and force-pushing does not un-leak a value that has already been "
                "pushed."
            ),
            path=hit["path"],
            line=hit["line"],
        )
        for hit in hits[:max_findings]
    ]

    # An explicitly empty fail_on means "report, never block", so it must not
    # fall through to the default.
    declared = policy.get("fail_on")
    fail_on = (
        DEFAULTS["fail_on"] if declared is None else str(declared).strip().lower()
    )
    status = verdict(findings, fail_at=fail_on, warn_at="low")
    if not hits and diff_truncated:
        status = "warn"

    summary = (
        f"{len(hits)} credential-shaped value(s) in added lines"
        if hits else f"No credential-shaped values in {scanned_lines} added line(s)"
    )
    if diff_truncated:
        summary += " (diff was truncated, so coverage is partial)"

    return result(
        status,
        summary,
        findings,
        scanned_files=scanned_files,
        scanned_lines=scanned_lines,
        # Masked only. No secret value is present anywhere in this payload.
        hits=hits[:max_findings],
        dropped=max(0, len(hits) - max_findings),
        allowlisted_paths=sorted(skipped)[:50],
        diff_truncated=diff_truncated,
        note=(
            "Values are masked and never stored. Rotate any credential reported here; "
            "removing the line does not remove it from git history."
        ),
    )


def rotation_advice(hits: Iterable[Mapping[str, Any]]) -> list[str]:
    """Per-provider rotation steps for the report, in a stable order."""
    providers = sorted({str(hit.get("provider") or "credential") for hit in hits})
    return [
        f"Rotate the {provider} credential now, then remove it from the branch."
        for provider in providers
    ]
