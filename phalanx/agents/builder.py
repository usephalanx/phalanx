"""
Builder Agent — implements code changes based on the Planner's output.

Responsibilities:
  1. Load task + planner's implementation plan from prior tasks
  2. Set up git workspace: clone/update repo, create/checkout branch
  3. Read existing file contents for context
  4. Call Claude to generate precise file changes (JSON)
  5. Write files to disk
  6. Git add + commit (with FORGE bot author); push if remote configured
  7. Update Run.active_branch; persist diff as Artifact
  8. Mark task COMPLETED

Design (AD-001):
  - Builder uses Anthropic API (not Claude Code SDK) for MVP.
    Claude Code SDK subprocess integration is post-MVP.
  - If GitHub token + project repo_url are configured: real git commits.
  - If not: writes to local workspace only (for local demo/testing).
  - AP-003: exceptions propagate — Celery handles retries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, update

from phalanx.agents.base import AgentResult, BaseAgent, get_anthropic_client, mark_task_failed
from phalanx.config.settings import get_settings
from phalanx.db.models import Artifact, Run, Task
from phalanx.db.session import get_db
from phalanx.queue.celery_app import celery_app

log = structlog.get_logger(__name__)

settings = get_settings()

# ─────────────────────────────────────────────────────────────────────────────
# TOKEN BUDGET — DO NOT CHANGE WITHOUT STRONG EVIDENCE
# ─────────────────────────────────────────────────────────────────────────────
# The Anthropic SDK raises ValueError("Streaming is required for operations
# that may take longer than 10 minutes") for non-streaming calls when:
#
#   estimated_time = 3600 * max_tokens / 128_000
#   if estimated_time > 600:  → raises ValueError
#
# This means the hard ceiling for non-streaming is:
#   max_tokens ≤ 21,333  (600 * 128_000 / 3600)
#
# History of changes (so you understand why we landed here):
#   32,000 → BROKEN: estimated 900s → SDK raises ValueError
#    8,192 → Worked but truncated long Xcode .pbxproj outputs (fell back to output.txt)
#   16,000 → Was the safe baseline (estimated 450s), but real Kanban sims showed that
#            production-quality FastAPI route files + Pydantic schemas + tests exceed
#            16K tokens even with lean input context (~6K tokens input). CRUD routes
#            with 6 endpoints, auth integration, error handling, and 10+ test cases
#            are legitimately verbose — this is NOT a task-design problem.
#   20,000 → Current: estimated 562.5s (3600*20000/128000) — safely under 600s limit.
#            Verified: 562.5 < 600. Fixes truncation for complex route+schema+test tasks.
#            Real simulation confirmed no ValueError at this value.
#
# WHY THE BUILDER STILL FALLS BACK TO output.txt (for pathological cases):
#   When Claude generates Xcode .pbxproj files or similar machine-generated blobs,
#   the output exceeds even 20K tokens. The fix for THOSE cases is task design
#   (scaffold with README + commands instead of generating the file). This value
#   change addresses legitimate production code that was hitting the 16K ceiling.
#
# BEFORE CHANGING THIS VALUE, verify:
#   1. New value passes: 3600 * NEW_VALUE / 128_000 ≤ 600
#   2. Real builder simulation completes without ValueError
#   3. Integration tests still pass at 70%+ coverage
# ─────────────────────────────────────────────────────────────────────────────
_BUILD_MAX_TOKENS = 20000
# Streaming has no SDK time-limit check — safe to use higher ceiling.
# claude-opus-4-6 supports up to 32,768 output tokens.
_STREAM_MAX_TOKENS = 32000
# Max bytes to read from any single existing file (avoid token overflow)
_MAX_FILE_READ_BYTES = 4_000
# Max total bytes of existing file context to send to Claude
# Kept at 16K to leave room for output: with 16K input cap, Claude has ~12K tokens
# for actual code output (vs ~4K when context was 40K and input ballooned to 12K tokens).
_MAX_CONTEXT_BYTES = 16_000


class BuilderAgent(BaseAgent):
    """
    IC4-level implementation agent.

    Reads the plan, writes the code, commits it. That's the contract.
    Every file change is recorded in Task.output and persisted as an Artifact.
    """

    AGENT_ROLE = "builder"

    async def execute(self) -> AgentResult:
        self._log.info("builder.execute.start")

        async with get_db() as session:
            task = await self._load_task(session)
            if task is None:
                return AgentResult(success=False, output={}, error=f"Task {self.task_id} not found")
            run = await self._load_run(session)
            plan = await self._load_planner_plan(session, task.sequence_num)

        # Set up workspace
        workspace = self._workspace_path(run)
        await self._ensure_workspace(workspace, run)

        # Read relevant existing files for context
        existing_files = self._read_existing_files(workspace, task)

        # Generate code changes
        changes = await self._generate_changes(task, plan, existing_files, workspace)

        # Apply changes to disk
        files_written = self._apply_changes(workspace, changes)

        # Commit (git if available, else record locally)
        commit_info = await self._commit_changes(workspace, task, run, files_written)

        output = {
            "workspace": str(workspace),
            "files_written": files_written,
            "commit": commit_info,
            "plan_used": bool(plan),
            "summary": changes.get("summary", ""),
        }

        async with get_db() as session:
            run_ref = await self._load_run(session)

            # Update Run.active_branch if commit produced a branch
            if commit_info.get("branch"):
                await session.execute(
                    update(Run)
                    .where(Run.id == self.run_id)
                    .values(active_branch=commit_info["branch"], updated_at=datetime.now(UTC))
                )

            # Persist diff artifact
            await self._persist_artifact(session, output, run_ref.project_id, changes)

            # Mark task complete
            await session.execute(
                update(Task)
                .where(Task.id == self.task_id)
                .values(
                    status="COMPLETED",
                    output=output,
                    actual_complexity=task.estimated_complexity,
                    completed_at=datetime.now(UTC),
                )
            )
            await session.commit()

        await self._audit(
            event_type="task_complete",
            payload={
                "files_written": len(files_written),
                "has_commit": bool(commit_info.get("sha")),
            },
        )

        self._log.info(
            "builder.execute.done",
            files_written=len(files_written),
            tokens_used=self._tokens_used,
        )
        return AgentResult(success=True, output=output, tokens_used=self._tokens_used)

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _load_task(self, session) -> Task | None:
        result = await session.execute(select(Task).where(Task.id == self.task_id))
        return result.scalar_one_or_none()

    async def _load_run(self, session) -> Run:
        result = await session.execute(select(Run).where(Run.id == self.run_id))
        return result.scalar_one()

    async def _load_planner_plan(self, session, before_seq: int) -> dict:
        """Find the most recent completed planner task output in this run."""
        result = await session.execute(
            select(Task)
            .where(
                Task.run_id == self.run_id,
                Task.agent_role == "planner",
                Task.sequence_num < before_seq,
                Task.status == "COMPLETED",
            )
            .order_by(Task.sequence_num.desc())
            .limit(1)
        )
        task = result.scalar_one_or_none()
        return task.output or {} if task else {}

    # ── Workspace helpers ─────────────────────────────────────────────────────

    def _workspace_path(self, run: Run, branch_name: str | None = None) -> Path:
        base = Path(settings.git_workspace)
        run_dir = base / run.project_id / self.run_id
        if branch_name:
            # Isolate each epic in its own subdir; replace / with _ for safety
            slug = branch_name.replace("/", "_")
            return run_dir / slug
        return run_dir

    async def _ensure_workspace(self, workspace: Path, run: Run, branch: str | None = None) -> None:
        """
        Set up the workspace directory. If GitHub is configured and project has
        a repo_url, clone/update. Otherwise create a local directory.
        """
        workspace.mkdir(parents=True, exist_ok=True)

        resolved_branch = branch or run.active_branch or f"phalanx/run-{self.run_id[:8]}"

        if settings.github_token:
            await self._setup_git_workspace(workspace, run, resolved_branch)
        else:
            self._log.info("builder.workspace.local", path=str(workspace))

    async def _setup_git_workspace(self, workspace: Path, run: Run, branch: str) -> None:
        """Clone or update the repo, then checkout/create the working branch."""
        try:
            from git import Repo  # noqa: PLC0415

            # Try to get project repo_url from DB project.config
            async with get_db() as session:
                from phalanx.db.models import Project  # noqa: PLC0415

                result = await session.execute(select(Project).where(Project.id == run.project_id))
                project = result.scalar_one_or_none()

            repo_url = (project.config or {}).get("repo_url", "") if project else ""

            if not repo_url:
                self._log.info("builder.git.no_repo_url", project_id=run.project_id)
                return

            # Embed token in URL for authentication
            auth_url = repo_url.replace("https://", f"https://{settings.github_token}@")

            git_dir = workspace / ".git"
            if git_dir.exists():
                repo = Repo(str(workspace))
                # Abort any stale rebase left by a crashed parallel builder
                rebase_merge = workspace / ".git" / "rebase-merge"
                rebase_apply = workspace / ".git" / "rebase-apply"
                if rebase_merge.exists() or rebase_apply.exists():
                    try:
                        repo.git.rebase("--abort")
                        self._log.warning("builder.git.stale_rebase_aborted", workspace=str(workspace))
                    except Exception:
                        pass
                repo.remotes.origin.fetch()
                self._log.info("builder.git.fetched", workspace=str(workspace))
            else:
                repo = Repo.clone_from(auth_url, str(workspace))
                self._log.info("builder.git.cloned", url=repo_url)

            # Checkout or create the working branch
            try:
                repo.git.checkout(branch)
            except Exception:
                repo.git.checkout("-b", branch)

            # Pull latest from remote so parallel builders start from the same base
            try:
                repo.git.pull("--rebase", "origin", branch)
                self._log.info("builder.git.pulled", branch=branch)
            except Exception:
                # Branch may not exist on remote yet — that's fine
                pass

            self._log.info("builder.git.branch_ready", branch=branch)

        except ImportError:
            self._log.warning("builder.git.gitpython_missing")
        except Exception as exc:
            self._log.warning("builder.git.setup_failed", error=str(exc))

    def _read_existing_files(self, workspace: Path, task: Task) -> dict[str, str]:
        """
        Read relevant existing files to give Claude the current code state.
        Prioritises files_likely_touched; falls back to a shallow directory scan.
        """
        contents: dict[str, str] = {}
        total_bytes = 0

        # First: explicitly listed files
        for rel_path in task.files_likely_touched or []:
            full = workspace / rel_path
            if full.exists() and full.is_file():
                try:
                    text = full.read_text(errors="replace")[:_MAX_FILE_READ_BYTES]
                    contents[rel_path] = text
                    total_bytes += len(text.encode())
                    if total_bytes >= _MAX_CONTEXT_BYTES:
                        break
                except OSError:
                    pass

        # Always include shared data/type files if they exist (critical for cross-task consistency)
        _SHARED_FILES = [
            "lib/data.ts", "lib/types.ts", "lib/constants.ts",
            "src/lib/data.ts", "src/lib/types.ts",
            "app/page.tsx", "app/layout.tsx",
        ]
        for shared in _SHARED_FILES:
            if shared in contents:
                continue
            full = workspace / shared
            if full.exists() and full.is_file():
                try:
                    text = full.read_text(errors="replace")[:_MAX_FILE_READ_BYTES]
                    contents[shared] = text
                    total_bytes += len(text.encode())
                    if total_bytes >= _MAX_CONTEXT_BYTES:
                        break
                except OSError:
                    pass

        # Fill remaining budget with source files (Python or TypeScript/TSX)
        if total_bytes < _MAX_CONTEXT_BYTES // 2:
            py_files = sorted(workspace.rglob("*.py"))
            ts_files = sorted(workspace.rglob("*.ts")) + sorted(workspace.rglob("*.tsx"))
            source_files = py_files if py_files else ts_files
            for src_file in source_files[:30]:
                if src_file.stat().st_size == 0:
                    continue
                rel = str(src_file.relative_to(workspace))
                if rel in contents:
                    continue
                # Skip test files, migrations, generated files
                if any(skip in rel for skip in ("test_", "alembic/versions", "__pycache__",
                                                 ".test.", ".spec.", "__tests__", "node_modules")):
                    continue
                try:
                    text = src_file.read_text(errors="replace")[:_MAX_FILE_READ_BYTES]
                    contents[rel] = text
                    total_bytes += len(text.encode())
                    if total_bytes >= _MAX_CONTEXT_BYTES:
                        break
                except OSError:
                    pass

        return contents

    # ── Code generation ───────────────────────────────────────────────────────

    # ── System prompts by agent role ──────────────────────────────────────────

    _SYSTEM_DEFAULT = """\
