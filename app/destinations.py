"""Bounded rclone operations. Only receipts created by this app are pruned."""
import configparser
import hashlib
import json
import re
import subprocess
import tempfile
import threading
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import requests

from .discovery import BackupError


def parse_smb(value):
    value = value.replace("\\", "/")
    if value.startswith("smb://"):
        value = "//" + value[6:]
    if not value.startswith("//"):
        raise BackupError("Use an SMB path such as //nas/backups/docker.")
    parts = value[2:].strip("/").split("/")
    if len(parts) < 2 or not re.fullmatch(r"[A-Za-z0-9._-]+", parts[0]):
        raise BackupError("SMB path needs a server and a share.")
    if any(p in {"", ".", ".."} or any(c in p for c in "\r\n\0:") for p in parts):
        raise BackupError("Invalid SMB path.")
    return parts[0], "/".join(parts[1:])


def validate_settings(settings):
    if not isinstance(settings["keep_days"], int) or not 1 <= settings["keep_days"] <= 3650:
        raise BackupError("Retention must be between 1 and 3650 days.")
    if settings["smb_enabled"]:
        parse_smb(settings["smb_path"])
        if not settings["smb_user"]:
            raise BackupError("Enter an SMB username.")
    for key, value in settings.items():
        if isinstance(value, str) and (len(value) > 16000 or any(c in value for c in "\r\n\0")):
            raise BackupError(f"Invalid value for {key}.")
    if settings["drive_token"]:
        try:
            token = json.loads(settings["drive_token"])
            if not isinstance(token, dict) or not token.get("refresh_token"):
                raise ValueError()
        except (ValueError, TypeError):
            raise BackupError("Google Drive needs the complete token JSON, including a refresh_token.") from None
    if settings["drive_enabled"] and (not settings["drive_token"] or not settings["drive_folder_id"]):
        raise BackupError("Connect Google Drive and select a destination folder first.")
    if settings["drive_folder_id"] and not re.fullmatch(r"[A-Za-z0-9_-]+", settings["drive_folder_id"]):
        raise BackupError("Invalid Google Drive folder ID.")
    if settings["notification_type"] not in {"none", "ntfy", "telegram", "pushbullet"}:
        raise BackupError("Unknown notification provider.")
    if settings["notification_type"] == "ntfy":
        parsed = urlparse(settings["notification_url"])
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise BackupError("Enter the ntfy server URL.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", settings["notification_topic"]):
            raise BackupError("Enter an ntfy topic using letters, numbers, underscores or hyphens.")
    if settings["notification_type"] in {"telegram", "pushbullet"} and not settings["notification_token"]:
        raise BackupError("Enter a notification token.")
    if settings["notification_type"] == "telegram" and not re.fullmatch(r"-?\d+", settings["telegram_chat_id"]):
        raise BackupError("Enter the Telegram numeric chat ID.")


