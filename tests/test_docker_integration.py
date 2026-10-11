"""Opt-in real Docker test, using disposable containers and a disposable volume."""
import io
import json
import os
import shutil
import tarfile
import uuid
import zipfile

import docker
import pytest

from app.backup import BackupEngine, utcnow
from app.destinations import Destinations
from app.discovery import Discovery
from app.store import Store


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_DOCKER_TESTS") != "1", reason="Set RUN_DOCKER_TESTS=1 after building the app image")
@pytest.mark.parametrize("mode", ["auto", "volumes", "manual"])
def test_real_named_volume_backup_restart_and_restore(tmp_path, mode):
    client = docker.from_env(timeout=120)
    project = "cloud-backup-test-" + uuid.uuid4().hex[:8]
    created, volumes = [], []
    engine = None
    try:
        store = Store(tmp_path / "data")
        store.execute("INSERT INTO rules VALUES (?,?,?)", ("library/docker-cloud-backup", '["/config"]', '["cache/**"]'))
        root = tmp_path / "host"
        folder = root / "stacks" / project
        folder.mkdir(parents=True)
        (folder / "compose.yaml").write_text("services:\n  active:\n    image: docker-cloud-backup:local\n  stopped:\n    image: docker-cloud-backup:local\n")
        (folder / ".env").write_text("TEST_CONFIG=fixture-only\n")
        for name in ("active", "stopped"):
            volume = client.volumes.create(name=project + "-" + name)
            volumes.append(volume)
            container = client.containers.create("docker-cloud-backup:local", ["python", "-c",
                "import signal, sys, time; signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(3600)"],
                name=project + "-" + name,
                labels={"com.docker.compose.project": project, "com.docker.compose.service": name,
                        "com.docker.compose.project.working_dir": "/stacks/" + project,
                        "com.docker.compose.project.config_files": "/stacks/" + project + "/compose.yaml"},
                volumes={volume.name: {"bind": "/config", "mode": "rw"}})
            created.append(container)
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                for filename, content in [("config/settings.json", b'{"fixture":"restorable"}'),
                                          ("config/cache/skip.txt", b"excluded cache"),
                                          ("media/skip.txt", b"excluded media")]:
                    member = tarfile.TarInfo(filename)
                    member.size, member.mode = len(content), 0o640
                    archive.addfile(member, io.BytesIO(content))
            container.put_archive("/", stream.getvalue())
            if name == "active":
                container.start()
        discovery = Discovery(client, store, root, "/stacks")
        stack = discovery.get(project)
        assert not stack["issues"]
        if mode == "volumes":
            assert len(stack["volumes"]) == 2
            store.save_backup_selection(project, {"mode": mode, "volumes": [v["id"] for v in stack["volumes"]], "paths": {}})
        elif mode == "manual":
            store.save_backup_selection(project, {"mode": mode, "volumes": [], "paths": {name: ["/config/settings.json"] for name in ("active", "stopped")}})
        if mode != "auto":
            store.execute("DELETE FROM rules WHERE pattern='library/docker-cloud-backup'")
        engine = BackupEngine(store, discovery, Destinations(store))
        store.execute("INSERT INTO jobs (id,stack,status,started) VALUES ('real',?,'queued',?)", (project, utcnow().isoformat()))
        engine.run("real", project)
        job = store.rows("SELECT * FROM jobs WHERE id='real'")[0]
        assert job["status"] == "success", job["detail"]
        created[0].reload()
        created[1].reload()
        assert created[0].status == "running"
        assert created[1].status == "created"
        with tarfile.open(engine.archive_dir / job["archive"]) as archive:
            names = archive.getnames()
            assert any("cache/skip.txt" in name for name in names) == (mode == "volumes")
            assert not any("media/skip.txt" in name for name in names)
            assert "manifest.json" in names
            paths = [name for name in names if name.endswith("settings.json")]
            assert len(paths) == 2
            assert archive.extractfile(paths[0]).read() == b'{"fixture":"restorable"}'
            assert archive.getmember(paths[0]).mode == 0o640
            # Restore the archived configuration into another clean container,
            # without running any executable content from the archive.
            restored = client.containers.create("docker-cloud-backup:local", ["true"], name=project + "-restore")
            created.append(restored)
            restored_tar = io.BytesIO()
            with tarfile.open(fileobj=restored_tar, mode="w") as target:
                member = archive.getmember(paths[0])
                content = archive.extractfile(member)
                member.name = "settings.json"
                target.addfile(member, content)
            restored.put_archive("/tmp", restored_tar.getvalue())
            chunks, _ = restored.get_archive("/tmp/settings.json")
            with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as check:
                assert check.extractfile("settings.json").read() == b'{"fixture":"restorable"}'
        # Exercise the app restore worker with a running, disposable target.
        restore_volume = client.volumes.create(name=project + "-restore-data")
        volumes.append(restore_volume)
        target = client.containers.create("docker-cloud-backup:local", ["python", "-c",
            "import signal, sys, time; signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(3600)"], entrypoint=[],
            name=project + "-restore-running", volumes={restore_volume.name: {"bind": "/config", "mode": "rw"}})
        created.append(target)
        target.start()
        token = uuid.uuid4().hex
        upload_folder = engine.restore.root / token
        upload_folder.mkdir()
        backup_path = engine.archive_dir / job["archive"]
        if mode == "volumes":
            with zipfile.ZipFile(upload_folder / "upload", "w", zipfile.ZIP_DEFLATED) as wrapper:
                wrapper.write(backup_path, "backup.tar.gz")
        else:
            shutil.copyfile(backup_path, upload_folder / "upload")
        info = engine.restore.inspect(token, upload_folder, "backup.zip" if mode == "volumes" else "backup.tar.gz", "test-session")
        source_index = next(c["index"] for c in info["containers"] if c["name"].endswith("-active"))
        preview = engine.restore.preview(token, "test-session", target.id, source_index)
        assert preview["paths"][0]["source"] == ("/config/settings.json" if mode == "manual" else "/config")
        restore_id = engine.restore.submit(token, "test-session")
        engine.pool.submit(lambda: None).result(timeout=30)
        restored_job = store.rows("SELECT * FROM jobs WHERE id=?", (restore_id,))[0]
        assert restored_job["status"] == "success", restored_job["detail"]
        target.reload()
        assert target.status == "running"
        chunks, _ = target.get_archive("/config/settings.json")
        with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as check:
            assert check.extractfile("settings.json").read() == b'{"fixture":"restorable"}'
            assert check.getmember("settings.json").mode == 0o640
        assert not store.rows("SELECT * FROM restart_journal")
    finally:
        if engine:
            engine.close()
        for container in reversed(created):
            container.remove(force=True)
        for volume in volumes:
            volume.remove()
        client.close()


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_DOCKER_TESTS") != "1", reason="Set RUN_DOCKER_TESTS=1 after building the app image")
@pytest.mark.parametrize("image", ["docker-cloud-backup:local", "eclipse-mosquitto:2"])
def test_real_container_local_config_size_without_copying_or_stopping(tmp_path, image):
    """Exercise both GNU and BusyBox metadata tools on disposable containers."""
    client = docker.from_env(timeout=30)
    container = None
    try:
        client.images.get(image)  # Do not pull unrelated test images automatically.
    except docker.errors.ImageNotFound:
        client.close()
        pytest.skip("Size-scan fixture image is not available locally")
    try:
        container = client.containers.create(image, ["sleep", "3600"],
            entrypoint=[], name="cloud-backup-size-test-" + uuid.uuid4().hex[:8])
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name, content in [("size-test/conf.yml", b"12345"), ("size-test/cache/skip", b"1234567890")]:
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        container.put_archive("/tmp", stream.getvalue())
        container.start()
        discovery = Discovery(client, Store(tmp_path / "data"), tmp_path / "host")
        info = {"id": container.id, "name": container.name, "state": "running", "mounts": []}
        assert discovery.container_size(info, "/tmp/size-test", ["cache/**"]) == 5
        assert discovery.container_size(info, "/tmp/size-test") == 15
        container.reload()
        assert container.status == "running"
        container.stop(timeout=1)
        info["state"] = "exited"
        metadata = discovery.check_path(container.id, container.name, "/tmp/size-test/conf.yml")
        assert discovery.container_size(info, "/tmp/size-test/conf.yml", metadata=metadata) == 5
        assert discovery.container_size(info, "/tmp/size-test", ["cache/**"]) is None
        container.reload()
        assert container.status == "exited"
    finally:
        if container is not None:
            container.remove(force=True)
        client.close()
