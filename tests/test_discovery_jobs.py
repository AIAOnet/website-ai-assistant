import json
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import closing

from fastapi.testclient import TestClient

from website_assistant.admin_auth import hash_password
from website_assistant.api import create_app
from website_assistant.build_jobs import DiscoveryJobs, StartDiscovery, JobConflict
from website_assistant.database_maintenance import open_database
from website_assistant.settings import Settings


class FixtureCrawler:
    def __init__(self, limits):
        self.limits = limits

    def run(self, homepage, *, cancelled, progress):
        progress({"requests":2,"page_count":1,"skipped_count":0})
        return {"homepage":homepage,"canonical_homepage":homepage,"pages":[{"url":homepage,"depth":0,"html":"<script>untrusted()</script>"}],
                "skipped":[],"status":"complete","requests":2,"page_count":1,"limits":self.limits.model_dump()}


class JobTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.jobs = DiscoveryJobs(self.root, crawler_factory=FixtureCrawler)
        self.addCleanup(self.jobs.wait)
        self.request = StartDiscovery(homepage="https://example.com/")

    def test_staging_and_saved_report_survive_new_manager(self):
        job = self.jobs.start(self.request)
        self.jobs.wait()
        result = DiscoveryJobs(self.root).get(job["id"])
        self.assertEqual(result["state"],"complete")
        self.assertEqual(result["progress"]["page_count"],1)
        self.assertNotIn("<script>",json.dumps(result))
        directory = self.root / "builds" / job["id"]
        self.assertTrue((directory / "discovery.json").exists())
        self.assertIn("<script>",(directory / "page-0000.html").read_text())
        self.assertFalse((self.root / "sources.json").exists())
        self.assertFalse((self.root / "active_version.json").exists())

    def blocking(self):
        entered, release = threading.Event(), threading.Event()
        class Blocking(FixtureCrawler):
            def run(self, homepage, *, cancelled, progress):
                progress({"requests":1,"page_count":0,"skipped_count":0})
                entered.set()
                release.wait(3)
                return super().run(homepage,cancelled=cancelled,progress=progress)
        self.jobs.crawler_factory = Blocking
        job = self.jobs.start(self.request)
        self.assertTrue(entered.wait(2))
        self.addCleanup(release.set)
        return job, release

    def test_duplicate_cross_instance_rejected_and_cancel_persisted(self):
        job, release = self.blocking()
        other = DiscoveryJobs(self.root)
        with self.assertRaises(JobConflict):
            other.start(self.request)
        self.assertTrue(other.cancel(job["id"])["cancel_requested"])
        release.set(); self.jobs.wait()
        self.assertEqual(other.get(job["id"])["state"],"cancelled")

    def test_expired_worker_cannot_complete_over_interrupted_state(self):
        job, release = self.blocking()
        with closing(open_database(self.jobs.path)) as db, db:
            db.execute("UPDATE discovery_jobs SET lease_until=0 WHERE id=?",(job["id"],))
        self.assertEqual(DiscoveryJobs(self.root).get(job["id"])["state"],"interrupted")
        release.set(); self.jobs.wait()
        self.assertEqual(self.jobs.get(job["id"])["state"],"interrupted")

    def test_worker_failure_is_sanitized(self):
        class Broken(FixtureCrawler):
            def run(self, *args, **kwargs):
                raise RuntimeError("sensitive path / secret credential")
        self.jobs.crawler_factory = Broken
        job = self.jobs.start(self.request); self.jobs.wait()
        result = self.jobs.get(job["id"])
        self.assertEqual(result["state"],"failed")
        self.assertEqual(result["error"],"discovery_failed")
        self.assertNotIn("secret",json.dumps(result))

    def test_partial_coverage_is_preserved(self):
        class Limited(FixtureCrawler):
            def run(self, *args, **kwargs):
                result = super().run(*args, **kwargs)
                result["status"] = "page_limit"
                return result
        self.jobs.crawler_factory = Limited
        job = self.jobs.start(self.request); self.jobs.wait()
        result = self.jobs.get(job["id"])
        self.assertEqual(result["state"],"complete")
        self.assertEqual(result["report"]["status"],"page_limit")

    def test_missing_job(self):
        with self.assertRaises(KeyError):
            self.jobs.get("missing")
        with self.assertRaises(KeyError):
            self.jobs.cancel("missing")


class DiscoveryApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "discovery-test-password"
        cls.password_hash = hash_password(cls.password)

    def client(self, role="editor"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        client = TestClient(create_app(Settings(data_path=Path(temp.name), admin_username="admin",
            admin_password_hash=self.password_hash,admin_role=role,admin_cookie_secure=False)))
        self.addCleanup(client.close)
        self.addCleanup(client.app.state.discovery_jobs.wait)
        client.app.state.discovery_jobs.crawler_factory = FixtureCrawler
        return client

    def login(self, client):
        client.post("/api/admin/login",json={"username":"admin","password":self.password})
        return {"X-CSRF-Token":client.get("/api/admin/status").json()["csrf"]}

    def test_authorization_and_saved_results(self):
        client = self.client()
        self.assertEqual(client.get("/api/admin/discovery").status_code,401)
        headers = self.login(client)
        self.assertEqual(client.post("/api/admin/discovery",json={"homepage":"https://example.com/"}).status_code,403)
        response=client.post("/api/admin/discovery",json={"homepage":"https://example.com/"},headers=headers)
        self.assertEqual(response.status_code,202)
        client.app.state.discovery_jobs.wait()
        job_id=response.json()["id"]
        result=client.get("/api/admin/discovery/"+job_id)
        self.assertEqual(result.json()["state"],"complete")
        self.assertNotIn("<script>",result.text)
        self.assertEqual(client.get("/api/admin/discovery").json()["jobs"][0]["id"],job_id)
        self.assertEqual(client.post("/api/chat",json={"message":"hello"}).json()["grounding"],"NOT_BUILT")
        self.assertEqual(client.get("/builds/"+job_id+"/page-0000.html").status_code,404)

    def test_viewer_can_read_but_cannot_start_or_cancel(self):
        client=self.client("viewer"); headers=self.login(client)
        self.assertEqual(client.get("/api/admin/discovery").status_code,200)
        self.assertEqual(client.post("/api/admin/discovery",json={"homepage":"https://example.com/"},headers=headers).status_code,403)
        self.assertEqual(client.post("/api/admin/discovery/missing/cancel",json={},headers=headers).status_code,403)

    def test_invalid_input_and_unknown_job(self):
        client=self.client(); headers=self.login(client)
        for body in ({"homepage":"http://127.0.0.1/"},{"homepage":"https://example.com/","limits":{"max_pages":0}}):
            self.assertEqual(client.post("/api/admin/discovery",json=body,headers=headers).status_code,422)
        self.assertEqual(client.get("/api/admin/discovery/unknown").status_code,404)
        self.assertEqual(client.post("/api/admin/discovery/unknown/cancel",json={},headers=headers).status_code,404)
