"""Bounded OpenAI-compatible chat transport; no redirect or ambient proxy forwarding."""
import asyncio
import json
import re
import urllib.request
from dataclasses import dataclass

MAX_RESPONSE_BYTES = 131072


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelResponse:
    content: str
    model: str


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("Provider redirects are disabled")


class OpenAICompatibleProvider:
    def __init__(self, endpoint, api_key, model, timeout=30):
        self.endpoint, self.api_key, self.model, self.timeout = endpoint, api_key, model, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    async def generate(self, messages):
        return await asyncio.wait_for(asyncio.to_thread(self._generate, messages), timeout=self.timeout)

    def _generate(self, messages):
        request = urllib.request.Request(self.endpoint,
            data=json.dumps({"model": self.model, "messages": messages, "temperature": 0,
                             "max_tokens": 800}).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ProviderError("Provider response exceeds the size limit")
            payload = json.loads(body)
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ProviderError("Provider returned an invalid answer")
            content = re.sub(r"<think\b[^>]*>.*?</think>\s*", "", content, flags=re.I | re.S).strip()
            if not content or len(content) > 12000:
                raise ProviderError("Provider returned an invalid answer")
            return ModelResponse(content, self.model)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            raise ProviderError("The model provider is unavailable") from error