You are implementing a phase of a carefully planned software project in FORGE.
Your role: deliver the code changes described in the task with production quality.

Rules:
- Write COMPLETE file contents — not partial diffs or snippets.
- Follow existing code style exactly (indentation, naming, patterns).
- Every new function/class must have a docstring.
- Implement tests for new functionality (test_*.py files in tests/).
- Use type annotations throughout.
- Never hardcode credentials or secrets.
- Every deliverable listed in the task description MUST be implemented.

NEVER generate auto-generated or tooling-produced files. These are always too large,
always wrong when hand-written, and must come from the project's own toolchain instead.
For any of these, write a SETUP.md with the exact commands to run:
- iOS/macOS:  *.pbxproj, *.xcworkspace, *.xcscheme, Podfile.lock, DerivedData/
- Android:    gradlew, gradlew.bat, gradle-wrapper.jar, *.iml
- JS/TS:      package-lock.json, yarn.lock, pnpm-lock.yaml, node_modules/
- Python:     poetry.lock, Pipfile.lock, *.egg-info/, __pycache__/, .venv/
- Flutter:    pubspec.lock, .flutter-tool/, .dart_tool/
- Any:        .git/ internals, *.lock files, binary files, build/ dist/ output dirs

Return ONLY valid JSON — no markdown fences, no explanation outside the JSON.

