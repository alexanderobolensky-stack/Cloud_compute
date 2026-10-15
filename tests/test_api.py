import sys

import pytest
from fastapi.testclient import TestClient

from agent_portal.app import create_app
from agent_portal.config import Settings

H = {"X-Portal-Request": "1"}


@pytest.fixture
def make_client(tmp_path):
    def factory(**overrides):
        settings = Settings(config_file=tmp_path / "agents.yaml", data_dir=tmp_path / "data",
                            sample_interval=0.5, **overrides)
        return TestClient(create_app(settings), base_url="http://localhost")
    return factory


def agent(**extra):
    return {"id": "echo", "name": "Эхо", "command": [sys.executable, "-c", "import time; time.sleep(30)"], **extra}


def test_crud_and_lifecycle(make_client, tmp_path):
    with make_client() as client:
        assert client.get("/api/agents").json() == []
        r = client.post("/api/agents", json=agent(), headers=H)
        assert r.status_code == 201, r.text
        assert client.post("/api/agents", json=agent(), headers=H).status_code == 409
        assert "echo" in (tmp_path / "agents.yaml").read_text()

        assert client.post("/api/agents/echo/start", headers=H).json()["state"] == "running"
        assert client.post("/api/agents/echo/stop", headers=H).json()["state"] == "stopped"

        updated = client.put("/api/agents/echo", json=agent(name="Новое имя", tags=["x"]), headers=H)
        assert updated.json()["name"] == "Новое имя"
        assert client.get("/api/agents/echo").json()["spec"]["tags"] == ["x"]

        messages = [e["message"] for e in client.get("/api/events?agent_id=echo").json()]
        assert "Агент добавлен" in messages

        assert client.delete("/api/agents/echo", headers=H).status_code == 204
        assert client.get("/api/agents/echo").status_code == 404


def test_validation(make_client):
    with make_client() as client:
        assert client.post("/api/agents", json={"id": "Bad Id", "name": "x", "command": "a"}, headers=H).status_code == 422
        assert client.post("/api/agents", json={"id": "x", "name": "x"}, headers=H).status_code == 422
        ext = {"id": "ext", "name": "Ext", "kind": "external", "process_match": "nothing-matches-this-xyz"}
        assert client.post("/api/agents", json=ext, headers=H).status_code == 201
        assert client.post("/api/agents/ext/start", headers=H).status_code == 400


def test_csrf_header_and_host_guard(make_client):
    with make_client() as client:
        assert client.post("/api/agents", json=agent()).status_code == 403
        assert client.get("/api/info", headers={"Host": "evil.example"}).status_code == 421
        assert client.get("/api/info").status_code == 200


def test_token_auth(make_client):
    with make_client(token="s3cret") as client:
        assert client.get("/api/agents").status_code == 401
        assert client.get("/api/agents", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert client.post("/api/auth", json={"token": "nope"}, headers=H).status_code == 401
        assert client.post("/api/auth", json={"token": "s3cret"}, headers=H).status_code == 200
        assert client.get("/api/agents").status_code == 200  # cookie set by /api/auth
        assert client.get("/").status_code == 200  # the UI shell itself is public


def test_live_websocket(make_client):
    with make_client() as client:
        with client.websocket_connect("/ws/live", headers={"Host": "localhost"}) as ws:
            msg = ws.receive_json()
            assert msg["type"] == "snapshot" and "agents" in msg


def test_cross_origin_websocket_rejected(make_client):
    from starlette.websockets import WebSocketDisconnect

    with make_client() as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/live", headers={"Host": "localhost", "Origin": "http://evil.example"}) as ws:
                ws.receive_json()
