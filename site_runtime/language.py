"""Conservative English/Swedish mismatch checks, without translating evidence."""
import re

MARKERS = {
    'en': frozenset('the and our with for this these is are we of from your which must not provides knowledge available'.split()),
    'sv': frozenset('och för våra med som är vi att av från vår har inte det detta dessa kan måste kunskap användningsfall tillgänglig teknisk'.split()),
}


def language_mismatch(text, language):
    """Reject clear opposite-language sentences; leave ambiguous short text alone."""
    if language not in MARKERS:
        return False
    # Citation IDs, URLs and identifiers are not natural-language evidence.
    clean = re.sub(r'\[[^\]]*\]|https?://\S+|\b\w*[_-]\w*\b', ' ', text)
    other = 'sv' if language == 'en' else 'en'
    for sentence in re.split(r'(?<=[.!?])\s+|\n+', clean):
        words = set(re.findall(r'[^\W\d_]+', sentence.casefold()))
        expected = len(words & MARKERS[language])
        opposite = len(words & MARKERS[other])
        if opposite >= 2 and opposite >= expected + 2:
            return True
    return False
