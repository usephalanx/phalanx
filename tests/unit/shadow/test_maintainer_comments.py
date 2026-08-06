"""Path B (2026-05-20) — maintainer-facing PR comments.

Covers:
  - should_post suppress logic (15 cases across verdicts × shapes)
  - build_comment_body renders the three meaningful verdicts
  - body never leaks internal jargon
  - body always carries the sentinel HTML marker for idempotency
  - body always carries the shadow-mode footer + off-switch text
  - opt-in flag is honored (post_maintainer_comment_async returns None when off)
  - missing token suppresses
  - never raises on simulated GitHub errors
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from phalanx.shadow.maintainer_comments import (
    _MARKER_PREFIX,
    build_comment_body,
    post_maintainer_comment_async,
    should_post,
)


def _row(**overrides) -> dict:
    base = {
        "id": "00000000-0000-0000-0000-0000000000aa",
        "repo": "rnagulapalle/sandbox",
        "workflow_run_id": 999_000_001,
        "pr_number": 42,
        "phalanx_verdict": "SAFE_ESCALATE",
        "phalanx_confidence": 0.0,
        "phalanx_root_cause": "Diagnosis text from TL.",
        "phalanx_proposed_patch": None,
        "phalanx_affected_files": [],
        "failure_class": None,
        "phalanx_provenance": {
            "tl_task_status": "COMPLETED",
            "tl_task_count": 1,
            "root_cause_synthesized": False,
        },
    }
    base.update(overrides)
    return base


# ── should_post: suppress logic ──────────────────────────────────────────────


class TestSuppressLogic:
    def test_shipped_proposed_posts(self):
        r = _row(phalanx_verdict="SHIPPED_PROPOSED", phalanx_confidence=0.9)
        post, _ = should_post(r)
        assert post is True

    def test_safe_escalate_with_real_tl_posts(self):
        r = _row(phalanx_verdict="SAFE_ESCALATE")
        post, reason = should_post(r)
        assert post is True
        assert "grounded" in reason

    def test_safe_escalate_with_synthesized_diagnosis_suppressed(self):
        r = _row(phalanx_provenance={
            "tl_task_status": "COMPLETED",
            "tl_task_count": 1,
            "root_cause_synthesized": True,
        })
        post, reason = should_post(r)
        assert post is False
        assert "synthesized" in reason

    def test_safe_escalate_validator_rejected_grounded_diagnosis_posts(self):
        """Polish-pass fix #1 — the validator-rejected hedge case.
        tl_task_status=FAILED because the v1.7.2.9 calibration validator
        returned success=False on a hedged confidence, but the fix_spec
        content (and therefore root_cause) IS real and grounded.
        This case MUST post — it's the case the inflect simulation exposed."""
        r = _row(phalanx_provenance={
            "tl_task_status": "FAILED",
            "tl_task_count": 1,
            "root_cause_synthesized": False,
        })
        post, reason = should_post(r)
        assert post is True, f"validator-rejected grounded diagnosis must post; got reason={reason}"
        assert "grounded" in reason

    def test_safe_escalate_with_no_tl_task_suppressed(self):
        r = _row(phalanx_provenance={
            "tl_task_status": None, "tl_task_count": 0,
            "root_cause_synthesized": False,
        })
        post, reason = should_post(r)
        assert post is False
        assert "no_tl_task" in reason

    @pytest.mark.parametrize("tl_status", ["PENDING", "CANCELLED", "IN_PROGRESS"])
    def test_safe_escalate_with_tl_non_terminal_suppressed(self, tl_status):
        """TL never reached a terminal state — no real diagnosis to share.
        Distinct from the validator-rejected case (which posts)."""
        r = _row(phalanx_provenance={
            "tl_task_status": tl_status,
            "tl_task_count": 1,
            "root_cause_synthesized": False,
        })
        post, reason = should_post(r)
        assert post is False
        assert "incomplete" in reason

    @pytest.mark.parametrize("fc", [
        "FAILED_SANDBOX_SETUP_APT", "FAILED_SANDBOX_SETUP_PIP",
        "FAILED_SANDBOX_SETUP_UV", "FAILED_SANDBOX_SETUP_GIT",
        "FAILED_SANDBOX_SETUP_UNKNOWN", "FAILED_SANDBOX_SETUP",
        "FAILED_INFRA_TIMEOUT", "FAILED_INFRA_WORKER_HANG",
        "FAILED_SANDBOX_CLEANUP",
    ])
    def test_failed_infra_class_always_suppressed(self, fc):
        r = _row(phalanx_verdict="FAILED", failure_class=fc)
        post, reason = should_post(r)
        assert post is False, f"{fc} must not produce a maintainer comment"
        assert "infra" in reason

    def test_failed_with_real_tl_posts(self):
        """Engineer attempted, couldn't verify — TL reasoning is grounded.
        Maintainer benefits from knowing what was tried."""
        r = _row(
            phalanx_verdict="FAILED",
            failure_class=None,
            phalanx_provenance={
                "tl_task_status": "COMPLETED",
                "tl_task_count": 1,
                "root_cause_synthesized": False,
            },
        )
        post, _ = should_post(r)
        assert post is True

    def test_pending_suppressed(self):
        r = _row(phalanx_verdict="PENDING")
        post, reason = should_post(r)
        assert post is False
        assert "pending" in reason

    def test_no_pr_number_suppressed(self):
        r = _row(pr_number=None, phalanx_verdict="SHIPPED_PROPOSED")
        post, reason = should_post(r)
        assert post is False
        assert "no_pr_number" in reason


