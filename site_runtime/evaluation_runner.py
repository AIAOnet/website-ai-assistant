"""Read-only execution of one versioned assistant evaluation case."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from time import perf_counter
from typing import Callable
from uuid import uuid4

from .diagnostics import REASONS, current_trace
from .evaluation_cases import EvaluationCase, EvaluationSuite, load_evaluation_suite
from .router import Intent
from .service import AssistantService


GROUNDING_FAILURE_REASONS = frozenset({
    "semantic_check_failed", "unknown_citation", "missing_claim", "unsupported_number",
    "unsupported_claim", "missing_citation", "invalid_answer", "unsafe_format",
    "tool_or_safety_claim", "other_validation_failure",
})
CHECK_FAILURE_CODES = {
    "execution": "execution_error",
    "intent": "intent_mismatch",
    "generation": "generation_mismatch",
    "sources": "source_mismatch",
    "contact": "contact_mismatch",
    "required_phrase": "required_phrase_missing",
    "forbidden_phrase": "forbidden_phrase_present",
}


class EvaluationCaseNotFound(LookupError):
    """Raised when a requested case is missing or disabled."""


class EvaluationAlreadyRunning(RuntimeError):
    """Raised when the same case is already executing."""


class EvaluationSuiteChanged(RuntimeError):
    """Raised when the confirmed case count no longer matches the suite."""


class EvaluationRunner:
    """Execute one case without persistence, public APIs, or write-capable tools."""

    def __init__(
        self,
        service: AssistantService,
        suite: EvaluationSuite | str | Path,
        *,
        timeout_seconds: float = 65.0,
        conversation_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not 0.01 <= timeout_seconds <= 120:
            raise ValueError("Evaluation timeout must be between 0.01 and 120 seconds")
        self.service = service
        self.suite = load_evaluation_suite(suite) if isinstance(suite, (str, Path)) else suite
        self.timeout_seconds = timeout_seconds
        self._conversation_id_factory = conversation_id_factory or (lambda: f"evaluation-{uuid4().hex}")

    async def run_case(self, case_id: str) -> dict:
        case = next((item for item in self.suite.cases if item.case_id == case_id and item.enabled), None)
        if case is None:
            raise EvaluationCaseNotFound("The evaluation case is unavailable")

        conversation_id = self._conversation_id_factory()
        trace = {"model_calls": [], "retrieval": None, "follow_up_resolved": False}
        token = current_trace.set(trace)
        started = perf_counter()
        try:
            try:
                observed = await asyncio.wait_for(
                    self._execute(case, conversation_id), timeout=self.timeout_seconds
                )
                retrieval = trace.get("retrieval") or {}
                observed["source_ids"] = retrieval.get("source_ids", observed.get("source_ids", []))
                checks = self._checks(case, observed)
                fallback_codes = self._safe_fallback_codes(observed.get("fallback_reasons", []))
            except asyncio.TimeoutError:
                checks = [{"code": "execution", "passed": False}]
                fallback_codes = ["evaluation_timeout"]
                observed = {}
            except Exception:
                checks = [{"code": "execution", "passed": False}]
                fallback_codes = ["evaluation_error"]
                observed = {}
        finally:
            self.service.memory.clear(conversation_id)
            current_trace.reset(token)

        retrieval = trace.get("retrieval") or {}
        source_ids = retrieval.get("source_ids", observed.get("source_ids", []))
        passed = bool(checks) and all(check["passed"] for check in checks)
        return {
            "case_id": case.case_id,
            "suite_version": self.suite.version,
            "passed": passed,
            "checks": checks,
            "duration_ms": round((perf_counter() - started) * 1000),
            "model_call_count": len(trace["model_calls"]),
            "retrieved_source_ids": list(dict.fromkeys(source_ids))[:4],
            "fallback_codes": fallback_codes[:8],
            "failure_codes": [] if passed else self._failure_codes(checks, fallback_codes),
        }

    async def _execute(self, case: EvaluationCase, conversation_id: str) -> dict:
        intent = self.service.router.route(case.question)
        if case.execution_mode == "retrieval":
            category = {
                Intent.SERVICE_INFORMATION: "service",
                Intent.COMPANY_INFORMATION: "company",
            }.get(intent)
            retrieval = await asyncio.to_thread(
                self.service.tools.search_knowledge, case.question, case.language, category
            )
            return {
                "intent": intent.value,
                "source_ids": [record["source_id"] for record in retrieval["records"]],
            }

        request_service = self.service
        if case.execution_mode == "deterministic":
            request_service = copy.copy(self.service)
            request_service.provider = None
            request_service.intent_classifier_enabled = False
        response = await request_service.respond_async(conversation_id, case.question, case.language)
        contact = response.get("contact") or {}
        return {
            "intent": response.get("intent"),
            "generation": response.get("generation"),
            "answer": response.get("answer", ""),
            "contact_id": contact.get("contact_id"),
            "fallback_reasons": response.get("fallback_reasons", []),
        }

    @staticmethod
    def _checks(case: EvaluationCase, observed: dict) -> list[dict]:
        checks = [{
            "code": "intent",
            "passed": observed.get("intent") == case.expected_intent,
        }]
        if case.execution_mode != "retrieval":
            checks.append({
                "code": "generation",
                "passed": observed.get("generation") in case.expected_generation,
            })

        actual_sources = set(observed.get("source_ids", []))
        if case.execution_mode == "retrieval" or case.expected_source_ids:
            checks.append({
                "code": "sources",
                "passed": set(case.expected_source_ids) <= actual_sources,
            })
        if case.expected_contact_id is not None:
            checks.append({
                "code": "contact",
                "passed": observed.get("contact_id") == case.expected_contact_id,
            })

        answer = observed.get("answer", "").casefold()
        checks.extend({"code": "required_phrase", "passed": phrase.casefold() in answer}
                      for phrase in case.required_phrases)
        checks.extend({"code": "forbidden_phrase", "passed": phrase.casefold() not in answer}
                      for phrase in case.forbidden_phrases)
        return checks

    @staticmethod
    def _safe_fallback_codes(reasons: list) -> list[str]:
        allowed = set(REASONS) | {"out_of_scope"}
        return [reason if reason in allowed else "other_validation_failure"
                for reason in reasons if isinstance(reason, str)]

    @staticmethod
    def _failure_codes(checks: list[dict], fallback_codes: list[str]) -> list[str]:
        codes = []
        for fallback in fallback_codes:
            if fallback in {"provider_unavailable", "evaluation_timeout", "evaluation_error"}:
                codes.append(fallback)
            elif fallback in {"invalid_evidence", "no_evidence"}:
                codes.append("evidence_unavailable")
            elif fallback in GROUNDING_FAILURE_REASONS:
                codes.append("grounding_rejected")
        specific_generation_failure = bool(set(codes) & {
            "provider_unavailable", "evaluation_timeout", "evaluation_error",
            "evidence_unavailable", "grounding_rejected",
        })
        for check in checks:
            if check.get("passed") is False and check.get("code") in CHECK_FAILURE_CODES:
                code = CHECK_FAILURE_CODES[check["code"]]
                if code == "execution_error" and set(codes) & {
                    "evaluation_timeout", "evaluation_error"
                }:
                    continue
                if code != "generation_mismatch" or not specific_generation_failure:
                    codes.append(code)
        return list(dict.fromkeys(codes))[:8]


class EvaluationCoordinator:
    """Expose bounded metadata and serialize duplicate case executions."""

    def __init__(self, runner: EvaluationRunner, *, suite_timeout_seconds: float = 300.0, store=None) -> None:
        if not 1 <= suite_timeout_seconds <= 300:
            raise ValueError("Suite timeout must be between 1 and 300 seconds")
        self.store = store
        self.runner = runner
        self.suite_timeout_seconds = suite_timeout_seconds
        self._active: set[str] = set()
        self._suite_active = False
        self._lock = asyncio.Lock()

    def refresh_definitions(self):
        if self.store is not None and not self._active and not self._suite_active:
            self.runner.suite = self.store.load()

    async def change(self, payload):
        async with self._lock:
            if self._active or self._suite_active:
                raise EvaluationAlreadyRunning("Wait for running evaluations to finish before editing tests")
            self.runner.suite = self.store.change(payload)
            return self.runner.suite

    def status(self) -> dict:
        self.refresh_definitions()
        return {
            "suite_version": self.runner.suite.version,
            "assistant_configured": self.runner.service.status()["configured"],
            "cases": [{
                "case_id": case.case_id,
                "description": case.description,
                "language": case.language,
                "execution_mode": case.execution_mode,
                "tags": case.tags,
            } for case in self.runner.suite.cases if case.enabled],
        }

    async def run_case(self, case_id: str) -> dict:
        async with self._lock:
            self.refresh_definitions()
            if case_id in self._active:
                raise EvaluationAlreadyRunning("The evaluation case is already running")
            self._active.add(case_id)
        try:
            return await self.runner.run_case(case_id)
        finally:
            async with self._lock:
                self._active.discard(case_id)

    async def run_non_llm_suite(self) -> dict:
        self.refresh_definitions()
        cases = [case for case in self.runner.suite.cases
                 if case.enabled and case.execution_mode in {"retrieval", "deterministic"}]
        return await self._run_suite(cases, "non_llm", require_zero_model_calls=True)

    async def run_full_suite(self, expected_case_count: int) -> dict:
        self.refresh_definitions()
        cases = [case for case in self.runner.suite.cases
                 if case.enabled and case.execution_mode == "full"]
        if expected_case_count != len(cases):
            raise EvaluationSuiteChanged("The live evaluation suite changed; refresh and confirm again")
        return await self._run_suite(cases, "full", require_zero_model_calls=False)

    async def _run_suite(self, cases: list[EvaluationCase], suite_kind: str,
                         *, require_zero_model_calls: bool) -> dict:
        case_ids = {case.case_id for case in cases}
        async with self._lock:
            if self._suite_active or self._active & case_ids:
                raise EvaluationAlreadyRunning("The non-LLM evaluation suite is already running")
            self._suite_active = True
            self._active.update(case_ids)

        results: list[dict] = []
        started = perf_counter()

        async def execute_sequentially() -> None:
            for case in cases:
                results.append(await self.runner.run_case(case.case_id))

        completed = True
        try:
            try:
                await asyncio.wait_for(execute_sequentially(), self.suite_timeout_seconds)
            except asyncio.TimeoutError:
                completed = False
        finally:
            async with self._lock:
                self._active.difference_update(case_ids)
                self._suite_active = False

        passed_cases = sum(1 for result in results if result["passed"])
        model_calls = sum(result["model_call_count"] for result in results)
        failed_case_codes = [{
            "case_id": result["case_id"],
            "failure_codes": result["failure_codes"],
        } for result in results if not result["passed"]]
        return {
            "suite_version": self.runner.suite.version,
            "suite_kind": suite_kind,
            "completed": completed,
            "passed": completed and len(results) == len(cases)
                      and passed_cases == len(cases)
                      and (not require_zero_model_calls or model_calls == 0),
            "total_cases": len(cases),
            "completed_cases": len(results),
            "passed_cases": passed_cases,
            "failed_cases": len(results) - passed_cases,
            "duration_ms": round((perf_counter() - started) * 1000),
            "model_call_count": model_calls,
            "failure_codes": list(dict.fromkeys(
                code for item in failed_case_codes for code in item["failure_codes"]
            ))[:8],
            "failed_case_codes": failed_case_codes,
            "results": results,
        }
