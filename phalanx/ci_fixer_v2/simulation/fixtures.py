"""Fixture I/O + metadata schema for the simulation corpus.

Fixture on-disk layout (matches spec §11):

    tests/simulation/fixtures/<lang>/<class>/<fixture_id>/
        raw_log.txt           — full CI log, already redacted
        clone_instructions.json  — {repo, sha, branch}
        pr_context.json       — PR metadata + unified diff
        ground_truth.json     — author's actual resolution commit (optional)
        meta.json             — {language, failure_class, origin_repo, license,
                                 redaction_report, ...}

Loader helpers in this module make it trivial to iterate the corpus in
the scoring harness:

    for fixture in iter_fixtures(root):
        run_agent_against(fixture)
        score = score_fixture(fixture, outcome)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator


# ── Schema constants ──────────────────────────────────────────────────────
LANGUAGES: frozenset[str] = frozenset(
    {"python", "javascript", "typescript", "java", "csharp"}
)
FAILURE_CLASSES: frozenset[str] = frozenset(
    {"lint", "test_fail", "flake", "coverage"}
)


@dataclass
class FixtureMeta:
    """Everything in meta.json plus a few derived fields."""

    fixture_id: str
    language: str
    failure_class: str
    origin_repo: str = ""
    origin_commit_sha: str = ""
    origin_pr_number: int | None = None
    license: str = ""
    redaction_report: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def validate(self) -> None:
        if self.language not in LANGUAGES:
            raise ValueError(
                f"invalid language {self.language!r} (expected one of {sorted(LANGUAGES)})"
            )
        if self.failure_class not in FAILURE_CLASSES:
            raise ValueError(
                f"invalid failure_class {self.failure_class!r} "
                f"(expected one of {sorted(FAILURE_CLASSES)})"
            )


@dataclass
class Fixture:
    """One simulation-corpus entry loaded into memory."""

    path: Path
    meta: FixtureMeta
    raw_log: str
    pr_context: dict[str, Any] | None = None
    clone_instructions: dict[str, Any] | None = None
    ground_truth: dict[str, Any] | None = None

    @property
    def fixture_id(self) -> str:
        return self.meta.fixture_id


# ── I/O helpers ───────────────────────────────────────────────────────────


def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _read_json_or_none(p: Path) -> dict[str, Any] | None:
    if not p.exists():
        return None
    try:
        return json.loads(_read_text(p))
    except json.JSONDecodeError:
        return None


def load_fixture(fixture_dir: Path) -> Fixture:
    """Load a fixture directory into memory. Raises FileNotFoundError if
    the required files (meta.json, raw_log.txt) are missing."""
    meta_raw = _read_json_or_none(fixture_dir / "meta.json")
    if meta_raw is None:
        raise FileNotFoundError(
            f"fixture_missing_meta: {fixture_dir}"
        )
    meta = FixtureMeta(
        fixture_id=str(meta_raw.get("fixture_id") or fixture_dir.name),
        language=str(meta_raw.get("language", "")),
        failure_class=str(meta_raw.get("failure_class", "")),
        origin_repo=str(meta_raw.get("origin_repo", "")),
        origin_commit_sha=str(meta_raw.get("origin_commit_sha", "")),
        origin_pr_number=meta_raw.get("origin_pr_number"),
        license=str(meta_raw.get("license", "")),
        redaction_report=meta_raw.get("redaction_report") or {},
        notes=str(meta_raw.get("notes", "")),
    )
    meta.validate()

    raw_log_path = fixture_dir / "raw_log.txt"
    if not raw_log_path.exists():
        raise FileNotFoundError(
            f"fixture_missing_raw_log: {fixture_dir}"
        )

    return Fixture(
        path=fixture_dir,
        meta=meta,
        raw_log=_read_text(raw_log_path),
        pr_context=_read_json_or_none(fixture_dir / "pr_context.json"),
        clone_instructions=_read_json_or_none(
            fixture_dir / "clone_instructions.json"
        ),
        ground_truth=_read_json_or_none(fixture_dir / "ground_truth.json"),
    )


def save_fixture(
    root: Path,
    meta: FixtureMeta,
    raw_log: str,
    pr_context: dict[str, Any] | None = None,
    clone_instructions: dict[str, Any] | None = None,
    ground_truth: dict[str, Any] | None = None,
) -> Path:
    """Write a fixture to the corpus root. Returns the fixture directory.

    Layout: root / <language> / <failure_class> / <fixture_id> / ...
    """
    meta.validate()
    fixture_dir = root / meta.language / meta.failure_class / meta.fixture_id
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / "meta.json").write_text(
        json.dumps(asdict(meta), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (fixture_dir / "raw_log.txt").write_text(raw_log, encoding="utf-8")
    if pr_context is not None:
        (fixture_dir / "pr_context.json").write_text(
            json.dumps(pr_context, indent=2), encoding="utf-8"
        )
    if clone_instructions is not None:
        (fixture_dir / "clone_instructions.json").write_text(
            json.dumps(clone_instructions, indent=2), encoding="utf-8"
        )
    if ground_truth is not None:
        (fixture_dir / "ground_truth.json").write_text(
            json.dumps(ground_truth, indent=2), encoding="utf-8"
        )
    return fixture_dir


def iter_fixtures(
    root: Path,
    language: str | None = None,
    failure_class: str | None = None,
) -> Iterator[Fixture]:
    """Yield Fixtures from `root`, optionally filtered."""
    if not root.exists():
        return
    languages = [language] if language else sorted(LANGUAGES)
    classes = [failure_class] if failure_class else sorted(FAILURE_CLASSES)

    for lang in languages:
        lang_dir = root / lang
        if not lang_dir.is_dir():
            continue
        for cls in classes:
            cls_dir = lang_dir / cls
            if not cls_dir.is_dir():
                continue
            for fixture_dir in sorted(cls_dir.iterdir()):
                if not fixture_dir.is_dir():
                    continue
                try:
                    yield load_fixture(fixture_dir)
                except (FileNotFoundError, ValueError):
                    # Skip malformed fixtures rather than aborting the
                    # whole scoring run.
                    continue


def count_fixtures_by_class(
    root: Path, language: str | None = None
) -> dict[str, int]:
    """Quick inventory for the scoreboard header."""
    counts: dict[str, int] = {}
    for f in iter_fixtures(root, language=language):
        counts[f.meta.failure_class] = counts.get(f.meta.failure_class, 0) + 1
    return counts
