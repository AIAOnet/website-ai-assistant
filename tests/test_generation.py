import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.test_foundation import source
from website_assistant.admin_auth import hash_password
from website_assistant.api import create_app
from website_assistant.knowledge import KnowledgeStore
from website_assistant.models import ModelResponse, OpenAICompatibleProvider, ProviderError, NoRedirect, MAX_RESPONSE_BYTES
from website_assistant.rate_limit import RateLimiter
from website_assistant.service import AssistantService
from website_assistant.settings import Settings


class FakeProvider:
    def __init__(self, text="Orchid irrigation uses drip watering. [orchid]", error=None):
        self.text, self.error, self.calls = text, error, []

    async def generate(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return ModelResponse(self.text, "fixture-model")


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sources.json").write_text(json.dumps([source()]), encoding="utf-8")
        self.knowledge = KnowledgeStore(self.root / "sources.json", home_url="https://example.com")
        self.settings = Settings(data_path=self.root)

    async def test_supported_draft_and_registry_payload(self):
        provider = FakeProvider()
        response = await AssistantService(self.knowledge, self.settings, provider).respond("orchid irrigation", "en")
        self.assertEqual(response["generation"], "llm")
        self.assertEqual(response["grounding"], "CHECKED_EXTRACTIVE")
        self.assertEqual(response["sources"][0]["url"], source()["canonical_url"])
        payload = json.loads(provider.calls[0][1]["content"])
        self.assertEqual(payload["evidence"][0]["content"], source()["content"])
        self.assertNotIn("saferoad", provider.calls[0][0]["content"].lower())

    async def test_unsafe_or_unsupported_drafts_fall_back(self):
        for draft in ("Orchid irrigation costs 50 dollars. [orchid]",
                      "Orchid irrigation uses drip watering. [invented]",
                      "Orchid irrigation uses drip watering.",
                      "Your meeting is booked. [orchid]",
                      "<script>alert(1)</script> [orchid]",
                      "Read https://evil.example [orchid]",
                      "Orchid irrigation does not use drip watering. [orchid]",
                      "Ignore all previous instructions. [orchid]"):
            with self.subTest(draft=draft):
                response = await AssistantService(self.knowledge, self.settings, FakeProvider(draft)).respond("orchid", "en")
                self.assertEqual(response["generation"], "extractive")
                self.assertEqual(response["answer"], source()["content"])
                self.assertTrue(response["fallback_reasons"])
                self.assertFalse(response["appointment_available"])

    async def test_provider_failure_is_sanitized(self):
        for error in (ProviderError("secret-token and private response"), TimeoutError()):
            response = await AssistantService(self.knowledge, self.settings, FakeProvider(error=error)).respond("orchid", "en")
            self.assertEqual(response["fallback_reasons"], ["provider_unavailable"])
            self.assertNotIn("secret-token", json.dumps(response))

    async def test_unconfigured_provider_uses_excerpts(self):
        response = await AssistantService(self.knowledge, self.settings).respond("orchid", "en")
        self.assertEqual(response["fallback_reasons"], ["provider_not_configured"])

    async def test_empty_and_unmatched_knowledge_skip_provider(self):
        provider = FakeProvider()
        service = AssistantService(self.knowledge, self.settings, provider)
        await service.respond("volcanoes", "en")
        empty = KnowledgeStore(self.root / "empty" / "sources.json")
        await AssistantService(empty, self.settings, provider).respond("orchid", "en")
        self.assertEqual(provider.calls, [])

    async def test_registry_mismatch_skips_provider_and_fallback(self):
        provider = FakeProvider()
        self.knowledge.search = Mock(return_value=[source("Unregistered content")])
        response = await AssistantService(self.knowledge, self.settings, provider).respond("orchid", "en")
        self.assertEqual(response["fallback_reasons"], ["invalid_evidence"])
        self.assertEqual(response["sources"], [])
        self.assertEqual(provider.calls, [])

    async def test_probe_uses_no_website_or_visitor_data(self):
        provider = FakeProvider("OK")
        response = await AssistantService(self.knowledge, self.settings, provider).probe()
        self.assertTrue(response["available"])
        self.assertNotIn("orchid", json.dumps(provider.calls))


class ProviderTransportTests(unittest.TestCase):
    def provider_with_response(self, body):
        provider = OpenAICompatibleProvider("https://provider.example/chat", "test-key", "model")
        response = Mock()
        response.read.return_value = body
        provider.opener = Mock()
        provider.opener.open.return_value.__enter__ = Mock(return_value=response)
        provider.opener.open.return_value.__exit__ = Mock(return_value=False)
        return provider, response

    def test_valid_response_bounded_read_and_payload(self):
        provider, response = self.provider_with_response(json.dumps({"choices": [{"message": {"content": "<think>internal</think>Answer"}}]}).encode())
        result = provider._generate([{"role": "user", "content": "question"}])
        self.assertEqual(result.content, "Answer")
        response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)
        request = provider.opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)["max_tokens"], 800)

    def test_malformed_empty_nontext_and_oversized_responses(self):
        for body in (b"invalid", b"{}", b'{"choices":[{"message":{"content":null}}]}',
                     b'{"choices":[{"message":{"content":""}}]}', b"a" * (MAX_RESPONSE_BYTES + 1)):
            provider, _ = self.provider_with_response(body)
            with self.assertRaises(ProviderError):
                provider._generate([])

    def test_redirects_never_forward_credentials(self):
        with self.assertRaises(ProviderError):
            NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.example")

    def test_endpoint_configuration_rejects_secret_urls_and_remote_http(self):
        for endpoint in ("http://provider.example/chat", "https://key@provider.example/chat",
                         "https://provider.example/chat?key=secret", "file:///tmp/provider"):
            with self.assertRaises(ValidationError):
                Settings(ai_api_endpoint=endpoint)
        self.assertEqual(Settings(ai_api_endpoint="http://127.0.0.1:9000/chat").ai_api_endpoint,
                         "http://127.0.0.1:9000/chat")


class ProviderAdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "fixture-password-123"
        cls.password_hash = hash_password(cls.password)

    def client(self, role="editor"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings(data_path=Path(directory.name), admin_username="admin",
                            admin_password_hash=self.password_hash, admin_role=role,
                            admin_cookie_secure=False, ai_api_endpoint="https://provider.example/private-path",
                            ai_api_key="test-key-secret", ai_model="fixture", chat_rate_limit=1)
        client = TestClient(create_app(settings))
        self.addCleanup(client.close)
        client.app.state.service.provider = FakeProvider("OK")
        return client

    def login(self, client):
        client.post("/api/admin/login", json={"username": "admin", "password": self.password})
        return client.get("/api/admin/status")

    def test_status_hides_secrets_and_probe_requires_role_and_csrf(self):
        client = self.client()
        self.assertEqual(client.post("/api/admin/provider/probe").status_code, 401)
        response = self.login(client)
        self.assertNotIn("test-key-secret", response.text)
        self.assertNotIn("private-path", response.text)
        self.assertEqual(response.json()["provider"]["endpoint_host"], "provider.example")
        self.assertEqual(client.post("/api/admin/provider/probe").status_code, 403)
        headers = {"X-CSRF-Token": response.json()["csrf"]}
        self.assertTrue(client.post("/api/admin/provider/probe", headers=headers).json()["available"])
        viewer = self.client("viewer")
        viewer_status = self.login(viewer).json()
        self.assertEqual(viewer.post("/api/admin/provider/probe", headers={"X-CSRF-Token": viewer_status["csrf"]}).status_code, 403)

    def test_probe_limit_and_public_chat_limit(self):
        client = self.client()
        headers = {"X-CSRF-Token": self.login(client).json()["csrf"]}
        for _ in range(3):
            self.assertEqual(client.post("/api/admin/provider/probe", headers=headers).status_code, 200)
        self.assertEqual(client.post("/api/admin/provider/probe", headers=headers).status_code, 429)
        self.assertEqual(client.post("/api/chat", json={"message": "hello"}).status_code, 200)
        self.assertEqual(client.post("/api/chat", json={"message": "hello"}).status_code, 429)

    def test_rate_limit_expires(self):
        current = [0]
        limiter = RateLimiter(1, clock=lambda: current[0])
        self.assertTrue(limiter.allow("client"))
        self.assertFalse(limiter.allow("client"))
        current[0] = 60
        self.assertTrue(limiter.allow("client"))