{
  "summary": "one sentence describing what was implemented",
  "commit_message": "feat: concise commit message (< 72 chars)",
  "files": [
    {
      "path": "relative/path/to/file.py",
      "action": "create|modify|delete",
      "content": "complete file content as a string"
    }
  ]
}"""

    _SYSTEM_COMPONENT_BUILDER = """\
You are a React component specialist in FORGE. Your job: build ONE atomic, reusable
UI component. This component will be imported by a page assembler in a later task.

Rules:
- Output EXACTLY 2 files: the component (.tsx) and its test (.test.tsx). No more.
- The component receives ALL data via props — no direct API calls, no routing logic.
- Export a single named component as the default export.
- Props must be fully typed with a TypeScript interface defined in the same file.
- Keep the component under 150 lines of TSX (excluding tests).
- Tests use Vitest + React Testing Library. Cover: renders without crashing, key props.
- Never hardcode strings that belong in props.
- Follow existing code style from the context files provided.

NEVER generate: package.json, vite.config.ts, tsconfig.json, package-lock.json,
node_modules/, or any config/tooling file. Only the component and its test.

Return ONLY valid JSON — no markdown fences.

{
  "summary": "one sentence: what component was built and its purpose",
  "commit_message": "feat: concise commit message (< 72 chars)",
  "files": [
    {
      "path": "frontend/src/components/ComponentName.tsx",
      "action": "create",
      "content": "complete component file"
    },
    {
      "path": "frontend/src/components/ComponentName.test.tsx",
      "action": "create",
      "content": "complete test file"
    }
  ]
}"""

    _SYSTEM_PAGE_ASSEMBLER = """\
