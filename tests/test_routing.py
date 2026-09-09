import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.test_foundation import source
from tests.test_generation import FakeProvider
from website_assistant.admin_auth import hash_password
from website_assistant.api import create_app
from website_assistant.database_maintenance import open_database
from website_assistant.routing import RoutingStore, Rule, Change, RuleConflict
from website_assistant.settings import Settings


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = RoutingStore(self.root / "routing.db")

    def change(self, rule, operation="create", revision=None):
        return self.store.change(Change(operation=operation, revision=revision or self.store.status()["revision"],
                                        rule=rule, confirmed=True))

    def test_neutral_defaults_and_general_fallback(self):
        self.assertEqual(self.store.preview("products", "en")["intent"], "product")
        self.assertEqual(self.store.preview("tjänster", "sv")["intent"], "service")
        self.assertEqual(self.store.preview("orchid irrigation", "en")["intent"], "information")
        self.assertNotIn("saferoad", json.dumps(self.store.status()).lower())

    def test_crud_priority_language_and_persistence(self):
        first = Rule(rule_id="orchid", name="Orchid", phrases=["orchid"], intent="out_of_scope", language="en", priority=1)
        self.change(first)
        self.assertEqual(self.store.preview("orchid products", "en")["intent"], "out_of_scope")
        self.assertEqual(self.store.preview("orchid", "sv")["intent"], "information")
        self.assertEqual(RoutingStore(self.root / "routing.db").preview("orchid", "en")["rule_id"], "orchid")
        self.change(first.model_copy(update={"enabled": False}), "update")
        self.assertEqual(self.store.preview("orchid", "en")["intent"], "information")
        self.change(first, "delete")
        self.assertNotIn("orchid", [r["rule_id"] for r in self.store.status()["rules"]])
        self.assertTrue(self.store.status()["previous_available"])

    def test_literal_matching_and_word_boundaries(self):
        self.change(Rule(rule_id="literal", name="Literal", phrases=["(a+)+$", "C++", "cat"], intent="out_of_scope"))
        self.assertEqual(self.store.preview("aaaaa!", "en")["intent"], "information")
        self.assertEqual(self.store.preview("catalog", "en")["intent"], "information")
        self.assertEqual(self.store.preview("C++", "en")["intent"], "out_of_scope")
        self.assertEqual(self.store.preview("cat", "en")["intent"], "out_of_scope")

    def test_protected_actions_cannot_be_overridden(self):
        self.change(Rule(rule_id="override", name="Override", phrases=["book a meeting"], priority=1))
        for phrase in ("book a meeting", "contact me", "boka möte", "request a quotation"):
            result = self.store.preview(phrase, "en")
            self.assertTrue(result["protected"])
            self.assertEqual(result["intent"], "action_unavailable")

    def test_cross_instance_stale_write_rejected(self):
        other = RoutingStore(self.root / "routing.db")
        revision = other.status()["revision"]
        self.change(Rule(rule_id="one", name="One", phrases=["one"]))
        with self.assertRaises(RuleConflict):
            other.change(Change(operation="create", revision=revision, rule=Rule(rule_id="two", name="Two", phrases=["two"]), confirmed=True))
        self.assertEqual(other.status()["revision"], revision + 1)

    def test_validation_and_empty_rules(self):
        for phrases in ([" "], ["x" * 81], [str(i) for i in range(26)]):
            with self.assertRaises(ValidationError):
                Rule(rule_id="invalid", name="Invalid", phrases=phrases)
        for raw in self.store.status()["rules"]:
            self.change(Rule.model_validate(raw), "delete")
        self.assertEqual(self.store.status()["rules"], [])
        self.assertEqual(self.store.preview("anything", "en")["intent"], "information")

    def test_failed_write_preserves_revision(self):
        before = self.store.status()
        with self.assertRaises(ValueError):
            self.change(Rule.model_validate(before["rules"][0]))
        self.assertEqual(self.store.status(), before)


class RoutingApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "test-routing-password"
        cls.password_hash = hash_password(cls.password)

    def client(self, role="editor"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "sources.json").write_text(json.dumps([source()]), encoding="utf-8")
        settings = Settings(data_path=root, home_url="https://example.com", admin_username="admin",
                            admin_password_hash=self.password_hash, admin_role=role, admin_cookie_secure=False)
        client = TestClient(create_app(settings))
        self.addCleanup(client.close)
        client.app.state.service.provider = FakeProvider()
        return client

    def login(self, client):
        client.post("/api/admin/login", json={"username": "admin", "password": self.password})
        return {"X-CSRF-Token": client.get("/api/admin/status").json()["csrf"]}

    def test_permissions_csrf_confirmation_and_conflicts(self):
        client = self.client()
        self.assertEqual(client.get("/api/admin/routing").status_code, 401)
        headers = self.login(client)
        body = {"revision": 1, "operation": "create", "rule": Rule(rule_id="orchid", name="Orchid", phrases=["orchid"], intent="out_of_scope").model_dump(), "confirmed": True}
        self.assertEqual(client.post("/api/admin/routing", json=body).status_code, 403)
        self.assertEqual(client.post("/api/admin/routing", json={**body, "confirmed": False}, headers=headers).status_code, 422)
        self.assertEqual(client.post("/api/admin/routing", json=body, headers=headers).status_code, 200)
        self.assertEqual(client.post("/api/admin/routing", json=body, headers=headers).status_code, 409)
        viewer = self.client("viewer")
        viewer_headers = self.login(viewer)
        self.assertEqual(viewer.get("/api/admin/routing").status_code, 200)
        self.assertEqual(viewer.post("/api/admin/routing", json=body, headers=viewer_headers).status_code, 403)
        self.assertEqual(viewer.post("/api/admin/routing/preview", json={"phrase": "hello"}, headers=viewer_headers).status_code, 403)

    def test_saved_rule_changes_chat_without_restart_and_preview_skips_model(self):
        client = self.client()
        headers = self.login(client)
        before = client.post("/api/chat", json={"message": "orchid irrigation"}).json()
        self.assertEqual(before["generation"], "llm")
        provider = client.app.state.service.provider
        provider.calls.clear()
        rule = Rule(rule_id="blocked", name="Blocked topic", phrases=["orchid"], intent="out_of_scope")
        client.post("/api/admin/routing", json={"operation":"create", "revision":1, "rule":rule.model_dump(), "confirmed":True}, headers=headers)
        preview = client.post("/api/admin/routing/preview", json={"phrase":"orchid irrigation"}, headers=headers).json()
        response = client.post("/api/chat", json={"message":"orchid irrigation"}).json()
        self.assertEqual(response["intent"], preview["intent"])
        self.assertEqual(response["intent"], "out_of_scope")
        self.assertEqual(provider.calls, [])

    def test_action_request_skips_retrieval_and_provider(self):
        client = self.client()
        knowledge = client.app.state.knowledge
        knowledge.search = Mock(side_effect=AssertionError("Action reached retrieval"))
        reply = client.post("/api/chat", json={"message":"book a meeting about orchid irrigation"}).json()
        self.assertEqual(reply["intent"], "action_unavailable")
        self.assertFalse(reply["appointment_available"])
        self.assertEqual(client.app.state.service.provider.calls, [])
