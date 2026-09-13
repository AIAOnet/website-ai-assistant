import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from site_runtime.evaluation_cases import EvaluationCase
from site_runtime.evaluation_store import EvaluationStore, EvaluationChange, EvaluationConflict
from site_runtime.evaluation_runner import EvaluationCoordinator, EvaluationAlreadyRunning, EvaluationRunner


def case(**changes):
    return EvaluationCase(**{**dict(case_id="company-en", description="Company overview", language="en",
        question="Tell me about the company", execution_mode="retrieval", expected_intent="COMPANY_INFORMATION",
        expected_generation=[], tags=["custom"]), **changes})


class EvaluationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "evaluations.db"

    def test_crud_persists_and_conflicting_writes_are_rejected(self):
        store = EvaluationStore(self.path)
        store.change(EvaluationChange(revision=0, operation="create", case_id="company-en", case=case()))
        reopened = EvaluationStore(self.path)
        self.assertEqual(reopened.load().cases[0].question, case().question)
        with self.assertRaises(EvaluationConflict):
            store.change(EvaluationChange(revision=0, operation="update", case_id="company-en", case=case()))
        reopened.change(EvaluationChange(revision=1, operation="update", case_id="company-en", case=case(enabled=False)))
        self.assertFalse(store.load().cases[0].enabled)
        store.change(EvaluationChange(revision=2, operation="delete", case_id="company-en"))
        self.assertEqual(EvaluationStore(self.path).load().cases, [])

    def test_legacy_import_happens_only_once_even_after_delete(self):
        legacy = self.root / "evaluation_cases.json"
        legacy.write_text(json.dumps({"version":"old", "cases":[case().model_dump()]}))
        store = EvaluationStore(self.path, legacy)
        self.assertEqual(len(store.load().cases), 1)
        store.change(EvaluationChange(revision=0, operation="delete", case_id="company-en"))
        self.assertEqual(EvaluationStore(self.path, legacy).load().cases, [])

    def test_invalid_migration_does_not_mark_import_complete(self):
        legacy = self.root / "evaluation_cases.json"
        legacy.write_text('{"version":"old","cases":[{}]}')
        with self.assertRaises(ValueError):
            EvaluationStore(self.path, legacy)
        legacy.write_text(json.dumps({"version":"old", "cases":[case().model_dump()]}))
        self.assertEqual(len(EvaluationStore(self.path, legacy).load().cases), 1)

    def test_runner_refreshes_and_rejects_edits_during_execution(self):
        store = EvaluationStore(self.path)
        runner = SimpleNamespace(suite=store.load(), service=SimpleNamespace(status=lambda:{"configured":False}))
        coordinator = EvaluationCoordinator(runner, store=store)
        payload = EvaluationChange(revision=0, operation="create", case_id="company-en", case=case())
        asyncio.run(coordinator.change(payload))
        self.assertEqual(coordinator.status()["cases"][0]["case_id"], "company-en")
        coordinator._active.add("company-en")
        with self.assertRaises(EvaluationAlreadyRunning):
            asyncio.run(coordinator.change(EvaluationChange(revision=1, operation="delete", case_id="company-en")))
        self.assertEqual(len(store.load().cases), 1)

    def test_tool_response_mode_cannot_call_generation_provider(self):
        class Service:
            provider = object()
            intent_classifier_enabled = True
            router = SimpleNamespace(route=lambda question: SimpleNamespace(value="COMPANY_INFORMATION"))
            memory = SimpleNamespace(clear=lambda conversation: None)
            async def respond_async(self, conversation, question, language):
                assert self.provider is None
                assert not self.intent_classifier_enabled
                return {"intent":"COMPANY_INFORMATION", "generation":"deterministic", "answer":"ok"}
        from site_runtime.evaluation_cases import EvaluationSuite
        service = Service()
        runner = EvaluationRunner(service, EvaluationSuite(version="test", cases=[case(
            execution_mode="deterministic", expected_generation=["deterministic"])]))
        result = asyncio.run(runner.run_case("company-en"))
        self.assertTrue(result["passed"])
        self.assertIsNotNone(service.provider)
        self.assertTrue(service.intent_classifier_enabled)
