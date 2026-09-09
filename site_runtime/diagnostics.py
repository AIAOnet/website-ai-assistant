"""Bounded, persistent diagnostics. Never retain prompts, answers or secrets."""
import json
import logging
import sqlite3
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import perf_counter
from uuid import uuid4
from .database_maintenance import Migration, apply_migrations, open_database, require_columns


current_trace = ContextVar("diagnostic_trace", default=None)
log = logging.getLogger(__name__)


def append_safely(store, event):
    """Diagnostics must never replace an already determined application result."""
    try:
        store.append(event)
    except sqlite3.Error:
        log.error("Diagnostic storage unavailable; event was not recorded")

REASONS = frozenset({"invalid_evidence", "no_evidence", "safety_sensitive_question",
    "provider_unavailable", "semantic_check_failed", "unknown_citation", "missing_claim",
    "unsupported_number", "unsupported_claim", "missing_citation", "invalid_answer",
    "unsafe_format", "tool_or_safety_claim"})
ADMIN_ACTIONS = {
    ("POST", "/api/admin/api-access"): "api_access.update",
    ("PUT", "/api/admin/website"): "website.save",
    ("PUT", "/api/admin/rag/settings"): "rag.settings.save",
    ("POST", "/api/admin/rag/rebuild"): "rag.rebuild",
    ("POST", "/api/admin/provider/probe"): "provider.probe",
    ("GET", "/api/admin/embeddings/rebuild-preview"): "embeddings.rebuild.preview",
    ("POST", "/api/admin/embeddings/rebuild"): "embeddings.rebuild",
    ("PUT", "/api/admin/provider/settings"): "provider.settings.update",
    ("PUT", "/api/admin/embeddings/settings"): "embeddings.settings.update",
    ("POST", "/api/admin/embeddings/probe"): "embeddings.probe",
    ("POST", "/api/admin/sources/changes"): "sources.change",
    ("POST", "/api/admin/sources/import-preview"): "sources.import.preview",
    ("POST", "/api/admin/sources/refresh-preview"): "sources.refresh.preview",
    ("POST", "/api/admin/sources/refresh-apply"): "sources.refresh.apply",
    ("POST", "/api/admin/evaluations/run"): "evaluation.run",
    ("POST", "/api/admin/evaluations/run-non-llm"): "evaluation.suite.run",
    ("POST", "/api/admin/evaluations/run-full"): "evaluation.live_suite.run",
    ("POST", "/api/admin/ontology/relationships"): "ontology.change",
    ("POST", "/api/admin/ontology/aliases"): "ontology.alias.change",
    ("GET", "/api/admin/security-audit"): "security.audit.view",
    ("GET", "/api/admin/traffic-metrics"): "traffic.metrics.view",
    ("PUT", "/api/admin/usage-budget"): "usage_budget.update",
    ("GET", "/api/admin/calendar"): "calendar.view",
    ("POST", "/api/admin/regex/rules"): "regex.rule.change",
    ("POST", "/api/admin/regex/preview"): "regex.preview",
    ("GET", "/api/admin/monitoring"): "monitoring.view",
    ("PUT", "/api/admin/monitoring"): "monitoring.thresholds.update",
}


def _diagnostic_schema_v1(database):
    database.execute("""CREATE TABLE IF NOT EXISTS diagnostic_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        timestamp TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('chat', 'admin')),
        payload TEXT NOT NULL
    )""")
    database.execute("CREATE INDEX IF NOT EXISTS diagnostic_kind_sequence "
                     "ON diagnostic_events(kind, sequence DESC)")


