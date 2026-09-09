"""Governed ontology editing; reference checks are not fact checks."""
import copy
import hashlib
import json
import unicodedata
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .knowledge import INJECTION_PATTERNS
from .rag_settings import atomic_json


PREDICATES = frozenset({"HAS_SUBSIDIARY", "IS_PRODUCT_CATEGORY_OF", "BELONGS_TO",
                        "IS_A", "RESPONSIBLE_CONTACT", "DOCUMENTED_BY", "USES_MATERIAL",
                        "INCLUDES", "HAS_TECHNICAL_PROPERTY", "PROVIDES", "HAS_LOCATION"})


def inspect_ontology(tools, payload=None):
    payload = tools.ontology if payload is None else payload
    report = {"version": None, "mode": "read_only", "retrieval_integrated": False,
              "issues": [], "entities": [], "relationships": [], "aliases": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("relationships"), list):
        report["issues"].append("Ontology must contain a relationships list.")
        return report
    version = payload.get("version")
    if isinstance(version, str) and 0 < len(version) <= 100:
        report["version"] = version
    else:
        report["issues"].append("Missing or invalid ontology version.")
    sources = {r["source_id"]: r for r in tools.knowledge.records}
    contacts = {c["contact_id"]: c for c in tools.contacts if c.get("active")}
    entities, seen = {}, set()
    fields = {"subject", "predicate", "object", "source_id"}
    for index, raw in enumerate(payload["relationships"], 1):
        row = {"row": index, "issues": [], "source": None}
        report["relationships"].append(row)
        if (not isinstance(raw, dict) or set(raw) != fields or
                any(not isinstance(raw.get(k), str) or not raw[k].strip() or
                    len(raw[k]) > 300 for k in fields)):
            row["issues"].append("Expected subject, predicate, object and source_id as nonempty text (maximum 300 characters).")
            continue
        row.update(raw)
        signature = tuple(raw[k] for k in sorted(fields))
        if signature in seen:
            row["issues"].append("Duplicate relationship.")
        seen.add(signature)
        if raw["predicate"] not in PREDICATES:
            row["issues"].append("Unsupported relationship type.")
        if any(INJECTION_PATTERNS.search(v) or "<" in v or ">" in v for v in raw.values()):
            row["issues"].append("Unsafe markup or instruction-like text requires review.")
        source = sources.get(raw["source_id"])
        if source is None:
            row["issues"].append("Source is missing or inactive in the approved registry.")
        else:
            row["source"] = {k: source[k] for k in ("source_id", "title", "canonical_url", "content", "retrieved_at")}
        if raw["predicate"] == "DOCUMENTED_BY" and raw["object"] not in sources:
            row["issues"].append("Document target is not an active approved source.")
        if raw["predicate"] == "RESPONSIBLE_CONTACT":
            contact = contacts.get(raw["object"])
            if contact is None or contact.get("official_source_url") not in tools.knowledge.approved_urls:
                row["issues"].append("Contact target is not active and source-approved.")
        for label in (raw["subject"], raw["object"]):
            entities.setdefault(label, []).append(index)
    report["entities"] = [{"label": label, "relationship_rows": rows} for label, rows in sorted(entities.items())]
    raw_aliases = payload.get("aliases", [])
    if not isinstance(raw_aliases, list):
        report["issues"].append("Ontology aliases must be a list.")
        return report
    alias_fields, seen_aliases = {"entity", "alias", "language", "source_id"}, set()
    normalized_entities = {normalize_label(label) for label in entities}
    for index, raw in enumerate(raw_aliases, 1):
        row = {"row": index, "issues": [], "source": None}
        report["aliases"].append(row)
        if (not isinstance(raw, dict) or set(raw) != alias_fields or
                any(not isinstance(raw.get(k), str) or not raw[k].strip() or
                    len(raw[k]) > 300 for k in alias_fields)):
            row["issues"].append("Expected entity, alias, language and source_id as nonempty text (maximum 300 characters).")
            continue
        row.update(raw)
        normalized = normalize_label(raw["alias"])
        signature = (raw["language"], normalized)
        if signature in seen_aliases:
            row["issues"].append("Duplicate or ambiguous alias in this language.")
        seen_aliases.add(signature)
        if not normalized or normalized in normalized_entities:
            row["issues"].append("Alias must differ from every canonical entity label.")
        if raw["entity"] not in entities:
            row["issues"].append("Canonical entity is not a relationship endpoint.")
        if raw["language"] not in {"sv", "en"}:
            row["issues"].append("Alias language must be sv or en.")
        if any(INJECTION_PATTERNS.search(v) or "<" in v or ">" in v for v in raw.values()):
            row["issues"].append("Unsafe markup or instruction-like text requires review.")
        source = sources.get(raw["source_id"])
        if source is None or source.get("language") != raw["language"]:
            row["issues"].append("Source is missing, inactive or not in the alias language.")
        else:
            row["source"] = {k: source[k] for k in ("source_id", "title", "canonical_url", "content", "retrieved_at")}
    return report


