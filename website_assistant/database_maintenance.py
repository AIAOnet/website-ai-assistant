"""Versioned SQLite migrations plus safe backup and restore validation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SQLITE_BUSY_TIMEOUT_MS = 5000


class DatabaseMigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: object

    @property
    def checksum(self):
        return hashlib.sha256(f"{self.version}:{self.name}".encode()).hexdigest()


def open_database(path: str | Path, *, check_same_thread=True, row_factory=None):
    """Open a store connection with the shared bounded multi-process policy."""
    database = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
                               check_same_thread=check_same_thread)
    try:
        database.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=NORMAL")
        if row_factory is not None:
            database.row_factory = row_factory
        return database
    except Exception:
        database.close()
        raise


def apply_migrations(database, database_name: str, migrations: tuple[Migration, ...]):
    if not database_name or not migrations or [item.version for item in migrations] != list(
            range(1, len(migrations) + 1)):
        raise DatabaseMigrationError("Invalid migration registry")
    try:
        database.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            database_name TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            PRIMARY KEY(database_name, version))""")
        rows = database.execute(
            "SELECT version, name, checksum FROM schema_migrations WHERE database_name=? "
            "ORDER BY version", (database_name,)).fetchall()
        expected_versions = list(range(1, len(rows) + 1))
        if [row[0] for row in rows] != expected_versions or len(rows) > len(migrations):
            raise DatabaseMigrationError("Database migration history is incompatible")
        for row, migration in zip(rows, migrations):
            if row[1] != migration.name or row[2] != migration.checksum:
                raise DatabaseMigrationError("Database migration checksum mismatch")
        for migration in migrations[len(rows):]:
            database.execute("SAVEPOINT website_assistant_migration")
            try:
                migration.apply(database)
                database.execute(
                    "INSERT INTO schema_migrations(database_name, version, name, checksum, applied_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (database_name, migration.version, migration.name, migration.checksum,
                     datetime.now(timezone.utc).isoformat()),
                )
                database.execute("RELEASE SAVEPOINT website_assistant_migration")
            except Exception as problem:
                database.execute("ROLLBACK TO SAVEPOINT website_assistant_migration")
                database.execute("RELEASE SAVEPOINT website_assistant_migration")
                raise DatabaseMigrationError("Database migration failed") from problem
        return len(migrations)
    except sqlite3.Error as problem:
        raise DatabaseMigrationError("Database migration failed") from problem


def require_columns(database, table: str, expected: set[str]):
    if not table.replace("_", "").isalnum():
        raise DatabaseMigrationError("Database schema validation failed")
    try:
        actual = {row[1] for row in database.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error as problem:
        raise DatabaseMigrationError("Database schema validation failed") from problem
    if actual != expected:
        raise DatabaseMigrationError("Database schema is incompatible")
