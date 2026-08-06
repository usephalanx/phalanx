"""P0-2 — ledger.jsonl export: atomicity, swallow-on-failure, corruption resilience.

The DB tests for `create_pending` + `update_with_results` writing rows are
covered elsewhere. This file isolates the export module's contract:

  - line shape is stable (_schema_version, _exported_at, _exported_by, row)
  - row is byte-identical to to_dict (audit tools depend on this)
  - never raises on failure (returns False instead)
  - concurrent appends serialize via flock (no byte-interleaving)
  - a truncated trailing line does not poison earlier valid lines
  - fsync is actually called (assert via monkeypatch counter)
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from phalanx.shadow import ledger_export


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sample_row(**overrides) -> dict:
    """Minimal row dict matching to_dict output."""
    base = {
        "id": "00000000-0000-0000-0000-000000000001",
        "repo": "python/mypy",
        "workflow_run_id": 25513763243,
        "attempt_number": 1,
        "pr_number": 21414,
        "failing_commit_sha": "abc123",
        "failure_class": None,
        "phalanx_run_id": "run-1",
        "phalanx_verdict": "SHIPPED_PROPOSED",
        "phalanx_confidence": 0.9,
        "phalanx_proposed_patch": None,
        "phalanx_root_cause": "narrowing missing",
        "phalanx_affected_files": ["mypy/typeanal.py"],
        "phalanx_iterations": 1,
        "phalanx_tool_calls": 8,
        "phalanx_cost_usd": 0.63,
        "phalanx_run_seconds": 245,
        "ground_truth_status": "pending",
        "maintainer_fix_commit_sha": None,
        "maintainer_actual_patch": None,
        "notes": "run_status=SHIPPED",
        "created_at": "2026-05-11T20:15:05+00:00",
        "updated_at": "2026-05-11T20:19:07+00:00",
    }
    base.update(overrides)
    return base


@pytest.fixture
def tmp_jsonl(tmp_path, monkeypatch):
    """Point the export module at a tmp file, return the Path."""
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(ledger_export, "_resolve_path", lambda: path)
    # Clear the build-sha cache so we don't drag state across tests.
    ledger_export._build_sha.cache_clear()
    return path


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            out.append(json.loads(raw))
    return out


# ── Line shape ────────────────────────────────────────────────────────────────


class TestLineShape:
    def test_one_append_one_line(self, tmp_jsonl):
        ok = ledger_export.append_ledger_row_sync(_sample_row(), exported_by="t")
        assert ok is True
        lines = _read_lines(tmp_jsonl)
        assert len(lines) == 1

    def test_schema_version_present(self, tmp_jsonl):
        ledger_export.append_ledger_row_sync(_sample_row(), exported_by="t")
        entry = _read_lines(tmp_jsonl)[0]
        assert entry["_schema_version"] == ledger_export.LEDGER_JSONL_SCHEMA_VERSION

    def test_provenance_fields_present(self, tmp_jsonl):
        """_exported_at, _exported_by, _pid, _phalanx_build_sha — required."""
        ledger_export.append_ledger_row_sync(_sample_row(), exported_by="create_pending")
        entry = _read_lines(tmp_jsonl)[0]
        assert "_exported_at" in entry and entry["_exported_at"].endswith("+00:00")
        assert entry["_exported_by"] == "create_pending"
        assert entry["_pid"] == os.getpid()
        assert "_phalanx_build_sha" in entry

    def test_row_is_byte_identical_to_to_dict_input(self, tmp_jsonl):
        """Audit tools compare entry['row'] to to_dict(db_row) — must match."""
        row = _sample_row(repo="agronholm/anyio")
        ledger_export.append_ledger_row_sync(row, exported_by="t")
        entry = _read_lines(tmp_jsonl)[0]
        assert entry["row"] == row

    def test_keys_are_sorted_for_stable_diffs(self, tmp_jsonl):
        ledger_export.append_ledger_row_sync(_sample_row(), exported_by="t")
        raw = tmp_jsonl.read_text().rstrip()
        # First-level keys should appear in sorted order in the raw bytes.
        first_under = raw.index('"_exported_at"')
        last_under = raw.index('"_phalanx_build_sha"')
        row_key = raw.index('"row"')
        assert first_under < last_under < row_key


# ── Append behavior ───────────────────────────────────────────────────────────


class TestAppendIsAppendOnly:
    def test_two_appends_produce_two_lines(self, tmp_jsonl):
        ledger_export.append_ledger_row_sync(_sample_row(id="a"), exported_by="t1")
        ledger_export.append_ledger_row_sync(_sample_row(id="b"), exported_by="t2")
        lines = _read_lines(tmp_jsonl)
        assert [e["row"]["id"] for e in lines] == ["a", "b"]
        assert [e["_exported_by"] for e in lines] == ["t1", "t2"]

    def test_does_not_overwrite_existing_content(self, tmp_jsonl):
        tmp_jsonl.write_text('{"pre-existing": "noise"}\n')
        ledger_export.append_ledger_row_sync(_sample_row(), exported_by="t")
        # File now has 2 lines: the noise + our append.
        raw = tmp_jsonl.read_text()
        assert raw.startswith('{"pre-existing":')
        assert raw.count("\n") == 2

    def test_creates_parent_directory_if_missing(self, tmp_path, monkeypatch):
        nested = tmp_path / "a" / "b" / "ledger.jsonl"
        monkeypatch.setattr(ledger_export, "_resolve_path", lambda: nested)
        ok = ledger_export.append_ledger_row_sync(_sample_row(), exported_by="t")
        assert ok and nested.exists()


# ── Failure semantics ─────────────────────────────────────────────────────────


class TestFailureNeverRaises:
    def test_returns_false_on_open_failure(self, tmp_path, monkeypatch):
        """If the target is unwritable, must return False — not raise."""
        bad = tmp_path / "missing_parent_with_no_perm" / "ledger.jsonl"
        monkeypatch.setattr(ledger_export, "_resolve_path", lambda: bad)
        # Sabotage mkdir so the parent can't be created either.
        def boom(*a, **kw): raise PermissionError("denied")
        monkeypatch.setattr(Path, "mkdir", boom)
        ok = ledger_export.append_ledger_row_sync(_sample_row(), exported_by="t")
        assert ok is False  # logged, not raised

    def test_returns_false_on_serialization_failure(self, tmp_jsonl):
        """A non-JSON-serializable row must be swallowed, not raised."""
        row = _sample_row()
        row["not_serializable"] = object()
        ok = ledger_export.append_ledger_row_sync(row, exported_by="t")
        assert ok is False
        # And the file is not corrupted with a partial line.
        if tmp_jsonl.exists():
            assert tmp_jsonl.read_text() == ""

    def test_async_wrapper_also_never_raises(self, tmp_path, monkeypatch):
        bad = tmp_path / "x" / "ledger.jsonl"
        monkeypatch.setattr(ledger_export, "_resolve_path", lambda: bad)
        monkeypatch.setattr(Path, "mkdir", lambda *a, **kw: (_ for _ in ()).throw(PermissionError("denied")))

        async def run():
            return await ledger_export.append_ledger_row_async(_sample_row(), exported_by="t")

        result = asyncio.run(run())
        assert result is False


# ── Durability semantics ──────────────────────────────────────────────────────


class TestDurabilityContract:
    def test_fsync_is_called(self, tmp_jsonl, monkeypatch):
        """fsync must run before the file descriptor is closed — otherwise
        the bytes may sit in OS buffers and be lost on power failure."""
        fsync_calls: list[int] = []
        original_fsync = os.fsync

        def tracked_fsync(fd):
            fsync_calls.append(fd)
            return original_fsync(fd)

        monkeypatch.setattr(os, "fsync", tracked_fsync)
        ledger_export.append_ledger_row_sync(_sample_row(), exported_by="t")
        assert len(fsync_calls) == 1

    def test_o_append_flag_used(self, tmp_jsonl, monkeypatch):
        """The open mode must include O_APPEND so concurrent writers can't
        clobber each other's offsets."""
        observed_flags: list[int] = []
        original_open = os.open

        def tracked_open(p, flags, mode=0o644, **kw):
            if str(p) == str(tmp_jsonl):
                observed_flags.append(flags)
            return original_open(p, flags, mode, **kw)

        monkeypatch.setattr(os, "open", tracked_open)
        ledger_export.append_ledger_row_sync(_sample_row(), exported_by="t")
        assert observed_flags, "open() was not called on the ledger path"
        assert observed_flags[0] & os.O_APPEND, "O_APPEND missing — concurrent-safe append broken"
        assert observed_flags[0] & os.O_CREAT, "O_CREAT missing — first write fails on missing file"


