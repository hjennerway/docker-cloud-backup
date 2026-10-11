import base64
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
                      "Mounts": [{"Type": "volume", "Destination": "/config", "Name": name + "-config", "Source": "/volumes/" + name},
                                 {"Type": "bind", "Destination": "/media", "Source": "/media"}]}
        self.archives = {
            "/config": {"config/settings.json": b'{"ok": true}', "config/cache/trash": b"not needed"},
            "/media": {"media/movie": b"selected media"},
            "/config/settings.json": {"settings.json": b'{"ok": true}'},
        }

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
        if self.fail_archive:
            raise RuntimeError("archive failed")
        return iter([tar_bytes(self.archives[path])]), {}


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


def test_auto_mapping_checks_paths_on_discovery_and_closes_response(fixture):
    closed = []
    fixture.client.api.head = lambda *a, **kw: SimpleNamespace(close=lambda: closed.append(True))
    fixture.client.api._raise_for_status = lambda response: (_ for _ in ()).throw(RuntimeError("missing"))
    stack = fixture.discovery.get("sample")
    assert len(closed) == 3
    assert all("/config" in issue and "not readable" in issue for issue in stack["issues"])
    assert fixture.events == []


def test_volume_discovery_deduplicates_mounts_and_reports_file_sizes(fixture):
    f = fixture
    config = f.discovery.host_root / "volumes/db"
    config.mkdir(parents=True)
    (config / "data").write_bytes(b"12345")
    (config / "empty").mkdir()
    (f.discovery.host_root / "media").write_bytes(b"123")
    f.web.attrs["Mounts"][0] = dict(f.db.attrs["Mounts"][0])
    volumes = f.discovery.stacks()[0]["volumes"]
    assert len(volumes) == 3
    by_name = {v["name"]: v for v in volumes}
    assert by_name["db-config"]["size_bytes"] == 5
    assert len(by_name["db-config"]["mounts"]) == 2
    assert by_name["/media"]["size_bytes"] == 3
    assert by_name["stopped-config"]["size_bytes"] is None
    (config / "data").unlink()
    assert f.discovery.volume_size("/volumes/db") == 5  # Cached between polls.
    f.discovery._size_cache.clear()
    assert f.discovery.volume_size("/volumes/db") == 0


@pytest.mark.parametrize("mode", ["auto", "manual", "volumes"])
def test_selected_size_includes_compose_and_only_chosen_contents(fixture, mode):
    f = fixture
    for name in ("db", "web", "stopped"):
        folder = f.discovery.host_root / "volumes" / name
        (folder / "cache").mkdir(parents=True)
        (folder / "settings.json").write_bytes(b"12345")
        (folder / "cache/trash").write_bytes(b"1234567890")
    (f.discovery.host_root / "media").write_bytes(b"unselected media")
    if mode == "manual":
        f.store.save_backup_selection("sample", {"mode": mode, "volumes": [], "paths": {"web": ["/config/settings.json"]}})
        expected = 5
    elif mode == "volumes":
        f.web.attrs["Mounts"][0] = dict(f.db.attrs["Mounts"][0])
        volume = next(v for v in f.discovery.get("sample")["volumes"] if v["name"] == "db-config")
        f.store.save_backup_selection("sample", {"mode": mode, "volumes": [volume["id"], volume["id"]], "paths": {}})
        expected = 15  # Shared selected mount counted once; whole volume includes cache.
    else:
        expected = 15  # Three config directories, each excluding cache.
    files, _ = f.discovery.compose_bundle(f.discovery.get("sample"))
    expected += sum(f.discovery.host_path(file).stat().st_size for file in files)
    stack = f.discovery.stacks()[0]
    assert not stack["issues"]
    assert stack["backup_size_bytes"] == expected
    assert f.events == []  # Size estimation never stops containers or copies data.


def test_selection_size_unknown_when_any_selected_path_is_unreadable(fixture):
    assert fixture.discovery.stacks()[0]["backup_size_bytes"] is None


@pytest.mark.parametrize("compose", [None, "services: [invalid]", "not-a-compose-mapping"])
def test_unavailable_compose_input_does_not_break_size_discovery(fixture, compose):
    f = fixture
    if compose is None:
        for container in f.client.items.values():
            container.labels.pop("com.docker.compose.project.config_files")
        (f.folder / "compose.yaml").unlink()
    else:
        (f.folder / "compose.yaml").write_text(compose)
    stack = f.discovery.stacks()[0]
    assert stack["backup_size_bytes"] is None