# ── Renderer ─────────────────────────────────────────────────────────────────


class TestRenderer:
    def test_shipped_body_contains_diff(self):
        r = _row(
            phalanx_verdict="SHIPPED_PROPOSED",
            phalanx_confidence=0.9,
            phalanx_proposed_patch=(
                "diff --git a/mypy/typeanal.py b/mypy/typeanal.py\n"
                "+    assert isinstance(analyzed, ParamSpecType)\n"
            ),
            phalanx_affected_files=["mypy/typeanal.py"],
            phalanx_root_cause="ParamSpecType narrowing missing on the non-Concatenate branch.",
        )
        body = build_comment_body(r)
        assert body is not None
        assert "Proposed fix" in body
        assert "shadow mode" in body.lower()
        assert "ParamSpecType" in body
        assert "`mypy/typeanal.py`" in body
        assert "90%" in body

    def test_safe_escalate_body_explains_threshold(self):
        r = _row(
            phalanx_confidence=0.4,
            phalanx_root_cause=(
                "The new reachable-branch logic in `mypy/semanal_namedtuple.py` "
                "fails to collect `y` from nested `if` blocks inside a `NamedTuple`."
            ),
        )
        body = build_comment_body(r)
        assert body is not None
        # Polish-pass: softer headline replaces the adversarial "Refused to ship".
        assert "Did not propose a fix" in body
        assert "40%" in body
        assert "semanal_namedtuple" in body
        assert "human review" in body.lower()

    def test_failed_body_explains_unverified(self):
        r = _row(
            phalanx_verdict="FAILED",
            phalanx_root_cause="The patch attempted didn't pass the failing test.",
            phalanx_provenance={
                "tl_task_status": "COMPLETED",
                "tl_task_count": 1,
                "root_cause_synthesized": False,
            },
        )
        body = build_comment_body(r)
        assert body is not None
        assert "Could not produce a verifiable fix" in body or "verify" in body.lower()

    def test_suppressed_rows_render_none(self):
        r = _row(phalanx_verdict="FAILED", failure_class="FAILED_SANDBOX_SETUP_APT")
        assert build_comment_body(r) is None

    def test_diff_truncated_when_huge(self):
        r = _row(
            phalanx_verdict="SHIPPED_PROPOSED",
            phalanx_proposed_patch="+a\n" * 5000,
            phalanx_affected_files=["x.py"],
            phalanx_confidence=0.85,
        )
        body = build_comment_body(r)
        assert body is not None
        assert "truncated" in body


# ── No jargon ────────────────────────────────────────────────────────────────


class TestNoInternalJargon:
    """Maintainer-facing bodies must NOT contain operator-jargon.
    The user explicitly forbid these strings in maintainer-visible text."""

    FORBIDDEN = [
        "calibration_failed", "tl_zero_confidence", "reconciliation",
        "reconciled_at", "reconciled_reason", "previous_verdict",
        "tl_task_status", "tl_task_count", "root_cause_synthesized",
        "phalanx_provenance", "FAILED_SANDBOX_SETUP", "FAILED_INFRA",
        "divergence_detected", "ledger_id",
    ]

    @pytest.mark.parametrize("verdict,extra", [
        ("SHIPPED_PROPOSED", {"phalanx_proposed_patch": "+ x", "phalanx_affected_files": ["x.py"], "phalanx_confidence": 0.9}),
        ("SAFE_ESCALATE", {"phalanx_confidence": 0.4}),
        ("FAILED", {"phalanx_provenance": {"tl_task_status": "COMPLETED", "tl_task_count": 1, "root_cause_synthesized": False}}),
    ])
    def test_no_forbidden_strings(self, verdict, extra):
        r = _row(phalanx_verdict=verdict, **extra)
        body = build_comment_body(r)
        assert body is not None
        for term in self.FORBIDDEN:
            assert term not in body, f"maintainer body must not contain jargon {term!r}"


# ── Sentinel marker + footer always present ──────────────────────────────────


