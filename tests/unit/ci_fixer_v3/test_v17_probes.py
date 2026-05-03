"""Tier-1 unit tests for v1.7 Tier 1 deterministic probes.

Validates:
  - extract_error_tokens picks distinctive identifiers, drops stopwords
  - git_log_search finds prior commits via -S<token> pickaxe
  - env_drift_probe finds recent infra commits
  - run_pre_tl_probes orchestrates both, renders for TL
  - c11 environmental-control: flags TL diagnoses missing env-cause when drift exists
  - c12 isolation-test: flags single-test diagnoses missing pollution acknowledgement

All tests use real git in a tempdir — no Postgres, no LLM, no Celery.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from phalanx.agents._tl_self_critique import (
    check_c11_environmental_control,
    check_c12_isolation_test_advisable,
)
from phalanx.agents._v17_probes import (
    GitCommitHit,
    env_drift_probe,
    extract_error_tokens,
    git_log_search,
    run_pre_tl_probes,
)


# ─── Token extraction ────────────────────────────────────────────────────────


class TestExtractErrorTokens:
    def test_pulls_distinctive_module_name(self):
        text = "ModuleNotFoundError: No module named 'httpx'"
        tokens = extract_error_tokens(text)
        assert "httpx" in tokens

    def test_drops_common_stopwords(self):
        text = "ImportError: cannot import name 'foo' AssertionError"
        tokens = extract_error_tokens(text)
        # ImportError / AssertionError are stopwords; they should not appear
        for stop in ("ImportError", "AssertionError", "Error", "import"):
            assert stop not in tokens, f"{stop!r} should be stopworded; got {tokens}"

    def test_picks_function_or_test_names(self):
        text = "tests/test_time.py:142: AssertionError in test_naturaldate_tz_aware"
        tokens = extract_error_tokens(text)
        assert "naturaldate" in tokens or "test_naturaldate_tz_aware" in tokens

    def test_empty_returns_empty(self):
        assert extract_error_tokens("") == []
        assert extract_error_tokens("   ") == []


# ─── Git probes (real git in tempdir) ────────────────────────────────────────


@pytest.fixture
def repo_with_history():
    """Create a tempdir git repo with a few commits — one of them touches
    `httpx` so git log -S<httpx> will find it."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        # init git
        subprocess.run(["git", "init", "--quiet"], cwd=str(ws), check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@local"], cwd=str(ws), check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "test"], cwd=str(ws), check=True
        )

        # commit 1: baseline (no httpx)
        (ws / "src.py").write_text("def hello():\n    return 'world'\n")
        subprocess.run(["git", "add", "-A"], cwd=str(ws), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial"], cwd=str(ws), check=True
        )

        # commit 2: add httpx import (this is what -S<httpx> should find)
        (ws / "src.py").write_text(
            "import httpx\n\n\ndef hello():\n    return httpx.get('https://x').text\n"
        )
        subprocess.run(["git", "add", "-A"], cwd=str(ws), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "use httpx instead of requests"],
            cwd=str(ws), check=True,
        )

        # commit 3: infra change — recent .github/ commit
        (ws / ".github").mkdir(exist_ok=True)
        (ws / ".github" / "workflows").mkdir(exist_ok=True)
        (ws / ".github" / "workflows" / "test.yml").write_text(
            "name: test\non: [push]\njobs:\n  test: {runs-on: ubuntu-latest}\n"
        )
        subprocess.run(["git", "add", "-A"], cwd=str(ws), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add ci workflow"],
            cwd=str(ws), check=True,
        )

        yield ws


class TestGitLogSearch:
    def test_finds_commit_that_added_token(self, repo_with_history):
        hits = git_log_search(token="httpx", workspace=repo_with_history)
        assert len(hits) >= 1
        assert any("httpx" in h.subject for h in hits)
        # Diff excerpt should contain "+import httpx" or similar
        assert any("httpx" in (h.diff_excerpt or "") for h in hits)

    def test_short_token_returns_empty(self, repo_with_history):
        # Tokens < 4 chars are too noisy
        assert git_log_search(token="x", workspace=repo_with_history) == []

    def test_no_match_returns_empty(self, repo_with_history):
        hits = git_log_search(token="nonexistent_token_xyz123", workspace=repo_with_history)
        assert hits == []

    def test_non_git_workspace_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert git_log_search(token="foo", workspace=tmp) == []


