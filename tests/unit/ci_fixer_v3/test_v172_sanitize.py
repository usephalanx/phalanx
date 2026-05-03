"""Tier-1 tests for v1.7.2 output sanitization.

Each transform tested in isolation, then composed.
"""

from __future__ import annotations

from phalanx.agents._v172_sanitize import (
    cap_bytes,
    has_secret,
    redact_args,
    sanitize_output,
    scrub_secrets,
    strip_ansi,
)


# ─── strip_ansi ──────────────────────────────────────────────────────────────


class TestStripAnsi:
    def test_strips_color_codes(self):
        s = "\x1b[31mFAIL\x1b[0m: test_x failed"
        assert strip_ansi(s) == "FAIL: test_x failed"

    def test_strips_cursor_movement(self):
        s = "\x1b[2J\x1b[H[progress] 50%"
        assert strip_ansi(s) == "[progress] 50%"

    def test_strips_csi_with_params(self):
        s = "\x1b[1;31;48;5;202mERROR\x1b[0m"
        assert strip_ansi(s) == "ERROR"

    def test_strips_osc_sequences(self):
        # OSC for window title — pytest sometimes emits these
        s = "\x1b]0;test running\x07stdout text"
        assert strip_ansi(s) == "stdout text"

    def test_preserves_text_without_ansi(self):
        s = "plain text\nline 2\n"
        assert strip_ansi(s) == s

    def test_empty_string_returns_empty(self):
        assert strip_ansi("") == ""

    def test_handles_pytest_progress_bar(self):
        # Real pytest output snippet
        s = (
            "\x1b[1m============================= test session starts =============================\x1b[0m\n"
            "tests/test_x.py \x1b[32m.\x1b[0m\x1b[31mF\x1b[0m\x1b[32m.\x1b[0m\n"
        )
        cleaned = strip_ansi(s)
        assert "\x1b" not in cleaned
        assert "============================= test session starts =============================" in cleaned
        assert "tests/test_x.py .F." in cleaned


# ─── cap_bytes ───────────────────────────────────────────────────────────────


class TestCapBytes:
    def test_under_cap_returns_unchanged(self):
        s = "small string"
        assert cap_bytes(s, max_bytes=1024) == s

    def test_over_cap_truncates_with_marker(self):
        s = "x" * 100_000
        capped = cap_bytes(s, max_bytes=1024)
        assert len(capped.encode()) <= 1500  # cap + marker overhead
        assert "[...truncated" in capped
        # Marker mentions both byte count and KB-equivalent
        assert "elided" in capped
        assert "original" in capped

    def test_keeps_head_and_tail(self):
        # Head should contain the first chars, tail the last
        s = "HEAD_MARKER" + "filler" * 10000 + "TAIL_MARKER"
        capped = cap_bytes(s, max_bytes=2048)
        assert "HEAD_MARKER" in capped
        assert "TAIL_MARKER" in capped
        assert "[...truncated" in capped

    def test_empty_returns_empty(self):
        assert cap_bytes("") == ""


# ─── scrub_secrets ───────────────────────────────────────────────────────────


