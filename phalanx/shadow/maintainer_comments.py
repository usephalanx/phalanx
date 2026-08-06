"""Path B (2026-05-20) — maintainer-facing PR comment delivery for shadow-mode verdicts.

This module produces a single GitHub-visible comment per terminal shadow
verdict, when (and only when) the repo is opted in via
`ci_integrations.maintainer_comments_enabled = true`. Three guarantees:

  1. Shadow-mode safety stays intact for opt-OUT repos. Comments are
     the ONLY side-effect Phalanx ever produces, even when opted in.
     No branches, no PRs, no commits, no labels.
  2. Comments are suppressed when the diagnosis would be uninformative
     to the maintainer (Phalanx-internal failures, synthesized fallbacks).
  3. Idempotent: each ledger_id can produce at most one comment. The
     check runs via a sentinel HTML marker embedded in the comment body.
     Re-runs of the reconciler will not double-post.

Failure handling: the poster NEVER raises. GitHub API failures are
logged as `maintainer_comment.post_failed` and the dispatch completes
normally. The ledger row's durability is what matters; the comment
is best-effort delivery on top.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

# HTML-comment marker embedded in every comment body. List-comments-then-grep
# uses this to detect prior posts for the same ledger row.
_MARKER_PREFIX = "<!-- phalanx-shadow-ledger-id:"
_MARKER_RE = re.compile(r"<!--\s*phalanx-shadow-ledger-id:([0-9a-f-]+)\s*-->")

# Fingerprint check happens against GitHub's comment list. We only need
# the most recent ~60 comments for the PR — Phalanx comments would be
# among the latest, and PRs with >60 comments are vanishingly rare for
# the failure shapes we'd be commenting on.
_COMMENT_LIST_PAGE_SIZE = 60

# Cap the diff snippet we paste into a SHIPPED comment. GitHub renders
# markdown well up to several thousand characters, but maintainers should
# read the actual file in their editor — the comment is a teaser.
_DIFF_MAX_CHARS = 4000


# ── Suppress logic ────────────────────────────────────────────────────────────


def _is_infra_failure_class(failure_class: str | None) -> bool:
    if not failure_class:
        return False
    return failure_class.startswith("FAILED_SANDBOX_SETUP_") or failure_class.startswith("FAILED_INFRA_") or failure_class in {"FAILED_SANDBOX_SETUP", "FAILED_SANDBOX_CLEANUP"}


def should_post(row_dict: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether to post a comment for this terminal row.

    Returns (post, reason). `reason` is a short string explaining the
    decision, useful for log lines and tests.

    Path B polish pass (#1): the gate is `root_cause_synthesized`, not
    `tl_task_status`. A SAFE_ESCALATE with `tl_status=FAILED` because the
    v1.7.2.9 calibration validator rejected a hedged confidence STILL
    has a grounded, maintainer-quality diagnosis (the fix_spec content
    spreads through `success=False` returns too). Only TRULY synthesized
    fallbacks should suppress.
    """
    verdict = row_dict.get("phalanx_verdict")
    pr_number = row_dict.get("pr_number")
    failure_class = row_dict.get("failure_class")
    provenance = row_dict.get("phalanx_provenance") or {}
    tl_status = provenance.get("tl_task_status")
    tl_count = provenance.get("tl_task_count") or 0
    rc_synthesized = bool(provenance.get("root_cause_synthesized"))

    if not pr_number:
        return False, "no_pr_number"

    if verdict == "PENDING":
        return False, "verdict_is_pending"

    if verdict == "SHIPPED_PROPOSED":
        return True, "shipped_proposed"

    # Both SAFE_ESCALATE and FAILED share the same grounding rule:
    # post iff the diagnosis is real (not synthesized) AND TL reached
    # a terminal state (COMPLETED or FAILED). PENDING/CANCELLED/IN_PROGRESS
    # means TL didn't finish — no real diagnosis to share.
    if verdict in ("SAFE_ESCALATE", "FAILED"):
        if verdict == "FAILED" and _is_infra_failure_class(failure_class):
            return False, f"failed_infra_class_{failure_class}"
        if rc_synthesized:
            return False, f"{verdict.lower()}_synthesized_diagnosis"
        if tl_count == 0:
            return False, f"{verdict.lower()}_no_tl_task"
        if tl_status not in ("COMPLETED", "FAILED"):
            return False, f"{verdict.lower()}_tl_incomplete_{tl_status}"
        return True, f"{verdict.lower()}_with_grounded_diagnosis"

    return False, f"unknown_verdict_{verdict}"


