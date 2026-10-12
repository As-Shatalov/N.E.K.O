"""``GET /visit/transport`` serves the transport iframe page (design §5 PR-10)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from main_routers import pages_router

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _client(monkeypatch) -> TestClient:
    templates = Jinja2Templates(directory=str(PROJECT_ROOT))
    monkeypatch.setattr(pages_router, "get_templates", lambda: templates)
    app = FastAPI()
    app.include_router(pages_router.router)
    return TestClient(app)


def test_route_is_registered_without_trailing_slash():
    paths = {getattr(r, "path", None) for r in pages_router.router.routes}
    assert "/visit/transport" in paths
    assert "/visit/transport/" not in paths


def test_page_injects_the_static_asset_version(monkeypatch):
    version = pages_router._static_assets_ctx()["static_asset_version"]
    resp = _client(monkeypatch).get("/visit/transport?v=1&side=guest&visit_id=AbCdEfGhIjKlMnOpQrStUv")
    assert resp.status_code == 200
    assert f"/static/visit/transport/loader.js?v={version}" in resp.text
    assert 'id="visit-transport-root"' in resp.text
    assert "{{" not in resp.text


def test_transport_scripts_feed_the_static_asset_version():
    loaded = {p.as_posix().split("/static/", 1)[1] for p in pages_router._TEMPLATE_STATIC_ASSET_VERSION_PATHS}
    for name in ("transport.js", "loader.js", "backend-ws.js", "pack.js"):
        assert f"visit/transport/{name}" in loaded
