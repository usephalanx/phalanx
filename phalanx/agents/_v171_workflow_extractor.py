"""v1.7.1 Tier 0 — workflow YAML extraction.

Most CI failures echo a `.github/workflows/<job>.yml` recipe that is
literally what CI just ran. Instead of asking an LLM to reverse-engineer
the env, we parse the workflow and render a deterministic shell command
list. ~80% of repos have a usable recipe (per docs/v171-provisioning-tiers.md
research).

Returns None when:
  - No workflow files
  - Job name doesn't match
  - Critical step uses an unsupported `uses:` action (custom org actions)
  - YAML parse error
  - Unresolved `${{ matrix.* }}` template literal in a critical path
    (we don't expand matrices in v1.7.1)

When None, caller falls through to Tier 1 (lockfile fingerprint).

Design constraints:
  - Pure parsing, no I/O outside reading YAML files in the workspace
  - No LLM calls
  - No subprocess calls (the rendered commands are EXECUTED by SRE setup,
    not by this module)
  - Stable output: same workflow → same recipe (deterministic ordering)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog
import yaml

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ExtractedRecipe:
    """A list of shell commands rendered from a workflow YAML, plus
    enough metadata for cache-keying and downstream debugging.

    `commands` are executed in order in the SRE setup container.
    `unsupported_steps` lists steps we couldn't render (skipped); if
    non-empty, downstream may decide the recipe is partial.
    """

    workflow_file: str        # repo-relative path (e.g. ".github/workflows/test.yml")
    job_key: str              # job key in `jobs:` map (e.g. "test")
    job_name: str             # job's `name:` field (or job_key if absent)
    runs_on: str              # informational
    commands: list[str] = field(default_factory=list)
    unsupported_steps: list[str] = field(default_factory=list)


# ─── Supported `uses:` action handlers ───────────────────────────────────────
#
# Each handler returns a list of shell commands (or [] to skip the step).
# Returning None means the action isn't supported and the WHOLE recipe
# should be rejected (callers fall to Tier 1). This is stricter than
# returning [] because some unsupported actions are critical setup
# (custom org installers).


def _handle_checkout(action_with: dict) -> list[str]:
    """actions/checkout — workspace already cloned; skip."""
    return []


def _handle_setup_python(action_with: dict) -> list[str]:
    """actions/setup-python — render python install if a specific
    version is requested. Skip if `python-version: '${{ matrix... }}'`
    (we can't expand) — best-effort: assume system python is OK.
    """
    version = action_with.get("python-version")
    if isinstance(version, str) and "${{" not in version:
        # Note: we can't reliably install arbitrary pythons in the sandbox
        # without infra hooks; for v1.7.1 we just record the requirement
        # as a pre-check and let SRE provisioning ensure it's available.
        # A more aggressive impl would invoke pyenv/uv-managed-python.
        return [f"# requires python {version} (provisioner should ensure)"]
    return []


def _handle_setup_uv(action_with: dict) -> list[str]:
    """astral-sh/setup-uv — install uv via the official script."""
    return ["curl -LsSf https://astral.sh/uv/install.sh | sh"]


def _handle_setup_node(action_with: dict) -> list[str]:
    """actions/setup-node — record version requirement, system node OK."""
    version = action_with.get("node-version")
    if isinstance(version, str) and "${{" not in version:
        return [f"# requires node {version}"]
    return []


def _handle_setup_go(action_with: dict) -> list[str]:
    """actions/setup-go — informational."""
    version = action_with.get("go-version")
    if isinstance(version, str) and "${{" not in version:
        return [f"# requires go {version}"]
    return []


def _handle_cache(action_with: dict) -> list[str]:
    """actions/cache — skip; we don't have a cache layer hooked up here."""
    return []


def _handle_upload_artifact(action_with: dict) -> list[str]:
    """actions/upload-artifact — skip; verify is downstream."""
    return []


def _handle_download_artifact(action_with: dict) -> list[str]:
    """actions/download-artifact — skip."""
    return []


def _handle_pre_commit(action_with: dict) -> list[str]:
    """pre-commit/action — render an equivalent install + run."""
    extra_args = action_with.get("extra_args") or ""
    cmd = "pre-commit run --all-files"
    if extra_args:
        cmd += f" {extra_args}"
    return ["pip install pre-commit", cmd]


# Map of `uses:` prefix → handler. Values are also valid as None to
# explicitly mark "supported but no commands rendered" — callers can
# distinguish from "unsupported action" because handler returns [] vs
# the action being absent from the map.
_USES_HANDLERS: dict[str, callable] = {
    "actions/checkout": _handle_checkout,
    "actions/setup-python": _handle_setup_python,
    "astral-sh/setup-uv": _handle_setup_uv,
    "actions/setup-node": _handle_setup_node,
    "actions/setup-go": _handle_setup_go,
    "actions/cache": _handle_cache,
    "actions/upload-artifact": _handle_upload_artifact,
    "actions/download-artifact": _handle_download_artifact,
    "pre-commit/action": _handle_pre_commit,
}


def _classify_uses(action_ref: str) -> tuple[str, str]:
    """Split 'owner/name@v3' → ('owner/name', 'v3'). Drops version for matching."""
    if "@" in action_ref:
        prefix, version = action_ref.rsplit("@", 1)
    else:
        prefix, version = action_ref, ""
    return prefix, version


# ─── Run-step rendering ──────────────────────────────────────────────────────


_TEMPLATE_LITERAL_RE = re.compile(r"\$\{\{[^}]*\}\}")

# Known test-runner first tokens. A `run:` step starting with one of
# these is the CI's TEST command, not a setup command — SRE setup must
# NOT execute it during provisioning (the test is supposed to be
# failing on the broken state; running it during setup makes the
# provisioner think "install failed").
#
# This is the v1.7.1.1 fix to the Tier 0 extractor — without this list,
# `pip install -e .[dev]` followed by `ruff check .` both got emitted
# as install commands; ruff failed because the lint bug is what we're
# trying to fix; setup reported install_command_failed.
# Single-token test runners (first token alone identifies as test).
_TEST_RUNNER_TOKENS: frozenset[str] = frozenset({
    "pytest", "py.test", "unittest",
    "ruff", "flake8", "pylint", "mypy", "black", "isort",
    "tox", "nox", "hatch",
    "jest", "vitest", "mocha",
    "eslint", "prettier",
    "rspec", "rubocop",
    "pre-commit",
    "rustc",
    "mvn", "gradle", "gradlew",
})

# Two-token compound runners: (first, second) pairs that identify as test.
# `go test` is a runner; `go mod download` is install. `cargo test` is a
# runner; `cargo build` is install.
_TEST_RUNNER_COMPOUNDS: frozenset[tuple[str, str]] = frozenset({
    ("go", "test"),
    ("go", "vet"),
    ("cargo", "test"),
    ("cargo", "clippy"),
    ("dotnet", "test"),
})

# Wrapper tokens — strip then re-classify the underlying command.
# `uv run pytest tests/` → after stripping `uv run` the first token is
# `pytest`, which IS a test runner. `npm run lint` → `npm run` strips to
# `lint` which isn't in our list, so consider checking if `npm run X`
# where X looks like a test command. For v1.7.1.1 we keep this simple
# and document the limitation.
_WRAPPER_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("uv", "run"),
    ("uv", "tool", "run"),
    ("python", "-m"),
    ("python3", "-m"),
    ("poetry", "run"),
    ("pdm", "run"),
)


