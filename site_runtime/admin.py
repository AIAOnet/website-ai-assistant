"""Protected admin namespace; public visitor APIs remain independent."""
import asyncio
import hmac
import json
from time import perf_counter
from pathlib import Path

from fastapi import APIRouter, Request, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal
from .rag_settings import RagSettings
from .rag_admin import EmbeddingRebuildBusy, EmbeddingRebuildConflict
from .knowledge import KnowledgeValidationError
from .website import WebsiteSettings
from .ontology_admin import OntologyAliasChange, OntologyChange, OntologyConflict, OntologyValidationError
from .source_admin import (DocumentArchiveRequest, DocumentReindexRequest, SourceChange,
                           SourceConflict, SourceFetchRequest, SourceRefreshApproval,
                           import_byte_limit, import_limit_message)

from .admin_auth import AdminAuth, COOKIE
from .diagnostics import ADMIN_ACTIONS, append_safely
from .evaluation_store import EvaluationChange, EvaluationConflict
from .evaluation_runner import EvaluationAlreadyRunning, EvaluationCaseNotFound, EvaluationSuiteChanged
from .provider_probe import ProviderProbeBusy
from .provider_settings import GenerationProviderChange
from .embedding_settings import EmbeddingProviderChange
from .usage_budget import UsageBudgetChange, UsageBudgetExceeded
from .appointments import AppointmentError
from .regex_admin import RegexChange, RegexConflict, RegexPreview, RegexValidationError
from .monitoring import MonitoringChange
from pydantic import ValidationError

UI = Path(__file__).parent / "admin_ui"
router = APIRouter()
auth = AdminAuth()
WRITE_ROLES = frozenset({"editor", "administrator"})


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class EmbeddingProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmed_embedding_request: Literal[True]


def _audit(request, session, action, outcome, status):
    store = getattr(request.app.state, "security_audit", None)
    if store is None:
        return
    target_type = target_id = None
    source_id = getattr(request.state, "admin_source_id", None)
    fields = getattr(request.state, "admin_diagnostic_fields", {})
    if source_id:
        target_type, target_id = "source", source_id
    elif fields.get("case_id"):
        target_type, target_id = "evaluation_case", fields["case_id"]
    elif path_parts := [part for part in request.url.path.split("/") if part]:
        if len(path_parts) >= 4 and path_parts[2] == "documents":
            target_type, target_id = "document", path_parts[3]
    try:
        store.append(actor=session.username if session else "unauthenticated",
                     role=session.role if session else "unauthenticated",
                     action=action, outcome=outcome, status=status,
                     target_type=target_type, target_id=target_id)
    except Exception:
        # The administrative result is already determined; audit storage errors
        # must not disclose internals or replace that response.
        pass