# ── Renderer ──────────────────────────────────────────────────────────────────


def _marker(ledger_id: str) -> str:
    return f"{_MARKER_PREFIX}{ledger_id} -->"


def _fmt_confidence(conf: float | None) -> str:
    if conf is None:
        return "n/a"
    return f"{int(round(conf * 100))}%"


def _ai_disclosure_banner() -> str:
    """Plain-English AI-authorship disclosure. Renders as a blockquote so it
    visually sits above the verdict headline. Tells the maintainer up-front
    that this comment is AI-generated, not human-written, and that nothing
    was pushed to their repo."""
    return (
        "> 🤖 _This comment is automatically generated by **Phalanx** — an AI "
        "agent that analyzes failing CI runs in shadow mode. The analysis "
        "below is AI-generated; review it before applying._"
    )


def _disable_footer() -> str:
    """Plain-English shadow-mode footer. Names the operator + the off-switch."""
    # Local import to avoid a circular import at module load.
    from phalanx.config.settings import get_settings  # noqa: PLC0415
    s = get_settings()
    operator = (s.phalanx_operator_handle or "").strip()
    operator_mention = f"@{operator}" if operator else "the operator who enrolled it"
    about_url = (s.phalanx_about_url or "").strip()
    parts = [
        f"_Phalanx ran in shadow mode. Nothing in this repo was modified "
        f"beyond this comment. To disable Phalanx on this repo, @-mention "
        f"{operator_mention} on this PR or revoke the PAT in your GitHub "
        f"settings._",
    ]
    if about_url:
        parts.append(f"_About Phalanx → {about_url}_")
    return "\n\n".join(parts)


def _exit_label(exit_code: Any) -> str:
    """Render an exit code as a maintainer-readable pass/fail cell."""
    if exit_code == 0:
        return "✅ exit 0 (passed)"
    if isinstance(exit_code, int):
        return f"❌ exit {exit_code} (failed)"
    return "_(not recorded)_"


def _verification_evidence_block(provenance: dict[str, Any]) -> str:
    """Render the before/after verification evidence, or "" if unavailable.

    Pure function of the provenance dict (schema v3 `verification_evidence`).
    Shows the maintainer the exact failing command, the relevant error line,
    and the same check failing before / passing after Phalanx's change — run
    in a clean sandbox. This is what turns "trust the AI" into "here are the
    receipts."
    """
    ev = provenance.get("verification_evidence")
    if not isinstance(ev, dict):
        return ""

    failing_command = (ev.get("failing_command") or "").strip()
    error_line = (ev.get("error_line") or "").strip()
    before = ev.get("before") if isinstance(ev.get("before"), dict) else None
    after = ev.get("after") if isinstance(ev.get("after"), dict) else None

    if not any([failing_command, error_line, before, after]):
        return ""

    lines: list[str] = ["<details><summary><b>Verification evidence</b></summary>", ""]

    if failing_command:
        lines.append(f"**Failing CI check:** `{failing_command}`")
    if error_line:
        lines.append(f"> {error_line}")
        lines.append("")

    if before or after:
        lines.append(
            "Phalanx reproduced the failure in a clean sandbox, applied the "
            "change, and re-ran the same check:"
        )
        lines.append("")
        lines.append("| Step | Command | Result |")
        lines.append("| ---- | ------- | ------ |")
        if before:
            cmd = before.get("cmd") or "_(not recorded)_"
            lines.append(f"| Before fix | `{cmd}` | {_exit_label(before.get('exit_code'))} |")
        if after:
            cmd = after.get("cmd") or "_(not recorded)_"
            lines.append(f"| After fix | `{cmd}` | {_exit_label(after.get('exit_code'))} |")
        lines.append("")

    if before and (before.get("output_tail") or "").strip():
        lines.append("<details><summary>before-fix output</summary>")
        lines.append("")
        lines.append("```")
        lines.append(before["output_tail"].strip())
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("</details>")
    return "\n".join(lines)


