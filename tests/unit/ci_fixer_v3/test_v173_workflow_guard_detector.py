"""v1.7.3 post-Phase-2a — workflow guard-command detector tests.

Phase 2a entries E2 (psf/black) and E5 (sphinx-doc/sphinx) hit
sandbox_provisioning_failed because the SRE setup ran workflow `run:`
steps that are CI-only guards — `if [ "$GITHUB_BASE_REF" != "main" ];
then exit 1; fi` and similar. These should NEVER execute in our
sandbox.

Tests cover:
  - Real shapes from psf/black + sphinx-doc/sphinx that triggered E2/E5
  - Synthetic guards (GITHUB_HEAD_REF, GHA annotation echo, etc.)
  - Defensive false-positive checks: install scripts that legitimately
    use exit codes or env vars must NOT be flagged
  - End-to-end: workflow with a guard step → recipe.commands skips it,
    recipe.skipped_guard_commands captures it
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import pytest

from phalanx.agents._v171_workflow_extractor import (
    _has_exit_equivalent,
    _is_ci_check_command,
    _is_workflow_guard_command,
    extract_recipe_from_workflow,
)


# ── Real-world guard shapes from Phase 2a ─────────────────────────────


class TestRealWorldGuardShapes:
    def test_psf_black_pr_target_check(self):
        """psf/black lint workflow's first step — fails E2 in Phase 2a."""
        cmd = (
            'if [ "$GITHUB_BASE_REF" != "main" ]; then\n'
            '    echo "::error::PR targeting \'$GITHUB_BASE_REF\', '
            'please refile targeting \'main\'." && exit 1\n'
            'fi'
        )
        assert _is_workflow_guard_command(cmd) is True

    def test_sphinx_event_name_gate(self):
        """Sphinx-style: gate on event name (synthetic but plausible
        shape — sphinx had a different failure that hit an `exit 1`
        path; this proves the same detector covers GITHUB_EVENT_NAME
        gates too)."""
        cmd = (
            'if [ "$GITHUB_EVENT_NAME" = "schedule" ]; then\n'
            '    echo "Skipping scheduled run on this branch"\n'
            '    exit 0\n'
            'fi'
        )
        assert _is_workflow_guard_command(cmd) is True

    def test_inverse_pr_target_one_liner(self):
        """Same idea, one-line || form."""
        cmd = '[ "$GITHUB_BASE_REF" = "main" ] || exit 1'
        assert _is_workflow_guard_command(cmd) is True

    def test_head_ref_regex_gate(self):
        """Bot-only branch enforcement — common in dependabot setups."""
        cmd = '[[ "$GITHUB_HEAD_REF" =~ ^bot/.* ]] || exit 1'
        assert _is_workflow_guard_command(cmd) is True

    def test_gha_annotation_with_exit(self):
        """GHA `::error::` workflow command + exit — unambiguously a
        runner-only gate even when no GHA env var is referenced
        directly."""
        cmd = 'echo "::error::Wrong base branch"; exit 1'
        assert _is_workflow_guard_command(cmd) is True

    def test_gha_warning_annotation_with_exit(self):
        """`::warning::` annotation also indicates GHA-runner-only logic."""
        cmd = 'echo "::warning::Suspicious file"; exit 2'
        assert _is_workflow_guard_command(cmd) is True


# ── False-positive defenses ───────────────────────────────────────────


