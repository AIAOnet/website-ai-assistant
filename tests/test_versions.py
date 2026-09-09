import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from website_assistant.admin_auth import hash_password
from website_assistant.api import create_app
from website_assistant.build_jobs import DiscoveryJobs, StartDiscovery
from website_assistant.knowledge import KnowledgeStore
from website_assistant.models import ModelResponse
from website_assistant.ontology_review import OntologyReviews, ReviewChange
from website_assistant.service import AssistantService
from website_assistant.settings import Settings
from website_assistant.versions import Versions, ActivateVersion, RestoreVersion, VersionConflict
from test_ontology import HTML, HOME


def crawler(pages):
    class FixtureCrawler:
        def __init__(self, limits): pass
        def run(self, homepage, **kwargs):
            return {"homepage":homepage,"canonical_homepage":homepage,
                    "pages":[{"url":url,"html":html,"depth":0} for url,html in pages],
                    "status":"complete","skipped":[],"page_count":len(pages),"requests":len(pages)}
    return FixtureCrawler


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.settings=Settings(data_path=self.root,auto_activate=False)
        self.jobs=DiscoveryJobs(self.root)
        self.reviews=OntologyReviews(self.root)
        self.legacy=KnowledgeStore(self.root/'sources.json')
        self.versions=Versions(self.settings,self.jobs,self.reviews,self.legacy)

    def build(self, html=HTML, home=HOME, pages=None):
        self.jobs.crawler_factory=crawler(pages if pages is not None else [(home,html)])
        job=self.jobs.start(StartDiscovery(homepage=home));self.jobs.wait()
        return job['id']

    def activate(self, job, **options):
        return self.versions.activate(job,ActivateVersion(revision=self.versions.status()['revision'],confirmed=True,**options),'tester')

    def test_activation_chat_and_restart_without_mutating_staging(self):
        job=self.build()
        before=(self.root/'builds'/job/'sources.json').read_bytes()
        self.assertFalse(self.versions.current().knowledge.records)
        status=self.activate(job)
        snapshot=self.versions.current()
        self.assertEqual(snapshot.version_id,status['active_version'])
        answer=asyncio.run(AssistantService(self.legacy,self.settings).respond('Water Monitor','en',knowledge=snapshot.knowledge))
        self.assertTrue(answer['sources'])
        self.assertEqual(answer['sources'][0]['url'],HOME)
        self.assertIn('Water Monitor',answer['answer'])
        self.assertEqual(len(snapshot.ontology['entities']),6)
        self.assertEqual(before,(self.root/'builds'/job/'sources.json').read_bytes())
        self.assertFalse((self.root/'sources.json').exists())
        restarted=Versions(self.settings,self.jobs,self.reviews,self.legacy)
        self.assertEqual(restarted.current().version_id,snapshot.version_id)

    def test_rollback_restores_sources_and_frozen_reviews(self):
        first=self.build()
        graph=self.jobs.ontology(first);entity=graph['entities'][0]
        self.reviews.change(graph,ReviewChange(entity_id=entity['entity_id'],revision=0,decision='suppressed',confirmed=True),'tester',first)
        old=self.activate(first)['active_version']
        self.reviews.change(graph,ReviewChange(entity_id=entity['entity_id'],revision=1,decision='reset',confirmed=True),'tester',first)
        self.assertNotIn(entity['entity_id'],[e['entity_id'] for e in self.versions.current().ontology['entities']])
        self.activate(self.build(HTML.replace('Water Monitor','Soil Monitor')))
        self.versions.restore(old,RestoreVersion(revision=2,confirmed=True),'tester')
        restored=self.versions.current()
        self.assertIn('Water Monitor',restored.knowledge.records[0]['content'])
        self.assertNotIn(entity['entity_id'],[e['entity_id'] for e in restored.ontology['entities']])
        self.assertEqual(len(self.versions.status()['versions']),2)

    def test_empty_invalid_and_missing_builds_keep_active_version(self):
        active=self.activate(self.build())['active_version']
        empty=self.build('<html><script>nothing</script></html>')
        for job in (empty,'0'*32,'../outside'):
            with self.assertRaises(VersionConflict):self.activate(job)
        job=self.build()
        path=self.root/'builds'/job/'chunks.json'
        value=json.loads(path.read_text());value['chunks'][0]['content']='Invented content';path.write_text(json.dumps(value))
        with self.assertRaises(VersionConflict):self.activate(job)
        self.assertEqual(self.versions.current().version_id,active)

    def test_coverage_and_website_change_require_explicit_choices(self):
        self.activate(self.build(pages=[(HOME,HTML),(HOME+'other',HTML.replace('Water Monitor','Soil Monitor'))]))
        smaller=self.build()
        with self.assertRaisesRegex(VersionConflict,'coverage'):self.activate(smaller)
        self.activate(smaller,allow_coverage_drop=True)
        other=self.build(home='https://other.example/')
        with self.assertRaisesRegex(VersionConflict,'Website changed'):self.activate(other)
        self.activate(other,replace_site=True,allow_coverage_drop=True)

    def test_revision_conflicts_and_failed_transaction_keep_pointer(self):
        job=self.build();active=self.activate(job)['active_version']
        with self.assertRaises(VersionConflict):
            self.versions.activate(job,ActivateVersion(revision=0,confirmed=True),'stale')
        with self.assertRaises(VersionConflict):
            self.versions.restore(active,RestoreVersion(revision=0,confirmed=True),'stale')
        with closing(sqlite3.connect(self.versions.path)) as db,db:
            db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON versions BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END")
        with self.assertRaises(sqlite3.Error):self.activate(job)
        self.assertEqual(self.versions.status()['revision'],1)
        self.assertEqual(self.versions.current().version_id,active)

    def test_corrupt_version_cannot_restore_or_load_after_restart(self):
        old=self.activate(self.build())['active_version']
        latest=self.activate(self.build(HTML.replace('Water Monitor','Soil Monitor')))['active_version']
        with closing(sqlite3.connect(self.versions.path)) as db,db:
            db.execute("UPDATE versions SET payload='{}' WHERE id=?",(old,))
        with self.assertRaises(VersionConflict):self.versions.restore(old,RestoreVersion(revision=2,confirmed=True),'tester')
        self.assertEqual(self.versions.current().version_id,latest)
        with closing(sqlite3.connect(self.versions.path)) as db,db:
            db.execute("UPDATE versions SET payload='{}' WHERE id=?",(latest,))
        with self.assertRaises(VersionConflict):Versions(self.settings,self.jobs,self.reviews,self.legacy)

    def test_automatic_activation_cannot_overwrite_mid_build_manual_change(self):
        older=self.build()
        self.activate(self.build(HTML.replace('Water Monitor','Soil Monitor')))
        enabled=Versions(self.settings.model_copy(update={'auto_activate':True}),self.jobs,self.reviews,self.legacy)
        result=enabled.completed(older)
        self.assertEqual(result['state'],'review_required')
        self.assertIn('Soil Monitor',enabled.current().knowledge.records[0]['content'])

    def test_inflight_answer_keeps_original_registry_during_activation(self):
        self.activate(self.build());first=self.versions.current()
        second=self.build(HTML.replace('Water Monitor','Soil Monitor'))
        async def run():
            entered=asyncio.Event();release=asyncio.Event()
            class Provider:
                async def generate(self,messages):
                    entered.set();await release.wait()
                    return ModelResponse('Unsupported draft','fixture')
            service=AssistantService(self.legacy,self.settings,Provider())
            task=asyncio.create_task(service.respond('Water Monitor','en',knowledge=first.knowledge))
            await entered.wait();self.activate(second);release.set()
            result=await task
            self.assertIn('Water Monitor',result['answer'])
            self.assertNotIn('Soil Monitor',result['answer'])
        asyncio.run(run())

    def test_automatic_empty_and_failed_builds_preserve_existing_knowledge(self):
        enabled=Versions(self.settings.model_copy(update={'auto_activate':True}),self.jobs,self.reviews,self.legacy)
        self.jobs.on_complete=enabled.completed
        first=self.build();active=enabled.current().version_id
        self.assertEqual(self.jobs.get(first)['report']['activation']['state'],'active')
        empty=self.build('<html><script>nothing</script></html>')
        self.assertEqual(self.jobs.get(empty)['report']['activation']['state'],'review_required')
        self.build(pages=[])
        self.assertEqual(enabled.current().version_id,active)
        self.assertEqual(enabled.status()['revision'],1)

    def test_configured_scope_and_unknown_restore_fail_closed(self):
        restricted=Versions(self.settings.model_copy(update={'home_url':'https://elsewhere.example/'}),self.jobs,self.reviews,self.legacy)
        job=self.build()
        with self.assertRaises(VersionConflict):restricted.activate(job,ActivateVersion(revision=0,confirmed=True),'tester')
        with self.assertRaises(VersionConflict):self.versions.restore('missing',RestoreVersion(revision=0,confirmed=True),'tester')
        self.assertIsNone(self.versions.current().version_id)

    def test_other_instance_observes_activation_and_rollback(self):
        reader=Versions(self.settings,self.jobs,self.reviews,self.legacy)
        old=self.activate(self.build())['active_version']
        self.assertEqual(reader.current().version_id,old)

        latest=self.activate(self.build(HTML.replace('Water Monitor','Soil Monitor')))['active_version']
        self.assertEqual(reader.current().version_id,latest)
        self.versions.restore(old,RestoreVersion(revision=2,confirmed=True),'tester')
        self.assertEqual(reader.current().version_id,old)

    def test_completed_job_keeps_polling_until_activation_finishes(self):
        entered=threading.Event();release=threading.Event()
        def completed(job_id):
            entered.set();release.wait(5)
            return {'state':'staged'}
        self.jobs.on_complete=completed
        self.jobs.crawler_factory=crawler([(HOME,HTML)])
        job=self.jobs.start(StartDiscovery(homepage=HOME))
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(self.jobs.list()[0]['activation_pending'])
        finally:
            release.set();self.jobs.wait()
        self.assertFalse(self.jobs.list()[0]['activation_pending'])
        self.assertEqual(self.jobs.get(job['id'])['report']['activation']['state'],'staged')
    def test_api_auto_activation_permissions_and_restore(self):
        password='version-test-password';password_hash=hash_password(password)
        for role in ('editor','viewer'):
            settings=Settings(data_path=self.root/role,admin_username='admin',admin_password_hash=password_hash,
                              admin_role=role,admin_cookie_secure=False)
            with TestClient(create_app(settings)) as client:
                jobs=client.app.state.discovery_jobs;jobs.crawler_factory=crawler([(HOME,HTML)])
                job=jobs.start(StartDiscovery(homepage=HOME));jobs.wait()
                report=jobs.get(job['id'])['report']
                self.assertEqual(report['activation']['state'],'active')
                old=report['activation']['version_id']
                reply=client.post('/api/chat',json={'message':'Water Monitor'}).json()
                self.assertEqual(reply['knowledge_version'],old)
                self.assertTrue(client.get('/api/status').json()['knowledge_ready'])
                url='/api/admin/versions/'+old+'/restore'
                body={'revision':1,'confirmed':True}
                self.assertEqual(client.post(url,json=body).status_code,401)
                self.assertEqual(client.get('/api/admin/versions').status_code,401)
                client.post('/api/admin/login',json={'username':'admin','password':password})
                headers={'X-CSRF-Token':client.get('/api/admin/status').json()['csrf']}
                self.assertEqual(client.post(url,json=body).status_code,403)
                self.assertEqual(client.post(url,json=body,headers=headers).status_code,200 if role=='editor' else 403)
                if role=='editor':
                    self.assertEqual(client.post(url,json=body,headers=headers).status_code,409)
                    self.assertEqual(client.post(url,json={**body,'confirmed':False},headers=headers).status_code,422)
                activate='/api/admin/discovery/'+job['id']+'/activate'
                self.assertEqual(client.post(activate,json=body).status_code,403)
                self.assertEqual(client.post(activate,json={'revision':2 if role=='editor' else 1,'confirmed':True},headers=headers).status_code,200 if role=='editor' else 403)