def test_unmounted_config_metadata_scan_respects_exclusions_and_caches(fixture):
    f = fixture
    calls = []
    def scan(command, **kwargs):
        calls.append(command)
        assert kwargs == {"stderr": False}
        return SimpleNamespace(exit_code=0, output=b"5 /config/settings.json\n10 /config/cache/skip\n3 /config/logs/a b.log\n")
    f.db.exec_run = scan
    container = f.discovery.get("sample")["containers"][0]
    assert f.discovery.container_size(container, "/config", ["cache/**", "logs/**"]) == 5
    assert calls == [["timeout", "-s", "KILL", "2", "find", "/config", "-type", "f", "-exec",
                      "stat", "-c", "%s %n", "--", "{}", "+"]]
    assert f.discovery.container_size(container, "/config", ["cache/**", "logs/**"]) == 5
    assert len(calls) == 1
    container["state"] = "exited"
    assert f.discovery.container_size(container, "/config", ["cache/**", "logs/**"]) is None
    assert len(calls) == 1


@pytest.mark.parametrize("exit_code,output", [(137, b"5 /config/partial\n"), (0, b"bad output"), (0, b"5 /outside\n")])
def test_incomplete_container_metadata_scan_is_unknown(fixture, exit_code, output):
    fixture.db.exec_run = lambda *args, **kwargs: SimpleNamespace(exit_code=exit_code, output=output)
    container = fixture.discovery.get("sample")["containers"][0]
    assert fixture.discovery.container_size(container, "/config") is None


def test_stopped_file_size_uses_docker_head_metadata_without_starting(fixture):
    f = fixture
    metadata = {"size": 12, "mode": 0o640}
    closed = []
    f.client.api.head = lambda *args, **kwargs: SimpleNamespace(
        headers={"X-Docker-Container-Path-Stat": base64.b64encode(json.dumps(metadata).encode()).decode()},
        close=lambda: closed.append(True))
    container = next(c for c in f.discovery.get("sample")["containers"] if c["state"] == "exited")
    info = f.discovery.check_path(container["id"], container["name"], "/internal/settings.yml")
    assert f.discovery.container_size(container, "/internal/settings.yml", metadata=info) == 12
    assert f.discovery.container_size(container, "/internal", metadata={"size": 4096, "mode": 1 << 31}) is None
    assert closed and f.events == []


@pytest.mark.parametrize("image", ["lissy93/dashy:latest", "ghcr.io/lissy93/dashy:4.0.0"])
def test_dashy_without_mounts_is_backed_up_using_config_mapping(fixture, image):
    f = fixture
    f.client.items = {f.db.id: f.db}
    f.db.attrs.update(Config={"Image": image, "Env": []}, Mounts=[])
    f.db.archives = {"/app/user-data": {"user-data/conf.yml": b"pageInfo: {}\n"}}
    f.db.exec_run = lambda *args, **kwargs: SimpleNamespace(exit_code=0, output=b"13 /app/user-data/conf.yml\n")
    (f.folder / "compose.yaml").write_text("services:\n  db:\n    image: " + image + "\n")
    stack = f.discovery.stacks()[0]
    assert stack["volumes"] == []  # Container-local config must not be mislabeled as a volume.
    assert not stack["issues"]
    assert stack["containers"][0]["backup_paths"][0]["path"] == "/app/user-data"
    assert stack["backup_size_bytes"] == 13 + (f.folder / "compose.yaml").stat().st_size + (f.folder / ".env").stat().st_size
    assert f.events == []
    job = run_backup(f)
    assert job["status"] == "success", job["detail"]
    with tarfile.open(f.engine.archive_dir / job["archive"]) as tar:
        name = next(name for name in tar.getnames() if name.endswith("user-data/conf.yml"))
        assert tar.extractfile(name).read() == b"pageInfo: {}\n"