def _shipped_body(row: dict[str, Any], ledger_id: str) -> str:
    """SHIPPED_PROPOSED — show the diff, the affected files, the confidence."""
    rc = (row.get("phalanx_root_cause") or "").strip() or "Phalanx produced a verified fix."
    conf = _fmt_confidence(row.get("phalanx_confidence"))
    files = row.get("phalanx_affected_files") or []
    files_str = ", ".join(f"`{f}`" for f in files[:5]) or "_(not recorded)_"
    patch = (row.get("phalanx_proposed_patch") or "").strip()
    if len(patch) > _DIFF_MAX_CHARS:
        patch = patch[:_DIFF_MAX_CHARS] + "\n[... diff truncated; review the patch in full before applying ...]"
    provenance = row.get("phalanx_provenance") or {}
    evidence_block = _verification_evidence_block(provenance)
    evidence_section = f"\n{evidence_block}\n" if evidence_block else ""
    return f"""{_marker(ledger_id)}
{_ai_disclosure_banner()}

🛠 **Phalanx — Proposed fix (shadow mode, read-only)**

{rc}

<details open><summary><b>Proposed change</b></summary>

```diff
{patch}
```

**Affected files:** {files_str}
**Confidence:** {conf}
**What was examined:** the failing CI job, the PR diff, and the full CI log.

</details>
{evidence_section}
{_disable_footer()}
"""


def _safe_escalate_body(row: dict[str, Any], ledger_id: str) -> str:
    """SAFE_ESCALATE with grounded TL — present the diagnosis + explain refusal."""
    rc = (row.get("phalanx_root_cause") or "").strip()
    conf = _fmt_confidence(row.get("phalanx_confidence"))
    threshold_explainer = (
        f"Phalanx's confidence ({conf}) was below the threshold required to "
        "propose a fix, so it escalated for human review instead of guessing."
    )
    return f"""{_marker(ledger_id)}
{_ai_disclosure_banner()}

🔍 **Phalanx — Did not propose a fix (shadow mode, read-only)**

{rc or '_(diagnosis not recorded)_'}

<details><summary><b>Why escalated</b></summary>

{threshold_explainer}

**What was examined:** the failing CI job, the PR diff, and the CI log.
**Outcome:** No code change was generated. This PR is unchanged.

</details>

{_disable_footer()}
"""


def _failed_body(row: dict[str, Any], ledger_id: str) -> str:
    """FAILED with real TL — engineer attempted but couldn't verify a fix."""
    rc = (row.get("phalanx_root_cause") or "").strip()
    return f"""{_marker(ledger_id)}
{_ai_disclosure_banner()}

⚠️ **Phalanx — Could not produce a verifiable fix (shadow mode, read-only)**

{rc or 'Phalanx examined this failure but could not produce a fix it could verify against the failing test.'}

<details><summary><b>Details</b></summary>

A proposed change was attempted but did not pass verification in Phalanx's sandbox. Out of caution, no patch is being suggested.

**What was examined:** the failing CI job, the PR diff, and the CI log.

</details>

{_disable_footer()}
"""


def build_comment_body(row_dict: dict[str, Any]) -> str | None:
    """Render the maintainer-facing comment body. None if suppressed.

    Pure function — no I/O, no env, no clock. Suitable for unit tests.
    """
    post, _reason = should_post(row_dict)
    if not post:
        return None
    ledger_id = str(row_dict.get("id") or "")
    if not ledger_id:
        return None
    verdict = row_dict.get("phalanx_verdict")
    if verdict == "SHIPPED_PROPOSED":
        return _shipped_body(row_dict, ledger_id)
    if verdict == "SAFE_ESCALATE":
        return _safe_escalate_body(row_dict, ledger_id)
    if verdict == "FAILED":
        return _failed_body(row_dict, ledger_id)
    return None