async def protect_admin(request: Request, call_next):
    path = request.url.path
    if not (path == "/admin" or path.startswith("/admin/") or path.startswith("/api/admin/")):
        return await call_next(request)
    session = auth.session(request.cookies.get(COOKIE))
    request.state.admin_session = session
    response, rejection = None, None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        # No proxy headers are trusted for identity or login throttling.
        origin = request.headers.get("origin")
        if origin != f"{request.url.scheme}://{request.url.netloc}" or request.headers.get("sec-fetch-site") == "cross-site":
            response = JSONResponse({"detail": "Request origin rejected"}, 403)
            rejection = "security.origin.rejected"
    if response is None and path.startswith("/api/admin/") and path != "/api/admin/login":
        if not session:
            response = JSONResponse({"detail": "Administrator sign-in required"}, 401)
            rejection = "security.authentication.required"
        elif path == "/api/admin/security-audit" and session.role != "administrator":
            response = JSONResponse({"detail": "Administrator role required"}, 403)
            rejection = "security.role.rejected"
        elif request.method not in {"GET", "HEAD", "OPTIONS"} and not hmac.compare_digest(
            request.headers.get("x-csrf-token", "").encode(), session.csrf.encode()
        ):
            response = JSONResponse({"detail": "Invalid CSRF token"}, 403)
            rejection = "security.csrf.rejected"
        elif (request.method not in {"GET", "HEAD", "OPTIONS"}
              and path != "/api/admin/logout" and session.role not in WRITE_ROLES):
            response = JSONResponse({"detail": "This role has read-only access"}, 403)
            rejection = "security.role.rejected"
    if response is None:
        action = ADMIN_ACTIONS.get((request.method, path))
        if path == "/api/admin/documents" and request.method == "GET":
            action = "documents.inspect"
        elif path.startswith("/api/admin/documents/"):
            if request.method == "GET" and path.endswith("/download"):
                action = "documents.download"
            elif request.method == "POST" and path.endswith("/archive"):
                action = "documents.archive"
            elif request.method == "POST" and path.endswith("/reindex"):
                action = "documents.reindex"
        if path.startswith('/api/admin/ingestion') and request.method == 'POST':
            suffix = path.rsplit('/', 1)[-1]
            action = 'ingestion.' + (suffix if suffix in {'cancel', 'publish', 'restore'} else 'start')
        started, status = perf_counter(), 500
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            if action and session:
                append_safely(request.app.state.diagnostics, {"kind": "admin", "action": action,
                    "outcome": "ok" if status < 400 else "error", "status": status,
                    "duration_ms": round((perf_counter() - started) * 1000),
                    **getattr(request.state, "admin_diagnostic_fields", {}),
                    **({"source_id": request.state.admin_source_id}
                       if getattr(request.state, "admin_source_id", None) else {})})
                _audit(request, session, action, "success" if status < 400 else "error", status)
    event = getattr(request.state, "security_audit_event", None)
    if event:
        attributed_session = (getattr(request.state, "admin_session", None)
                              if event.get("attributed") else None)
        _audit(request, attributed_session,
               event["action"], event["outcome"], event["status"])
    elif rejection:
        _audit(request, session, rejection, "rejected", response.status_code)
    elif path == "/api/admin/login" and response.status_code >= 400:
        _audit(request, None, "authentication.login", "rejected", response.status_code)
    connection = request.app.state.website.settings.api_base_url
    connect_policy = "connect-src 'self'" + (" " + connection if connection else "") + "; "
    response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                             "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                             "Content-Security-Policy": "default-src 'self'; " + connect_policy + "script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    return response


@router.get("/admin", include_in_schema=False)
@router.get("/admin/", include_in_schema=False)
async def dashboard(request: Request):
    if not request.state.admin_session:
        return RedirectResponse("/admin/login", 303)
    return FileResponse(UI / "index.html")


@router.get("/admin/login", include_in_schema=False)
async def login_page():
    return FileResponse(UI / "login.html")


@router.get("/admin/assets/{name}", include_in_schema=False)
async def asset(name: str):
    if name not in {"usage-budget.js", "api-access.js", "ingestion.js", "admin.css", "admin.js", "rag.css", "rag.js", "website.css", "website.js", "ontology.js", "ontology-aliases.js", "sources.js", "logs.js", "evaluations.css", "evaluations.js", "calendar.js", "calendar.css", "regex.js", "regex.css"}:
        return JSONResponse({"detail": "Not found"}, 404)
    return FileResponse(UI / name)


@router.post("/api/admin/login")
async def login(payload: Login, request: Request):
    result, token = await asyncio.to_thread(auth.login, payload.username, payload.password,
                                           request.client.host if request.client else "unknown")
    if result != "ok":
        status, message = {"disabled": (503, "Admin access is not configured. Set the admin credentials in .env."),
                           "limited": (429, "Too many sign-in attempts. Try again in five minutes."),
                           "invalid": (401, "Invalid username or password.")}[result]
        request.state.security_audit_event = {
            "action": "authentication.login", "outcome": "rejected",
            "status": status, "attributed": False}
        return JSONResponse({"detail": message}, status, headers={"Retry-After": "300"} if status == 429 else None)
    auth.logout(request.cookies.get(COOKIE))
    response = JSONResponse({"authenticated": True})
    request.state.admin_session = auth.session(token)
    request.state.security_audit_event = {
        "action": "authentication.login", "outcome": "success",
        "status": 200, "attributed": True}
    response.set_cookie(COOKIE, token, max_age=auth.ttl, httponly=True, secure=auth.secure,
                        samesite="strict", path="/")
    return response


