"""Tests for application configuration."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.config import Settings, get_settings  # noqa: E402


def test_default_settings() -> None:
    """Settings use sensible defaults when no env vars are set."""
    settings = Settings()
    assert "kanban" in settings.database_url
    assert settings.access_token_expire_minutes == 30
    assert settings.jwt_algorithm == "HS256"
    assert settings.debug is False


def test_default_redis_url() -> None:
    """Settings include a default Redis URL."""
    settings = Settings()
    assert settings.redis_url == "redis://localhost:6379/0"


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings pick up values from environment variables."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://custom:pass@db:5432/mydb")
    monkeypatch.setenv("REDIS_URL", "redis://custom-redis:6380/1")
    monkeypatch.setenv("JWT_SECRET", "super-secret")
    monkeypatch.setenv("JWT_ALGORITHM", "HS512")
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60")
    monkeypatch.setenv("DEBUG", "true")

    settings = Settings()
    assert settings.database_url == "postgresql+asyncpg://custom:pass@db:5432/mydb"
    assert settings.redis_url == "redis://custom-redis:6380/1"
    assert settings.jwt_secret == "super-secret"
    assert settings.jwt_algorithm == "HS512"
    assert settings.access_token_expire_minutes == 60
    assert settings.debug is True


def test_get_settings_returns_instance() -> None:
    """get_settings() returns a Settings instance."""
    settings = get_settings()
    assert isinstance(settings, Settings)


def test_settings_is_frozen() -> None:
    """Settings dataclass is frozen (immutable)."""
    settings = Settings()
    with pytest.raises(AttributeError):
        settings.debug = True  # type: ignore[misc]


def test_test_database_url_default() -> None:
    """Default test database URL is an in-memory SQLite."""
    settings = Settings()
    assert settings.test_database_url == "sqlite+aiosqlite:///:memory:"


def test_cors_origins_default() -> None:
    """Default CORS origins is wildcard."""
    settings = Settings()
    assert settings.cors_origins == "*"
