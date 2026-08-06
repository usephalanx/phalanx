"""Grounding attribution for the FetchSandbox analysis surface.

Phalanx NEVER interprets `spec` or the grounding text — the brain is
FetchSandbox's, and a runtime that understands what "paddle" means is a runtime
that has to ship in lockstep with the oracle (see
docs/fs-phalanx-proof-contract-v1.md §0).

What Phalanx DOES owe the caller is an honest, runtime-observed record of which
grounding actually reached the subprocess. Without it, a grounded run and an
ungrounded run are indistinguishable in the response, so no A/B measurement can
attribute a result to a grounding without trusting the caller's own bookkeeping.

That is the same principle as the shadow ledger's provenance (which task row did
this field come from) and FetchSandbox's judge fetching the gate verdict
independently rather than believing the agent's self-report: the measurement
comes from the thing that ran, not from the thing that asked.

Metadata only. The prompt text is never returned — it can carry user code
context — so callers get a stable fingerprint instead.
"""

from __future__ import annotations

import hashlib

# Fingerprint length. 16 hex chars = 64 bits: collision-free at any volume this
# surface will ever see, short enough to eyeball in a results table.
_SHA_CHARS = 16


def describe(
    prompt_used: str,
    *,
    spec: str | None = None,
    fields: dict[str, object] | None = None,
) -> dict:
    """Describe the grounding that produced `prompt_used`.

    `fields` maps a grounding-source name (e.g. "fix_pattern", "prompt") to the
    caller-supplied value; a source counts as present only when it is a
    non-empty string. `spec` is an opaque label echoed straight back.

    Returns, for example:
        {"spec": "paddle", "grounded": True,
         "grounding_fields": ["fix_pattern"],
         "prompt_sha256": "9f2c...", "prompt_chars": 1840}

    Never raises — attribution must not be able to break a run.
    """
    try:
        present = sorted(
            name
            for name, value in (fields or {}).items()
            if isinstance(value, str) and value.strip()
        )
        text = prompt_used if isinstance(prompt_used, str) else ""
        return {
            "spec": spec if isinstance(spec, str) and spec.strip() else None,
            "grounded": bool(present),
            "grounding_fields": present,
            "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:_SHA_CHARS],
            "prompt_chars": len(text),
        }
    except Exception:  # noqa: BLE001 — attribution is metadata, never a failure mode
        return {
            "spec": None,
            "grounded": False,
            "grounding_fields": [],
            "prompt_sha256": None,
            "prompt_chars": 0,
        }
