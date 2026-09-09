"""Persistent review overlays; generated evidence is never rewritten."""
import hashlib
import json
import time
from contextlib import closing
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .database_maintenance import Migration, apply_migrations, open_database


class ReviewChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=0)
    decision: Literal["approved", "suppressed", "reset"]
    display_label: str | None = Field(default=None, max_length=160)
    confirmed: Literal[True]


class ReviewConflict(ValueError):
    pass


class ItemReviewChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["relationship", "alias"]
    item_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=0)
    decision: Literal["approved", "suppressed", "reset"]
    confirmed: Literal[True]


def item_identity(kind, item):
    parts = ([item["subject_id"], item["predicate"], item["object_id"]] if kind == "relationship"
             else [item["entity_id"], item["language"], item["label"]])
    return kind + ":" + hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def item_fingerprint(graph, item):
    proofs = {e["evidence_id"]: e for e in graph["evidence"]}
    return hashlib.sha256(json.dumps({"item": item, "evidence": [proofs[key] for key in item["evidence_ids"]]}, sort_keys=True).encode()).hexdigest()


def fingerprint(graph, entity):
    # Full source checksum deliberately makes even unrelated page edits stale.
    value = {"entity": entity, "checksum": graph["source_checksums"][entity["source_id"]]}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class OntologyReviews:
    def __init__(self, data):
        self.path = data / "ontology_reviews.db"
        with closing(open_database(self.path)) as db, db:
            def schema(connection):
                connection.execute("CREATE TABLE reviews (entity_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)")
                connection.execute("CREATE TABLE review_history (id INTEGER PRIMARY KEY, entity_id TEXT NOT NULL, payload TEXT NOT NULL)")
            def item_schema(connection):
                connection.execute("CREATE TABLE item_reviews (item_key TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)")
                connection.execute("CREATE TABLE item_review_history (id INTEGER PRIMARY KEY, item_key TEXT NOT NULL, payload TEXT NOT NULL)")
            apply_migrations(db, "ontology_reviews", (Migration(1, "create ontology reviews", schema),
                                                      Migration(2, "create relationship and alias reviews", item_schema)))

    def inspect(self, graph):
        with closing(open_database(self.path)) as db:
            db.execute("BEGIN")
            saved = {row[0]: json.loads(row[1]) for row in db.execute("SELECT entity_id,payload FROM reviews")}
            saved_items = {row[0]: json.loads(row[1]) for row in db.execute("SELECT item_key,payload FROM item_reviews")}
        item_reviews = []
        excluded = set()
        for kind, collection in (("relationship", "relationships"), ("alias", "aliases")):
            for item in graph[collection]:
                decision = saved_items.get(item_identity(kind, item))
                status = "automated"
                if decision and decision["decision"] != "reset":
                    status = decision["decision"] if decision["fingerprint"] == item_fingerprint(graph, item) else "needs_review"
                identifier = item[kind + "_id"]
                if status in {"suppressed", "needs_review"}:
                    excluded.add(identifier)
                item_reviews.append({"kind": kind, "item_id": identifier, "status": status,
                                     "revision": decision["revision"] if decision else 0, "saved_decision": decision})
        reviews = []
        suppressed = set()
        for entity in graph["entities"]:
            decision = saved.get(entity["entity_id"])
            status = "automated"
            if decision and decision["decision"] != "reset":
                status = decision["decision"] if decision["fingerprint"] == fingerprint(graph, entity) else "needs_review"
            if status in {"suppressed", "needs_review"}:
                suppressed.add(entity["entity_id"])
            reviews.append({"entity_id": entity["entity_id"], "status": status,
                            "revision": decision["revision"] if decision else 0,
                            "display_label": decision["display_label"] if status == "approved" and decision["display_label"] and not any(a["alias_id"] in excluded and a["entity_id"] == entity["entity_id"] and a["label"] == decision["display_label"] for a in graph["aliases"]) else entity["label"],
                            "saved_decision": decision})
        return {"reviews": reviews, "item_reviews": item_reviews, "effective_entity_ids": [e["entity_id"] for e in graph["entities"] if e["entity_id"] not in suppressed],
                "effective_relationship_ids": [e["relationship_id"] for e in graph["relationships"] if e["relationship_id"] not in excluded and e["subject_id"] not in suppressed and e["object_id"] not in suppressed],
                "effective_alias_ids": [e["alias_id"] for e in graph["aliases"] if e["alias_id"] not in excluded and e["entity_id"] not in suppressed]}

    def change_item(self, graph, change, actor, job_id):
        collection = "relationships" if change.kind == "relationship" else "aliases"
        item = next((i for i in graph[collection] if i[change.kind + "_id"] == change.item_id), None)
        if item is None:
            raise ReviewConflict("Review item is not present in this build.")
        identity = item_identity(change.kind, item)
        with closing(open_database(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM item_reviews WHERE item_key=?", (identity,)).fetchone()
            revision = row[0] if row else 0
            if revision != change.revision:
                raise ReviewConflict("Review changed. Reload before saving.")
            payload = {**change.model_dump(exclude={"confirmed"}), "revision": revision + 1,
                       "fingerprint": item_fingerprint(graph, item), "actor": actor, "job_id": job_id, "updated": time.time()}
            encoded = json.dumps(payload)
            db.execute("INSERT INTO item_reviews VALUES(?,?,?) ON CONFLICT(item_key) DO UPDATE SET revision=excluded.revision,payload=excluded.payload", (identity, revision + 1, encoded))
            db.execute("INSERT INTO item_review_history(item_key,payload) VALUES(?,?)", (identity, encoded))
        return self.inspect(graph)

    def change(self, graph, change, actor, job_id):
        entity = next((e for e in graph["entities"] if e["entity_id"] == change.entity_id), None)
        if entity is None:
            raise ReviewConflict("Entity is not present in this build.")
        allowed = {entity["label"]} | {a["label"] for a in graph["aliases"] if a["entity_id"] == change.entity_id}
        if change.display_label is not None and (change.decision != "approved" or change.display_label not in allowed):
            raise ReviewConflict("Choose the source label or an extracted alias for an approved entity.")
        with closing(open_database(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM reviews WHERE entity_id=?", (change.entity_id,)).fetchone()
            revision = row[0] if row else 0
            if revision != change.revision:
                raise ReviewConflict("Review changed. Reload before saving.")
            payload = {**change.model_dump(exclude={"confirmed"}), "revision": revision + 1,
                       "fingerprint": fingerprint(graph, entity), "actor": actor, "job_id": job_id, "updated": time.time()}
            encoded = json.dumps(payload)
            db.execute("INSERT INTO reviews VALUES(?,?,?) ON CONFLICT(entity_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload", (change.entity_id, revision + 1, encoded))
            db.execute("INSERT INTO review_history(entity_id,payload) VALUES(?,?)", (change.entity_id, encoded))
        return self.inspect(graph)
