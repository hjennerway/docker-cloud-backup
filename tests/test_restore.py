import io
import json
import tarfile
import time
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.discovery import Discovery
from app.main import create_app
from app.store import Store


HEADERS = {"X-Requested-With": "cloud-backup"}
PREFIX = "containers/original-123/0"


def backup_bytes(fmt="zip", files=None, paths=None, mutate=None):
    files = files if files is not None else {
        PREFIX + "/config/": None,
        PREFIX + "/config/settings.json": b'{"restored":true}',
        PREFIX + "/config/nested/other.txt": b"also restored",
    }
    manifest = {"format_version": 1, "created": "2026-10-11T12:00:00+00:00", "containers": [
        {"name": "original", "service": "app", "image": "example/app:old", "paths": paths or [
            {"source": "/config", "archive_prefix": PREFIX}]}]}
    if mutate:
        mutate(manifest)
    data = {"manifest.json": json.dumps(manifest).encode(), **files}
    stream = io.BytesIO()
    if fmt == "zip":
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in data.items():
                info = zipfile.ZipInfo(name)
                info.filename = name  # Keep hostile backslashes even on Windows.
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content or b"")
    else:
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, content in data.items():
                member = tarfile.TarInfo(name)
                member.uid, member.gid, member.mode = 1000, 1001, 0o640
                if content is None:
                    member.type = tarfile.DIRTYPE
                    archive.addfile(member)
                else:
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
    if fmt == "wrapped":
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("backup.tar.gz", stream.getvalue())
        return outer.getvalue()
    return stream.getvalue()


class Container:
    def __init__(self, name, events, state="running"):
        self.id, self.name, self.status, self.events = name + "-id", name, state, events
        self.labels = {"com.docker.compose.service": "app"}
        self.attrs = {"Config": {"Image": "example/app:latest", "Env": []},
                      "Mounts": [{"Type": "volume", "Destination": "/config", "RW": True,
                                  "Source": "/volumes/" + name, "Name": name + "-data"}]}
        self.fail_stop = self.fail_put = self.fail_start = False
        self.restored = {}

    def reload(self):
        pass

    def stop(self, timeout):
        self.events.append("stop:" + self.name)
        self.status = "exited"
        if self.fail_stop:
            raise RuntimeError("lost stop response")

    def start(self):
        self.events.append("start:" + self.name)
        if self.fail_start:
            raise RuntimeError("failed restart")
        self.status = "running"

    def put_archive(self, destination, stream):
        assert self.status != "running"
        self.events.append("write:" + self.name)
        if self.fail_put:
            raise RuntimeError("failed write")
        with tarfile.open(fileobj=stream) as archive:
            for member in archive:
                if member.isfile():
                    self.restored[destination + "/" + member.name] = (
                        archive.extractfile(member).read(), member.uid, member.gid, member.mode)
        return True


@pytest.fixture
def restore_app(tmp_path, monkeypatch):
    events = []
    target = Container("target", events)
    stopped = Container("stopped", events, "exited")
    excluded = Container("backup", events)
    excluded.labels["cloud-backup.exclude"] = "true"
    items = {c.id: c for c in (target, stopped, excluded)}
    docker = SimpleNamespace(containers=SimpleNamespace(list=lambda all: list(items.values()), get=lambda cid: items[cid]), close=lambda: None)
    monkeypatch.setattr(Discovery, "check_path", lambda *a, **kw: {"mode": (1 << 31) | 0o755, "uid": 1000, "gid": 1001})
    monkeypatch.setattr("app.restore.time.sleep", lambda n: None)
    app = create_app(Store(tmp_path), docker, start_worker=False)
    with TestClient(app) as client:
        client.post("/api/login", headers=HEADERS, json={"password": "test-password-only"})
        yield SimpleNamespace(app=app, client=client, target=target, stopped=stopped, events=events, items=items)


