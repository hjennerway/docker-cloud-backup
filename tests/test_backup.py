import io
import json
import tarfile
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.backup import BackupEngine, append_archive, next_run, stack_key, utcnow
from app.destinations import Destinations, parse_smb, validate_settings
from app.discovery import BackupError, Discovery, image_repository, match_rule, validate_rule
from app.store import Store


def tar_bytes(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for name, value in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(value)
            entry.mode = 0o640
            tar.addfile(entry, io.BytesIO(value))
    return stream.getvalue()


class FakeContainer:
    def __init__(self, name, events, running=True):
        self.name = self.id = name
        self.status = "running" if running else "exited"
        self.events = events
        self.fail_archive = self.fail_start = self.fail_stop = False
        self.labels = {"com.docker.compose.project": "sample", "com.docker.compose.service": name,
                       "com.docker.compose.project.config_files": "/stacks/sample/compose.yaml",
                       "com.docker.compose.project.working_dir": "/stacks/sample"}
        self.attrs = {"Config": {"Image": "test/service:latest", "Env": []},
                      "Mounts": [{"Type": "volume", "Destination": "/config"}, {"Type": "bind", "Destination": "/media"}]}

    def reload(self):
        pass

    def stop(self, timeout=30):
        self.events.append("stop:" + self.name)
        self.status = "exited"
        if self.fail_stop:
            raise RuntimeError("stop response lost")

    def start(self):
        self.events.append("start:" + self.name)
        if self.fail_start:
            raise RuntimeError("start failed")
        self.status = "running"

    def get_archive(self, path, **kwargs):
        self.events.append("archive:" + self.name)
        assert self.status != "running"
        assert path == "/config"
        if self.fail_archive:
            raise RuntimeError("archive failed")
        return iter([tar_bytes({"config/settings.json": b'{"ok": true}', "config/cache/trash": b"not needed"})]), {}


class FakeClient:
    def __init__(self, containers):
        self.items = {c.id: c for c in containers}
        self.containers = SimpleNamespace(list=lambda all: list(self.items.values()), get=lambda cid: self.items[cid])
        self.api = SimpleNamespace(head=lambda *a, **kw: SimpleNamespace(close=lambda: None),
                                   _url=lambda *a: "url", _raise_for_status=lambda res: None)

    def close(self):
        pass


class FakeDestinations:
    def __init__(self, events, errors=None):
        self.events, self.errors, self.deleted = events, errors or [], []

    def upload(self, archive, key, existing=None):
        self.events.append("upload")
        assert archive.is_file()
        return {}, self.errors

    def delete_receipts(self, receipts):
        self.deleted.append(receipts)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr("app.backup.time.sleep", lambda n: None)
    store = Store(tmp_path / "data")
    store.execute("INSERT INTO rules VALUES (?,?,?)", ("test/service", '["/config"]', '["cache/**"]'))
    root = tmp_path / "host"
    folder = root / "stacks/sample"
    folder.mkdir(parents=True)
    (folder / "compose.yaml").write_text("services:\n  db:\n    image: test/service\n  web:\n    image: test/service\n    depends_on: [db]\n  stopped:\n    image: test/service\n")
    (folder / ".env").write_text("APP_SECRET=fixture-only\n")
    events = []
    db, web, stopped = [FakeContainer(n, events, n != "stopped") for n in ("db", "web", "stopped")]
    client = FakeClient([db, web, stopped])
    discovery = Discovery(client, store, root, "/stacks")
    destinations = FakeDestinations(events)
    engine = BackupEngine(store, discovery, destinations)
    yield SimpleNamespace(store=store, engine=engine, discovery=discovery, destinations=destinations,
                          db=db, web=web, stopped=stopped, events=events, folder=folder, client=client)
    engine.close()


def run_backup(f):
    f.store.execute("INSERT INTO jobs (id,stack,status,started) VALUES ('job','sample','queued',?)", (utcnow().isoformat(),))
    f.engine.run("job", "sample")
    return f.store.rows("SELECT * FROM jobs WHERE id='job'")[0]


def test_backup_contains_only_mapped_config_and_compose(fixture):
    f = fixture
    job = run_backup(f)
    assert job["status"] == "success"
    assert f.events.index("stop:web") < f.events.index("stop:db")
    assert f.events.index("start:db") < f.events.index("start:web") < f.events.index("upload")
    assert "start:stopped" not in f.events
    assert f.db.status == f.web.status == "running"
    assert f.stopped.status == "exited"
    assert not f.store.rows("SELECT * FROM restart_journal")
    with tarfile.open(f.engine.archive_dir / job["archive"]) as tar:
        names = tar.getnames()
        assert "compose/stacks/sample/compose.yaml" in names
        assert "compose/stacks/sample/.env" in names
        assert any(n.endswith("settings.json") for n in names)
        assert not any("cache" in n or "media" in n or "layer" in n for n in names)
        manifest = json.load(tar.extractfile("manifest.json"))
        assert manifest["containers"][0]["paths"][0]["source"] == "/config"
    assert len(job["sha256"]) == 64


@pytest.mark.parametrize("failure", ["fail_archive", "fail_stop"])
def test_restart_even_when_backup_or_stop_response_fails(fixture, failure):
    setattr(fixture.web, failure, True)
    job = run_backup(fixture)
    assert job["status"] == "failed"
    assert fixture.web.status == fixture.db.status == "running"
    assert "upload" not in fixture.events
    assert not fixture.store.rows("SELECT * FROM restart_journal")


def test_restart_failure_remains_in_journal_and_recovery_retries(fixture):
    fixture.web.fail_start = True
    job = run_backup(fixture)
    assert job["status"] == "failed"
    assert "Restart needs attention" in job["detail"]
    assert fixture.db.status == "running"
    assert fixture.store.rows("SELECT name FROM restart_journal") == [{"name": "web"}]
    fixture.web.fail_start = False
    assert fixture.engine.recover() == []
    assert fixture.web.status == "running"
    assert not fixture.store.rows("SELECT * FROM restart_journal")


def test_missing_rule_prevents_any_container_stop(fixture):
    fixture.store.execute("DELETE FROM rules WHERE pattern='test/service'")
    assert run_backup(fixture)["status"] == "failed"
    assert fixture.events == []


def test_missing_config_preflight_prevents_stop(fixture):
    def missing(*args, **kwargs):
        raise RuntimeError("not found")
    fixture.client.api.head = missing
    job = run_backup(fixture)
    assert job["status"] == "failed"
    assert "not readable" in job["detail"]
    assert fixture.events == []


def test_failed_upload_keeps_local_archive_after_restart(fixture):
    fixture.destinations.errors = ["SMB offline"]
    job = run_backup(fixture)
    assert job["status"] == "partial"
    assert (fixture.engine.archive_dir / job["archive"]).is_file()
    assert fixture.events[-1] == "upload"
    assert fixture.web.status == "running"


def test_shutdown_cancels_archive_but_restarts(fixture):
    fixture.engine.cancelled.set()
    job = run_backup(fixture)
    assert job["status"] == "failed"
    assert fixture.web.status == fixture.db.status == "running"
    assert not list(fixture.engine.archive_dir.glob("*.partial"))


def test_recovery_cannot_run_concurrently_with_backup(fixture):
    fixture.engine.lock.acquire()
    fixture.web.status = "exited"
    fixture.store.execute("INSERT INTO restart_journal VALUES ('web','web','job')")
    fixture.engine.tick()
    assert fixture.web.status == "exited"
    fixture.engine.lock.release()
    fixture.engine.tick()
    assert fixture.web.status == "running"


def test_compose_references_and_optional_env_file(fixture):
    (fixture.folder / "extra.env").write_text("FIXTURE=1")
    (fixture.folder / "secret.txt").write_text("fixture")
    (fixture.folder / "compose.yaml").write_text("services:\n  web:\n    image: test/service\n    env_file:\n      - extra.env\n      - path: optional.env\n        required: false\nsecrets:\n  token:\n    file: ./secret.txt\n")
    files, _ = fixture.discovery.compose_bundle(fixture.discovery.get("sample"))
    assert set(files) == {"/stacks/sample/compose.yaml", "/stacks/sample/.env", "/stacks/sample/extra.env", "/stacks/sample/secret.txt"}


def test_missing_referenced_env_blocks_backup(fixture):
    (fixture.folder / "compose.yaml").write_text("services:\n  web:\n    image: test/service\n    env_file: missing.env\n")
    job = run_backup(fixture)
    assert job["status"] == "failed"
    assert "missing" in job["detail"]
    assert fixture.events == []


def test_saved_inactive_stack_is_discovered(fixture):
    folder = fixture.folder.parent / "inactive"
    folder.mkdir()
    (folder / "compose.yaml").write_text("services: {}")
    stack = fixture.discovery.get("inactive")
    assert stack["containers"] == []
    assert "Inactive stack" in stack["issues"][0]


@pytest.mark.parametrize("path", ["/", "/etc", "/usr/share", "/proc/self", "/config/../media", "relative"])
def test_disallow_system_root_and_traversal_mappings(path):
    with pytest.raises(BackupError):
        validate_rule("test/image", [path], [])


def test_image_matching_registry_port_tags_and_specificity():
    assert image_repository("registry:5000/team/app:v3") == "registry:5000/team/app"
    assert image_repository("eclipse-mosquitto:2") == "library/eclipse-mosquitto"
    rules = [{"pattern": "*/app"}, {"pattern": "registry:5000/team/app"}]
    assert match_rule("registry:5000/team/app:v3", rules) == rules[1]


def test_cron_timezone_and_validation():
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    assert next_run("0 3 * * *", "Europe/London", start) == "2026-07-01T02:00:00+00:00"
    with pytest.raises(BackupError):
        next_run("* * * * * *", "UTC")
    with pytest.raises(BackupError):
        next_run("0 3 * * *", "Wrong/Zone")
    with pytest.raises(BackupError):
        next_run("61 * * * *", "UTC")


def test_archive_rejects_traversal_and_size_limit():
    for files, budget in [({"../escape": b"x"}, [100]), ({"config/big": b"123456"}, [3])]:
        with tarfile.open(fileobj=io.BytesIO(), mode="w") as output, pytest.raises(BackupError):
            append_archive(output, iter([tar_bytes(files)]), "container/config", [], budget, threading.Event())


def test_secrets_encrypted_and_redacted(tmp_path):
    store = Store(tmp_path)
    store.save_settings({"smb_password": "fixture-secret"})
    assert store.settings()["smb_password"] == "fixture-secret"
    assert store.settings(redacted=True)["smb_password"] == ""
    assert store.settings(redacted=True)["smb_password_set"] is True
    assert "fixture-secret" not in str(store.rows("SELECT value FROM settings"))


def test_smb_validation():
    assert parse_smb(r"\\nas\backups\docker") == ("nas", "backups/docker")
    assert parse_smb("smb://nas/backups/docker") == ("nas", "backups/docker")
    for value in ["nas/path", "//nas", "//nas/share/../escape", "//user:pass@nas/share"]:
        with pytest.raises(BackupError):
            parse_smb(value)


def test_retention_does_not_delete_failed_or_other_stack_backups(fixture):
    for job_id, stack, status in [("old", "sample", "success"), ("failed", "sample", "partial"), ("other", "other", "success")]:
        file = fixture.engine.archive_dir / (job_id + ".tar.gz")
        file.write_bytes(b"fixture")
        fixture.store.execute("INSERT INTO jobs(id,stack,status,started,archive) VALUES (?,?,?,?,?)", (job_id, stack, status, "2020-01-01T00:00:00+00:00", file.name))
    fixture.engine.prune("sample", "new")
    assert not (fixture.engine.archive_dir / "old.tar.gz").exists()
    assert (fixture.engine.archive_dir / "failed.tar.gz").exists()
    assert (fixture.engine.archive_dir / "other.tar.gz").exists()


def test_retention_refuses_changed_remote(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save_settings({"smb_enabled": True, "smb_path": "//newnas/backups", "smb_user": "user"})
    dest = Destinations(store)
    monkeypatch.setattr(dest, "run", lambda *a, **kw: pytest.fail("Must not delete"))
    with pytest.raises(BackupError):
        dest.delete_receipts({"smb": {"identity": "smb:oldnas/backups", "remote": "nas:backups/cloud-backup/old.tar.gz"}})
