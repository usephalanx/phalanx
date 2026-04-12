# Phase 2 — Streaming Builder

**Status:** Planned (not started)
**Priority:** High — eliminates token-ceiling truncation permanently
**Prerequisite:** Phase 1 (component_builder / page_assembler roles) deployed and validated

---

## Problem This Solves

The current builder calls Claude with `max_tokens=20000` and expects a single JSON blob
containing ALL file contents for a task. If Claude's output exceeds that limit, the JSON
is truncated mid-string → `json.loads()` fails → `files_written=0` → silent data loss.

The ceiling is hard: `3600 * max_tokens / 128_000 ≤ 600` → max safe value is 21,333 tokens.
No task-splitting strategy can fully escape this because some individual files (e.g. a complex
route file with full test suite) can approach or exceed 20K tokens on their own.

---

## Architecture

Replace the single blocking API call with a streaming session that writes each file
as Claude completes it.

### Current flow
```
BuilderAgent._generate_changes()
    → client.messages.create(max_tokens=20000)   # blocking, all-or-nothing
    → parse entire JSON blob
    → _apply_changes() writes all files at once
```

### Phase 2 flow
```
BuilderAgent._generate_changes_streaming()
    → client.messages.stream(...)                # streaming, file-by-file
    → StreamingJsonFileParser.on_chunk(chunk)    # stateful incremental parser
    → yield FileObject as each file completes    # write + commit per file
    → collect all FileObjects → return summary
```

---

## Implementation Plan

### 1. StreamingJsonFileParser (`phalanx/agents/streaming_parser.py`)

A stateful parser that consumes the Claude streaming response token by token and emits
complete `FileObject` dicts as each file's `content` field closes.

```python
class StreamingJsonFileParser:
    """
    Parses a streaming JSON response of the form:
    {
      "summary": "...",
      "commit_message": "...",
      "files": [
        {"path": "...", "action": "...", "content": "..."},   ← emit when complete
        {"path": "...", "action": "...", "content": "..."},   ← emit when complete
      ]
    }

    Yields FileObject dicts as each file's content field closes.
    Never buffers the full response — safe for arbitrarily large outputs.
    """

    def feed(self, chunk: str) -> list[dict]:
        """Feed a text chunk, return any newly completed file objects."""
        ...

    @property
    def summary(self) -> str: ...

    @property
    def commit_message(self) -> str: ...
```

**Parsing strategy:** Track bracket/brace depth and string escape state.
Detect when a `files[i]` object closes (depth returns to array level) → emit.

### 2. Builder streaming method

```python
async def _generate_changes_streaming(
    self,
    task: Task,
    plan: dict,
    existing_files: dict[str, str],
    workspace: Path,
) -> dict[str, Any]:
    """Streaming variant — writes files as Claude generates them."""

    # Build prompt (same as current _generate_changes)
    system, messages = self._build_prompt(task, plan, existing_files)

    parser = StreamingJsonFileParser()
    files_written = []

    with self._anthropic.messages.stream(
        model=settings.anthropic_model,
        max_tokens=_BUILD_MAX_TOKENS,
        system=system,
        messages=messages,
    ) as stream:
        for chunk in stream.text_stream:
            completed_files = parser.feed(chunk)
            for file_obj in completed_files:
                # Write each file immediately as it completes
                written = self._apply_single_file(workspace, file_obj)
                if written:
                    files_written.append(written)
                    self._log.debug(
                        "builder.streaming.file_written",
                        path=written,
                        total_so_far=len(files_written),
                    )

    return {
        "summary": parser.summary,
        "commit_message": parser.commit_message,
        "files": [{"path": p, "action": "create", "content": ""} for p in files_written],
    }
```

### 3. Feature flag rollout

Add `FORGE_STREAMING_BUILDER=1` env var. Builder checks this flag:

```python
async def _generate_changes(self, task, plan, existing_files, workspace):
    if settings.streaming_builder_enabled:
        return await self._generate_changes_streaming(task, plan, existing_files, workspace)
    return await self._generate_changes_blocking(task, plan, existing_files, workspace)
```

Ship streaming behind the flag → validate in simulation → flip flag in prod.

---

## Benefits

| | Current (blocking) | Phase 2 (streaming) |
|---|---|---|
| Max output | 20,000 tokens (hard limit) | **Unlimited** |
| Truncation risk | Any task > 20K tokens | Zero |
| First file written | After full response | As each file completes |
| Partial recovery | None (0 files on failure) | Files already written survive |
| Token budget tuning | Required (fragile) | Not needed |
| Files/task limit | Practical ~5-10 files | Any number |

---

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Streaming parser bugs | Extensive unit tests with truncated/malformed inputs |
| Partial write on stream error | Each file is written atomically before git add |
| Claude stops mid-file | Parser detects incomplete file, logs warning, skips |
| Streaming not available in SDK version | Pin `anthropic>=0.26` (streaming stable since 0.18) |

---

## Test Plan

- `tests/unit/test_streaming_parser.py` — parser unit tests:
  - Normal complete response
  - Response truncated mid-file (parser skips incomplete file)
  - Response truncated mid-string-escape
  - Large file (> 16K tokens) completes successfully
  - Multi-file response, files emitted in order
- Integration: run full Kanban simulation with `FORGE_STREAMING_BUILDER=1`
  - Validate `files_written > 0` for all builder tasks
  - Validate no `output_tokens=20000` truncations in logs

---

## Estimated Effort

- `StreamingJsonFileParser`: ~200 lines + 150 lines tests
- Builder integration: ~80 lines
- Settings flag: ~5 lines
- Full test suite update: ~50 lines

Total: ~1-2 days engineering.
