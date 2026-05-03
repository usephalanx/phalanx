"""v1.7 Tier 1 probes — deterministic evidence collected BEFORE TL plans.

Per docs/v17-architecture-gaps.md: most CI failures echo prior fixes in
the same repo, and most "flaky" diagnoses are actually env drift the
maintainer would spot in `git log .github/` if they thought to check.
Running these probes deterministically (no LLM) at commander dispatch
time gives TL evidence it would otherwise have to discover via N tool
calls — or miss entirely.

Two probes:

  1. git_log_search(error_token)
     Runs `git log -S<token> --since=180d --format=...` to find commits
     whose content (added/removed) contained the error token. Strong
     signal — most repeat CI failures echo a prior fix.

  2. env_drift_probe()
     Runs `git log --since=<window> -- .github/ Dockerfile* requirements*.txt
     pyproject.toml setup.py setup.cfg`. Shows recent infra changes that
     could be the actual cause of a "flaky" failure.

Both are pure subprocess calls with timeouts. No network. Bounded output.
Idempotent — safe to call from anywhere.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


_MAX_HITS = 8           # cap log entries we surface to TL
_MAX_BODY_BYTES = 600   # truncate per-commit body
_TIMEOUT = 10           # seconds per git command


@dataclass(frozen=True)
class GitCommitHit:
    sha: str            # short sha (10 chars)
    date: str           # ISO date "2025-04-12"
    subject: str        # commit subject line
    files: list[str]    # files modified
    diff_excerpt: str   # ≤_MAX_BODY_BYTES; lines mentioning the search token


@dataclass(frozen=True)
class ProbeResults:
    """Bundle of all pre-dispatch probe results, attached to TL's input.

    Empty lists / None mean "probe ran cleanly, no signal." Non-empty
    means TL should treat as supporting evidence — e.g., env_drift
    commits during the broken window suggest infra-cause, not code-cause.
    """
    git_log_hits: list[GitCommitHit] = field(default_factory=list)
    env_drift_hits: list[GitCommitHit] = field(default_factory=list)
    error_tokens_searched: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "git_log_hits": [h.__dict__ for h in self.git_log_hits],
            "env_drift_hits": [h.__dict__ for h in self.env_drift_hits],
            "error_tokens_searched": list(self.error_tokens_searched),
            "notes": list(self.notes),
        }

    def render_for_tl(self) -> str:
        """Compact human-readable form attached to TL's initial message."""
        lines: list[str] = []
        if self.git_log_hits:
            lines.append(
                f"=== Git history matches for error token "
                f"{self.error_tokens_searched!r} (top {len(self.git_log_hits)}) ==="
            )
            for h in self.git_log_hits:
                lines.append(f"  {h.sha} {h.date} {h.subject}")
                if h.files:
                    lines.append(f"    files: {', '.join(h.files[:5])}")
                if h.diff_excerpt:
                    excerpt = h.diff_excerpt[:300].replace("\n", "\n      ")
                    lines.append(f"      {excerpt}")
            lines.append(
                "  (note: a strong match here often means a prior fix to "
                "the same error class — review before re-deriving from scratch)"
            )
        else:
            lines.append("=== Git history: no prior matches for error token ===")
        lines.append("")
        if self.env_drift_hits:
            lines.append(
                f"=== Recent infra commits (.github/, Dockerfile, deps) — "
                f"top {len(self.env_drift_hits)} in 30d ==="
            )
            for h in self.env_drift_hits:
                lines.append(f"  {h.sha} {h.date} {h.subject}")
                if h.files:
                    lines.append(f"    files: {', '.join(h.files[:5])}")
            lines.append(
                "  (note: if failure first appeared after one of these, the "
                "fix may be reverting/adjusting the infra change, NOT patching "
                "code. Consider env_drift class diagnosis.)"
            )
        else:
            lines.append("=== No recent infra commits (env unchanged 30d) ===")
        if self.notes:
            lines.append("")
            for n in self.notes:
                lines.append(f"  [probe note] {n}")
        return "\n".join(lines)