@router.get("/api/admin/session")
async def identity(request: Request):
    session = request.state.admin_session
    editable = session.role in WRITE_ROLES
    return {"username": session.username, "role": session.role,
            "permissions": {"read": True, "write": editable},
            "csrf_token": session.csrf,
            "session_minutes": auth.ttl // 60,
            "sections": [{"id": key, "status": ("ready" if key in {"calendar", "logs"}
                                                     else "editable" if editable else "read-only")}
                         for key in ("website", "configuration", "rag", "sources", "ontology", "evaluations",
                                      "regex", "calendar", "logs")]}


@router.get("/api/admin/regex")
async def regex_status(request: Request):
    return request.app.state.regex_rules.status()


@router.post("/api/admin/regex/preview")
async def regex_preview(payload: RegexPreview, request: Request):
    return request.app.state.regex_rules.preview(payload.phrase, payload.language)


@router.post("/api/admin/regex/rules")
async def regex_change(change: RegexChange, request: Request):
    request.state.admin_diagnostic_fields = {"rule_id": change.rule.rule_id,
                                             "operation": change.operation}
    try:
        return await asyncio.to_thread(request.app.state.regex_rules.change, change)
    except RegexConflict as problem:
        return JSONResponse({"detail": str(problem)}, 409)
    except (RegexValidationError, ValueError, TypeError) as problem:
        return JSONResponse({"detail": str(problem)}, 422)
    except OSError:
        return JSONResponse({"detail": "Intent rules could not be saved. Active rules are unchanged."}, 503)


