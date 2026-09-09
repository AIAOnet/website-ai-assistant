from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelResponse:
    content: str
    model: str


class OpenAICompatibleProvider:
    """Optional phrasing provider. Deterministic tools remain the source of truth."""

    def __init__(self, endpoint: str, api_key: str, model: str, timeout: float = 30) -> None:
        self.endpoint, self.api_key, self.model, self.timeout = endpoint, api_key, model, timeout

    async def generate(self, messages: list[dict[str, str]]) -> ModelResponse:
        return await asyncio.to_thread(self._generate, messages)

    def _generate(self, messages: list[dict[str, str]]) -> ModelResponse:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps({"model": self.model, "messages": messages, "temperature": 0}).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
            content = payload["choices"][0]["message"]["content"]
        except (OSError, KeyError, IndexError, TypeError, json.JSONDecodeError, urllib.error.HTTPError) as error:
            raise ProviderError("The optional model provider is unavailable") from error
        content = re.sub(r"<think\b[^>]*>.*?</think>\s*", "", content, flags=re.I | re.S).strip()
        if not content:
            raise ProviderError("The optional model provider returned no answer")
        return ModelResponse(content, payload.get("model", self.model))