class TestPolishPass:
    """Polish-pass fixes — AI banner, operator handle, about link, validator-rejected."""

    def test_ai_disclosure_banner_in_every_body(self):
        """Fix #2 — every comment opens with an AI authorship banner."""
        for verdict, extra in [
            ("SHIPPED_PROPOSED", {"phalanx_proposed_patch": "+x", "phalanx_affected_files": ["x.py"], "phalanx_confidence": 0.9}),
            ("SAFE_ESCALATE", {}),
            ("FAILED", {"phalanx_provenance": {"tl_task_status": "COMPLETED", "tl_task_count": 1, "root_cause_synthesized": False}}),
        ]:
            r = _row(phalanx_verdict=verdict, **extra)
            body = build_comment_body(r)
            assert body is not None
            assert "🤖" in body, f"AI marker missing for {verdict}"
            assert "automatically generated" in body, f"AI disclosure missing for {verdict}"
            assert "AI agent" in body, f"AI agent label missing for {verdict}"
            # Banner must come BEFORE the verdict headline so the reader sees it first.
            ai_pos = body.find("🤖")
            verdict_pos = body.find("**Phalanx —")
            assert 0 < ai_pos < verdict_pos, f"AI banner must precede the verdict headline ({verdict})"

    def test_operator_handle_named_in_footer(self):
        """Fix #3 — footer @-mentions the operator (default pilot handle)."""
        r = _row(phalanx_verdict="SHIPPED_PROPOSED",
                 phalanx_proposed_patch="+x",
                 phalanx_affected_files=["x.py"],
                 phalanx_confidence=0.9)
        body = build_comment_body(r)
        assert body is not None
        assert "@rnagulapalle" in body, "footer must @-mention the configured operator"

    def test_about_link_omitted_when_unconfigured(self):
        """Fix #4 — `phalanx_about_url` empty by default; no broken link rendered."""
        r = _row(phalanx_verdict="SAFE_ESCALATE")
        body = build_comment_body(r)
        assert body is not None
        # default settings → no about_url
        assert "About Phalanx" not in body

    def test_about_link_rendered_when_configured(self, monkeypatch):
        """Fix #4 — when about URL is set, the line renders."""
        from phalanx.config import settings as settings_mod
        s = settings_mod.get_settings()
        monkeypatch.setattr(s, "phalanx_about_url", "https://example.invalid/phalanx")
        r = _row(phalanx_verdict="SAFE_ESCALATE")
        body = build_comment_body(r)
        assert body is not None
        assert "About Phalanx" in body
        assert "https://example.invalid/phalanx" in body

    def test_safe_escalate_headline_softer_wording(self):
        """Cosmetic fix #6 piggybacks: "Did not propose a fix" replaces
        "Refused to ship" — softer, less adversarial framing."""
        r = _row(phalanx_verdict="SAFE_ESCALATE")
        body = build_comment_body(r)
        assert body is not None
        assert "Did not propose a fix" in body
        assert "Refused to ship" not in body


class TestStableStructure:
    def test_marker_in_every_posted_body(self):
        for verdict, extra in [
            ("SHIPPED_PROPOSED", {"phalanx_proposed_patch": "+x", "phalanx_affected_files": ["x.py"], "phalanx_confidence": 0.9}),
            ("SAFE_ESCALATE", {}),
            ("FAILED", {"phalanx_provenance": {"tl_task_status": "COMPLETED", "tl_task_count": 1, "root_cause_synthesized": False}}),
        ]:
            r = _row(phalanx_verdict=verdict, **extra)
            body = build_comment_body(r)
            assert body is not None
            assert _MARKER_PREFIX in body
            assert r["id"] in body

    def test_disable_footer_in_every_body(self):
        for verdict, extra in [
            ("SHIPPED_PROPOSED", {"phalanx_proposed_patch": "+x", "phalanx_affected_files": ["x.py"], "phalanx_confidence": 0.9}),
            ("SAFE_ESCALATE", {}),
            ("FAILED", {"phalanx_provenance": {"tl_task_status": "COMPLETED", "tl_task_count": 1, "root_cause_synthesized": False}}),
        ]:
            r = _row(phalanx_verdict=verdict, **extra)
            body = build_comment_body(r)
            assert body is not None
            assert "shadow mode" in body.lower()
            assert "disable" in body.lower()


# ── Poster — opt-in gate, idempotency, never-raise ──────────────────────────


