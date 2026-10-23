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


def test_manage_hosts_encrypts_passwords_persists_and_invalidates_sessions(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCKER_HOSTS_FILE", raising=False)
    store = Store(tmp_path / "data")
    values = {"name": "NAS", "url": "http://192.168.1.20:8088", "password": "private-remote-password"}
    with TestClient(create_app(store, start_worker=False)) as client:
        assert client.get("/api/host-config").status_code == 401
        assert client.post("/api/host-config", json=values, headers=HEADERS).status_code == 401
        login(client)
        assert client.post("/api/host-config", json=values).status_code == 403
        response = client.post("/api/host-config", json=values, headers=HEADERS)
        assert response.status_code == 200
        host_id = response.json()["id"]
        row = store.rows("SELECT * FROM remote_hosts")[0]
        assert values["password"] not in row["password"]
        assert store.cipher.decrypt(row["password"].encode()).decode() == values["password"]
        config = client.get("/api/host-config")
        assert values["password"] not in config.text
        assert config.json()["hosts"][0]["password_set"] is True
        assert config.json()["hosts"][0]["read_only"] is False
        assert client.get("/api/hosts").json()["hosts"][-1] == {"id": host_id, "name": "NAS"}
        # Blank password retains the secret for the same address, including after restart.
        response = client.put("/api/host-config/" + host_id,
                              json={**values, "name": "NAS updated", "password": ""}, headers=HEADERS)
        assert response.status_code == 200
        hosts = Hosts(store)
        assert hosts.hosts[host_id]["password"] == values["password"]
        assert hosts.hosts[host_id]["name"] == "NAS updated"
        assert client.put("/api/host-config/" + host_id,
                          json={**values, "url": "http://192.168.1.21:8088", "password": ""}, headers=HEADERS).status_code == 400
        hosts.sessions[("user", host_id)] = (99999999999, "cookie")
        hosts.save({**values, "password": "new-secret"}, host_id)
        assert not hosts.sessions
        response = client.delete("/api/host-config/" + host_id, headers=HEADERS)
        assert response.status_code == 200
        assert not Hosts(store).hosts
        assert not store.rows("SELECT * FROM remote_hosts")


def test_file_managed_hosts_are_read_only(configured, tmp_path):
    with TestClient(create_app(Store(tmp_path / "data"), start_worker=False)) as client:
        login(client)
        assert client.get("/api/host-config").json()["hosts"][0]["read_only"] is True
        assert client.delete("/api/host-config/nas", headers=HEADERS).status_code == 400
        assert client.put("/api/host-config/nas", json={"name": "NAS", "url": "http://nas", "password": "x"}, headers=HEADERS).status_code == 400


@pytest.mark.parametrize("url", ["file:///secret", "http://user:password@nas", "http://nas/path", "https://nas?x=1", "http://nas:invalid", "http://na s", "http://nas/#fragment"])
def test_ui_host_address_validation(tmp_path, monkeypatch, url):
    monkeypatch.delenv("DOCKER_HOSTS_FILE", raising=False)
    hosts = Hosts(Store(tmp_path / "data"))
    with pytest.raises(Exception, match="HTTP or HTTPS"):
        hosts.save({"name": "NAS", "url": url, "password": "secret"})
    assert not hosts.hosts


def test_connection_test_uses_unsaved_details_and_never_returns_password(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCKER_HOSTS_FILE", raising=False)
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.post.return_value = remote_login()
    dashboard = remote_login()
    dashboard.json.return_value = {"stacks": [], "docker_error": None}
    connection.get.return_value = dashboard
    monkeypatch.setattr("app.hosts.requests.Session", lambda: connection)
    store = Store(tmp_path / "data")
    with TestClient(create_app(store, start_worker=False)) as client:
        login(client)
        values = {"name": "NAS", "url": "https://nas", "password": "test-private-password"}
        response = client.post("/api/host-config/test", json=values, headers=HEADERS)
        assert response.status_code == 200
        assert "test-private-password" not in response.text
        assert not store.rows("SELECT * FROM remote_hosts")
        assert connection.post.call_args_list[0].kwargs["json"] == {"password": values["password"]}
        dashboard.json.return_value = {"stacks": [], "docker_error": "Docker offline"}
        response = client.post("/api/host-config/test", json=values, headers=HEADERS)
        assert response.status_code == 400
        assert "Docker is unavailable" in response.text
        connection.post.side_effect = requests.ConnectionError("sensitive connection details")
        response = client.post("/api/host-config/test", json=values, headers=HEADERS)
        assert response.status_code == 400
        assert "sensitive connection details" not in response.text