def upload(f, fmt="zip", **kwargs):
    response = f.client.post("/api/restore/upload?filename=backup." + ("tar.gz" if fmt == "tar" else "zip"),
                             content=backup_bytes(fmt, **kwargs), headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def preview(f, token, target=None, source_index=0):
    return f.client.post("/api/restore/preview", headers=HEADERS,
                         json={"token": token, "container_id": (target or f.target).id, "source_index": source_index})


def restore(f, token):
    response = f.client.post("/api/restore", headers=HEADERS, json={"token": token, "confirmed": True})
    assert response.status_code == 200, response.text
    engine = f.app.state.engine
    engine.pool.submit(lambda: None).result(timeout=5)
    return engine.store.rows("SELECT * FROM jobs WHERE id=?", (response.json()["job_id"],))[0]


@pytest.mark.parametrize("fmt", ["zip", "tar", "wrapped"])
def test_restore_overwrites_correct_container_paths_and_restarts(restore_app, fmt):
    f = restore_app
    token = upload(f, fmt)
    response = preview(f, token)
    assert response.status_code == 200, response.text
    assert response.json()["paths"][0]["source"] == "/config"
    assert f.events == []  # Upload and review never stop or write containers.
    job = restore(f, token)
    assert job["status"] == "success", job["detail"]
    assert f.target.restored["/config/settings.json"][:3] == (b'{"restored":true}', 1000, 1001)
    assert f.target.restored["/config/nested/other.txt"][0] == b"also restored"
    assert "/config/config/settings.json" not in f.target.restored
    assert f.events == ["stop:target", "write:target", "start:target"]
    assert not f.app.state.engine.store.rows("SELECT * FROM restart_journal")
    assert not f.app.state.engine.restore.root.joinpath(token).exists()
    assert f.client.post("/api/restore", headers=HEADERS, json={"token": token, "confirmed": True}).status_code == 400


def test_single_file_backup_uses_existing_parent_and_keeps_stopped_container_stopped(restore_app):
    f = restore_app
    token = upload(f, "tar", files={PREFIX + "/settings.json": b"old config"},
                   paths=[{"source": "/config/settings.json", "archive_prefix": PREFIX}])
    assert preview(f, token, f.stopped).status_code == 200
    assert restore(f, token)["status"] == "success"
    assert f.stopped.restored["/config/settings.json"] == (b"old config", 1000, 1001, 0o640)
    assert f.stopped.status == "exited"
    assert f.events == ["write:stopped"]


@pytest.mark.parametrize("failure", ["fail_stop", "fail_put", "fail_start"])
def test_restore_failure_attempts_restart_and_retains_recovery_journal_when_needed(restore_app, failure):
    f = restore_app
    token = upload(f)
    assert preview(f, token).status_code == 200
    setattr(f.target, failure, True)
    job = restore(f, token)
    assert job["status"] == "failed"
    assert "start:target" in f.events
    journal = f.app.state.engine.store.rows("SELECT * FROM restart_journal")
    assert bool(journal) == (failure == "fail_start")
    assert f.target.status == ("exited" if failure == "fail_start" else "running")
    assert not f.app.state.engine.lock.locked()
    if failure == "fail_start":
        f.target.fail_start = False
        assert f.app.state.engine.recover() == []
        assert f.target.status == "running"


def test_restore_requires_review_confirmation_and_session_owner(restore_app):
    f = restore_app
    token = upload(f)
    body = {"token": token, "confirmed": True}
    assert f.client.post("/api/restore", json=body, headers=HEADERS).status_code == 400
    assert preview(f, token).status_code == 200
    assert f.client.post("/api/restore", json={**body, "confirmed": False}, headers=HEADERS).status_code == 400
    assert f.client.post("/api/restore", json=body).status_code == 403
    f.client.post("/api/logout", headers=HEADERS)
    assert preview(f, token).status_code == 401
    f.client.post("/api/login", headers=HEADERS, json={"password": "test-password-only"})
    assert preview(f, token).status_code == 400
    assert f.events == []


def test_restore_listing_includes_standalone_containers_and_excludes_backup_app(restore_app):
    f = restore_app
    response = f.client.get("/api/restore/containers")
    assert response.status_code == 200
    assert {c["name"] for c in response.json()["containers"]} == {"target", "stopped"}
    token = upload(f)
    assert preview(f, token, f.items["backup-id"]).status_code == 400


@pytest.mark.parametrize("name", ["../escape", "/escape", "containers/../escape", "containers\\escape"])
def test_unsafe_archive_paths_are_rejected_before_any_stop(restore_app, name):
    f = restore_app
    response = f.client.post("/api/restore/upload?filename=bad.zip", headers=HEADERS,
                             content=backup_bytes(files={name: b"bad"}))
    assert response.status_code == 400
    assert not f.events
    assert not list(f.app.state.engine.restore.root.iterdir())


def test_corrupt_and_missing_manifest_backups_are_rejected(restore_app):
    f = restore_app
    for content in (b"not a zip", b"PK\x03\x04broken"):
        assert f.client.post("/api/restore/upload?filename=bad.zip", content=content, headers=HEADERS).status_code == 400
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("settings.json", "{}")
    assert f.client.post("/api/restore/upload?filename=plain.zip", content=stream.getvalue(), headers=HEADERS).status_code == 400


def test_upload_and_unpacked_size_limits_are_enforced(restore_app, monkeypatch):
    f = restore_app
    monkeypatch.setenv("MAX_BACKUP_GB", str(4096 / 1024**3))
    assert f.client.post("/api/restore/upload?filename=large.zip", content=b"x" * 4097, headers=HEADERS).status_code == 413
    # Highly compressible content must count towards the unpacked size limit.
    response = f.client.post("/api/restore/upload?filename=bomb.zip", headers=HEADERS,
                             content=backup_bytes(files={PREFIX + "/config/big": b"x" * 10000}))
    assert response.status_code == 400
    assert "MAX_BACKUP_GB" in response.json()["detail"]
    # Existing small JSON endpoints keep their original limit.
    assert f.client.post("/api/login", content=b"x" * 100001, headers=HEADERS).status_code == 413


def test_streamed_upload_limit_is_enforced_without_content_length(restore_app, monkeypatch):
    f = restore_app
    monkeypatch.setenv("MAX_BACKUP_GB", str(4096 / 1024**3))
    response = f.client.post("/api/restore/upload?filename=large.zip", headers=HEADERS,
                             content=iter([b"x" * 3000, b"y" * 3000]))
    assert response.status_code == 413
    assert not f.app.state.engine.lock.locked()
    assert not list(f.app.state.engine.restore.root.iterdir())


def test_busy_worker_and_restart_recovery_block_restore(restore_app):
    f = restore_app
    token = upload(f)
    engine = f.app.state.engine
    engine.lock.acquire()
    try:
        assert preview(f, token).status_code == 400
        assert f.client.post("/api/restore/upload?filename=another.zip", headers=HEADERS, content=backup_bytes()).status_code == 400
    finally:
        engine.lock.release()
    engine.store.execute("INSERT INTO restart_journal VALUES (?,?,?)", (f.target.id, f.target.name, "previous"))
    assert preview(f, token).status_code == 400
    assert not f.events


def test_image_mismatch_read_only_and_shared_running_mounts_fail_preflight(restore_app):
    f = restore_app
    token = upload(f)
    f.target.attrs["Config"]["Image"] = "other/application:latest"
    assert preview(f, token).status_code == 400
    f.target.attrs["Config"]["Image"] = "example/app:latest"
    f.target.attrs["Mounts"][0]["RW"] = False
    assert preview(f, token).status_code == 400
    f.target.attrs["Mounts"][0]["RW"] = True
    f.stopped.attrs["Mounts"] = f.target.attrs["Mounts"]
    f.stopped.status = "running"
    assert "shares storage" in preview(f, token).json()["detail"]
    f.stopped.status = "exited"
    assert preview(f, token).status_code == 200
    # Repeat validation immediately before queueing to catch changed host state.
    f.stopped.status = "running"
    assert f.client.post("/api/restore", headers=HEADERS, json={"token": token, "confirmed": True}).status_code == 400
    assert not f.events


@pytest.mark.parametrize("escape", [True, False])
def test_symlink_escape_and_symlink_parent_are_rejected(restore_app, escape):
    f = restore_app
    content = backup_bytes("tar")
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(content)) as original, tarfile.open(fileobj=output, mode="w:gz") as archive:
        for member in original:
            archive.addfile(member, original.extractfile(member) if member.isfile() else None)
        link = tarfile.TarInfo(PREFIX + "/config/link")
        link.type, link.linkname = tarfile.SYMTYPE, "../../escape" if escape else "nested"
        archive.addfile(link)
        if not escape:
            child = tarfile.TarInfo(PREFIX + "/config/link/overwrite")
            child.size = 3
            archive.addfile(child, io.BytesIO(b"bad"))
    response = f.client.post("/api/restore/upload?filename=unsafe.tar.gz", headers=HEADERS, content=output.getvalue())
    assert response.status_code == 200
    assert preview(f, response.json()["token"]).status_code == 400
    assert not f.events


def test_expired_upload_cannot_restore(restore_app):
    f = restore_app
    token = upload(f)
    f.app.state.engine.restore.uploads[token]["expires"] = time.time() - 1
    assert preview(f, token).status_code == 400
    assert not f.events


def test_shared_volume_can_be_restored_using_another_recorded_service(restore_app):
    f = restore_app
    def shared_manifest(manifest):
        manifest["containers"][0]["paths"][0]["volume_id"] = "shared"
        manifest["containers"].append({"name": "secondary", "service": "second", "image": "example/app:old", "paths": []})
        manifest["volumes"] = [{"id": "shared", "mounts": [{"service": "second", "destination": "/config"}]}]
    token = upload(f, mutate=shared_manifest)
    assert preview(f, token, source_index=1).status_code == 200
    assert restore(f, token)["status"] == "success"
    assert f.target.restored["/config/settings.json"][0] == b'{"restored":true}'
