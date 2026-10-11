from types import SimpleNamespace

import pytest

from app.destinations import Destinations
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
