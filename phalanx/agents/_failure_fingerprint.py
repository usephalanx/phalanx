"""v1.7.2.3 — Failure fingerprint for no-progress loop detection.

Commander uses this to detect "engineer applied a patch but verify still
fails the SAME way" — a strong signal that the next iteration won't help.
Cuts off pointless replan loops before they hit the cost cap.

Fingerprint inputs:
  - verify_command (what was run)
  - exit_code
  - normalized stdout_tail + stderr_tail (truncated tool output)

Normalization strips noise that would change the hash without changing
the semantic failure (timestamps, /tmp paths, line numbers in tracebacks,
hex pointers, durations). After normalization, two iterations producing
"the same" failure produce the same hash.

NOT cryptographic — just a deduper. SHA-256 truncated to 16 chars is
plenty for the no-progress signal.
"""

from __future__ import annotations

import hashlib
import re

# Each pattern replaced with a stable token. Keep these minimal — over-
# normalization would collapse genuinely-different failures into the same
# hash and cause us to terminate runs that were actually progressing.
_NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Hex pointers / addresses: 0xdeadbeef → __HEX__
    (re.compile(r"0x[0-9a-fA-F]{4,}"), "__HEX__"),
    # Tempdir paths: /tmp/abc, /var/folders/..., /private/var/...
    (re.compile(r"(?:/private)?/(?:tmp|var)/[A-Za-z0-9_./-]+"), "__TMPPATH__"),
    # Forge run-scoped paths: /tmp/forge-repos/v3-<uuid>-<role>/...
    (re.compile(r"v3-[0-9a-f-]{8,}-[a-z]+"), "__RUNDIR__"),
    # Container IDs: 12-char hex (or longer when emitted unabridged)
    (re.compile(r"\b[0-9a-f]{12,}\b"), "__CID__"),
    # Pytest line numbers in tracebacks: "test_foo.py:42:" → "test_foo.py:__LN__:"
    (re.compile(r"(\.py):(\d+):"), r"\1:__LN__:"),
    # Floating durations: "1.234s", "0.05 seconds"
    (re.compile(r"\d+\.\d+\s*s(?:econds)?"), "__DUR__"),
    # ISO timestamps: 2026-05-03T05:21:26
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"), "__TS__"),
    # Memory addresses in repr: <object at 0x...>
    (re.compile(r"<[\w.]+ object at __HEX__>"), "<obj>"),
)


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    out = text
    for pat, repl in _NORMALIZERS:
        out = pat.sub(repl, out)
    # Collapse runs of whitespace to a single space — different shells
    # may emit different padding for the same error.
    out = re.sub(r"\s+", " ", out).strip()
    return out


def compute_fingerprint(
    *,
    cmd: str,
    exit_code: int,
    stdout_tail: str | None,
    stderr_tail: str | None,
) -> str:
    """Stable 16-char hash for de-duping verify failures.

    Same semantic failure (same command, same exit code, same normalized
    output) → same fingerprint, regardless of timing/paths/pointers.
    """
    norm_stdout = _normalize(stdout_tail)
    norm_stderr = _normalize(stderr_tail)
    payload = f"{cmd.strip()}|{exit_code}|{norm_stdout}|{norm_stderr}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def is_repeated(fingerprints: list[str]) -> bool:
    """True iff the LAST fingerprint matches the previous one.

    Two consecutive identical fingerprints = no progress = stop iterating.
    Three-in-a-row would be even stronger evidence but two is the cheaper
    signal and matches what the commander gate enforces.
    """
    return len(fingerprints) >= 2 and fingerprints[-1] == fingerprints[-2]
