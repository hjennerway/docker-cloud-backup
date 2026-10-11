"""Validate uploaded backups and restore selected data through Docker's archive API."""
import copy
import json
import os
import posixpath
import shutil
import stat
import tarfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import PurePosixPath

from .discovery import BackupError, image_repository, validate_rule


def restore_limit():
    return int(float(os.getenv("MAX_BACKUP_GB", "10")) * 1024**3)


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
            or "\x00" in name or ":" in name or str(path) != name.rstrip("/")):
        raise BackupError("The backup contains an unsafe archive path.")
    return str(path)


class BackupArchive:
    """Read ZIP or tar without extracting uploaded paths onto the host."""
    def __init__(self, path):
        self.zip = zipfile.is_zipfile(path)
        self.archive = zipfile.ZipFile(path) if self.zip else tarfile.open(path, "r:gz")
        self.members = {}
        total = 0
        try:
            entries = self.archive.infolist() if self.zip else self.archive
            for item in entries:
                if self.zip:
                    safe_name(item.orig_filename)
                name = safe_name(item.filename if self.zip else item.name)
                if name in self.members or len(self.members) >= 100000:
                    raise BackupError("The backup contains duplicate paths or too many files.")
                if self.zip:
                    if item.flag_bits & 1:
                        raise BackupError("Password-protected ZIP files are not supported.")
                    mode = item.external_attr >> 16 if item.create_system == 3 else 0
                    kind = stat.S_IFMT(mode)
                    if kind not in {0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}:
                        raise BackupError("The backup contains an unsupported file type.")
                    member = tarfile.TarInfo(name)
                    member.size = item.file_size
                    directory = item.is_dir() or kind == stat.S_IFDIR
                    member.mode = (mode & 0o777) or (0o755 if directory else 0o644)
                    member.type = tarfile.DIRTYPE if directory else tarfile.SYMTYPE if kind == stat.S_IFLNK else tarfile.REGTYPE
                    if directory:
                        member.size = 0
                    if member.issym():
                        if member.size > 4096:
                            raise BackupError("The backup contains an invalid symbolic link.")
                        member.linkname = self.archive.read(item).decode("utf-8")
                        member.size = 0
                    total += item.file_size
                else:
                    member = item
                    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                        raise BackupError("The backup contains an unsupported file type.")
                    total += member.size
                if member.size < 0:
                    raise BackupError("The backup contains an invalid file size.")
                if total > restore_limit():
                    raise BackupError("The unpacked backup exceeds MAX_BACKUP_GB.")
                self.members[name] = (member, item)
        except BaseException:
            self.archive.close()
            raise

    def close(self):
        self.archive.close()

    @contextmanager
    def read(self, name):
        member, item = self.members[name]
        if not member.isfile():
            raise BackupError("Expected a regular file in the backup.")
        stream = self.archive.open(item) if self.zip else self.archive.extractfile(item)
        try:
            yield stream
        finally:
            stream.close()

    def manifest(self):
        if "manifest.json" not in self.members or self.members["manifest.json"][0].size > 1024 * 1024:
            raise BackupError("Upload a Cloud Backup archive containing manifest.json, or a ZIP containing one .tar.gz backup.")
        with self.read("manifest.json") as stream:
            manifest = json.load(stream)
        if (not isinstance(manifest, dict) or manifest.get("format_version") != 1
                or not isinstance(manifest.get("containers"), list) or not 1 <= len(manifest["containers"]) <= 1000):
            raise BackupError("The backup manifest is invalid or uses an unsupported format.")
        for entry in manifest["containers"]:
            if (not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) and entry[k] for k in ("name", "image", "service"))
                    or not isinstance(entry.get("paths"), list) or len(entry["paths"]) > 100):
                raise BackupError("The backup contains an invalid container manifest.")
            for path in entry["paths"]:
                if not isinstance(path, dict) or not isinstance(path.get("source"), str) or not isinstance(path.get("archive_prefix"), str):
                    raise BackupError("The backup contains an invalid path mapping.")
                validate_rule("restore", [path["source"]], [])
                prefix = safe_name(path["archive_prefix"])
                if not prefix.startswith("containers/") or len(PurePosixPath(prefix).parts) != 3:
                    raise BackupError("The backup contains an invalid path mapping.")
        volumes = manifest.get("volumes", [])
        if not isinstance(volumes, list) or len(volumes) > 1000:
            raise BackupError("The backup contains invalid volume mappings.")
        for volume in volumes:
            if (not isinstance(volume, dict) or not isinstance(volume.get("id"), str)
                    or not isinstance(volume.get("mounts"), list) or len(volume["mounts"]) > 1000):
                raise BackupError("The backup contains invalid volume mappings.")
            for mount in volume["mounts"]:
                if not isinstance(mount, dict) or not isinstance(mount.get("service"), str) or not isinstance(mount.get("destination"), str):
                    raise BackupError("The backup contains invalid volume mappings.")
                validate_rule("restore", [mount["destination"]], [])
        return manifest