# ── Corruption resilience ─────────────────────────────────────────────────────


class TestCorruptionResilience:
    def test_truncated_trailing_line_does_not_poison_prior_lines(self, tmp_jsonl):
        """Simulate a crash mid-append: write 2 valid lines, then a
        partial third line with no trailing newline + invalid JSON.
        The verify tool must report 2 valid + 1 corrupt."""
        ledger_export.append_ledger_row_sync(_sample_row(id="a"), exported_by="t1")
        ledger_export.append_ledger_row_sync(_sample_row(id="b"), exported_by="t2")
        # Append a deliberately-corrupt partial line (no closing brace, no newline)
        with tmp_jsonl.open("ab") as f:
            f.write(b'{"_schema_version": 1, "_exported_at": "2026-05-11', )

        # Now run the verify tool's parser logic.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ledger_jsonl_verify",
            Path(__file__).resolve().parents[3] / "scripts" / "ledger_jsonl_verify.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rc = module.verify(tmp_jsonl)
        assert rc == 1  # corrupt lines present
        # And the first 2 valid entries are still readable.
        valid = [json.loads(l) for l in tmp_jsonl.read_text().splitlines() if l.startswith("{") and l.rstrip().endswith("}")]
        assert len(valid) == 2
        assert valid[0]["row"]["id"] == "a"
        assert valid[1]["row"]["id"] == "b"


