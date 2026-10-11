import configparser
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.destinations import Destinations, validate_settings
from app.discovery import BackupError
from app.main import create_app
from app.store import Store


def drive_token(name):
    return json.dumps({"access_token": name, "refresh_token": "fixture-refresh-secret",
                       "token_type": "Bearer", "expiry": "2099-01-01T00:00:00Z"})


def read_config(cmd):
    config = configparser.RawConfigParser()
    path = cmd[cmd.index("--config") + 1]
    config.read(path, encoding="utf-8")
    return config, path


def result(output="[]", error=""):
    return SimpleNamespace(returncode=bool(error), stdout=output, stderr=error)


@pytest.mark.parametrize("error,expected", [
    ("googleapi: Error 401: Invalid Credentials, authError", "rejected the Drive access token"),
    ('failed to refresh token: oauth2: "invalid_client"', "rejected the OAuth client"),
    ('oauth2: "unauthorized_client"', "rejected the OAuth client"),
    ('oauth2: "deleted_client"', "rejected the OAuth client"),
    ("googleapi: Error 403: accessNotConfigured", "Drive API is disabled"),
    ("googleapi: Error 403: SERVICE_DISABLED", "Drive API is disabled"),
    ("googleapi: Error 403: insufficientPermissions", "lacks Drive permissions"),
    ("googleapi: Error 403: insufficient authentication scopes", "lacks Drive permissions"),
    ("googleapi: Error 403: userRateLimitExceeded", "limit was reached"),
    ("googleapi: Error 403: storageQuotaExceeded", "limit was reached"),
])
def test_google_errors_are_specific_and_redacted(tmp_path, monkeypatch, error, expected):
    dest = Destinations(Store(tmp_path))
    monkeypatch.setattr("app.destinations.subprocess.run", lambda *a, **k: result(error=error + " fixture-secret"))
    with pytest.raises(BackupError) as exc:
        dest._run(["lsjson", "gdrive:"])
    assert expected in str(exc.value)
    assert "fixture-secret" not in str(exc.value)


@pytest.mark.parametrize("folder_id", ["root", "nested-folder-id"])
@pytest.mark.parametrize("custom_client", [False, True])
def test_folder_browser_uses_drive_credentials_and_ignores_unrelated_smb(tmp_path, monkeypatch, folder_id, custom_client):
    store = Store(tmp_path)
    store.save_settings({"drive_token": drive_token("fixture-access"), "drive_folder_id": "saved-backup-folder",
                         "smb_path": "an unfinished SMB setting", "smb_password": "fixture-smb-secret",
                         "drive_client_id": "fixture-client" if custom_client else "",
                         "drive_client_secret": "fixture-client-secret" if custom_client else ""})

    def listing(cmd, **kwargs):
        config, _ = read_config(cmd)
        assert config.sections() == ["gdrive"]
        assert config["gdrive"]["root_folder_id"] == folder_id
        assert config["gdrive"]["scope"] == "drive"
        assert config["gdrive"].get("client_id", "") == ("fixture-client" if custom_client else "")
        assert config["gdrive"].get("client_secret", "") == ("fixture-client-secret" if custom_client else "")
        assert cmd[:7] == ["rclone", "lsjson", "gdrive:", "--dirs-only", "--max-depth", "1", "--config"]
        return result(json.dumps([{"ID": "b", "Name": "zebra"}, {"ID": "a", "Name": "Alpha"}]))

    monkeypatch.setattr("app.destinations.subprocess.run", listing)
    assert Destinations(store).folders(folder_id) == [{"id": "a", "name": "Alpha"}, {"id": "b", "name": "zebra"}]


@pytest.mark.parametrize("candidate", [False, True])
@pytest.mark.parametrize("retry_error", ["", 'failed to refresh token: oauth2: "invalid_grant"', "googleapi: Error 401: authError"])
def test_rejected_access_token_refreshes_once_without_saving_failed_candidates(tmp_path, monkeypatch, candidate, retry_error):
    store = Store(tmp_path)
    original = drive_token("fixture-original")
    refreshed = drive_token("fixture-refreshed")
    store.save_settings({"drive_token": original})
    settings = store.settings()
    attempts = []

    def listing(cmd, **kwargs):
        config, path = read_config(cmd)
        token = json.loads(config["gdrive"]["token"])
        attempts.append(token)
        if len(attempts) == 1:
            return result(error="googleapi: Error 401: Invalid Credentials, authError")
        assert token["expiry"] == "1970-01-01T00:00:00Z"
        assert token["refresh_token"] == "fixture-refresh-secret"
        if retry_error:
            return result(error=retry_error)
        config["gdrive"]["token"] = refreshed
        with open(path, "w", encoding="utf-8") as handle:
            config.write(handle)
        return result('[{"ID":"folder","Name":"Backups"}]')

    monkeypatch.setattr("app.destinations.subprocess.run", listing)
    dest = Destinations(store)
    if retry_error:
        with pytest.raises(BackupError):
            dest.folders(settings=settings if candidate else None)
    else:
        assert dest.folders(settings=settings if candidate else None) == [{"id": "folder", "name": "Backups"}]
        if candidate:
            assert settings["drive_token"] == refreshed
    assert len(attempts) == 2
    assert store.settings()["drive_token"] == (original if candidate or retry_error else refreshed)


