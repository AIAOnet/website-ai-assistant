"""Deterministic output gates and optional exact-sentence support checking."""
import re
import unicodedata
from dataclasses import dataclass

from .grounding import GroundedAnswer


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    reasons: tuple[str, ...]
    sources: list[dict[str, str]]


@dataclass(frozen=True)
class CheckedAnswer:
    result: GroundedAnswer
    used_fallback: bool
    reasons: tuple[str, ...]


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split()).strip(" .!?;:\"“”")


class GroundingValidator:
    # Output contract permits source-ID citations only, never model-generated links.
    CITATIONS = re.compile(r"(?:\[[A-Za-z0-9_-]+\]\s*)+")
    SOURCE_ID = re.compile(r"\[([A-Za-z0-9_-]+)\]")
    UNSAFE_FORMAT = re.compile(r"[<>@]|(?:https?://|www\.|mailto:|tel:|javascript:)|\]\s*\(", re.I)
    ACTION = re.compile(
        r"\b(contact\w*|representative\w*|book\w*|appoint\w*|meeting\w*|schedul\w*|confirm\w*|"
        r"calendar\w*|email\w*|sent|send|install\w*|must|should|recommend\w*|"
        r"kontakt\w*|representant\w*|bok\w*|möte\w*|mötes\w*|bekräft\w*|kalender\w*|"
        r"skicka\w*|mejl\w*|bör|måste|rekommender\w*)\b", re.I
    )

    @classmethod
    def validate(cls, answer, evidence, contact_names=(), *, require_extractive=True) -> ValidationResult:
        """Evidence must be the exact registry-checked payload sent to the model.

        Each claim must immediately precede source-ID citation(s) and appear as
        a complete normalized sentence (or full text) in a cited record.
        This intentionally rejects unsupported numbers, negation, recombination,
        uncited text, and paraphrases rather than guessing semantic equivalence.
        With require_extractive=False, success means only the deterministic
        gates passed. A separate semantic check is mandatory before publication.
        """
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 12000:
            return ValidationResult(False, ("invalid_answer",), [])
        if cls.UNSAFE_FORMAT.search(answer):
            return ValidationResult(False, ("unsafe_format",), [])
        if cls.ACTION.search(answer) or any(normalize(name) in normalize(answer) for name in contact_names if name):
            return ValidationResult(False, ("tool_or_safety_claim",), [])
        registry = {r["source_id"]: r for r in evidence}
        reasons, used, cursor = [], [], 0
        for match in cls.CITATIONS.finditer(answer):
            claim = normalize(answer[cursor:match.start()])
            ids = cls.SOURCE_ID.findall(match.group())
            if any(identifier not in registry for identifier in ids):
                reasons.append("unknown_citation")
            elif not claim:
                reasons.append("missing_claim")
            elif not require_extractive and any(
                number not in set(re.findall(r"\d+(?:[.,]\d+)?", " ".join(registry[i]["content"] for i in ids)))
                for number in re.findall(r"\d+(?:[.,]\d+)?", claim)
            ):
                reasons.append("unsupported_number")
            elif require_extractive and not any(
                claim in {
                    normalize(registry[identifier]["content"]),
                    *(normalize(sentence) for sentence in re.split(r"(?<=[.!?])\s+", registry[identifier]["content"])),
                }
                for identifier in ids
            ):
                reasons.append("unsupported_claim")
            else:
                used.extend(ids)
            cursor = match.end()
        if not used or normalize(answer[cursor:]):
            reasons.append("missing_citation")
        if reasons:
            return ValidationResult(False, tuple(dict.fromkeys(reasons)), [])
        sources, seen = [], set()
        for identifier in used:
            record = registry[identifier]
            if record["canonical_url"] not in seen:
                sources.append({"title": record["title"], "url": record["canonical_url"], "category": record["category"]})
                seen.add(record["canonical_url"])
        return ValidationResult(True, (), sources)
