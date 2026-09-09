"""Immutable knowledge snapshots and transactional activation/rollback."""
import hashlib
import json
import re
import time
from contextlib import closing
from dataclasses import dataclass
from threading import Lock
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .chunking import build_chunks
from .database_maintenance import Migration, apply_migrations, open_database
from .knowledge import KnowledgeStore, validate_record
from .ontology_builder import validate_ontology
from .site_settings import SiteScope

MAX_SNAPSHOT_BYTES = 100_000_000


class VersionConflict(ValueError):
    pass


class ActivateVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    confirmed: Literal[True]
    allow_coverage_drop: bool = False
    replace_site: bool = False


class RestoreVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    confirmed: Literal[True]


@dataclass(frozen=True)
class Snapshot:
    version_id: str | None
    knowledge: KnowledgeStore
    ontology: dict
    home_url: str


def encode(payload):
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(text.encode()) > MAX_SNAPSHOT_BYTES:
        raise VersionConflict("Snapshot exceeds the supported size.")
    return text, hashlib.sha256(text.encode()).hexdigest()


def read_json(path):
    if path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError("Artifact exceeds size limit")
    return json.loads(path.read_text(encoding="utf-8"))


def effective_graph(graph, view):
    result = dict(graph)
    for kind, collection in (("entity", "entities"), ("relationship", "relationships"), ("alias", "aliases")):
        allowed = set(view["effective_" + kind + "_ids"])
        result[collection] = [dict(item) for item in graph[collection] if item[kind + "_id"] in allowed]
    names = {item["entity_id"]: item["display_label"] for item in view["reviews"]}
    for entity in result["entities"]:
        entity["display_label"] = names[entity["entity_id"]]
    return result