class TestEnvDriftProbe:
    def test_finds_github_workflow_commit(self, repo_with_history):
        hits = env_drift_probe(workspace=repo_with_history)
        assert len(hits) >= 1
        assert any(
            ".github/workflows/test.yml" in h.files for h in hits
        )

    def test_no_infra_commits_returns_empty(self):
        """Bare repo with only a non-infra commit produces no env_drift hits."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            subprocess.run(["git", "init", "--quiet"], cwd=str(ws), check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@local"], cwd=str(ws), check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "test"], cwd=str(ws), check=True
            )
            (ws / "code.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "-A"], cwd=str(ws), check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "code only"], cwd=str(ws), check=True
            )
            assert env_drift_probe(workspace=ws) == []


class TestRunPreTlProbes:
    def test_full_run_produces_renderable_block(self, repo_with_history):
        result = run_pre_tl_probes(
            failing_command="python -c 'import httpx'",
            error_line_or_log="ModuleNotFoundError: No module named 'httpx'",
            workspace_path=repo_with_history,
        )
        assert result.git_log_hits, "should find httpx commit"
        assert result.env_drift_hits, "should find .github/ commit"
        assert "httpx" in result.error_tokens_searched
        rendered = result.render_for_tl()
        assert "Git history matches" in rendered
        assert "Recent infra commits" in rendered

    def test_skips_cleanly_with_no_workspace(self):
        result = run_pre_tl_probes(
            failing_command="x", error_line_or_log="y", workspace_path="",
        )
        assert result.git_log_hits == []
        assert result.notes


# ─── c11 environmental-control ───────────────────────────────────────────────


class TestC11EnvironmentalControl:
    def test_passes_when_no_env_drift(self):
        ok, _ = check_c11_environmental_control(
            draft_root_cause="some code bug",
            draft_open_questions=[],
            env_drift_hits=[],
        )
        assert ok

    def test_passes_when_tl_acknowledges_env(self):
        ok, _ = check_c11_environmental_control(
            draft_root_cause="github actions runner image rotated last week, breaking the cython build",
            draft_open_questions=[],
            env_drift_hits=[GitCommitHit(
                sha="abc123def0", date="2025-04-12",
                subject="bump runner image",
                files=[".github/workflows/test.yml"],
                diff_excerpt="",
            )],
        )
        assert ok, "TL referenced infra cause; should pass"

    def test_fails_when_drift_exists_but_tl_blames_code(self):
        ok, reason = check_c11_environmental_control(
            draft_root_cause="apply_discount returns wrong value because the multiplier sign is flipped",
            draft_open_questions=[],
            env_drift_hits=[GitCommitHit(
                sha="abc123def0", date="2025-04-30",
                subject="bump pytest version",
                files=["requirements-dev.txt"],
                diff_excerpt="",
            )],
        )
        assert not ok
        assert "infra commit" in reason


# ─── c12 isolation-test ──────────────────────────────────────────────────────


class TestC12IsolationTest:
    def test_passes_for_multi_test_verify(self):
        ok, _ = check_c12_isolation_test_advisable(
            draft_root_cause="something broke",
            draft_failing_command="python -m pytest tests/",
            draft_open_questions=[],
        )
        assert ok

    def test_passes_when_tl_acknowledges_pollution(self):
        ok, _ = check_c12_isolation_test_advisable(
            draft_root_cause="test_foo's autouse fixture leaks sys.modules state into sibling tests",
            draft_failing_command="pytest tests/test_bar.py::test_baz -xvs",
            draft_open_questions=[],
        )
        assert ok

    def test_fails_when_single_test_and_no_pollution_consideration(self):
        ok, reason = check_c12_isolation_test_advisable(
            draft_root_cause="naturaldate returns wrong value for tz-aware datetime",
            draft_failing_command="pytest tests/test_time.py::test_naturaldate_tz_aware -xvs",
            draft_open_questions=[],
        )
        assert not ok
        assert "isolation" in reason.lower() or "polluter" in reason.lower()

    def test_minus_k_selector_also_treated_as_single_test(self):
        ok, _ = check_c12_isolation_test_advisable(
            draft_root_cause="test failed",
            draft_failing_command="pytest -k test_specific_function tests/",
            draft_open_questions=[],
        )
        assert not ok
