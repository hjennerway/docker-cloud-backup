import fnmatch
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from docker.errors import NotFound

from .destinations import notify
from .discovery import BackupError, start_order

log = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc)


def next_run(expression, tz, after=None):
    if len(expression.split()) != 5:
        raise BackupError("Use five cron fields: minute hour day month weekday.")
    try:
        zone = ZoneInfo(tz)
        result = croniter(expression, (after or utcnow()).astimezone(zone), max_years_between_matches=8).get_next(datetime)
        return result.astimezone(timezone.utc).isoformat()
    except (ValueError, KeyError, ZoneInfoNotFoundError) as exc:
        raise BackupError("Invalid schedule or timezone, or no occurrence within eight years.") from exc


def stack_key(name):
    clean = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:70].strip(".") or "stack"
    return clean + "-" + hashlib.sha256(name.encode()).hexdigest()[:8]


class ChunkReader(io.RawIOBase):
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.pending = b""

    def readable(self):
        return True

    def readinto(self, buffer):
        while not self.pending:
            try:
                self.pending = next(self.chunks)
            except StopIteration:
                return 0
        count = min(len(buffer), len(self.pending))
        buffer[:count] = self.pending[:count]
        self.pending = self.pending[count:]
        return count


def append_archive(output, chunks, prefix, excludes, budget, cancelled):
    """Repack Docker tar stream without extraction, image export or shell access."""
    count = 0
    with io.BufferedReader(ChunkReader(chunks)) as stream, tarfile.open(fileobj=stream, mode="r|") as source:
        for member in source:
            if cancelled.is_set():
                raise BackupError("Backup interrupted by app shutdown; container restart requested.")
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise BackupError("Docker returned an unsafe archive path.")
            relative = str(PurePosixPath(*name.parts[1:])) if len(name.parts) > 1 else ""
            if any(fnmatch.fnmatchcase(relative, pattern) or
                   (pattern.endswith("/**") and relative == pattern[:-3]) for pattern in excludes):
                continue
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                continue  # Never include sockets, devices or FIFOs.
            budget[0] -= member.size
            if budget[0] < 0:
                raise BackupError("Config exceeds MAX_BACKUP_GB. Narrow the mapping or raise the limit.")
            source_file = source.extractfile(member) if member.isfile() else None
            member.name = prefix + "/" + str(name)
            # Preserve permissions, owner IDs and symlinks, but no extended path
            # overrides that could escape the archive namespace on restoration.
            member.pax_headers = {k: v for k, v in member.pax_headers.items() if k not in {"path", "linkpath"}}
            if member.islnk():
                link = PurePosixPath(member.linkname)
                if link.is_absolute() or ".." in link.parts:
                    raise BackupError("Docker returned an unsafe hardlink.")
                member.linkname = prefix + "/" + str(link)
            output.addfile(member, source_file)
            count += 1
    return count


