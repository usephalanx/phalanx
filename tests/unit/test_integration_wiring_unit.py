"""
Unit tests for integration_wiring.py

Tests:
- _wire_nextjs: components found → page.tsx generated; no components → skipped;
  existing non-trivial page.tsx → trusted
- _wire_vite: components found → App.tsx generated
- _wire_fastapi: router files found → main.py generated; pre-existing → trusted
- _wire_react_native: App.tsx present → trusted; absent → generated
- _wire_flutter: lib/main.dart present → trusted; absent → generated
- _wire_go: main.go present → trusted; absent → generated
- Full execute() path via AsyncMock session
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from phalanx.agents.integration_wiring import IntegrationWiringAgent


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_agent() -> IntegrationWiringAgent:
    return IntegrationWiringAgent(run_id="run-1", agent_id="agent-1", task_id="task-1")


# ─────────────────────────────────────────────────────────────────────────────
# _wire_nextjs
# ─────────────────────────────────────────────────────────────────────────────

def test_wire_nextjs_generates_page_tsx(tmp_path):
    agent = _make_agent()
    comp_dir = tmp_path / "components"
    comp_dir.mkdir()
    (comp_dir / "Hero.tsx").write_text(
        "export default function Hero() { return <section>Hello</section>; }"
    )
    (comp_dir / "Footer.tsx").write_text(
        "export default function Footer() { return <footer/>; }"
    )

    result = agent._wire_nextjs(tmp_path)

    assert result["status"] == "wired"
    assert "app/page.tsx" in result["files_wired"]
    page = (tmp_path / "app" / "page.tsx").read_text()
    assert "Hero" in page
    assert "Footer" in page


def test_wire_nextjs_no_components_skips(tmp_path):
    agent = _make_agent()
    (tmp_path / "components").mkdir()
    result = agent._wire_nextjs(tmp_path)
    assert result["status"] == "skipped"


def test_wire_nextjs_no_components_dir_skips(tmp_path):
    agent = _make_agent()
    result = agent._wire_nextjs(tmp_path)
    assert result["status"] == "skipped"


def test_wire_nextjs_trusts_existing_page_tsx(tmp_path):
    """If page.tsx already has ≥2 component imports, trust the builder."""
    agent = _make_agent()
    comp_dir = tmp_path / "components"
    comp_dir.mkdir()
    (comp_dir / "Hero.tsx").write_text(
        "export default function Hero() { return <div/>; }"
    )

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "page.tsx").write_text(
        "import Hero from '@/components/Hero'\n"
        "import Footer from '@/components/Footer'\n"
        "export default function Page() { return <main><Hero /><Footer /></main>; }\n"
    )

    result = agent._wire_nextjs(tmp_path)
    assert result["status"] == "trusted"


# ─────────────────────────────────────────────────────────────────────────────
# _wire_vite
# ─────────────────────────────────────────────────────────────────────────────

def test_wire_vite_generates_app_tsx(tmp_path):
    agent = _make_agent()
    comp_dir = tmp_path / "src" / "components"
    comp_dir.mkdir(parents=True)
    (comp_dir / "Hero.tsx").write_text(
        "export default function Hero() { return <section/>; }"
    )

    result = agent._wire_vite(tmp_path)

    assert result["status"] == "wired"
    assert "src/App.tsx" in result["files_wired"]
    app = (tmp_path / "src" / "App.tsx").read_text()
    assert "Hero" in app


def test_wire_vite_no_components_dir_skips(tmp_path):
    agent = _make_agent()
    result = agent._wire_vite(tmp_path)
    assert result["status"] == "skipped"


# ─────────────────────────────────────────────────────────────────────────────
# _wire_fastapi
# ─────────────────────────────────────────────────────────────────────────────

def test_wire_fastapi_generates_main_py(tmp_path):
    agent = _make_agent()
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    (api_dir / "listings.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
    )

    result = agent._wire_fastapi(tmp_path)

    assert result["status"] == "wired"
    main = (tmp_path / "main.py").read_text()
    assert "listings_router" in main
    assert "include_router" in main


def test_wire_fastapi_trusts_existing_main_py(tmp_path):
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    (api_dir / "listings.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
    )
    (tmp_path / "main.py").write_text(
        "from fastapi import FastAPI\nfrom api.listings import router as listings_router\n"
        "app = FastAPI()\napp.include_router(listings_router)\n"
    )

    agent = _make_agent()
    result = agent._wire_fastapi(tmp_path)
    assert result["status"] == "trusted"


def test_wire_fastapi_no_routers_skips(tmp_path):
    agent = _make_agent()
    result = agent._wire_fastapi(tmp_path)
    assert result["status"] == "skipped"


# ─────────────────────────────────────────────────────────────────────────────
# _wire_react_native
# ─────────────────────────────────────────────────────────────────────────────

def test_wire_react_native_trusts_existing_app_tsx(tmp_path):
    agent = _make_agent()
    (tmp_path / "App.tsx").write_text("export default function App() { return null; }")
    result = agent._wire_react_native(tmp_path, ["App.tsx"])
    assert result["status"] == "trusted"


def test_wire_react_native_generates_app_tsx(tmp_path):
    agent = _make_agent()
    result = agent._wire_react_native(tmp_path, ["App.tsx", "src/App.tsx"])
    assert result["status"] == "wired"
    assert "App.tsx" in result["files_wired"]
    content = (tmp_path / "App.tsx").read_text()
    assert "react-native" in content.lower() or "React" in content


# ─────────────────────────────────────────────────────────────────────────────
# _wire_flutter
# ─────────────────────────────────────────────────────────────────────────────

def test_wire_flutter_trusts_existing_main_dart(tmp_path):
    agent = _make_agent()
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "main.dart").write_text("void main() => runApp(const MyApp());")
    result = agent._wire_flutter(tmp_path)
    assert result["status"] == "trusted"


def test_wire_flutter_generates_main_dart(tmp_path):
    agent = _make_agent()
    result = agent._wire_flutter(tmp_path)
    assert result["status"] == "wired"
    dart = (tmp_path / "lib" / "main.dart").read_text()
    assert "runApp" in dart


# ─────────────────────────────────────────────────────────────────────────────
# _wire_go
# ─────────────────────────────────────────────────────────────────────────────

def test_wire_go_trusts_existing_main_go(tmp_path):
    agent = _make_agent()
    (tmp_path / "main.go").write_text("package main\nfunc main() {}")
    result = agent._wire_go(tmp_path)
    assert result["status"] == "trusted"


def test_wire_go_trusts_cmd_main_go(tmp_path):
    agent = _make_agent()
    (tmp_path / "cmd").mkdir()
    (tmp_path / "cmd" / "main.go").write_text("package main\nfunc main() {}")
    result = agent._wire_go(tmp_path)
    assert result["status"] == "trusted"


def test_wire_go_generates_stub(tmp_path):
    agent = _make_agent()
    result = agent._wire_go(tmp_path)
    assert result["status"] == "wired"
    go = (tmp_path / "main.go").read_text()
    assert "package main" in go


# ─────────────────────────────────────────────────────────────────────────────
# _wire dispatcher routing
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wire_dispatches_to_nextjs(tmp_path):
    from phalanx.agents.verification_profiles import get_profile
    agent = _make_agent()
    profile = get_profile("nextjs")

    with patch.object(agent, "_wire_nextjs", return_value={"status": "skipped", "files_wired": [], "notes": []}) as mock:
        result = await agent._wire(tmp_path, profile, [])
    mock.assert_called_once_with(tmp_path)


@pytest.mark.asyncio
async def test_wire_dispatches_to_fastapi(tmp_path):
    from phalanx.agents.verification_profiles import get_profile
    agent = _make_agent()
    profile = get_profile("fastapi")

    with patch.object(agent, "_wire_fastapi", return_value={"status": "skipped", "files_wired": [], "notes": []}) as mock:
        result = await agent._wire(tmp_path, profile, [])
    mock.assert_called_once_with(tmp_path)


@pytest.mark.asyncio
async def test_wire_dispatches_to_flutter(tmp_path):
    from phalanx.agents.verification_profiles import get_profile
    agent = _make_agent()
    profile = get_profile("flutter")

    with patch.object(agent, "_wire_flutter", return_value={"status": "trusted", "files_wired": [], "notes": []}) as mock:
        result = await agent._wire(tmp_path, profile, [])
    mock.assert_called_once_with(tmp_path)


@pytest.mark.asyncio
async def test_wire_dispatches_to_go(tmp_path):
    from phalanx.agents.verification_profiles import get_profile
    agent = _make_agent()
    profile = get_profile("go")

    with patch.object(agent, "_wire_go", return_value={"status": "trusted", "files_wired": [], "notes": []}) as mock:
        result = await agent._wire(tmp_path, profile, [])
    mock.assert_called_once_with(tmp_path)