class TestFalsePositiveDefenses:
    def test_pip_install_is_not_a_guard(self):
        cmd = 'pip install -e ".[tests]"'
        assert _is_workflow_guard_command(cmd) is False

    def test_pytest_invocation_is_not_a_guard(self):
        cmd = "pytest -v tests/"
        assert _is_workflow_guard_command(cmd) is False

    def test_install_script_with_exit_but_no_gha_var_is_not_a_guard(self):
        """Real installers sometimes early-exit (pip install || exit 1).
        Without a GHA-only env var, this stays in install_commands."""
        cmd = "pip install -r requirements.txt || exit 1"
        assert _is_workflow_guard_command(cmd) is False

    def test_command_referencing_github_workspace_is_not_a_guard(self):
        """GITHUB_WORKSPACE is set in the sandbox — using it doesn't
        make the command a guard. Also: no exit."""
        cmd = "ls $GITHUB_WORKSPACE"
        assert _is_workflow_guard_command(cmd) is False

    def test_github_token_alone_is_not_a_guard(self):
        """We deliberately don't flag GITHUB_TOKEN — it's commonly used
        in legitimate install commands (private repo fetches, GH releases)."""
        cmd = (
            "curl -sSL -H 'Authorization: token $GITHUB_TOKEN' "
            "https://example.com/install.sh | sh"
        )
        assert _is_workflow_guard_command(cmd) is False

    def test_exit_zero_in_install_path_is_not_a_guard(self):
        """`|| exit 0` patterns in install scripts (e.g., 'cleanup
        cache; rm -rf x || exit 0') aren't guards — no GHA env var."""
        cmd = "rm -rf .pytest_cache || exit 0"
        assert _is_workflow_guard_command(cmd) is False

    def test_empty_command_is_not_a_guard(self):
        assert _is_workflow_guard_command("") is False
        assert _is_workflow_guard_command(None) is False  # type: ignore[arg-type]


# ── End-to-end: extract_recipe_from_workflow handles guards ───────────


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / ".github" / "workflows").mkdir(parents=True)
        yield ws


def _write_workflow(ws: Path, name: str, content: str) -> Path:
    path = ws / ".github" / "workflows" / name
    path.write_text(textwrap.dedent(content))
    return path


class TestExtractRecipeWithGuards:
    def test_psf_black_style_workflow_skips_guard(self, workspace):
        """End-to-end: workflow with a PR-target guard step + a real
        install step. Guard goes to skipped_guard_commands; install
        stays in commands. The recipe is still usable (commands non-empty)."""
        wf = _write_workflow(
            workspace,
            "lint.yml",
            """\
            name: lint
            on: pull_request
            jobs:
              lint:
                name: lint
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - uses: actions/setup-python@v5
                    with:
                      python-version: '3.12'
                  - name: Check PR target
                    run: |
                      if [ "$GITHUB_BASE_REF" != "main" ]; then
                          echo "::error::Wrong target" && exit 1
                      fi
                  - name: Install
                    run: pip install -e ".[lint]"
            """,
        )
        recipe = extract_recipe_from_workflow(wf, "lint")
        assert recipe is not None
        # The guard is NOT in install commands
        assert not any('GITHUB_BASE_REF' in c for c in recipe.commands)
        # But it IS recorded as evidence
        assert len(recipe.skipped_guard_commands) == 1
        assert 'GITHUB_BASE_REF' in recipe.skipped_guard_commands[0]
        # The real install command survives
        assert any('pip install' in c for c in recipe.commands)

    def test_workflow_with_only_guard_step_produces_empty_install(self, workspace):
        """Edge case: workflow whose only `run:` step is a guard.
        Recipe.commands ends up empty (apart from any uses-handler
        outputs). Caller should be able to detect this and fall to
        Tier 1 if needed."""
        wf = _write_workflow(
            workspace,
            "guard-only.yml",
            """\
            name: guard
            on: pull_request
            jobs:
              guard:
                name: guard
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: '[ "$GITHUB_BASE_REF" = "main" ] || exit 1'
            """,
        )
        recipe = extract_recipe_from_workflow(wf, "guard")
        assert recipe is not None
        assert recipe.commands == []
        assert recipe.skipped_guard_commands == [
            '[ "$GITHUB_BASE_REF" = "main" ] || exit 1'
        ]

    def test_workflow_without_guards_gets_empty_skipped_list(self, workspace):
        """Non-guard workflows produce skipped_guard_commands == []."""
        wf = _write_workflow(
            workspace,
            "test.yml",
            """\
            name: test
            on: pull_request
            jobs:
              test:
                name: test
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: pip install -e .
                  - run: pytest
            """,
        )
        recipe = extract_recipe_from_workflow(wf, "test")
        assert recipe is not None
        assert recipe.skipped_guard_commands == []
        # pytest is also skipped — but as test runner, not guard
        assert any('pip install' in c for c in recipe.commands)
        assert not any('pytest' in c for c in recipe.commands)


