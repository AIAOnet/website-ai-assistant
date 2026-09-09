"""Local privacy-safe health thresholds and bounded alert transitions."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .rag_settings import atomic_json


class MonitoringThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fallback_warning_percent: int = Field(default=30, ge=1, le=100)
    fallback_critical_percent: int = Field(default=60, ge=1, le=100)
    provider_failure_warning: int = Field(default=3, ge=1, le=10000)
    provider_failure_critical: int = Field(default=10, ge=1, le=10000)
    api_error_warning: int = Field(default=10, ge=1, le=100000)
    api_error_critical: int = Field(default=30, ge=1, le=100000)
    rate_limit_warning: int = Field(default=10, ge=1, le=100000)
    rate_limit_critical: int = Field(default=30, ge=1, le=100000)


class MonitoringChange(MonitoringThresholds):
    confirmed: Literal[True]


class MonitoringAdmin:
    CAPACITY = 100

    def __init__(self, settings_path: Path, alerts_path: Path):
        self.settings_path, self.alerts_path, self.lock = settings_path, alerts_path, RLock()
        self.thresholds = self._load_settings()
        self.alerts = self._load_alerts()

    def _load_settings(self):
        if not self.settings_path.exists():
            return MonitoringThresholds()
        return MonitoringThresholds.model_validate_json(self.settings_path.read_text(encoding="utf-8"))

    def _load_alerts(self):
        if not self.alerts_path.exists():
            return {"last_status": "healthy", "events": []}
        payload = json.loads(self.alerts_path.read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or set(payload) != {"last_status", "events"}
                or payload["last_status"] not in {"healthy", "warning", "critical"}
                or not isinstance(payload["events"], list) or len(payload["events"]) > self.CAPACITY):
            raise ValueError("Monitoring alert history is invalid")
        return payload

    @staticmethod
    def _snapshot(diagnostics, traffic):
        # The store itself retains at most 500 events. Read that bounded set without
        # relying on the optional kind index, then select chat diagnostics in memory.
        # This keeps monitoring available if a persisted SQLite index is damaged while
        # the underlying diagnostic rows remain readable.
        report = diagnostics.read("all", 500)
        events = [item for item in report["events"]
                  if item.get("kind") == "chat"]
        chat_count = len(events)
        fallback_count = sum(item.get("generation") == "fallback" for item in events)
        provider_failures = sum(call.get("outcome") != "ok" for item in events
                                for call in item.get("model_calls", []))
        routes = traffic["routes"]
        return {"diagnostic_storage_failed": not report.get("storage_healthy", True),
                "chat_events": chat_count, "fallback_percent": round(
                    fallback_count * 100 / chat_count) if chat_count else 0,
                "provider_failures": provider_failures,
                "api_errors": sum(item["failed"] for item in routes.values()),
                "rate_limited": sum(item["limited"] for item in routes.values())}

    def status(self, diagnostics, traffic):
        with self.lock:
            values, rules = self._snapshot(diagnostics, traffic), self.thresholds
            critical, warning = [], []
            if values["diagnostic_storage_failed"]:
                critical.append("diagnostic_storage_failed")
            checks = (("fallback_percent", rules.fallback_warning_percent, rules.fallback_critical_percent),
                      ("provider_failures", rules.provider_failure_warning, rules.provider_failure_critical),
                      ("api_errors", rules.api_error_warning, rules.api_error_critical),
                      ("rate_limited", rules.rate_limit_warning, rules.rate_limit_critical))
            for name, warn, severe in checks:
                if values[name] >= severe: critical.append(name)
                elif values[name] >= warn: warning.append(name)
            health = "critical" if critical else "warning" if warning else "healthy"
            reasons = critical + warning
            if health != self.alerts["last_status"]:
                event = {"alert_id": uuid4().hex, "timestamp": datetime.now(timezone.utc).isoformat(),
                         "status": health, "reasons": reasons, "metrics": values}
                self.alerts = {"last_status": health,
                               "events": ([event] + self.alerts["events"])[:self.CAPACITY]}
                atomic_json(self.alerts_path, self.alerts)
            return {"status": health, "reasons": reasons, "metrics": values,
                    "thresholds": rules.model_dump(), "alerts": list(self.alerts["events"]),
                    "capacity": self.CAPACITY, "external_delivery": False,
                    "privacy": "aggregate_only"}

    def save(self, change: MonitoringChange):
        thresholds = MonitoringThresholds.model_validate(change.model_dump(exclude={"confirmed"}))
        if (thresholds.fallback_warning_percent >= thresholds.fallback_critical_percent
                or thresholds.provider_failure_warning >= thresholds.provider_failure_critical
                or thresholds.api_error_warning >= thresholds.api_error_critical
                or thresholds.rate_limit_warning >= thresholds.rate_limit_critical):
            raise ValueError("Every warning threshold must be lower than its critical threshold")
        with self.lock:
            atomic_json(self.settings_path, thresholds.model_dump())
            self.thresholds = thresholds