@pytest.mark.parametrize("customized", [False, True])
def test_dashy_seed_migrates_existing_store_once_without_overwriting(tmp_path, customized):
    store = Store(tmp_path)
    store.execute("DELETE FROM settings WHERE key='seeded_dashy_v1'")
    if customized:
        store.execute("UPDATE rules SET paths=? WHERE pattern='*/dashy'", ('["/custom/conf.yml"]',))
    else:
        store.execute("DELETE FROM rules WHERE pattern='*/dashy'")
    store.execute("DELETE FROM rules WHERE pattern='*/sonarr'")
    store = Store(tmp_path)
    rule = next(r for r in store.rules() if r["pattern"] == "*/dashy")
    assert rule["paths"] == (["/custom/conf.yml"] if customized else ["/app/user-data"])
    assert not any(r["pattern"] == "*/sonarr" for r in store.rules())
    store.execute("DELETE FROM rules WHERE pattern='*/dashy'")
    assert not any(r["pattern"] == "*/dashy" for r in Store(tmp_path).rules())


@pytest.mark.parametrize("multiple", [False, True])
def test_selected_volumes_work_without_rules_and_skip_unselected_mounts(fixture, multiple):
    f = fixture
    f.store.execute("DELETE FROM rules")
    f.web.attrs["Mounts"][0] = dict(f.db.attrs["Mounts"][0])
    volumes = f.discovery.get("sample")["volumes"]
    wanted = {"db-config", "/media"} if multiple else {"db-config"}
    ids = [v["id"] for v in volumes if v["name"] in wanted]
    f.store.save_backup_selection("sample", {"mode": "volumes", "volumes": ids, "paths": {}})
    job = run_backup(f)
    assert job["status"] == "success", job["detail"]
    assert f.events.count("archive:db") == len(ids)
    assert "archive:web" not in f.events and "archive:stopped" not in f.events
    assert f.web.status == f.db.status == "running" and f.stopped.status == "exited"
    with tarfile.open(f.engine.archive_dir / job["archive"]) as tar:
        names = tar.getnames()
        assert sum(n.endswith("settings.json") for n in names) == 1
        assert any(n.endswith("cache/trash") for n in names)  # Whole volume, no rule exclusions.
        assert any(n.endswith("media/movie") for n in names) == multiple
        manifest = json.load(tar.extractfile("manifest.json"))
        assert len(manifest["volumes"]) == len(ids)
        shared = next(v for v in manifest["volumes"] if v["name"] == "db-config")
        assert len(shared["mounts"]) == 2
        assert manifest["backup_selection"]["mode"] == "volumes"


def test_nested_mount_is_not_included_with_parent_volume(fixture):
    f = fixture
    f.db.attrs["Mounts"].append({"Type": "bind", "Source": "/nested", "Destination": "/config/media"})
    f.db.archives["/config"]["config/media/movie"] = b"unselected"
    volume = next(v for v in f.discovery.get("sample")["volumes"] if v["name"] == "db-config")
    f.store.save_backup_selection("sample", {"mode": "volumes", "volumes": [volume["id"]], "paths": {}})
    job = run_backup(f)
    assert job["status"] == "success"
    with tarfile.open(f.engine.archive_dir / job["archive"]) as tar:
        assert not any("media" in n for n in tar.getnames())


def test_manual_file_selection_survives_store_reopen_and_ignores_rules(fixture):
    f = fixture
    f.store.execute("DELETE FROM rules")
    selection = {"mode": "manual", "volumes": [], "paths": {"web": ["/config/settings.json"]}}
    f.store.save_backup_selection("sample", selection)
    assert Store(f.store.root).backup_selection("sample") == selection
    job = run_backup(f)
    assert job["status"] == "success", job["detail"]
    with tarfile.open(f.engine.archive_dir / job["archive"]) as tar:
        paths = [n for n in tar.getnames() if n.startswith("containers/")]
        assert len(paths) == 1 and paths[0].endswith("/settings.json")
    assert "archive:db" not in f.events


def test_empty_manual_services_do_not_block_after_removal(fixture):
    selection = {"mode": "manual", "volumes": [], "paths": {"removed": [], "web": ["/config"]}}
    fixture.store.save_backup_selection("sample", selection)
    assert not fixture.discovery.get("sample")["issues"]


def test_volume_selection_survives_container_recreation(fixture):
    f = fixture
    volume = next(v for v in f.discovery.get("sample")["volumes"] if v["name"] == "db-config")
    f.store.save_backup_selection("sample", {"mode": "volumes", "volumes": [volume["id"]], "paths": {}})
    f.db.id = "new-container-id"
    f.client.items = {c.id: c for c in (f.db, f.web, f.stopped)}
    stack = f.discovery.get("sample")
    assert not stack["issues"]
    selected = next(c for c in stack["containers"] if c["id"] == "new-container-id")
    assert selected["backup_paths"][0]["volume_id"] == volume["id"]