class Versions:
    def __init__(self, settings, jobs, reviews, legacy):
        self.settings, self.jobs, self.reviews = settings, jobs, reviews
        self.path = settings.data_path / "versions.db"
        self.legacy = Snapshot(None, legacy, {"version":1,"entities":[],"relationships":[],"aliases":[]}, settings.home_url)
        self.lock = Lock()
        self.cached = None
        with closing(open_database(self.path)) as db, db:
            def schema(connection):
                connection.execute("CREATE TABLE versions (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, created REAL NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, checksum TEXT NOT NULL)")
                connection.execute("CREATE TABLE active_version (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version_id TEXT REFERENCES versions(id), revision INTEGER NOT NULL, updated REAL NOT NULL)")
                connection.execute("INSERT INTO active_version VALUES(1,NULL,0,0)")
                connection.execute("CREATE TABLE version_history (revision INTEGER PRIMARY KEY, version_id TEXT NOT NULL REFERENCES versions(id), operation TEXT NOT NULL, actor TEXT NOT NULL, created REAL NOT NULL)")
            apply_migrations(db, "versions", (Migration(1, "create immutable versions and activation history", schema),))
        self.current()  # Fail closed on a corrupt active snapshot at startup.

    def _validate(self, payload):
        if payload["format_version"] != 1 or not payload["records"]:
            raise ValueError("Empty or unsupported snapshot")
        records = [validate_record(item) for item in payload["records"]]
        if len({item["source_id"] for item in records}) != len(records) or any(item["source_status"] != "active" for item in records):
            raise ValueError("Invalid source registry")
        scope = SiteScope(payload["home_url"])
        configured = SiteScope(self.settings.home_url)
        if any(not scope.allows(item["canonical_url"]) or (self.settings.home_url and not configured.allows(item["canonical_url"])) for item in records):
            raise ValueError("Source outside scope")
        if payload["chunks"] != build_chunks(records, None):
            raise ValueError("Passages do not match source records")
        graph = validate_ontology(payload["ontology"], records)
        # The review snapshot is frozen with this version, never re-read on rollback.
        reviewed = effective_graph(graph, payload["reviews"])
        return records, reviewed

    def _decode(self, text, checksum):
        try:
            if len(text.encode()) > MAX_SNAPSHOT_BYTES or hashlib.sha256(text.encode()).hexdigest() != checksum:
                raise ValueError("Snapshot checksum mismatch")
            payload = json.loads(text)
            self._validate(payload)
            return payload
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise VersionConflict("Saved version cannot be validated.") from error

    def current(self):
        with closing(open_database(self.path)) as db:
            row = db.execute("SELECT a.version_id,v.payload,v.checksum FROM active_version a LEFT JOIN versions v ON v.id=a.version_id WHERE a.singleton=1").fetchone()
        if row[0] is None:
            return self.legacy
        with self.lock:
            if self.cached and self.cached.version_id == row[0]:
                return self.cached
            payload = self._decode(row[1], row[2])
            knowledge = KnowledgeStore(self.settings.data_path / "sources.json", use_index=False,
                                       home_url=payload["home_url"], records_payload=payload["records"])
            self.cached = Snapshot(row[0], knowledge, effective_graph(payload["ontology"], payload["reviews"]), payload["home_url"])
            return self.cached

    def status(self):
        with closing(open_database(self.path)) as db:
            db.execute("BEGIN")
            active, revision = db.execute("SELECT version_id,revision FROM active_version WHERE singleton=1").fetchone()
            rows = db.execute("SELECT id,job_id,created,actor FROM versions ORDER BY created DESC LIMIT 20").fetchall()
            if active:
                row = db.execute("SELECT payload,checksum FROM versions WHERE id=?", (active,)).fetchone()
                payload = self._decode(*row)
                graph = effective_graph(payload["ontology"],payload["reviews"])
                summary = {"sources":len(payload["records"]),"entities":len(graph["entities"]),"relationships":len(graph["relationships"])}
            else:
                summary = {"sources":len(self.legacy.knowledge.records),"entities":0,"relationships":0}
        return {"active_version": active, "revision": revision, "auto_activate": self.settings.auto_activate,
                "summary":summary,
                "versions": [{"id":r[0],"job_id":r[1],"created":r[2],"actor":r[3]} for r in rows]}

    def activate(self, job_id, request, actor, *, automatic=False):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise VersionConflict("Invalid build identifier.")
        try:
            job = self.jobs.get(job_id)
            graph = self.jobs.ontology(job_id)
            directory = self.settings.data_path / "builds" / job_id
            records = read_json(directory / "sources.json")["records"]
            payload = {"format_version":1,"home_url":job["report"]["canonical_homepage"],"records":records,
                       "chunks":read_json(directory / "chunks.json")["chunks"],"ontology":graph,
                       "reviews":self.reviews.inspect(graph)}
            self._validate(payload)
            text, checksum = encode(payload)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise VersionConflict("Build is incomplete, empty, or invalid; current knowledge was kept.") from error
        version_id = uuid4().hex
        with closing(open_database(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            active, revision, updated = db.execute("SELECT version_id,revision,updated FROM active_version WHERE singleton=1").fetchone()
            if revision != request.revision or (automatic and updated > job["created"]):
                raise VersionConflict("Active knowledge changed. Review this build before activating it.")
            if active:
                row = db.execute("SELECT payload,checksum FROM versions WHERE id=?", (active,)).fetchone()
                old = self._decode(*row)
                if old["home_url"] != payload["home_url"] and not request.replace_site:
                    raise VersionConflict("Website changed. Confirm replacement of the current website.")
                old_ids = {r["source_id"] for r in old["records"]}
                missing = len(old_ids - {r["source_id"] for r in records}) / len(old_ids)
                if missing > self.settings.max_source_drop_fraction and not request.allow_coverage_drop:
                    raise VersionConflict("Source coverage dropped substantially. Review and explicitly allow reduced coverage.")
            now = time.time()
            db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?)", (version_id,job_id,now,actor,text,checksum))
            db.execute("UPDATE active_version SET version_id=?,revision=?,updated=? WHERE singleton=1", (version_id,revision+1,now))
            db.execute("INSERT INTO version_history VALUES(?,?,?,?,?)", (revision+1,version_id,"activate",actor,now))
        return self.status()

    def restore(self, version_id, request, actor):
        with closing(open_database(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload,checksum FROM versions WHERE id=?", (version_id,)).fetchone()
            if row is None:
                raise VersionConflict("Version not found.")
            self._decode(*row)
            revision = db.execute("SELECT revision FROM active_version WHERE singleton=1").fetchone()[0]
            if revision != request.revision:
                raise VersionConflict("Active knowledge changed. Reload before restoring.")
            now = time.time()
            db.execute("UPDATE active_version SET version_id=?,revision=?,updated=? WHERE singleton=1", (version_id,revision+1,now))
            db.execute("INSERT INTO version_history VALUES(?,?,?,?,?)", (revision+1,version_id,"restore",actor,now))
        return self.status()

    def completed(self, job_id):
        if not self.settings.auto_activate:
            return {"state":"staged","reason":"Automatic activation is disabled."}
        try:
            status = self.activate(job_id, ActivateVersion(revision=self.status()["revision"], confirmed=True), "automatic", automatic=True)
            return {"state":"active","version_id":status["active_version"]}
        except VersionConflict as error:
            return {"state":"review_required","reason":str(error)}
