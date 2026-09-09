import copy
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from website_assistant.api import create_app
from website_assistant.admin_auth import hash_password
from website_assistant.settings import Settings
from website_assistant.ontology_builder import build_ontology
from website_assistant.ontology_review import OntologyReviews, ReviewChange, ReviewConflict, ItemReviewChange
from test_ontology import record, HTML


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = OntologyReviews(self.root)
        self.graph = build_ontology([record()])
        self.entity = next(e for e in self.graph['entities'] if e['label'] == 'Water Monitor')

    def change(self, decision='approved', revision=0, display_label=None):
        return ReviewChange(entity_id=self.entity['entity_id'], decision=decision,
                            revision=revision, display_label=display_label, confirmed=True)

    def test_persistent_source_backed_name_and_reset(self):
        before = copy.deepcopy(self.graph)
        self.store.change(self.graph, self.change(display_label='WM'), 'editor', 'build')
        view = OntologyReviews(self.root).inspect(self.graph)
        item = next(r for r in view['reviews'] if r['entity_id'] == self.entity['entity_id'])
        self.assertEqual(item['display_label'], 'WM')
        self.assertEqual(item['status'], 'approved')
        self.assertEqual(self.graph, before)
        self.store.change(self.graph, self.change('reset', 1), 'editor', 'build')
        item = next(r for r in self.store.inspect(self.graph)['reviews'] if r['entity_id'] == self.entity['entity_id'])
        self.assertEqual(item['status'], 'automated')
        self.assertEqual(item['revision'], 2)

    def test_suppression_excludes_attached_edges_and_aliases(self):
        view = self.store.change(self.graph, self.change('suppressed'), 'editor', 'build')
        self.assertNotIn(self.entity['entity_id'], view['effective_entity_ids'])
        for edge in self.graph['relationships']:
            if self.entity['entity_id'] in (edge['subject_id'], edge['object_id']):
                self.assertNotIn(edge['relationship_id'], view['effective_relationship_ids'])
        self.assertEqual(view['effective_alias_ids'], [])

    def test_changed_evidence_requires_review_and_can_be_reapproved(self):
        self.store.change(self.graph, self.change(), 'editor', 'old')
        changed = build_ontology([record(HTML.replace('The team maintains gardens.', 'The team maintains local gardens.'))])
        item = next(r for r in self.store.inspect(changed)['reviews'] if r['entity_id'] == self.entity['entity_id'])
        self.assertEqual(item['status'], 'needs_review')
        self.assertNotIn(self.entity['entity_id'], self.store.inspect(changed)['effective_entity_ids'])
        self.store.change(changed, self.change(revision=1), 'editor', 'new')
        self.assertEqual(next(r for r in self.store.inspect(changed)['reviews'] if r['entity_id'] == self.entity['entity_id'])['status'], 'approved')

    def test_conflicts_and_unsupported_names(self):
        with self.assertRaises(ReviewConflict):
            self.store.change(self.graph, self.change(display_label='Invented'), 'editor', 'build')
        self.store.change(self.graph, self.change(), 'editor', 'build')
        with self.assertRaises(ReviewConflict):
            OntologyReviews(self.root).change(self.graph, self.change(), 'editor', 'build')
        with self.assertRaises(ReviewConflict):
            self.store.change(build_ontology([]), self.change(), 'editor', 'build')

    def test_api_permissions_confirmation_and_conflicts(self):
        password = 'review-test-password'
        password_hash = hash_password(password)
        for role in ('editor', 'viewer'):
            app = create_app(Settings(data_path=self.root / role, admin_username='admin',
                admin_password_hash=password_hash, admin_role=role, admin_cookie_secure=False))
            app.state.discovery_jobs.ontology = lambda job_id: self.graph
            with TestClient(app) as client:
                url = '/api/admin/discovery/build/ontology/reviews'
                body = self.change().model_dump()
                self.assertEqual(client.get(url).status_code, 401)
                self.assertEqual(client.post(url, json=body).status_code, 401)
                client.post('/api/admin/login', json={'username':'admin','password':password})
                headers = {'X-CSRF-Token':client.get('/api/admin/status').json()['csrf']}
                self.assertEqual(client.get(url).status_code, 200)
                self.assertEqual(client.post(url, json=body).status_code, 403)
                response = client.post(url, json=body, headers=headers)
                self.assertEqual(response.status_code, 200 if role == 'editor' else 403)
                if role == 'editor':
                    self.assertEqual(client.post(url, json=body, headers=headers).status_code, 409)
                    self.assertEqual(client.post(url, json={**body,'confirmed':False}, headers=headers).status_code, 422)

    def item_change(self, kind, item, decision='suppressed', revision=0):
        return ItemReviewChange(kind=kind, item_id=item[kind+'_id'], decision=decision,
                                revision=revision, confirmed=True)

    def test_independent_reviews_persist_reset_and_preserve_graph(self):
        before = copy.deepcopy(self.graph)
        for kind, collection in [('relationship','relationships'), ('alias','aliases')]:
            item = self.graph[collection][0]
            view = self.store.change_item(self.graph, self.item_change(kind,item), 'editor', 'first')
            self.assertNotIn(item[kind+'_id'], view['effective_'+kind+'_ids'])
            self.assertEqual(len(view['effective_entity_ids']), len(self.graph['entities']))
            restarted = OntologyReviews(self.root)
            self.assertNotIn(item[kind+'_id'], restarted.inspect(self.graph)['effective_'+kind+'_ids'])
            with self.assertRaises(ReviewConflict):
                restarted.change_item(self.graph, self.item_change(kind,item), 'editor', 'first')
            view = restarted.change_item(self.graph, self.item_change(kind,item,'reset',1), 'editor', 'second')
            self.assertIn(item[kind+'_id'], view['effective_'+kind+'_ids'])
        self.assertEqual(before, self.graph)

    def test_shifted_evidence_matches_stable_identity_but_requires_review(self):
        for kind, collection in [('relationship','relationships'), ('alias','aliases')]:
            for item in self.graph[collection]:
                self.store.change_item(self.graph, self.item_change(kind,item,'approved'), 'editor', 'first')
        changed = build_ontology([record(HTML.replace('The team maintains gardens.', 'The team maintains local gardens.'))])
        view = self.store.inspect(changed)
        self.assertTrue(all(r['status']=='needs_review' and r['revision']==1 for r in view['item_reviews']))
        self.assertEqual(view['effective_relationship_ids'], [])
        self.assertEqual(view['effective_alias_ids'], [])
        item = changed['aliases'][0]
        view = self.store.change_item(changed, self.item_change('alias',item,'approved',1), 'editor', 'second')
        self.assertIn(item['alias_id'],view['effective_alias_ids'])

    def test_parent_exclusion_wins_and_suppressed_alias_cannot_be_display_name(self):
        alias = self.graph['aliases'][0]
        self.store.change(self.graph,self.change(display_label='WM'),'editor','first')
        view = self.store.change_item(self.graph,self.item_change('alias',alias),'editor','first')
        entity = next(r for r in view['reviews'] if r['entity_id']==self.entity['entity_id'])
        self.assertEqual(entity['display_label'],'Water Monitor')
        self.store.change(self.graph,self.change('suppressed',1),'editor','first')
        view = self.store.change_item(self.graph,self.item_change('alias',alias,'approved',1),'editor','first')
        self.assertEqual(view['effective_alias_ids'],[])

    def test_item_api_boundary(self):
        password='item-review-password'
        password_hash=hash_password(password)
        for role in ('viewer','editor'):
            app=create_app(Settings(data_path=self.root/role,admin_username='admin',admin_password_hash=password_hash,
                                    admin_role=role,admin_cookie_secure=False))
            app.state.discovery_jobs.ontology=lambda job_id:self.graph
            with TestClient(app) as client:
                url='/api/admin/discovery/build/ontology/item-reviews'
                body=self.item_change('alias',self.graph['aliases'][0]).model_dump()
                self.assertEqual(client.post(url,json=body).status_code,401)
                client.post('/api/admin/login',json={'username':'admin','password':password})
                headers={'X-CSRF-Token':client.get('/api/admin/status').json()['csrf']}
                self.assertEqual(client.post(url,json=body).status_code,403)
                self.assertEqual(client.post(url,json=body,headers=headers).status_code,200 if role=='editor' else 403)
                if role=='editor':
                    self.assertEqual(client.post(url,json=body,headers=headers).status_code,409)
                    self.assertEqual(client.post(url,json={**body,'confirmed':False},headers=headers).status_code,422)
                    self.assertEqual(client.post(url,json={**body,'display_label':'invented'},headers=headers).status_code,422)
                    self.assertEqual(client.post(url,json={**body,'item_id':'missing'},headers=headers).status_code,409)

    def test_version_one_database_migrates_without_losing_entity_review(self):
        import sqlite3
        self.store.change(self.graph,self.change(),'editor','first')
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('DROP TABLE item_reviews')
            db.execute('DROP TABLE item_review_history')
            db.execute("DELETE FROM schema_migrations WHERE database_name='ontology_reviews' AND version=2")
        migrated=OntologyReviews(self.root)
        self.assertEqual(next(r for r in migrated.inspect(self.graph)['reviews'] if r['entity_id']==self.entity['entity_id'])['status'],'approved')
        migrated.change_item(self.graph,self.item_change('alias',self.graph['aliases'][0]),'editor','second')