@router.get("/api/admin/calendar")
async def calendar_status(request: Request, contact_id: str | None = Query(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"),
        week_start: str | None = Query(default=None, min_length=10, max_length=10,
                                       pattern=r"^\d{4}-\d{2}-\d{2}$")):
    try:
        return await asyncio.to_thread(request.app.state.calendar.status, contact_id, week_start)
    except (AppointmentError, ValueError, TypeError):
        return JSONResponse({"detail": "Approved calendar contact not found"}, 404)


@router.get("/api/admin/logs")
async def diagnostic_logs(request: Request, kind: Literal["all", "chat", "admin"] = "all",
                          limit: int = Query(default=100, ge=1, le=100)):
    return request.app.state.diagnostics.read(kind, limit)


@router.get("/api/admin/traffic-metrics")
async def traffic_metrics(request: Request):
    try:
        return request.app.state.public_limiter.metrics()
    except sqlite3.Error:
        return JSONResponse({"detail": "Traffic metrics are temporarily unavailable"}, 503)


@router.get('/api/admin/usage-budget')
async def usage_budget_status(request: Request):
    if request.state.admin_session.role != 'administrator':
        return JSONResponse({'detail':'Administrator role required'},403)
    try: return await asyncio.to_thread(request.app.state.usage_budget.status)
    except sqlite3.Error: return JSONResponse({'detail':'Usage budget is temporarily unavailable'},503)


@router.put('/api/admin/usage-budget')
async def usage_budget_save(change: UsageBudgetChange, request: Request):
    if request.state.admin_session.role != 'administrator':
        return JSONResponse({'detail':'Administrator role required'},403)
    try: return await asyncio.to_thread(request.app.state.usage_budget.save, change)
    except ValueError: return JSONResponse({'detail':'Usage budget limits are invalid'},422)
    except sqlite3.Error: return JSONResponse({'detail':'Usage budget could not be saved'},503)


@router.get("/api/admin/monitoring")
async def monitoring_status(request: Request):
    try:
        return await asyncio.to_thread(request.app.state.monitoring.status,
            request.app.state.diagnostics, request.app.state.public_limiter.metrics())
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return JSONResponse({"detail": "Monitoring status is unavailable"}, 503)


@router.put("/api/admin/monitoring")
async def monitoring_save(change: MonitoringChange, request: Request):
    try:
        await asyncio.to_thread(request.app.state.monitoring.save, change)
        return await asyncio.to_thread(request.app.state.monitoring.status,
            request.app.state.diagnostics, request.app.state.public_limiter.metrics())
    except (ValueError, TypeError):
        return JSONResponse({"detail": "Monitoring thresholds are invalid"}, 422)
    except (OSError, sqlite3.Error):
        return JSONResponse({"detail": "Monitoring thresholds could not be saved"}, 503)


@router.get("/api/admin/security-audit")
async def security_audit(request: Request, limit: int = Query(default=100, ge=1, le=100)):
    try:
        return await asyncio.to_thread(request.app.state.security_audit.read, limit)
    except OSError:
        return JSONResponse({"detail": "Security audit is unavailable"}, 503)


@router.get("/api/admin/ontology")
async def ontology_status(request: Request):
    return await asyncio.to_thread(request.app.state.ontology.status)


@router.get("/api/admin/sources")
async def source_status(request: Request):
    return await asyncio.to_thread(request.app.state.sources.status)


@router.get("/api/admin/documents")
async def document_status(request: Request):
    try:
        return await asyncio.to_thread(request.app.state.sources.documents_status)
    except (KnowledgeValidationError, ValueError, TypeError, OSError):
        return JSONResponse({"detail": "Approved documents could not be inspected safely"}, 422)


@router.get("/api/admin/documents/{document_id}/download")
async def document_download(document_id: str, request: Request):
    if len(document_id) != 32 or any(character not in "0123456789abcdef" for character in document_id):
        return JSONResponse({"detail": "Approved document not found"}, 404)
    try:
        path, filename, media_type = await asyncio.to_thread(
            request.app.state.sources.download, document_id)
        return FileResponse(path, media_type=media_type, filename=filename)
    except SourceConflict:
        return JSONResponse({"detail": "Approved document not found"}, 404)
    except (KnowledgeValidationError, ValueError, TypeError, OSError):
        return JSONResponse({"detail": "Approved document could not be downloaded safely"}, 422)


@router.post("/api/admin/documents/{document_id}/archive")
async def document_archive(document_id: str, payload: DocumentArchiveRequest, request: Request):
    try:
        return await asyncio.to_thread(request.app.state.sources.archive_document, document_id,
            payload, request.cookies.get(COOKIE, ""))
    except SourceConflict as problem:
        return JSONResponse({"detail": str(problem)}, 409)
    except (KnowledgeValidationError, ValueError, TypeError) as problem:
        return JSONResponse({"detail": str(problem)}, 422)
    except OSError:
        return JSONResponse({"detail": "Document could not be archived. Previous live knowledge remains active."}, 503)


@router.post("/api/admin/documents/{document_id}/reindex")
async def document_reindex(document_id: str, payload: DocumentReindexRequest, request: Request):
    try:
        return await asyncio.to_thread(request.app.state.sources.reindex_document,
                                       document_id, payload)
    except SourceConflict as problem:
        return JSONResponse({"detail": str(problem)}, 409)
    except (KnowledgeValidationError, ValueError, TypeError, OSError):
        return JSONResponse({"detail": "Document re-index failed. Previous live knowledge remains active."}, 422)


@router.post("/api/admin/sources/import-preview")
async def source_import_preview(request: Request):
    filename = request.headers.get("x-website-filename", "")
    limit = import_byte_limit(filename)
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) < 0:
                return JSONResponse({"detail": "Import content length is invalid"}, 422)
            if int(declared) > limit:
                return JSONResponse({"detail": import_limit_message(filename)}, 413)
        except ValueError:
            return JSONResponse({"detail": "Import content length is invalid"}, 422)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return JSONResponse({"detail": import_limit_message(filename)}, 413)
    try:
        return await asyncio.to_thread(request.app.state.sources.stage_import,
            filename,
            request.headers.get("content-type", ""), bytes(body), request.cookies.get(COOKIE, ""))
    except (KnowledgeValidationError, ValueError, TypeError) as problem:
        return JSONResponse({"detail": str(problem)}, 422)


@router.post("/api/admin/sources/changes")
async def source_change(change: SourceChange, request: Request):
    request.state.admin_source_id = change.source_id or change.record.source_id
    try:
        return await asyncio.to_thread(request.app.state.sources.change, change,
                                       request.cookies.get(COOKIE, ""))
    except SourceConflict as problem:
        return JSONResponse({"detail": str(problem)}, 409)
    except (KnowledgeValidationError, ValueError, KeyError, TypeError) as problem:
        return JSONResponse({"detail": str(problem)}, 422)
    except OSError:
        return JSONResponse({"detail": "Approved sources could not be saved. The previous live index remains active."}, 503)


