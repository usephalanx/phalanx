#!/usr/bin/env python3
"""Render the 16 scorecard fixtures into a static JSON the website consumes.

Reads tests/fixtures/scorecard/<lang>/<cell>.json, pulls the fields a
public evidence card needs (failure trigger, agent plan, verification
command, commit SHA + URL, verdict, tokens, cost), and writes a single
~/usephalanx-website/scorecard.json.

No backend, no LLM call — pure data transformation. Run after recording
any new fixture row; commit the output to the website repo and redeploy.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Per-run cost/tokens map (keyed by ci_fix_run_id) ─────────────────────────
# Populated from a direct prod DB query (see docs/scorecard_cost_source.md if
# regenerated). Source of truth is ci_fix_runs.tokens_used +
# cost_breakdown_json.total_cost_usd. Refresh when a new row lands.
_RUN_COSTS: dict[str, dict[str, float | int]] = {
    # Python row
    "8409f5b3-72df-4f47-a68d-81ca6da1ad35": {"tokens": 63774, "cost_usd": 0.2320},
    "c78d2c68-b294-4882-9585-2242c726a526": {"tokens": 48890, "cost_usd": 0.1840},
    "02e770d9-ccde-4b9c-ba8b-4098ee5a98d9": {"tokens": 56446, "cost_usd": 0.2181},
    "eb3ccf1b-acb2-4ae4-bf9c-105b632e0c82": {"tokens": 45503, "cost_usd": 0.1687},
    # TS row
    "0a1002fa-ab4f-4030-a5ff-d27944e10ad1": {"tokens": 70645, "cost_usd": 0.2577},
    "107261a1-582e-4a25-8997-747f3a23cb71": {"tokens": 81077, "cost_usd": 0.2793},
    "c3af06ff-b023-491c-80f7-298a7a5841ed": {"tokens": 239770, "cost_usd": 0.7963},
    "3b6c1478-ad02-4f3f-83bb-08992fe0162e": {"tokens": 80967, "cost_usd": 0.2904},
    # JS row
    "0229e359-7977-49f4-aa1a-c24f250165d6": {"tokens": 115099, "cost_usd": 0.4170},
    "de95e5c1-307f-45f0-867f-3b91cdd24958": {"tokens": 46346, "cost_usd": 0.1680},
    "43d771af-f904-4357-ac2b-226d7435d9d2": {"tokens": 51573, "cost_usd": 0.1877},
    "df760e68-e76e-456e-8a82-e30b6763edd8": {"tokens": 71420, "cost_usd": 0.2532},
    # Java row
    "26e9fced-5efa-454e-9bec-6f37a13932fb": {"tokens": 46392, "cost_usd": 0.1657},
    "6621edff-499e-4949-9c31-ed5efbf9731f": {"tokens": 76360, "cost_usd": 0.2685},
    "2b1925c9-ab6e-46f4-a372-3f1fe79f981b": {"tokens": 62031, "cost_usd": 0.2353},
    "114ea055-fc84-41c5-a6bf-1cfa876c0e2d": {"tokens": 64876, "cost_usd": 0.2337},
    # C# row
    "0724b28e-fd35-4f41-bd68-f450cceda8e4": {"tokens": 51243, "cost_usd": 0.1801},
    "b52d07b2-9699-4a94-a462-4edf28247413": {"tokens": 72240, "cost_usd": 0.2513},
    "f9d7b2c8-4e26-4ca5-ae37-0dcf4eca073a": {"tokens": 49924, "cost_usd": 0.1882},
    "159a2206-6133-422f-9aa6-fb273e390b67": {"tokens": 73073, "cost_usd": 0.2541},
}

# Languages whose record-branch commits are no longer reachable anonymously.
# Renders the SHA as muted text with a "replay-pinned" note instead of a
# dead GitHub link. Python fixtures predate the branch-retention policy.
_LANG_NO_LINK: set[str] = {"python"}

_CELL_ORDER: list[str] = ["lint", "test_fail", "flake", "coverage"]
_LANG_ORDER: list[str] = ["python", "js", "ts", "java", "csharp"]

_CELL_DISPLAY: dict[str, str] = {
    "lint": "Lint",
    "test_fail": "Test assertion",
    "flake": "Flake (sleep / jitter)",
    "coverage": "Coverage drop",
}

_LANG_DISPLAY: dict[str, str] = {
    "python": "Python",
    "js": "JavaScript",
    "ts": "TypeScript",
    "java": "Java",
    "csharp": "C#",
}


# ─────────────────────────────────────────────────────────────────────────────
# Real-world runs — agent fixes on external OSS repos (NOT our own testbeds).
# These are the "we didn't tune the benchmark to ourselves" proof points.
# Hardcoded because they're rare events (one per major canary). When a new
# real-world run lands, append an entry. Source-of-truth fields come from
# the prod runs/tasks tables — see /tmp/v3-canary-data.json shape.
# ─────────────────────────────────────────────────────────────────────────────
_REAL_WORLD_RUNS: list[dict] = [
    {
        "id": "humanize-pr-2-2026-04-24",
        "label": "First v3 commit on an external OSS repo",
        "date": "2026-04-24",
        "upstream_repo": "python-humanize/humanize",
        "fork_repo": "usephalanx/humanize",
        "pr_number": 2,
        "pr_url": "https://github.com/usephalanx/humanize/pull/2",
        "failing_check": "ruff E501 (long line)",
        "headline_commit": {
            "sha": "75b624a",
            "url": "https://github.com/usephalanx/humanize/commit/75b624a",
            "files_modified": ["src/humanize/number.py"],
            "tokens_used": 13752,
            "diff_summary": "Replaced a 143-char canary comment with a concise 50-char one.",
        },
        "pipeline": [
            {"role": "cifix_sre", "mode": "setup", "summary": "Cloned humanize, env_detector picked python:3.10-slim from requires-python>=3.10, provisioned fresh container with workflow-derived deps."},
            {"role": "cifix_techlead", "summary": "GPT-5.4 read the CI log, identified ruff E501 at filesize.py line 3, narrowed the failing_command to `ruff check src/humanize/number.py`."},
            {"role": "cifix_engineer", "summary": "Sonnet edited the line, ran the narrow failing_command in sandbox (exit 0), commit_and_push succeeded."},
        ],
        "honest_footnote": (
            "v3's full-CI verify (cifix_sre verify mode) ran the broader workflow "
            "and caught cascading failures from upstream's `prek`/`uv` toolchain "
            "that our minimal sandbox doesn't ship — so the system iterated. "
            "Iteration 2 over-reached and patched the workflow files themselves, "
            "which a real maintainer wouldn't accept. The commit shown here is "
            "iteration 1 only — a clean one-line fix to the actual reported "
            "error. The over-reach in iteration 2 is a tracked Phase-2 prompt "
            "issue (TL should recognize 'sandbox env mismatch' and escalate "
            "instead of editing CI infra). v3's verification gate held both "
            "times — every commit went through a green sandbox run before push."
        ),
    },
]


def _find_tool(calls: list[dict], name: str) -> dict | None:
    for tc in calls:
        if tc.get("tool_name") == name:
            return tc
    return None


def _find_last_tool(calls: list[dict], name: str) -> dict | None:
    last = None
    for tc in calls:
        if tc.get("tool_name") == name:
            last = tc
    return last


def _find_successful_delegate(calls: list[dict]) -> dict | None:
    """Return the last delegate_to_coder call whose subagent succeeded."""
    best = None
    for tc in calls:
        if tc.get("tool_name") != "delegate_to_coder":
            continue
        res = tc.get("tool_result") or {}
        if res.get("success") and res.get("sandbox_exit_code") == 0 and res.get("failing_command_matched"):
            best = tc
    return best


def _verification_sandbox_run(calls: list[dict]) -> dict | None:
    """Return the run_in_sandbox call that gated commit_and_push.

    The agent's verification gate requires a successful sandbox run of the
    original failing command before commit is allowed, so the LAST
    run_in_sandbox with exit 0 preceding commit_and_push is the gate.
    Falls back to the last run_in_sandbox if no exit-0 pre-commit match.
    """
    commit_turn = None
    for tc in calls:
        if tc.get("tool_name") == "commit_and_push":
            commit_turn = tc.get("turn")
            break
    best = None
    for tc in calls:
        if tc.get("tool_name") != "run_in_sandbox":
            continue
        if commit_turn is not None and tc.get("turn", 0) >= commit_turn:
            continue
        res = tc.get("tool_result") or {}
        if res.get("exit_code") == 0:
            best = tc
    return best or _find_last_tool(calls, "run_in_sandbox")


def _build_cell(lang: str, cell: str, fixture_path: Path) -> dict:
    data = json.loads(fixture_path.read_text())
    ic = data.get("initial_context") or {}
    eo = data.get("expected_outcome") or {}
    tcs = data.get("tool_calls") or []

    # Some cells invoke delegate_to_coder multiple times (coder retried).
    # The gate that matters is the LAST successful call, since that's what
    # ultimately unblocked commit_and_push.
    delegate = _find_successful_delegate(tcs) or _find_last_tool(tcs, "delegate_to_coder") or {}
    del_input = delegate.get("tool_input") or {}
    del_result = delegate.get("tool_result") or {}

    run_sb = _verification_sandbox_run(tcs) or {}
    sb_input = run_sb.get("tool_input") or {}
    sb_result = run_sb.get("tool_result") or {}

    # Sandbox verification can come from two places:
    #   1. The coder subagent re-runs the failing command and returns
    #      `sandbox_exit_code` + `failing_command_matched` in its result.
    #      This is the common path — the subagent owns verification.
    #   2. The main agent explicitly calls `run_in_sandbox` itself (less
    #      common; usually a retry loop or extra-careful cell).
    # Either one counts; we check both, preferring the coder result for
    # the displayed exit code since that's what gated commit_and_push.
    coder_sb_exit = del_result.get("sandbox_exit_code")
    coder_sb_matched = bool(del_result.get("failing_command_matched"))
    main_sb_exit = sb_result.get("exit_code")
    verified = (coder_sb_exit == 0 and coder_sb_matched) or (main_sb_exit == 0)
    verify_exit = coder_sb_exit if coder_sb_exit is not None else main_sb_exit

    commit_sha = eo.get("committed_sha") or ""
    repo = ic.get("repo") or ""
    commit_public = bool(commit_sha) and lang not in _LANG_NO_LINK
    commit_url = (
        f"https://github.com/{repo}/commit/{commit_sha}" if commit_public else None
    )

    cost_entry = _RUN_COSTS.get(eo.get("ci_fix_run_id") or "", {})

    plan_raw = (del_input.get("task_description") or "").strip()
    plan_trimmed = (plan_raw[:260] + "…") if len(plan_raw) > 260 else plan_raw

    return {
        "lang": lang,
        "lang_display": _LANG_DISPLAY.get(lang, lang),
        "cell": cell,
        "cell_display": _CELL_DISPLAY.get(cell, cell),
        "label": f"{_LANG_DISPLAY.get(lang, lang)} · {_CELL_DISPLAY.get(cell, cell)}",
        "repo": repo,
        "repo_url": f"https://github.com/{repo}" if repo else None,
        "pr_number": ic.get("pr"),
        "failing_job": ic.get("failing_job_name") or "",
        "failing_command": ic.get("failing_command") or "",
        "plan": plan_trimmed,
        "target_files": del_input.get("target_files") or [],
        "verified_in_sandbox": verified,
        "verify_command": sb_input.get("command") or ic.get("failing_command") or "",
        "verify_exit_code": verify_exit,
        "verify_source": (
            "coder_subagent" if coder_sb_exit == 0 and coder_sb_matched
            else ("main_agent" if main_sb_exit == 0 else None)
        ),
        "verdict": eo.get("verdict") or "unknown",
        "escalation_reason": eo.get("escalation_reason"),
        "commit_sha": commit_sha,
        "commit_short": commit_sha[:7] if commit_sha else "",
        "commit_url": commit_url,
        "commit_public": commit_public,
        "tokens_used": cost_entry.get("tokens"),
        "cost_usd": cost_entry.get("cost_usd"),
    }


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    fx_root = repo_root / "tests" / "fixtures" / "scorecard"
    out_path = Path.home() / "usephalanx-website" / "scorecard.json"

    if not fx_root.is_dir():
        print(f"fixture root missing: {fx_root}", file=sys.stderr)
        return 1
    if not out_path.parent.is_dir():
        print(f"website repo missing: {out_path.parent}", file=sys.stderr)
        return 1

    cells: list[dict] = []
    for lang in _LANG_ORDER:
        lang_dir = fx_root / lang
        if not lang_dir.is_dir():
            continue
        for cell in _CELL_ORDER:
            fp = lang_dir / f"{cell}.json"
            if not fp.exists():
                continue
            cells.append(_build_cell(lang, cell, fp))

    total_cost = sum((c.get("cost_usd") or 0) for c in cells)
    total_tokens = sum((c.get("tokens_used") or 0) for c in cells)
    committed = sum(1 for c in cells if c["verdict"] == "committed")

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "total_cells": len(cells),
            "committed": committed,
            "target_total_cells": 20,  # 5 langs × 4 cells — locked MVP scope
            "total_cost_usd": round(total_cost, 4),
            "total_tokens": total_tokens,
            "avg_cost_per_cell_usd": round(total_cost / len(cells), 4) if cells else 0,
            "languages_complete": sum(
                1 for lang in _LANG_ORDER
                if sum(1 for c in cells if c["lang"] == lang) == len(_CELL_ORDER)
            ),
        },
        "cells": cells,
        "real_world_runs": _REAL_WORLD_RUNS,
    }

    out_path.write_text(json.dumps(doc, indent=2))
    print(
        f"[scorecard] wrote {out_path} "
        f"({len(cells)} cells, {committed} committed, ${total_cost:.2f} total)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
