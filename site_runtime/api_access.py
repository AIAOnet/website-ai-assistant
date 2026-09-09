"""Private API credentials and separately scoped, anonymous visitor sessions."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from typing import Literal

from .rag_settings import atomic_json

VISITOR_TTL = 3600


class AccessChange(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['rotate', 'revoke', 'widget']
    widget_enabled: bool = True


class ApiAccess:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        if not self.path.exists():
            atomic_json(self.path, {'key_hash': '', 'widget_enabled': True,
                                   'visitor_secret': secrets.token_hex(32)})
        # Invalid configuration fails startup closed, never silently resets access.
        self.values = json.loads(self.path.read_text(encoding='utf-8'))
        if (not isinstance(self.values, dict)
                or not isinstance(self.values.get('widget_enabled'), bool)
                or not isinstance(self.values.get('key_hash'), str)
                or not re.fullmatch(r'(?:[a-f0-9]{64})?', self.values['key_hash'])
                or not isinstance(self.values.get('visitor_secret'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', self.values['visitor_secret'])):
            raise ValueError('Invalid API access configuration')

    def status(self):
        return {'configured': bool(self.values['key_hash']),
                'widget_enabled': self.values['widget_enabled'],
                'visitor_ttl_seconds': VISITOR_TTL}

    def change(self, change):
        with self.lock:
            values = dict(self.values)
            token = None
            if change.action == 'rotate':
                token = 'wa_api_' + secrets.token_urlsafe(32)
                values['key_hash'] = hashlib.sha256(token.encode()).hexdigest()
            elif change.action == 'revoke':
                values['key_hash'] = ''
            else:
                values['widget_enabled'] = change.widget_enabled
                # Disable/re-enable never resurrects old visitor tokens.
                values['visitor_secret'] = secrets.token_hex(32)
            atomic_json(self.path, values)
            self.values = values
            return {**self.status(), **({'token': token} if token else {})}

    def private(self, token):
        expected = self.values['key_hash']
        return bool(expected) and hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), expected)

    def issue(self, origin, visitor=None):
        payload = {'sub': visitor or secrets.token_hex(16), 'origin': origin,
                   'exp': int(time.time()) + VISITOR_TTL}
        encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
        signature = hmac.new(bytes.fromhex(self.values['visitor_secret']), encoded.encode(), hashlib.sha256).hexdigest()
        return {'token': 'wa_vis_' + encoded + '.' + signature, 'expires_at': payload['exp']}

    def visitor(self, token, origin):
        if not self.values['widget_enabled'] or not token.startswith('wa_vis_'):
            return None
        try:
            encoded, signature = token[7:].split('.')
            expected = hmac.new(bytes.fromhex(self.values['visitor_secret']), encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            if (payload['origin'] != origin or not isinstance(payload['exp'], int)
                    or payload['exp'] <= time.time() or not re.fullmatch(r'[a-f0-9]{32}', payload['sub'])):
                return None
            return payload['sub']
        except (ValueError, TypeError, KeyError, UnicodeError):
            return None


def bearer(request):
    headers = request.headers.getlist('authorization')
    if len(headers) != 1 or len(headers[0]) > 2048:
        return ''
    parts = headers[0].split(' ')
    return parts[1] if len(parts) == 2 and parts[0].lower() == 'bearer' else ''


async def protect_api_access(request, call_next):
    path = request.url.path
    if not path.startswith('/api/') or path.startswith('/api/admin/'):
        return await call_next(request)
    access = request.app.state.api_access
    token = bearer(request)
    if path.startswith('/api/widget/'):
        own = f'{request.url.scheme}://{request.url.netloc}'
        origin = request.headers.get('origin', '')
        if not origin and request.method == 'GET' and request.headers.get('sec-fetch-site') == 'same-origin':
            origin = own
        if not origin or (origin != own and origin not in request.app.state.website.settings.allowed_origins):
            return JSONResponse({'detail': 'Website origin is not allowed'}, 403)
        if not access.values['widget_enabled']:
            return JSONResponse({'detail': 'Public widget is disabled'}, 403)
        if path == '/api/widget/session':
            return await call_next(request)
        visitor = access.visitor(token, origin)
        if visitor:
            request.state.visitor_id = 'visitor-' + visitor
            # Appointment ownership comes from the signed token, not a caller-selected ID.
            request.scope['headers'] = [(k, v) for k, v in request.scope['headers'] if k.lower() != b'x-demo-session'] + [
                (b'x-demo-session', request.state.visitor_id.encode())]
            return await call_next(request)
    elif access.private(token):
        return await call_next(request)
    return JSONResponse({'detail': 'Valid bearer token required'}, 401,
                        headers={'WWW-Authenticate': 'Bearer', 'Cache-Control': 'no-store'})


def install(app, data):
    app.state.api_access = ApiAccess(Path(data) / 'api_access.json')
    router = APIRouter()

    @router.get('/api/admin/api-access')
    async def status(request: Request):
        if request.state.admin_session.role != 'administrator':
            return JSONResponse({'detail': 'Administrator role required'}, 403)
        return app.state.api_access.status()

    @router.post('/api/admin/api-access')
    async def change(body: AccessChange, request: Request):
        if request.state.admin_session.role != 'administrator':
            return JSONResponse({'detail': 'Administrator role required'}, 403)
        try:
            return app.state.api_access.change(body)
        except OSError:
            return JSONResponse({'detail': 'API access could not be saved; previous settings remain active'}, 503)

    @router.post('/api/widget/session')
    async def visitor_session(request: Request):
        origin = request.headers['origin']
        token = bearer(request)
        previous = app.state.api_access.visitor(token, origin) if token else None
        if token and not previous:
            return JSONResponse({'detail': 'Visitor session expired'}, 401)
        return app.state.api_access.issue(origin, previous)

    app.include_router(router)
    app.middleware('http')(protect_api_access)
