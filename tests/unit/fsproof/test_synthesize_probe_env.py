"""Service-app detection for probe synthesis.

`_service_signals` decides whether the synthesis prompt gains the large
"SERVICE APP — you MUST stub the DB or the probe will exit 2" block and an extra
`npm install supertest`. Getting it wrong is not free in either direction:

  - false NEGATIVE on a real service app → the probe can't boot the app → exit 2
    → INCONCLUSIVE, which is the failure this detection was added to prevent.
  - false POSITIVE on a plain app → the probe author is ordered to stub infra
    that does not exist, on exactly the simple apps that work today.

These cover both, plus the manifest formats the detector actually has to read.
"""

from __future__ import annotations

import json

import pytest

from phalanx.ci_fixer_v3.synthesize_probe_task import _detect_lang, _service_signals


def _write(tmp_path, files: dict[str, str]):
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


# ── false positives: prose must never imply infra ────────────────────────────


class TestNotAServiceApp:
    def test_jpg_in_description_is_not_a_postgres_app(self, tmp_path):
        """Regression: `pg` was matched as a substring of the whole manifest, so
        any package.json mentioning 'jpg' was declared a Postgres service app."""
        ws = _write(tmp_path, {"package.json": json.dumps({
            "name": "thumbnailer",
            "description": "Resize and convert jpg images. No database anywhere.",
            "dependencies": {"express": "^4.18.0", "sharp": "^0.33.0"},
        })})
        service, hint = _service_signals(ws)
        assert service is False, f"plain image app misread as a service app ({hint!r})"

    def test_prose_mentioning_databases_is_not_a_service_app(self, tmp_path):
        ws = _write(tmp_path, {"package.json": json.dumps({
            "name": "docs-site",
            "description": "Guides for working with databases and motor controllers",
            "dependencies": {"express": "^4.18.0"},
        })})
        assert _service_signals(ws)[0] is False

    def test_plain_app_gets_no_supertest_install(self, tmp_path):
        ws = _write(tmp_path, {"package.json": json.dumps({
            "name": "plain", "dependencies": {"express": "^4.18.0"},
        })})
        setup = _detect_lang(ws)["setup_cmds"]
        assert not any("supertest" in c for c in setup)


# ── true positives: real dependencies must still be caught ───────────────────


class TestIsAServiceApp:
    def test_node_pg_dependency(self, tmp_path):
        ws = _write(tmp_path, {"package.json": json.dumps({
            "name": "api", "dependencies": {"express": "^4", "pg": "^8.11.0"},
        })})
        service, hint = _service_signals(ws)
        assert service is True
        assert "pg" in hint

    def test_node_dev_dependency_counts(self, tmp_path):
        ws = _write(tmp_path, {"package.json": json.dumps({
            "name": "api", "dependencies": {"express": "^4"},
            "devDependencies": {"ioredis": "^5"},
        })})
        assert _service_signals(ws)[0] is True

    def test_scoped_package_name(self, tmp_path):
        ws = _write(tmp_path, {"package.json": json.dumps({
            "name": "api", "dependencies": {"@prisma/client": "^5"},
        })})
        assert _service_signals(ws)[0] is True

    @pytest.mark.parametrize("line", [
        "psycopg2-binary==2.9.9",
        "psycopg[binary] >= 3.1",
        "sqlalchemy",
        "asyncpg==0.29.0  # postgres driver",
    ])
    def test_python_requirements(self, tmp_path, line):
        ws = _write(tmp_path, {"requirements.txt": f"fastapi==0.110.0\n{line}\n"})
        assert _service_signals(ws)[0] is True

    def test_pyproject_pep621(self, tmp_path):
        ws = _write(tmp_path, {"pyproject.toml": (
            '[project]\nname = "api"\n'
            'dependencies = ["fastapi>=0.110", "sqlalchemy>=2.0"]\n'
        )})
        assert _service_signals(ws)[0] is True

    def test_pyproject_poetry(self, tmp_path):
        ws = _write(tmp_path, {"pyproject.toml": (
            '[tool.poetry.dependencies]\npython = "^3.12"\nredis = "^5.0"\n'
        )})
        assert _service_signals(ws)[0] is True

    def test_node_service_gets_supertest_install(self, tmp_path):
        ws = _write(tmp_path, {"package.json": json.dumps({
            "name": "api", "dependencies": {"express": "^4", "pg": "^8"},
        })})
        setup = _detect_lang(ws)["setup_cmds"]
        assert any("supertest" in c for c in setup)

    def test_content_fallback_when_manifest_is_silent(self, tmp_path):
        """No declared dep, but the code clearly opens a pool — still a service."""
        ws = _write(tmp_path, {
            "package.json": json.dumps({"name": "api", "dependencies": {}}),
            "src/db.js": "const { Pool } = require('pg');\nconst p = new Pool();\n",
        })
        assert _service_signals(ws)[0] is True


# ── robustness ───────────────────────────────────────────────────────────────


class TestRobustness:
    def test_malformed_package_json_does_not_raise(self, tmp_path):
        ws = _write(tmp_path, {"package.json": "{not valid json at all"})
        assert _service_signals(ws) == (False, "")

    def test_malformed_pyproject_does_not_raise(self, tmp_path):
        ws = _write(tmp_path, {"pyproject.toml": "[project\nbroken = "})
        service, _ = _service_signals(ws)
        assert service is False

    def test_empty_workspace(self, tmp_path):
        assert _service_signals(tmp_path) == (False, "")

    def test_detect_lang_still_reports_service_flag(self, tmp_path):
        ws = _write(tmp_path, {"requirements.txt": "fastapi\nasyncpg\n"})
        env = _detect_lang(ws)
        assert env["service"] is True
        assert env["lang"] == "Python"
