"""Server-only embedding-provider configuration and safe connectivity probe."""
from __future__ import annotations
from .configuration import setting

import json
import os
import urllib.error
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .knowledge import EmbeddingClient
from .provider_settings import RuntimeGenerationSettings
from .usage_budget import UsageBudgetExceeded


class RuntimeEmbeddingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    endpoint: str = Field(min_length=1, max_length=500)
    api_key: str = Field(min_length=1, max_length=1000)
    model: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    timeout_seconds: float = Field(gt=0, le=120)

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value):
        return RuntimeGenerationSettings.safe_endpoint(value)

    def client(self):
        return EmbeddingClient(self.endpoint, self.api_key, self.model, self.timeout_seconds)


class EmbeddingProviderChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    endpoint: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, min_length=1, max_length=1000)
    model: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    timeout_seconds: float = Field(gt=0, le=120)
    confirmed: Literal[True]

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value):
        return RuntimeGenerationSettings.safe_endpoint(value)


class EmbeddingConfigurationStore:
    def __init__(self, path: str | Path, usage_budget=None):
        self.path, self.lock, self.usage_budget = Path(path), Lock(), usage_budget
        self.client, self.source, self.warning = self._load()
        self.client.usage_budget = usage_budget

    @staticmethod
    def _environment():
        try:
            timeout = float(setting("WEBSITE_ASSISTANT_EMBEDDING_TIMEOUT_SECONDS", "15"))
        except ValueError:
            timeout = 15.0
        return EmbeddingClient(timeout_seconds=timeout if 0 < timeout <= 120 else 15.0)

    def _load(self):
        environment = self._environment()
        if not self.path.exists():
            return environment, "environment" if environment.configured else "not_configured", None
        try:
            runtime = RuntimeEmbeddingSettings.model_validate_json(self.path.read_text(encoding="utf-8"))
            return runtime.client(), "runtime_override", None
        except (OSError, ValueError, TypeError):
            return environment, "environment" if environment.configured else "not_configured", \
                "Invalid runtime embedding configuration; environment settings remain active."

    def public_status(self):
        parsed = urlsplit(self.client.endpoint) if self.client.endpoint else None
        return {"configured": self.client.configured, "source": self.source,
                "endpoint_host": parsed.hostname if parsed else None,
                "model": self.client.model or None, "timeout_seconds": self.client.timeout_seconds,
                "api_key_configured": bool(self.client.key), "warning": self.warning}

    def save(self, change: EmbeddingProviderChange, rag):
        with self.lock:
            key = change.api_key if change.api_key is not None else self.client.key
            runtime = RuntimeEmbeddingSettings(endpoint=change.endpoint, api_key=key,
                model=change.model, timeout_seconds=change.timeout_seconds)
            client = runtime.client()
            client.usage_budget = self.usage_budget
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(runtime.model_dump(), indent=2) + "\n", encoding="utf-8")
                try: temporary.chmod(0o600)
                except OSError: pass
                temporary.replace(self.path)
            finally:
                try: temporary.unlink(missing_ok=True)
                except OSError: pass
            with rag.lock:
                rag.tools.knowledge.embedding_client = client
            self.client, self.source, self.warning = client, "runtime_override", None
            return self.public_status()


class EmbeddingProviderProbe:
    def __init__(self, store):
        self.store, self.lock = store, Lock()

    def run(self):
        if not self.lock.acquire(blocking=False):
            return {"configured": self.store.client.configured, "outcome": "busy", "duration_ms": 0}
        started = perf_counter()
        try:
            if not self.store.client.configured: outcome = "not_configured"
            else:
                try:
                    vectors = self.store.client.embed(["connection check"])
                    outcome = "available" if len(vectors) == 1 and bool(vectors[0]) else "provider_error"
                except UsageBudgetExceeded: outcome = "usage_budget_exhausted"
                except urllib.error.HTTPError as error:
                    outcome = "unauthorized" if error.code in {401, 403} else "rate_limited" if error.code == 429 else "timeout" if error.code in {408, 504} else "upstream_gateway" if error.code in {502, 503} else "request_rejected"
                except (TimeoutError, urllib.error.URLError) as error:
                    outcome = "timeout" if isinstance(getattr(error, "reason", None), TimeoutError) else "network_unavailable"
                except Exception: outcome = "provider_error"
            return {"configured": outcome != "not_configured", "outcome": outcome,
                    "duration_ms": round((perf_counter() - started) * 1000)}
        finally:
            self.lock.release()
