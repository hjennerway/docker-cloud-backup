import hashlib
import hmac
import json
import os
import secrets
import shutil
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlparse

import docker
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field, StrictBool
from starlette.concurrency import run_in_threadpool

from .backup import BackupEngine, next_run, utcnow
from .auth import Credentials
from .destinations import Destinations, notify, validate_settings
from .discovery import BackupError, Discovery, validate_rule
from .store import DEFAULTS, SECRETS, Store
from .restore import restore_limit
from .scheduled import scheduled_jobs


class Login(BaseModel):
    password: str = Field(max_length=1024)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)
    confirm_password: str = Field(max_length=1024)


class Schedule(BaseModel):
    cron: str = Field(max_length=100)
    timezone: str = Field(max_length=100)
    enabled: StrictBool = False


class Rule(BaseModel):
    pattern: str
    paths: list[str]
    excludes: list[str] = []


class BackupSelection(BaseModel):
    mode: Literal["auto", "volumes", "manual"] = "auto"
    volumes: list[str] = Field(default_factory=list, max_length=100)
    paths: dict[str, list[str]] = Field(default_factory=dict, max_length=100)


class ManualBackupPreference(BaseModel):
    include_in_manual_backup: StrictBool


class RestorePreview(BaseModel):
    token: str = Field(min_length=1, max_length=100)
    container_id: str = Field(min_length=1, max_length=100)
    source_index: int = Field(ge=0, le=999)


class RestoreSelection(BaseModel):
    token: str = Field(min_length=1, max_length=100)
    confirmed: StrictBool = False


