"""Age-based sandbox reaper.

This thing removes containers on a production host from a scheduled job with no
human watching, so the tests that matter are the ones proving what it will NOT
touch. Every safety property is asserted against a fixture containing the real
neighbours it runs beside on prod: the app stack, the demo containers, and the
old forge-* stack.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from phalanx.runtime import sandbox_reaper as reaper


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S +0000 UTC")


def _ps_line(cid: str, name: str, age_hours: float) -> str:
    created = datetime.now(UTC) - timedelta(hours=age_hours)
    return f"{cid}\t{name}\t{_fmt(created)}"


# The real prod neighbourhood, as observed 2026-08-10.
PROD_NEIGHBOURS = [
    _ps_line("aaa1", "phalanx-prod-phalanx-api-1", 12),
    _ps_line("aaa2", "phalanx-prod-postgres-1", 12),
    _ps_line("aaa3", "phalanx-prod-phalanx-ci-fixer-worker-1", 12),
    _ps_line("aaa4", "phalanx-prod-nginx-1", 12),
    _ps_line("bbb1", "forge-postgres", 2900),
    _ps_line("bbb2", "forge-redis", 2900),
    _ps_line("ccc1", "phalanx-demo-hello-world-fastapi-v4", 2900),
    _ps_line("ddd1", "amazing_solomon", 400),          # legacy pool container
    _ps_line("ddd2", "phalanx-sandbox-python-thing", 400),
]


@pytest.fixture
def fake_docker(monkeypatch):
    """Intercept every docker call; record removals."""
    state = {"ps_lines": [], "removed": [], "ps_code": 0, "rm_code": 0}

    async def _fake(*args, timeout_s=30):
        if args[0] == "ps":
            return state["ps_code"], "\n".join(state["ps_lines"])
        if args[0] == "rm":
            state["removed"].append(args[-1])
            return state["rm_code"], ""
        return 0, ""

    monkeypatch.setattr(reaper, "_docker", _fake)
    return state


# ── what it must never touch ─────────────────────────────────────────────────


class TestSafety:
    @pytest.mark.asyncio
    async def test_never_touches_prod_demos_or_legacy_stack(self, fake_docker):
        fake_docker["ps_lines"] = PROD_NEIGHBOURS
        orphans = await reaper.find_orphans(max_age_hours=6)
        assert orphans == [], f"reaper selected a non-sandbox container: {orphans}"

    @pytest.mark.asyncio
    async def test_removes_nothing_when_only_neighbours_present(self, fake_docker):
        fake_docker["ps_lines"] = PROD_NEIGHBOURS
        summary = await reaper.reap_orphans(max_age_hours=6)
        assert fake_docker["removed"] == []
        assert summary["reaped"] == 0

    @pytest.mark.asyncio
    async def test_young_sandbox_is_left_alone(self, fake_docker):
        """A container from a run that is still executing must survive."""
        fake_docker["ps_lines"] = [_ps_line("e1", "cifix-v3-deadbeef", 0.5)]
        assert await reaper.find_orphans(max_age_hours=6) == []

    @pytest.mark.asyncio
    async def test_boundary_is_respected(self, fake_docker):
        fake_docker["ps_lines"] = [
            _ps_line("e1", "cifix-v3-aaaaaaaa", 5.9),
            _ps_line("e2", "cifix-v3-bbbbbbbb", 6.1),
        ]
        names = [o["name"] for o in await reaper.find_orphans(max_age_hours=6)]
        assert names == ["cifix-v3-bbbbbbbb"]

    @pytest.mark.asyncio
    async def test_unparseable_timestamp_is_skipped_not_guessed(self, fake_docker):
        fake_docker["ps_lines"] = ["e1\tcifix-v3-aaaaaaaa\tnot-a-timestamp"]
        assert await reaper.find_orphans(max_age_hours=6) == []

    @pytest.mark.asyncio
    async def test_lookalike_names_are_not_matched(self, fake_docker):
        """Only the provisioner's exact `cifix-v3-<8 hex>` shape qualifies."""
        fake_docker["ps_lines"] = [
            _ps_line("e1", "cifix-v3-notahexvalue", 500),
            _ps_line("e2", "my-cifix-v3-aaaaaaaa", 500),
            _ps_line("e3", "cifix-v3-aaaaaaaa-backup", 500),
            _ps_line("e4", "cifix-v2-aaaaaaaa", 500),
        ]
        assert await reaper.find_orphans(max_age_hours=6) == []


