"""Durable discovery jobs with cross-process exclusion and staged page storage."""
import json
import threading
import time
import re
from contextlib import closing
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .crawl_policy import CrawlLimits, normalize_url
from .crawler import WebsiteCrawler
from .database_maintenance import Migration, apply_migrations, open_database
from .rag_settings import atomic_json
from .extraction import extract_pages
from .chunking import build_chunks
from .ontology_builder import build_ontology, validate_ontology

ACTIVE = ("queued", "crawling")
LEASE_SECONDS = 120


class StartDiscovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    homepage: str = Field(min_length=1, max_length=2048)
    limits: CrawlLimits = Field(default_factory=CrawlLimits)

    @field_validator("homepage")
    @classmethod
    def url(cls, value):
        return normalize_url(value)


class JobConflict(ValueError):
    pass


class DiscoveryJobs:
    def __init__(self, data, *, crawler_factory=WebsiteCrawler):
        self.data = data
        self.path = data / "discovery_jobs.db"
        data.mkdir(parents=True, exist_ok=True)
        self.crawler_factory = crawler_factory
        self.threads = []
        self.on_complete = None
        with closing(open_database(self.path)) as db, db:
            def schema(database):
                database.execute("""CREATE TABLE discovery_jobs (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, homepage TEXT NOT NULL,
                    limits TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    lease_until REAL NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    progress TEXT NOT NULL, report TEXT, error TEXT)""")
            apply_migrations(db, "discovery_jobs", (Migration(1, "create discovery jobs", schema),))
        self.list()

    @staticmethod
    def recover(db):
        db.execute("UPDATE discovery_jobs SET state='interrupted',error='worker_interrupted' "
                   "WHERE state IN ('queued','crawling') AND lease_until<?", (time.time(),))

    def list(self):
        with closing(open_database(self.path)) as db, db:
            self.recover(db)
            rows = db.execute("SELECT id,state,homepage,created,updated,progress,error,cancel_requested,report FROM discovery_jobs ORDER BY created DESC LIMIT 20").fetchall()
        return [{"id": r[0], "state": r[1], "homepage": r[2], "created": r[3], "updated": r[4],
                 "progress": json.loads(r[5]), "error": r[6], "cancel_requested": bool(r[7]),
                 "activation_pending": bool(r[8] and json.loads(r[8]).get("activation",{}).get("state") == "pending" and r[4]+LEASE_SECONDS > time.time())} for r in rows]

    def get(self, job_id):
        with closing(open_database(self.path)) as db, db:
            self.recover(db)
            row = db.execute("SELECT state,homepage,limits,progress,report,error,cancel_requested,created FROM discovery_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return {"id":job_id, "state":row[0], "homepage":row[1], "limits":json.loads(row[2]),
                "progress":json.loads(row[3]), "report":json.loads(row[4]) if row[4] else None,
                "error":row[5], "cancel_requested":bool(row[6]), "created":row[7]}

    def ontology(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}",job_id):
            raise KeyError(job_id)
        job = self.get(job_id)
        if job["state"] != "complete" or not (job["report"] or {}).get("ontology"):
            raise JobConflict("This job has no completed ontology. Run discovery again.")
        directory = self.data / "builds" / job_id
        try:
            path = directory / "ontology.json"
            if path.stat().st_size > 10_000_000:
                raise ValueError("Ontology file exceeds the size limit")
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = json.loads((directory / "sources.json").read_text(encoding="utf-8"))["records"]
            return validate_ontology(payload,records)
        except (OSError,ValueError,KeyError,TypeError) as error:
            raise JobConflict("Saved ontology cannot be validated. Run discovery again.") from error

    def start(self, request):
        job_id, now = uuid4().hex, time.time()
        with closing(open_database(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.recover(db)
            if db.execute("SELECT 1 FROM discovery_jobs WHERE state IN ('queued','crawling')").fetchone():
                raise JobConflict("A discovery job is already running.")
            db.execute("INSERT INTO discovery_jobs(id,state,homepage,limits,created,updated,lease_until,progress) VALUES(?,?,?,?,?,?,?,?)",
                (job_id,"queued",request.homepage,request.limits.model_dump_json(),now,now,now+LEASE_SECONDS,
                 json.dumps({"requests":0,"page_count":0,"skipped_count":0})))
        thread = threading.Thread(target=self._run, args=(job_id, request), daemon=True, name="website-discovery")
        self.threads = [item for item in self.threads if item.is_alive()]
        self.threads.append(thread)
        try:
            thread.start()
        except RuntimeError:
            self._finish(job_id, "failed", None, "worker_start_failed")
            raise
        return self.get(job_id)

    def cancel(self, job_id):
        with closing(open_database(self.path)) as db, db:
            if not db.execute("SELECT 1 FROM discovery_jobs WHERE id=?", (job_id,)).fetchone():
                raise KeyError(job_id)
            db.execute("UPDATE discovery_jobs SET cancel_requested=1 WHERE id=? AND state IN ('queued','crawling')", (job_id,))
        return self.get(job_id)

    def _cancelled(self, job_id):
        with closing(open_database(self.path)) as db:
            row = db.execute("SELECT state,cancel_requested,lease_until FROM discovery_jobs WHERE id=?", (job_id,)).fetchone()
        return row is None or row[0] not in ACTIVE or bool(row[1]) or row[2] < time.time()

    def _finish(self, job_id, state, report, error=None):
        with closing(open_database(self.path)) as db, db:
            db.execute("UPDATE discovery_jobs SET state=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE ? END, "
                       "report=?,error=?,updated=? WHERE id=? AND state IN ('queued','crawling') AND lease_until>=?",
                       (state,json.dumps(report) if report else None,error,time.time(),job_id,time.time()))

    def _run(self, job_id, request):
        last_update = [0.0]
        def progress(value):
            now = time.time()
            if now - last_update[0] < 1:
                return
            with closing(open_database(self.path)) as db, db:
                db.execute("UPDATE discovery_jobs SET state='crawling',progress=?,updated=?,lease_until=? "
                           "WHERE id=? AND state IN ('queued','crawling') AND lease_until>=?",
                           (json.dumps(value),now,now+LEASE_SECONDS,job_id,now))
            last_update[0] = now
        try:
            result = self.crawler_factory(request.limits).run(request.homepage,
                cancelled=lambda:self._cancelled(job_id), progress=progress)
            if self._cancelled(job_id):
                self._finish(job_id,"cancelled",None)
                return
            directory = self.data / "builds" / job_id
            directory.mkdir(parents=True, exist_ok=True)
            pages = []
            for index, page in enumerate(result["pages"]):
                progress({"requests":result["requests"],"page_count":result["page_count"],"skipped_count":len(result["skipped"])})
                if self._cancelled(job_id):
                    self._finish(job_id,"cancelled",None)
                    return
                filename = f"page-{index:04d}.html"
                (directory / filename).write_text(page["html"],encoding="utf-8")
                pages.append({"url":page["url"], "depth":page["depth"], "file":filename,
                              "fetched_at":page.get("fetched_at"),"encoding":page.get("encoding","utf-8")})
            def extraction_checkpoint():
                progress({"requests":result["requests"],"page_count":result["page_count"],
                          "skipped_count":len(result["skipped"]),"stage":"extracting"})
                if self._cancelled(job_id):
                    raise ValueError("cancelled")
            last_update[0] = 0
            records, extraction = extract_pages(result["pages"], result.get("canonical_homepage") or request.homepage,
                                                checkpoint=extraction_checkpoint)
            chunks = build_chunks(records,directory)
            extraction["chunk_count"] = len(chunks)
            extraction_checkpoint()
            atomic_json(directory / "sources.json", {"records":records})
            atomic_json(directory / "chunks.json", {"chunks":chunks})
            atomic_json(directory / "extraction.json", extraction)
            def ontology_checkpoint():
                progress({"requests":result["requests"],"page_count":result["page_count"],
                          "skipped_count":len(result["skipped"]),"stage":"building_ontology"})
                if self._cancelled(job_id):
                    raise ValueError("cancelled")
            last_update[0] = 0
            ontology = build_ontology(records,checkpoint=ontology_checkpoint)
            ontology_checkpoint()
            atomic_json(directory / "ontology.json",ontology)
            report = {**result,"pages":pages}
            report["extraction"] = extraction
            report["ontology"] = {"entity_count":len(ontology["entities"]),"relationship_count":len(ontology["relationships"]),
                                  "alias_count":len(ontology["aliases"]),"truncated":ontology["truncated"],
                                  "validation":"passed","review_status":"automated"}
            atomic_json(directory / "discovery.json", report)
            # File publication is staging only, never an active-knowledge switch.
            state = "complete" if result["page_count"] and result["status"] in {"complete","page_limit","time_limit"} else "failed"
            if result["status"] == "cancelled":
                state = "cancelled"
            if state == "complete" and self.on_complete:
                report["activation"] = {"state":"pending","reason":"Activation is being checked. Refresh versions if the worker was interrupted."}
            last_update[0] = 0
            progress({"requests":result["requests"],"page_count":result["page_count"],"skipped_count":len(result["skipped"])})
            self._finish(job_id,state,report,result.get("error"))
            if self.on_complete and self.get(job_id)["state"] == "complete":
                try:
                    report["activation"] = self.on_complete(job_id)
                except Exception:
                    report["activation"] = {"state":"review_required","reason":"Activation unavailable. Current knowledge was kept unless the version transaction already completed; check version history."}
                with closing(open_database(self.path)) as db, db:
                    db.execute("UPDATE discovery_jobs SET report=? WHERE id=? AND state='complete'", (json.dumps(report),job_id))
        except Exception:
            # Do not persist provider, socket, filesystem, or page exception details.
            try:
                self._finish(job_id,"failed",None,"discovery_failed")
            except Exception:
                pass  # An unavailable store is recovered by lease expiration.

    def wait(self, timeout=5):
        for thread in self.threads:
            thread.join(timeout)