def _is_test_runner_command(cmd: str) -> bool:
    """True iff the first non-shell-prefix token of `cmd` is a known
    test runner — including:
      - bare runners (pytest, ruff, ...)
      - compound runners (go test, cargo test)
      - wrapped runners (uv run pytest, python -m pytest)

    Multi-line scripts checked on the first non-empty line.
    """
    if not cmd:
        return False
    first_line = next(
        (line.strip() for line in cmd.splitlines() if line.strip()),
        "",
    )
    if not first_line:
        return False
    tokens = first_line.split()
    # Strip leading shell helpers
    while tokens and tokens[0] in {"sudo", "env", "time"}:
        tokens = tokens[1:]
    if not tokens:
        return False

    # Strip path component on token 0: /usr/bin/pytest → pytest
    tokens[0] = tokens[0].rsplit("/", 1)[-1]

    # Check wrapper prefixes — peel layers, retry classification
    for wrapper in _WRAPPER_PREFIXES:
        if len(tokens) > len(wrapper) and tuple(tokens[: len(wrapper)]) == wrapper:
            tokens = tokens[len(wrapper) :]
            tokens[0] = tokens[0].rsplit("/", 1)[-1]
            break

    if not tokens:
        return False

    first = tokens[0]

    # Compound runner check (go test, cargo test, ...)
    if len(tokens) >= 2:
        if (first, tokens[1]) in _TEST_RUNNER_COMPOUNDS:
            return True

    # Single-token check
    return first in _TEST_RUNNER_TOKENS


