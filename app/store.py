"""Small SQLite store; secrets are encrypted with a key held in the data volume."""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet


DEFAULTS = {
    "smb_enabled": False, "smb_path": "", "smb_user": "", "smb_password": "",
    "smb_domain": "WORKGROUP", "drive_enabled": False, "drive_token": "",
    "drive_folder_id": "", "drive_folder_name": "", "drive_client_id": "",
    "drive_client_secret": "", "oauth_redirect_uri": "", "keep_days": 7,
    "notification_type": "none", "notification_url": "https://ntfy.sh",
    "notification_topic": "", "notification_token": "", "telegram_chat_id": "",
    "notify_success": True,
}
SECRETS = {"smb_password", "drive_token", "drive_client_secret", "notification_token"}

# Rules are explicit: no generic /data or media mount inference. More specific
# image patterns take precedence over wildcard patterns. Empty paths = stateless.
SEED_RULES = [
    {"pattern": pattern, "paths": paths, "excludes": excludes}
    for pattern, paths, excludes in [
        ("*/sonarr", ["/config"], ["logs/**"]),
        ("*/radarr", ["/config"], ["logs/**"]),
        ("*/prowlarr", ["/config"], ["logs/**"]),
        ("*/sabnzbd", ["/config"], ["logs/**"]),
        ("*/arch-sabnzbdvpn", ["/config"], ["logs/**"]),
        ("*/qbittorrent", ["/config"], ["logs/**"]),
        ("*/jellyseerr", ["/app/config"], ["logs/**"]),
        ("*/seerr", ["/app/config"], ["logs/**"]),
        ("*/uptime-kuma", ["/app/data"], []),
        ("*/portainer-ce", ["/data"], []),
        ("*/portainer-ee", ["/data"], []),
        ("lscr.io/linuxserver/jellyfin", ["/config"], ["cache/**", "log/**", "logs/**", "transcodes/**"]),
        ("linuxserver/jellyfin", ["/config"], ["cache/**", "log/**", "logs/**", "transcodes/**"]),
        ("jellyfin/jellyfin", ["/config"], ["cache/**", "log/**", "logs/**", "transcodes/**"]),
        ("library/eclipse-mosquitto", ["/mosquitto/config", "/mosquitto/data"], []),
        ("*/n8n", ["/home/node/.n8n"], []),
        ("*/go2rtc", ["/config"], []),
        ("*/netalertx", ["/data/config", "/data/db"], []),
        ("*/dockge", ["/app/data"], []),
        ("*/otterwiki", ["/app-data"], []),
        ("*/home-assistant", ["/config"], ["*.log*", "tts/**"]),
        ("*/frigate", ["/config"], []),
        ("*/dashy", ["/app/user-data"], []),
    ]
]


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        keyfile = self.root / "secret.key"
        if not keyfile.exists():
            with keyfile.open("xb") as f:
                f.write(Fernet.generate_key())
            keyfile.chmod(0o600)
        self.cipher = Fernet(keyfile.read_bytes())
        self.path = self.root / "backup.sqlite3"
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS remote_hosts (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    url TEXT NOT NULL, password TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS admin_credentials (id INTEGER PRIMARY KEY CHECK (id=1),
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL, session_version TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rules (pattern TEXT PRIMARY KEY, paths TEXT NOT NULL, excludes TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS schedules (stack TEXT PRIMARY KEY, cron TEXT NOT NULL,
                    timezone TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, next_run TEXT);
                CREATE TABLE IF NOT EXISTS backup_selections (stack TEXT PRIMARY KEY, selection TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS manual_backup_stacks (stack TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, stack TEXT NOT NULL,
                    status TEXT NOT NULL, started TEXT NOT NULL, finished TEXT, detail TEXT,
                    archive TEXT, size INTEGER DEFAULT 0, sha256 TEXT, receipts TEXT DEFAULT '{}',
                    notification_error TEXT);
                CREATE TABLE IF NOT EXISTS restart_journal (container_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL, job_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth_states (state TEXT PRIMARY KEY, expires REAL NOT NULL);
            """)
            if not db.execute("SELECT 1 FROM settings WHERE key='seeded'").fetchone():
                for rule in SEED_RULES:
                    db.execute("INSERT OR IGNORE INTO rules VALUES (?,?,?)", (
                        rule["pattern"], json.dumps(rule["paths"]), json.dumps(rule["excludes"])))
                db.execute("INSERT INTO settings VALUES ('seeded','true')")
            # Add the new mapping to existing installations once, preserving
            # custom rules and subsequent intentional deletion of this seed.
            if not db.execute("SELECT 1 FROM settings WHERE key='seeded_dashy_v1'").fetchone():
                rule = next(r for r in SEED_RULES if r["pattern"] == "*/dashy")
                db.execute("INSERT OR IGNORE INTO rules VALUES (?,?,?)", (
                    rule["pattern"], json.dumps(rule["paths"]), json.dumps(rule["excludes"])))
                db.execute("INSERT INTO settings VALUES ('seeded_dashy_v1','true')")
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self, redacted=False):
        result = dict(DEFAULTS)
        with self.db() as db:
            for row in db.execute("SELECT key,value FROM settings"):
                key, value = row
                if key not in result:
                    continue
                if key in SECRETS:
                    value = self.cipher.decrypt(value.encode()).decode()
                result[key] = json.loads(value)
        if redacted:
            for key in SECRETS:
                result[key + "_set"] = bool(result[key])
                result[key] = ""
        return result

    def save_settings(self, values):
        with self.db() as db:
            for key, value in values.items():
                if key not in DEFAULTS:
                    continue
                encoded = json.dumps(value)
                if key in SECRETS:
                    encoded = self.cipher.encrypt(encoded.encode()).decode()
                db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, encoded))

    def rows(self, sql, args=()):
        with self.db() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.db() as db:
            db.execute(sql, args)

    def rules(self):
        return [{**r, "paths": json.loads(r["paths"]), "excludes": json.loads(r["excludes"])}
                for r in self.rows("SELECT * FROM rules ORDER BY pattern")]

    def backup_selection(self, stack):
        rows = self.rows("SELECT selection FROM backup_selections WHERE stack=?", (stack,))
        return json.loads(rows[0]["selection"]) if rows else {"mode": "auto", "volumes": [], "paths": {}}

    def save_backup_selection(self, stack, selection):
        self.execute("INSERT OR REPLACE INTO backup_selections VALUES (?,?)", (stack, json.dumps(selection)))

    def manual_backup_stacks(self):
        return {row["stack"] for row in self.rows("SELECT stack FROM manual_backup_stacks")}

    def save_manual_backup(self, stack, included):
        if included:
            self.execute("INSERT OR IGNORE INTO manual_backup_stacks VALUES (?)", (stack,))
        else:
            self.execute("DELETE FROM manual_backup_stacks WHERE stack=?", (stack,))

    def job_update(self, job_id, **values):
        allowed = {"status", "finished", "detail", "archive", "size", "sha256", "receipts", "notification_error"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid job fields")
        self.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                     (*values.values(), job_id))
