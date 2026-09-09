"""Registry-checked evidence and exact-sentence validation before publishing drafts."""
import asyncio
from urllib.parse import urlsplit

from .models import OpenAICompatibleProvider, ProviderError
from .prompts import build_grounded_messages, EvidenceError
from .validation import GroundingValidator
from .routing import RoutingStore


class AssistantService:
    def __init__(self, knowledge, settings, provider=None):
        self.knowledge, self.settings = knowledge, settings
        self.routing = RoutingStore(settings.data_path / "routing.db")
        configured = bool(settings.ai_api_endpoint and settings.ai_api_key.get_secret_value() and settings.ai_model)
        self.provider = provider if provider is not None else (OpenAICompatibleProvider(
            settings.ai_api_endpoint, settings.ai_api_key.get_secret_value(), settings.ai_model,
            settings.ai_timeout_seconds) if configured else None)

    def status(self):
        return {"configured": self.provider is not None,
                "endpoint_host": urlsplit(self.settings.ai_api_endpoint).hostname,
                "model": self.settings.ai_model, "timeout_seconds": self.settings.ai_timeout_seconds,
                "key_present": bool(self.settings.ai_api_key.get_secret_value()),
                "answer_mode": "checked_extractive"}

    async def probe(self):
        if self.provider is None:
            return {"available": False, "reason": "not_configured"}
        try:
            result = await asyncio.wait_for(self.provider.generate([
                {"role": "system", "content": "Reply with exactly OK."},
                {"role": "user", "content": "Connection test."}]), self.settings.ai_timeout_seconds)
            return {"available": result.content.strip() == "OK", "reason":
                    "ok" if result.content.strip() == "OK" else "unexpected_response"}
        except (ProviderError, TimeoutError):
            return {"available": False, "reason": "provider_unavailable"}

    async def respond(self, message, language, *, knowledge=None):
        knowledge = self.knowledge if knowledge is None else knowledge
        base = {"sources": [], "contact": None, "appointment_available": False,
                "generation": "extractive", "grounding": "UNAVAILABLE", "fallback_reasons": []}
        if not knowledge.records:
            return {**base, "grounding": "NOT_BUILT", "answer": (
                "Webbplatsens kunskap har inte byggts ännu." if language == "sv"
                else "Website knowledge has not been built yet.")}
        route = self.routing.preview(message, language)
        base["intent"] = route["intent"]
        if route["intent"] == "action_unavailable":
            return {**base, "generation": "deterministic", "answer": (
                "Kontakt- och bokningsfunktioner är inte konfigurerade." if language == "sv"
                else "Contact and booking actions are not configured.")}
        if route["intent"] == "out_of_scope":
            return {**base, "generation": "deterministic", "answer": (
                "Den frågan ligger utanför assistentens konfigurerade ämnen." if language == "sv"
                else "That question is outside the assistant's configured topics.")}
        category = route["intent"] if route["intent"] in {"product", "service", "company"} else None
        records = knowledge.search(message, language, category=category, limit=2)
        if not records:
            return {**base, "answer": "Jag hittar inte den informationen i webbplatsens källor."
                    if language == "sv" else "I cannot find that information in the website sources."}
        fallback = {**base, "grounding": "GROUNDED", "answer": "\n\n".join(r["content"] for r in records),
                    "sources": [{"title": r["title"], "url": r["canonical_url"]} for r in records]}
        if self.provider is None:
            return {**fallback, "fallback_reasons": ["provider_not_configured"]}
        try:
            messages = build_grounded_messages(message, language, records, knowledge.records)
        except EvidenceError:
            # Never publish even an extractive fallback from a mismatched registry.
            return {**base, "answer": "Källorna kunde inte verifieras." if language == "sv"
                    else "The source evidence could not be verified.", "fallback_reasons": ["invalid_evidence"]}
        try:
            result = await asyncio.wait_for(self.provider.generate(messages), self.settings.ai_timeout_seconds)
        except (ProviderError, TimeoutError):
            return {**fallback, "fallback_reasons": ["provider_unavailable"]}
        checked = GroundingValidator.validate(result.content, records)
        if not checked.accepted:
            return {**fallback, "fallback_reasons": list(checked.reasons)}
        return {**base, "answer": result.content, "sources": checked.sources,
                "grounding": "CHECKED_EXTRACTIVE", "generation": "llm"}