def normalize_label(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFC", value).casefold()))


class Relationship(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    subject: str = Field(min_length=1, max_length=300)
    predicate: str = Field(min_length=1, max_length=300)
    object: str = Field(min_length=1, max_length=300)
    source_id: str = Field(min_length=1, max_length=300)

    @field_validator("subject", "predicate", "object", "source_id")
    @classmethod
    def safe_text(cls, value):
        if any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError("Control characters are not allowed")
        return unicodedata.normalize("NFC", value)


class OntologyAlias(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    entity: str = Field(min_length=1, max_length=300)
    alias: str = Field(min_length=1, max_length=300)
    language: Literal["sv", "en"]
    source_id: str = Field(min_length=1, max_length=300)

    @field_validator("entity", "alias", "source_id")
    @classmethod
    def safe_text(cls, value):
        if any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError("Control characters are not allowed")
        return unicodedata.normalize("NFC", value)


class OntologyChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation: Literal["add", "update", "delete"]
    row: int | None = Field(default=None, ge=1)
    relationship: Relationship | None = None
    confirmed: bool

    @model_validator(mode="after")
    def check_operation(self):
        if not self.confirmed:
            raise ValueError("Explicit confirmation is required")
        if (self.operation == "add") != (self.row is None):
            raise ValueError("Update/delete require a row; add must not include one")
        if (self.operation == "delete") != (self.relationship is None):
            raise ValueError("Add/update require a relationship; delete must not include one")
        return self


class OntologyAliasChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation: Literal["add", "update", "delete"]
    row: int | None = Field(default=None, ge=1)
    alias: OntologyAlias | None = None
    confirmed: bool

    @model_validator(mode="after")
    def check_operation(self):
        if not self.confirmed:
            raise ValueError("Explicit confirmation is required")
        if (self.operation == "add") != (self.row is None):
            raise ValueError("Update/delete require a row; add must not include one")
        if (self.operation == "delete") != (self.alias is None):
            raise ValueError("Add/update require an alias; delete must not include one")
        return self


class OntologyConflict(ValueError):
    pass


class OntologyValidationError(ValueError):
    pass


def revision_of(payload):
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class OntologyAdmin:
    def __init__(self, rag):
        self.tools = rag.tools
        # Serialize changes with source-index rebuilds as well as other edits.
        self.lock = rag.lock
        self.path = self.tools.root / "ontology.json"
        self.backup_path = self.tools.root / "ontology.previous.json"

    def _status(self):
        result = inspect_ontology(self.tools)
        result.update(mode="editable", retrieval_integrated=True, revision=revision_of(self.tools.ontology),
                      predicates=sorted(PREDICATES),
                      sources=[{k: r[k] for k in ("source_id", "title", "content", "canonical_url", "language")}
                               for r in self.tools.knowledge.records],
                      contact_ids=[c["contact_id"] for c in self.tools.contacts if c.get("active")])
        return result

    def status(self):
        with self.lock:
            return self._status()

    def change(self, change: OntologyChange):
        with self.lock:
            previous = self.tools.ontology
            if change.revision != revision_of(previous):
                raise OntologyConflict("Ontology changed since you opened the editor. Cancel and refresh before trying again.")
            try:
                disk = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                raise OntologyConflict("Ontology file is invalid. Resolve external file changes and restart before editing.") from None
            if disk != previous:
                raise OntologyConflict("Ontology file changed outside this app. Restart to load it before editing.")
            candidate = copy.deepcopy(previous)
            if not isinstance(candidate, dict) or not isinstance(candidate.get("relationships"), list):
                raise OntologyValidationError("Ontology structure is invalid; repair the file before editing.")
            rows = candidate["relationships"]
            if change.operation == "add":
                rows.append(change.relationship.model_dump())
            else:
                if change.row > len(rows):
                    raise OntologyConflict("Relationship no longer exists. Cancel and refresh before trying again.")
                if change.operation == "delete":
                    rows.pop(change.row - 1)
                else:
                    rows[change.row - 1] = change.relationship.model_dump()
            if len(rows) > 4000:
                raise OntologyValidationError("The demo supports at most 500 relationships.")
            candidate["version"] = datetime.now(timezone.utc).isoformat()
            report = inspect_ontology(self.tools, candidate)
            issues = (report["issues"] +
                      [f"Relationship {r['row']}: {issue}" for r in report["relationships"] for issue in r["issues"]] +
                      [f"Alias {r['row']}: {issue}" for r in report["aliases"] for issue in r["issues"]])
            if issues:
                raise OntologyValidationError(" ".join(issues[:4]))
            # Complete both writes before changing the live reference. The backup
            # is a single previous snapshot, not an audit log or unlimited history.
            atomic_json(self.backup_path, previous)
            atomic_json(self.path, candidate)
            self.tools.ontology = candidate
            return self._status()

    def change_alias(self, change: OntologyAliasChange):
        with self.lock:
            previous = self.tools.ontology
            if change.revision != revision_of(previous):
                raise OntologyConflict("Ontology changed since you opened the editor. Cancel and refresh before trying again.")
            try:
                disk = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                raise OntologyConflict("Ontology file is invalid. Resolve external file changes and restart before editing.") from None
            if disk != previous:
                raise OntologyConflict("Ontology file changed outside this app. Restart to load it before editing.")
            candidate = copy.deepcopy(previous)
            rows = candidate.setdefault("aliases", [])
            if not isinstance(rows, list):
                raise OntologyValidationError("Ontology aliases are invalid; repair the file before editing.")
            if change.operation == "add":
                rows.append(change.alias.model_dump())
            else:
                if change.row > len(rows):
                    raise OntologyConflict("Alias no longer exists. Cancel and refresh before trying again.")
                if change.operation == "delete":
                    rows.pop(change.row - 1)
                else:
                    rows[change.row - 1] = change.alias.model_dump()
            if len(rows) > 4000:
                raise OntologyValidationError("The demo supports at most 500 aliases.")
            candidate["version"] = datetime.now(timezone.utc).isoformat()
            report = inspect_ontology(self.tools, candidate)
            issues = (report["issues"] +
                      [f"Relationship {r['row']}: {issue}" for r in report["relationships"] for issue in r["issues"]] +
                      [f"Alias {r['row']}: {issue}" for r in report["aliases"] for issue in r["issues"]])
            if issues:
                raise OntologyValidationError(" ".join(issues[:4]))
            atomic_json(self.backup_path, previous)
            atomic_json(self.path, candidate)
            self.tools.ontology = candidate
            return self._status()