class BackupEngine:
    def __init__(self, store, discovery, destinations):
        self.store, self.discovery, self.destinations = store, discovery, destinations
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backup")
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.scheduler = None
        self.archive_dir = store.root / "archives"
        self.archive_dir.mkdir(exist_ok=True)

    def start(self):
        self.recover()
        self.store.execute("UPDATE jobs SET status='interrupted',finished=?,detail='App restarted during this job. Local archive, if present, is retained; check restart recovery.' WHERE status IN ('queued','running','uploading')", (utcnow().isoformat(),))
        self.scheduler = threading.Thread(target=self.loop, name="scheduler", daemon=True)
        self.scheduler.start()

    def close(self):
        self.cancelled.set()
        if self.scheduler:
            self.scheduler.join(timeout=5)
        self.pool.shutdown(wait=True, cancel_futures=False)

    def recover(self):
        failures = []
        for item in self.store.rows("SELECT * FROM restart_journal ORDER BY rowid DESC"):
            try:
                container = self.discovery.client.containers.get(item["container_id"])
                container.reload()
                if container.status != "running":
                    container.start()
                    container.reload()
                if container.status != "running":
                    raise BackupError("Container has not returned to running state.")
                self.store.execute("DELETE FROM restart_journal WHERE container_id=?", (item["container_id"],))
            except Exception:
                failures.append(item["name"])
        return failures

    def loop(self):
        while not self.cancelled.wait(10):
            try:
                self.tick()
            except Exception:
                log.error("Scheduler iteration failed; retrying in ten seconds.")

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.recover():
                return
        finally:
            self.lock.release()
        now = utcnow()
        for schedule in self.store.rows("SELECT * FROM schedules WHERE enabled=1 ORDER BY next_run"):
            if schedule["next_run"] and datetime.fromisoformat(schedule["next_run"]) > now:
                continue
            # A single persisted next_run means a missed schedule runs once after
            # downtime. Busy stacks retain their due time until the worker is free.
            if self.submit(schedule["stack"], scheduled=True):
                self.store.execute("UPDATE schedules SET next_run=? WHERE stack=?", (
                    next_run(schedule["cron"], schedule["timezone"], now), schedule["stack"]))
                break

    def submit(self, stack, scheduled=False):
        if self.cancelled.is_set() or not self.lock.acquire(blocking=False):
            if scheduled:
                return None
            raise BackupError("Another backup or recovery is in progress. Try again shortly.")
        try:
            if self.store.rows("SELECT 1 FROM restart_journal"):
                raise BackupError("Container restart recovery needs attention before another backup can run.")
            job_id = uuid.uuid4().hex
            self.store.execute("INSERT INTO jobs (id,stack,status,started) VALUES (?,?,?,?)",
                               (job_id, stack, "queued", utcnow().isoformat()))
            self.pool.submit(self._work, job_id, stack)
            return job_id
        except Exception:
            self.lock.release()
            raise

    def _work(self, job_id, stack):
        try:
            self.run(job_id, stack)
        finally:
            self.lock.release()

    def run(self, job_id, name):
        partial = self.archive_dir / (job_id + ".partial")
        final = self.archive_dir / ("backup-" + utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + job_id[:8] + ".tar.gz")
        status, detail = "failed", "Backup failed."
        try:
            self.store.job_update(job_id, status="running", detail="Checking config mappings and Compose files")
            stack = self.discovery.get(name)
            if stack["issues"]:
                raise BackupError(" ".join(stack["issues"]))
            compose_files, dependencies = self.discovery.compose_bundle(stack)
            ordered = start_order(stack["containers"], dependencies)
            selected = []
            # Validate every path before any container is stopped. Never silently
            # skip a missing mapped path and report a misleading success.
            for item in ordered:
                c = self.discovery.client.containers.get(item["id"])
                c.reload()
                if c.status not in {"running", "exited", "created"}:
                    raise BackupError(f"{item['name']} is {c.status}; wait for a stable state before backup.")
                for path in item["rule"]["paths"]:
                    try:
                        api = self.discovery.client.api
                        # Docker's HEAD archive endpoint checks the path without
                        # reading live config or opening a tar stream.
                        response = api.head(api._url('/containers/{0}/archive', c.id), params={"path": path}, timeout=30)
                        try:
                            api._raise_for_status(response)
                        finally:
                            response.close()
                    except Exception:
                        raise BackupError(f"Config path {path} is not readable in {item['name']}. Update its image mapping.") from None
                selected.append((item, c, c.status == "running"))
            if shutil.disk_usage(self.archive_dir).free < 256 * 1024 * 1024:
                raise BackupError("Less than 256 MiB free in the backup data volume.")
            manifest = {"format_version": 1, "stack": name, "created": utcnow().isoformat(),
                        "compose_files": compose_files, "working_dir": stack["working_dir"], "containers": [],
                        "notes": ["Config and application state only. Images and unselected mounts are excluded.",
                                  "Compose and config may contain credentials. Protect this archive."]}
            restart_errors = []
            try:
                for item, c, was_running in reversed(selected):
                    if was_running:
                        # Commit before stop, so a crash between the two is safe.
                        self.store.execute("INSERT OR REPLACE INTO restart_journal VALUES (?,?,?)", (c.id, item["name"], job_id))
                        self.store.job_update(job_id, detail="Stopping " + item["name"])
                        c.stop(timeout=30)
                        c.reload()
                        if c.status == "running":
                            raise BackupError(f"Could not stop {item['name']}.")
                budget = [int(float(os.getenv("MAX_BACKUP_GB", "10")) * 1024**3)]
                with tarfile.open(partial, "w:gz", compresslevel=3) as archive:
                    for file in compose_files:
                        host_file = self.discovery.host_path(file)
                        budget[0] -= host_file.stat().st_size
                        if budget[0] < 0:
                            raise BackupError("Compose inputs exceed MAX_BACKUP_GB.")
                        archive.add(host_file, arcname="compose/" + file.lstrip("/"), recursive=False)
                    for item, c, was_running in selected:
                        entry = {"name": item["name"], "service": item["service"], "image": item["image"],
                                 "was_running": was_running, "mounts": item["mounts"], "paths": [], "excludes": item["rule"]["excludes"]}
                        for index, path in enumerate(item["rule"]["paths"]):
                            self.store.job_update(job_id, detail=f"Archiving {item['name']}: {path}")
                            chunks, stat = c.get_archive(path, chunk_size=1024 * 1024)
                            prefix = "containers/" + stack_key(item["name"]) + "/" + str(index)
                            try:
                                count = append_archive(archive, chunks, prefix, item["rule"]["excludes"], budget, self.cancelled)
                            finally:
                                close = getattr(chunks, "close", None)
                                if close:
                                    close()
                            if not count:
                                raise BackupError(f"Config path {path} produced an empty archive.")
                            entry["paths"].append({"source": path, "archive_prefix": prefix, "entries": count})
                        manifest["containers"].append(entry)
                    data = json.dumps(manifest, indent=2).encode()
                    member = tarfile.TarInfo("manifest.json")
                    member.size, member.mode = len(data), 0o600
                    archive.addfile(member, io.BytesIO(data))
                partial.replace(final)
                final.chmod(0o600)
                with final.open("rb") as f:
                    digest = hashlib.file_digest(f, "sha256").hexdigest()
                self.store.job_update(job_id, archive=final.name, size=final.stat().st_size, sha256=digest)
            finally:
                # Always attempt every restart, even if an earlier restart fails.
                journal = {r["container_id"] for r in self.store.rows("SELECT container_id FROM restart_journal WHERE job_id=?", (job_id,))}
                for item, c, was_running in selected:
                    if c.id not in journal:
                        continue
                    for attempt in range(3):
                        try:
                            c.reload()
                            if c.status != "running":
                                c.start()
                            c.reload()
                            if c.status != "running":
                                raise BackupError("Container did not remain running.")
                            self.store.execute("DELETE FROM restart_journal WHERE container_id=?", (c.id,))
                            break
                        except Exception:
                            if attempt == 2:
                                restart_errors.append(item["name"])
                            else:
                                time.sleep(1)
                if restart_errors:
                    raise BackupError("Restart needs attention: " + ", ".join(restart_errors) + ". Recovery will retry automatically.")
            # Network transfers happen only after containers have restarted.
            self.store.job_update(job_id, status="uploading", detail="Containers restored; uploading backup")
            receipts, errors = self.destinations.upload(final, stack_key(name))
            self.store.job_update(job_id, receipts=json.dumps(receipts))
            if errors:
                status, detail = "partial", "Local backup saved. " + " ".join(errors)
            else:
                status = "success"
                detail = "Backup complete; containers restored. " + ("Copied to " + ", ".join(receipts) + "." if receipts else "Saved locally; no remote destinations enabled.")
        except BackupError as exc:
            detail = str(exc)
        except Exception:
            # Docker/provider exceptions can echo credentials from Compose.
            detail = "Backup failed during Docker or archive operation. Check Docker, mappings and free disk space. Restart recovery has been attempted."
            log.error("Backup %s failed with an unexpected Docker/archive error", job_id)
        finally:
            partial.unlink(missing_ok=True)
            self.store.job_update(job_id, status=status, detail=detail, finished=utcnow().isoformat())
        self.report(job_id, name, status, detail)
        if status == "success":
            self.prune(name, job_id)

    def report(self, job_id, name, status, detail):
        settings = self.store.settings()
        if status == "success" and not settings["notify_success"]:
            return
        try:
            notify(settings, "Cloud Backup: " + status, name + "\n" + detail)
        except BackupError as exc:
            self.store.job_update(job_id, notification_error=str(exc))

    def retry_upload(self, job_id):
        if not self.lock.acquire(blocking=False):
            raise BackupError("A backup is already in progress.")
        try:
            rows = self.store.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
            if not rows or not rows[0]["archive"]:
                raise BackupError("No completed local archive to upload.")
            job = rows[0]
            if job["status"] not in {"partial", "success"}:
                raise BackupError("Resolve the failed backup or restart recovery before retrying its upload.")
            archive = self.archive_dir / job["archive"]
            if not archive.is_file():
                raise BackupError("Local archive no longer exists.")
            self.pool.submit(self._retry, job, archive)
        except Exception:
            self.lock.release()
            raise

    def _retry(self, job, archive):
        try:
            self.store.job_update(job["id"], status="uploading", detail="Retrying upload; containers are unchanged")
            receipts, errors = self.destinations.upload(archive, stack_key(job["stack"]), json.loads(job["receipts"]))
            status = "partial" if errors else "success"
            detail = " ".join(errors) if errors else "Upload complete."
            self.store.job_update(job["id"], status=status, detail=detail, receipts=json.dumps(receipts), finished=utcnow().isoformat())
            self.report(job["id"], job["stack"], status, detail)
        except Exception:
            self.store.job_update(job["id"], status="partial", detail="Upload retry failed; local archive retained.")
        finally:
            self.lock.release()

    def prune(self, name, current_id):
        cutoff = (utcnow() - timedelta(days=self.store.settings()["keep_days"])).isoformat()
        for job in self.store.rows("SELECT * FROM jobs WHERE stack=? AND status='success' AND started<? AND id!=? AND archive IS NOT NULL", (name, cutoff, current_id)):
            try:
                self.destinations.delete_receipts(json.loads(job["receipts"]))
                (self.archive_dir / job["archive"]).unlink(missing_ok=True)
                self.store.job_update(job["id"], archive=None, detail="Expired under retention policy; history retained.")
            except Exception:
                # Keep all copies on a retention failure; report without changing
                # the success status of the new backup.
                self.store.job_update(current_id, detail="Backup complete. Some expired copies could not be pruned; retained for safety.")