class TestScrubSecrets:
    def test_redacts_github_pat(self):
        s = "Bearer ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
        out = scrub_secrets(s)
        assert "ghp_" not in out
        assert "<REDACTED:github_pat>" in out

    def test_redacts_anthropic_key(self):
        s = "ANTHROPIC_API_KEY=sk-ant-api03-aBcDeFgHi_jKlMnOpQrStUvWxYz0123456789-abc"
        out = scrub_secrets(s)
        assert "sk-ant-" not in out
        assert "<REDACTED:anthropic_api_key>" in out

    def test_redacts_openai_proj_key(self):
        s = "OPENAI_API_KEY=sk-proj-aBcDeFgHi_jKlMnOpQrStUvWxYz0123456789ABCD-extra-blob"
        out = scrub_secrets(s)
        assert "sk-proj-" not in out

    def test_redacts_aws_access_key(self):
        s = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        out = scrub_secrets(s)
        assert "AKIA" not in out
        assert "<REDACTED:aws_access_key_id>" in out

    def test_redacts_jwt(self):
        # Real-shape JWT (header.payload.signature)
        s = (
            "Authorization: Bearer "
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        out = scrub_secrets(s)
        assert "<REDACTED:jwt>" in out

    def test_redacts_multiple_secrets_in_one_string(self):
        s = "GH=ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789 AWS=AKIAIOSFODNN7EXAMPLE"
        out = scrub_secrets(s)
        assert "<REDACTED:github_pat>" in out
        assert "<REDACTED:aws_access_key_id>" in out

    def test_preserves_non_secret_strings(self):
        s = "regular log line; pip install -e .; exit 0"
        assert scrub_secrets(s) == s

    def test_does_not_redact_innocent_ghp_substring(self):
        # "ghp" without _ + 36 chars after isn't a token shape
        s = "the ghp prefix is short"
        assert scrub_secrets(s) == s


# ─── has_secret ──────────────────────────────────────────────────────────────


class TestHasSecret:
    def test_true_for_obvious_secret(self):
        assert has_secret("ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789")

    def test_false_for_clean_string(self):
        assert not has_secret("normal log output, no secrets")

    def test_false_for_empty(self):
        assert not has_secret("")


# ─── sanitize_output (composed pipeline) ─────────────────────────────────────


class TestSanitizeOutput:
    def test_combines_all_three_transforms(self):
        # ANSI + secret + huge size
        secret = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
        s = (
            "\x1b[31m"
            + ("filler" * 20000)
            + f" leaked={secret} "
            + ("filler" * 20000)
            + "\x1b[0m"
        )
        cleaned = sanitize_output(s, max_bytes=8192)
        # No ANSI
        assert "\x1b" not in cleaned
        # No raw secret (either redacted or in elided portion)
        assert secret not in cleaned
        # Capped
        assert len(cleaned.encode()) <= 9000

    def test_short_clean_output_unchanged(self):
        s = "all good"
        assert sanitize_output(s) == s


# ─── redact_args ─────────────────────────────────────────────────────────────


class TestRedactArgs:
    def test_redacts_secret_in_dict_value(self):
        args = {"command": "echo hi", "token": "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"}
        out = redact_args(args)
        assert out["command"] == "echo hi"
        assert out["token"] == "<REDACTED:github_pat>"

    def test_redacts_secret_in_nested_dict(self):
        args = {"env": {"API_KEY": "sk-ant-api03-aBcDeFgHi_jKlMnOpQrStUvWxYz0123456789-abc"}}
        out = redact_args(args)
        assert out["env"]["API_KEY"] == "<REDACTED:anthropic_api_key>"

    def test_redacts_secret_in_list_value(self):
        args = {"command_argv": ["curl", "-H", "Authorization: token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"]}
        out = redact_args(args)
        assert "ghp_" not in str(out)

    def test_redacts_whole_value_even_for_partial_match(self):
        # If a value contains a secret embedded in a longer string,
        # we redact the WHOLE value (paranoid). Surrounding context
        # might give away the rest of the secret.
        args = {"text": "exec env shows: GITHUB_TOKEN=ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789 in env"}
        out = redact_args(args)
        # The whole value gets replaced (because partial match)
        assert out["text"].startswith("<REDACTED:")

    def test_preserves_clean_args(self):
        args = {"command": "pytest tests/", "expect_exit": 0}
        assert redact_args(args) == args

    def test_does_not_mutate_original(self):
        args = {"token": "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"}
        original_token = args["token"]
        out = redact_args(args)
        # Original args still has the real token; only the returned copy is scrubbed
        assert args["token"] == original_token
        assert out["token"] != original_token

    def test_handles_non_string_leaves(self):
        args = {"count": 5, "enabled": True, "values": [1, 2, 3], "name": None}
        assert redact_args(args) == args
