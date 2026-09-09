"""Public website connection settings and narrowly scoped browser CORS."""
import ipaddress
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .rag_settings import atomic_json


def normalize_origin(value: str) -> str:
    if not value or any(c.isspace() or ord(c) < 32 for c in value) or any(c in value for c in "*\\<>'\""):
        raise ValueError("Use an exact website origin, not a wildcard")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Use an HTTP(S) origin without credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Use only scheme, hostname and optional port; no path, query or fragment")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
        host = f"[{address.compressed}]" if address.version == 6 else address.compressed
        loopback = address.is_loopback
    except ValueError:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in host.split(".")
        ):
            raise ValueError("Invalid hostname")
        loopback = host == "localhost"
    if parsed.scheme == "http" and not loopback:
        raise ValueError("Use HTTPS except for a loopback development server")
    port = parsed.port
    if port == 0:
        raise ValueError("Invalid port")
    suffix = f":{port}" if port is not None and port != {"http": 80, "https": 443}[parsed.scheme] else ""
    return f"{parsed.scheme}://{host}{suffix}"


class WebsiteSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    api_base_url: str = Field(default="", max_length=300)
    allowed_origins: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("api_base_url")
    @classmethod
    def base_url(cls, value):
        return normalize_origin(value) if value else ""

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def origins(cls, value):
        if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
            raise ValueError("Allowed origins must be a list of website origins")
        return tuple(dict.fromkeys(normalize_origin(item) for item in value))


class WebsiteConnection:
    def __init__(self, data: Path):
        self.path = Path(data) / "website_settings.json"
        self.settings = WebsiteSettings()
        self.warning = None
        self.lock = threading.Lock()
        if self.path.exists():
            try:
                self.settings = WebsiteSettings.model_validate(json.loads(self.path.read_text(encoding="utf-8")))
            except (ValueError, TypeError, OSError):
                self.warning = "Invalid saved website settings; cross-site access is disabled. Save valid settings to repair."

    def status(self, current_origin):
        base = self.settings.api_base_url or current_origin
        return {"settings": self.settings.model_dump(mode="json"), "warning": self.warning,
                "effective_api_base_url": base, "chat_endpoint": base + "/api/widget/chat",
                "health_endpoint": base + "/healthz"}

    def save(self, settings):
        with self.lock:
            atomic_json(self.path, settings.model_dump(mode="json"))
            self.settings = settings
            self.warning = None


async def public_website_cors(request, call_next):
    methods = {"/api/chat": "POST", "/healthz": "GET"}
    if request.url.path.startswith('/api/widget/'):
        methods[request.url.path] = 'GET, POST'
    if request.url.path not in methods:
        return await call_next(request)
    origin = request.headers.get("origin")
    if not origin:
        response = await call_next(request)
        response.headers["Vary"] = ", ".join(filter(None, [response.headers.get("Vary"), "Origin"]))
        response.headers["Cache-Control"] = "no-store"
        return response
    same_origin = f"{request.url.scheme}://{request.url.netloc}"
    allowed = request.app.state.website.settings.allowed_origins
    permitted = origin == same_origin or origin in allowed
    if not permitted:
        return JSONResponse({"detail": "Website origin is not allowed"}, 403,
                            headers={"Vary": "Origin", "Cache-Control": "no-store"})
    if request.method == "OPTIONS":
        method = request.headers.get("access-control-request-method", "")
        headers = {h.strip().lower() for h in request.headers.get("access-control-request-headers", "").split(",") if h.strip()}
        widget = request.url.path.startswith('/api/widget/')
        accepted_headers = {'content-type', 'authorization', 'x-demo-session', 'idempotency-key'} if widget else {'content-type', 'authorization'}
        if method not in methods[request.url.path].split(', ') or not headers <= accepted_headers:
            return JSONResponse({"detail": "Preflight method or headers rejected"}, 403, headers={"Vary": "Origin"})
        response = Response(status_code=204)
        response.headers["Access-Control-Allow-Methods"] = method
        response.headers["Access-Control-Allow-Headers"] = ', '.join(sorted(accepted_headers))
    else:
        response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = origin
    # Deliberately no Allow-Credentials: admin sessions never belong in widgets.
    response.headers["Vary"] = ", ".join(filter(None, [response.headers.get("Vary"), "Origin"]))
    response.headers["Cache-Control"] = "no-store"
    return response
