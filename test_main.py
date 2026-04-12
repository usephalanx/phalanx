"""Tests for the root GET / endpoint."""

from starlette.testclient import TestClient

from main import app

client = TestClient(app)


def test_root_returns_200() -> None:
    """GET / should return HTTP 200."""
    response = client.get("/")
    assert response.status_code == 200


def test_root_returns_hello_world_message() -> None:
    """GET / response should contain the expected greeting message."""
    response = client.get("/")
    assert response.json()["message"] == "Hello, World!"


def test_root_returns_version() -> None:
    """GET / response should contain version 1.0.0."""
    response = client.get("/")
    assert response.json()["version"] == "1.0.0"


def test_root_content_type_is_json() -> None:
    """GET / response Content-Type should be application/json."""
    response = client.get("/")
    assert response.headers["content-type"].startswith("application/json")


def test_root_response_schema() -> None:
    """GET / response should contain exactly 'message' and 'version' keys."""
    response = client.get("/")
    assert set(response.json().keys()) == {"message", "version"}