def _render_run_step(run_value: str | list, env: dict | None = None) -> str | None:
    """Render a `run:` step's shell. Returns None if the step contains
    unresolved template literals we can't expand (e.g., `${{ matrix.x }}`)
    OR if the step is a test-runner command (we don't want to execute
    those during setup).

    YAML's `run:` can be a string or list-of-strings. We join lists with newlines.
    """
    if isinstance(run_value, list):
        run_value = "\n".join(str(x) for x in run_value)
    if not isinstance(run_value, str) or not run_value.strip():
        return None
    if _TEMPLATE_LITERAL_RE.search(run_value):
        # Some templates we COULD resolve (env.X, github.repo) but we don't
        # have that context here. Bail to Tier 1.
        return None
    rendered = run_value.strip()
    if _is_test_runner_command(rendered):
        # Test runner — exclude from setup commands. SRE will run the
        # actual failing_command during VERIFY, not during SETUP.
        return ""
    return rendered


# ─── Top-level extraction ────────────────────────────────────────────────────


def find_workflow_files(workspace: Path) -> list[Path]:
    """Return all .yml/.yaml under .github/workflows/ in stable order."""
    wf_dir = workspace / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    files = sorted(
        list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))
    )
    return files


def _job_matches_failing_name(
    job_key: str, job_dict: dict, failing_job_name: str
) -> bool:
    """A job in workflow YAML can be addressed by either its `name:` field
    or its dictionary key. CI's `failing_job_name` could be either.
    Match either form, case-insensitively, with whitespace tolerance.
    """
    if not failing_job_name:
        return False
    name = (job_dict.get("name") or "").strip().lower()
    key = job_key.strip().lower()
    target = failing_job_name.strip().lower()
    return target in {name, key}