# ── Concurrency ───────────────────────────────────────────────────────────────


class TestConcurrentAppends:
    def test_n_concurrent_writers_produce_n_clean_lines(self, tmp_jsonl):
        """flock must prevent byte-interleaving even with many writers."""
        N = 32

        def worker(i: int):
            ledger_export.append_ledger_row_sync(
                _sample_row(id=f"w{i:02d}", phalanx_root_cause="x" * 1000),
                exported_by=f"worker-{i}",
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads: t.start()
        for t in threads: t.join()

        # Every line parses; we got exactly N lines; ids all present.
        lines = _read_lines(tmp_jsonl)
        assert len(lines) == N
        ids = {e["row"]["id"] for e in lines}
        assert ids == {f"w{i:02d}" for i in range(N)}


# ── Schema stability ──────────────────────────────────────────────────────────


class TestSchemaStability:
    """If LEDGER_JSONL_SCHEMA_VERSION changes, this test forces an explicit
    decision — bump the constant, update the doc, write a migration note."""

    EXPECTED_VERSION = 1
    EXPECTED_TOP_LEVEL_KEYS = {
        "_schema_version",
        "_exported_at",
        "_exported_by",
        "_pid",
        "_phalanx_build_sha",
        "row",
    }

    def test_schema_version_is_pinned(self):
        assert ledger_export.LEDGER_JSONL_SCHEMA_VERSION == self.EXPECTED_VERSION, (
            "Schema version changed — update docs/ops/ledger-jsonl.md and bump EXPECTED_VERSION."
        )

    def test_top_level_keys_are_exactly_six(self, tmp_jsonl):
        ledger_export.append_ledger_row_sync(_sample_row(), exported_by="t")
        entry = _read_lines(tmp_jsonl)[0]
        assert set(entry.keys()) == self.EXPECTED_TOP_LEVEL_KEYS
