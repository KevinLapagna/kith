"""Health and dashboard: the two things M0 is accepted on."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app import config, db


def test_health_ok(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["schema"] == config.SCHEMA_VERSION
    assert body["db_writable"] is True
    assert body["pragma"]["journal_mode"] == "wal"
    assert body["pragma"]["integrity"] == "ok"
    assert body["error"] is None


def test_health_reports_real_numbers(client: TestClient) -> None:
    body = client.get("/api/health").json()
    # A fresh database has no people yet, but the key must exist and be zero.
    assert body["people"] == 0
    assert body["expected_schema"] == config.SCHEMA_VERSION
    assert isinstance(body["uptime_s"], (int, float))


def test_dashboard_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Kith" in response.text
    assert "healthy" in response.text
    # The roadmap is honest about what does not exist yet.
    assert "M2" in response.text


def test_health_is_503_when_database_not_writable(settings: config.Settings) -> None:
    """A read-only database must NOT report healthy — silence here would surface as
    lost answers instead."""
    with db.connect(settings.db_path) as conn:  # create the file via a normal connect
        db.migrate(conn)

    ro = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, isolation_level=None)
    try:
        assert db.probe_write(ro) is False
    finally:
        ro.close()