@router.post("/api/admin/sources/refresh-preview")
async def source_refresh_preview(payload: SourceFetchRequest, request: Request):
    request.state.admin_source_id = payload.source_id
    try:
        return await asyncio.to_thread(request.app.state.sources.preview_refresh, payload,
                                       request.cookies.get(COOKIE, ""))
    except SourceConflict as problem:
        return JSONResponse({"detail": str(problem)}, 409)
    except (KnowledgeValidationError, ValueError, KeyError, TypeError) as problem:
        return JSONResponse({"detail": str(problem)}, 422)
    except OSError:
        return JSONResponse({"detail": "Approved source refresh could not read the current registry."}, 503)


@router.post("/api/admin/sources/refresh-apply")
async def source_refresh_apply(payload: SourceRefreshApproval, request: Request):
    try:
        result = await asyncio.to_thread(request.app.state.sources.apply_refresh, payload,
                                         request.cookies.get(COOKIE, ""))
        request.state.admin_source_id = result.pop("changed_source_id")
        return result
    except SourceConflict as problem:
        return JSONResponse({"detail": str(problem)}, 409)
    except (KnowledgeValidationError, ValueError, KeyError, TypeError) as problem:
        return JSONResponse({"detail": str(problem)}, 422)
    except OSError:
        return JSONResponse({"detail": "Refreshed source could not be activated. The previous live index remains active."}, 503)


@router.post("/api/admin/ontology/relationships")
async def ontology_change(change: OntologyChange, request: Request):
    try:
        return await asyncio.to_thread(request.app.state.ontology.change, change)
    except OntologyConflict as problem:
        return JSONResponse({"detail": str(problem)}, 409)
    except OntologyValidationError as problem:
        return JSONResponse({"detail": str(problem)}, 422)
    except OSError:
        return JSONResponse({"detail": "Ontology could not be saved. Active relationships are unchanged."}, 503)


@router.post("/api/admin/ontology/aliases")
async def ontology_alias_change(change: OntologyAliasChange, request: Request):
    try:
        return await asyncio.to_thread(request.app.state.ontology.change_alias, change)
    except OntologyConflict as problem:
        return JSONResponse({"detail": str(problem)}, 409)
    except OntologyValidationError as problem:
        return JSONResponse({"detail": str(problem)}, 422)
    except OSError:
        return JSONResponse({"detail": "Ontology alias could not be saved. Active aliases are unchanged."}, 503)


@router.get("/api/admin/website")
async def website_status(request: Request):
    return request.app.state.website.status(f"{request.url.scheme}://{request.url.netloc}")


@router.put("/api/admin/website")
async def website_save(settings: WebsiteSettings, request: Request):
    try:
        await asyncio.to_thread(request.app.state.website.save, settings)
    except OSError:
        return JSONResponse({"detail": "Website settings could not be saved. Active settings are unchanged."}, 503)
    return request.app.state.website.status(f"{request.url.scheme}://{request.url.netloc}")


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=2, max_length=500)
    language: Literal["sv", "en"] = "sv"
    category: str | None = Field(default=None, max_length=100)


class EvaluationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    case_id: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]+$")


class LiveEvaluationSuiteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmed_live_model_calls: Literal[True]
    expected_case_count: int = Field(ge=1, le=100)


class ProviderProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmed_model_request: Literal[True]


class EmbeddingRebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    confirmed_external_embedding_calls: Literal[True]
    expected_model: str = Field(min_length=1, max_length=200)
    expected_chunk_count: int = Field(ge=1, le=3000)


@router.get("/api/admin/evaluations/manage")
async def evaluation_manage(request: Request):
    suite = request.app.state.evaluation_store.load()
    return {"revision": int(suite.version), "cases": [case.model_dump() for case in suite.cases]}


@router.post("/api/admin/evaluations/manage")
async def evaluation_change(payload: EvaluationChange, request: Request):
    try:
        suite = await request.app.state.evaluations.change(payload)
    except (EvaluationConflict, EvaluationAlreadyRunning) as problem:
        return JSONResponse({"detail": str(problem)}, 409)
    except ValueError as problem:
        return JSONResponse({"detail": str(problem)}, 422)
    request.state.admin_diagnostic_fields = {"case_id": payload.case_id}
    return {"revision": int(suite.version), "cases": [case.model_dump() for case in suite.cases]}


@router.get("/api/admin/evaluations/cases")
async def evaluation_cases(request: Request):
    return request.app.state.evaluations.status()


