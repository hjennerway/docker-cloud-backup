import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from app.main import create_app
from app.store import Store


HEADERS = {"X-Requested-With": "cloud-backup"}
OLD_PASSWORD = "test-password-only"
NEW_PASSWORD = "a different admin passphrase"
CHANGE = {"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD}


def login(client, password=OLD_PASSWORD):
    return client.post("/api/login", json={"password": password}, headers=HEADERS)


def test_password_change_persists_and_revokes_other_sessions(tmp_path, monkeypatch):
    store = Store(tmp_path)
    app = create_app(store, start_worker=False)
    # Another application instance must also see updated credentials immediately.
    other_app = create_app(Store(tmp_path), start_worker=False)
    serializer = URLSafeTimedSerializer(store.cipher._signing_key, salt="cloud-backup-session-v1")
    with TestClient(app) as client, TestClient(other_app) as other:
        assert login(client).status_code == 200
        assert login(other).status_code == 200
        old_cookie = client.cookies.get("session")
        original_session = serializer.loads(old_cookie)
        response = client.post("/api/password", json=CHANGE, headers=HEADERS)
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=lax" in response.headers["set-cookie"]
        assert OLD_PASSWORD not in response.text and NEW_PASSWORD not in response.text
        assert client.get("/api/session").json()["authenticated"] is True
        renewed_session = serializer.loads(client.cookies.get("session"))
        assert renewed_session["id"] == original_session["id"]
        assert renewed_session["auth"] != original_session["auth"]
        assert other.get("/api/session").json()["authenticated"] is False
        assert other.get("/api/settings").status_code == 401
        assert login(other).status_code == 401
        assert login(other, NEW_PASSWORD).status_code == 200
        assert OLD_PASSWORD not in str(store.rows("SELECT * FROM admin_credentials"))
        assert NEW_PASSWORD not in str(store.rows("SELECT * FROM admin_credentials"))
        assert "password_hash" not in client.get("/api/settings").text

    monkeypatch.setenv("ADMIN_PASSWORD", "a changed environment password")
    with TestClient(create_app(Store(tmp_path), start_worker=False)) as restarted:
        assert login(restarted).status_code == 401
        assert login(restarted, "a changed environment password").status_code == 401
        assert login(restarted, NEW_PASSWORD).status_code == 200
        assert restarted.get("/api/session").json()["authenticated"] is True


@pytest.mark.parametrize("generated", [True, False])
def test_existing_generated_password_migrates_and_file_is_removed_on_change(tmp_path, monkeypatch, generated):
    monkeypatch.delenv("ADMIN_PASSWORD")
    password_file = tmp_path / "admin-password.txt"
    if not generated:
        password_file.write_text("existing-bootstrap-password\n")
    with TestClient(create_app(Store(tmp_path), start_worker=False)) as client:
        password = password_file.read_text().strip()
        if not generated:
            assert password == "existing-bootstrap-password"
        assert login(client, password).status_code == 200
        assert client.post("/api/password", json={**CHANGE, "current_password": password}, headers=HEADERS).status_code == 200
        assert not password_file.exists()
    with TestClient(create_app(Store(tmp_path), start_worker=False)) as restarted:
        assert not password_file.exists()
        assert login(restarted, password).status_code == 401
        assert login(restarted, NEW_PASSWORD).status_code == 200


def test_password_change_requires_authentication_and_same_origin(tmp_path):
    with TestClient(create_app(Store(tmp_path), start_worker=False)) as client:
        assert client.post("/api/password", json=CHANGE, headers=HEADERS).status_code == 401
        assert login(client).status_code == 200
        assert client.post("/api/password", json=CHANGE).status_code == 403
        assert client.post("/api/password", json=CHANGE, headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
        assert login(client).status_code == 200
        assert login(client, NEW_PASSWORD).status_code == 401


@pytest.mark.parametrize("changes,status,detail", [
    ({"current_password": "wrong"}, 400, "Incorrect password"),
    ({"confirm_password": "another password"}, 400, "do not match"),
    ({"new_password": OLD_PASSWORD, "confirm_password": OLD_PASSWORD}, 400, "different"),
    ({"new_password": "short", "confirm_password": "short"}, 422, None),
    ({"new_password": "x" * 1025, "confirm_password": "x" * 1025}, 422, None),
    ({"current_password": "x" * 1025}, 422, None),
])
def test_invalid_password_changes_preserve_password_and_session(tmp_path, changes, status, detail):
    with TestClient(create_app(Store(tmp_path), start_worker=False)) as client:
        assert login(client).status_code == 200
        response = client.post("/api/password", json={**CHANGE, **changes}, headers=HEADERS)
        assert response.status_code == status
        if detail:
            assert detail in response.json()["detail"]
        assert client.get("/api/session").json()["authenticated"] is True
        assert login(client).status_code == 200
        assert login(client, NEW_PASSWORD).status_code == 401


def test_password_change_is_throttled_and_cannot_bypass_login_limit(tmp_path):
    with TestClient(create_app(Store(tmp_path), start_worker=False)) as client:
        assert login(client).status_code == 200
        for _ in range(10):
            assert client.post("/api/password", json={**CHANGE, "current_password": "wrong"}, headers=HEADERS).status_code == 400
        assert client.post("/api/password", json=CHANGE, headers=HEADERS).status_code == 429
        assert login(client).status_code == 429
        assert client.get("/api/session").json()["authenticated"] is True


def test_failed_bootstrap_file_cleanup_preserves_credentials(tmp_path, monkeypatch):
    from pathlib import Path

    password_file = tmp_path / "admin-password.txt"
    password_file.write_text("old generated password")
    with TestClient(create_app(Store(tmp_path), start_worker=False)) as client:
        assert login(client).status_code == 200
        def denied(*args, **kwargs):
            raise PermissionError("fixture")
        monkeypatch.setattr(Path, "unlink", denied)
        response = client.post("/api/password", json=CHANGE, headers=HEADERS)
        assert response.status_code == 400
        assert "permissions" in response.json()["detail"]
        assert password_file.exists()
        assert client.get("/api/session").json()["authenticated"] is True
        assert login(client).status_code == 200


def test_new_password_can_be_changed_again_and_uses_secure_cookies(tmp_path, monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "true")
    with TestClient(create_app(Store(tmp_path), start_worker=False), base_url="https://testserver") as client:
        assert login(client).status_code == 200
        assert client.post("/api/password", json=CHANGE, headers=HEADERS).status_code == 200
        password = "yet another admin passphrase"
        response = client.post("/api/password", json={"current_password": NEW_PASSWORD,
                               "new_password": password, "confirm_password": password}, headers=HEADERS)
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]
        assert client.post("/api/logout", headers=HEADERS).status_code == 200
        assert login(client, NEW_PASSWORD).status_code == 401
        assert login(client, password).status_code == 200