def extract_recipe_from_workflow(
    workflow_file: Path, failing_job_name: str
) -> ExtractedRecipe | None:
    """Parse a single workflow file; return a recipe for the matching job
    or None if no matching job (or unrecoverable parse error).
    """
    try:
        content = workflow_file.read_text(errors="replace")
    except OSError:
        return None
    try:
        wf = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        log.warning(
            "v171.workflow.yaml_parse_failed",
            file=str(workflow_file),
            error=str(exc)[:200],
        )
        return None

    if not isinstance(wf, dict):
        return None
    jobs = wf.get("jobs")
    if not isinstance(jobs, dict):
        return None

    matching: tuple[str, dict] | None = None
    for job_key, job_dict in jobs.items():
        if not isinstance(job_dict, dict):
            continue
        if _job_matches_failing_name(job_key, job_dict, failing_job_name):
            matching = (job_key, job_dict)
            break

    if matching is None:
        return None

    job_key, job_dict = matching
    runs_on = job_dict.get("runs-on") or ""
    if isinstance(runs_on, list):
        runs_on = runs_on[0] if runs_on else ""
    runs_on = str(runs_on)

    steps = job_dict.get("steps")
    if not isinstance(steps, list):
        return None

    commands: list[str] = []
    unsupported: list[str] = []

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if "uses" in step:
            uses_ref = step["uses"]
            if not isinstance(uses_ref, str):
                continue
            prefix, _version = _classify_uses(uses_ref)
            handler = _USES_HANDLERS.get(prefix)
            if handler is None:
                # Unsupported action → mark as critical unsupported and bail.
                # Some unsupported actions are non-critical (e.g., codecov),
                # but we don't have a heuristic for that yet — be conservative.
                unsupported.append(f"step[{i}]: uses={uses_ref!r} (no handler)")
                # If the unsupported action looks safe to skip (upload/ download
                # post-build artifacts, codecov, etc.), don't reject the recipe.
                if _is_known_safe_to_skip(prefix):
                    continue
                # Reject the whole recipe — let Tier 1 take over.
                return None
            with_args = step.get("with") or {}
            handler_cmds = handler(with_args)
            if handler_cmds is None:
                unsupported.append(
                    f"step[{i}]: uses={uses_ref!r} (handler rejected)"
                )
                return None
            commands.extend(handler_cmds)
        elif "run" in step:
            rendered = _render_run_step(
                step["run"], env=step.get("env"),
            )
            if rendered is None:
                # Template literal we can't expand. If it's a critical
                # step (most common), bail to Tier 1.
                unsupported.append(
                    f"step[{i}]: run contains unresolved template literal"
                )
                return None
            if not rendered:
                # Empty rendered = test runner that we explicitly skipped
                # (setup recipe never runs the failing test command itself).
                continue
            commands.append(rendered)

    if not commands:
        # Empty recipe means we recognized everything but rendered nothing.
        # Probably the workflow was all just `uses:` skips. Still useful as
        # a "no-op recipe" — caller may want to fall through anyway.
        log.info(
            "v171.workflow.empty_recipe",
            file=str(workflow_file),
            job_key=job_key,
        )

    return ExtractedRecipe(
        workflow_file=str(workflow_file.relative_to(workflow_file.parents[2])),
        job_key=job_key,
        job_name=str(job_dict.get("name") or job_key),
        runs_on=runs_on,
        commands=commands,
        unsupported_steps=unsupported,
    )


def _is_known_safe_to_skip(action_prefix: str) -> bool:
    """Actions that are non-critical to env setup (post-build telemetry,
    artifact upload, badges). Listed here so we don't reject a recipe
    on encountering them.
    """
    return action_prefix in {
        "codecov/codecov-action",
        "treosh/lighthouse-ci-action",
        "github/super-linter",
        "softprops/action-gh-release",
    }


def extract_recipe(
    *, workspace_path: str | Path, failing_job_name: str
) -> ExtractedRecipe | None:
    """Top-level entry point. Iterate workflow files; return the first
    recipe whose job matches `failing_job_name`. Returns None if no
    workflow file yields a usable recipe (caller falls to Tier 1).
    """
    workspace = Path(workspace_path)
    if not workspace.is_dir():
        return None

    files = find_workflow_files(workspace)
    if not files:
        return None

    for wf_file in files:
        recipe = extract_recipe_from_workflow(wf_file, failing_job_name)
        if recipe is None:
            continue
        if not recipe.commands and not recipe.unsupported_steps:
            # Empty recipe with nothing to skip — try next file
            continue
        log.info(
            "v171.workflow.extracted",
            file=str(wf_file),
            job_key=recipe.job_key,
            n_commands=len(recipe.commands),
            n_unsupported=len(recipe.unsupported_steps),
        )
        return recipe

    return None


__all__ = [
    "ExtractedRecipe",
    "extract_recipe",
    "extract_recipe_from_workflow",
    "find_workflow_files",
]