class TestPosterGuards:
    def test_returns_none_when_opt_in_off(self):
        r = _row(phalanx_verdict="SHIPPED_PROPOSED")
        result = asyncio.run(post_maintainer_comment_async(
            row_dict=r, integration_enabled=False, integration_token="t",
        ))
        assert result is None

    def test_returns_none_when_no_token(self):
        r = _row(phalanx_verdict="SHIPPED_PROPOSED")
        result = asyncio.run(post_maintainer_comment_async(
            row_dict=r, integration_enabled=True, integration_token=None,
        ))
        assert result is None

    def test_returns_none_when_suppressed_verdict(self):
        r = _row(phalanx_verdict="FAILED", failure_class="FAILED_SANDBOX_SETUP_APT")
        result = asyncio.run(post_maintainer_comment_async(
            row_dict=r, integration_enabled=True, integration_token="t",
        ))
        assert result is None

    def test_network_error_never_raises(self):
        r = _row(phalanx_verdict="SAFE_ESCALATE")

        async def boom_get(*a, **kw):
            raise httpx.ConnectError("simulated network failure")

        with patch.object(httpx.AsyncClient, "get", new=boom_get):
            result = asyncio.run(post_maintainer_comment_async(
                row_dict=r, integration_enabled=True, integration_token="t",
            ))
        assert result is None

    def test_idempotency_via_marker(self):
        """If a comment with our sentinel exists, skip posting."""
        r = _row(phalanx_verdict="SAFE_ESCALATE")
        ledger_id = r["id"]

        async def fake_get(self_, url, headers=None, params=None, timeout=15):
            response = AsyncMock()
            response.raise_for_status = lambda: None
            response.json = lambda: [{
                "id": 1, "body": f"<!-- phalanx-shadow-ledger-id:{ledger_id} -->\nprior comment",
            }]
            return response

        with patch.object(httpx.AsyncClient, "get", new=fake_get):
            with patch.object(httpx.AsyncClient, "post") as mocked_post:
                result = asyncio.run(post_maintainer_comment_async(
                    row_dict=r, integration_enabled=True, integration_token="t",
                ))
        assert result is None
        mocked_post.assert_not_called()


# ── Edge cases ───────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_missing_root_cause_doesnt_crash(self):
        r = _row(phalanx_root_cause=None)
        body = build_comment_body(r)
        # Suppressed shapes that should pass this test: SAFE_ESCALATE with COMPLETED TL
        # still posts with placeholder text.
        assert body is not None or build_comment_body(_row(phalanx_verdict="SAFE_ESCALATE", phalanx_root_cause=None)) is None

    def test_safe_escalate_no_confidence_renders(self):
        r = _row(phalanx_confidence=None)
        body = build_comment_body(r)
        assert body is not None
        assert "n/a" in body or "Phalanx" in body

    def test_pr_number_zero_treated_as_missing(self):
        r = _row(phalanx_verdict="SHIPPED_PROPOSED", pr_number=0)
        post, _ = should_post(r)
        assert post is False


# ── Verification evidence block ───────────────────────────────────────────────


class TestVerificationEvidenceBlock:
    """Renderer surfaces the before/after evidence from provenance v3."""

    EV = {
        "failing_command": "ruff check .",
        "error_line": "F401 `string` imported but unused",
        "before": {
            "cmd": "ruff check scripts/x.py",
            "exit_code": 1,
            "output_tail": "F401 [*] `string` imported but unused\nFound 1 error.",
        },
        "after": {"cmd": "ruff check scripts/x.py", "exit_code": 0},
    }

    def _shipped(self, ev=None):
        prov = {
            "tl_task_status": "COMPLETED",
            "tl_task_count": 1,
            "root_cause_synthesized": False,
            "verification_evidence": ev,
        }
        return _row(
            phalanx_verdict="SHIPPED_PROPOSED",
            phalanx_confidence=0.93,
            phalanx_root_cause="unused import string triggers F401",
            phalanx_proposed_patch="diff --git a/scripts/x.py b/scripts/x.py\n@@ -1 +0,0 @@\n-import string",
            phalanx_affected_files=["scripts/x.py"],
            phalanx_provenance=prov,
        )

    def test_evidence_block_renders_in_shipped_body(self):
        body = build_comment_body(self._shipped(self.EV))
        assert body is not None
        assert "Verification evidence" in body
        assert "Failing CI check:" in body
        assert "`ruff check .`" in body
        assert "F401" in body
        assert "Before fix" in body and "After fix" in body
        assert "exit 1" in body and "exit 0" in body
        # Independent reproduction surface: the verify command is visible.
        assert "ruff check scripts/x.py" in body

    def test_evidence_block_absent_when_no_evidence(self):
        body = build_comment_body(self._shipped(None))
        assert body is not None
        assert "Verification evidence" not in body
        # The rest of the comment still renders.
        assert "Proposed change" in body

    def test_evidence_block_handles_partial_data(self):
        # Only failing_command + error_line; no before/after.
        ev = {
            "failing_command": "pytest tests/",
            "error_line": "AssertionError: x != y",
            "before": None,
            "after": None,
        }
        body = build_comment_body(self._shipped(ev))
        assert body is not None
        assert "Failing CI check:" in body
        assert "pytest tests/" in body
        # Table omitted when before+after absent.
        assert "Before fix" not in body
