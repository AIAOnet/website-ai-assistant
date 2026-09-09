"""Server-only generation-provider configuration with sanitized admin visibility."""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .service import AssistantConfiguration


class RuntimeGenerationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    endpoint: str = Field(min_length=1, max_length=500)
    api_key: str = Field(min_length=1, max_length=1000)
    model: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    timeout_seconds: float = Field(gt=0, le=120)

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value):
        parsed = urlsplit(value)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)) or not parsed.hostname:
            raise ValueError("Provider endpoint must use HTTPS or loopback HTTP")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Provider endpoint cannot contain credentials or a fragment")
        return value

    def configuration(self):
        return AssistantConfiguration(self.endpoint, self.api_key, self.model, self.timeout_seconds)


class GenerationProviderChange(BaseModel):
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


class ProviderConfigurationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = Lock()
        self.configuration, self.source, self.warning = self._load()

    def _load(self):
        environment = AssistantConfiguration.from_environment()
        if not self.path.exists():
            return environment, "environment" if any((environment.endpoint, environment.api_key,
                                                        environment.model)) else "not_configured", None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            runtime = RuntimeGenerationSettings.model_validate(payload)
            return runtime.configuration(), "runtime_override", None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return environment, "environment" if any((environment.endpoint, environment.api_key,
                                                        environment.model)) else "not_configured", \
                   "Invalid runtime model configuration; environment settings remain active."

    def public_status(self):
        parsed = urlsplit(self.configuration.endpoint) if self.configuration.endpoint else None
        return {
            "configured": self.configuration.configured,
            "source": self.source,
            "endpoint_host": parsed.hostname if parsed else None,
            "model": self.configuration.model or None,
            "timeout_seconds": self.configuration.timeout_seconds,
            "api_key_configured": bool(self.configuration.api_key),
            "warning": self.warning,
        }

    def save(self, change: GenerationProviderChange, service):
        with self.lock:
            return self._save(change, service)

    def _save(self, change: GenerationProviderChange, service):
        api_key = change.api_key if change.api_key is not None else self.configuration.api_key
        runtime = RuntimeGenerationSettings(endpoint=change.endpoint, api_key=api_key,
            model=change.model, timeout_seconds=change.timeout_seconds)
        configuration = runtime.configuration()
        service.build_provider(configuration)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(runtime.model_dump(), indent=2) + "\n", encoding="utf-8")
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        service.replace_configuration(configuration)
        self.configuration, self.source, self.warning = configuration, "runtime_override", None
        return self.public_status()
