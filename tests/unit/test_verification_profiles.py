"""
Unit tests for verification_profiles.py

Tests:
- All profiles have required non-empty fields
- detect_tech_stack filesystem heuristics (one test per stack)
- detect_tech_stack fallback paths (no workspace, unknown app_type)
- get_profile returns fallback for unknown tech_stack
- run_profile_checks routes correctly (mocked _run)
"""

from __future__ import annotations

import json
from unittest.mock import patch

from phalanx.agents.verification_profiles import (
    PROFILES,
    _discover_fastapi_routers,
    _discover_react_components,
    detect_tech_stack,
    get_profile,
    run_profile_checks,
)

# ─────────────────────────────────────────────────────────────────────────────
# Profile registry sanity
# ─────────────────────────────────────────────────────────────────────────────


def test_all_profiles_non_empty():
    assert len(PROFILES) >= 10, "Expected at least 10 registered profiles"


def test_all_profiles_have_required_fields():
    required_keys = [
        "tech_stack",
        "app_type",
        "entry_points",
        "detection_files",
        "install_cmd",
        "build_cmd",
        "typecheck_cmd",
        "lint_cmd",
        "install_timeout",
        "build_timeout",
        "integration_pattern",
    ]
    for name, profile in PROFILES.items():
        for key in required_keys:
            assert hasattr(profile, key), f"Profile '{name}' missing field '{key}'"
        assert profile.tech_stack == name, (
            f"Profile key '{name}' != tech_stack '{profile.tech_stack}'"
        )
        assert profile.app_type in ("web", "api", "mobile", "cli"), f"Invalid app_type in '{name}'"
        assert profile.integration_pattern, f"Empty integration_pattern in '{name}'"
        assert len(profile.entry_points) >= 1, f"No entry_points in '{name}'"
        assert profile.build_timeout > 0, f"build_timeout must be >0 in '{name}'"


def test_generic_web_profile_is_fallback():
    profile = get_profile("does_not_exist_xyz")
    assert profile.tech_stack == "generic_web"


def test_get_profile_known():
    assert get_profile("nextjs").tech_stack == "nextjs"
    assert get_profile("flutter").tech_stack == "flutter"
    assert get_profile("go").tech_stack == "go"


# ─────────────────────────────────────────────────────────────────────────────
# detect_tech_stack — filesystem heuristics
# ─────────────────────────────────────────────────────────────────────────────


def test_detect_nextjs(tmp_path):
    (tmp_path / "next.config.js").touch()
    assert detect_tech_stack(tmp_path, "web") == "nextjs"


def test_detect_nextjs_mjs(tmp_path):
    (tmp_path / "next.config.mjs").touch()
    assert detect_tech_stack(tmp_path, "web") == "nextjs"


def test_detect_vite(tmp_path):
    (tmp_path / "vite.config.ts").touch()
    assert detect_tech_stack(tmp_path, "web") == "vite"


def test_detect_sveltekit(tmp_path):
    (tmp_path / "svelte.config.js").touch()
    assert detect_tech_stack(tmp_path, "web") == "sveltekit"


def test_detect_flutter(tmp_path):
    (tmp_path / "pubspec.yaml").touch()
    assert detect_tech_stack(tmp_path, "mobile") == "flutter"