class RestoreManager:
    def __init__(self, engine):
        self.engine = engine
        self.root = engine.store.root / "restores"
        self.root.mkdir(exist_ok=True)
        self.uploads = {}

    def acquire(self):
        if self.engine.cancelled.is_set() or not self.engine.lock.acquire(blocking=False):
            raise BackupError("Another backup, restore or recovery is in progress. Try again shortly.")
        try:
            if self.engine.store.rows("SELECT 1 FROM restart_journal"):
                raise BackupError("Container restart recovery needs attention before restoring.")
        except Exception:
            self.engine.lock.release()
            raise

    def cleanup(self):
        for folder in self.root.iterdir():
            if folder.is_dir() and time.time() - folder.stat().st_mtime > 3600:
                self.uploads.pop(folder.name, None)
                shutil.rmtree(folder)

    def containers(self):
        try:
            containers = self.engine.discovery.client.containers.list(all=True)
            excluded_projects = {c.labels.get("com.docker.compose.project") for c in containers
                                 if c.labels.get("cloud-backup.exclude", "").lower() == "true"}
            return sorted([{"id": c.id, "name": c.name, "image": c.attrs["Config"]["Image"],
                            "state": c.status, "service": c.labels.get("com.docker.compose.service", c.name)}
                           for c in containers if c.status in {"running", "exited", "created"}
                           and c.labels.get("cloud-backup.exclude", "").lower() != "true"
                           and not (c.labels.get("com.docker.compose.project") in excluded_projects
                                    and c.labels.get("com.docker.compose.project"))
                           and not (os.getenv("HOSTNAME") and c.id.startswith(os.getenv("HOSTNAME")))],
                          key=lambda item: item["name"])
        except Exception:
            raise BackupError("Cannot list local containers. Check the Docker connection.") from None

    def target(self, container_id):
        if not any(c["id"] == container_id for c in self.containers()):
            raise BackupError("Select an available local container. The backup app and excluded stacks cannot be restored.")
        try:
            container = self.engine.discovery.client.containers.get(container_id)
            container.reload()
            if container.status not in {"running", "exited", "created"}:
                raise BackupError("Wait for the selected container to reach a stable state before restoring.")
            return container
        except BackupError:
            raise
        except Exception:
            raise BackupError("The selected container is no longer available.") from None

    def inspect(self, token, folder, filename, owner):
        path = folder / "upload"
        try:
            # ZIP wrappers let users upload existing backups without converting
            # their tar metadata (including ownership and file modes).
            if zipfile.is_zipfile(path):
                nested = None
                with zipfile.ZipFile(path) as archive:
                    if len(archive.infolist()) > 100000:
                        raise BackupError("The ZIP backup contains too many entries.")
                    for item in archive.infolist():
                        safe_name(item.orig_filename)
                        safe_name(item.filename)
                    if "manifest.json" not in archive.namelist():
                        files = [item for item in archive.infolist() if not item.is_dir()]
                        if len(files) != 1 or not files[0].filename.endswith((".tar.gz", ".tgz")):
                            raise BackupError("A ZIP must contain manifest.json and backup data, or one .tar.gz backup.")
                        safe_name(files[0].filename)
                        if files[0].file_size > restore_limit() or files[0].flag_bits & 1:
                            raise BackupError("The ZIP backup is too large or password protected.")
                        nested = folder / "backup.tar.gz"
                        with archive.open(files[0]) as source, nested.open("wb") as output:
                            size = 0
                            while chunk := source.read(1024 * 1024):
                                size += len(chunk)
                                if size > restore_limit():
                                    raise BackupError("The unpacked ZIP backup exceeds MAX_BACKUP_GB.")
                                if shutil.disk_usage(folder).free < len(chunk) + 256 * 1024 * 1024:
                                    raise BackupError("Not enough free space to unpack this ZIP backup.")
                                output.write(chunk)
                        nested.chmod(0o600)
                if nested:
                    path.unlink()
                    path = nested
            archive = BackupArchive(path)
            try:
                manifest = archive.manifest()
            finally:
                archive.close()
            self.uploads[token] = {"folder": folder, "path": path, "manifest": manifest,
                                   "owner": owner, "expires": time.time() + 3600, "preview": None}
            return {"token": token, "filename": filename, "created": manifest.get("created"),
                    "containers": [{"index": i, "name": c["name"], "service": c["service"], "image": c["image"]}
                                   for i, c in enumerate(manifest["containers"])]}
        except BackupError:
            raise
        except Exception:
            raise BackupError("The uploaded backup is invalid, damaged or unsupported.") from None

    def get_upload(self, token, owner):
        upload = self.uploads.get(token)
        if not upload or upload["owner"] != owner or upload["expires"] < time.time():
            raise BackupError("The upload has expired. Upload the backup again.")
        return upload

    def check_destinations(self, container, paths):
        discovery = self.engine.discovery
        for path in paths:
            source = path["source"]
            # A directory is copied into itself; a single file into its parent.
            destination = source if path["directory"] else str(PurePosixPath(source).parent)
            metadata = discovery.check_path(container.id, container.name, destination)
            mode = metadata.get("mode", 0)
            if mode and not (mode & (1 << 31) or stat.S_ISDIR(mode)):
                raise BackupError(f"Restore destination {destination} must be an existing directory.")
            if metadata.get("linkTarget"):
                raise BackupError(f"Restore destination {destination} is a symbolic link. Use a direct config path.")
            for mount in container.attrs.get("Mounts", []):
                mount_path = mount["Destination"].rstrip("/") or "/"
                overlaps = (source == mount_path or source.startswith(mount_path.rstrip("/") + "/")
                            or mount_path.startswith(source + "/"))
                if not overlaps:
                    continue
                if mount.get("RW") is False:
                    raise BackupError(f"Restore path {source} includes a read-only mount.")
                for other in discovery.client.containers.list(all=True):
                    if other.id == container.id or other.status != "running":
                        continue
                    def shared(m):
                        left, right = m.get("Source", "").rstrip("/"), mount.get("Source", "").rstrip("/")
                        return bool(left and right and (left == right or
                            (m.get("Type") == mount.get("Type") == "bind" and
                             (left.startswith(right + "/") or right.startswith(left + "/")))))
                    if any(shared(m) for m in other.attrs.get("Mounts", [])):
                        raise BackupError(f"Stop {other.name} before restoring: it shares storage with {container.name}.")
            covering_mount = any(m.get("RW", True) and (source == m["Destination"] or source.startswith(m["Destination"].rstrip("/") + "/"))
                                 for m in container.attrs.get("Mounts", []))
            if container.attrs.get("HostConfig", {}).get("ReadonlyRootfs") and not covering_mount:
                raise BackupError(f"Restore path {source} is on a read-only container filesystem.")

    def preview(self, token, owner, container_id, source_index):
        upload = self.get_upload(token, owner)
        upload["preview"] = None
        container = self.target(container_id)
        entries = upload["manifest"]["containers"]
        if source_index < 0 or source_index >= len(entries):
            raise BackupError("Select a container from the uploaded backup.")
        entry = entries[source_index]
        if image_repository(entry["image"]) != image_repository(container.attrs["Config"]["Image"]):
            raise BackupError("The backup image does not match the selected container's image repository.")
        paths = list(entry["paths"])
        # Shared volumes are backed up once. Allow each recorded mount's service
        # to restore that data even when another service owns the archive entry.
        for volume in upload["manifest"].get("volumes", []):
            mounts = [m for m in volume.get("mounts", []) if m.get("service") == entry["service"]]
            if mounts:
                for other in entries:
                    for path in other["paths"]:
                        if path.get("volume_id") == volume.get("id") and not any(p["archive_prefix"] == path["archive_prefix"] for p in paths):
                            paths.append({**path, "source": mounts[0]["destination"]})
        if not paths:
            raise BackupError("This container has no configuration or volume data in the backup.")
        archive = BackupArchive(upload["path"])
        staged = []
        try:
            for old_path in upload["folder"].glob("path-*.tar"):
                old_path.unlink()
            for index, path in enumerate(paths):
                validate_rule("restore", [path["source"]], [])
                prefix = safe_name(path["archive_prefix"]) + "/"
                members = [(name, item[0]) for name, item in archive.members.items() if name.startswith(prefix)]
                roots = {name[len(prefix):].split("/")[0] for name, member in members}
                if len(roots) != 1:
                    raise BackupError("A backup path is missing or has multiple archive roots.")
                root = roots.pop()
                root_name = prefix + root
                root_member = archive.members.get(root_name, (None,))[0]
                directory = root_member.isdir() if root_member else any("/" in name[len(prefix):] for name, member in members)
                if root_member and not (root_member.isdir() or root_member.isfile()):
                    raise BackupError("A restore root must be a regular file or directory.")
                if not directory and len(members) != 1:
                    raise BackupError("A file backup contains unexpected nested paths.")
                mapped = {}
                for name, member in members:
                    relative = name[len(root_name):].lstrip("/") if directory else PurePosixPath(path["source"]).name
                    if not relative:
                        continue  # Keep the existing mount directory and its ownership.
                    safe_name(relative)
                    mapped[relative] = (name, member)
                for relative, (name, member) in mapped.items():
                    for parent in PurePosixPath(relative).parents:
                        if str(parent) in mapped and not mapped[str(parent)][1].isdir():
                            raise BackupError("The backup contains a file or link used as a parent directory.")
                    if member.issym():
                        link = member.linkname
                        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), link))
                        if not link or link.startswith("/") or "\\" in link or resolved == ".." or resolved.startswith("../"):
                            raise BackupError("The backup contains a symbolic link outside its restored directory.")
                    if member.islnk():
                        link = safe_name(member.linkname)
                        relative_link = link[len(root_name) + 1:] if link.startswith(root_name + "/") else ""
                        if relative_link not in mapped or not mapped[relative_link][1].isfile():
                            raise BackupError("The backup contains an unsafe hard link.")
                output_path = upload["folder"] / f"path-{index}.tar"
                data_size = 0
                destination = path["source"] if directory else str(PurePosixPath(path["source"]).parent)
                owner = self.engine.discovery.check_path(container.id, container.name, destination) if archive.zip else {}
                if shutil.disk_usage(self.root).free < sum(member.size for _, member in members) + 256 * 1024 * 1024:
                    raise BackupError("Not enough free space to prepare the restore.")
                with tarfile.open(output_path, "w") as output:
                    # Regular files precede hard links, so their targets exist.
                    for relative, (name, original) in sorted(mapped.items(), key=lambda item: item[1][1].islnk()):
                        member = copy.copy(original)
                        member.name, member.mode = relative, original.mode & 0o777
                        member.pax_headers = {}
                        if archive.zip:
                            # ZIP has no UID/GID fields. Keep the destination's
                            # owner rather than making app-owned files root-owned.
                            member.uid, member.gid = owner.get("uid", 0), owner.get("gid", 0)
                        if member.islnk():
                            member.linkname = original.linkname[len(root_name) + 1:]
                        if member.isfile():
                            with archive.read(name) as content:
                                output.addfile(member, content)
                            data_size += member.size
                        else:
                            output.addfile(member)
                output_path.chmod(0o600)
                staged.append({"source": path["source"], "directory": directory, "tar": output_path,
                               "files": len(mapped), "size": data_size})
            self.check_destinations(container, staged)
            upload["preview"] = {"container_id": container.id, "source_index": source_index, "paths": staged}
            return {"container": container.name, "backup_container": entry["name"], "state": container.status,
                    "paths": [{k: p[k] for k in ("source", "files", "size")} for p in staged]}
        except BackupError:
            raise
        except Exception:
            raise BackupError("Could not prepare the restore. Check the archive, Docker and free space.") from None
        finally:
            archive.close()

    def submit(self, token, owner):
        self.acquire()
        job_id = uuid.uuid4().hex
        try:
            upload = self.get_upload(token, owner)
            preview = upload["preview"]
            if not preview:
                raise BackupError("Review the uploaded backup before restoring.")
            container = self.target(preview["container_id"])
            self.check_destinations(container, preview["paths"])
            self.engine.store.execute("INSERT INTO jobs (id,stack,status,started,detail) VALUES (?,?,?,?,?)",
                                      (job_id, "Restore: " + container.name, "queued", datetime.now(timezone.utc).isoformat(), "Waiting to restore uploaded backup"))
            self.engine.pool.submit(self.run, job_id, container.id, upload)
            self.uploads.pop(token)
            return job_id
        except Exception:
            try:
                self.engine.store.job_update(job_id, status="failed", finished=datetime.now(timezone.utc).isoformat(), detail="Could not queue the restore. Upload and review the backup again.")
            finally:
                self.engine.lock.release()
            raise

    def run(self, job_id, container_id, upload):
        store = self.engine.store
        status, detail = "failed", "Restore failed. Check container state and Activity before retrying."
        container = None
        try:
            if self.engine.cancelled.is_set():
                raise BackupError("App shutdown cancelled the restore before any files were changed.")
            container = self.target(container_id)
            self.check_destinations(container, upload["preview"]["paths"])
            store.job_update(job_id, status="running", detail="Stopping " + container.name + " for restore")
            if container.status == "running":
                store.execute("INSERT OR REPLACE INTO restart_journal VALUES (?,?,?)", (container.id, container.name, job_id))
                container.stop(timeout=30)
                container.reload()
                if container.status == "running":
                    raise BackupError("The container did not stop. Restore was cancelled.")
            for path in upload["preview"]["paths"]:
                if self.engine.cancelled.is_set():
                    raise BackupError("Restore interrupted by app shutdown. Some files may have been restored.")
                store.job_update(job_id, detail="Restoring " + container.name + ": " + path["source"])
                destination = path["source"] if path["directory"] else str(PurePosixPath(path["source"]).parent)
                with path["tar"].open("rb") as data:
                    if not container.put_archive(destination, data):
                        raise BackupError("Docker rejected the restored files. Some files may have been restored.")
            status, detail = "success", "Restore complete. Backed-up files were overwritten; other files were kept."
        except BackupError as exc:
            detail = str(exc)
        except Exception:
            detail = "Restore failed during a Docker operation. Some files may have been restored. Restart recovery has been attempted."
        finally:
            if container and store.rows("SELECT 1 FROM restart_journal WHERE container_id=? AND job_id=?", (container.id, job_id)):
                for attempt in range(3):
                    try:
                        container.reload()
                        if container.status != "running":
                            container.start()
                        container.reload()
                        if container.status != "running":
                            raise BackupError("Container did not remain running.")
                        store.execute("DELETE FROM restart_journal WHERE container_id=?", (container.id,))
                        detail += " Container restarted."
                        break
                    except Exception:
                        if attempt == 2:
                            status = "failed"
                            detail += " Restart needs attention. Recovery will retry automatically."
                        else:
                            time.sleep(1)
            try:
                store.job_update(job_id, status=status, detail=detail, finished=datetime.now(timezone.utc).isoformat())
            finally:
                try:
                    shutil.rmtree(upload["folder"])
                finally:
                    self.engine.lock.release()
