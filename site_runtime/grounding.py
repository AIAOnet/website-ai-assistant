from __future__ import annotations

from dataclasses import dataclass
import re
from .knowledge import passage_score, sentences
from .language import language_mismatch


@dataclass(frozen=True)
class GroundedAnswer:
    answer: str
    sources: list[dict[str, str]]
    status: str


def grounded_summary(records: list[dict], language: str, safety_critical: bool = False, query: str = "") -> GroundedAnswer:
    # Reject the whole mismatched passage rather than removing a translated
    # exception or qualification while leaving its preceding claim intact.
    records = [record for record in records if not language_mismatch(record['content'], language)]
    if not records:
        answer = (
            "Jag hittar inte den informationen i de godkända källorna."
            if language == "sv"
            else "I cannot find that information in the approved sources."
        )
        return GroundedAnswer(answer, [], "UNAVAILABLE")
    candidates = []
    for position, record in enumerate(records[:2]):
        for order, sentence in enumerate(sentences(record["content"])):
            sentence = sentence.strip()
            if sentence:
                score = passage_score(query, sentence) if query else 0
                candidates.append((score, position, order, sentence))
    # Prefer sentences over short navigation labels or headings, but retain
    # heading-only evidence when that is all the approved source contains.
    all_candidates = candidates
    substantive = [item for item in candidates if len(item[3].split()) >= 5
                 or item[3].endswith(('.', '!', '?'))]
    candidates = substantive or candidates
    # Keep the selected statement with its neighboring qualifications instead
    # of assembling disconnected sentences from different parts of a page.
    if candidates:
        anchor = min(candidates, key=lambda item: (item[1], -item[0], item[2]))
        start = anchor[2]
        previous = next((item for item in all_candidates
                         if item[1] == anchor[1] and item[2] == start - 1), None)
        if previous and previous not in substantive and previous[0] > 0:
            start -= 1  # Keep the source heading that identifies this statement.
        if re.match(r'(?i)(?:however|except|unless|but|this|these|they|it|dock|utom|men|detta|dessa)\b', anchor[3]):
            start = max(0, start - 1)
        window = []
        for item in all_candidates:
            if item[1] != anchor[1] or not start <= item[2] <= start + 2:
                continue
            if item[2] > anchor[2] and item not in substantive:
                break  # Do not cross into another section's heading.
            window.append(item)
        candidates = window or [anchor]
    selected, seen, size = [], set(), 0
    for item in sorted(candidates, key=lambda item: (item[1], -item[0], item[2])):
        sentence = item[3]
        if sentence.casefold() in seen or (selected and size + len(sentence) > 900):
            continue
        selected.append(item); seen.add(sentence.casefold()); size += len(sentence)
        if len(selected) == 3:
            break
    selected.sort(key=lambda item: (item[1], item[2]))
    text = " ".join(item[3] for item in selected)
    used = {item[1] for item in selected}
    if safety_critical:
        text += (
            " Detta är endast allmän information och inte ett slutligt professionellt beslut."
            if language == "sv"
            else " This is general information only, not a final professional decision."
        )
    sources = [{"title": record["title"], "url": record["canonical_url"],
                "category": record["category"],
                **({"location": record["source_location"]} if record.get("source_location") else {})}
               for position, record in enumerate(records[:2]) if position in used]
    return GroundedAnswer(text, sources, "GROUNDED")