You are a React page assembler in FORGE. Your job: compose existing components into
a complete page. The components you need already exist — import them, wire up state,
connect to the API layer. Do NOT rewrite component logic that already exists.

Rules:
- Output EXACTLY 1 file: the page component (.tsx). Tests are a separate task.
- Import components from their existing paths (provided in the context).
- Handle page-level concerns only: data fetching (useEffect/React Query), routing
  (useNavigate/useParams), and wiring props from API data to components.
- Keep the page under 200 lines. If it grows beyond that, extract to a new component task.
- Use the existing API service modules (provided in context) — don't write raw fetch calls.
- Never duplicate component logic that already exists in the imported components.

NEVER generate: package.json, tsconfig.json, test files, or component files.
Only the single page file.

Return ONLY valid JSON — no markdown fences.

{
  "summary": "one sentence: what page was assembled and what it does",
  "commit_message": "feat: concise commit message (< 72 chars)",
  "files": [
    {
      "path": "frontend/src/pages/PageName.tsx",
      "action": "create",
      "content": "complete page file"
    }
  ]
}"""

    def _get_system_prompt(self, task: Task) -> str:
        """Select system prompt based on agent_role."""
        role = getattr(task, "agent_role", "builder")
        role_block = ""
        if getattr(task, "role_context", None):
            role_block = f"{task.role_context}\n\n"
        if role == "component_builder":
            return role_block + self._SYSTEM_COMPONENT_BUILDER
        if role == "page_assembler":
            return role_block + self._SYSTEM_PAGE_ASSEMBLER
        return role_block + self._SYSTEM_DEFAULT

    async def _generate_changes(
        self,
        task: Task,
        plan: dict,
        existing_files: dict[str, str],
        workspace: Path,
    ) -> dict[str, Any]:
        """Dispatch to streaming or blocking based on feature flag."""
        if settings.forge_streaming_builder:
            return await self._generate_changes_streaming(task, plan, existing_files, workspace)
        return await self._generate_changes_blocking(task, plan, existing_files, workspace)

    def _build_prompt(
        self,
        task: Task,
        plan: dict,
        existing_files: dict[str, str],
    ) -> tuple[str, list[dict]]:
        """Build the system prompt and messages list shared by both generation paths."""
        file_context = ""
        if existing_files:
            parts = [f"--- {path} ---\n{content}" for path, content in existing_files.items()]
            file_context = "\n\n".join(parts)[:_MAX_CONTEXT_BYTES]

        plan_text = (
            json.dumps(plan, indent=2)[:4000]
            if plan
            else "No explicit plan — use task description."
        )

        system = self._get_system_prompt(task)
        messages = [
            {
                "role": "user",
                "content": (
                    f"Task: {task.title}\n\n"
                    f"Description:\n{task.description}\n\n"
                    f"Implementation Plan:\n{plan_text}\n\n"
                    + (
                        f"Existing code context:\n{file_context}\n\n"
                        if file_context
                        else "No existing files — create everything from scratch.\n\n"
                    )
                    + "Implement ALL deliverables. Write complete, production-ready code."
                ),
            }
        ]
        return system, messages

    async def _generate_changes_blocking(
        self,
        task: Task,
        plan: dict,
        existing_files: dict[str, str],
        workspace: Path,
    ) -> dict[str, Any]:
        """Blocking path: single API call, parse entire JSON response."""
        system, messages = self._build_prompt(task, plan, existing_files)
        raw = self._call_claude(messages=messages, system=system, max_tokens=_BUILD_MAX_TOKENS)

        parsed = self._parse_json_response(raw)
        if parsed is not None:
            return parsed

        self._log.error("builder.json_parse_failed", raw_len=len(raw))
        return {
            "summary": "Code generation completed (raw output stored)",
            "commit_message": f"feat: {task.title[:60]}",
            "files": [
                {
                    "path": "forge/_generated/output.txt",
                    "action": "create",
                    "content": raw,
                }
            ],
        }

    async def _generate_changes_streaming(
        self,
        task: Task,
        plan: dict,
        existing_files: dict[str, str],
        workspace: Path,
    ) -> dict[str, Any]:
        """
        Streaming path — writes each file as Claude generates it.

        Benefits over blocking:
        - No 20K output token ceiling (streaming has no SDK time limit)
        - Files written to disk immediately as each completes
        - Partial recovery: files already written survive a mid-stream error
        """
        from phalanx.agents.streaming_parser import StreamingJsonFileParser  # noqa: PLC0415

        system, messages = self._build_prompt(task, plan, existing_files)
        parser = StreamingJsonFileParser()
        files_written: list[str] = []
        collected_files: list[dict] = []

        client = get_anthropic_client()
        with client.messages.stream(
            model=settings.anthropic_model_default,
            max_tokens=_STREAM_MAX_TOKENS,
            system=system,
            messages=messages,
        ) as stream:
            for text_chunk in stream.text_stream:
                for file_obj in parser.feed(text_chunk):
                    path = self._apply_single_file(workspace, file_obj)
                    if path:
                        files_written.append(path)
                        collected_files.append(file_obj)
                        self._log.debug(
                            "builder.streaming.file_written",
                            path=path,
                            total=len(files_written),
                        )

            usage = stream.get_final_message().usage
            tokens = usage.input_tokens + usage.output_tokens
            self._tokens_used += tokens
            self._log.debug(
                "agent.claude_call",
                via="api_stream",
                model=settings.anthropic_model_default,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=tokens,
                budget_remaining=self.token_budget - self._tokens_used,
            )

        return {
            "summary": parser.summary or f"Streaming build: {task.title}",
            "commit_message": parser.commit_message or f"feat: {task.title[:60]}",
            "files": collected_files,
        }

    def _parse_json_response(self, raw: str) -> dict | None:
        """
        Robustly extract the JSON object from Claude's response.

        Handles:
        - Markdown fences (```json ... ```)
        - Extra prose before/after the JSON
        - Responses that open with { directly
        """
        text = raw.strip()

        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Drop the first fence line (```json or ```) and the last if it's also a fence
            inner_lines = lines[1:]
            if inner_lines and inner_lines[-1].strip() == "```":
                inner_lines = inner_lines[:-1]
            text = "\n".join(inner_lines).strip()

        # Find the outermost JSON object by scanning for matching braces.
        # Try each { position in order — prose before the real JSON may contain
        # {}-like patterns (e.g. "{status: 'ok'}") that look like JSON starts
        # but fail to parse. Advancing past each failed attempt finds the real object.
        search_from = 0
        while True:
            start = text.find("{", search_from)
            if start == -1:
                return None

            depth = 0
            in_string = False
            escape_next = False
            end = -1

            for i, ch in enumerate(text[start:], start):
                if escape_next:
                    escape_next = False
                    continue
                if ch == "\\" and in_string:
                    escape_next = True
                    continue
                if ch == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break

            if end == -1:
                # Truncated — fall back to rfind heuristic from current start
                end = text.rfind("}") + 1

            if end <= start:
                search_from = start + 1
                continue

            try:
                return json.loads(text[start:end])
            except (json.JSONDecodeError, ValueError):
                # This { was not the start of the real JSON object — try next one
                search_from = start + 1
                continue

    # ── File application ──────────────────────────────────────────────────────

    def _apply_single_file(self, workspace: Path, file_spec: dict) -> str | None:
        """Write a single file spec to disk. Returns relative path or None on skip."""
        rel_path = file_spec.get("path", "")
        action = file_spec.get("action", "create")
        content = file_spec.get("content", "")

        if not rel_path:
            return None

        full_path = workspace / rel_path
        if action == "delete":
            if full_path.exists():
                full_path.unlink()
            return f"DELETE:{rel_path}"
        else:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            self._log.debug("builder.file_written", path=rel_path)
            return rel_path

    def _apply_changes(self, workspace: Path, changes: dict) -> list[str]:
        """Write file contents to disk. Returns list of relative paths written."""
        written: list[str] = []
        for file_spec in changes.get("files", []):
            rel_path = file_spec.get("path", "")
            action = file_spec.get("action", "create")
            content = file_spec.get("content", "")

            if not rel_path:
                continue

            full_path = workspace / rel_path

            if action == "delete":
                if full_path.exists():
                    full_path.unlink()
                    written.append(f"DELETE:{rel_path}")
            else:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content, encoding="utf-8")
                written.append(rel_path)
                self._log.debug("builder.file_written", path=rel_path)

        return written

    # ── Git commit ────────────────────────────────────────────────────────────

    async def _commit_changes(
        self, workspace: Path, task: Task, run: Run, files_written: list[str],
        branch: str | None = None,
    ) -> dict:
        """Commit changes to git if available. Returns commit info dict."""
        if not files_written:
            return {}

        branch = branch or run.active_branch or f"phalanx/run-{self.run_id[:8]}"

        try:
            from git import Actor, Repo  # noqa: PLC0415
            from git.exc import InvalidGitRepositoryError  # noqa: PLC0415

            try:
                repo = Repo(str(workspace))
            except (InvalidGitRepositoryError, Exception):
                repo = Repo.init(str(workspace))
                self._log.info("builder.git.initialized", workspace=str(workspace))

            # Stage all written files
            repo.git.add("-A")

            if not repo.index.diff("HEAD") and not repo.untracked_files:
                return {"branch": branch, "sha": None, "message": "No changes to commit"}

            author = Actor(settings.git_author_name, settings.git_author_email)
            commit_message = (
                f"feat: {task.title[:60]}\n\n"
                f"Run: {self.run_id}\n"
                f"Task: {self.task_id}\n"
                f"Agent: {self.AGENT_ROLE}"
            )
            commit = repo.index.commit(commit_message, author=author, committer=author)

            sha = commit.hexsha[:8]
            self._log.info("builder.git.committed", sha=sha, branch=branch)

            # Push if remote configured; rebase + retry once on non-fast-forward
            if settings.github_token and repo.remotes:
                try:
                    repo.git.push("origin", branch, "--set-upstream")
                    self._log.info("builder.git.pushed", branch=branch)
                except Exception as push_exc:
                    self._log.warning("builder.git.push_failed_retrying", error=str(push_exc))
                    try:
                        repo.remotes.origin.fetch()
                        repo.git.rebase(f"origin/{branch}")
                        # SHA changes after rebase — read the new one
                        sha = repo.head.commit.hexsha[:8]
                        repo.git.push("origin", branch, "--set-upstream")
                        self._log.info("builder.git.pushed_after_rebase", branch=branch, sha=sha)
                    except Exception as rebase_exc:
                        self._log.warning(
                            "builder.git.push_conflict_unresolvable",
                            error=str(rebase_exc),
                        )
                        try:
                            repo.git.rebase("--abort")
                        except Exception:
                            pass

            return {"branch": branch, "sha": sha, "message": commit_message.split("\n")[0]}

        except ImportError:
            self._log.warning("builder.git.unavailable")
            return {"branch": branch, "sha": None, "message": "git unavailable"}
        except Exception as exc:
            self._log.warning("builder.git.commit_failed", error=str(exc))
            return {"branch": branch, "sha": None, "error": str(exc)}

    # ── Artifact ──────────────────────────────────────────────────────────────

    async def _persist_artifact(
        self, session, output: dict, project_id: str, changes: dict
    ) -> None:
        try:
            json_bytes = json.dumps(output).encode()
            artifact = Artifact(
                run_id=self.run_id,
                task_id=self.task_id,
                project_id=project_id,
                artifact_type="diff",
                title=f"Build: {changes.get('summary', self.task_id)}",
                s3_key=f"local/{self.run_id}/{self.task_id}/build.json",
                content_hash=hashlib.sha256(json_bytes).hexdigest(),
                quality_evidence={
                    "gate": "build",
                    "files_written": output["files_written"],
                    "commit": output["commit"],
                    "summary": changes.get("summary", ""),
                },
            )
            session.add(artifact)
            await session.commit()
        except Exception as exc:
            self._log.warning("builder.artifact_persist_failed", error=str(exc))


# ── Celery task entry point ───────────────────────────────────────────────────


@celery_app.task(
    name="phalanx.agents.builder.execute_task",
    bind=True,
    queue="builder",
    max_retries=2,
    acks_late=True,
    soft_time_limit=1800,   # 30 min: git clone + LLM codegen can be slow
    time_limit=3600,        # 1 hour hard kill
)
def execute_task(  # pragma: no cover
    self, task_id: str, run_id: str, assigned_agent_id: str | None = None, **kwargs
) -> dict:
    """Celery entry point: build code for a single task."""

    agent = BuilderAgent(
        run_id=run_id,
        task_id=task_id,
        agent_id=assigned_agent_id or "builder",
    )
    try:
        result = asyncio.run(agent.execute())
    except Exception as exc:
        log.exception("builder.celery_task_unhandled", task_id=task_id, run_id=run_id)
        asyncio.run(mark_task_failed(task_id, str(exc)))
        raise

    if not result.success:
        log.error("builder.task_failed", task_id=task_id, run_id=run_id, error=result.error)

    return {
        "success": result.success,
        "task_id": task_id,
        "run_id": run_id,
        "tokens_used": result.tokens_used,
        "error": result.error,
    }
