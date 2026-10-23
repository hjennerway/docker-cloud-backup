"""Authenticated connections to Cloud Backup instances on other Docker hosts."""
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from .discovery import BackupError


class Hosts:
    def __init__(self, store=None):
        self.store = store
        self.hosts = {}
        self.file_ids = set()
        self.sessions = {}
        self.lock = threading.RLock()
        filename = os.getenv("DOCKER_HOSTS_FILE")
        if not filename:
            self.load_saved()
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
                self.file_ids.add(host_id)
        except Exception:
            raise RuntimeError("Invalid Docker hosts file. Use a JSON list of unique id, name, url and password entries.") from None
        self.load_saved()

    def load_saved(self):
        if self.store:
            for host in self.store.rows("SELECT * FROM remote_hosts ORDER BY name,id"):
                if host["id"] not in self.file_ids:
                    host["password"] = self.store.cipher.decrypt(host["password"].encode()).decode()
                    self.hosts[host["id"]] = host

    def configuration(self):
        with self.lock:
            return [{"id": h["id"], "name": h["name"], "url": h["url"],
                     "read_only": h["id"] in self.file_ids, "password_set": bool(h["password"])}
                    for h in self.hosts.values()]

    def candidate(self, values, host_id=None):
        previous = self.hosts.get(host_id) if host_id else None
        if host_id and previous is None:
            raise BackupError("Unknown Docker host.")
        name, url = values["name"].strip(), values["url"].strip().rstrip("/")
        password = values.get("password", "")
        try:
            parsed = urlparse(url)
            port = parsed.port
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
                     and not parsed.password and not parsed.query and not parsed.fragment and not parsed.path
                     and not any(c.isspace() or c in "\\" for c in url))
        except ValueError:
            valid = False
        if not valid:
            raise BackupError("Enter the Cloud Backup app's HTTP or HTTPS address without a path, for example http://192.168.1.20:8088.")
        if not name or len(name) > 100:
            raise BackupError("Enter a host name of up to 100 characters.")
        if not password and previous and url == previous["url"]:
            password = previous["password"]
        if not password:
            raise BackupError("Enter this host's admin password. Re-enter it when changing the address.")
        return {"id": host_id or "host-" + uuid.uuid4().hex[:12], "name": name, "url": url, "password": password}

    def save(self, values, host_id=None):
        with self.lock:
            if host_id in self.file_ids:
                raise BackupError("This host is managed by the hosts file. Edit that file to change it.")
            host = self.candidate(values, host_id)
            if not host_id and len(self.hosts) >= 30:
                raise BackupError("You can configure up to 30 remote hosts.")
            secret = self.store.cipher.encrypt(host["password"].encode()).decode()
            self.store.execute("INSERT OR REPLACE INTO remote_hosts VALUES (?,?,?,?)",
                               (host["id"], host["name"], host["url"], secret))
            self.hosts[host["id"]] = host
            self.sessions = {k: v for k, v in self.sessions.items() if k[1] != host["id"]}
            return host["id"]

    def remove(self, host_id):
        with self.lock:
            if host_id in self.file_ids:
                raise BackupError("This host is managed by the hosts file. Edit that file to remove it.")
            if host_id not in self.hosts:
                raise BackupError("Unknown Docker host.")
            self.store.execute("DELETE FROM remote_hosts WHERE id=?", (host_id,))
            self.hosts.pop(host_id)
            self.sessions = {k: v for k, v in self.sessions.items() if k[1] != host_id}

    def test(self, values, host_id=None):
        with self.lock:
            host = self.candidate(values, host_id)
        try:
            with requests.Session() as connection:
                response = connection.post(host["url"] + "/api/login", json={"password": host["password"]},
                                           headers={"X-Requested-With": "cloud-backup"},
                                           timeout=(5, 30), allow_redirects=False)
                with response:
                    if response.status_code != 200 or not response.cookies.get("session"):
                        raise ValueError()
                with connection.get(host["url"] + "/api/dashboard", timeout=(5, 30), allow_redirects=False) as response:
                    data = response.json()
                    if response.status_code != 200 or not isinstance(data.get("stacks"), list):
                        raise ValueError()
                    if data.get("docker_error"):
                        raise BackupError("Signed in successfully, but Docker is unavailable on this host. Check its Docker connection.")
                # A test should not keep a signed-in session on the remote instance.
                connection.post(host["url"] + "/api/logout", headers={"X-Requested-With": "cloud-backup"},
                                timeout=(5, 10), allow_redirects=False).close()
        except BackupError:
            raise
        except Exception:
            raise BackupError("Could not connect. Check the app address, admin password and that the remote Cloud Backup instance is running.") from None
        return {"message": "Connected successfully. Docker is available on this host."}

    def public(self):
        with self.lock:
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
        with self.lock:
            cookie = self.cookie(host_id, session_id)
            base_url = self.hosts[host_id]["url"]
        try:
            response = requests.request(method, base_url + "/api/" + quote(path, safe="/"),
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
