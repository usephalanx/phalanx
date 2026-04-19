"""Unit tests for simulation.fixtures — on-disk schema + I/O."""

from __future__ import annotations

import json

import pytest

from phalanx.ci_fixer_v2.simulation.fixtures import (
    FAILURE_CLASSES,
    LANGUAGES,
    FixtureMeta,
    count_fixtures_by_class,
    iter_fixtures,
    load_fixture,
    save_fixture,
)


def _meta(**overrides) -> FixtureMeta:
    defaults = dict(
        fixture_id="ruff-001",
        language="python",
        failure_class="lint",
        origin_repo="astral-sh/ruff",
        origin_commit_sha="abc123",
        origin_pr_number=42,
        license="MIT",
    )
    defaults.update(overrides)
    return FixtureMeta(**defaults)


def test_meta_validate_accepts_valid_values():
    _meta().validate()  # no exception


def test_meta_validate_rejects_unknown_language():
    with pytest.raises(ValueError, match="language"):
        _meta(language="cobol").validate()


def test_meta_validate_rejects_unknown_failure_class():
    with pytest.raises(ValueError, match="failure_class"):
        _meta(failure_class="infra").validate()


def test_schema_constants_are_consistent_with_spec():
    # Top-5 languages + 4 failure classes per spec §11.
    assert LANGUAGES == {"python", "javascript", "typescript", "java", "csharp"}
    assert FAILURE_CLASSES == {"lint", "test_fail", "flake", "coverage"}


def test_save_and_load_round_trip(tmp_path):
    meta = _meta()
    fixture_dir = save_fixture(
        root=tmp_path,
        meta=meta,
        raw_log="E501 line too long\n",
        pr_context={"title": "fix lint", "body": "..."},
        clone_instructions={
            "repo": "astral-sh/ruff",
            "sha": "abc123",
            "branch": "main",
        },
        ground_truth={"fix_commit_sha": "def456"},
    )
    assert fixture_dir == tmp_path / "python" / "lint" / "ruff-001"
    loaded = load_fixture(fixture_dir)
    assert loaded.meta.fixture_id == "ruff-001"
    assert loaded.meta.language == "python"
    assert loaded.meta.failure_class == "lint"
    assert loaded.raw_log == "E501 line too long\n"
    assert loaded.pr_context == {"title": "fix lint", "body": "..."}
    assert loaded.clone_instructions["sha"] == "abc123"
    assert loaded.ground_truth == {"fix_commit_sha": "def456"}


def test_save_fixture_only_writes_provided_optionals(tmp_path):
    meta = _meta()
    save_fixture(root=tmp_path, meta=meta, raw_log="log")
    fixture_dir = tmp_path / "python" / "lint" / "ruff-001"
    assert (fixture_dir / "meta.json").exists()
    assert (fixture_dir / "raw_log.txt").exists()
    assert not (fixture_dir / "pr_context.json").exists()
    assert not (fixture_dir / "clone_instructions.json").exists()
    assert not (fixture_dir / "ground_truth.json").exists()


def test_load_fixture_missing_meta_raises(tmp_path):
    (tmp_path / "raw_log.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="fixture_missing_meta"):
        load_fixture(tmp_path)


def test_load_fixture_missing_raw_log_raises(tmp_path):
    (tmp_path / "meta.json").write_text(
        json.dumps(
            {
                "fixture_id": "x",
                "language": "python",
                "failure_class": "lint",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError, match="fixture_missing_raw_log"):
        load_fixture(tmp_path)


def test_iter_fixtures_yields_across_languages_and_classes(tmp_path):
    save_fixture(
        root=tmp_path,
        meta=_meta(fixture_id="py-lint-1", language="python", failure_class="lint"),
        raw_log="",
    )
    save_fixture(
        root=tmp_path,
        meta=_meta(fixture_id="js-test-1", language="javascript", failure_class="test_fail"),
        raw_log="",
    )
    save_fixture(
        root=tmp_path,
        meta=_meta(fixture_id="py-flake-1", language="python", failure_class="flake"),
        raw_log="",
    )
    ids = sorted(f.fixture_id for f in iter_fixtures(tmp_path))
    assert ids == ["js-test-1", "py-flake-1", "py-lint-1"]


def test_iter_fixtures_filters_by_language(tmp_path):
    save_fixture(
        root=tmp_path,
        meta=_meta(fixture_id="py-1", language="python", failure_class="lint"),
        raw_log="",
    )
    save_fixture(
        root=tmp_path,
        meta=_meta(fixture_id="java-1", language="java", failure_class="test_fail"),
        raw_log="",
    )
    py_only = list(iter_fixtures(tmp_path, language="python"))
    assert len(py_only) == 1
    assert py_only[0].fixture_id == "py-1"


def test_iter_fixtures_filters_by_failure_class(tmp_path):
    save_fixture(
        root=tmp_path,
        meta=_meta(fixture_id="l1", language="python", failure_class="lint"),
        raw_log="",
    )
    save_fixture(
        root=tmp_path,
        meta=_meta(fixture_id="t1", language="python", failure_class="test_fail"),
        raw_log="",
    )
    lints = list(iter_fixtures(tmp_path, failure_class="lint"))
    assert len(lints) == 1
    assert lints[0].fixture_id == "l1"


def test_iter_fixtures_skips_malformed(tmp_path):
    # Make a valid fixture plus a malformed directory.
    save_fixture(
        root=tmp_path,
        meta=_meta(fixture_id="ok"),
        raw_log="",
    )
    bad = tmp_path / "python" / "lint" / "broken"
    bad.mkdir(parents=True)
    (bad / "meta.json").write_text("not-json", encoding="utf-8")
    results = list(iter_fixtures(tmp_path))
    # Only the valid fixture is yielded.
    assert len(results) == 1
    assert results[0].fixture_id == "ok"


def test_count_fixtures_by_class(tmp_path):
    for i in range(3):
        save_fixture(
            root=tmp_path,
            meta=_meta(fixture_id=f"l{i}", failure_class="lint"),
            raw_log="",
        )
    save_fixture(
        root=tmp_path,
        meta=_meta(fixture_id="t1", failure_class="test_fail"),
        raw_log="",
    )
    counts = count_fixtures_by_class(tmp_path, language="python")
    assert counts == {"lint": 3, "test_fail": 1}


def test_iter_fixtures_empty_root(tmp_path):
    # Missing root directory returns an empty iterator, not an error.
    results = list(iter_fixtures(tmp_path / "nope"))
    assert results == []