# ── ExtractedRecipe shape stability ───────────────────────────────────


class TestRecipeShape:
    def test_recipe_has_skipped_guard_commands_field(self, workspace):
        """The new field defaults to [] for workflows without guards
        — backward-compat with any consumer that introspects the
        recipe dataclass."""
        from phalanx.agents._v171_workflow_extractor import ExtractedRecipe

        recipe = ExtractedRecipe(
            workflow_file=".github/workflows/x.yml",
            job_key="test",
            job_name="test",
            runs_on="ubuntu-latest",
        )
        assert recipe.skipped_guard_commands == []


# ── Defense-in-depth: handler outputs filtered too ────────────────────


class TestHandlerOutputFilter:
    """v1.7.3 post-Phase-2a — every command from a `uses:` handler
    goes through the same test-runner / guard checks as raw run-step
    commands. Catches future handlers that accidentally emit a
    verify-mode command (the pre-commit/action handler did this until
    E2 attempt #2's evidence surfaced it)."""

    def test_urllib3_towncrier_guard_with_false(self, workspace):
        """Phase 2b F1 — urllib3's towncrier guard ends in `false`,
        not `exit 1`. Detector now recognizes false / return N as
        exit-equivalents alongside literal `exit N`."""
        cmd = (
            "if ! pipx run towncrier check --compare-with origin/$GITHUB_BASE_REF; then\n"
            '    echo "Please see https://example.com/guidance"\n'
            "    false\n"
            "fi"
        )
        assert _is_workflow_guard_command(cmd) is True

    def test_pre_commit_action_only_emits_install_during_setup(self, workspace):
        _write_workflow(
            workspace,
            "lint.yml",
            """\
            on: pull_request
            jobs:
              lint:
                name: lint
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - uses: pre-commit/action@v3
            """,
        )
        recipe = extract_recipe_from_workflow(workspace / ".github/workflows/lint.yml", "lint")
        assert recipe is not None
        # pip install pre-commit is install; pre-commit run is verify
        assert "pip install pre-commit" in recipe.commands
        assert not any("pre-commit run" in c for c in recipe.commands)


# ── NM1: exit-equivalent matchers (false, return N) ──────────────────


class TestExitEquivalents:
    """v1.7.3 post-Phase-2b — `false` and `return N` count as exits
    inside guard contexts. Surfaced by Phase 2b F1 (urllib3): a guard
    that ended in `false` (not `exit 1`) bypassed the detector."""

    def test_has_exit_equivalent_recognizes_exit_n(self):
        assert _has_exit_equivalent("exit 1") is True
        assert _has_exit_equivalent("exit 0") is True
        assert _has_exit_equivalent("exit 137") is True

    def test_has_exit_equivalent_recognizes_false(self):
        assert _has_exit_equivalent("false") is True
        assert _has_exit_equivalent("cmd; false; fi") is True

    def test_has_exit_equivalent_recognizes_return_n(self):
        assert _has_exit_equivalent("return 1") is True
        assert _has_exit_equivalent("return 0") is True

    def test_has_exit_equivalent_word_boundary_on_false(self):
        # `false` substring in identifier shouldn't match — e.g.,
        # Python `False`, `false_positive`, `falsethorn` etc.
        # We rely on \bfalse\b — uppercase F doesn't match.
        assert _has_exit_equivalent("assertEquals(False, x)") is False
        # Lowercase keyword as identifier inside larger word is also
        # word-bounded out — `falsethorn` doesn't match `\bfalse\b`
        # in the boundary sense (no boundary between `false` and `thorn`).
        assert _has_exit_equivalent("falsethorn = 1") is False

    def test_has_exit_equivalent_no_match_in_normal_install(self):
        assert _has_exit_equivalent("pip install -e .") is False
        assert _has_exit_equivalent("python -m pytest") is False

    def test_guard_with_false_inside_gha_var_block_matches(self):
        """The literal F1 shape from urllib3 — should now classify as guard."""
        cmd = (
            "if [ \"$GITHUB_BASE_REF\" != \"main\" ]; then\n"
            "    echo 'wrong target'; false\nfi"
        )
        assert _is_workflow_guard_command(cmd) is True

    def test_guard_with_return_inside_gha_var_block_matches(self):
        cmd = (
            'if [ "$GITHUB_HEAD_REF" = "wip" ]; then return 1; fi'
        )
        assert _is_workflow_guard_command(cmd) is True

    def test_bare_false_without_gha_var_is_not_guard(self):
        """Defensive: install scripts can use `false` legitimately
        in conditional pipelines. Without a GHA-only env var, we
        don't classify."""
        assert _is_workflow_guard_command("cmd_a || false") is False
        assert _is_workflow_guard_command("[ -f x ] && true || false") is False


