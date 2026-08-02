from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("S15_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("S15_A2A_GRPC_ENABLED", "0")
    monkeypatch.setenv("S15_SANDBOX_ROOT", str(tmp_path / "sandbox"))
    (tmp_path / "sandbox").mkdir()
    # A developer's own .env may point this at a real, running collector (ours
    # does, at a live Jaeger). Tests assert the no-collector-configured default,
    # so they must not inherit whatever the local environment happens to export.
    monkeypatch.delenv("S15_OTEL_EXPORTER_ENDPOINT", raising=False)


@pytest.fixture
def app_client():
    from fastapi.testclient import TestClient

    from s15code.main import app

    with TestClient(app) as client:
        yield client