@router.post("/api/admin/evaluations/run")
async def evaluation_run(payload: EvaluationRunRequest, request: Request):
    try:
        result = await request.app.state.evaluations.run_case(payload.case_id)
    except EvaluationCaseNotFound:
        return JSONResponse({"detail": "Evaluation case not found"}, 404)
    except EvaluationAlreadyRunning:
        return JSONResponse({"detail": "Evaluation case is already running"}, 409)
    request.state.admin_diagnostic_fields = {
        "case_id": result["case_id"],
        "passed": result["passed"],
        "evaluation_duration_ms": result["duration_ms"],
        "model_call_count": result["model_call_count"],
        "failure_codes": result["failure_codes"],
    }
    return result


@router.post("/api/admin/evaluations/run-non-llm")
async def evaluation_run_non_llm(request: Request):
    try:
        result = await request.app.state.evaluations.run_non_llm_suite()
    except EvaluationAlreadyRunning:
        return JSONResponse({"detail": "Non-LLM evaluation suite is already running"}, 409)
    request.state.admin_diagnostic_fields = {
        "suite_version": result["suite_version"],
        "passed": result["passed"],
        "total_cases": result["total_cases"],
        "completed_cases": result["completed_cases"],
        "passed_cases": result["passed_cases"],
        "failed_cases": result["failed_cases"],
        "evaluation_duration_ms": result["duration_ms"],
        "model_call_count": result["model_call_count"],
        "failure_codes": result["failure_codes"],
        "failed_case_codes": result["failed_case_codes"],
    }
    return result


@router.post("/api/admin/evaluations/run-full")
async def evaluation_run_full(payload: LiveEvaluationSuiteRequest, request: Request):
    try:
        result = await request.app.state.evaluations.run_full_suite(payload.expected_case_count)
    except EvaluationAlreadyRunning:
        return JSONResponse({"detail": "An evaluation suite is already running"}, 409)
    except EvaluationSuiteChanged as problem:
        return JSONResponse({"detail": str(problem)}, 409)
    request.state.admin_diagnostic_fields = {
        "suite_version": result["suite_version"],
        "suite_kind": result["suite_kind"],
        "passed": result["passed"],
        "total_cases": result["total_cases"],
        "completed_cases": result["completed_cases"],
        "passed_cases": result["passed_cases"],
        "failed_cases": result["failed_cases"],
        "evaluation_duration_ms": result["duration_ms"],
        "model_call_count": result["model_call_count"],
        "failure_codes": result["failure_codes"],
        "failed_case_codes": result["failed_case_codes"],
    }
    return result


def _rag_status(request: Request):
    result = request.app.state.rag.status()
    result["provider"] = request.app.state.provider_probe.status()
    result["generation_configuration"] = request.app.state.provider_configuration.public_status()
    result["embedding_configuration"] = request.app.state.embedding_configuration.public_status()
    result["intent_classification"] = request.app.state.service.intent_classifier_status()
    return result


@router.get("/api/admin/rag")
async def rag_status(request: Request):
    return _rag_status(request)


@router.post("/api/admin/provider/probe")
async def provider_probe(payload: ProviderProbeRequest, request: Request):
    try:
        result = await request.app.state.provider_probe.run()
    except ProviderProbeBusy:
        return JSONResponse({"detail": "A provider test is already running"}, 409)
    request.state.admin_diagnostic_fields = {
        "provider_outcome": result["outcome"],
        "provider_duration_ms": result["duration_ms"],
    }
    return result


@router.get("/api/admin/embeddings/rebuild-preview")
async def embedding_rebuild_preview(request: Request):
    return await asyncio.to_thread(request.app.state.rag.embedding_rebuild_preview)


@router.post("/api/admin/embeddings/probe")
async def embedding_provider_probe(payload: EmbeddingProbeRequest, request: Request):
    result = await asyncio.to_thread(request.app.state.embedding_probe.run)
    request.state.admin_diagnostic_fields = {"embedding_provider_outcome": result["outcome"],
        "embedding_provider_duration_ms": result["duration_ms"]}
    return result


