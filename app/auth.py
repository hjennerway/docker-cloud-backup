"""Persistent administrator credentials; the environment supplies only the first password."""
import hashlib
import hmac
import os
import secrets

from .discovery import BackupError


PASSWORD_ITERATIONS = 600_000


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PASSWORD_ITERATIONS).hex()


class Credentials:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM admin_credentials WHERE id=1").fetchone():
                return
            password = os.getenv("ADMIN_PASSWORD", "")
            if not password:
                password_file = store.root / "admin-password.txt"
                if not password_file.exists():
                    password_file.write_text(secrets.token_urlsafe(24))
                    password_file.chmod(0o600)
                password = password_file.read_text().strip()
            salt = secrets.token_hex(16)
            db.execute("INSERT INTO admin_credentials VALUES (1,?,?,?)",
                       (salt, password_hash(password, salt), secrets.token_hex(32)))

    def session_version(self):
        return self.store.rows("SELECT session_version FROM admin_credentials WHERE id=1")[0]["session_version"]

    def authenticate(self, password):
        row = self.store.rows("SELECT * FROM admin_credentials WHERE id=1")[0]
        if hmac.compare_digest(password_hash(password, row["salt"]), row["password_hash"]):
            return row["session_version"]
        return None

    def change(self, current_password, new_password, session_version):
        # Serialize changes and recheck the session inside the transaction, so a
        # second request cannot overwrite a password changed by another browser.
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM admin_credentials WHERE id=1").fetchone()
            if not hmac.compare_digest(session_version, row["session_version"]):
                return None
            if not hmac.compare_digest(password_hash(current_password, row["salt"]), row["password_hash"]):
                return None
            salt, version = secrets.token_hex(16), secrets.token_hex(32)
            digest = password_hash(new_password, salt)
            try:
                (self.store.root / "admin-password.txt").unlink(missing_ok=True)
            except OSError:
                raise BackupError("Could not change the password. Check the data volume's permissions.") from None
            db.execute("UPDATE admin_credentials SET salt=?,password_hash=?,session_version=? WHERE id=1",
                       (salt, digest, version))
            return version
