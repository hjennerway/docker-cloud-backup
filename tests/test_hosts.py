import json
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient

from app.hosts import Hosts
from app.main import create_app
from app.store import Store


HEADERS = {"X-Requested-With": "cloud-backup"}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    config = tmp_path / "hosts.json"
    config.write_text(json.dumps([{"id": "nas", "name": "NAS", "url": "https://nas.example",
                                   "password": "remote-secret"}]))
    monkeypatch.setenv("DOCKER_HOSTS_FILE", str(config))
    return config


def login(client):
    assert client.post("/api/login", json={"password": "test-password-only"}, headers=HEADERS).status_code == 200


def remote_login(cookie="remote-session"):
    response = Mock(status_code=200)
    response.cookies = {"session": cookie}
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


def test_host_configuration_is_private_and_requires_sign_in(configured, tmp_path):
    with TestClient(create_app(Store(tmp_path / "data"), start_worker=False)) as client:
        assert client.get("/api/hosts").status_code == 401
        assert client.get("/api/hosts/nas/dashboard").status_code == 401
        login(client)
        response = client.get("/api/hosts")
        assert response.json() == {"hosts": [{"id": "local", "name": "Local Docker host"}, {"id": "nas", "name": "NAS"}]}
        assert "remote-secret" not in response.text
        assert "nas.example" not in response.text
        assert client.get("/api/hosts/missing/dashboard").status_code == 404
        assert client.post("/api/hosts/nas/restore", json={}).status_code == 403


@pytest.mark.parametrize("entry", [
    {"id": "local"}, {"url": "file:///etc/passwd"}, {"url": "https://user:pass@nas.example"},
    {"url": "https://nas.example/subpath"}, {"id": "../nas"}, {"password": ""},
])
def test_invalid_configuration_fails_without_leaking_credentials(configured, entry):
    config = json.loads(configured.read_text())
    config[0].update(entry)
    configured.write_text(json.dumps(config))
    with pytest.raises(RuntimeError, match="Invalid Docker hosts file") as error:
        Hosts()
    assert "remote-secret" not in str(error.value)


def test_sessions_are_reused_and_isolated_by_portal_session(configured, monkeypatch):
    post = Mock(side_effect=[remote_login("one"), remote_login("two"), remote_login("three")])
    monkeypatch.setattr("app.hosts.requests.post", post)
    hosts = Hosts()
    assert hosts.cookie("nas", "user1") == "one"
    assert hosts.cookie("nas", "user1") == "one"
    assert hosts.cookie("nas", "user2") == "two"
    hosts.forget("user1")
    assert hosts.cookie("nas", "user1") == "three"
    assert hosts.cookie("nas", "user2") == "two"
    assert post.call_count == 3


def test_remote_actions_uploads_and_downloads(configured, tmp_path, monkeypatch):
    monkeypatch.setattr("app.hosts.requests.post", Mock(return_value=remote_login()))
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs, kwargs["data"].read() if kwargs["data"] else b""))
        response = Mock(status_code=200)
        response.headers = {"Content-Type": "application/json", "Set-Cookie": "never-forward-this"}
        response.iter_content.return_value = iter([b'{"ok":true}'])
        if url.endswith("/download"):
            response.headers = {"Content-Type": "application/gzip", "Content-Disposition": 'attachment; filename="backup.tar.gz"'}
            response.iter_content.return_value = iter([b"archive-", b"contents"])
        return response

    monkeypatch.setattr("app.hosts.requests.request", request)
    with TestClient(create_app(Store(tmp_path / "data"), start_worker=False)) as client:
        login(client)
        for path in ["stacks/same-name/backup", "restore/preview", "restore"]:
            response = client.post("/api/hosts/nas/" + path, json={"confirmed": True}, headers=HEADERS)
            assert response.json() == {"ok": True}
            assert "set-cookie" not in response.headers
        # Files larger than the normal API body limit must reach the remote host.
        payload = b"backup data" * 15000
        response = client.post("/api/hosts/nas/restore/upload?filename=backup.tar.gz", content=payload,
                               headers={**HEADERS, "Content-Type": "application/octet-stream"})
        assert response.status_code == 200
        assert calls[-1][3] == payload
        assert calls[-1][2]["params"] == [("filename", "backup.tar.gz")]
        response = client.get("/api/hosts/nas/jobs/job-id/download")
        assert response.content == b"archive-contents"
        assert response.headers["content-disposition"] == 'attachment; filename="backup.tar.gz"'
        assert all(call[2]["cookies"] == {"session": "remote-session"} for call in calls)
        assert all(call[2]["allow_redirects"] is False for call in calls)


def test_remote_failure_and_forbidden_actions(configured, tmp_path, monkeypatch):
    post = Mock(side_effect=requests.ConnectionError("credential-sensitive-detail"))
    monkeypatch.setattr("app.hosts.requests.post", post)
    with TestClient(create_app(Store(tmp_path / "data"), start_worker=False)) as client:
        login(client)
        for path in ["login", "password", "hosts/another/dashboard", "drive/connect"]:
            response = client.post("/api/hosts/nas/" + path, json={}, headers=HEADERS)
            assert response.status_code == 400
        assert post.call_count == 0
        response = client.get("/api/hosts/nas/dashboard")
        assert response.status_code == 400
        assert "credential-sensitive-detail" not in response.text
        assert client.get("/api/hosts").status_code == 200
        assert client.get("/api/session").json()["authenticated"] is True


def test_streamed_body_limits(configured, tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.restore_limit", lambda: 200000)
    with TestClient(create_app(Store(tmp_path / "data"), start_worker=False)) as client:
        login(client)
        response = client.post("/api/hosts/nas/restore/upload", content=iter([b"x" * 100001, b"x" * 100000]), headers=HEADERS)
        assert response.status_code == 413
        response = client.put("/api/hosts/nas/settings", content=iter([b"x" * 100001]), headers=HEADERS)
        assert response.status_code == 413