def test_detect_expo(tmp_path):
    (tmp_path / "app.json").touch()
    pkg = {"dependencies": {"expo": "^50.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    assert detect_tech_stack(tmp_path, "mobile") == "expo"


def test_detect_react_native(tmp_path):
    (tmp_path / "app.json").touch()
    pkg = {"dependencies": {"react-native": "0.73.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    assert detect_tech_stack(tmp_path, "mobile") == "react_native"


def test_detect_go(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\ngo 1.21\n")
    assert detect_tech_stack(tmp_path, "api") == "go"


def test_detect_django(tmp_path):
    (tmp_path / "manage.py").touch()
    assert detect_tech_stack(tmp_path, "api") == "django"


def test_detect_fastapi(tmp_path):
    main = tmp_path / "main.py"
    main.write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    assert detect_tech_stack(tmp_path, "api") == "fastapi"


def test_detect_express(tmp_path):
    pkg = {"dependencies": {"express": "^4.18.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    assert detect_tech_stack(tmp_path, "api") == "express"


def test_detect_generic_web_fallback(tmp_path):
    # package.json with no recognised framework
    pkg = {"dependencies": {"lodash": "4.17.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    assert detect_tech_stack(tmp_path, "web") == "generic_web"


def test_detect_generic_python_fallback(tmp_path):
    (tmp_path / "main.py").write_text("print('hello')\n")
    assert detect_tech_stack(tmp_path, "api") == "generic_python"


def test_detect_no_workspace_web():
    assert detect_tech_stack(None, "web") == "generic_web"


def test_detect_no_workspace_api():
    assert detect_tech_stack(None, "api") == "generic_python"


def test_detect_no_workspace_mobile():
    assert detect_tech_stack(None, "mobile") == "react_native"


def test_detect_empty_dir(tmp_path):
    result = detect_tech_stack(tmp_path, "web")
    assert result == "generic_web"


def test_detect_nextjs_wins_over_generic_web(tmp_path):
    # Has both package.json and next.config.js — should detect nextjs
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"next": "14"}}))
    (tmp_path / "next.config.js").touch()
    assert detect_tech_stack(tmp_path, "web") == "nextjs"


# ─────────────────────────────────────────────────────────────────────────────
# run_profile_checks
# ─────────────────────────────────────────────────────────────────────────────


def test_run_profile_checks_passes_on_success(tmp_path):
    profile = get_profile("nextjs")
    # Create the entry point so the entry-point check passes
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("export default function Page() { return <div/>; }")

    with patch("phalanx.agents.verification_profiles._run", return_value=(True, "", "")):
        errors = run_profile_checks(profile, tmp_path)
    assert errors == []


def test_run_profile_checks_returns_errors_on_build_failure(tmp_path):
    profile = get_profile("nextjs")
    # Don't create entry point — will also fail entry-point check
    with patch("phalanx.agents.verification_profiles._run") as mock_run:
        mock_run.return_value = (False, "", "error TS2307: Cannot find module './Hero'")
        errors = run_profile_checks(profile, tmp_path)
    assert len(errors) > 0
    assert any("TS2307" in e or "entry_point" in e for e in errors)


def test_run_profile_checks_stops_after_install_failure(tmp_path):
    """If install fails, build should not be attempted."""
    profile = get_profile("nextjs")
    call_count = 0

    def fake_run(cmd, cwd, timeout):
        nonlocal call_count
        call_count += 1
        return (False, "", "npm install failed")

    with patch("phalanx.agents.verification_profiles._run", side_effect=fake_run):
        errors = run_profile_checks(profile, tmp_path)

    assert call_count == 1  # only install was called
    assert len(errors) == 1


def test_run_profile_skips_install_when_cmd_empty(tmp_path):
    """Profiles like 'go' have no install_cmd — should skip to build."""
    profile = get_profile("go")
    assert profile.install_cmd == []

    with patch(
        "phalanx.agents.verification_profiles._run", return_value=(True, "", "")
    ) as mock_run:
        # Create entry point
        (tmp_path / "main.go").write_text("package main\nfunc main() {}\n")
        run_profile_checks(profile, tmp_path)
    # install should not have been called with empty list
    calls = [c for c in mock_run.call_args_list if c[0][0] == []]
    assert len(calls) == 0


# ─────────────────────────────────────────────────────────────────────────────
# _discover_react_components
# ─────────────────────────────────────────────────────────────────────────────


def test_discover_react_components_finds_tsx(tmp_path):
    comp_dir = tmp_path / "components"
    comp_dir.mkdir()
    (comp_dir / "Hero.tsx").write_text(
        "export default function Hero() { return <section>Hero</section>; }"
    )
    (comp_dir / "Footer.tsx").write_text("export default function Footer() { return <footer/>; }")
    result = _discover_react_components(comp_dir)
    names = {c["name"] for c in result}
    assert "Hero" in names
    assert "Footer" in names


def test_discover_react_components_skips_non_components(tmp_path):
    comp_dir = tmp_path / "components"
    comp_dir.mkdir()
    # No default export, no JSX
    (comp_dir / "utils.tsx").write_text("export const add = (a: number, b: number) => a + b;")
    result = _discover_react_components(comp_dir)
    assert all(c["name"] != "utils" for c in result)


def test_discover_react_components_skips_layout_dir(tmp_path):
    comp_dir = tmp_path / "components"
    (comp_dir / "layout").mkdir(parents=True)
    (comp_dir / "layout" / "Header.tsx").write_text(
        "export default function Header() { return <header/>; }"
    )
    result = _discover_react_components(comp_dir)
    assert all(c["name"] != "Header" for c in result)


def test_discover_react_components_named_export(tmp_path):
    comp_dir = tmp_path / "components"
    comp_dir.mkdir()
    (comp_dir / "PricingCards.tsx").write_text(
        "export const PricingCards = () => <div/>;",
    )
    result = _discover_react_components(comp_dir)
    names = {c["name"] for c in result}
    assert "PricingCards" in names


# ─────────────────────────────────────────────────────────────────────────────
# _discover_fastapi_routers
# ─────────────────────────────────────────────────────────────────────────────


def test_discover_fastapi_routers_finds_router_files(tmp_path):
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    (api_dir / "listings.py").write_text("from fastapi import APIRouter\nrouter = APIRouter()\n")
    (api_dir / "users.py").write_text("from fastapi import APIRouter\nrouter = APIRouter()\n")
    result = _discover_fastapi_routers(tmp_path)
    names = {r["name"] for r in result}
    assert "listings" in names
    assert "users" in names


def test_discover_fastapi_routers_skips_init(tmp_path):
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    (api_dir / "__init__.py").write_text("from fastapi import APIRouter\nrouter = APIRouter()\n")
    result = _discover_fastapi_routers(tmp_path)
    assert all(r["name"] != "__init__" for r in result)