# ── GitHub poster ────────────────────────────────────────────────────────────


async def _list_comment_markers(
    client: httpx.AsyncClient, repo: str, pr_number: int, token: str
) -> set[str]:
    """Pull recent comments for the PR and extract phalanx markers."""
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params = {"per_page": _COMMENT_LIST_PAGE_SIZE}
    r = await client.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    markers: set[str] = set()
    for entry in r.json():
        body = entry.get("body") or ""
        m = _MARKER_RE.search(body)
        if m:
            markers.add(m.group(1))
    return markers


async def _post_comment(
    client: httpx.AsyncClient, repo: str, pr_number: int, token: str, body: str
) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    r = await client.post(url, headers=headers, json={"body": body}, timeout=30)
    r.raise_for_status()
    return r.json()


async def post_maintainer_comment_async(
    *,
    row_dict: dict[str, Any],
    integration_enabled: bool,
    integration_token: str | None,
) -> dict[str, Any] | None:
    """Post a single maintainer-facing comment if all guards pass.

    Returns the GitHub API response on successful post, None on suppress
    or failure. NEVER raises — all exceptions are caught + logged.

    Caller (ledger writer / reconciler) passes the integration's flag +
    token explicitly so this module has no DB dependency.
    """
    ledger_id = str(row_dict.get("id") or "")
    repo = row_dict.get("repo")
    pr_number = row_dict.get("pr_number")
    verdict = row_dict.get("phalanx_verdict")

    if not integration_enabled:
        log.debug(
            "maintainer_comment.suppressed.opt_out",
            ledger_id=ledger_id, repo=repo, reason="integration_disabled",
        )
        return None
    if not integration_token:
        log.warning(
            "maintainer_comment.suppressed.no_token",
            ledger_id=ledger_id, repo=repo,
        )
        return None
    if not (repo and pr_number and ledger_id):
        log.debug(
            "maintainer_comment.suppressed.missing_fields",
            ledger_id=ledger_id, repo=repo, pr_number=pr_number,
        )
        return None

    body = build_comment_body(row_dict)
    if body is None:
        post_flag, reason = should_post(row_dict)
        log.debug(
            "maintainer_comment.suppressed.verdict_rule",
            ledger_id=ledger_id, repo=repo, verdict=verdict, reason=reason,
        )
        return None

    try:
        async with httpx.AsyncClient() as client:
            existing = await _list_comment_markers(
                client, repo, pr_number, integration_token,
            )
            if ledger_id in existing:
                log.info(
                    "maintainer_comment.idempotent_skip",
                    ledger_id=ledger_id, repo=repo, pr_number=pr_number,
                )
                return None
            response = await _post_comment(
                client, repo, pr_number, integration_token, body,
            )
            log.info(
                "maintainer_comment.posted",
                ledger_id=ledger_id, repo=repo, pr_number=pr_number,
                comment_id=response.get("id"),
                verdict=verdict,
            )
            return response
    except Exception as exc:  # noqa: BLE001
        log.error(
            "maintainer_comment.post_failed",
            ledger_id=ledger_id, repo=repo, pr_number=pr_number,
            error=str(exc), error_type=type(exc).__name__,
        )
        return None


def post_maintainer_comment_sync(
    *,
    row_dict: dict[str, Any],
    integration_enabled: bool,
    integration_token: str | None,
) -> dict[str, Any] | None:
    """Sync variant for replay/repair tools. Same suppress + never-raise contract."""
    return asyncio.run(
        post_maintainer_comment_async(
            row_dict=row_dict,
            integration_enabled=integration_enabled,
            integration_token=integration_token,
        )
    )
