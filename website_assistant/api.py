"""Phase A foundation: isolated startup, cited retrieval, and protected status."""
import hmac
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .admin_auth import AdminAuth, COOKIE
from .knowledge import KnowledgeStore
from .settings import ROOT, Settings
from .service import AssistantService
from .rate_limit import RateLimiter
from .routing import Change, Preview, RuleConflict
from .build_jobs import DiscoveryJobs, StartDiscovery, JobConflict
from .ontology_review import OntologyReviews, ReviewChange, ReviewConflict, ItemReviewChange
from .versions import Versions, ActivateVersion, RestoreVersion, VersionConflict


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    language: Literal["en", "sv"] = "en"

    @field_validator("message")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Message must not be blank")
        return value.strip()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.load()
    knowledge = KnowledgeStore(settings.data_path / "sources.json", home_url=settings.home_url)
    auth = AdminAuth(settings)
    service = AssistantService(knowledge, settings)
    jobs = DiscoveryJobs(settings.data_path)
    reviews = OntologyReviews(settings.data_path)
    versions = Versions(settings, jobs, reviews, knowledge)
    jobs.on_complete = versions.completed
    chat_limiter, probe_limiter = RateLimiter(settings.chat_rate_limit), RateLimiter(3)
    app = FastAPI(title=settings.name, version="0.1.0", docs_url=None, redoc_url=None)
    app.state.knowledge = knowledge
    app.state.auth = auth
    app.state.service = service
    app.state.discovery_jobs = jobs
    app.state.versions = versions
    # Valid, empty graph: generation and persistence are implemented in Phase D.
    app.state.ontology = {"version": 1, "entities": [], "relationships": [], "aliases": []}
    app.state.contacts = []
    app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, error):
        return JSONResponse({"detail": "Storage is temporarily unavailable."}, status_code=503)

    @app.exception_handler(VersionConflict)
    async def invalid_version(request, error):
        return JSONResponse({"detail": "Active knowledge is temporarily unavailable."}, status_code=503)

    @app.middleware("http")
    async def browser_boundary(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if (origin and origin != str(request.base_url).rstrip("/")) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site requests are disabled."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def require_admin(request: Request):
        session = auth.session(request.cookies.get(COOKIE))
        if session is None:
            raise HTTPException(401, "Administrator sign-in required.")
        return session

    def require_editor(request: Request, session=Depends(require_admin)):
        if session.role not in {"editor", "administrator"}:
            raise HTTPException(403, "Editor access required.")
        if not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), session.csrf.encode()):
            raise HTTPException(403, "Invalid request token.")
        return session

    @app.get("/api/admin/discovery")
    def discovery_list(session=Depends(require_admin)):
        return {"jobs": jobs.list()}

    @app.post("/api/admin/discovery", status_code=202)
    def discovery_start(body: StartDiscovery, session=Depends(require_editor)):
        try:
            return jobs.start(body)
        except JobConflict as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/admin/discovery/{job_id}")
    def discovery_get(job_id: str, session=Depends(require_admin)):
        try:
            return jobs.get(job_id)
        except KeyError:
            raise HTTPException(404, "Discovery job not found.")

    @app.post("/api/admin/discovery/{job_id}/cancel")
    def discovery_cancel(job_id: str, session=Depends(require_editor)):
        try:
            return jobs.cancel(job_id)
        except KeyError:
            raise HTTPException(404, "Discovery job not found.")

    @app.get("/api/admin/discovery/{job_id}/ontology")
    def discovery_ontology(job_id: str, session=Depends(require_admin)):
        try:
            return jobs.ontology(job_id)
        except KeyError:
            raise HTTPException(404, "Discovery job not found.")
        except JobConflict as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/admin/discovery/{job_id}/ontology/reviews")
    def ontology_reviews(job_id: str, session=Depends(require_admin)):
        return reviews.inspect(discovery_ontology(job_id, session))

    @app.post("/api/admin/discovery/{job_id}/ontology/reviews")
    def ontology_review_change(job_id: str, body: ReviewChange, session=Depends(require_editor)):
        graph = discovery_ontology(job_id, session)
        try:
            return reviews.change(graph, body, session.username, job_id)
        except ReviewConflict as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/admin/discovery/{job_id}/ontology/item-reviews")
    def ontology_item_review_change(job_id: str, body: ItemReviewChange, session=Depends(require_editor)):
        graph = discovery_ontology(job_id, session)
        try:
            return reviews.change_item(graph, body, session.username, job_id)
        except ReviewConflict as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/admin/versions")
    def version_status(session=Depends(require_admin)):
        return versions.status()

    @app.post("/api/admin/discovery/{job_id}/activate")
    def activate_build(job_id: str, body: ActivateVersion, session=Depends(require_editor)):
        try:
            return versions.activate(job_id, body, session.username)
        except VersionConflict as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/admin/versions/{version_id}/restore")
    def restore_version(version_id: str, body: RestoreVersion, session=Depends(require_editor)):
        try:
            return versions.restore(version_id, body, session.username)
        except VersionConflict as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/admin/routing")
    def routing_status(session=Depends(require_admin)):
        return service.routing.status()

    @app.post("/api/admin/routing/preview")
    def routing_preview(body: Preview, session=Depends(require_editor)):
        return service.routing.preview(body.phrase, body.language)

    @app.post("/api/admin/routing")
    def routing_change(body: Change, session=Depends(require_editor)):
        try:
            return service.routing.change(body)
        except RuleConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web" / "index.html")

    @app.get("/admin")
    def admin():
        return FileResponse(ROOT / "web" / "admin.html")

    @app.get("/healthz")
    def health():
        return {"status": "healthy", "knowledge_ready": bool(versions.current().knowledge.records)}

    @app.get("/api/status")
    def public_status():
        snapshot = versions.current()
        return {"name": settings.name, "knowledge_ready": bool(snapshot.knowledge.records),
                "source_count": len(snapshot.knowledge.records), "active_version":snapshot.version_id, "appointments_enabled": False,
                "crawl_available": False}

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request):
        if not chat_limiter.allow(request.client.host if request.client else "unknown"):
            raise HTTPException(429, "Too many requests. Try again shortly.", headers={"Retry-After": "60"})
        snapshot = versions.current()
        result = await service.respond(body.message, body.language, knowledge=snapshot.knowledge)
        return {**result, "knowledge_version":snapshot.version_id}

    @app.post("/api/admin/login")
    def login(body: LoginRequest, request: Request, response: Response):
        outcome, token = auth.login(body.username, body.password, request.client.host if request.client else "unknown")
        if outcome != "ok":
            raise HTTPException({"disabled": 503, "limited": 429}.get(outcome, 401),
                                "Sign-in unavailable." if outcome == "disabled" else "Sign-in failed.")
        response.set_cookie(COOKIE, token, max_age=auth.ttl, secure=auth.secure,
                            httponly=True, samesite="strict", path="/")
        return {"signed_in": True}

    @app.get("/api/admin/status")
    def admin_status(session=Depends(require_admin)):
        snapshot = versions.current()
        return {"username": session.username, "role": session.role, "csrf": session.csrf,
                "source_count": len(snapshot.knowledge.records), "ontology": snapshot.ontology,
                "active_version":snapshot.version_id, "home_url": snapshot.home_url, "stage": "Phase E: versioned knowledge",
                "provider": service.status(),
                "crawl_available": True}

    @app.post("/api/admin/provider/probe")
    async def provider_probe(request: Request, session=Depends(require_admin)):
        if session.role not in {"editor", "administrator"}:
            raise HTTPException(403, "Editor access required.")
        if not hmac.compare_digest(request.headers.get("x-csrf-token", "").encode(), session.csrf.encode()):
            raise HTTPException(403, "Invalid request token.")
        if not probe_limiter.allow(session.username):
            raise HTTPException(429, "Connection test limit reached.", headers={"Retry-After": "60"})
        return await service.probe()

    @app.post("/api/admin/logout")
    def logout(request: Request, response: Response, session=Depends(require_admin)):
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf):
            raise HTTPException(403, "Invalid request token.")
        auth.logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"signed_in": False}

    return app


def main():
    import uvicorn
    settings = Settings.load()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, proxy_headers=False)


if __name__ == "__main__":
    main()
