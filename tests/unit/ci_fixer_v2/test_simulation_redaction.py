"""Unit tests for simulation.redaction — secret/PII scrubbing."""

from __future__ import annotations

from phalanx.ci_fixer_v2.simulation.redaction import redact


def test_redacts_github_personal_token():
    text = "Running with token ghp_abcdef1234567890abcdef1234567890"
    out, report = redact(text, use_detect_secrets=False)
    assert "ghp_" not in out
    assert "<REDACTED:SECRET>" in out
    assert report.by_label.get("github_token") == 1


def test_redacts_openai_key():
    text = "OPENAI_API_KEY=sk-abcdef1234567890ABCDEF12345"  # pragma: allowlist secret
    out, report = redact(text, use_detect_secrets=False)
    assert "sk-abcdef" not in out
    assert report.by_label.get("openai_key", 0) >= 1


def test_redacts_anthropic_key():
    text = "ANTHROPIC_API_KEY=sk-ant-abcdef1234567890ABCDEF"  # pragma: allowlist secret
    out, report = redact(text, use_detect_secrets=False)
    assert "sk-ant-" not in out
    assert report.by_label.get("anthropic_key", 0) >= 1


def test_redacts_aws_access_key():
    text = "aws key: AKIAIOSFODNN7EXAMPLE (don't commit)"
    out, report = redact(text, use_detect_secrets=False)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert report.by_label.get("aws_access_key") == 1


def test_redacts_slack_bot_token():
    text = "slack bot: xoxb-12345-67890-abcdefghij"
    out, report = redact(text, use_detect_secrets=False)
    assert "xoxb-" not in out
    assert report.by_label.get("slack_token", 0) >= 1


def test_redacts_jwt():
    text = (
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    out, report = redact(text, use_detect_secrets=False)
    # Match will hit jwt AND/OR auth_header; both fine.
    assert "eyJhbGciOiJI" not in out
    assert report.total_matches >= 1


def test_redacts_authorization_header():
    text = "Authorization: Bearer a1b2c3d4e5f6g7h8i9j0"
    out, report = redact(text, use_detect_secrets=False)
    assert "a1b2c3d4" not in out
    assert report.by_label.get("auth_header", 0) >= 1


def test_redacts_email_addresses():
    text = "Author: alice@example.com and bob.j@enterprise.co.uk"
    out, report = redact(text, use_detect_secrets=False)
    assert "alice@example.com" not in out
    assert "bob.j@" not in out
    assert "<REDACTED:EMAIL>" in out
    assert report.by_label.get("email") == 2


def test_redacts_url_with_credentials():
    text = "Cloning https://foo:supersecret@github.com/acme/repo.git"
    out, report = redact(text, use_detect_secrets=False)
    assert "foo:supersecret" not in out
    assert "<REDACTED:URL_CREDS>" in out
    assert report.by_label.get("url_with_creds") == 1


def test_plain_log_unchanged():
    text = "FAILED tests/test_foo.py::test_bar\nAssertionError: expected 1 got 2\n"
    out, report = redact(text, use_detect_secrets=False)
    assert out == text
    assert report.total_matches == 0


def test_multiple_secrets_in_one_pass():
    text = (
        "ghp_" + "a" * 36 + "\n"
        "sk-ant-" + "b" * 24 + "\n"  # pragma: allowlist secret
        "alice@example.com\n"
        "https://x:y@host/\n"
    )
    out, report = redact(text, use_detect_secrets=False)
    assert report.total_matches >= 4
    assert "alice@example.com" not in out


def test_report_to_dict_shape():
    text = "ghp_" + "x" * 36
    _, report = redact(text, use_detect_secrets=False)
    d = report.to_dict()
    assert set(d.keys()) == {"total_matches", "by_label", "detect_secrets_used"}
    assert d["total_matches"] >= 1


def test_detect_secrets_gracefully_skipped_when_missing(monkeypatch):
    # Simulate detect-secrets import failure; redact() should still work.
    import builtins

    real_import = builtins.__import__

    def bad_import(name, *a, **k):
        if name == "detect_secrets" or name.startswith("detect_secrets."):
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", bad_import)

    text = "ghp_" + "a" * 36
    out, report = redact(text, use_detect_secrets=True)
    assert "<REDACTED:SECRET>" in out
    # Report reflects that the DS pass was not run.
    assert report.detect_secrets_used is False
