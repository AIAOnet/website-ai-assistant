import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from site_runtime.api_access import ApiAccess, AccessChange


class ApiAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'api_access.json'
        self.access = ApiAccess(self.path)

    def test_rotation_revocation_persistence_and_no_plaintext(self):
        first = self.access.change(AccessChange(action='rotate'))['token']
        self.assertTrue(self.access.private(first))
        self.assertNotIn(first, self.path.read_text())
        self.assertNotIn('token', self.access.status())
        second = self.access.change(AccessChange(action='rotate'))['token']
        restored = ApiAccess(self.path)
        self.assertFalse(restored.private(first))
        self.assertTrue(restored.private(second))
        restored.change(AccessChange(action='revoke'))
        self.assertFalse(ApiAccess(self.path).private(second))

    def test_visitor_expiry_origin_signature_and_scope(self):
        with patch('site_runtime.api_access.time.time', return_value=1000):
            token = self.access.issue('https://widget.example')['token']
            self.assertTrue(self.access.visitor(token, 'https://widget.example'))
            self.assertIsNone(self.access.visitor(token, 'https://other.example'))
            self.assertFalse(self.access.private(token))
            self.assertIsNone(self.access.visitor(token[:-1] + ('0' if token[-1] != '0' else '1'), 'https://widget.example'))
        with patch('site_runtime.api_access.time.time', return_value=4600):
            self.assertIsNone(self.access.visitor(token, 'https://widget.example'))

    def test_disable_revokes_visitors_and_preserves_private_key(self):
        key = self.access.change(AccessChange(action='rotate'))['token']
        token = self.access.issue('https://widget.example')['token']
        self.access.change(AccessChange(action='widget', widget_enabled=False))
        self.assertIsNone(self.access.visitor(token, 'https://widget.example'))
        self.access.change(AccessChange(action='widget', widget_enabled=True))
        self.assertIsNone(self.access.visitor(token, 'https://widget.example'))
        self.assertTrue(self.access.private(key))

    def test_failed_save_keeps_old_credential(self):
        key = self.access.change(AccessChange(action='rotate'))['token']
        with patch('site_runtime.api_access.atomic_json', side_effect=OSError):
            with self.assertRaises(OSError):
                self.access.change(AccessChange(action='revoke'))
        self.assertTrue(self.access.private(key))

    def test_corrupt_configuration_fails_closed(self):
        self.path.write_text('{}')
        with self.assertRaises(ValueError):
            ApiAccess(self.path)

    def test_http_security_and_widget_contract(self):
        result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'tests.test_api_access',
                                 '--http', self.temp.name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def http_checks(directory):
    import sqlite3
    from fastapi.testclient import TestClient
    from site_runtime import configuration
    from site_runtime.admin_auth import hash_password
    configuration._values = {'WEBSITE_ASSISTANT_DATA_PATH': directory,
        'WEBSITE_ASSISTANT_ADMIN_USERNAME': 'tester',
        'WEBSITE_ASSISTANT_ADMIN_PASSWORD_HASH': hash_password('test-password-only'),
        'WEBSITE_ASSISTANT_ADMIN_COOKIE_SECURE': 'false'}
    import site_runtime.api as runtime
    from site_runtime.website import WebsiteSettings
    from unittest.mock import AsyncMock
    app = runtime.app
    origin = 'http://testserver'
    payload = {'conversation_id': 'caller-selected', 'message': 'hello', 'language': 'en'}
    with TestClient(app) as client, patch.object(runtime, 'traced_chat', new=AsyncMock(return_value={'answer': 'fixture'})) as chat:
        for headers in ({}, {'Authorization': 'Bearer invalid'}, {'Authorization': 'Basic invalid'}):
            assert client.post('/api/chat', json=payload, headers=headers).status_code == 401
        assert chat.await_count == 0
        with patch.object(app.state.public_limiter, 'check', side_effect=sqlite3.OperationalError):
            assert client.post('/api/chat', json=payload).status_code == 503
        assert chat.await_count == 0
        schema = client.get('/openapi.json').json()
        assert schema['paths']['/api/chat']['post']['security'] == [{'AssistantBearer': []}]
        assert client.get('/api/appointment-requests').status_code == 401
        assert client.get('/api/admin/api-access').status_code == 401
        assert client.post('/api/widget/session').status_code == 403
        assert client.post('/api/widget/session', headers={'Origin': 'https://evil.example'}).status_code == 403
        first = client.post('/api/widget/session', headers={'Origin': origin}).json()['token']
        second = client.post('/api/widget/session', headers={'Origin': origin}).json()['token']
        visitor = {'Origin': origin, 'Authorization': 'Bearer ' + first}
        assert client.post('/api/chat', json=payload, headers=visitor).status_code == 401
        assert client.post('/api/widget/chat', json=payload, headers=visitor).status_code == 200
        first_id = chat.await_args.args[2]
        assert first_id != payload['conversation_id']
        assert client.post('/api/widget/chat', json=payload, headers={'Origin': origin, 'Authorization': 'Bearer ' + second}).status_code == 200
        assert chat.await_args.args[2] != first_id
        refreshed = client.post('/api/widget/session', headers=visitor).json()['token']
        assert app.state.api_access.visitor(refreshed, origin) == app.state.api_access.visitor(first, origin)
        with patch.object(runtime.appointment_tools, 'list_appointment_requests', return_value=[]) as listing:
            assert client.get('/api/widget/appointment-requests', headers={**visitor, 'X-Demo-Session': 'someone-else'}).status_code == 200
            assert listing.call_args.args[0] == first_id
            assert client.get('/api/widget/appointment-requests', headers={'Authorization': 'Bearer ' + first, 'Sec-Fetch-Site': 'same-origin'}).status_code == 200
        app.state.website.save(WebsiteSettings(allowed_origins=('https://widget.example',)))
        preflight = client.options('/api/widget/chat', headers={'Origin': 'https://widget.example',
            'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,content-type'})
        assert preflight.status_code == 204
        assert preflight.headers['Access-Control-Allow-Origin'] == 'https://widget.example'
        assert 'Access-Control-Allow-Credentials' not in preflight.headers
        assert client.post('/api/widget/chat', json=payload, headers={**visitor, 'Origin': 'https://widget.example'}).status_code == 401
        assert client.post('/api/admin/login', json={'username': 'tester', 'password': 'test-password-only'}, headers={'Origin': origin}).status_code == 200
        identity = client.get('/api/admin/session').json()
        headers = {'Origin': origin, 'X-CSRF-Token': identity['csrf_token']}
        from types import SimpleNamespace
        for role in ('editor', 'viewer'):
            with patch('site_runtime.admin.auth.session', return_value=SimpleNamespace(
                    role=role, csrf=identity['csrf_token'], username='fixture')):
                assert client.get('/api/admin/api-access').status_code == 403
                assert client.post('/api/admin/api-access', json={'action': 'rotate'}, headers=headers).status_code == 403
                assert client.get('/api/admin/usage-budget').status_code == 403
        assert client.post('/api/admin/api-access', json={'action': 'rotate'}, headers={'Origin': origin}).status_code == 403
        response = client.post('/api/admin/api-access', json={'action': 'rotate'}, headers=headers)
        assert response.status_code == 200, response.text
        key = response.json()['token']
        assert response.headers['Cache-Control'] == 'no-store'
        assert key not in client.get('/api/admin/api-access').text
        private = {'Authorization': 'Bearer ' + key}
        assert client.post('/api/chat', json=payload, headers=private).status_code == 200
        assert client.post('/api/widget/chat', json=payload, headers={**private, 'Origin': origin}).status_code == 401
        assert client.post('/api/admin/api-access', json={'action': 'revoke'}, headers=headers).status_code == 200
        assert client.post('/api/chat', json=payload, headers=private).status_code == 401
        assert client.post('/api/admin/api-access', json={'action': 'widget', 'widget_enabled': False}, headers=headers).status_code == 200
        budget = client.get('/api/admin/usage-budget')
        assert budget.status_code == 200 and budget.json()['measurement']['generation'] == 'provider_calls'
        limits={'generation_daily':5,'generation_monthly':20,'embedding_daily':10,'embedding_monthly':100,'confirmed':True}
        assert client.put('/api/admin/usage-budget',json=limits,headers={'Origin':origin}).status_code == 403
        assert client.put('/api/admin/usage-budget',json=limits,headers=headers).json()['revision'] == 1
        assert client.post('/api/widget/session', headers={'Origin': origin}).status_code == 403
        assert client.post('/api/widget/chat', json=payload, headers=visitor).status_code == 403
        app.state.public_limiter.limits['chat'] = 1
        assert client.post('/api/widget/session', headers={'Origin': origin}).status_code == 429
        assert client.get('/admin/assets/api-access.js').status_code == 200
        assert client.get('/admin/assets/usage-budget.js').status_code == 200
        assert client.get('/assets/visitor-client.js').status_code == 200
        assert key not in client.get('/api/admin/logs').text


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--http':
        http_checks(sys.argv[2])
    else:
        unittest.main()