# ── NM2: ci_check (state-assertion) detector ─────────────────────────


class TestCIChecks:
    """v1.7.3 post-Phase-2b — `git diff --exit-code` and `git status
    --porcelain` patterns are state assertions, not install steps.
    Surfaced by Phase 2b F2 (aio-libs/aiohttp)."""

    def test_aiohttp_git_diff_exit_code(self):
        """Phase 2b F2 — the literal aiohttp shape."""
        cmd = (
            "set -eEuo pipefail\n"
            "make sync-direct-runtime-deps\n"
            "git diff --exit-code -- requirements/runtime-deps.in"
        )
        assert _is_ci_check_command(cmd) is True

    def test_bare_git_diff_exit_code(self):
        assert _is_ci_check_command("git diff --exit-code") is True

    def test_git_diff_exit_code_with_paths(self):
        assert _is_ci_check_command("git diff --exit-code -- src/") is True
        assert _is_ci_check_command("git diff HEAD --exit-code") is True

    def test_no_pager_git_diff_variant(self):
        assert (
            _is_ci_check_command("git --no-pager diff --exit-code")
            is True
        )

    def test_git_status_porcelain_pipe_to_wc(self):
        cmd = "git status --porcelain | wc -l"
        assert _is_ci_check_command(cmd) is True

    def test_git_status_porcelain_pipe_to_test(self):
        cmd = "git status --porcelain | grep -q '^M'"
        assert _is_ci_check_command(cmd) is True

    def test_bash_empty_status_check(self):
        """`[ -z "$(git status --porcelain)" ]` empty-diff idiom."""
        assert (
            _is_ci_check_command(
                '[ -z "$(git status --porcelain)" ] || exit 1'
            )
            is True
        )

    def test_plain_git_diff_is_not_ci_check(self):
        """Without --exit-code, `git diff` is just informational."""
        assert _is_ci_check_command("git diff HEAD~1") is False
        assert _is_ci_check_command("git diff > /tmp/diff.patch") is False

    def test_plain_git_status_is_not_ci_check(self):
        assert _is_ci_check_command("git status") is False

    def test_install_command_not_ci_check(self):
        assert _is_ci_check_command("pip install -e .") is False

    def test_recipe_has_skipped_ci_checks_field(self):
        from phalanx.agents._v171_workflow_extractor import ExtractedRecipe

        recipe = ExtractedRecipe(
            workflow_file="x", job_key="y", job_name="y", runs_on="ubuntu-latest",
        )
        assert recipe.skipped_ci_checks == []

    def test_extract_recipe_skips_ci_check_step(self, workspace):
        """End-to-end: workflow with a make+diff CI-check step. The
        check is pulled out of install_commands and into
        skipped_ci_checks. Real install commands stay."""
        wf = _write_workflow(
            workspace,
            "ci.yml",
            """\
            name: ci
            on: pull_request
            jobs:
              ci:
                name: ci
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
                  - run: pip install -e .
                  - name: Verify deps in sync
                    run: |
                      set -eEuo pipefail
                      make sync-direct-runtime-deps
                      git diff --exit-code -- requirements/runtime-deps.in
            """,
        )
        recipe = extract_recipe_from_workflow(wf, "ci")
        assert recipe is not None
        # Real install present
        assert any("pip install" in c for c in recipe.commands)
        # CI-check pulled out
        assert not any(
            "git diff --exit-code" in c for c in recipe.commands
        )
        assert len(recipe.skipped_ci_checks) == 1
        assert "git diff --exit-code" in recipe.skipped_ci_checks[0]