# ─── Distinctive-token extraction (shared with c1 self-critique) ─────────────


_DISTINCTIVE_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{4,}\b")
_TOKEN_STOPWORDS = frozenset({
    "error", "errors", "failed", "failure", "failing", "fails", "warning",
    "exception", "trace", "traceback", "process", "completed", "exited",
    "tests", "passed", "platform", "linux", "macos", "windows", "python",
    "session", "starts", "Found", "TODO", "BUG", "result", "results",
    "module", "import", "ImportError", "AttributeError", "AssertionError",
    "KeyError", "ValueError", "TypeError", "RuntimeError", "Exception",
    "config", "configfile", "rootdir", "items", "collected", "summary",
    "running", "install", "installing", "successfully", "warning",
    "deprecation", "deprecated",
})


def extract_error_tokens(text: str, *, max_tokens: int = 5) -> list[str]:
    """Pick the most distinctive ≥5-char identifiers from `text` likely
    to appear in past code that touched the bug.

    Examples:
      "ModuleNotFoundError: No module named 'httpx'" → ["httpx"]
      "tests/test_time.py:142: AssertionError" → ["test_time"]
      "tomorrow != today (naturaldate fail)" → ["naturaldate", "tomorrow"]
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _DISTINCTIVE_TOKEN_RE.finditer(text):
        tok = m.group(0)
        # Strip leading/trailing punctuation already done by regex; check stopwords
        if tok in _TOKEN_STOPWORDS or tok.lower() in _TOKEN_STOPWORDS:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= max_tokens:
            break
    return out


# ─── Core git probes ─────────────────────────────────────────────────────────


def _run_git(args: list[str], cwd: str | Path) -> str | None:
    """Run a git subprocess, return stdout (str) or None on failure."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, scoped to workspace
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# Each commit emits: "<COMMIT>%h\t%aI\t%s" then file list.
# We use TAB as the field separator (NOT 0x1e as one might expect) because
# Python's str.splitlines() treats 0x1e (and 0x1d/0x1c/0x85) as line
# terminators in addition to \n — silently splitting our metadata line into
# pieces. TAB is unambiguous and never appears in git's %h, %aI, or %s output.
_COMMIT_SENTINEL = "<<<COMMIT>>>"
_LOG_FORMAT = f"{_COMMIT_SENTINEL}%h\t%aI\t%s"


def git_log_search(
    *, token: str, workspace: str | Path, since: str = "180.days.ago"
) -> list[GitCommitHit]:
    """Find commits whose diff content contains `token`.

    Uses `git log -S<token>` (pickaxe) — finds commits where the COUNT
    of `token` occurrences differs between parent and child. Strong
    signal because it isolates commits that actually added or removed
    that string, not just mentioned it in a message.
    """
    if not token or len(token) < 4:
        return []
    workspace = Path(workspace).resolve()
    if not (workspace / ".git").exists():
        log.info("v3.probe.git_log_search.skip_no_git", workspace=str(workspace))
        return []

    raw = _run_git(
        [
            "log",
            f"-S{token}",
            "--since",
            since,
            f"--format={_LOG_FORMAT}",
            "--name-only",
            "--",
        ],
        cwd=workspace,
    )
    if not raw:
        return []

    hits = _parse_git_log_records(raw, with_diff_excerpt=True, workspace=workspace, token=token)
    log.info("v3.probe.git_log_search.done", token=token, n_hits=len(hits))
    return hits


