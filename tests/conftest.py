"""Shared fixtures. Tests never touch the real data directory: every test gets its
own tmp_path database, and migrations run through the same code path the app uses."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> config.Settings:
    return config.Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "kith.sqlite",
        host="127.0.0.1",
        port=8080,
        auth_enabled=False,
        display_tz="Europe/Zurich",
        log_level="warning",
    )


@pytest.fixture
def client(settings: config.Settings) -> Iterator[TestClient]:
    # Using TestClient as a context manager runs the lifespan, so migrations happen
    # exactly as they would in the container.
    with TestClient(create_app(settings)) as test_client:
        yield test_client
