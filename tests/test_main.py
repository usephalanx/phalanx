"""Tests for the FastAPI Hello World endpoint."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_root_returns_200() -> None:
    """GET / should return HTTP 200 OK."""
    response = client.get("/")
    assert response.status_code == 200


def test_root_returns_hello_message() -> None:
    """GET / should return the expected JSON payload."""
    response = client.get("/")
    assert response.json() == {"message": "Hello, World!", "version": "1.0.0"}


def test_root_content_type_is_json() -> None:
    """GET / should return application/json content type."""
    response = client.get("/")
    assert response.headers["content-type"] == "application/json"


def test_undefined_route_returns_404() -> None:
    """GET on an undefined path should return HTTP 404."""
    response = client.get("/nonexistent")
    assert response.status_code == 404
