"""Grounding attribution — the seam that makes brain-wiring measurable.

The claim under test: after a run, the response alone tells you whether the
brain's grounding actually reached the analysis subprocess. Without that, a
grounded run and an ungrounded run are indistinguishable and no A/B comparison
can attribute a result to a grounding.

These tests drive the REAL task functions (find_bugs_task / fix_bug_task) with
the claude subprocess stubbed, so they cover the wiring, not just the helper.
"""

from __future__ import annotations

import base64
import io
import tarfile

import pytest

from phalanx.ci_fixer_v3 import find_bugs_task as fb
from phalanx.ci_fixer_v3 import fix_bug_task as fx
from phalanx.ci_fixer_v3.grounding import describe

PADDLE_FIX_PATTERN = (
    "Persist the Paddle event_id and reject a replay before any side effect: "
    "INSERT ... ON CONFLICT DO NOTHING, then fulfil only when the insert won."
)


def _tar_b64(files: dict[str, str]) -> str:
    """Minimal in-memory .tar.gz so _materialize has something real to unpack."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


# ── the helper ───────────────────────────────────────────────────────────────


class TestDescribe:
    def test_ungrounded_run_is_marked_ungrounded(self):
        meta = describe("generic audit prompt", spec=None, fields={"prompt": None})
        assert meta["grounded"] is False
        assert meta["grounding_fields"] == []
        assert meta["spec"] is None

    def test_fix_pattern_counts_as_grounding(self):
        meta = describe(
            "prompt with pattern", spec="paddle",
            fields={"fix_pattern": PADDLE_FIX_PATTERN, "prompt": None},
        )
        assert meta["grounded"] is True
        assert meta["grounding_fields"] == ["fix_pattern"]
        assert meta["spec"] == "paddle"

    def test_multiple_sources_are_all_reported_sorted(self):
        meta = describe(
            "p", spec="stripe",
            fields={"prompt": "override", "fix_pattern": PADDLE_FIX_PATTERN},
        )
        assert meta["grounding_fields"] == ["fix_pattern", "prompt"]

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
    def test_whitespace_only_grounding_does_not_count(self, blank):
        # A caller sending an empty fix_pattern must not be able to inflate the
        # grounded-run count — that would corrupt the A/B comparison silently.
        meta = describe("p", fields={"fix_pattern": blank})
        assert meta["grounded"] is False

    def test_non_string_grounding_does_not_count(self):
        meta = describe("p", fields={"fix_pattern": {"not": "a string"}})
        assert meta["grounded"] is False

    def test_same_prompt_same_fingerprint_different_prompt_differs(self):
        a = describe("identical prompt text")["prompt_sha256"]
        b = describe("identical prompt text")["prompt_sha256"]
        c = describe("identical prompt text.")["prompt_sha256"]
        assert a == b
        assert a != c
        assert len(a) == 16

    def test_prompt_text_never_leaks_into_the_metadata(self):
        secret = "CUSTOMER SOURCE: api_key = sk_live_do_not_export"
        meta = describe(secret, spec="paddle", fields={"prompt": secret})
        blob = repr(meta)
        assert "sk_live_do_not_export" not in blob
        assert "CUSTOMER SOURCE" not in blob
        assert meta["prompt_chars"] == len(secret)

    def test_never_raises_on_garbage(self):
        meta = describe(None, spec=123, fields=None)  # type: ignore[arg-type]
        assert meta["grounded"] is False
        assert meta["spec"] is None


# ── find_bugs_task wiring ────────────────────────────────────────────────────


class TestFindBugsAttribution:
    def test_default_prompt_run_reports_ungrounded(self, monkeypatch):
        monkeypatch.setattr(
            fb, "_run_claude",
            lambda ws, prompt, t: {"available": True, "bugs": "1. bug", "account": "max1", "error": None},
        )
        out = fb.find_bugs_task(workspace_tar_b64=_tar_b64({"app.py": "x=1"}))
        assert out["grounding"]["grounded"] is False
        assert out["grounding"]["spec"] is None

    def test_brain_grounded_run_is_attributable(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            fb, "_run_claude",
            lambda ws, prompt, t: seen.update(prompt=prompt) or {
                "available": True, "bugs": "1. duplicate fulfilment", "account": "max1", "error": None},
        )
        brain_prompt = "Audit this Paddle integration. Ranked failure classes: ..."
        out = fb.find_bugs_task(
            workspace_tar_b64=_tar_b64({"app.py": "x=1"}),
            prompt=brain_prompt, spec="paddle",
        )
        # The grounded prompt actually reached the subprocess...
        assert seen["prompt"] == brain_prompt
        # ...and the response says so, without echoing the prompt itself.
        assert out["grounding"] == {
            "spec": "paddle",
            "grounded": True,
            "grounding_fields": ["prompt"],
            "prompt_sha256": describe(brain_prompt)["prompt_sha256"],
            "prompt_chars": len(brain_prompt),
        }

    def test_grounded_and_ungrounded_runs_are_distinguishable(self, monkeypatch):
        monkeypatch.setattr(
            fb, "_run_claude",
            lambda ws, prompt, t: {"available": True, "bugs": "b", "account": "max1", "error": None},
        )
        tar = _tar_b64({"app.py": "x=1"})
        plain = fb.find_bugs_task(workspace_tar_b64=tar)
        brained = fb.find_bugs_task(workspace_tar_b64=tar, prompt="paddle-aware", spec="paddle")
        # This is the whole point: the judge can separate the two populations.
        assert plain["grounding"]["prompt_sha256"] != brained["grounding"]["prompt_sha256"]
        assert (plain["grounding"]["grounded"], brained["grounding"]["grounded"]) == (False, True)

    def test_attribution_survives_a_failed_run(self, monkeypatch):
        # A grounded run that fails and an ungrounded run that fails are
        # different data points; losing attribution on the error path would
        # bias the comparison toward whichever arm happens to fail more.
        out = fb.find_bugs_task(workspace_tar_b64="!!!not-valid-base64!!!", spec="paddle")
        assert out["available"] is False
        assert out["grounding"]["spec"] == "paddle"


# ── fix_bug_task wiring ──────────────────────────────────────────────────────


class TestFixBugAttribution:
    def test_fix_pattern_reaches_the_subprocess_and_is_recorded(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            fx, "_run_fix",
            lambda ws, prompt, t: seen.update(prompt=prompt) or {
                "available": True, "diff": "--- a\n+++ b\n", "summary": "s",
                "account": "max1", "error": None},
        )
        out = fx.fix_bug_task(
            workspace_tar_b64=_tar_b64({"app.py": "x=1"}),
            bug="duplicate fulfilment on repeated transaction.completed",
            fix_pattern=PADDLE_FIX_PATTERN,
            spec="paddle",
        )
        # Grounding is genuinely in the prompt the fixer saw...
        assert "ON CONFLICT DO NOTHING" in seen["prompt"]
        assert "KNOWN REMEDIATION" in seen["prompt"]
        # ...and the response can prove which arm this run belongs to.
        assert out["grounding"]["grounded"] is True
        assert out["grounding"]["grounding_fields"] == ["fix_pattern"]
        assert out["grounding"]["spec"] == "paddle"

    def test_without_fix_pattern_the_same_bug_is_ungrounded(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            fx, "_run_fix",
            lambda ws, prompt, t: seen.update(prompt=prompt) or {
                "available": True, "diff": "d", "summary": "s", "account": "max1", "error": None},
        )
        out = fx.fix_bug_task(
            workspace_tar_b64=_tar_b64({"app.py": "x=1"}),
            bug="duplicate fulfilment on repeated transaction.completed",
        )
        assert "KNOWN REMEDIATION" not in seen["prompt"]
        assert out["grounding"]["grounded"] is False
        assert out["grounding"]["grounding_fields"] == []

    def test_attribution_present_on_the_rejected_input_path(self):
        out = fx.fix_bug_task(workspace_tar_b64=_tar_b64({"a.py": "x"}), bug="", spec="paddle")
        assert out["available"] is False
        assert out["grounding"]["spec"] == "paddle"