class DiagnosticStore:
    CAPACITY = 500

    def __init__(self, database_path: str | Path | None = None):
        self.path = Path(database_path) if database_path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        self.write_failed = False
        self.connection = open_database(
            str(self.path) if self.path is not None else ":memory:",
            check_same_thread=False, row_factory=sqlite3.Row)
        with self.connection:
            apply_migrations(self.connection, "diagnostics", (
                Migration(1, "create diagnostic events", _diagnostic_schema_v1),))
            require_columns(self.connection, "diagnostic_events",
                            {"sequence", "event_id", "timestamp", "kind", "payload"})

    def append(self, event):
        stored = {**deepcopy(event), "id": uuid4().hex,
                  "timestamp": datetime.now(timezone.utc).isoformat()}
        if stored.get("kind") not in {"chat", "admin"}:
            raise ValueError("Diagnostic event kind must be chat or admin")
        payload = json.dumps(stored, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        with self.lock:
            try:
                with self.connection:
                    self.connection.execute(
                        "INSERT INTO diagnostic_events(event_id, timestamp, kind, payload) VALUES(?, ?, ?, ?)",
                        (stored["id"], stored["timestamp"], stored["kind"], payload),
                    )
                    self.connection.execute(
                        "DELETE FROM diagnostic_events WHERE sequence NOT IN "
                        "(SELECT sequence FROM diagnostic_events ORDER BY sequence DESC LIMIT ?)",
                        (self.CAPACITY,),
                    )
                self.write_failed = False
            except sqlite3.Error:
                self.write_failed = True
                raise

    def read(self, kind="all", limit=100):
        with self.lock:
            where, parameters = ("", ()) if kind == "all" else (" WHERE kind=?", (kind,))
            matching = self.connection.execute(
                "SELECT COUNT(*) FROM diagnostic_events" + where, parameters
            ).fetchone()[0]
            retained = self.connection.execute(
                "SELECT COUNT(*) FROM diagnostic_events"
            ).fetchone()[0]
            rows = self.connection.execute(
                "SELECT payload FROM diagnostic_events" + where +
                " ORDER BY sequence DESC LIMIT ?", (*parameters, limit)
            ).fetchall()
            return {"events": [json.loads(row["payload"]) for row in rows],
                    "matching_events": matching, "retained_events": retained,
                    "capacity": self.CAPACITY, "persistent": self.path is not None,
                    "storage_healthy": not self.write_failed}

    def close(self):
        with self.lock:
            connection, self.connection = self.connection, None
            if connection is not None:
                connection.close()

    def __del__(self):
        connection = getattr(self, "connection", None)
        if connection is not None:
            connection.close()


def record_retrieval(details, graph):
    trace = current_trace.get()
    if trace is None:
        return
    trace["retrieval"] = {
        "source_ids": [r["source_id"] for r in details["records"]],
        "search_mode": details["search_mode"],
        "embedding_used": details["semantic_used"],
        "embedding_fallback": details["fallback_reason"],
        # Row numbers, not editable entity labels or source content.
        "ontology_rows": graph["relationship_rows"],
        "ontology_depth": graph["max_hops"],
        "ontology_paths": [{"depth": path["depth"], "relationship_rows": path["relationship_rows"]}
                           for path in graph["paths"]],
        "ontology_truncated": graph["truncated"],
        "ontology_match_count": len(graph["matched_entities"]),
        "ontology_source_ids": graph["source_ids"],
        "ontology_fallback": graph["fallback_reason"],
    }


def record_follow_up(resolved):
    trace = current_trace.get()
    if trace is not None:
        trace["follow_up_resolved"] = bool(resolved)


def record_intent_classification(mode, outcome):
    trace = current_trace.get()
    if trace is not None:
        trace["intent_classification"] = {
            "mode": mode if mode in {"deterministic", "schema_model", "disabled"} else "unknown",
            "outcome": outcome if outcome in {"matched", "out_of_scope", "invalid", "unavailable"} else "unknown",
        }


async def measured_generate(provider, messages):
    trace = current_trace.get()
    started, outcome = perf_counter(), "error"
    try:
        result = await provider.generate(messages)
        outcome = "ok"
        return result
    finally:
        if trace is not None:
            trace["model_calls"].append({"outcome": outcome,
                                         "duration_ms": round((perf_counter() - started) * 1000)})


async def traced_chat(store, service, conversation_id, message, language):
    trace = {"kind": "chat", "outcome": "error", "generation": "error",
             "model_calls": [], "retrieval": None, "fallback_reasons": [],
             "follow_up_resolved": False,
             "intent_classification": {"mode": "deterministic", "outcome": "out_of_scope"}}
    token = current_trace.set(trace)
    started = perf_counter()
    try:
        result = await service.respond_async(conversation_id, message, language)
        generation = result.get("generation")
        trace.update(outcome="ok", generation=generation if generation in {"llm", "fallback", "deterministic"} else "unknown",
                     fallback_reasons=[r if r in REASONS else "other_validation_failure"
                                       for r in result.get("fallback_reasons", [])])
        if generation == "fallback" and result.get("intent") == "OUT_OF_SCOPE":
            trace["fallback_reasons"] = ["out_of_scope"]
        return result
    finally:
        current_trace.reset(token)
        trace["duration_ms"] = round((perf_counter() - started) * 1000)
        append_safely(store, trace)
