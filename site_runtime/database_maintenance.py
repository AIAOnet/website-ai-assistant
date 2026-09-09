"""Versioned SQLite migrations plus safe backup and restore validation."""
from __future__ import annotations
from .configuration import setting

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
            database.execute("SAVEPOINT website_migration")
            try:
                migration.apply(database)
                database.execute(
                    "INSERT INTO schema_migrations(database_name, version, name, checksum, applied_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (database_name, migration.version, migration.name, migration.checksum,
                     datetime.now(timezone.utc).isoformat()),
                )
                database.execute("RELEASE SAVEPOINT website_migration")
            except Exception as problem:
                database.execute("ROLLBACK TO SAVEPOINT website_migration")
                database.execute("RELEASE SAVEPOINT website_migration")
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


def database_inventory(data_path: str | Path | None = None):
    data = Path(data_path or setting("WEBSITE_ASSISTANT_DATA_PATH", "data"))
    return {
        "appointments": Path(setting("WEBSITE_ASSISTANT_APPOINTMENT_DB", data / "appointments.db")),
        "diagnostics": data / "diagnostics.db",
        "admin_sessions": Path(setting("WEBSITE_ASSISTANT_ADMIN_SESSION_DB", data / "admin_sessions.db")),
        "security_audit": Path(setting("WEBSITE_ASSISTANT_SECURITY_AUDIT_DB", data / "security_audit.db")),
        "embedding_cache": data / "embedding_cache.db",
        "rate_limits": Path(setting("WEBSITE_ASSISTANT_RATE_LIMIT_DB", data / "rate_limits.db")),
        "usage_budget": data / "usage_budget.db",
    }


def _checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _versions(database):
    try:
        return {row[0]: row[1] for row in database.execute(
            "SELECT database_name, MAX(version) FROM schema_migrations GROUP BY database_name")}
    except sqlite3.Error as problem:
        raise DatabaseMigrationError("Database has no valid migration metadata") from problem


def backup_databases(output_directory: str | Path, inventory=None):
    inventory = inventory or database_inventory()
    output = Path(output_directory)
    if output.exists():
        raise FileExistsError("Backup destination already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".website-backup-", dir=output.parent))
    created = datetime.now(timezone.utc).isoformat()
    records = []
    try:
        for name, source_path in inventory.items():
            source_path = Path(source_path)
            if not source_path.is_file():
                raise FileNotFoundError(f"Required database is unavailable: {name}")
            destination = temporary / f"{name}.db"
            with closing(sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True)) as source:
                versions = _versions(source)
                with closing(sqlite3.connect(destination)) as target:
                    source.backup(target)
            with closing(sqlite3.connect(f"file:{destination.resolve()}?mode=ro", uri=True)) as check:
                if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise DatabaseMigrationError("Backup integrity check failed")
            records.append({"database": name, "file": destination.name,
                            "schema_versions": versions, "bytes": destination.stat().st_size,
                            "checksum": _checksum(destination)})
        manifest = {"format_version": 1, "created_at": created, "databases": records}
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, output)
        return manifest
    except Exception:
        for path in temporary.glob("*"):
            path.unlink(missing_ok=True)
        temporary.rmdir()
        raise


def validate_backup(directory: str | Path, expected_names=None):
    root = Path(directory)
    expected_names = set(expected_names or database_inventory())
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as problem:
        raise DatabaseMigrationError("Backup manifest is invalid") from problem
    if not isinstance(manifest, dict) or set(manifest) != {"format_version", "created_at", "databases"} \
            or manifest["format_version"] != 1 or not isinstance(manifest["created_at"], str) \
            or not isinstance(manifest["databases"], list):
        raise DatabaseMigrationError("Backup manifest is invalid")
    try:
        created_at = datetime.fromisoformat(manifest["created_at"])
    except ValueError as problem:
        raise DatabaseMigrationError("Backup manifest is invalid") from problem
    if created_at.tzinfo is None:
        raise DatabaseMigrationError("Backup manifest is invalid")
    records = manifest["databases"]
    if len(records) != len(expected_names) or any(not isinstance(record, dict) for record in records) \
            or {record.get("database") for record in records} != expected_names:
        raise DatabaseMigrationError("Backup database inventory is incomplete")
    for record in records:
        if set(record) != {"database", "file", "schema_versions", "bytes", "checksum"} \
                or not isinstance(record["database"], str) \
                or not isinstance(record["file"], str) \
                or not isinstance(record["schema_versions"], dict) \
                or not isinstance(record["bytes"], int) or record["bytes"] < 1 \
                or not isinstance(record["checksum"], str):
            raise DatabaseMigrationError("Backup manifest record is invalid")
        expected_file = f"{record['database']}.db"
        if record["file"] != expected_file:
            raise DatabaseMigrationError("Backup filename is invalid")
        path = root / expected_file
        if not path.is_file() or path.stat().st_size != record["bytes"] or _checksum(path) != record["checksum"]:
            raise DatabaseMigrationError("Backup checksum or size mismatch")
        with closing(sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)) as database:
            if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise DatabaseMigrationError("Backup database integrity check failed")
            if _versions(database) != record["schema_versions"]:
                raise DatabaseMigrationError("Backup schema version mismatch")
    return {"valid": True, "format_version": 1, "database_count": len(records),
            "created_at": manifest["created_at"]}


def main():
    parser = argparse.ArgumentParser(description="Website SQLite backup and validation")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="Create a consistent backup directory")
    backup.add_argument("destination")
    validate = commands.add_parser("validate", help="Validate a backup without restoring it")
    validate.add_argument("directory")
    arguments = parser.parse_args()
    result = (backup_databases(arguments.destination) if arguments.command == "backup"
              else validate_backup(arguments.directory))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
