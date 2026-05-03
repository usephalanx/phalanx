#!/usr/bin/env python3
"""v1.7 TL corpus visual demo — show INPUT + OUTPUT for each fixture.

Renders, for each fixture in the corpus:

  INPUT:
    - CI log (truncated)
    - Failing command
    - Repo files (paths + sizes)

  OUTPUT (the canned good TL output, which is what real TL is supposed to
  emit — once we wire the real GPT-5.4 run, this shows actual outputs):
    - The fix_spec summary fields (root_cause, confidence, etc.)
    - The Task rows that would land in the DB (sequence_num + agent + ...)
    - For engineer tasks: each step rendered as numbered instructions
    - For SRE setup: env_requirements key:value
    - For SRE verify: the verify command

This is the "what would happen" preview. Once real TL is wired, swap
the canned outputs for live ones and re-run — same renderer, same shape.

Usage:
  python scripts/v17_tl_corpus_demo.py            # all fixtures
  python scripts/v17_tl_corpus_demo.py 03         # only fixture 03_*
  python scripts/v17_tl_corpus_demo.py humanize   # by substring match

Note: there is currently NO LLM call. This shows the canned good outputs
that we use as the prompt-eng target. The next step is to wire real TL
and swap these for real outputs.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

# Ensure repo root on path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.integration.v3_harness.fixtures.v17_tl_corpus.harness import (  # noqa: E402
    discover_corpus,
    validate_tl_output,
)
from tests.integration.v3_harness.test_v17_tl_corpus_harness import _GOOD_OUTPUTS  # noqa: E402


# ─── Visual helpers ───────────────────────────────────────────────────────────


def hr(char: str = "─", width: int = 100) -> str:
    return char * width


def banner(title: str, char: str = "═", width: int = 100) -> str:
    """Centered title with surrounding bars."""
    pad = max(0, (width - len(title) - 2) // 2)
    return f"{char * pad} {title} {char * (width - pad - len(title) - 2)}"


def truncate(text: str, n: int = 80, suffix: str = "…") -> str:
    return text if len(text) <= n else text[: n - len(suffix)] + suffix


def indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# ─── Section renderers ────────────────────────────────────────────────────────


def render_input(fx) -> str:
    out = [banner("INPUT (what TL sees)", char="─")]
    out.append(f"  fixture           : {fx.name}")
    out.append(f"  source            : {fx.source_repo}  ({fx.source_pr_or_commit})")
    out.append(f"  complexity        : {fx.complexity}")
    out.append(f"  failing_command   : {fx.failing_command}")
    out.append(f"  failing_job_name  : {fx.failing_job_name}")
    out.append(f"  pr_number         : {fx.pr_number}")

    out.append("")
    out.append("  ci_log_text (truncated):")
    log_lines = fx.ci_log_text.splitlines()
    head = log_lines[:3]
    tail = log_lines[-7:] if len(log_lines) > 10 else log_lines[3:]
    for line in head:
        out.append(f"    │ {truncate(line, 95)}")
    if len(log_lines) > 10:
        out.append(f"    │ ... ({len(log_lines) - 10} lines elided) ...")
    for line in tail:
        out.append(f"    │ {truncate(line, 95)}")

    out.append("")
    out.append("  repo_files (visible to TL via read_file):")
    for path, content in fx.repo_files.items():
        n_lines = content.count("\n") + 1
        out.append(f"    • {path:<50} {len(content):>5}b  {n_lines:>3}L")

    return "\n".join(out)


def render_fix_spec_summary(out_dict: dict) -> str:
    out = [banner("OUTPUT — fix_spec summary", char="─")]
    keys = [
        "root_cause",
        "fix_spec",
        "affected_files",
        "failing_command",
        "verify_command",
        "verify_success",
        "confidence",
        "open_questions",
        "review_decision",
        "replan_reason",
    ]
    for k in keys:
        v = out_dict.get(k)
        if v in (None, [], ""):
            continue
        if isinstance(v, list):
            v_str = "[" + ", ".join(str(x) for x in v) + "]" if v else "[]"
        elif isinstance(v, dict):
            v_str = "{" + ", ".join(f"{kk}: {vv}" for kk, vv in v.items()) + "}"
        else:
            v_str = str(v)
        # Wrap long values
        if len(v_str) > 90:
            v_str = "\n      " + "\n      ".join(textwrap.wrap(v_str, width=90))
        out.append(f"  {k:<20}: {v_str}")
    return "\n".join(out)


def render_task_db_rows(out_dict: dict) -> str:
    """Render the Task rows commander would persist to the DB after reading
    TL's task_plan. Mirrors the actual Task model's relevant columns.
    """
    plan = out_dict.get("task_plan") or []
    if not plan:
        return banner("OUTPUT — Tasks created in DB", char="─") + "\n  (no tasks)"

    out = [banner("OUTPUT — Tasks commander would persist to DB", char="─")]
    out.append(
        "  sequence_num  agent              status   task_id  depends_on    purpose"
    )
    out.append(
        "  ────────────  ─────────────────  ───────  ───────  ────────────  ──────────────────────"
    )
    # Initial DB shape: TL plan task is seq=1 (already done by the time
    # commander reads task_plan). The plan tasks land at seq=2..N.
    out.append(
        f"  {1:<12}  {'cifix_techlead':<17}  {'COMPLETED':<7}  {'T1':<7}  "
        f"{'(initial)':<12}  diagnose + emit task_plan"
    )
    for i, ts in enumerate(plan, start=2):
        agent = ts.get("agent") or "?"
        tid = ts.get("task_id") or f"T{i}"
        deps = ts.get("depends_on") or []
        deps_str = ",".join(deps) if deps else "-"
        purpose = truncate(ts.get("purpose") or "?", 30)
        out.append(
            f"  {i:<12}  {agent:<17}  {'PENDING':<7}  {tid:<7}  {deps_str:<12}  {purpose}"
        )
    return "\n".join(out)


def render_engineer_steps(out_dict: dict) -> str:
    out = []
    plan = out_dict.get("task_plan") or []
    for i, ts in enumerate(plan, start=2):
        if ts.get("agent") != "cifix_engineer":
            continue
        out.append("")
        out.append(banner(
            f"  ENGINEER TASK seq={i} ({ts.get('task_id')}) — exact steps  ",
            char="·",
        ))
        out.append(f"    purpose: {ts.get('purpose')}")
        for j, step in enumerate(ts.get("steps") or [], start=1):
            action = step.get("action")
            label = f"step {step.get('id', j)}/{action}"
            if action == "read":
                out.append(f"    {label}: read_file({step['file']!r})")
            elif action == "replace":
                out.append(f"    {label}: in {step['file']!r}")
                out.append(f"      OLD: {truncate(repr(step['old']), 90)}")
                out.append(f"      NEW: {truncate(repr(step['new']), 90)}")
            elif action == "insert":
                out.append(f"    {label}: insert into {step['file']!r} after line {step.get('after_line')}")
                out.append(f"      CONTENT: {truncate(repr(step.get('content', '')), 90)}")
            elif action == "delete_lines":
                out.append(
                    f"    {label}: delete lines {step.get('line')}-"
                    f"{step.get('end_line', step.get('line'))} of {step['file']!r}"
                )
            elif action == "apply_diff":
                diff = step.get("diff") or ""
                lines = diff.splitlines()
                out.append(f"    {label}: apply_diff ({len(lines)} lines)")
                # Show diff header lines + a couple of body context lines
                preview = []
                for ln in lines:
                    if ln.startswith(("---", "+++", "@@", "diff ", "deleted ", "new file ")):
                        preview.append(ln)
                    elif ln.startswith(("+", "-")) and len(preview) < 10:
                        preview.append(ln)
                for ln in preview[:10]:
                    out.append(f"      │ {truncate(ln, 92)}")
                if len(preview) > 10 or len(lines) > len(preview):
                    out.append(f"      │ ... ({len(lines) - min(10, len(preview))} more lines)")
            elif action == "run":
                expect = step.get("expect_exit", 0)
                out.append(f"    {label}: $ {step['command']}  (expect exit {expect})")
                if step.get("expect_stdout_contains"):
                    out.append(f"      stdout must contain: {step['expect_stdout_contains']!r}")
            elif action == "commit":
                out.append(f"    {label}: git commit -m {step['message']!r}")
            elif action == "push":
                out.append(f"    {label}: git push")
            else:
                out.append(f"    {label}: <unknown action>")
    return "\n".join(out)


def render_sre_setup_env(out_dict: dict) -> str:
    out = []
    plan = out_dict.get("task_plan") or []
    for i, ts in enumerate(plan, start=2):
        if ts.get("agent") != "cifix_sre_setup":
            continue
        out.append("")
        out.append(banner(
            f"  SRE_SETUP TASK seq={i} ({ts.get('task_id')}) — env_requirements  ",
            char="·",
        ))
        out.append(f"    purpose: {ts.get('purpose')}")
        env = ts.get("env_requirements") or {}
        if env.get("python"):
            out.append(f"    python              : {env['python']}")
        if env.get("os_packages"):
            out.append(f"    os_packages         : {', '.join(env['os_packages'])}")
        if env.get("python_packages"):
            out.append(f"    python_packages     : {', '.join(env['python_packages'])}")
        if env.get("env_vars"):
            kv = ", ".join(f"{k}={v}" for k, v in env["env_vars"].items())
            out.append(f"    env_vars            : {kv}")
        if env.get("services"):
            out.append(f"    services            : {', '.join(env['services'])}")
        if env.get("reproduce_command"):
            out.append(f"    reproduce_command   : {env['reproduce_command']}")
        if env.get("reproduce_expected"):
            out.append(f"    reproduce_expected  : {env['reproduce_expected']}")
    return "\n".join(out)


def render_sre_verify(out_dict: dict) -> str:
    out = []
    plan = out_dict.get("task_plan") or []
    for i, ts in enumerate(plan, start=2):
        if ts.get("agent") != "cifix_sre_verify":
            continue
        out.append("")
        out.append(banner(
            f"  SRE_VERIFY TASK seq={i} ({ts.get('task_id')}) — verify command  ",
            char="·",
        ))
        out.append(f"    purpose: {ts.get('purpose')}")
        for step in ts.get("steps") or []:
            if step.get("action") == "run":
                out.append(
                    f"    $ {step['command']}  "
                    f"(expect exit {step.get('expect_exit', 0)})"
                )
    return "\n".join(out)


def render_invariants_check(report) -> str:
    out = [banner("VALIDATION — invariants run on this output", char="─")]
    if report.plan_validator_passed:
        out.append("  ✓ plan_validator (structural OK: no cycles, valid agents, terminates in verify)")
    else:
        out.append(f"  ✗ plan_validator — {report.plan_validator_error}")
    for name in report.invariants_passed:
        out.append(f"  ✓ {name}")
    for name, err in report.invariants_failed:
        out.append(f"  ✗ {name}")
        out.append(f"      └─ {err}")
    out.append("")
    out.append(f"  RESULT: {'PASS' if report.ok else 'FAIL'}  "
               f"({len(report.invariants_passed)} passed, "
               f"{len(report.invariants_failed)} failed)")
    return "\n".join(out)


# ─── Main ─────────────────────────────────────────────────────────────────────


def render_fixture(fx, out_dict: dict) -> str:
    sections = [
        banner(f"FIXTURE  {fx.name}", char="═"),
        "",
        render_input(fx),
        "",
        render_fix_spec_summary(out_dict),
        "",
        render_task_db_rows(out_dict),
        render_engineer_steps(out_dict),
        render_sre_setup_env(out_dict),
        render_sre_verify(out_dict),
        "",
        render_invariants_check(validate_tl_output(fx, out_dict)),
        "",
    ]
    return "\n".join(sections)


def main():
    args = sys.argv[1:]
    use_real = "--real" in args
    force = "--force" in args
    args = [a for a in args if a not in {"--real", "--force"}]

    corpus = discover_corpus()
    if args:
        needle = args[0].lower()
        corpus = [f for f in corpus if needle in f.name.lower()]
        if not corpus:
            print(f"no fixture matches {needle!r}")
            return 1

    print()
    print(banner("v1.7 TL OUTPUT CORPUS — VISUAL DEMO", char="█"))
    print()
    if use_real:
        print("Mode: REAL TL — running GPT-5.4 against each fixture.")
        print("      Outputs are cached to /tmp/v17_tl_cache (re-runs free).")
        print("      Pass --force to bust the cache.")
    else:
        print("Mode: CANNED — showing the prompt-engineering target outputs.")
        print("      Pass --real to run actual GPT-5.4 against each fixture.")
    print()
    print(f"Corpus size: {len(corpus)} fixtures")
    print()

    if use_real:
        from tests.integration.v3_harness.v17_real_tl_runner import (  # noqa: PLC0415
            run_real_tl_against_fixture,
        )
        cache_dir = "/tmp/v17_tl_cache"
        for fx in corpus:
            try:
                out_dict = run_real_tl_against_fixture(
                    fx, cache_dir=cache_dir, force=force
                )
            except Exception as exc:  # noqa: BLE001
                print(f"⚠ real-TL run failed for {fx.name}: {type(exc).__name__}: {exc}")
                continue
            print(render_fixture(fx, out_dict))
            meta = out_dict.get("_meta") or {}
            cache_marker = " (from cache)" if meta.get("from_cache") else ""
            print(
                f"  [meta] turns={meta.get('turns_used')}  "
                f"tool_calls={meta.get('tool_calls_used')}  "
                f"model={meta.get('model')}{cache_marker}"
            )
    else:
        for fx in corpus:
            good_fn = _GOOD_OUTPUTS.get(fx.name)
            if good_fn is None:
                print(f"⚠ no canned output for {fx.name}; skipping")
                continue
            out_dict = good_fn()
            print(render_fixture(fx, out_dict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
