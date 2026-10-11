"""Opt-in real Docker test, using disposable containers and a disposable volume."""
import io
import json
import os
import tarfile
import uuid

import docker
import pytest

from app.backup import BackupEngine, utcnow
from app.destinations import Destinations
from app.discovery import Discovery
from app.store import Store


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_DOCKER_TESTS") != "1", reason="Set RUN_DOCKER_TESTS=1 after building the app image")
def test_real_named_volume_backup_restart_and_restore(tmp_path):
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
            container = client.containers.create("docker-cloud-backup:local", ["python", "-c", "import time; time.sleep(3600)"],
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
            assert not any("skip.txt" in name for name in names)
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
    finally:
        if engine:
            engine.close()
        for container in reversed(created):
            container.remove(force=True)
        for volume in volumes:
            volume.remove()
        client.close()