def _parse_git_log_records(
    raw: str,
    *,
    with_diff_excerpt: bool,
    workspace: Path,
    token: str = "",
) -> list[GitCommitHit]:
    """Parse the sentinel-delimited output of `git log --format=_LOG_FORMAT
    --name-only`. Each chunk after the sentinel has the metadata line on
    the first line and file paths on subsequent non-empty lines.
    """
    hits: list[GitCommitHit] = []
    chunks = raw.split(_COMMIT_SENTINEL)
    for chunk in chunks[1:]:  # first chunk before any sentinel is empty
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        head = lines[0]
        try:
            sha, date, subject = head.split("\t", 2)
        except ValueError:
            continue
        files = lines[1:]  # remaining non-empty lines are file paths
        excerpt = ""
        if with_diff_excerpt and token:
            excerpt = _git_diff_excerpt(sha, token, workspace)
        hits.append(GitCommitHit(
            sha=sha.strip()[:10],
            date=date[:10],
            subject=subject.strip()[:140],
            files=files[:8],
            diff_excerpt=excerpt[:_MAX_BODY_BYTES],
        ))
        if len(hits) >= _MAX_HITS:
            break
    return hits


def _git_diff_excerpt(sha: str, token: str, workspace: Path) -> str:
    """Pull a few diff lines containing the search token from a commit's
    patch. Truncated to keep TL's prompt manageable.
    """
    raw = _run_git(["show", "--format=", "--unified=1", sha], cwd=workspace)
    if not raw:
        return ""
    lines = []
    for line in raw.splitlines():
        if not (line.startswith("+") or line.startswith("-")):
            continue
        if line.startswith(("+++", "---")):
            continue
        if token in line:
            lines.append(line[:140])
            if len(lines) >= 6:
                break
    return "\n".join(lines)


def env_drift_probe(
    *, workspace: str | Path, since: str = "30.days.ago"
) -> list[GitCommitHit]:
    """List recent commits to infra files that could explain a "code is
    fine but CI broke" failure.
    """
    workspace = Path(workspace).resolve()
    if not (workspace / ".git").exists():
        return []

    paths = [
        ".github/",
        "Dockerfile",
        "Dockerfile.*",
        "requirements.txt",
        "requirements*.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pixi.toml",
        "pixi.lock",
        "poetry.lock",
        "uv.lock",
    ]
    raw = _run_git(
        [
            "log",
            "--since",
            since,
            f"--format={_LOG_FORMAT}",
            "--name-only",
            "--",
            *paths,
        ],
        cwd=workspace,
    )
    if not raw:
        return []
    hits = _parse_git_log_records(raw, with_diff_excerpt=False, workspace=workspace)
    # env_drift wants commits that ACTUALLY touched infra files
    hits = [h for h in hits if h.files]
    log.info("v3.probe.env_drift.done", n_hits=len(hits))
    return hits


# ─── Top-level orchestration ─────────────────────────────────────────────────


def run_pre_tl_probes(
    *,
    failing_command: str,
    error_line_or_log: str,
    workspace_path: str | Path,
) -> ProbeResults:
    """Run all pre-dispatch probes against `workspace_path` and return a
    bundle. Best-effort — failures in one probe don't break the others.
    """
    if not workspace_path:
        return ProbeResults(notes=["workspace_path missing — probes skipped"])
    ws = Path(workspace_path)
    if not ws.is_dir():
        return ProbeResults(notes=[f"workspace_path not a directory: {ws}"])

    tokens = extract_error_tokens(error_line_or_log, max_tokens=3)
    if failing_command:
        tokens.extend(extract_error_tokens(failing_command, max_tokens=2))
    # Dedup but keep order
    seen: set[str] = set()
    tokens = [t for t in tokens if not (t in seen or seen.add(t))][:5]

    git_hits: list[GitCommitHit] = []
    if tokens:
        for token in tokens:
            hits = git_log_search(token=token, workspace=ws)
            if hits:
                git_hits.extend(hits)
                # Limit total hits across tokens
                if len(git_hits) >= _MAX_HITS:
                    git_hits = git_hits[:_MAX_HITS]
                    break

    env_hits = env_drift_probe(workspace=ws)

    notes: list[str] = []
    if not tokens:
        notes.append(
            "no distinctive tokens extracted from error_line — git history "
            "search skipped"
        )
    return ProbeResults(
        git_log_hits=git_hits,
        env_drift_hits=env_hits,
        error_tokens_searched=tokens,
        notes=notes,
    )
