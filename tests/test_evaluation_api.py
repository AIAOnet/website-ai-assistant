"""Exercise protected evaluation editing in a separate, isolated app process."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = r'''import sys
from pathlib import Path
from site_runtime import configuration
from site_runtime.admin_auth import hash_password
from fastapi.testclient import TestClient
root=Path(sys.argv[1]); env=root/'.env'
env.write_text('WEBSITE_ASSISTANT_DATA_PATH='+root.as_posix()+'\nWEBSITE_ASSISTANT_ADMIN_USERNAME=testadmin\nWEBSITE_ASSISTANT_ADMIN_PASSWORD_HASH='+hash_password('temporary-test-password')+'\nWEBSITE_ASSISTANT_ADMIN_COOKIE_SECURE=false\n')
configuration.load(env)
from site_runtime.api import app
import site_runtime.admin as admin
with TestClient(app) as client:
 url='/api/admin/evaluations/manage'
 assert client.get(url).status_code==401
 def login():
  result=client.post('/api/admin/login',headers={'Origin':'http://testserver'},json={'username':'testadmin','password':'temporary-test-password'})
  assert result.status_code==200,result.text
  return {'Origin':'http://testserver','X-CSRF-Token':client.get('/api/admin/session').json()['csrf_token']}
 headers=login()
 payload={'revision':0,'operation':'create','case_id':'company-en','case':{'case_id':'company-en','description':'Company overview','language':'en','question':'Tell me about the company','execution_mode':'retrieval','expected_intent':'COMPANY_INFORMATION','expected_generation':[],'tags':['custom']}}
 assert client.post(url,json=payload).status_code==403
 result=client.post(url,headers=headers,json=payload);assert result.status_code==200,result.text
 assert client.get('/api/admin/evaluations/cases').json()['cases'][0]['case_id']=='company-en'
 assert client.post(url,headers=headers,json=payload).status_code==409
 run=client.post('/api/admin/evaluations/run',headers=headers,json={'case_id':'company-en'});assert run.status_code==200,run.text
 assert run.json()['suite_version']=='1'
 payload['revision']=1;payload['operation']='update';payload['case']['enabled']=False
 assert client.post(url,headers=headers,json=payload).status_code==200
 assert client.get('/api/admin/evaluations/cases').json()['cases']==[]
 invalid={**payload,'revision':2,'case':{**payload['case'],'language':'xx'}}
 assert client.post(url,headers=headers,json=invalid).status_code==422
 admin.auth.role='viewer'
 headers=login()
 assert client.get(url).status_code==200
 assert client.post(url,headers=headers,json={'revision':2,'operation':'delete','case_id':'company-en'}).status_code==403
 admin.auth.role='editor'
 headers=login()
 assert client.post(url,headers=headers,json={'revision':2,'operation':'delete','case_id':'company-en'}).status_code==200
 assert client.get(url).json()['cases']==[]
'''

class EvaluationApiTests(unittest.TestCase):
    def test_management_auth_validation_and_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable, '-c', SCRIPT, directory],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