@router.put("/api/admin/embeddings/settings")
async def embedding_settings_save(request: Request):
    declared = request.headers.get("content-length")
    try:
        if declared is not None and (int(declared) < 0 or int(declared) > 4096):
            return JSONResponse({"detail": "Embedding configuration request is too large"}, 413)
    except ValueError:
        return JSONResponse({"detail": "Embedding configuration is invalid"}, 422)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            return JSONResponse({"detail": "Embedding configuration request is too large"}, 413)
    try:
        change = EmbeddingProviderChange.model_validate(json.loads(body))
        await asyncio.to_thread(request.app.state.embedding_configuration.save, change,
                                request.app.state.rag)
        return _rag_status(request)
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
        return JSONResponse({"detail": "Embedding configuration is invalid"}, 422)
    except OSError:
        return JSONResponse({"detail": "Embedding configuration could not be saved. The previous provider remains active."}, 503)


@router.post("/api/admin/embeddings/rebuild")
async def embedding_rebuild(payload: EmbeddingRebuildRequest, request: Request):
    try:
        result = await asyncio.to_thread(request.app.state.rag.rebuild_embeddings,
            payload.expected_model, payload.expected_chunk_count)
        request.state.admin_diagnostic_fields = {"embedding_chunk_count": result["chunk_count"],
            "embedding_dimensions": result["dimensions"], "embedding_batch_count": result["batch_count"]}
        return result
    except EmbeddingRebuildBusy:
        return JSONResponse({"detail": "Embedding rebuild is already running"}, 409)
    except EmbeddingRebuildConflict as problem:
        return JSONResponse({"detail": str(problem)}, 409)
    except UsageBudgetExceeded:
        return JSONResponse({"detail": "Embedding usage budget exhausted"}, 429)
    except Exception:
        return JSONResponse({"detail": "Embedding rebuild failed. The previous vector cache remains active."}, 502)


@router.put("/api/admin/provider/settings")
async def provider_settings_save(request: Request):
    declared = request.headers.get("content-length")
    try:
        if declared is not None and (int(declared) < 0 or int(declared) > 4096):
            return JSONResponse({"detail": "Model configuration request is too large"}, 413)
    except ValueError:
        return JSONResponse({"detail": "Model configuration is invalid"}, 422)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            return JSONResponse({"detail": "Model configuration request is too large"}, 413)
    try:
        change = GenerationProviderChange.model_validate(json.loads(body))
        await asyncio.to_thread(request.app.state.provider_configuration.save, change,
                                request.app.state.provider_probe.service)
        return _rag_status(request)
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
        return JSONResponse({"detail": "Model configuration is invalid"}, 422)
    except OSError:
        return JSONResponse({"detail": "Model configuration could not be saved. The previous provider remains active."}, 503)


@router.put("/api/admin/rag/settings")
async def rag_save(settings: RagSettings, request: Request):
    try:
        await asyncio.to_thread(request.app.state.rag.save, settings)
        return _rag_status(request)
    except OSError:
        return JSONResponse({"detail": "Settings could not be saved. Active settings are unchanged."}, 503)


@router.post("/api/admin/rag/search")
async def rag_search(query: SearchQuery, request: Request):
    result = await asyncio.to_thread(request.app.state.rag.tools.search_knowledge,
                                     query.query, query.language, query.category)
    fields = {"source_id", "title", "canonical_url", "category", "language", "source_status",
              "retrieved_at", "checksum", "document_version", "content", "score",
              "lexical_score", "semantic_score", "ontology_score", "confidence"}
    fields.update({"chunk_id", "source_location"})
    result["records"] = [{key: value for key, value in record.items() if key in fields}
                         for record in result["records"]]
    return result


@router.post("/api/admin/rag/rebuild")
async def rag_rebuild(request: Request):
    try:
        await asyncio.to_thread(request.app.state.rag.rebuild)
        return _rag_status(request)
    except (KnowledgeValidationError, ValueError, KeyError, TypeError, OSError):
        return JSONResponse({"detail": "Index rebuild failed validation or storage checks. The previous live index remains active."}, 422)


@router.post("/api/admin/logout")
async def logout(request: Request):
    request.state.security_audit_event = {
        "action": "authentication.logout", "outcome": "success",
        "status": 200, "attributed": True}
    auth.logout(request.cookies.get(COOKIE))
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE, path="/", secure=auth.secure, httponly=True, samesite="strict")
    return response
