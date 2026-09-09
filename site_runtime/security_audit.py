"""Bounded, attributable security audit with tamper-evident event sequencing."""
from __future__ import annotations
from .configuration import setting

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import time
from uuid import uuid4
from .database_maintenance import (DatabaseMigrationError, Migration, apply_migrations,
                                   open_database, require_columns)


SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,120}$")
ROLES = frozenset({"viewer", "editor", "administrator", "unauthenticated"})
OUTCOMES = frozenset({"success", "rejected", "error"})


def _security_audit_schema_v1(database):
    database.execute("""CREATE TABLE IF NOT EXISTS security_audit_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        timestamp TEXT NOT NULL,
        occurred_at REAL NOT NULL,
        actor TEXT NOT NULL,
        role TEXT NOT NULL,
        action TEXT NOT NULL,
        outcome TEXT NOT NULL,
        status INTEGER NOT NULL,
        target_type TEXT,
        target_id TEXT,
        previous_hash TEXT NOT NULL,
        event_hash TEXT NOT NULL UNIQUE
    )""")
    database.execute("CREATE INDEX IF NOT EXISTS security_audit_time "
                     "ON security_audit_events(occurred_at)")


class SecurityAuditStore:
    def __init__(self, database_path: str | Path, retention_days: int | None = None,
                 max_records: int | None = None):
        self.path = Path(database_path)
        self.retention_days = self._bounded(
            retention_days, "WEBSITE_ASSISTANT_AUDIT_RETENTION_DAYS", 90, 1, 365)
        self.max_records = self._bounded(
            max_records, "WEBSITE_ASSISTANT_AUDIT_MAX_RECORDS", 10000, 100, 100000)
        self.lock = Lock()
        self.ready = self._initialize()

    @staticmethod
    def _bounded(value, environment, default, minimum, maximum):
        try:
            parsed = int(setting(environment, str(default))) if value is None else int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def _connect(self):
        return open_database(self.path)

    def _initialize(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as database, database:
                apply_migrations(database, "security_audit", (
                    Migration(1, "create security audit events", _security_audit_schema_v1),))
                require_columns(database, "security_audit_events", {"sequence", "event_id",
                    "timestamp", "occurred_at", "actor", "role", "action", "outcome", "status",
                    "target_type", "target_id", "previous_hash", "event_hash"})
            return True
        except (OSError, sqlite3.Error, DatabaseMigrationError):
            return False

    @staticmethod
    def _safe(value, fallback):
        return value if isinstance(value, str) and SAFE_VALUE.fullmatch(value) else fallback

    @staticmethod
    def _digest(event):
        encoded = json.dumps(event, ensure_ascii=True, allow_nan=False,
                             sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def append(self, *, actor, role, action, outcome, status,
               target_type=None, target_id=None):
        if not self.ready:
            return False
        now = time()
        event = {
            "event_id": uuid4().hex,
            "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "occurred_at": now,
            "actor": self._safe(actor, "unknown"),
            "role": role if role in ROLES else "unauthenticated",
            "action": self._safe(action, "unknown"),
            "outcome": outcome if outcome in OUTCOMES else "error",
            "status": status if isinstance(status, int) and 100 <= status <= 599 else 500,
            "target_type": self._safe(target_type, None) if target_type is not None else None,
            "target_id": self._safe(target_id, None) if target_id is not None else None,
        }
        try:
            with self.lock, closing(self._connect()) as database, database:
                database.execute("BEGIN IMMEDIATE")
                previous = database.execute(
                    "SELECT event_hash FROM security_audit_events ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                previous_hash = previous[0] if previous else "0" * 64
                event_hash = self._digest({**event, "previous_hash": previous_hash})
                database.execute("""INSERT INTO security_audit_events(
                    event_id, timestamp, occurred_at, actor, role, action, outcome, status,
                    target_type, target_id, previous_hash, event_hash)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*event.values(), previous_hash, event_hash))
                cutoff = now - self.retention_days * 86400
                database.execute("DELETE FROM security_audit_events WHERE occurred_at<?", (cutoff,))
                database.execute("""DELETE FROM security_audit_events WHERE sequence NOT IN
                    (SELECT sequence FROM security_audit_events ORDER BY sequence DESC LIMIT ?)""",
                    (self.max_records,))
            return True
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return False

    def read(self, limit=100):
        if not self.ready:
            raise OSError("Security audit store unavailable")
        limit = max(1, min(100, int(limit)))
        try:
            with self.lock, closing(self._connect()) as database:
                database.row_factory = sqlite3.Row
                rows = database.execute(
                    "SELECT * FROM security_audit_events ORDER BY sequence DESC LIMIT ?", (limit,)
                ).fetchall()
                retained = database.execute("SELECT COUNT(*) FROM security_audit_events").fetchone()[0]
            chronological = list(reversed(rows))
            chain_valid = True
            previous = None
            for row in chronological:
                event = {key: row[key] for key in (
                    "event_id", "timestamp", "occurred_at", "actor", "role", "action",
                    "outcome", "status", "target_type", "target_id")}
                if self._digest({**event, "previous_hash": row["previous_hash"]}) != row["event_hash"]:
                    chain_valid = False
                if previous is not None and row["previous_hash"] != previous:
                    chain_valid = False
                previous = row["event_hash"]
            events = [{key: row[key] for key in (
                "sequence", "event_id", "timestamp", "actor", "role", "action", "outcome",
                "status", "target_type", "target_id", "previous_hash", "event_hash")}
                      for row in rows]
            return {"events": events, "retained_events": retained, "limit": limit,
                    "retention_days": self.retention_days, "max_records": self.max_records,
                    "chain_valid": chain_valid, "tamper_evident": True}
        except (OSError, sqlite3.Error, ValueError, TypeError) as problem:
            raise OSError("Security audit store unavailable") from problem
