"""Secret detection on added lines, including the rule that values are masked."""
from __future__ import annotations

from scripts.insights.changes import Line, parse_changes
from scripts.insights.secrets import (
    evaluate,
    is_env_file,
    looks_like_placeholder,
    mask,
    scan_line,
    shannon_entropy,
)

# A syntactically valid AWS key id that is not a real credential. Used only to
# prove the detector fires; nothing here is live.
FAKE_AWS_ID = "AKIA" + "Q7RJ4KLMNOPQRSTU"
FAKE_GITHUB = "ghp_" + "aZ9bY8cX7dW6eV5fU4gT3hS2iR1jQ0kP9lO8"
FAKE_HIGH_ENTROPY = "Kj8Hn2Qp5Zx9Vb3Lm7Rt4Wy6Ac1Df0Gh"


def _line(text, path="app/config.py", number=10):
    return Line(path=path, line=number, text=text)


def _diff(path, added_lines):
    body = "".join(f"+{line}\n" for line in added_lines)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,0 +1,{len(added_lines)} @@\n{body}"
    )


# -- masking, which is the non-negotiable part -----------------------------

def test_mask_never_reveals_more_than_two_characters():
    masked = mask(FAKE_HIGH_ENTROPY)
    assert masked.startswith(FAKE_HIGH_ENTROPY[:2])
    assert FAKE_HIGH_ENTROPY[2:] not in masked
    assert str(len(FAKE_HIGH_ENTROPY)) in masked


def test_a_short_value_is_masked_completely():
    assert mask("abc123") == "******"
    assert mask("") == ""


def test_the_secret_value_never_appears_anywhere_in_the_result():
    changes = parse_changes(_diff("app/config.py", [f'API_KEY = "{FAKE_HIGH_ENTROPY}"']))
    result = evaluate(changes)
    serialised = repr(result)
    assert FAKE_HIGH_ENTROPY not in serialised
    assert result["hits"], "the detector should have fired"


# -- provider detectors ----------------------------------------------------

def test_an_aws_access_key_id_is_critical():
    hits = scan_line(_line(f'aws_key = "{FAKE_AWS_ID}"'))
    assert [h["rule"] for h in hits][:1] == ["aws-access-key-id"]
    assert hits[0]["severity"] == "critical"


def test_a_github_token_is_detected():
    hits = scan_line(_line(f"token: {FAKE_GITHUB}"))
    assert any(h["rule"] == "github-token" for h in hits)


def test_a_private_key_block_is_detected():
    hits = scan_line(_line("-----BEGIN RSA PRIVATE KEY-----"))
    assert any(h["rule"] == "private-key-block" for h in hits)


def test_a_password_in_a_connection_string_is_detected():
    hits = scan_line(_line("DATABASE_URL = 'postgres://user:Sup3rS3cret9x@db:5432/app'"))
    rules = {h["rule"] for h in hits}
    assert rules & {"connection-string-password", "basic-auth-url"}


def test_a_jwt_is_detected():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
    hits = scan_line(_line(f'jwt = "{token}"'))
    assert any(h["rule"] == "jwt" for h in hits)


def test_a_generic_assignment_needs_entropy():
    # Low entropy: this is a placeholder, not a credential.
    assert scan_line(_line('password = "aaaaaaaaaaaa"')) == []
    assert scan_line(_line(f'password = "{FAKE_HIGH_ENTROPY}"'))


# -- false positives -------------------------------------------------------

def test_placeholders_are_recognised():
    for value in ("changeme", "your-api-key-here", "REPLACE_ME", "xxxxxxxxxx",
                  "${API_KEY}", "os.environ['KEY']", "dummy-secret-value"):
        assert looks_like_placeholder(value), value


def test_an_environment_variable_reference_is_not_a_secret():
    assert scan_line(_line('api_key = os.environ["API_KEY"]')) == []
    assert scan_line(_line('secret = process.env.SECRET')) == []
    assert scan_line(_line('password = "${DB_PASSWORD}"')) == []


def test_a_commented_out_credential_is_lower_severity_but_still_reported():
    hits = scan_line(_line(f'# password = "{FAKE_HIGH_ENTROPY}"'))
    assert hits
    assert hits[0]["severity"] == "medium"
    assert hits[0]["commented_out"] is True


