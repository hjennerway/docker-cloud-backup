"""Authenticated connections to Cloud Backup instances on other Docker hosts."""
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from .discovery import BackupError


class Hosts:
    def __init__(self):
        self.hosts = {}
        self.sessions = {}
        self.lock = threading.Lock()
        filename = os.getenv("DOCKER_HOSTS_FILE")
        if not filename:
            return
        try:
            entries = json.loads(Path(filename).read_text(encoding="utf-8"))
            if not isinstance(entries, list) or len(entries) > 30:
                raise ValueError()
            for entry in entries:
                host_id, name, url, password = (entry[k] for k in ("id", "name", "url", "password"))
                parsed = urlparse(url)
                if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", host_id) or host_id == "local"
                        or host_id in self.hosts or not isinstance(name, str) or not 1 <= len(name) <= 100
                        or parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.username or parsed.password or parsed.query or parsed.fragment
                        or parsed.path not in {"", "/"} or not isinstance(password, str) or not password):
                    raise ValueError()
                self.hosts[host_id] = {"id": host_id, "name": name, "url": url.rstrip("/"), "password": password}
        except Exception:
            raise RuntimeError("Invalid Docker hosts file. Use a JSON list of unique id, name, url and password entries.") from None

    def public(self):
        return [{"id": "local", "name": "Local Docker host"}] + [
            {"id": h["id"], "name": h["name"]} for h in self.hosts.values()]

    def forget(self, session_id):
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if k[0] != session_id}

    def cookie(self, host_id, session_id):
        host = self.hosts.get(host_id)
        if host is None:
            raise BackupError("Unknown Docker host.")
        key = (session_id, host_id)
        with self.lock:
            now = time.monotonic()
            self.sessions = {k: v for k, v in self.sessions.items() if v[0] > now}
            cached = self.sessions.get(key)
            if cached:
                return cached[1]
            if len(self.sessions) >= 1000:
                raise BackupError("Too many host sessions. Try again later.")
            try:
                with requests.post(host["url"] + "/api/login", json={"password": host["password"]},
                                   headers={"X-Requested-With": "cloud-backup"},
                                   timeout=(5, 30), allow_redirects=False) as response:
                    cookie = response.cookies.get("session")
                    if response.status_code != 200 or not cookie:
                        raise ValueError()
            except Exception:
                raise BackupError("Cannot sign in to the selected Docker host. Check its connection and configured password.") from None
            self.sessions[key] = (now + 11 * 3600, cookie)
            return cookie

    def request(self, host_id, session_id, method, path, query, body, content_type):
        # Only application APIs are proxied. Never forward another host proxy,
        # authentication changes, arbitrary URLs or browser OAuth callbacks.
        allowed = (
            r"dashboard|scheduled|restore(?:/containers|/upload|/preview)?|cron/preview|"
            r"stacks/.+/(?:schedule|backup|manual-backup|selection)|backups/manual|"
            r"jobs/[^/]+/(?:retry|download)|rules(?:/.+)?|settings|"
            r"destinations/(?:smb|drive)/test|drive/(?:folders|disconnect)|notifications/test"
        )
        if not re.fullmatch(allowed, path) or "\\" in path or any(p in {".", ".."} for p in path.split("/")):
            raise BackupError("This action is unavailable through the host switcher. Open that host directly for browser Google sign-in.")
        cookie = self.cookie(host_id, session_id)
        try:
            response = requests.request(method, self.hosts[host_id]["url"] + "/api/" + quote(path, safe="/"),
                                        params=query, data=body,
                                        cookies={"session": cookie},
                                        headers={"X-Requested-With": "cloud-backup", "Content-Type": content_type},
                                        stream=True, timeout=(5, 120), allow_redirects=False)
            if response.status_code == 401:
                with self.lock:
                    self.sessions.pop((session_id, host_id), None)
                response.close()
                raise BackupError("The selected host's sign-in expired. Retry the action; check its configured password if it persists.")
            if 300 <= response.status_code < 400:
                response.close()
                raise BackupError("The selected host redirected the request. Configure its final app URL.")
            return response
        except requests.RequestException:
            raise BackupError("Cannot reach the selected Docker host. Check its connection and container status.") from None
