# Shadow ledger JSONL stream (P0-2)

Append-only durable evidence stream for every `shadow_ledger` row state change.
Closes blocker B1 from the [Week 1 operational report](../phase-2c-week1-operational-report-2026-05-11.md):
even if postgres is lost between scheduled dumps, every dispatch's evidence
survives in a flat file.

## What it is

- **One file**: `ledger.jsonl` at the repo root (configurable via `ledger_jsonl_path`).
- **One line per row state change**: `create_pending` → PENDING line, `link_run_id` → linking line, `update_with_results` → terminal line. A run that completes leaves 3 lines.
- **Append-only**: the writer uses `O_APPEND | O_WRONLY`. Existing lines are never mutated.
- **fsync'd after every write**: bytes are on stable storage when the call returns.
- **flock-serialized**: multiple writers (host CLI + worker container) cannot interleave bytes.
- **Crash-tolerant**: a process crash mid-write may truncate the last line. Earlier lines are intact. The verify tool flags the corruption.
- **Non-blocking on the dispatch**: if the export fails, the dispatch still completes and the verdict is durable in postgres.

## File format

Each line is one JSON object with this shape:

```json
{
  "_schema_version": 1,
  "_exported_at": "2026-05-11T20:30:00.123456+00:00",
  "_exported_by": "update_with_results",
  "_pid": 35839,
  "_phalanx_build_sha": "fef4ae2527ee",
  "row": { /* exact output of phalanx.shadow.ledger.to_dict(row) */ }
}
```

| Field                 | Meaning                                                                                  |
| --------------------- | ---------------------------------------------------------------------------------------- |
| `_schema_version`     | Integer. Bumped only on incompatible row-format change.                                  |
| `_exported_at`        | UTC ISO 8601. When this line was written. Distinct from `row.updated_at` (DB commit).    |
| `_exported_by`        | Name of the write site: `create_pending`, `link_run_id`, `update_with_results`, `replay`. |
| `_pid`                | OS process ID of the writer — helpful when triaging concurrent-writer races.             |
| `_phalanx_build_sha`  | Short git SHA at module load. `unknown` if not in a git repo.                            |
| `row`                 | Unmodified output of `to_dict(db_row)` — byte-equal to the DB row.                       |

The outer `_*` keys are export metadata. The inner `row` is the audit-grade
snapshot of DB state at commit time. Audit tools can compare
`entry["row"] == to_dict(db_row)` directly.

## Operator commands

```bash
make ledger-tail        # pretty-print last 20 lines
make ledger-stats       # summary: counts, unique ledger_ids, verdict breakdown
make ledger-verify      # strict integrity check; exits 1 on any corrupt line
make ledger-replay LEDGER_REPLAY_CONFIRM=1 [LEDGER_REPLAY_LIMIT=N]
                        # backfill ledger.jsonl from the live DB
```

## Daily check (operator)

```bash
make ledger-stats       # confirm line count growth matches expected dispatches
```

If `corrupt lines` > 0, run:

```bash
make ledger-verify 2>&1 | tail -20
```

Each corrupt line is printed with `line_no` and `byte_offset` — you can
inspect with `sed -n "${line_no}p" ledger.jsonl` or excise with `dd`.
Earlier evidence is unaffected.

## After a data-loss event

If the live DB was restored from a postgres dump but the JSONL is out of sync:

```bash
# 1. Move the existing JSONL aside (don't delete — diff is useful later)
mv ledger.jsonl ledger.jsonl.pre-restore

# 2. Replay every shadow_ledger row from the DB
LEDGER_REPLAY_CONFIRM=1 make ledger-replay

# 3. Verify the result
make ledger-verify
```

Replay APPENDS — it does not overwrite. The `_exported_by` field on
replay-emitted lines is `replay`, so you can tell which lines came from
the original real-time write vs. a backfill.

## Where `ledger.jsonl` is written

- **From the worker container**: at `/app/ledger.jsonl` (the docker-compose `.:/app` mount points back to the repo).
- **From the host CLI** (`python -m phalanx.shadow.cli run …`): at `<repo>/ledger.jsonl`.