# ── what it must do ──────────────────────────────────────────────────────────


class TestReaping:
    @pytest.mark.asyncio
    async def test_reaps_old_sandboxes_only(self, fake_docker):
        fake_docker["ps_lines"] = PROD_NEIGHBOURS + [
            _ps_line("f1", "cifix-v3-11111111", 380),
            _ps_line("f2", "cifix-v3-22222222", 2700),
            _ps_line("f3", "cifix-v3-33333333", 0.2),
        ]
        summary = await reaper.reap_orphans(max_age_hours=6)
        assert sorted(fake_docker["removed"]) == ["f1", "f2"]
        assert summary["reaped"] == 2
        assert summary["oldest_age_hours"] >= 2700

    @pytest.mark.asyncio
    async def test_dry_run_removes_nothing_but_reports(self, fake_docker):
        fake_docker["ps_lines"] = [_ps_line("f1", "cifix-v3-11111111", 500)]
        summary = await reaper.reap_orphans(max_age_hours=6, dry_run=True)
        assert fake_docker["removed"] == []
        assert summary["found"] == 1
        assert summary["reaped"] == 0
        assert summary["dry_run"] is True

    @pytest.mark.asyncio
    async def test_sweep_is_bounded_and_reports_truncation(self, fake_docker):
        fake_docker["ps_lines"] = [
            _ps_line(f"g{i}", f"cifix-v3-{i:08x}", 500) for i in range(250)
        ]
        summary = await reaper.reap_orphans(max_age_hours=6, limit=200)
        assert summary["found"] == 250
        assert summary["reaped"] == 200
        assert summary["truncated"] is True

    @pytest.mark.asyncio
    async def test_rm_failure_is_counted_not_raised(self, fake_docker):
        fake_docker["ps_lines"] = [_ps_line("f1", "cifix-v3-11111111", 500)]
        fake_docker["rm_code"] = 1
        summary = await reaper.reap_orphans(max_age_hours=6)
        assert summary["failed"] == 1
        assert summary["reaped"] == 0


# ── robustness ───────────────────────────────────────────────────────────────


class TestRobustness:
    @pytest.mark.asyncio
    async def test_docker_unavailable_yields_empty_not_exception(self, fake_docker):
        fake_docker["ps_code"] = -1
        assert await reaper.find_orphans() == []

    @pytest.mark.asyncio
    async def test_malformed_ps_lines_are_skipped(self, fake_docker):
        fake_docker["ps_lines"] = ["", "onlyone", "two\tfields", _ps_line("f1", "cifix-v3-11111111", 500)]
        assert [o["name"] for o in await reaper.find_orphans()] == ["cifix-v3-11111111"]

    def test_task_entrypoint_never_raises(self, monkeypatch):
        async def _boom(*a, **k):
            raise RuntimeError("docker exploded")
        monkeypatch.setattr(reaper, "reap_orphans", _boom)
        out = reaper.reap_sandboxes()
        assert out["reaped"] == 0
        assert "error" in out

    def test_registered_on_the_beat_schedule(self):
        """A reaper that isn't scheduled is the bug it exists to fix."""
        from phalanx.queue.celery_app import celery_app
        sched = celery_app.conf.beat_schedule
        entry = sched.get("reap-orphaned-sandboxes")
        assert entry is not None, "reaper missing from beat_schedule"
        assert entry["task"] == "phalanx.runtime.sandbox_reaper.reap_sandboxes"
        assert entry["schedule"] <= 3600
