"""Literal-phrase routing with durable revisions and immutable action guards."""
import json
import re
from contextlib import closing
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .database_maintenance import Migration, apply_migrations, open_database

Intent = Literal["information", "product", "service", "company", "out_of_scope"]
ACTION = re.compile(r"\b(booking|book an?|appointment\w*|meeting\w*|schedule\w*|availability|"
                    r"contact\w*|email\w*|quotation\w*|quote\w*|purchase\w*|buy|"
                    r"boka\w*|möte\w*|mötestid\w*|kontakt\w*|offert\w*|köpa|skicka\w*)\b", re.I)


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    rule_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,60}$")
    name: str = Field(min_length=1, max_length=100)
    phrases: list[str] = Field(min_length=1, max_length=25)
    intent: Intent = "information"
    language: Literal["all", "en", "sv"] = "all"
    priority: int = Field(default=100, ge=1, le=1000)
    enabled: bool = True

    @field_validator("phrases")
    @classmethod
    def phrase_values(cls, values):
        cleaned = []
        for value in values:
            phrase = " ".join(value.split())
            if not 1 <= len(phrase) <= 80:
                raise ValueError("Each phrase must contain 1 to 80 characters")
            if phrase.casefold() not in {item.casefold() for item in cleaned}:
                cleaned.append(phrase)
        return cleaned

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Name cannot be blank")
        return value.strip()


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=1)
    operation: Literal["create", "update", "delete"]
    rule: Rule
    confirmed: Literal[True]


class Preview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    phrase: str = Field(min_length=1, max_length=500)
    language: Literal["en", "sv"] = "en"


class RuleConflict(ValueError):
    pass


DEFAULTS = [
    Rule(rule_id="product", name="Products", phrases=["product", "products", "produkt", "produkter"], intent="product", priority=100),
    Rule(rule_id="service", name="Services", phrases=["service", "services", "tjänst", "tjänster"], intent="service", priority=110),
    Rule(rule_id="company", name="Company", phrases=["company", "företag", "about us", "om oss"], intent="company", priority=120),
]


def decide(rules, phrase, language):
    if ACTION.search(phrase):
        return {"intent": "action_unavailable", "rule_id": None, "protected": True}
    text = " ".join(phrase.casefold().split())
    for rule in sorted(rules, key=lambda item: (item.priority, item.rule_id)):
        if not rule.enabled or rule.language not in {"all", language}:
            continue
        if any(re.search(r"(?<!\w)" + re.escape(value.casefold()) + r"(?!\w)", text) for value in rule.phrases):
            return {"intent": rule.intent, "rule_id": rule.rule_id, "protected": False}
    return {"intent": "information", "rule_id": None, "protected": False}


class RoutingStore:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(open_database(path)) as database, database:
            def schema(db):
                db.execute("CREATE TABLE routing (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, payload TEXT NOT NULL, previous TEXT)")
                db.execute("INSERT INTO routing(id,revision,payload) VALUES(1,1,?)",
                           (json.dumps([rule.model_dump() for rule in DEFAULTS]),))
            apply_migrations(database, "routing", (Migration(1, "create routing rules", schema),))
        self.status()  # Invalid persisted rules fail startup without overwriting them.

    @staticmethod
    def decode(payload):
        raw = json.loads(payload)
        if not isinstance(raw, list) or len(raw) > 100:
            raise ValueError("Routing requires at most 100 rules")
        rules = [Rule.model_validate(item) for item in raw]
        if len({rule.rule_id for rule in rules}) != len(rules):
            raise ValueError("Duplicate routing rule IDs")
        return rules

    def status(self):
        with closing(open_database(self.path)) as database:
            row = database.execute("SELECT revision,payload,previous FROM routing WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Routing record is missing")
        return {"revision": row[0], "rules": [r.model_dump() for r in sorted(self.decode(row[1]), key=lambda r: (r.priority, r.rule_id))],
                "previous_available": row[2] is not None, "match_mode": "literal_phrases",
                "action_rules_editable": False}

    def preview(self, phrase, language):
        return decide([Rule.model_validate(r) for r in self.status()["rules"]], phrase, language)

    def change(self, change):
        with closing(open_database(self.path)) as database, database:
            database.execute("BEGIN IMMEDIATE")
            revision, payload = database.execute("SELECT revision,payload FROM routing WHERE id=1").fetchone()
            if revision != change.revision:
                raise RuleConflict("Rules changed. Reload before saving.")
            rules = {r.rule_id: r for r in self.decode(payload)}
            key = change.rule.rule_id
            if change.operation == "create" and key in rules:
                raise ValueError("Rule ID already exists")
            if change.operation != "create" and key not in rules:
                raise ValueError("Rule does not exist")
            if change.operation == "delete":
                del rules[key]
            else:
                rules[key] = change.rule
            updated = json.dumps([r.model_dump() for r in rules.values()])
            self.decode(updated)
            database.execute("UPDATE routing SET revision=?,payload=?,previous=? WHERE id=1",
                             (revision + 1, updated, payload))
        return self.status()
