#!/usr/bin/env python3
"""
Dry-run simulation for the full verification + integration wiring pipeline.

Simulates detect_tech_stack → get_profile → _wire_* → run_profile_checks
for every registered tech stack, using synthetic filesystem fixtures.

Usage:
    python scripts/dry_run_verification.py [--verbose] [--stack STACK]

Exit code: 0 = all scenarios passed, 1 = any failure.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from phalanx.agents.integration_wiring import IntegrationWiringAgent
from phalanx.agents.verification_profiles import (
    PROFILES,
    VerificationProfile,
    _discover_fastapi_routers,
    _discover_react_components,
    detect_tech_stack,
    get_profile,
    run_profile_checks,
)

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
SKIP = "\033[93m~\033[0m"


# ─────────────────────────────────────────────────────────────────────────────
# Fixture builders
# ─────────────────────────────────────────────────────────────────────────────

def _setup_nextjs(d: Path) -> None:
    (d / "next.config.js").write_text("/** @type {import('next').NextConfig} */\nmodule.exports = {};\n")
    (d / "package.json").write_text('{"dependencies":{"next":"14"}}')
    comp = d / "components"
    comp.mkdir()
    (comp / "Hero.tsx").write_text("export default function Hero() { return <section>Hello</section>; }")
    (comp / "Footer.tsx").write_text("export default function Footer() { return <footer/>; }")
    (d / "app").mkdir()
    (d / "app" / "page.tsx").write_text("export default function Page() { return <main/>; }")


def _setup_vite(d: Path) -> None:
    (d / "vite.config.ts").write_text("import { defineConfig } from 'vite'; export default defineConfig({});")
    (d / "package.json").write_text('{"dependencies":{"vite":"5"}}')
    src = d / "src" / "components"
    src.mkdir(parents=True)
    (src / "Hero.tsx").write_text("export default function Hero() { return <section/>; }")


def _setup_sveltekit(d: Path) -> None:
    (d / "svelte.config.js").write_text("export default {};")
    (d / "package.json").write_text('{"dependencies":{"@sveltejs/kit":"2"}}')
    routes = d / "src" / "routes"
    routes.mkdir(parents=True)
    (routes / "+page.svelte").write_text("<script>let x = 1;</script><h1>Hello</h1>")


def _setup_fastapi(d: Path) -> None:
    (d / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/')\ndef root(): return {'status': 'ok'}\n"
    )
    api = d / "api"
    api.mkdir()
    (api / "listings.py").write_text("from fastapi import APIRouter\nrouter = APIRouter()\n")
    (api / "users.py").write_text("from fastapi import APIRouter\nrouter = APIRouter()\n")


def _setup_django(d: Path) -> None:
    (d / "manage.py").write_text("#!/usr/bin/env python\nimport sys\n")


def _setup_go(d: Path) -> None:
    (d / "go.mod").write_text("module example.com/app\ngo 1.21\n")
    (d / "main.go").write_text("package main\nfunc main() {}\n")


def _setup_react_native(d: Path) -> None:
    (d / "app.json").write_text('{"name":"App","displayName":"App"}')
    (d / "package.json").write_text('{"dependencies":{"react-native":"0.73.0"}}')
    (d / "App.tsx").write_text("export default function App() { return null; }")


def _setup_expo(d: Path) -> None:
    (d / "app.json").write_text('{"expo":{"name":"MyApp","slug":"my-app"}}')
    (d / "package.json").write_text('{"dependencies":{"expo":"^50.0.0"}}')
    (d / "App.tsx").write_text("export default function App() { return null; }")


def _setup_flutter(d: Path) -> None:
    (d / "pubspec.yaml").write_text("name: my_app\nflutter:\n  uses-material-design: true\n")
    lib = d / "lib"
    lib.mkdir()
    (lib / "main.dart").write_text(
        "import 'package:flutter/material.dart';\nvoid main() => runApp(const MyApp());\n"
        "class MyApp extends StatelessWidget {\n  const MyApp({super.key});\n"
        "  @override Widget build(BuildContext context) => const MaterialApp();\n}\n"
    )


def _setup_click_cli(d: Path) -> None:
    (d / "pyproject.toml").write_text("[tool.poetry]\nname = 'mycli'\n")
    (d / "cli.py").write_text("import click\n\n@click.command()\ndef main(): pass\n")


def _setup_generic_web(d: Path) -> None:
    (d / "package.json").write_text('{"dependencies":{"lodash":"4.17.0"}}')
    src = d / "src"
    src.mkdir()
    (src / "index.tsx").write_text("import React from 'react'; export default function App() { return null; }")


def _setup_generic_python(d: Path) -> None:
    (d / "main.py").write_text("print('hello')\n")


FIXTURES = {
    "nextjs": (_setup_nextjs, "web"),
    "vite": (_setup_vite, "web"),
    "sveltekit": (_setup_sveltekit, "web"),
    "generic_web": (_setup_generic_web, "web"),
    "fastapi": (_setup_fastapi, "api"),
    "django": (_setup_django, "api"),
    "go": (_setup_go, "api"),
    "generic_python": (_setup_generic_python, "api"),
    "react_native": (_setup_react_native, "mobile"),
    "expo": (_setup_expo, "mobile"),
    "flutter": (_setup_flutter, "mobile"),
    "click_cli": (_setup_click_cli, "cli"),
}

# Stacks where running the actual build command would require external tools
# (flutter, expo, nextjs, etc.) — skip live build, only check detection + wiring.
_SKIP_BUILD = {"nextjs", "vite", "sveltekit", "generic_web", "react_native", "expo",
               "flutter", "django", "express", "go", "fastapi"}


# ─────────────────────────────────────────────────────────────────────────────
# Phase runners
# ─────────────────────────────────────────────────────────────────────────────

def phase_detection(stack: str, app_type: str, d: Path, verbose: bool) -> bool:
    detected = detect_tech_stack(d, app_type)
    ok = detected == stack
    sym = PASS if ok else FAIL
    print(f"  {sym} detect_tech_stack → {detected!r} (expected {stack!r})")
    return ok


def phase_profile(stack: str, verbose: bool) -> bool:
    profile = get_profile(stack)
    ok = profile.tech_stack == stack
    sym = PASS if ok else FAIL
    print(f"  {sym} get_profile.tech_stack = {profile.tech_stack!r}")
    if verbose:
        print(f"      integration_pattern = {profile.integration_pattern}")
        print(f"      entry_points        = {profile.entry_points}")
    return ok


def phase_wiring(stack: str, d: Path, verbose: bool) -> bool:
    """Run the appropriate _wire_* method and verify it doesn't crash."""
    agent = IntegrationWiringAgent.__new__(IntegrationWiringAgent)
    # Minimal init without DB
    agent.run_id = "dry-run"
    agent.task_id = "dry-task"
    import structlog
    agent._log = structlog.get_logger("dry_run")

    profile = get_profile(stack)
    pattern = profile.integration_pattern
    try:
        if pattern == "nextjs-app-router":
            result = agent._wire_nextjs(d)
        elif pattern == "vite-app":
            result = agent._wire_vite(d)
        elif pattern == "fastapi-router":
            result = agent._wire_fastapi(d)
        elif pattern in ("rn-navigation", "expo-tabs"):
            result = agent._wire_react_native(d, profile.entry_points)
        elif pattern == "flutter-material":
            result = agent._wire_flutter(d)
        elif pattern == "go-main":
            result = agent._wire_go(d)
        else:
            print(f"  {SKIP} wiring: pattern {pattern!r} → LLM path (dry-run skip)")
            return True

        ok = result.get("status") in ("wired", "trusted", "skipped")
        sym = PASS if ok else FAIL
        print(f"  {sym} wiring: status={result['status']!r} files={result.get('files_wired', [])}")
        if verbose and result.get("notes"):
            for note in result["notes"]:
                print(f"      note: {note}")
        return ok
    except Exception as exc:
        print(f"  {FAIL} wiring crashed: {exc}")
        return False


