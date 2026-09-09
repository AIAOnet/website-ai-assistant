"""Bounded, content-free connectivity check for the configured model provider."""
from __future__ import annotations

import asyncio
import urllib.error
from time import perf_counter

from .models import ProviderError
from .usage_budget import UsageBudgetExceeded


class ProviderProbeBusy(RuntimeError):
    """Raised when a provider probe is already active."""


class ProviderProbe:
    MESSAGES = [
        {"role": "system", "content": "Connection check. Return only OK."},
        {"role": "user", "content": "OK"},
    ]

    def __init__(self, service, *, timeout_seconds: float = 20.0) -> None:
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("Provider probe timeout must be between 1 and 30 seconds")
        self.service = service
        self.timeout_seconds = timeout_seconds
        self._lock = asyncio.Lock()

    def status(self) -> dict:
        return {"configured": self.service.provider is not None}

    async def run(self) -> dict:
        if self._lock.locked():
            raise ProviderProbeBusy("A provider test is already running")
        started = perf_counter()
        if self.service.provider is None:
            return self._result("not_configured", started)
        async with self._lock:
            try:
                await asyncio.wait_for(
                    self.service.generate_grounded_answer(self.MESSAGES), self.timeout_seconds
                )
                outcome = "available"
            except asyncio.TimeoutError:
                outcome = "timeout"
            except UsageBudgetExceeded:
                outcome = "usage_budget_exhausted"
            except ProviderError as error:
                outcome = self._classify(error)
            except Exception:
                outcome = "provider_error"
        return self._result(outcome, started)

    @staticmethod
    def _result(outcome: str, started: float) -> dict:
        return {
            "configured": outcome != "not_configured",
            "outcome": outcome,
            "duration_ms": round((perf_counter() - started) * 1000),
        }

    @staticmethod
    def _classify(error: ProviderError) -> str:
        cause = error.__cause__
        status = getattr(cause, "code", None)
        if status in {401, 403}:
            return "unauthorized"
        if status == 429:
            return "rate_limited"
        if status in {408, 504}:
            return "timeout"
        if status in {502, 503}:
            return "upstream_gateway"
        if isinstance(cause, urllib.error.HTTPError):
            return "request_rejected"
        if isinstance(cause, (TimeoutError, asyncio.TimeoutError)):
            return "timeout"
        if isinstance(cause, OSError):
            return "network_unavailable"
        return "provider_error"
