import json
from types import SimpleNamespace

import pytest

from app.destinations import Destinations, DriveRateLimitError
from app.discovery import BackupError
from app.store import Store


@pytest.mark.parametrize("error,expected", [
    ("The attempted logon is invalid. fixture-secret", "SMB login was rejected"),
    ("invalid_grant fixture-secret", "Google Drive authorization expired"),
    ("Access is denied fixture-secret", "destination denied access"),
    ("dial tcp: connection refused fixture-secret", "could not be reached"),
    ("Unknown provider error fixture-secret", "Destination operation failed"),
])
def test_destination_errors_are_actionable_without_leaking_secrets(tmp_path, monkeypatch, error, expected):
    dest = Destinations(Store(tmp_path))
    monkeypatch.setattr("app.destinations.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=1, stderr=error, stdout=""))
    with pytest.raises(BackupError) as exc:
        dest._run(["lsjson", "nas:share"])
    assert expected in str(exc.value)
    assert "fixture-secret" not in str(exc.value)


@pytest.fixture
def quota_clock(monkeypatch):
    clock = SimpleNamespace(now=0, delays=[])
    def sleep(delay):
        clock.delays.append(delay)
        clock.now += delay
    monkeypatch.setattr("app.destinations.time.monotonic", lambda: clock.now)
    monkeypatch.setattr("app.destinations.time.sleep", sleep)
    monkeypatch.setattr("app.destinations.random.uniform", lambda *args: 0.5)
    return clock


@pytest.mark.parametrize("error", [
    "googleapi: Error 403: userRateLimitExceeded",
    "googleapi: Error 403: rateLimitExceeded",
    "googleapi: Error 429: Too Many Requests",
    "googleapi: Error 403: Quota exceeded for quota metric 'Queries' and limit 'Queries per minute', quotaExceeded",
])
def test_drive_throttling_recovers_with_pacing_and_backoff(tmp_path, monkeypatch, quota_clock, error):
    dest = Destinations(Store(tmp_path))
    attempts = []
    def run(cmd, **kwargs):
        attempts.append((cmd, kwargs["timeout"]))
        quota_clock.now += 2
        return SimpleNamespace(returncode=len(attempts) == 1, stderr=error + " fixture-secret", stdout="[]")
    monkeypatch.setattr("app.destinations.subprocess.run", run)
    assert dest._run(["lsjson", "gdrive:"], config=tmp_path / "rclone.conf") == "[]"
    assert quota_clock.delays == [10.5]
    assert len(attempts) == 2
    assert attempts[0][1] == 120
    assert attempts[1][1] == 107.5  # All attempts share the original deadline.
    cmd = attempts[0][0]
    assert cmd[cmd.index("--tpslimit") + 1] == "2"
    assert cmd[cmd.index("--tpslimit-burst") + 1] == "1"
    assert cmd[cmd.index("--drive-pacer-burst") + 1] == "1"
    assert cmd[cmd.index("--low-level-retries") + 1] == "8"


def test_persistent_drive_throttling_stops_after_bounded_retries(tmp_path, monkeypatch, quota_clock):
    dest = Destinations(Store(tmp_path))
    attempts = []
    def run(cmd, **kwargs):
        attempts.append(kwargs["timeout"])
        return SimpleNamespace(returncode=1, stderr="googleapi: Error 403: rateLimitExceeded fixture-secret", stdout="")
    monkeypatch.setattr("app.destinations.subprocess.run", run)
    with pytest.raises(DriveRateLimitError, match="rateLimitExceeded") as exc:
        dest._run(["copyto", "archive.tar.gz", "gdrive:archive.tar.gz"], timeout=3600)
    assert len(attempts) == 4
    assert quota_clock.delays == [10.5, 20.5, 40.5]
    assert "fixture-secret" not in str(exc.value)


def test_drive_backoff_does_not_exceed_operation_timeout(tmp_path, monkeypatch, quota_clock):
    dest = Destinations(Store(tmp_path))
    attempts = []
    def run(cmd, **kwargs):
        attempts.append(kwargs["timeout"])
        quota_clock.now += 15
        return SimpleNamespace(returncode=1, stderr="googleapi: Error 403: rateLimitExceeded", stdout="")
    monkeypatch.setattr("app.destinations.subprocess.run", run)
    with pytest.raises(DriveRateLimitError):
        dest._run(["lsjson", "gdrive:"], timeout=20)
    assert attempts == [20]
    assert quota_clock.delays == []


@pytest.mark.parametrize("error,expected", [
    ("googleapi: Error 403: storageQuotaExceeded", "storage limit"),
    ("googleapi: Error 403: dailyLimitExceeded", "daily API limit"),
    ("Quota exceeded for limit 'Queries per day', quotaExceeded", "daily API limit"),
    ("googleapi: Error 403: quotaExceeded", "API quota"),
    ("googleapi: Error 403: insufficientFilePermissions", "denied access"),
    ("oauth2: invalid_grant", "authorization expired"),
])
def test_non_transient_drive_errors_are_not_retried(tmp_path, monkeypatch, quota_clock, error, expected):
    dest = Destinations(Store(tmp_path))
    calls = []
    monkeypatch.setattr("app.destinations.subprocess.run", lambda *args, **kwargs:
                        calls.append(args) or SimpleNamespace(returncode=1, stderr=error + " fixture-secret", stdout=""))
    with pytest.raises(BackupError, match=expected) as exc:
        dest._run(["lsjson", "gdrive:"])
    assert len(calls) == 1
    assert quota_clock.delays == []
    assert "fixture-secret" not in str(exc.value)


def test_upload_verification_throttling_retries_listing_without_reupload(tmp_path, monkeypatch, quota_clock):
    store = Store(tmp_path / "data")
    store.save_settings({"drive_enabled": True, "drive_folder_id": "fixture-folder",
                         "drive_token": json.dumps({"access_token": "fixture-secret", "refresh_token": "fixture-refresh"})})
    archive = tmp_path / "backup.tar.gz"
    archive.write_bytes(b"fixture backup")
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd[1])
        if calls == ["copyto", "lsjson"]:
            return SimpleNamespace(returncode=1, stderr="googleapi: Error 403: userRateLimitExceeded", stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"Size": archive.stat().st_size}))
    monkeypatch.setattr("app.destinations.subprocess.run", run)
    receipts, errors = Destinations(store).upload(archive, "fixture-stack")
    assert calls == ["copyto", "lsjson", "lsjson"]
    assert errors == []
    assert receipts["drive"]["identity"] == "drive:fixture-folder"


def test_smb_does_not_get_google_pacing_or_extra_retries(tmp_path, monkeypatch, quota_clock):
    calls = []
    monkeypatch.setattr("app.destinations.subprocess.run", lambda cmd, **kwargs:
                        calls.append(cmd) or SimpleNamespace(returncode=0, stderr="", stdout="[]"))
    assert Destinations(Store(tmp_path))._run(["lsjson", "nas:share"], config=tmp_path / "rclone.conf") == "[]"
    assert "--tpslimit" not in calls[0]
    assert calls[0][calls[0].index("--low-level-retries") + 1] == "2"
    assert quota_clock.delays == []
