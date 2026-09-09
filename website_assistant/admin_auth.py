"""Local admin authentication with durable, server-side revocable sessions."""
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from .database_maintenance import (DatabaseMigrationError, Migration, apply_migrations,
                                   open_database, require_columns)

COOKIE = "website_assistant_admin"
ROLES = frozenset({"viewer", "editor", "administrator"})


def _admin_session_schema_v1(database):
    database.execute("""CREATE TABLE IF NOT EXISTS admin_sessions (
        token_hash TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('viewer', 'editor', 'administrator')),
        csrf TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        configuration_fingerprint TEXT NOT NULL,
        revoked_at REAL
    )""")
    database.execute("CREATE INDEX IF NOT EXISTS admin_sessions_expiry ON admin_sessions(expires_at)")
    database.execute("CREATE INDEX IF NOT EXISTS admin_sessions_active ON admin_sessions(revoked_at, expires_at)")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return f"pbkdf2_sha256$600000${salt}${digest}"


def valid_hash(value: str) -> bool:
    try:
        algorithm, iterations, salt, digest = value.split("$")
        return (algorithm == "pbkdf2_sha256" and iterations == "600000"
                and len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32)
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Session:
    username: str
    role: str
    csrf: str
    expires: float


class AdminAuth:
    def __init__(self, settings):
        self.username = settings.admin_username.strip()
        self.password_hash = settings.admin_password_hash.get_secret_value()
        self.role = settings.admin_role.strip().lower()
        self.secure = settings.admin_cookie_secure
        self.ttl = settings.admin_session_minutes * 60
        self.database_path = settings.data_path / "admin_sessions.db"
        self.attempts = {}
        self.global_attempts = []
        self.lock = threading.Lock()
        self.store_ready = self._initialize_store()

    @property
    def configured(self):
        return bool(self.username and valid_hash(self.password_hash)
                    and self.role in ROLES and self.store_ready)

    def _connect(self):
        return open_database(self.database_path)

    def _initialize_store(self):
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as database, database:
                apply_migrations(database, "admin_sessions", (
                    Migration(1, "create admin sessions", _admin_session_schema_v1),))
                require_columns(database, "admin_sessions", {"token_hash", "username", "role",
                    "csrf", "created_at", "expires_at", "configuration_fingerprint", "revoked_at"})
            return True
        except (OSError, sqlite3.Error, DatabaseMigrationError):
            return False

    def _configuration_fingerprint(self):
        value = "\0".join((self.username, self.password_hash, self.role))
        return hashlib.sha256(value.encode()).hexdigest()

    def _revoke_mismatched(self, database, now):
        database.execute(
            "UPDATE admin_sessions SET revoked_at=? WHERE revoked_at IS NULL "
            "AND configuration_fingerprint<>?",
            (now, self._configuration_fingerprint()),
        )

    def login(self, username, password, peer):
        if not self.configured:
            return "disabled", None
        attempt_now = time.monotonic()
        with self.lock:
            self.attempts = {key: [t for t in values if attempt_now - t < 300]
                             for key, values in self.attempts.items()
                             if values[-1] > attempt_now - 300}
            self.global_attempts = [t for t in self.global_attempts if attempt_now - t < 300]
            attempts = self.attempts.get(peer, [])
            if len(attempts) >= 5 or len(self.global_attempts) >= 100:
                return "limited", None
            self.attempts[peer] = attempts + [attempt_now]
            self.global_attempts.append(attempt_now)
        _, _, salt, expected = self.password_hash.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
        password_ok = hmac.compare_digest(actual, expected)
        username_ok = hmac.compare_digest(username.encode(), self.username.encode())
        if not (password_ok and username_ok):
            return "invalid", None
        token = secrets.token_urlsafe(32)
        now = time.time()
        try:
            with self.lock, closing(self._connect()) as database, database:
                database.execute("BEGIN IMMEDIATE")
                self._revoke_mismatched(database, now)
                database.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (now,))
                active = database.execute(
                    "SELECT COUNT(*) FROM admin_sessions WHERE revoked_at IS NULL AND expires_at>?",
                    (now,),
                ).fetchone()[0]
                if active >= 100:
                    return "limited", None
                database.execute(
                    "INSERT INTO admin_sessions(token_hash, username, role, csrf, created_at, "
                    "expires_at, configuration_fingerprint, revoked_at) VALUES(?, ?, ?, ?, ?, ?, ?, NULL)",
                    (self._key(token), self.username, self.role, secrets.token_urlsafe(32),
                     now, now + self.ttl, self._configuration_fingerprint()),
                )
        except (OSError, sqlite3.Error):
            self.store_ready = False
            return "disabled", None
        return "ok", token

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, token):
        if not self.configured or not token or len(token) > 128:
            return None
        now = time.time()
        try:
            with self.lock, closing(self._connect()) as database, database:
                self._revoke_mismatched(database, now)
                row = database.execute(
                    "SELECT username, role, csrf, expires_at FROM admin_sessions "
                    "WHERE token_hash=? AND revoked_at IS NULL AND expires_at>? "
                    "AND configuration_fingerprint=?",
                    (self._key(token), now, self._configuration_fingerprint()),
                ).fetchone()
                if row:
                    return Session(row[0], row[1], row[2], row[3])
        except (OSError, sqlite3.Error):
            self.store_ready = False
        return None

    def logout(self, token):
        if not token or len(token) > 128 or not self.store_ready:
            return
        try:
            with self.lock, closing(self._connect()) as database, database:
                database.execute(
                    "UPDATE admin_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                    (time.time(), self._key(token)),
                )
        except (OSError, sqlite3.Error):
            self.store_ready = False

    def revoke_all(self):
        if not self.store_ready:
            return 0
        try:
            with self.lock, closing(self._connect()) as database, database:
                cursor = database.execute(
                    "UPDATE admin_sessions SET revoked_at=? WHERE revoked_at IS NULL",
                    (time.time(),),
                )
                return cursor.rowcount
        except (OSError, sqlite3.Error):
            self.store_ready = False
            return 0


if __name__ == "__main__":
    import getpass
    password = getpass.getpass("New admin password (at least 12 characters): ")
    if len(password) < 12 or password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords must match and contain at least 12 characters.")
    print("Paste this value in WEBSITE_ASSISTANT_ADMIN_PASSWORD_HASH in .env (keep it private):")
    print(hash_password(password))
