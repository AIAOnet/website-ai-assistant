from __future__ import annotations

import json, re
from pathlib import Path
from .knowledge import KnowledgeStore
from .ontology_retrieval import search_ontology
from .diagnostics import record_retrieval

NEED_ALIASES = {}

class ToolValidationError(ValueError): pass

class AssistantTools:
    def __init__(self, data_path: str | Path) -> None:
        from .bootstrap import initialize, selected_directory
        initialize(data_path)
        self.root = selected_directory(data_path); self.knowledge = KnowledgeStore(self.root / "approved_sources.json")
        self.contacts = json.loads((self.root / "contacts.json").read_text(encoding="utf-8"))
        self.ontology = json.loads((self.root / "ontology.json").read_text(encoding="utf-8"))
        allowed = self.knowledge.approved_urls
        for contact in self.contacts:
            if contact["official_source_url"] not in allowed: raise ToolValidationError("Contact source is not in the approved registry")

    def search_knowledge(self, query: str, language: str, category: str | None = None) -> dict:
        knowledge, ontology = self.knowledge, self.ontology
        graph = search_ontology(ontology, knowledge, self.contacts, query, language, category,
                                knowledge.settings.ontology_depth)
        details = knowledge.search_details(query[:500], language, category,
                                           ontology_source_scores=graph["source_scores"])
        record_retrieval(details, graph)
        return {"tool": "search_knowledge", **details, "ontology": graph}

    def search_ontology(self, query: str, language: str = "sv", category: str | None = None,
                        depth: int | None = None) -> dict:
        return search_ontology(self.ontology, self.knowledge, self.contacts, query, language, category,
                               self.knowledge.settings.ontology_depth if depth is None else depth)

    @staticmethod
    def classify_need(text: str) -> set[str]:
        lowered = text.lower()
        return {category for category, aliases in NEED_ALIASES.items() if any(alias in lowered for alias in aliases)}

    def find_contact(self, need: str, region: str | None = None, language: str = "sv") -> dict:
        if not need.strip(): raise ToolValidationError("Need is required")
        categories = self.classify_need(need); region_key = (region or "sweden").strip().lower()
        matches = []
        for contact in self.contacts:
            if not contact.get("active"): continue
            supported = set(contact["product_categories"] + contact["service_categories"])
            category_score = len(categories & supported)
            region_match = region_key in contact["geographic_regions"] or "sweden" in contact["geographic_regions"]
            if category_score and region_match: matches.append((category_score, int(contact.get("priority", 0)), contact))
        matches.sort(key=lambda item: (-item[0], -item[1], item[2]["contact_id"]))
        if not matches: return {"tool":"find_contact", "contact":None, "alternative":None, "ambiguous":False, "reason":"no_exact_route", "matched_categories":sorted(categories)}
        best = matches[0]; tied = [item for item in matches if item[:2] == best[:2]]; alternative = tied[1][2] if len(tied) > 1 else None
        def public(item: dict | None) -> dict | None:
            if not item: return None
            return {key:item[key] for key in ("contact_id","name","job_title","company","areas_of_responsibility","public_business_email","public_business_telephone","official_source_url","last_verified_date")}
        return {"tool":"find_contact", "contact":public(best[2]), "alternative":public(alternative), "ambiguous":len(tied)>1, "matched_categories":sorted(categories), "reason":"matched responsibility and region"}

    def approved_link(self, url: str) -> bool:
        return url in self.knowledge.approved_urls
