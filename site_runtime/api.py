from __future__ import annotations
from .configuration import setting

import os
import copy
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request, Depends
from fastapi.security import HTTPBearer
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .service import AssistantService
from .tools import AssistantTools, ToolValidationError
from .appointments import AppointmentError, AppointmentStore, AppointmentTools, SESSION
from .admin import router as admin_router, protect_admin
from .rag_admin import RagAdmin
from .ontology_admin import OntologyAdmin
from .source_admin import SourceAdmin
from .website import WebsiteConnection, public_website_cors
from .diagnostics import DiagnosticStore, traced_chat
from .evaluation_runner import EvaluationCoordinator, EvaluationRunner
from .evaluation_store import EvaluationStore
from .provider_probe import ProviderProbe
from .provider_settings import ProviderConfigurationStore
from .security_audit import SecurityAuditStore
from .rate_limit import PublicRateLimiter
from .calendar_admin import CalendarAdmin
from .regex_admin import RegexRuleStore
from .router import IntentRouter
from .monitoring import MonitoringAdmin
from .embedding_settings import EmbeddingConfigurationStore, EmbeddingProviderProbe
from .usage_budget import UsageBudget


ROOT = Path(__file__).parents[1]
DATA = Path(setting("WEBSITE_ASSISTANT_DATA_PATH", ROOT / "data"))
tools = AssistantTools(DATA)
usage_budget = UsageBudget(DATA / "usage_budget.db")
embedding_configuration = EmbeddingConfigurationStore(DATA / "embedding_provider_settings.json", usage_budget)
tools.knowledge.embedding_client = embedding_configuration.client
provider_configuration = ProviderConfigurationStore(DATA / "provider_settings.json")
service = AssistantService(tools, configuration=provider_configuration.configuration, usage_budget=usage_budget)
regex_rules = RegexRuleStore(DATA / "intent_rules.json")
service.router = IntentRouter(regex_rules)
appointment_store = AppointmentStore(Path(setting("WEBSITE_ASSISTANT_APPOINTMENT_DB", DATA / "appointments.db")))
appointment_tools = AppointmentTools(tools.contacts, appointment_store)
app = FastAPI(title="Website Assistant Concept Demo", version="3.0.0")
API_AUTH = [Depends(HTTPBearer(auto_error=False, scheme_name='AssistantBearer'))]


@app.exception_handler(sqlite3.Error)
async def storage_error(request, error):
    return JSONResponse({"detail": "Storage is temporarily unavailable. Please try again after repair."},
                        status_code=503, headers={"Cache-Control": "no-store"})

app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")
app.middleware("http")(protect_admin)
app.include_router(admin_router)
app.state.rag = RagAdmin(tools)
app.state.ontology = OntologyAdmin(app.state.rag)
app.state.sources = SourceAdmin(app.state.rag)
app.state.website = WebsiteConnection(DATA)
app.state.diagnostics = DiagnosticStore(DATA / "diagnostics.db")
app.state.security_audit = SecurityAuditStore(
    Path(setting("WEBSITE_ASSISTANT_SECURITY_AUDIT_DB", DATA / "security_audit.db")))
app.state.evaluation_store = EvaluationStore(DATA / "evaluations.db", DATA / "evaluation_cases.json")
app.state.evaluations = EvaluationCoordinator(
    EvaluationRunner(service, app.state.evaluation_store.load()), store=app.state.evaluation_store
)
app.state.provider_probe = ProviderProbe(service)
app.state.provider_configuration = provider_configuration
app.state.embedding_configuration = embedding_configuration
app.state.embedding_probe = EmbeddingProviderProbe(embedding_configuration)
app.state.usage_budget = usage_budget
app.state.service = service
app.state.public_limiter = PublicRateLimiter.from_environment(DATA)
app.state.calendar = CalendarAdmin(appointment_tools)
app.state.regex_rules = regex_rules
app.state.monitoring = MonitoringAdmin(DATA / "monitoring_settings.json",
                                       DATA / "monitoring_alerts.json")


from .website_ingestion import install
install(app, DATA)
from .api_access import install as install_api_access
install_api_access(app, DATA)

