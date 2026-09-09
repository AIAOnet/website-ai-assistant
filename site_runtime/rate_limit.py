"""Persistent, multi-process public API rate limiting with privacy-safe aggregates."""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .configuration import setting
from .database_maintenance import DatabaseMigrationError, Migration, apply_migrations, open_database, require_columns

ROUTES = {("POST", "/api/chat"): "chat", ("GET", "/api/availability/"): "appointments",
    ("POST", "/api/appointment-requests"): "appointments", ("GET", "/api/appointment-requests/"): "appointments",
    ("GET", "/api/appointment-requests"): "appointments", ("POST", "/api/appointment-requests/"): "appointments"}


def _bounded_environment(name, default, minimum=1, maximum=10000):
    try: value = int(setting(name, str(default)))
    except ValueError: value = default
    return max(minimum, min(maximum, value))


def _schema_v1(database):
    database.execute("CREATE TABLE rate_limit_requests (request_id INTEGER PRIMARY KEY AUTOINCREMENT, route TEXT NOT NULL CHECK(route IN ('chat','appointments')), client_hash TEXT NOT NULL, occurred_at REAL NOT NULL)")
    database.execute("CREATE INDEX rate_limit_window ON rate_limit_requests(route,client_hash,occurred_at)")
    database.execute("CREATE TABLE rate_limit_counters (route TEXT PRIMARY KEY CHECK(route IN ('chat','appointments')), allowed INTEGER NOT NULL DEFAULT 0 CHECK(allowed>=0), limited INTEGER NOT NULL DEFAULT 0 CHECK(limited>=0), failed INTEGER NOT NULL DEFAULT 0 CHECK(failed>=0))")
    database.executemany("INSERT INTO rate_limit_counters(route) VALUES(?)", (("chat",), ("appointments",)))
    database.execute("CREATE TABLE rate_limit_metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), client_secret TEXT NOT NULL, started_at TEXT NOT NULL)")
    database.execute("INSERT INTO rate_limit_metadata VALUES(1,?,?)", (secrets.token_hex(32), datetime.now(timezone.utc).isoformat()))


class PublicRateLimiter:
    def __init__(self, path, *, chat_limit=60, appointment_limit=30, window_seconds=60,
                 max_clients=10000, clock=time.time):
        self.path, self.clock, self.window_seconds, self.max_clients = Path(path), clock, window_seconds, max_clients
        self.limits = {"chat": chat_limit, "appointments": appointment_limit}
        self._initialize()

    @classmethod
    def from_environment(cls, data_path=None):
        data = Path(data_path or setting("WEBSITE_ASSISTANT_DATA_PATH", "data"))
        return cls(Path(setting("WEBSITE_ASSISTANT_RATE_LIMIT_DB", data / "rate_limits.db")),
            chat_limit=_bounded_environment("WEBSITE_ASSISTANT_CHAT_RATE_LIMIT", 60),
            appointment_limit=_bounded_environment("WEBSITE_ASSISTANT_APPOINTMENT_RATE_LIMIT", 30),
            window_seconds=_bounded_environment("WEBSITE_ASSISTANT_RATE_LIMIT_WINDOW_SECONDS", 60, 10, 3600))

    def _connect(self): return open_database(self.path)

    def _initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as database, database:
            apply_migrations(database, "rate_limits", (Migration(1, "persistent shared rate limits", _schema_v1),))
            require_columns(database, "rate_limit_requests", {"request_id", "route", "client_hash", "occurred_at"})
            require_columns(database, "rate_limit_counters", {"route", "allowed", "limited", "failed"})
            require_columns(database, "rate_limit_metadata", {"singleton", "client_secret", "started_at"})
            secret, self.started_at = database.execute("SELECT client_secret,started_at FROM rate_limit_metadata WHERE singleton=1").fetchone()
            if len(secret) != 64 or any(c not in "0123456789abcdef" for c in secret):
                raise DatabaseMigrationError("Rate-limit metadata is incompatible")
            self._secret = bytes.fromhex(secret)

    @staticmethod
    def route(method, path):
        if path == "/api/widget/session" and method == "POST": return "chat"
        if path.startswith("/api/widget/"): path = "/api/" + path[len("/api/widget/"):]
        exact = ROUTES.get((method, path))
        if exact: return exact
        for (route_method, prefix), name in ROUTES.items():
            if route_method == method and prefix.endswith("/") and path.startswith(prefix): return name
        return None

    def _client_hash(self, client):
        return hmac.new(self._secret, str(client).encode(), hashlib.sha256).hexdigest()

    def check(self, route, client):
        now, client_hash = float(self.clock()), self._client_hash(client)
        with closing(self._connect()) as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                database.execute("DELETE FROM rate_limit_requests WHERE occurred_at<=?", (now-self.window_seconds,))
                first, count = database.execute("SELECT MIN(occurred_at),COUNT(*) FROM rate_limit_requests WHERE route=? AND client_hash=?", (route, client_hash)).fetchone()
                if count >= self.limits[route]:
                    database.execute("UPDATE rate_limit_counters SET limited=limited+1 WHERE route=?", (route,)); database.commit()
                    return False, max(1, math.ceil(first+self.window_seconds-now))
                database.execute("INSERT INTO rate_limit_requests(route,client_hash,occurred_at) VALUES(?,?,?)", (route,client_hash,now))
                database.execute("UPDATE rate_limit_counters SET allowed=allowed+1 WHERE route=?", (route,))
                clients = database.execute("SELECT COUNT(*) FROM (SELECT 1 FROM rate_limit_requests GROUP BY route,client_hash)").fetchone()[0]
                if clients > self.max_clients:
                    database.execute("DELETE FROM rate_limit_requests WHERE (route,client_hash) IN (SELECT route,client_hash FROM rate_limit_requests GROUP BY route,client_hash ORDER BY MAX(occurred_at) LIMIT ?)", (clients-self.max_clients,))
                database.commit(); return True, 0
            except Exception: database.rollback(); raise

    def failed(self, route):
        with closing(self._connect()) as database, database:
            database.execute("UPDATE rate_limit_counters SET failed=failed+1 WHERE route=?", (route,))

    def metrics(self):
        cutoff = float(self.clock())-self.window_seconds
        with closing(self._connect()) as database:
            counters = {r: {"allowed": a, "limited": l, "failed": f} for r,a,l,f in database.execute("SELECT route,allowed,limited,failed FROM rate_limit_counters")}
            active = {route: database.execute("SELECT COUNT(DISTINCT client_hash) FROM rate_limit_requests WHERE route=? AND occurred_at>?", (route,cutoff)).fetchone()[0] for route in self.limits}
        return {"started_at": self.started_at, "window_seconds": self.window_seconds, "storage": "sqlite_shared",
            "routes": {route: {**counters[route], "limit_per_client": self.limits[route], "active_clients": active[route]} for route in self.limits},
            "privacy": "keyed_client_hashes_and_aggregate_counts"}