class Destinations:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()

    def _run(self, args, config=None, timeout=120, input_text=None):
        cmd = ["rclone", *args]
        if config:
            cmd += ["--config", str(config), "--contimeout", "15s", "--timeout", "60s", "--retries", "2", "--low-level-retries", "2"]
        try:
            p = subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError:
            raise BackupError("rclone is missing. Use the supplied container image.") from None
        except subprocess.TimeoutExpired:
            raise BackupError("Destination timed out. Check network connectivity and try again.") from None
        if p.returncode:
            # Provider errors can contain tokens, URLs and credentials. Keep them
            # out of logs and UI; return only fixed, safe classifications.
            error = p.stderr.lower()
            if any(text in error for text in ("logon is invalid", "logon failure", "status_logon_failure", "bad username or authentication")):
                raise BackupError("SMB login was rejected. Check the username, password and domain.")
            if any(text in error for text in ("invalid_grant", "token has been expired or revoked", "failed to refresh token")):
                raise BackupError("Google Drive authorization expired or was revoked. Reconnect Google Drive.")
            if any(text in error for text in ("permission denied", "access is denied", "status_access_denied", "insufficientfilepermissions")):
                raise BackupError("The destination denied access. Check the account's permission to the chosen folder.")
            if any(text in error for text in ("no such host", "connection refused", "network is unreachable", "i/o timeout")):
                raise BackupError("The destination could not be reached. Check the server address and network.")
            raise BackupError("Destination operation failed. Check credentials, folder access, network and available space.")
        return p.stdout

    def run(self, settings, args, timeout=120, root_id=None):
        with self.lock, tempfile.TemporaryDirectory(prefix="cloud-backup-") as temp:
            config = configparser.RawConfigParser()
            if settings.get("smb_path"):
                host, _ = parse_smb(settings["smb_path"])
                password = self._run(["obscure", "-"], input_text=settings["smb_password"] + "\n").strip() if settings["smb_password"] else ""
                config["nas"] = {"type": "smb", "host": host, "user": settings["smb_user"],
                                 "pass": password, "domain": settings["smb_domain"]}
            if settings["drive_token"]:
                config["gdrive"] = {"type": "drive", "token": settings["drive_token"], "scope": "drive",
                                    "root_folder_id": root_id if root_id is not None else settings["drive_folder_id"]}
                if settings["drive_client_id"]:
                    config["gdrive"]["client_id"] = settings["drive_client_id"]
                    config["gdrive"]["client_secret"] = settings["drive_client_secret"]
            path = Path(temp) / "rclone.conf"
            with path.open("w") as f:
                config.write(f)
            path.chmod(0o600)
            try:
                return self._run(args, path, timeout)
            finally:
                # rclone refreshes tokens in its config. Persist the refresh only
                # if the connection has not been replaced by another UI request.
                refreshed = configparser.RawConfigParser()
                refreshed.read(path)
                if refreshed.has_option("gdrive", "token"):
                    token = refreshed.get("gdrive", "token")
                    current = self.store.settings()["drive_token"]
                    if token != settings["drive_token"] and current == settings["drive_token"]:
                        self.store.save_settings({"drive_token": token})
                        settings["drive_token"] = token

    def targets(self, settings):
        result = {}
        if settings["smb_enabled"]:
            host, path = parse_smb(settings["smb_path"])
            result["smb"] = {"remote": "nas:" + path.rstrip("/"), "identity": "smb:" + host.lower() + "/" + path}
        if settings["drive_enabled"]:
            result["drive"] = {"remote": "gdrive:", "identity": "drive:" + settings["drive_folder_id"]}
        return result

    def test(self, provider):
        settings = self.store.settings()
        if provider == "smb":
            _, path = parse_smb(settings["smb_path"])
            target = "nas:" + path
        elif provider == "drive" and settings["drive_token"] and settings["drive_folder_id"]:
            target = "gdrive:"
        else:
            raise BackupError("Connect and choose a destination first.")
        # Non-destructive read probe. Actual writes are verified on each backup.
        self.run(settings, ["lsjson", target, "--max-depth", "1", "--dirs-only"])
        return {"message": "Connection works. Upload permission is checked during the first backup."}

    def folders(self, folder_id="root"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id):
            raise BackupError("Invalid folder ID.")
        settings = self.store.settings()
        if not settings["drive_token"]:
            raise BackupError("Connect Google Drive first.")
        data = json.loads(self.run(settings, ["lsjson", "gdrive:", "--dirs-only", "--max-depth", "1"], root_id=folder_id))
        return sorted([{"id": item["ID"], "name": item["Name"]} for item in data if item.get("ID")], key=lambda f: f["name"].lower())

    def upload(self, archive, stack_key, existing=None):
        settings, receipts, errors = self.store.settings(), dict(existing or {}), []
        targets = self.targets(settings)
        for provider, target in targets.items():
            remote_dir = target["remote"].rstrip("/") + ("" if target["remote"].endswith(":") else "/") + "cloud-backup/" + stack_key
            remote = remote_dir + "/" + archive.name
            try:
                self.run(settings, ["copyto", str(archive), remote, "--immutable"], timeout=3600)
                info = json.loads(self.run(settings, ["lsjson", remote, "--stat"]))
                if info.get("Size") != archive.stat().st_size:
                    raise BackupError("Uploaded size does not match the local archive.")
                receipts[provider] = {"identity": target["identity"], "remote": remote}
            except BackupError as exc:
                errors.append(f"{provider}: {exc}")
        return receipts, errors

    def delete_receipts(self, receipts):
        settings = self.store.settings()
        targets = self.targets(settings)
        for provider, receipt in receipts.items():
            target = targets.get(provider)
            if not target or receipt["identity"] != target["identity"]:
                raise BackupError("Retention deferred: the original destination is no longer connected.")
            remote = receipt["remote"]
            prefix = target["remote"].rstrip("/") + ("" if target["remote"].endswith(":") else "/") + "cloud-backup/"
            if not remote.startswith(prefix) or ".." in PurePosixPath(remote).parts or not remote.endswith(".tar.gz"):
                raise BackupError("Refusing to remove a file outside this app's backup folder.")
            self.run(settings, ["deletefile", remote])


def notify(settings, title, message):
    kind = settings["notification_type"]
    if kind == "none":
        return
    try:
        if kind == "ntfy":
            headers = {"Title": title.encode("ascii", "replace").decode()}
            if settings["notification_token"]:
                headers["Authorization"] = "Bearer " + settings["notification_token"]
            response = requests.post(settings["notification_url"].rstrip("/") + "/" + settings["notification_topic"],
                                     data=message.encode(), headers=headers, timeout=20, allow_redirects=False)
        elif kind == "telegram":
            response = requests.post("https://api.telegram.org/bot" + settings["notification_token"] + "/sendMessage",
                                     json={"chat_id": settings["telegram_chat_id"], "text": title + "\n" + message}, timeout=20, allow_redirects=False)
        else:
            response = requests.post("https://api.pushbullet.com/v2/pushes",
                                     headers={"Access-Token": settings["notification_token"]},
                                     json={"type": "note", "title": title, "body": message}, timeout=20, allow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise BackupError("Notification provider rejected the message. Check its settings.")
    except requests.RequestException:
        raise BackupError("Notification could not be delivered. Check network and provider settings.") from None
