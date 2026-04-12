"""
Unit tests for SRE agent pure utility functions.
No Docker, no DB, no network — pure logic only.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from phalanx.agents.sre import (
    _detect_app_type,
    _make_slug,
    _nginx_conf_for_slug,
    _validate_dockerfile,
    _scan_repo,
)


# ── _make_slug ─────────────────────────────────────────────────────────────────

class TestMakeSlug:
    def test_basic_title(self):
        assert _make_slug("Salon Booking App") == "salon-booking-app"

    def test_strips_build_prefix(self):
        result = _make_slug("Build a Salon Booking App")
        assert "build" not in result
        assert "salon" in result

    def test_strips_create_prefix(self):
        result = _make_slug("Create an E-commerce Store")
        assert "create" not in result
        assert "e-commerce" in result or "e" in result

    def test_strips_special_chars(self):
        result = _make_slug("My App (v2.0)!")
        assert "(" not in result
        assert "!" not in result

    def test_lowercase(self):
        result = _make_slug("UPPER CASE")
        assert result == result.lower()

    def test_spaces_to_dashes(self):
        result = _make_slug("hello world")
        assert " " not in result
        assert "-" in result

    def test_truncated_at_60(self):
        long_title = "a " * 40
        result = _make_slug(long_title)
        assert len(result) <= 60

    def test_empty_string_returns_demo(self):
        assert _make_slug("") == "demo"

    def test_no_consecutive_dashes(self):
        result = _make_slug("hello  --  world")
        assert "--" not in result


# ── _validate_dockerfile ───────────────────────────────────────────────────────

class TestValidateDockerfile:
    def test_valid_dockerfile_passes(self):
        content = "FROM python:3.12-slim\nWORKDIR /app\nCMD ['python', 'app.py']\n"
        _validate_dockerfile(content)  # should not raise

    def test_valid_with_arg_first(self):
        content = "ARG VERSION=1.0\nFROM python:3.12\nCMD python app.py\n"
        _validate_dockerfile(content)  # should not raise

    def test_raises_for_empty_dockerfile(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_dockerfile("")

    def test_raises_for_comment_only(self):
        with pytest.raises(ValueError):
            _validate_dockerfile("# just a comment\n")

    def test_raises_for_non_from_first_line(self):
        content = "RUN echo hello\nCMD echo done\n"
        with pytest.raises(ValueError, match="FROM or ARG"):
            _validate_dockerfile(content)

    def test_raises_for_no_cmd_or_entrypoint(self):
        content = "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\n"
        with pytest.raises(ValueError, match="CMD or ENTRYPOINT"):
            _validate_dockerfile(content)

    def test_entrypoint_accepted_instead_of_cmd(self):
        content = "FROM golang:1.21\nWORKDIR /app\nENTRYPOINT ['/app/server']\n"
        _validate_dockerfile(content)  # should not raise

    def test_cmd_array_accepted(self):
        content = "FROM node:20\nWORKDIR /app\nCMD['node', 'index.js']\n"
        _validate_dockerfile(content)  # should not raise


# ── _nginx_conf_for_slug ───────────────────────────────────────────────────────

class TestNginxConfForSlug:
    def test_contains_slug(self):
        conf = _nginx_conf_for_slug("my-app", "172.17.0.2", 3000)
        assert "my-app" in conf

    def test_contains_ip_and_port(self):
        conf = _nginx_conf_for_slug("demo", "10.0.0.5", 8080)
        assert "10.0.0.5:8080" in conf

    def test_has_redirect_and_location(self):
        conf = _nginx_conf_for_slug("test-app", "172.17.0.3", 80)
        assert "return 301" in conf
        assert "proxy_pass" in conf

    def test_proxy_headers_present(self):
        conf = _nginx_conf_for_slug("demo", "1.2.3.4", 5000)
        assert "proxy_set_header" in conf
        assert "X-Forwarded-Prefix" in conf

    def test_returns_string(self):
        result = _nginx_conf_for_slug("app", "127.0.0.1", 8000)
        assert isinstance(result, str)
        assert len(result) > 0


# ── _detect_app_type ───────────────────────────────────────────────────────────

class TestDetectAppType:
    def test_react_from_package_json(self, tmp_path):
        pkg = {"dependencies": {"react": "^18", "vite": "^5"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "react"
        assert port == 80

    def test_nextjs_from_package_json(self, tmp_path):
        pkg = {"dependencies": {"next": "^14", "react": "^18"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "nextjs"
        assert port == 3000

    def test_express_from_package_json(self, tmp_path):
        pkg = {"dependencies": {"express": "^4"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "express"
        assert port == 3000

    def test_fastapi_from_requirements(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "fastapi"
        assert port == 8000

    def test_django_from_requirements(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("django>=4\ngunicorn\n")
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "django"
        assert port == 8000

    def test_flask_from_requirements(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask\nwerkzeug\n")
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "flask"
        assert port == 5000

    def test_go_from_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/app\ngo 1.21\n")
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "go"
        assert port == 8080

    def test_rust_from_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "myapp"\n')
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "rust"
        assert port == 8080

    def test_ruby_from_gemfile(self, tmp_path):
        (tmp_path / "Gemfile").write_text('source "https://rubygems.org"\ngem "rails"\n')
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "ruby"
        assert port == 3000

    def test_php_from_index_php(self, tmp_path):
        (tmp_path / "index.php").write_text("<?php echo 'hello'; ?>\n")
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "php"
        assert port == 80

    def test_static_from_index_html(self, tmp_path):
        (tmp_path / "index.html").write_text("<html><body>Hello</body></html>")
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "static"
        assert port == 80

    def test_static_fallback_for_empty_dir(self, tmp_path):
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "static"
        assert port == 80

    def test_fullstack_detection(self, tmp_path):
        frontend = tmp_path / "frontend"
        frontend.mkdir()
        (frontend / "package.json").write_text('{"dependencies": {"react": "^18"}}')
        backend = tmp_path / "backend"
        backend.mkdir()
        (backend / "requirements.txt").write_text("fastapi\n")
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "fullstack"
        assert port == 80


# ── _scan_repo ─────────────────────────────────────────────────────────────────

class TestScanRepo:
    def test_returns_listing_and_contents(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "requirements.txt").write_text("flask\n")
        result = _scan_repo(str(tmp_path))
        assert "listing" in result
        assert "contents" in result
        assert isinstance(result["listing"], list)
        assert isinstance(result["contents"], dict)

    def test_priority_files_in_contents(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("django\n")
        result = _scan_repo(str(tmp_path))
        assert "requirements.txt" in result["contents"]

    def test_listing_capped_at_80(self, tmp_path):
        for i in range(100):
            (tmp_path / f"file{i}.py").write_text("x = 1")
        result = _scan_repo(str(tmp_path))
        assert len(result["listing"]) <= 80

    def test_node_modules_excluded(self, tmp_path):
        node_modules = tmp_path / "node_modules"
        node_modules.mkdir()
        (node_modules / "some_pkg.js").write_text("module.exports = {}")
        result = _scan_repo(str(tmp_path))
        assert not any("node_modules" in f for f in result["listing"])

    def test_package_json_content_read(self, tmp_path):
        pkg = {"name": "myapp", "version": "1.0"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        result = _scan_repo(str(tmp_path))
        assert "package.json" in result["contents"]


# ── More _detect_app_type coverage ────────────────────────────────────────────

class TestDetectAppTypeExtra:
    def test_php_via_composer_json(self, tmp_path):
        (tmp_path / "composer.json").write_text('{"require": {"php": ">=8.0"}}')
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "php"
        assert port == 80

    def test_node_invalid_json_falls_back_to_react(self, tmp_path):
        (tmp_path / "package.json").write_text("not valid json {{{")
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "react"
        assert port == 80

    def test_node_no_known_deps_falls_back_to_react(self, tmp_path):
        pkg = {"dependencies": {"lodash": "^4"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "react"  # default for node project
        assert port == 80

    def test_python_generic_fastapi_fallback(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("sqlalchemy\npydantic\n")
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "fastapi"  # generic Python fallback
        assert port == 8000

    def test_pyproject_toml_django(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "myapp"\ndependencies = ["django"]\n')
        app_type, port = _detect_app_type(str(tmp_path))
        assert app_type == "django"
        assert port == 8000


# ── _validate_dockerfile extra branches ───────────────────────────────────────

class TestValidateDockerfileExtra:
    def test_cmd_on_first_line_accepted(self):
        """CMD at start of content (no newline before it)."""
        content = "FROM alpine\nRUN echo hello\nCMD echo done\n"
        _validate_dockerfile(content)  # should not raise

    def test_entrypoint_array_accepted(self):
        content = "FROM golang:1.21\nWORKDIR /app\nENTRYPOINT['/app/main']\n"
        _validate_dockerfile(content)  # should not raise


# ── _make_slug additional edge cases ──────────────────────────────────────────

class TestMakeSlugExtra:
    def test_make_an_prefix_stripped(self):
        result = _make_slug("make an online store")
        assert "make" not in result
        assert "online" in result

    def test_implement_the_prefix_stripped(self):
        result = _make_slug("implement the dashboard")
        assert "implement" not in result
        assert "dashboard" in result

    def test_develop_a_prefix_stripped(self):
        result = _make_slug("develop a booking system")
        assert "develop" not in result
        assert "booking" in result

    def test_underscores_to_dashes(self):
        result = _make_slug("my_app_name")
        assert "_" not in result

    def test_trailing_dashes_stripped(self):
        result = _make_slug("---My App---")
        assert not result.startswith("-")
        assert not result.endswith("-")