@pytest.mark.parametrize("output", ["not JSON", "{}", "null", "[null]", '[{"ID":"folder"}]'])
def test_unreadable_folder_listing_returns_safe_error(tmp_path, monkeypatch, output):
    store = Store(tmp_path)
    store.save_settings({"drive_token": drive_token("fixture-access")})
    monkeypatch.setattr("app.destinations.subprocess.run", lambda *a, **k: result(output))
    with pytest.raises(BackupError, match="unreadable folder listing"):
        Destinations(store).folders()


def test_refresh_cannot_overwrite_a_replaced_connection(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save_settings({"drive_token": drive_token("fixture-old")})
    replacement = drive_token("fixture-replacement")

    def listing(cmd, **kwargs):
        config, path = read_config(cmd)
        config["gdrive"]["token"] = drive_token("fixture-refreshed-old")
        with open(path, "w", encoding="utf-8") as handle:
            config.write(handle)
        store.save_settings({"drive_token": replacement})
        return result()

    monkeypatch.setattr("app.destinations.subprocess.run", listing)
    assert Destinations(store).folders() == []
    assert store.settings()["drive_token"] == replacement


@pytest.mark.parametrize("fail", [False, True])
def test_token_connection_is_verified_before_saving_and_preserves_existing_settings(tmp_path, monkeypatch, fail):
    store = Store(tmp_path)
    store.save_settings({"drive_token": drive_token("fixture-old"), "drive_enabled": True,
                         "drive_folder_id": "old-folder", "drive_folder_name": "Old backups"})
    original = store.settings()
    refreshed = drive_token("fixture-refreshed")

    def listing(cmd, **kwargs):
        config, path = read_config(cmd)
        assert config["gdrive"]["root_folder_id"] == "root"
        assert json.loads(config["gdrive"]["token"])["access_token"] == "fixture-candidate"
        # Even a refreshed candidate must not replace the working connection
        # until its read probe succeeds.
        config["gdrive"]["token"] = refreshed
        with open(path, "w", encoding="utf-8") as handle:
            config.write(handle)
        return result(error="accessNotConfigured fixture-secret" if fail else "")

    monkeypatch.setattr("app.destinations.subprocess.run", listing)
    with TestClient(create_app(store, start_worker=False)) as client:
        headers = {"X-Requested-With": "cloud-backup"}
        client.post("/api/login", json={"password": "test-password-only"}, headers=headers)
        response = client.put("/api/settings", json={"drive_token": drive_token("fixture-candidate"),
                              "drive_enabled": False, "drive_folder_id": "", "drive_folder_name": ""}, headers=headers)
        assert response.status_code == (400 if fail else 200)
        assert "fixture-secret" not in response.text
        assert "fixture-candidate" not in response.text
        if fail:
            assert "Drive API is disabled" in response.json()["detail"]
            assert store.settings() == original
        else:
            assert response.json()["drive_token"] == ""
            assert response.json()["drive_token_set"] is True
            assert store.settings()["drive_token"] == refreshed
            assert store.settings()["drive_enabled"] is False


@pytest.mark.parametrize("fail", [False, True])
def test_browser_oauth_also_verifies_folder_access_before_replacing_token(tmp_path, monkeypatch, fail):
    store = Store(tmp_path)
    store.save_settings({"drive_token": drive_token("fixture-old"), "drive_client_id": "fixture-client",
                         "drive_client_secret": "fixture-secret", "oauth_redirect_uri": "http://localhost/oauth/google/callback"})
    original = store.settings()
    token_response = SimpleNamespace(raise_for_status=lambda: None,
                                    json=lambda: json.loads(drive_token("fixture-browser")))
    monkeypatch.setattr("app.main.requests.post", lambda *a, **k: token_response)
    monkeypatch.setattr("app.destinations.subprocess.run", lambda *a, **k: result(error="insufficientPermissions" if fail else ""))
    with TestClient(create_app(store, start_worker=False)) as client:
        headers = {"X-Requested-With": "cloud-backup"}
        client.post("/api/login", json={"password": "test-password-only"}, headers=headers)
        auth_url = client.post("/api/drive/connect", headers=headers).json()["url"]
        state = parse_qs(urlparse(auth_url).query)["state"][0]
        response = client.get("/oauth/google/callback", params={"state": state, "code": "fixture-code"}, follow_redirects=False)
        assert response.status_code == (400 if fail else 303)
        if fail:
            assert "lacks Drive permissions" in response.json()["detail"]
            assert store.settings() == original
        else:
            assert json.loads(store.settings()["drive_token"])["access_token"] == "fixture-browser"


@pytest.mark.parametrize("refresh_token", [True, 123, "", " "])
def test_invalid_refresh_token_is_rejected_before_running_rclone(tmp_path, refresh_token):
    settings = Store(tmp_path).settings()
    settings["drive_token"] = json.dumps({"refresh_token": refresh_token})
    with pytest.raises(BackupError, match="complete token JSON"):
        validate_settings(settings)