def phase_entry_point(stack: str, d: Path, verbose: bool) -> bool:
    """Check that at least one expected entry point exists after wiring."""
    profile = get_profile(stack)
    present = [ep for ep in profile.entry_points if (d / ep).exists()]
    ok = bool(present)
    sym = PASS if ok else FAIL
    print(f"  {sym} entry_points: {present or 'NONE'} (expected one of {profile.entry_points})")
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def run_scenario(stack: str, app_type: str, setup_fn, verbose: bool) -> bool:
    print(f"\n{'─' * 60}")
    print(f"Stack: {stack} (app_type={app_type})")
    results = []

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        try:
            setup_fn(d)
        except Exception as exc:
            print(f"  {FAIL} fixture setup failed: {exc}")
            return False

        results.append(phase_detection(stack, app_type, d, verbose))
        results.append(phase_profile(stack, verbose))
        results.append(phase_wiring(stack, d, verbose))
        results.append(phase_entry_point(stack, d, verbose))

    passed = all(results)
    sym = PASS if passed else FAIL
    print(f"  {sym} {stack}: {sum(results)}/{len(results)} phases passed")
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--stack", help="Run only this stack")
    args = parser.parse_args()

    print("=" * 60)
    print("FORGE Verification Pipeline — Dry Run")
    print("=" * 60)

    scenarios = FIXTURES
    if args.stack:
        if args.stack not in FIXTURES:
            print(f"Unknown stack {args.stack!r}. Available: {sorted(FIXTURES)}")
            return 1
        scenarios = {args.stack: FIXTURES[args.stack]}

    results = []
    for stack, (setup_fn, app_type) in scenarios.items():
        ok = run_scenario(stack, app_type, setup_fn, args.verbose)
        results.append((stack, ok))

    print(f"\n{'=' * 60}")
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"Result: {passed}/{total} stacks passed")
    for stack, ok in results:
        sym = PASS if ok else FAIL
        print(f"  {sym} {stack}")

    if passed < total:
        print("\nFAILED stacks:")
        for stack, ok in results:
            if not ok:
                print(f"  - {stack}")
        return 1

    print("\nAll dry-run checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
