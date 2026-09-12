import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from site_runtime import configuration
from site_runtime.tools import AssistantTools
from site_runtime.service import AssistantService, AssistantConfiguration
from site_runtime.rag_admin import RagAdmin
from site_runtime.website_ingestion import WebsiteIngestion
from site_runtime.ontology_admin import inspect_ontology
from website_assistant.build_jobs import StartDiscovery
from test_ontology import HTML

HOME='https://garden.example/'

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        patcher=patch.object(configuration,'_values',{'WEBSITE_ASSISTANT_DATA_PATH':str(self.root)})
        patcher.start();self.addCleanup(patcher.stop)
        self.tools=AssistantTools(self.root)
        self.service=AssistantService(self.tools,configuration=AssistantConfiguration('','','',30))
        self.app=SimpleNamespace(state=SimpleNamespace(rag=RagAdmin(self.tools),service=self.service))
        self.ingestion=WebsiteIngestion(self.app,self.root)

    def build(self,html=HTML,home=HOME):
        class Crawler:
            def __init__(self,limits):pass
            def run(self,homepage,**kwargs):return {'homepage':homepage,'canonical_homepage':homepage,'pages':[{'url':homepage,'html':html,'depth':0}],'skipped':[],'status':'complete','page_count':1,'requests':0}
        self.ingestion.jobs.crawler_factory=Crawler
        job=self.ingestion.jobs.start(StartDiscovery(homepage=home));self.ingestion.jobs.wait()
        return self.ingestion.jobs.get(job['id'])

    def test_empty_copy_has_no_company_data_or_contacts(self):
        self.assertEqual(self.tools.knowledge.records,[])
        self.assertEqual(self.tools.contacts,[])
        self.assertEqual(self.tools.ontology['relationships'],[])
        answer=asyncio.run(self.service.respond_async('empty','What products are available?','en'))
        self.assertEqual(answer['grounding'],'NOT_BUILT')
        self.assertFalse(answer['appointment_available'])

    def test_website_populates_existing_formats_and_cited_chat(self):
        job=self.build()
        self.assertEqual(job['report']['activation']['state'],'active')
        self.assertTrue((self.tools.root/'approved_sources.json').exists())
        self.assertTrue((self.tools.root/'ontology.json').exists())
        self.assertTrue((self.tools.root/'knowledge'/'index.json').exists())
        report=inspect_ontology(self.tools)
        self.assertEqual(report['issues'],[])
        self.assertFalse(any(r['issues'] for r in report['relationships']+report['aliases']))
        answer=asyncio.run(self.service.respond_async('garden','Water Monitor','en'))
        self.assertIn('Water Monitor',answer['answer'])
        self.assertTrue(answer['sources'])
        self.assertEqual(answer['sources'][0]['url'],HOME)
        graph=self.tools.search_ontology('WM','en')
        self.assertTrue(graph['matched_aliases'])
        self.assertFalse(answer['appointment_available'])

    def test_new_website_replaces_data_and_restart_loads_it(self):
        self.build();first=self.tools.root
        self.build(HTML.replace('Water Monitor','Soil Monitor'),'https://other.example/')
        self.assertNotEqual(first,self.tools.root)
        restarted=AssistantTools(self.root)
        self.assertIn('Soil Monitor',restarted.knowledge.records[0]['content'])
        self.assertEqual(restarted.knowledge.records[0]['canonical_url'],'https://other.example/')
        self.ingestion.restore(first.name)
        self.assertEqual(self.tools.knowledge.records[0]['canonical_url'],HOME)

    def test_empty_or_tampered_build_does_not_replace_current_data(self):
        self.build();selected=self.tools.root
        job=self.build('<html><script>nothing</script></html>')
        self.assertEqual(job['report']['activation']['state'],'review_required')
        self.assertEqual(self.tools.root,selected)
        with self.assertRaises(ValueError):self.ingestion.restore('../outside')

    def test_copied_layout_and_neutral_runtime(self):
        root=Path(__file__).resolve().parents[1]
        index=(root/'web/index.html').read_text(encoding='utf-8')
        for selector in ('site-header','hero-copy','road-visual','chat-launcher','appointment-panel'):
            self.assertIn(selector,index)
        admin=(root/'site_runtime/admin_ui/index.html').read_text(encoding='utf-8')
        for section in ('Website &amp; Integrations','AI Settings','Knowledge Sources','Ontology','Evaluations','Calendar','Logs','homepage-build'):
            self.assertIn(section,admin)
        for base in (root/'site_runtime',root/'web'):
            for path in base.rglob('*'):
                if path.suffix in {'.py','.html','.js','.css'}:
                    text=path.read_text(encoding='utf-8').lower()
                    self.assertNotIn('saferoad',text,str(path))
                    self.assertNotIn('www.website.com',text,str(path))

    def test_configuration_ignores_ambient_provider_keys(self):
        with patch.dict(os.environ,{'WEBSITE_ASSISTANT_AI_API_KEY':'unexpected'}):
            self.assertEqual(configuration.setting('WEBSITE_ASSISTANT_AI_API_KEY',''),'')

    def test_copied_api_boot_and_ingestion_access_control(self):
        script=r'''
import json,tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from site_runtime import configuration
from site_runtime.admin_auth import hash_password
with tempfile.TemporaryDirectory() as directory:
    configuration._values={'WEBSITE_ASSISTANT_DATA_PATH':directory,'WEBSITE_ASSISTANT_ADMIN_USERNAME':'tester','WEBSITE_ASSISTANT_ADMIN_PASSWORD_HASH':hash_password('test-password-only'),'WEBSITE_ASSISTANT_ADMIN_COOKIE_SECURE':'false'}
    from site_runtime.api import app
    from site_runtime.api_access import AccessChange
    api_token=app.state.api_access.change(AccessChange(action='rotate'))['token']
    from tests.test_ontology import HTML
    from website_assistant.build_jobs import StartDiscovery
    with TestClient(app) as client:
        assert client.get('/healthz').status_code==200
        assert client.get('/api/admin/ingestion').status_code==401
        assert client.post('/api/admin/ingestion',json={'homepage':'https://garden.example/'}).status_code==403
        response=client.post('/api/admin/login',json={'username':'tester','password':'test-password-only'},headers={'Origin':'http://testserver'})
        assert response.status_code==200,response.text
        identity=client.get('/api/admin/session').json()
        headers={'Origin':'http://testserver','X-CSRF-Token':identity['csrf_token']}
        assert client.get('/admin').status_code==200
        import re
        for asset in re.findall(r'(?:src|href)="(/admin/assets/[^"]+)"', client.get('/admin').text):
            assert client.get(asset).status_code==200,asset
        assert client.get('/api/admin/ingestion').status_code==200
        assert client.post('/api/admin/ingestion',json={'homepage':'http://127.0.0.1/'},headers=headers).status_code==422
        assert client.post('/api/admin/ingestion/no-build/publish',json={'confirmed':False},headers=headers).status_code==422
        response=client.post('/api/chat',json={'conversation_id':'smoke','message':'What products are available?','language':'en'},headers={'Authorization':'Bearer '+api_token})
        assert response.status_code==200,response.text
        assert response.json()['grounding']=='NOT_BUILT'
        for endpoint in ['rag','ontology','sources','website','logs','evaluations/cases']:
            response=client.get('/api/admin/'+endpoint)
            assert response.status_code==200,(endpoint,response.text)
    # Persistent store connections are owned by the process and exit before parent cleanup.
'''
        # The child owns its temporary data; avoid Windows deletion of live SQLite handles.
        script=script.replace('with tempfile.TemporaryDirectory() as directory:',"if True:\n    directory=tempfile.mkdtemp(dir="+repr(str(self.root))+')')
        result=subprocess.run([sys.executable,'-X','utf8','-c',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