@pytest.mark.parametrize("selection", [
    {"mode": "volumes", "volumes": ["removed"], "paths": {}},
    {"mode": "volumes", "volumes": [], "paths": {}},
    {"mode": "manual", "volumes": [], "paths": {"removed": ["/config"]}},
    {"mode": "manual", "volumes": [], "paths": {}},
])
def test_invalid_saved_selections_never_stop_containers(fixture, selection):
    fixture.store.save_backup_selection("sample", selection)
    job = run_backup(fixture)
    assert job["status"] == "failed"
    assert fixture.events == []


def test_paths_are_rechecked_after_discovery_before_stopping(fixture):
    f = fixture
    calls = []
    def head(*args, **kwargs):
        calls.append(kwargs["params"]["path"])
        if len(calls) > 3:
            raise RuntimeError("Path disappeared")
        return SimpleNamespace(close=lambda: None)
    f.client.api.head = head
    job = run_backup(f)
    assert job["status"] == "failed" and "not readable" in job["detail"]
    assert f.events == []


def test_selection_and_mapping_api_validate_before_saving(fixture):
    from fastapi.testclient import TestClient
    from app.main import create_app
    f = fixture
    app = create_app(f.store, f.client, start_worker=False)
    app.state.engine = f.engine
    headers = {"X-Requested-With": "cloud-backup"}
    def head(*args, **kwargs):
        if kwargs["params"]["path"] == "/missing":
            raise RuntimeError("missing")
        return SimpleNamespace(close=lambda: None)
    f.client.api.head = head
    with TestClient(app) as client:
        endpoint = "/api/stacks/sample/selection"
        assert client.put(endpoint, json={"mode": "auto"}, headers=headers).status_code == 401
        client.post("/api/login", json={"password": "test-password-only"}, headers=headers)
        original = next(r for r in f.store.rules() if r["pattern"] == "test/service")
        response = client.put("/api/rules", json={**original, "paths": ["/missing"]}, headers=headers)
        assert response.status_code == 400 and "/missing" in response.json()["detail"]
        assert next(r for r in f.store.rules() if r["pattern"] == "test/service") == original
        response = client.put("/api/rules", json=original, headers=headers)
        assert response.json()["checked_containers"] == 3
        response = client.put("/api/rules", json={"pattern": "future/image", "paths": ["/missing"]}, headers=headers)
        assert response.json()["checked_containers"] == 0
        for selection in [
            {"mode": "manual", "paths": {"web": ["/missing"]}},
            {"mode": "manual", "paths": {"web": ["/etc"]}},
            {"mode": "manual", "paths": {}},
            {"mode": "volumes", "volumes": ["unknown"]},
        ]:
            assert client.put(endpoint, json=selection, headers=headers).status_code == 400
            assert f.store.backup_selection("sample")["mode"] == "auto"
        assert client.put(endpoint, json={"mode": "invalid"}, headers=headers).status_code == 422
        assert client.put(endpoint, json={"mode": "manual", "paths": {"web": ["/config/settings.json"], "db": []}}, headers=headers).status_code == 200
        assert f.store.backup_selection("sample")["paths"] == {"web": ["/config/settings.json"]}
        assert client.get("/api/dashboard").json()["stacks"][0]["backup_selection"]["mode"] == "manual"
        volume = f.discovery.get("sample")["volumes"][0]
        assert client.put(endpoint, json={"mode": "volumes", "volumes": [volume["id"]]}, headers=headers).status_code == 200
        assert f.store.backup_selection("sample")["volumes"] == [volume["id"]]
        assert client.put("/api/stacks/sample/schedule", json={"cron": "0 3 * * *", "timezone": "UTC", "enabled": True}, headers=headers).status_code == 200
        assert client.put(endpoint, json={"mode": "auto"}, headers=headers).status_code == 200
        assert f.store.backup_selection("sample") == {"mode": "auto", "paths": {}, "volumes": []}


def test_mapping_validation_respects_specific_rule_precedence(fixture):
    def no_check(*args, **kwargs):
        pytest.fail("A shadowed rule must not be applied to this container")
    fixture.client.api.head = no_check
    assert fixture.discovery.validate_mapping({"pattern": "test/*", "paths": ["/missing"], "excludes": []}) == 0


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