Both resolve to the same inode through the volume mount. The setting
`ledger_jsonl_path` is a relative path anchored to the repo root, so the
same value works in both contexts.

To override: `export PHALANX_LEDGER_JSONL_PATH=/some/abs/path/ledger.jsonl`
(pydantic-settings env var, kebab-cased automatically).

## Durability semantics

The append happens **after** the DB transaction commits. Concretely:

```
session.commit()         # row durable in postgres
session.refresh(row)     # row object reflects DB state
append_ledger_row_async  # line appended + fsync'd
```

This ordering gives a clean recovery story:

| Scenario                                  | DB state | JSONL state          | Resolution                       |
| ----------------------------------------- | -------- | -------------------- | -------------------------------- |
| Append succeeds                           | row OK   | line OK              | normal                           |
| Crash between commit and append           | row OK   | line missing         | `make ledger-replay` backfills   |
| Append fails (disk full, perms)           | row OK   | line missing         | structured error log + replay    |
| Crash mid-write                           | row OK   | last line truncated  | verify tool flags it; replay     |
| DB restored from dump (data older)        | older    | newer                | Lines newer than DB are evidence; reconcile manually |

The DB → JSONL replay is always lossless and idempotent — re-running
appends fresh lines, never mutates existing ones.

## Crash-consistency guarantees (what we promise)

1. **Every committed shadow_ledger row state-change attempts a JSONL append.** Three commit sites, three append calls.
2. **The append never fails the dispatch.** Any error is logged as `ledger_jsonl.export_failed` and swallowed; the dispatch's verdict has already landed in postgres.
3. **The JSONL on disk is always a prefix-consistent subset of the DB**, modulo a possibly-truncated last line. Earlier lines are always intact.
4. **The fsync is real.** On systems with honest disk firmware, the bytes are on stable storage when `append_ledger_row_*` returns.
5. **Concurrent writers serialize.** `fcntl.LOCK_EX` covers the entire write+fsync. Two processes appending at the same time get clean, distinct lines.

## What we do NOT promise

1. **No detection of "DB has row but JSONL missing"**. The replay tool exists; running it is operator-driven. P2 may add an automatic reconciliation pass.
2. **No remote upload.** The JSONL stays on the host. Pair it with P0-1 backups for off-host coverage.
3. **No retention/rotation.** The file grows unbounded. At current cadence (~10 lines/dispatch, ~10 dispatches/week) that's <1MB/year — not a near-term concern. P2 may add weekly rotation.
4. **No append-with-network-write semantics.** This is a local file. Don't bind the file to a network filesystem and expect honest fsync.
5. **Secrets are NOT redacted.** The dump may contain whatever lives in the row (proposed_patch text, root_cause text). Don't share `ledger.jsonl` outside the trust boundary.

## Tests

[tests/unit/shadow/test_ledger_jsonl_export.py](../../tests/unit/shadow/test_ledger_jsonl_export.py)
covers:

- Line shape (schema version, provenance fields, sort_keys, byte-identity with `to_dict`).
- Append-only behavior (two appends → two lines, pre-existing content preserved, parent dir auto-created).
- Failure semantics (returns False on open/serialization failure, async wrapper same).
- Durability contract (`fsync` actually called, `O_APPEND` flag used).
- Corruption resilience (truncated trailing line does not invalidate earlier lines, verify tool flags it).
- Concurrency (32 parallel threads produce 32 clean lines, no interleaving).
- Schema stability (version is pinned, top-level keys are exactly the documented 6).

Run with: `.venv/bin/python -m pytest tests/unit/shadow/test_ledger_jsonl_export.py -v`

## Schema migrations

If the `row` shape changes — for example when P0-5 lands the
`phalanx_provenance` column — bump `LEDGER_JSONL_SCHEMA_VERSION` in
[phalanx/shadow/ledger_export.py](../../phalanx/shadow/ledger_export.py)
and:

1. Update the `EXPECTED_VERSION` constant in `TestSchemaStability`.
2. Update the doc above with the new line shape.
3. Note in this section what changed and the migration recipe for old lines.

Schema migrations are append-only — never rewrite history. The version
field tells readers what to expect per line.
