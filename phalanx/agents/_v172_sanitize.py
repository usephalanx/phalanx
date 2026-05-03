"""v1.7.2 sandbox output sanitization.

Three transforms applied at the `_exec_in_container` boundary BEFORE
captured stdout/stderr reaches the LLM context:

  1. ANSI strip — terminal escape codes corrupt the LLM's parser and
     fill its context window (real Anthropic claude-code issue #26373:
     pytest with color, mypy progress bars, etc.)

  2. Byte-cap with explicit truncation marker — bound the size of any
     single tool output. The truncation marker tells the LLM what
     happened so it doesn't hallucinate completeness.

  3. Secret-token regex scrub — known-shape tokens (GitHub PAT, Anthropic
     API key, AWS access key, JWT, Slack, etc.) replaced with
     `<REDACTED:type>`. Defends against `printenv` / `env | grep` /
     accidental log emission of credentials.

Each transform is independent; combined `sanitize_output()` runs all
three in order: ANSI strip → secret scrub → byte-cap. Order matters:
ANSI removal first (smaller bytes), scrub second (so secrets in colored
output get caught), byte-cap last (final size guarantee).

Pure functions. No I/O. Used by tools.py wrappers + log emission.
"""

from __future__ import annotations

import re

# ─── 1. ANSI escape stripper ─────────────────────────────────────────────────
#
# Covers CSI sequences (\x1b[...) which is what 99% of terminal output uses
# (color, cursor movement, screen clear). Less common: OSC sequences
# (\x1b]...) for window titles + hyperlinks. We strip both.

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)")
_ANSI_OTHER_RE = re.compile(r"\x1b[@-Z\\-_]")  # Fe sequences (no params)


def strip_ansi(s: str) -> str:
    """Remove terminal escape sequences. Preserves text content."""
    if not s:
        return s
    s = _ANSI_CSI_RE.sub("", s)
    s = _ANSI_OSC_RE.sub("", s)
    s = _ANSI_OTHER_RE.sub("", s)
    return s


# ─── 2. Byte-cap with truncation marker ──────────────────────────────────────


_DEFAULT_BYTE_CAP = 65_536  # 64 KB per stream
_TRUNC_HEAD_BYTES = 16_384  # keep first 16K
_TRUNC_TAIL_BYTES = 49_152  # keep last 48K (failures usually tail)


def cap_bytes(s: str, max_bytes: int = _DEFAULT_BYTE_CAP) -> str:
    """Cap output at `max_bytes`. If exceeded, keep head+tail with an
    explicit `[...truncated...]` marker between them. Tail-heavy split:
    most CI failures put the useful info at the end.
    """
    if not s:
        return s
    encoded = s.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return s
    # Split: head + truncation notice + tail
    head_bytes = min(_TRUNC_HEAD_BYTES, max_bytes // 4)
    tail_bytes = min(_TRUNC_TAIL_BYTES, max_bytes - head_bytes - 200)  # 200B for marker
    head = encoded[:head_bytes].decode("utf-8", errors="replace")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="replace")
    elided = len(encoded) - head_bytes - tail_bytes
    marker = (
        f"\n\n[...truncated {elided} bytes ({elided // 1024}KB elided)"
        f"; original {len(encoded)} bytes...]\n\n"
    )
    return head + marker + tail


# ─── 3. Secret-token regex scrub ─────────────────────────────────────────────
#
# Each tuple: (kind_label, regex). Order matters only for log readability;
# all patterns are scanned. Patterns are tight: prefix + length so we
# don't redact innocent strings that happen to start with the same chars.

_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("github_install_token", re.compile(r"\bghs_[A-Za-z0-9]{36}\b")),
    ("github_oauth_token", re.compile(r"\bgho_[A-Za-z0-9]{36}\b")),
    ("github_user_token", re.compile(r"\bghu_[A-Za-z0-9]{36}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{30,}\b")),
    ("openai_api_key", re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}\b")),
    ("openai_legacy_key", re.compile(r"\bsk-[A-Za-z0-9]{40,}\b")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # JWTs — three base64url segments separated by dots, ≥20 char header
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    # Slack tokens
    ("slack_token", re.compile(r"\bxox[abprsv]-[A-Za-z0-9-]{10,}\b")),
    # Generic high-entropy hex (32+ chars; coarse, last resort)
    # NOT included — too many false positives. Add per-incident.
]


def scrub_secrets(s: str) -> str:
    """Replace known-shape secret tokens with `<REDACTED:type>`."""
    if not s:
        return s
    for kind, pattern in _SECRET_PATTERNS:
        s = pattern.sub(f"<REDACTED:{kind}>", s)
    return s


def has_secret(s: str) -> bool:
    """True iff any secret pattern matches. Used for fast-path checks
    in audit code that only needs to know "is there a leak here?"
    """
    if not s:
        return False
    return any(p.search(s) for _, p in _SECRET_PATTERNS)


# ─── Composed sanitizer ──────────────────────────────────────────────────────


def sanitize_output(s: str, *, max_bytes: int = _DEFAULT_BYTE_CAP) -> str:
    """Apply all three transforms in order: strip ANSI → scrub secrets
    → cap bytes. The composed pipeline is what tool-result wrappers
    should call.
    """
    if not s:
        return s
    s = strip_ansi(s)
    s = scrub_secrets(s)
    s = cap_bytes(s, max_bytes=max_bytes)
    return s


# ─── Tool-args redaction (used by tool log writers) ──────────────────────────


def redact_args(args: dict) -> dict:
    """Return a SCRUBBED COPY of an args dict suitable for logging.

    For every str leaf, if any secret pattern matches the value, replace
    the WHOLE value with `<REDACTED:type>` (paranoid — even a partial
    match means the surrounding context might leak the rest of the
    secret). Recurses into nested dicts and lists.

    Original args dict is unchanged; tools still see full fidelity.
    """
    return _redact_value(args)


def _redact_value(v):
    if isinstance(v, str):
        for kind, pattern in _SECRET_PATTERNS:
            if pattern.search(v):
                return f"<REDACTED:{kind}>"
        return v
    if isinstance(v, dict):
        return {k: _redact_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_redact_value(x) for x in v]
    if isinstance(v, tuple):
        return tuple(_redact_value(x) for x in v)
    return v


__all__ = [
    "strip_ansi",
    "cap_bytes",
    "scrub_secrets",
    "has_secret",
    "sanitize_output",
    "redact_args",
]
