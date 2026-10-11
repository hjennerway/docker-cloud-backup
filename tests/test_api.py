from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store


def test_api_requires_auth_and_csrf_header(tmp_path):
    app = create_app(Store(tmp_path), start_worker=False)
    with TestClient(app) as client:
        assert client.get("/api/dashboard").status_code == 401
        assert client.post("/api/login", json={"password": "test-password-only"}).status_code == 403
        headers = {"X-Requested-With": "cloud-backup"}
        assert client.post("/api/login", json={"password": "wrong"}, headers=headers).status_code == 401
        response = client.post("/api/login", json={"password": "test-password-only"}, headers=headers)
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert client.get("/api/session").json()["authenticated"] is True
        response = client.put("/api/settings", json={"smb_password": "secret-fixture"}, headers=headers)
        assert response.status_code == 200
        assert "secret-fixture" not in response.text
        assert response.json()["smb_password_set"] is True
        assert client.put("/api/settings", json={"keep_days": 7}, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
        assert client.put("/api/settings", json={"keep_days": -1}, headers=headers).status_code == 400
        assert client.post("/api/cron/preview", json={"cron":"0 3 * * *","timezone":"Europe/London","enabled":False}, headers=headers).status_code == 200
        assert client.post("/api/logout", headers=headers).status_code == 200
        assert not client.get("/api/session").json()["authenticated"]


def test_oauth_rejects_private_ip_web_callback(tmp_path):
    store = Store(tmp_path)
    store.save_settings({"drive_client_id":"fixture","drive_client_secret":"fixture","oauth_redirect_uri":"http://192.168.1.10/oauth/google/callback"})
    with TestClient(create_app(store, start_worker=False)) as client:
        headers = {"X-Requested-With": "cloud-backup"}
        client.post("/api/login", json={"password":"test-password-only"}, headers=headers)
        response = client.post("/api/drive/connect", headers=headers)
        assert response.status_code == 400
        assert "HTTPS" in response.json()["detail"]
        assert client.get("/oauth/google/callback?state=forged&code=forged").status_code == 400
