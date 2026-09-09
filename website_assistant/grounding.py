"""Shared grounded answer type used by the adapted output validator."""
from dataclasses import dataclass


@dataclass(frozen=True)
class GroundedAnswer:
    answer: str
    sources: list[dict[str, str]]
    status: str
