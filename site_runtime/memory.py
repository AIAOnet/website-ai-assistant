from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ConversationContext:
    topic: str | None
    categories: tuple[str, ...]
    intent: str
    language: str


class ConversationStore:
    def __init__(self, max_conversations: int = 1000, ttl_seconds: int = 3600,
                 max_turns: int = 4) -> None:
        self.max_conversations = max(1, max_conversations)
        self.ttl_seconds = max(1, ttl_seconds)
        self.max_turns = min(4, max(1, max_turns))
        self._items: dict[str, tuple[float, tuple[ConversationContext, ...]]] = {}
        self._lock = threading.Lock()

    def context(self, key: str) -> ConversationContext | None:
        contexts = self.contexts(key)
        return contexts[-1] if contexts else None

    def contexts(self, key: str) -> tuple[ConversationContext, ...]:
        with self._lock:
            self._expire()
            item = self._items.get(key)
            if item:
                self._items[key] = (time.time(), item[1])
            return item[1] if item else ()

    def remember_context(self, key: str, context: ConversationContext) -> None:
        with self._lock:
            self._expire()
            if key not in self._items and len(self._items) >= self.max_conversations:
                oldest = min(self._items, key=lambda item: self._items[item][0])
                self._items.pop(oldest, None)
            history = self._items.get(key, (0, ()))[1]
            if not history or history[-1] != context:
                history = (*history, context)[-self.max_turns:]
            self._items[key] = (time.time(), history)

    def clear(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def size(self) -> int:
        with self._lock:
            self._expire()
            return len(self._items)

    def _expire(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        for key in [key for key, (updated, _) in self._items.items() if updated < cutoff]:
            self._items.pop(key, None)
