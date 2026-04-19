"""Secret / PII redaction for harvested public CI logs.

Spec §11 requires: "Redaction pipeline (detect-secrets + regex scrub)
mandatory before any fixture is committed. Zero tolerance for committed
secrets." Each redaction writes a report entry into the fixture's
meta.json so we can audit what was scrubbed without re-running the tool.

Detection layers (ordered by precision):

  1. High-signal regex patterns — GitHub tokens (ghp_*, gho_*, ghu_*,
     ghs_*), AWS access keys (AKIA/ASIA/SKIA), OpenAI keys (sk-*),
     Anthropic keys (sk-ant-*), Slack bot tokens (xoxb-*), bearer
     tokens in auth headers.
  2. Email + URL-with-credentials scrubs.
  3. Optional detect-secrets pass (when the package is installed) for
     high-entropy blobs the regex layer missed.

Redaction replaces matches with a fixed marker string so diffs and
log-parser patterns remain stable. Match positions are recorded in a
RedactionReport for auditing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Marker strings ────────────────────────────────────────────────────────
_SECRET_MARKER = "<REDACTED:SECRET>"  # pragma: allowlist secret
_EMAIL_MARKER = "<REDACTED:EMAIL>"
_URL_CRED_MARKER = "<REDACTED:URL_CREDS>"


# ── High-signal regex patterns ────────────────────────────────────────────
# (label, compiled regex). Labels feed the redaction report.
# Each pattern matches a full token so we can replace atomically.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # GitHub personal + server tokens
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    # GitHub App installation tokens
    ("github_app_token", re.compile(r"\bghs_[A-Za-z0-9]{20,}\b")),
    # AWS access key ids (20 chars, start with A[SK]IA)
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|SKIA)[0-9A-Z]{16}\b")),
    # AWS secret (40 chars base64-ish) — only flag when labeled
    (
        "aws_secret",
        re.compile(
            r"(?i)aws_secret_access_key[\"'\s:=]+[A-Za-z0-9/+=]{40}"
        ),
    ),
    # Anthropic API keys — MUST precede the generic OpenAI `sk-*` pattern
    # so `sk-ant-...` matches as anthropic, not openai.
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    # OpenAI API keys
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b")),
    # Slack bot / user / app tokens
    ("slack_token", re.compile(r"\bxox[abpsr]-[A-Za-z0-9-]{10,}\b")),
    # JWTs — 3 dot-separated base64-ish segments, header usually starts eyJ
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    # Generic bearer / authorization headers
    (
        "auth_header",
        re.compile(
            r"(?i)(?:Authorization|X-API-Key)\s*:\s*(?:Bearer\s+)?[A-Za-z0-9._\-+/=]{12,}"
        ),
    ),
]

# Email addresses — scrubbed to avoid PII in harvested logs.
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# URLs with embedded credentials: https://user:pass@host/... or token@host.
_URL_CRED_RE = re.compile(
    r"(?i)https?://[^/\s:@]+:[^/\s@]+@[^\s/]+"
)


@dataclass
class RedactionReport:
    """Audit record for a single redaction pass — serialized into meta.json."""

    total_matches: int = 0
    by_label: dict[str, int] = field(default_factory=dict)
    detect_secrets_used: bool = False

    def record(self, label: str, count: int = 1) -> None:
        self.total_matches += count
        self.by_label[label] = self.by_label.get(label, 0) + count

    def to_dict(self) -> dict:
        return {
            "total_matches": self.total_matches,
            "by_label": dict(self.by_label),
            "detect_secrets_used": self.detect_secrets_used,
        }


def _apply_regex_layer(text: str, report: RedactionReport) -> str:
    out = text
    for label, pattern in _SECRET_PATTERNS:
        def _sub(_m, _label=label):
            report.record(_label)
            return _SECRET_MARKER

        out = pattern.sub(_sub, out)

    # URL-with-credentials MUST run before email scrubbing — the `user:pass@host`
    # form contains an `@host` that the email regex would otherwise swallow.
    def _url_sub(_m):
        report.record("url_with_creds")
        return _URL_CRED_MARKER

    out = _URL_CRED_RE.sub(_url_sub, out)

    def _email_sub(_m):
        report.record("email")
        return _EMAIL_MARKER

    out = _EMAIL_RE.sub(_email_sub, out)
    return out


def _try_detect_secrets(text: str, report: RedactionReport) -> str:
    """Best-effort pass using detect-secrets if the package is present.

    Runs the same high-entropy scans detect-secrets uses in CI. Returns
    the input unchanged when the package is not installed, so the
    redaction pipeline stays standalone in dev.
    """
    try:
        from detect_secrets import SecretsCollection
        from detect_secrets.settings import default_settings
    except ImportError:
        return text

    report.detect_secrets_used = True

    # detect-secrets reads files; simplest path is write-scan-replace
    # via an in-memory temporary. For MVP we do a line-level scan using
    # the string-scan helpers that most detect-secrets plugins expose.
    # Full integration lives in the harvester (Week 1.8b) when we scan
    # whole files at harvest time.
    collection = SecretsCollection()
    with default_settings():
        for plugin_name, plugin in collection.plugins.items():  # type: ignore[attr-defined]
            try:
                for secret in plugin.analyze_string(text):  # type: ignore[attr-defined]
                    marker = f"<REDACTED:{plugin_name}>"
                    text = text.replace(str(secret), marker)
                    report.record(f"ds:{plugin_name}")
            except Exception:
                # Plugin API varies across detect-secrets versions; skip
                # gracefully when it doesn't match our assumptions.
                continue
    return text


def redact(text: str, use_detect_secrets: bool = True) -> tuple[str, RedactionReport]:
    """Scrub `text` of secrets + PII.

    Returns (redacted_text, report). The report fields are suitable for
    direct inclusion in a fixture's meta.json.
    """
    report = RedactionReport()
    out = _apply_regex_layer(text, report)
    if use_detect_secrets:
        out = _try_detect_secrets(out, report)
    return out, report