def test_example_files_are_allowlisted():
    changes = parse_changes(_diff(".env.example", [f'API_KEY={FAKE_HIGH_ENTROPY}']))
    result = evaluate(changes)
    assert result["hits"] == []
    assert ".env.example" in result["allowlisted_paths"]


def test_a_private_key_in_an_example_file_is_still_reported():
    # An allowlist exempts credential-shaped strings, never an actual key block.
    changes = parse_changes(
        _diff("keys/example.pem", ["-----BEGIN PRIVATE KEY-----"])
    )
    result = evaluate(changes)
    assert [h["rule"] for h in result["hits"]] == ["private-key-block"]


def test_base64_encoded_prose_is_not_treated_as_a_secret():
    # "this is just some ordinary configuration text" encoded.
    encoded = "dGhpcyBpcyBqdXN0IHNvbWUgb3JkaW5hcnkgY29uZmlndXJhdGlvbiB0ZXh0"
    assert scan_line(_line(f'secret = "{encoded}"')) == []


# -- env files -------------------------------------------------------------

def test_env_files_are_recognised():
    assert is_env_file(".env")
    assert is_env_file("config/.env.production")
    assert is_env_file("deploy/staging.env")
    assert not is_env_file("app/environment.py")


def test_the_env_detector_only_applies_to_env_files():
    text = f"DB_PASSWORD={FAKE_HIGH_ENTROPY}"
    assert any(h["rule"] == "env-file-assignment" for h in scan_line(_line(text, path=".env")))
    assert not any(
        h["rule"] == "env-file-assignment"
        for h in scan_line(_line(text, path="docs/notes.py"))
    )


# -- the envelope ----------------------------------------------------------

def test_a_clean_diff_passes():
    changes = parse_changes(_diff("app/main.py", ["def handler():", "    return 1"]))
    result = evaluate(changes)
    assert result["status"] == "pass"
    assert result["scanned_lines"] == 2


def test_a_detected_credential_fails_by_default():
    changes = parse_changes(_diff("app/config.py", [f'API_KEY = "{FAKE_HIGH_ENTROPY}"']))
    result = evaluate(changes)
    assert result["status"] == "fail"
    assert result["counts"]["critical"] or result["counts"]["high"]


def test_failing_can_be_switched_off():
    changes = parse_changes(_diff("app/config.py", [f'API_KEY = "{FAKE_HIGH_ENTROPY}"']))
    result = evaluate(changes, {"secret_scan": {"fail_on": ""}})
    assert result["status"] == "warn"


def test_scanning_can_be_disabled():
    changes = parse_changes(_diff("app/config.py", [f'API_KEY = "{FAKE_HIGH_ENTROPY}"']))
    result = evaluate(changes, {"secret_scan": {"enabled": False}})
    assert result["status"] == "not_run"
    assert result["hits"] == []


def test_a_truncated_diff_is_never_reported_as_a_clean_pass():
    changes = parse_changes(_diff("app/main.py", ["clean = 1"]))
    result = evaluate(changes, diff_truncated=True)
    assert result["status"] == "warn"
    assert "truncated" in result["summary"]


def test_deleted_files_are_not_scanned():
    diff = (
        "diff --git a/old.py b/old.py\ndeleted file mode 100644\n"
        f"--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-API_KEY = \"{FAKE_HIGH_ENTROPY}\"\n"
    )
    result = evaluate(parse_changes(diff))
    assert result["hits"] == []


def test_the_finding_advises_rotation_and_says_history_is_not_cleaned():
    changes = parse_changes(_diff("app/config.py", [f'API_KEY = "{FAKE_HIGH_ENTROPY}"']))
    detail = evaluate(changes)["findings"][0]["detail"]
    assert "rotate" in detail.lower()
    assert "history" in detail.lower()


def test_findings_are_capped():
    lines = [f'secret = "{FAKE_HIGH_ENTROPY}{n}"' for n in range(120)]
    result = evaluate(parse_changes(_diff("app/c.py", lines)),
                      {"secret_scan": {"max_findings": 10}})
    assert len(result["findings"]) == 10
    assert result["dropped"] >= 100


def test_entropy_maths():
    assert shannon_entropy("") == 0.0
    assert shannon_entropy("aaaa") == 0.0
    assert shannon_entropy(FAKE_HIGH_ENTROPY) > 3.0
