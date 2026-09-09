"""Governed CRUD for deterministic chat intent-routing expressions."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .rag_settings import atomic_json
from .router import Intent


class RegexRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=120)
    intent: Intent
    pattern: str = Field(min_length=1, max_length=600)
    priority: int = Field(ge=1, le=1000)
    language: Literal["all", "sv", "en"] = "all"
    enabled: bool = True


class RegexChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["create", "update", "delete"]
    revision: int = Field(ge=1)
    confirmed: Literal[True]
    rule: RegexRuleInput


class RegexPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phrase: str = Field(min_length=1, max_length=500)
    language: Literal["sv", "en"] = "en"


class RegexValidationError(ValueError):
    pass


class RegexConflict(RuntimeError):
    pass


DEFAULT_RULES = ({'rule_id': 'appointment', 'name': 'Appointment requests', 'intent': 'REQUEST_APPOINTMENT', 'priority': 10, 'language': 'all', 'enabled': True, 'pattern': '\\b(boka|möte|mötestid|appointment|meeting|request.*time)\\b'}, {'rule_id': 'availability', 'name': 'Availability requests', 'intent': 'REQUEST_APPOINTMENT', 'priority': 20, 'language': 'all', 'enabled': True, 'pattern': '\\b(lediga tider|tillgängliga tider|tillgänglighet|availability|available (?:times?|slots?)|slots?)\\b'}, {'rule_id': 'contact', 'name': 'Contact and sales requests', 'intent': 'FIND_CONTACT', 'priority': 30, 'language': 'all', 'enabled': True, 'pattern': '\\b(contact\\w*|kontakt\\w*|representative\\w*|säljare\\w*)\\b'}, {'rule_id': 'service', 'name': 'Service information', 'intent': 'SERVICE_INFORMATION', 'priority': 40, 'language': 'all', 'enabled': True, 'pattern': '\\b(service\\w*|tjänst\\w*)\\b'}, {'rule_id': 'product', 'name': 'Product information', 'intent': 'PRODUCT_INFORMATION', 'priority': 50, 'language': 'all', 'enabled': True, 'pattern': '\\b(product\\w*|produkt\\w*|equipment|utrustning\\w*)\\b'}, {'rule_id': 'company', 'name': 'Company information', 'intent': 'COMPANY_INFORMATION', 'priority': 60, 'language': 'all', 'enabled': True, 'pattern': '\\b(company|företag\\w*|organisation\\w*|organization\\w*|about us|om oss|who are you|vilka är ni)\\b'})


def validate_pattern(pattern: str):
    if "\x00" in pattern or "(?" in pattern.replace("(?:", "") or re.search(r"\\[1-9]", pattern):
        raise RegexValidationError("Lookarounds, special groups, and backreferences are not allowed")
    if re.search(r"\([^)]*[+*][^)]*\)[+*?]", pattern) or re.search(r"(?:\.\*|\.\+){2,}", pattern):
        raise RegexValidationError("Potentially unsafe nested repetition is not allowed")
    try:
        return re.compile(pattern, re.I)
    except re.error as problem:
        raise RegexValidationError("Regular expression is invalid") from problem


class RegexRuleStore:
    def __init__(self, path: Path):
        self.path, self.backup_path, self.lock = path, path.with_name("intent_rules.previous.json"), RLock()
        self._payload = self._load()

    def _load(self):
        if not self.path.exists():
            return {"revision": 1, "rules": [dict(item) for item in DEFAULT_RULES]}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            revision, raw = payload["revision"], payload["rules"]
            if not isinstance(revision, int) or revision < 1 or not isinstance(raw, list):
                raise ValueError
            rules = [RegexRuleInput.model_validate(item).model_dump(mode="json") for item in raw]
            self._validate_rules(rules)
            return {"revision": revision, "rules": rules}
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as problem:
            raise RegexValidationError("Saved intent rules are invalid") from problem

    @staticmethod
    def _validate_rules(rules):
        if not 1 <= len(rules) <= 100 or len({item["rule_id"] for item in rules}) != len(rules):
            raise RegexValidationError("Intent rules must contain 1 to 100 unique IDs")
        for item in rules:
            validate_pattern(item["pattern"])

    def status(self):
        with self.lock:
            return {"revision": self._payload["revision"], "rules": [dict(item) for item in self._ordered()],
                    "protected_regexes_editable": False, "recovery_snapshot": self.backup_path.name}

    def _ordered(self):
        return sorted(self._payload["rules"], key=lambda item: (item["priority"], item["rule_id"]))

    def compiled_rules(self):
        with self.lock:
            return [(dict(item), validate_pattern(item["pattern"])) for item in self._ordered() if item["enabled"]]

    def preview(self, phrase, language):
        for item, expression in self.compiled_rules():
            if item["language"] in {"all", language} and expression.search(phrase.strip()):
                return {"matched": True, "rule_id": item["rule_id"], "name": item["name"], "intent": item["intent"]}
        return {"matched": False, "rule_id": None, "name": None, "intent": Intent.OUT_OF_SCOPE.value}

    def change(self, change: RegexChange):
        with self.lock:
            if change.revision != self._payload["revision"]:
                raise RegexConflict("Intent rules changed; refresh and try again")
            rule = change.rule.model_dump(mode="json")
            rules = [dict(item) for item in self._payload["rules"]]
            index = next((i for i, item in enumerate(rules) if item["rule_id"] == rule["rule_id"]), None)
            if change.operation == "create":
                if index is not None: raise RegexValidationError("Rule ID already exists")
                rules.append(rule)
            elif change.operation == "update":
                if index is None: raise RegexValidationError("Intent rule was not found")
                rules[index] = rule
            else:
                if index is None: raise RegexValidationError("Intent rule was not found")
                rules.pop(index)
            self._validate_rules(rules)
            updated = {"revision": self._payload["revision"] + 1, "rules": rules}
            if self.path.exists():
                self.backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.path, self.backup_path)
            atomic_json(self.path, updated)
            self._payload = updated
            return self.status()