def create_app(store=None, client=None, start_worker=True):
    store = store or Store(os.getenv("DATA_DIR", "/data"))
    serializer = URLSafeTimedSerializer(store.cipher._signing_key, salt="cloud-backup-session-v1")
    credentials = Credentials(store)
    attempts, attempt_lock = {}, threading.Lock()

    app = FastAPI(title="Docker Cloud Backup", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.client, app.state.engine = store, client, None
    app.state.init_lock = threading.Lock()
    app.state.destinations = Destinations(store)

    def engine():
        with app.state.init_lock:
            if app.state.engine is None:
                try:
                    if app.state.client is None:
                        app.state.client = docker.from_env(timeout=120)
                    discovery = Discovery(app.state.client, store, os.getenv("HOST_ROOT", "/host"), os.getenv("STACKS_ROOT", "/opt/stacks"))
                    app.state.engine = BackupEngine(store, discovery, app.state.destinations)
                    if start_worker:
                        app.state.engine.start()
                except Exception as exc:
                    app.state.engine = None
                    raise BackupError("Docker is unavailable. Check the socket mount and Docker daemon.") from exc
            return app.state.engine

    # Scheduler must run before anyone visits the UI and retry if Docker was down
    # at startup. Initialization happens in its own thread, outside the ASGI loop.
    boot_stop = threading.Event()

    @asynccontextmanager
    async def full_lifespan(app):
        def bootstrap():
            while not boot_stop.is_set():
                try:
                    engine()
                    return
                except BackupError:
                    boot_stop.wait(10)
        worker = None
        if start_worker:
            worker = threading.Thread(target=bootstrap, name="bootstrap", daemon=True)
            worker.start()
        yield
        boot_stop.set()
        if worker:
            worker.join(timeout=5)
        if app.state.engine:
            app.state.engine.close()
        if app.state.client:
            app.state.client.close()
    app.router.lifespan_context = full_lifespan

    def session(request):
        try:
            data = serializer.loads(request.cookies.get("session", ""), max_age=43200)
            if not hmac.compare_digest(data.get("auth", ""), credentials.session_version()):
                return None
            return data
        except (BadSignature, ValueError):
            return None

    @app.middleware("http")
    async def protect(request, call_next):
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/login", "/api/session"} and not session(request):
            return JSONResponse({"detail": "Sign in to continue."}, status_code=401)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if request.headers.get("x-requested-with") != "cloud-backup" or (origin and urlparse(origin).netloc != request.headers.get("host")):
                return JSONResponse({"detail": "Request origin rejected."}, status_code=403)
            try:
                content_length = int(request.headers.get("content-length", 0))
            except ValueError:
                return JSONResponse({"detail": "Invalid request size."}, status_code=400)
            limit = restore_limit() if request.url.path == "/api/restore/upload" else 100000
            if content_length < 0 or content_length > limit:
                return JSONResponse({"detail": "Request is too large."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(BackupError)
    async def backup_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/api/session")
    def current_session(request: Request):
        return {"authenticated": bool(session(request))}

    def check_password(request, verify, error_status=401):
        address = request.client.host if request.client else "unknown"
        with attempt_lock:
            now = time.monotonic()
            failures = [t for t in attempts.get(address, []) if now - t < 300]
            if len(failures) >= 10:
                raise HTTPException(429, "Too many attempts. Try again in five minutes.")
            version = verify()
            if version is None:
                attempts[address] = failures + [now]
                raise HTTPException(error_status, "Incorrect password.")
            attempts.pop(address, None)
            return version

    def signed_in_response(version, session_id=None):
        response = JSONResponse({"ok": True})
        response.set_cookie("session", serializer.dumps({"auth": version, "id": session_id or secrets.token_hex(16)}),
                            httponly=True, samesite="lax", secure=os.getenv("COOKIE_SECURE", "false").lower() == "true", max_age=43200)
        return response

    @app.post("/api/login")
    def login(body: Login, request: Request):
        return signed_in_response(check_password(request, lambda: credentials.authenticate(body.password)))

    @app.post("/api/password")
    def change_password(body: PasswordChange, request: Request):
        user = session(request)
        if not user:
            raise HTTPException(401, "Sign in to continue.")
        if body.new_password != body.confirm_password:
            raise HTTPException(400, "New passwords do not match.")
        if body.current_password == body.new_password:
            raise HTTPException(400, "Choose a different new password.")
        version = check_password(request, lambda: credentials.change(
            body.current_password, body.new_password, user["auth"]), error_status=400)
        return signed_in_response(version, user["id"])

    @app.post("/api/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie("session")
        return response

    @app.get("/api/dashboard")
    def dashboard():
        error, stacks = None, []
        try:
            stacks = engine().discovery.stacks()
        except BackupError as exc:
            error = str(exc)
        jobs = store.rows("SELECT * FROM jobs ORDER BY started DESC LIMIT 100")
        return {"stacks": stacks, "jobs": jobs, "docker_error": error,
                "recovery": store.rows("SELECT name,job_id FROM restart_journal"),
                "settings": store.settings(redacted=True), "rules": store.rules(),
                "busy": bool(app.state.engine and app.state.engine.lock.locked()),
                "timezone": os.getenv("TZ", "UTC")}

    @app.get("/api/scheduled")
    def scheduled(days: int = 7, start: datetime | None = None,
                  after: datetime | None = None, after_stack: str = ""):
        if days not in {7, 30}:
            raise HTTPException(422, "Choose a 7-day or 30-day window.")
        if any(value is not None and value.tzinfo is None for value in (start, after)):
            raise HTTPException(422, "Scheduled job dates must include a timezone.")
        start = start.astimezone(timezone.utc) if start else utcnow()
        after = after.astimezone(timezone.utc) if after else None
        return scheduled_jobs(store.rows("SELECT * FROM schedules WHERE enabled=1"),
                              start, days, after, after_stack)

    @app.get("/api/restore/containers")
    def restore_containers():
        return {"containers": engine().restore.containers(), "max_upload_bytes": restore_limit()}

    @app.post("/api/restore/upload")
    async def upload_restore(request: Request, filename: str = "backup.zip"):
        if not filename.lower().endswith((".zip", ".tar.gz", ".tgz")) or len(filename) > 255:
            raise BackupError("Choose a ZIP or .tar.gz backup file.")
        worker = await run_in_threadpool(engine)
        manager = worker.restore
        await run_in_threadpool(manager.acquire)
        token = secrets.token_urlsafe(32)
        folder = manager.root / token
        keep_upload = False
        try:
            await run_in_threadpool(manager.cleanup)
            folder.mkdir(mode=0o700)
            path = folder / "upload"
            # Stream directly to the data volume: /tmp may be a small tmpfs.
            received = 0
            with path.open("wb") as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > restore_limit():
                        raise HTTPException(413, "The uploaded backup exceeds MAX_BACKUP_GB.")
                    if shutil.disk_usage(folder).free < len(chunk) + 256 * 1024 * 1024:
                        raise BackupError("Not enough free space to upload this backup.")
                    await run_in_threadpool(output.write, chunk)
            path.chmod(0o600)
            result = await run_in_threadpool(manager.inspect, token, folder, filename, session(request)["id"])
            keep_upload = True
            return result
        finally:
            try:
                if not keep_upload and folder.exists():
                    await run_in_threadpool(shutil.rmtree, folder)
            finally:
                worker.lock.release()

    @app.post("/api/restore/preview")
    def preview_restore(body: RestorePreview, request: Request):
        manager = engine().restore
        manager.acquire()
        try:
            return manager.preview(body.token, session(request)["id"], body.container_id, body.source_index)
        finally:
            manager.engine.lock.release()

    @app.post("/api/restore")
    def restore_container(body: RestoreSelection, request: Request):
        if not body.confirmed:
            raise BackupError("Confirm that the backed-up files may overwrite this container's data.")
        return {"job_id": engine().restore.submit(body.token, session(request)["id"])}

    @app.put("/api/stacks/{name:path}/schedule")
    def save_schedule(name: str, body: Schedule):
        due = next_run(body.cron, body.timezone)
        stack = engine().discovery.get(name)
        if body.enabled and stack["issues"]:
            raise BackupError("Resolve this stack's config issues before enabling its schedule.")
        store.execute("INSERT OR REPLACE INTO schedules VALUES (?,?,?,?,?)", (name, body.cron, body.timezone, int(body.enabled), due if body.enabled else None))
        return {"ok": True, "next_run": due}

    @app.post("/api/cron/preview")
    def cron_preview(body: Schedule):
        from datetime import datetime
        dates, after = [], utcnow()
        for _ in range(5):
            due = next_run(body.cron, body.timezone, after)
            dates.append(due)
            after = datetime.fromisoformat(due)
        return {"dates": dates}

    @app.post("/api/stacks/{name:path}/backup")
    def backup_stack(name: str):
        stack = engine().discovery.get(name)
        if stack["issues"]:
            raise BackupError(" ".join(stack["issues"]))
        return {"job_id": engine().submit(name)}

    @app.put("/api/stacks/{name:path}/manual-backup")
    def save_manual_backup(name: str, body: ManualBackupPreference):
        stack = engine().discovery.get(name)
        if body.include_in_manual_backup and stack["excluded"]:
            raise BackupError("Excluded stacks cannot be included in manual backup.")
        store.save_manual_backup(name, body.include_in_manual_backup)
        return {"include_in_manual_backup": body.include_in_manual_backup}

    @app.post("/api/backups/manual")
    def backup_selected():
        worker = engine()
        stacks = [stack for stack in worker.discovery.stacks(include_sizes=False)
                  if stack["include_in_manual_backup"]]
        if not stacks:
            raise BackupError("Select at least one stack to include in manual backup.")
        issues = [stack["name"] + ": " + " ".join(stack["issues"]) for stack in stacks if stack["issues"]]
        if issues:
            raise BackupError("Review the selected stacks before backing up. " + " ".join(issues))
        job_ids = worker.submit_many([stack["name"] for stack in stacks])
        return {"job_ids": job_ids, "count": len(job_ids)}

    @app.put("/api/stacks/{name:path}/selection")
    def save_selection(name: str, body: BackupSelection):
        selection = body.model_dump()
        if body.mode == "manual":
            for paths in body.paths.values():
                validate_rule("manual", paths, [])
            selection["paths"] = {service: paths for service, paths in body.paths.items() if paths}
            selection["volumes"] = []
        elif body.mode == "volumes":
            selection["paths"] = {}
        else:
            selection.update(volumes=[], paths={})
        discovery = engine().discovery
        stack = discovery.get(name)
        issues = discovery.configure_stack(stack, selection)
        if issues:
            raise BackupError(" ".join(issues))
        store.save_backup_selection(name, selection)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str):
        engine().retry_upload(job_id)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/download")
    def download(job_id: str):
        rows = store.rows("SELECT archive FROM jobs WHERE id=?", (job_id,))
        if not rows or not rows[0]["archive"]:
            raise HTTPException(404, "Archive not available.")
        path = store.root / "archives" / rows[0]["archive"]
        if not path.is_file():
            raise HTTPException(404, "Archive not available.")
        return FileResponse(path, filename=path.name, media_type="application/gzip")

    @app.put("/api/rules")
    def save_rule(body: Rule):
        validate_rule(body.pattern, body.paths, body.excludes)
        checked = engine().discovery.validate_mapping(body.model_dump())
        store.execute("INSERT OR REPLACE INTO rules VALUES (?,?,?)", (body.pattern, json.dumps(body.paths), json.dumps(body.excludes)))
        return {"ok": True, "checked_containers": checked}

    @app.delete("/api/rules/{pattern:path}")
    def delete_rule(pattern: str):
        store.execute("DELETE FROM rules WHERE pattern=?", (pattern,))
        return {"ok": True}

    @app.put("/api/settings")
    def save_settings(body: dict):
        unknown = set(body) - set(DEFAULTS)
        if unknown:
            raise BackupError("Unknown setting.")
        current = store.settings()
        for key, value in body.items():
            if type(value) is not type(DEFAULTS[key]):
                raise BackupError("Invalid setting type: " + key)
            # Empty secret fields preserve the stored value; disconnect is explicit.
            if key in SECRETS and not value:
                continue
            if key == "drive_token":
                try:
                    value = json.dumps(json.loads(value))
                except ValueError:
                    raise BackupError("Invalid Google Drive token JSON.") from None
            current[key] = value
        validate_settings(current)
        with app.state.destinations.lock:
            if body.get("drive_token"):
                # Check a candidate before replacing the saved connection.
                # The probe may refresh its token; save the refreshed value.
                app.state.destinations.folders(settings=current)
            store.save_settings({key: current[key] for key in body if key not in SECRETS or body[key]})
        return store.settings(redacted=True)

    @app.post("/api/destinations/{provider}/test")
    def test_destination(provider: str):
        return app.state.destinations.test(provider)

    @app.get("/api/drive/folders")
    def drive_folders(parent: str = "root"):
        return {"folders": app.state.destinations.folders(parent)}

    @app.post("/api/drive/disconnect")
    def disconnect():
        with app.state.destinations.lock:
            store.save_settings({"drive_enabled": False, "drive_token": "", "drive_folder_id": "", "drive_folder_name": ""})
        return {"ok": True}

    @app.post("/api/notifications/test")
    def test_notification():
        settings = store.settings()
        if settings["notification_type"] == "none":
            raise BackupError("Choose and save a notification service first.")
        notify(settings, "Cloud Backup test", "Your backup notifications are connected.")
        return {"message": "Test notification sent."}

    @app.post("/api/drive/connect")
    def drive_connect(request: Request):
        s = store.settings()
        uri = urlparse(s["oauth_redirect_uri"])
        if not s["drive_client_id"] or not s["drive_client_secret"]:
            raise BackupError("Enter your Google Web OAuth client ID and secret, or use the LAN token connection below.")
        if uri.scheme != "https" and not (uri.scheme == "http" and uri.hostname in {"localhost", "127.0.0.1"}):
            raise BackupError("Google browser sign-in needs an HTTPS hostname or localhost. Use the LAN token connection for a private IP address.")
        if uri.path != "/oauth/google/callback" or uri.query or uri.fragment or uri.username:
            raise BackupError("Redirect URL must end with /oauth/google/callback.")
        state = secrets.token_urlsafe(32)
        bound = hashlib.sha256((state + session(request)["id"]).encode()).hexdigest()
        store.execute("DELETE FROM oauth_states WHERE expires<?", (time.time(),))
        store.execute("INSERT INTO oauth_states VALUES (?,?)", (bound, time.time() + 600))
        return {"url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": s["drive_client_id"], "redirect_uri": s["oauth_redirect_uri"],
            "response_type": "code", "scope": "https://www.googleapis.com/auth/drive",
            "access_type": "offline", "prompt": "consent", "state": state,
        })}

    @app.get("/oauth/google/callback")
    def drive_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        user = session(request)
        if not user or not state:
            raise HTTPException(400, "Sign in on the configured redirect hostname before connecting Google Drive.")
        bound = hashlib.sha256((state + user["id"]).encode()).hexdigest()
        with store.db() as db:
            exists = db.execute("SELECT 1 FROM oauth_states WHERE state=? AND expires>?", (bound, time.time())).fetchone()
            db.execute("DELETE FROM oauth_states WHERE state=?", (bound,))
        if not exists or error or not code:
            raise HTTPException(400, "Google connection was cancelled or expired. Start again from Destinations.")
        s = store.settings()
        try:
            response = requests.post("https://oauth2.googleapis.com/token", data={
                "code": code, "client_id": s["drive_client_id"], "client_secret": s["drive_client_secret"],
                "redirect_uri": s["oauth_redirect_uri"], "grant_type": "authorization_code",
            }, timeout=30)
            response.raise_for_status()
            token = response.json()
            if not token.get("refresh_token"):
                raise ValueError()
            token["expiry"] = (utcnow() + timedelta(seconds=token.get("expires_in", 3600))).isoformat()
            with app.state.destinations.lock:
                candidate = {**s, "drive_token": json.dumps(token), "drive_enabled": False,
                             "drive_folder_id": "", "drive_folder_name": ""}
                app.state.destinations.folders(settings=candidate)
                store.save_settings({key: candidate[key] for key in ("drive_token", "drive_enabled", "drive_folder_id", "drive_folder_name")})
        except (requests.RequestException, ValueError):
            raise HTTPException(400, "Google token exchange failed. Check the OAuth client and redirect URL.") from None
        return RedirectResponse("/#destinations", status_code=303)

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(Path(__file__).parent / "static" / "icons" / "favicon.ico", media_type="image/vnd.microsoft.icon")

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    return app


app = create_app()