async def protect_public_api(request, call_next):
    route = app.state.public_limiter.route(request.method, request.url.path)
    if route is None:
        return await call_next(request)
    client = request.client.host if request.client else "unknown"
    try:
        allowed, retry_after = app.state.public_limiter.check(route, client)
    except sqlite3.Error:
        return JSONResponse({"detail": "Request protection is temporarily unavailable."}, 503,
                            headers={"Cache-Control": "no-store"})
    if not allowed:
        return JSONResponse({"detail": "Too many requests. Try again later."}, 429,
                            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"})
    try:
        response = await call_next(request)
    except Exception:
        try: app.state.public_limiter.failed(route)
        except sqlite3.Error: pass
        raise
    if response.status_code >= 400:
        try: app.state.public_limiter.failed(route)
        except sqlite3.Error: pass
    if route == "appointments":
        response.headers["Cache-Control"] = "no-store"
    return response


app.middleware("http")(protect_public_api)
app.middleware("http")(public_website_cors)


class ChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    message: str = Field(min_length=1, max_length=500)
    language: Literal["sv", "en"] = "sv"


class AppointmentRequest(BaseModel):
    contact_id: str = Field(min_length=1, max_length=100)
    slot_id: str = Field(min_length=1, max_length=100)
    visitor_name: str = Field(min_length=1, max_length=100)
    visitor_email: str = Field(min_length=3, max_length=254)
    company_name: str = Field(default="", max_length=120)
    meeting_topic: str = Field(min_length=1, max_length=300)
    preferred_language: Literal["sv", "en"]
    consent: bool


class RescheduleRequest(BaseModel):
    slot_id: str = Field(min_length=1, max_length=100)
    confirmed: Literal[True]


@app.get("/healthz")
async def health() -> dict:
    if app.state.diagnostics.write_failed:
        return JSONResponse({"status": "degraded", "demo": True, "reason": "diagnostic_storage_unavailable"}, 503)
    return {"status": "healthy", "demo": True}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


@app.post("/api/chat", dependencies=API_AUTH)
@app.post("/api/widget/chat", dependencies=API_AUTH)
async def chat(request: ChatRequest, http_request: Request) -> dict:
    if not request.message.strip():
        raise HTTPException(status_code=422, detail="Message must not be blank")
    with app.state.rag.lock:
        request_service = copy.copy(service)
        request_service.tools = copy.copy(tools)
    conversation = getattr(http_request.state, 'visitor_id', 'server-' + request.conversation_id)
    return await traced_chat(app.state.diagnostics, request_service, conversation,
                             request.message.strip(), request.language)


def _session(value: str | None) -> str:
    if not value or not SESSION.fullmatch(value):
        raise HTTPException(status_code=400, detail={"code": "missing_session", "message": "X-Demo-Session is required"})
    return value


def _appointment_error(error: AppointmentError) -> HTTPException:
    statuses = {"slot_unavailable": 409, "expired_slot": 410, "invalid_state": 409, "storage_unavailable": 503}
    return HTTPException(status_code=statuses.get(error.code, 422), detail={"code": error.code, "message": str(error)})


@app.get("/api/availability/{contact_id}", dependencies=API_AUTH)
@app.get("/api/widget/availability/{contact_id}", dependencies=API_AUTH)
async def availability(contact_id: str) -> dict:
    try:
        return appointment_tools.check_availability(contact_id)
    except AppointmentError as error:
        raise _appointment_error(error) from error


@app.post("/api/appointment-requests", dependencies=API_AUTH)
@app.post("/api/widget/appointment-requests", dependencies=API_AUTH)
async def create_appointment_request(
    request: AppointmentRequest,
    x_demo_session: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> dict:
    try:
        result, created = appointment_tools.create_appointment_request(
            request.model_dump(), _session(x_demo_session), idempotency_key or ""
        )
        return {"appointment_request": result, "created": created, "demo_disclaimer": {
            "en": "This is a concept demonstration. The appointment has not been sent to Website.",
            "sv": "Detta är en konceptdemo. Mötesförfrågan har inte skickats till Website."
        }}
    except AppointmentError as error:
        raise _appointment_error(error) from error


@app.get("/api/appointment-requests", dependencies=API_AUTH)
@app.get("/api/widget/appointment-requests", dependencies=API_AUTH)
async def list_appointment_requests(x_demo_session: str | None = Header(default=None)) -> dict:
    return {"appointment_requests": appointment_tools.list_appointment_requests(_session(x_demo_session))}


@app.post("/api/appointment-requests/{request_id}/reschedule", dependencies=API_AUTH)
@app.post("/api/widget/appointment-requests/{request_id}/reschedule", dependencies=API_AUTH)
async def reschedule_appointment_request(request_id: str, request: RescheduleRequest,
                                         x_demo_session: str | None = Header(default=None)) -> dict:
    try:
        result = appointment_tools.reschedule_appointment_request(request_id, _session(x_demo_session), request.slot_id)
        if not result:
            raise HTTPException(status_code=404, detail="Appointment request not found")
        return {"appointment_request": result}
    except AppointmentError as error:
        raise _appointment_error(error) from error


@app.get("/api/appointment-requests/{request_id}", dependencies=API_AUTH)
@app.get("/api/widget/appointment-requests/{request_id}", dependencies=API_AUTH)
async def get_appointment_request(request_id: str, x_demo_session: str | None = Header(default=None)) -> dict:
    result = appointment_tools.get_appointment_request(request_id, _session(x_demo_session))
    if not result:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Appointment request not found"})
    return {"appointment_request": result}


@app.post("/api/appointment-requests/{request_id}/cancel", dependencies=API_AUTH)
@app.post("/api/widget/appointment-requests/{request_id}/cancel", dependencies=API_AUTH)
async def cancel_appointment_request(request_id: str, x_demo_session: str | None = Header(default=None)) -> dict:
    result = appointment_tools.cancel_appointment_request(request_id, _session(x_demo_session))
    if not result:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Appointment request not found"})
    return {"appointment_request": result}


def main() -> None:
    import uvicorn

    uvicorn.run("site_runtime.api:app", host=setting("WEBSITE_ASSISTANT_HOST", "127.0.0.1"), port=int(setting("WEBSITE_ASSISTANT_PORT", "8001")), proxy_headers=False)


if __name__ == "__main__":
    main()
