from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo
from .database_maintenance import Migration, apply_migrations, open_database, require_columns


STOCKHOLM = ZoneInfo("Europe/Stockholm")
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
SESSION = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
log = logging.getLogger(__name__)


def _appointment_schema_v1(db):
    db.execute("""CREATE TABLE IF NOT EXISTS appointment_requests (
        request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
        created_at TEXT NOT NULL, status TEXT NOT NULL, contact_id TEXT NOT NULL,
        slot_id TEXT NOT NULL, selected_utc_time TEXT NOT NULL, display_timezone TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL, visitor_name TEXT NOT NULL, visitor_email TEXT NOT NULL,
        company_name TEXT, meeting_topic TEXT NOT NULL, preferred_language TEXT NOT NULL,
        consent_timestamp TEXT NOT NULL, demo INTEGER NOT NULL CHECK (demo = 1),
        UNIQUE(session_id, idempotency_key))""")
    db.execute("CREATE INDEX IF NOT EXISTS appointment_owner ON appointment_requests(request_id, session_id)")


def _appointment_schema_v2(db):
    db.execute("CREATE UNIQUE INDEX appointment_active_slot ON appointment_requests(slot_id) "
               "WHERE status='CONFIRMED_AS_DEMO_REQUEST'")


class AppointmentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AppointmentState(StrEnum):
    INITIAL = "INITIAL"
    CONTACT_SELECTED = "CONTACT_SELECTED"
    SLOT_SELECTED = "SLOT_SELECTED"
    DETAILS_REQUIRED = "DETAILS_REQUIRED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CONFIRMED_AS_DEMO_REQUEST = "CONFIRMED_AS_DEMO_REQUEST"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class Slot:
    slot_id: str
    contact_id: str
    starts_at: str
    display_starts_at: str
    timezone: str = "Europe/Stockholm"
    duration_minutes: int = 30
    status: str = "available"


class CalendarProvider:
    def get_availability(self, contact_id: str, now: datetime | None = None) -> list[Slot]:
        raise NotImplementedError

    def create_event(self, *args, **kwargs) -> None:
        raise NotImplementedError

    def cancel_event(self, *args, **kwargs) -> None:
        raise NotImplementedError


class DemoCalendarProvider(CalendarProvider):
    """Deterministic local slots. It never contacts an external calendar."""

    HOURS = (time(9, 0), time(13, 30))

    def __init__(self, approved_contact_ids: set[str]) -> None:
        self.approved_contact_ids = approved_contact_ids

    @staticmethod
    def _id(contact_id: str, starts_at: datetime) -> str:
        digest = hashlib.sha256(f"demo|{contact_id}|{starts_at.isoformat()}".encode()).hexdigest()[:16]
        return f"slot-{digest}"

    def get_availability(self, contact_id: str, now: datetime | None = None) -> list[Slot]:
        if contact_id not in self.approved_contact_ids:
            raise AppointmentError("invalid_contact", "Contact is not in the approved directory")
        current = (now or datetime.now(UTC)).astimezone(STOCKHOLM)
        day = current.date() + timedelta(days=1)
        slots: list[Slot] = []
        while len(slots) < 10:
            if day.weekday() < 5:
                for clock in self.HOURS:
                    local = datetime.combine(day, clock, STOCKHOLM)
                    utc = local.astimezone(UTC)
                    slots.append(Slot(self._id(contact_id, utc), contact_id, utc.isoformat().replace("+00:00", "Z"), local.isoformat()))
            day += timedelta(days=1)
        return slots

    def create_event(self, *args, **kwargs) -> None:
        raise AppointmentError("demo_only", "DemoCalendarProvider does not create calendar events")

    def cancel_event(self, *args, **kwargs) -> None:
        raise AppointmentError("demo_only", "DemoCalendarProvider does not cancel calendar events")


class AppointmentStore:
    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = open_database(self.path, row_factory=sqlite3.Row)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            apply_migrations(db, "appointments", (
                Migration(1, "create appointment requests", _appointment_schema_v1),
                Migration(2, "protect active appointment slots", _appointment_schema_v2)))
            require_columns(db, "appointment_requests", {"request_id", "session_id",
                "idempotency_key", "created_at", "status", "contact_id", "slot_id",
                "selected_utc_time", "display_timezone", "duration_minutes", "visitor_name",
                "visitor_email", "company_name", "meeting_topic", "preferred_language",
                "consent_timestamp", "demo"})

    def create(self, record: dict) -> tuple[dict, bool]:
        try:
            with self._lock, self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT * FROM appointment_requests WHERE session_id=? AND idempotency_key=?", (record["session_id"], record["idempotency_key"])).fetchone()
                if existing:
                    return dict(existing), False
                occupied = db.execute("SELECT 1 FROM appointment_requests WHERE slot_id=? AND status=?", (record["slot_id"], AppointmentState.CONFIRMED_AS_DEMO_REQUEST)).fetchone()
                if occupied:
                    raise AppointmentError("slot_unavailable", "The slot was selected by another demo session")
                columns = ",".join(record)
                placeholders = ",".join("?" for _ in record)
                db.execute(f"INSERT INTO appointment_requests ({columns}) VALUES ({placeholders})", tuple(record.values()))
                return record, True
        except sqlite3.Error as problem:
            raise AppointmentError("storage_unavailable", "Appointment storage is temporarily unavailable") from problem

    def get(self, request_id: str, session_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM appointment_requests WHERE request_id=? AND session_id=?", (request_id, session_id)).fetchone()
            return dict(row) if row else None

    def list_for_session(self, session_id: str) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM appointment_requests WHERE session_id=? ORDER BY created_at DESC LIMIT 100",
                (session_id,)).fetchall()]

    def reschedule(self, request_id: str, session_id: str, slot: Slot) -> dict | None:
        try:
            with self._lock, self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM appointment_requests WHERE request_id=? AND session_id=?",
                                 (request_id, session_id)).fetchone()
                if not row:
                    return None
                if row["status"] != AppointmentState.CONFIRMED_AS_DEMO_REQUEST:
                    raise AppointmentError("invalid_state", "Only active requests can be rescheduled")
                if row["contact_id"] != slot.contact_id:
                    raise AppointmentError("invalid_contact", "The contact cannot be changed")
                db.execute("UPDATE appointment_requests SET slot_id=?, selected_utc_time=? WHERE request_id=? AND session_id=?",
                           (slot.slot_id, slot.starts_at, request_id, session_id))
                return dict(db.execute("SELECT * FROM appointment_requests WHERE request_id=? AND session_id=?",
                                       (request_id, session_id)).fetchone())
        except sqlite3.IntegrityError as problem:
            raise AppointmentError("slot_unavailable", "The selected time is no longer available") from problem
        except sqlite3.Error as problem:
            raise AppointmentError("storage_unavailable", "Appointment storage is temporarily unavailable") from problem

    def confirmed_for_calendar(self, contact_id: str, starts_at: str, ends_at: str) -> list[dict]:
        """Return the minimum protected calendar projection; never visitor identity."""
        with self._connect() as db:
            rows = db.execute("""SELECT request_id, contact_id, slot_id, selected_utc_time,
                display_timezone, duration_minutes, meeting_topic, status, demo
                FROM appointment_requests
                WHERE contact_id=? AND status=? AND selected_utc_time>=? AND selected_utc_time<?
                ORDER BY selected_utc_time, request_id""",
                (contact_id, AppointmentState.CONFIRMED_AS_DEMO_REQUEST, starts_at, ends_at)).fetchall()
            return [dict(row) for row in rows]

    def cancel(self, request_id: str, session_id: str) -> dict | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM appointment_requests WHERE request_id=? AND session_id=?", (request_id, session_id)).fetchone()
            if not row:
                return None
            if row["status"] != AppointmentState.CANCELLED:
                db.execute("UPDATE appointment_requests SET status=? WHERE request_id=? AND session_id=?", (AppointmentState.CANCELLED, request_id, session_id))
            result = dict(row); result["status"] = AppointmentState.CANCELLED
            return result

    def clear(self) -> int:
        with self._lock, self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM appointment_requests").fetchone()[0]
            db.execute("DELETE FROM appointment_requests")
            return count


class AppointmentTools:
    def __init__(self, contacts: list[dict], store: AppointmentStore, calendar: CalendarProvider | None = None, clock=None) -> None:
        self.contacts = {item["contact_id"]: item for item in contacts if item.get("active")}
        self.store = store
        self.calendar = calendar or DemoCalendarProvider(set(self.contacts))
        self.clock = clock or (lambda: datetime.now(UTC))

    def check_availability(self, contact_id: str) -> dict:
        slots = self.calendar.get_availability(contact_id, self.clock())
        if slots:
            occupied = {row["slot_id"] for row in self.store.confirmed_for_calendar(
                contact_id, slots[0].starts_at, "9999")}
            slots = [slot for slot in slots if slot.slot_id not in occupied]
        return {"contact_id": contact_id, "timezone": "Europe/Stockholm", "duration_minutes": 30, "available_slots": [asdict(slot) for slot in slots]}

    def _slot(self, contact_id: str, slot_id: str) -> Slot:
        slots = self.calendar.get_availability(contact_id, self.clock())
        slot = next((item for item in slots if item.slot_id == slot_id), None)
        if not slot:
            raise AppointmentError("invalid_slot", "Slot ID is invalid, modified, fabricated, or expired")
        starts = datetime.fromisoformat(slot.starts_at.replace("Z", "+00:00"))
        if starts <= self.clock():
            raise AppointmentError("expired_slot", "The selected slot has expired")
        return slot

    def create_appointment_request(self, payload: dict, session_id: str, idempotency_key: str) -> tuple[dict, bool]:
        self._validate(payload, session_id, idempotency_key)
        slot = self._slot(payload["contact_id"], payload["slot_id"])
        now = self.clock().astimezone(UTC).isoformat().replace("+00:00", "Z")
        request_id = "req-" + hashlib.sha256(f"{session_id}|{idempotency_key}".encode()).hexdigest()[:18]
        record = {"request_id": request_id, "session_id": session_id, "idempotency_key": idempotency_key,
            "created_at": now, "status": AppointmentState.CONFIRMED_AS_DEMO_REQUEST, "contact_id": payload["contact_id"],
            "slot_id": slot.slot_id, "selected_utc_time": slot.starts_at, "display_timezone": slot.timezone,
            "duration_minutes": slot.duration_minutes, "visitor_name": payload["visitor_name"].strip(),
            "visitor_email": payload["visitor_email"].strip().lower(), "company_name": payload.get("company_name", "").strip() or None,
            "meeting_topic": payload["meeting_topic"].strip(), "preferred_language": payload["preferred_language"],
            "consent_timestamp": now, "demo": 1}
        result, created = self.store.create(record)
        log.info("Demo appointment request %s for email [REDACTED]", request_id)
        return self._public(result), created

    def get_appointment_request(self, request_id: str, session_id: str) -> dict | None:
        row = self.store.get(request_id, session_id)
        return self._public(row) if row else None

    def cancel_appointment_request(self, request_id: str, session_id: str) -> dict | None:
        row = self.store.cancel(request_id, session_id)
        return self._public(row) if row else None

    def list_appointment_requests(self, session_id: str) -> list[dict]:
        return [self._public(row) for row in self.store.list_for_session(session_id)]

    def reschedule_appointment_request(self, request_id: str, session_id: str, slot_id: str) -> dict | None:
        row = self.store.get(request_id, session_id)
        if not row:
            return None
        slot = self._slot(row["contact_id"], slot_id)
        result = self.store.reschedule(request_id, session_id, slot)
        return self._public(result) if result else None

    @staticmethod
    def _public(row: dict) -> dict:
        return {key: value for key, value in row.items() if key not in {"session_id", "idempotency_key"}}

    def _validate(self, payload: dict, session_id: str, idempotency_key: str) -> None:
        if not SESSION.fullmatch(session_id): raise AppointmentError("invalid_session", "Invalid demonstration session")
        if not 1 <= len(idempotency_key) <= 100: raise AppointmentError("invalid_idempotency_key", "Invalid idempotency key")
        if payload.get("contact_id") not in self.contacts: raise AppointmentError("invalid_contact", "Contact is not approved")
        if payload.get("preferred_language") not in {"sv", "en"}: raise AppointmentError("invalid_language", "Language must be sv or en")
        if payload.get("consent") is not True: raise AppointmentError("missing_consent", "Explicit consent is required")
        limits = {"visitor_name": 100, "visitor_email": 254, "company_name": 120, "meeting_topic": 300}
        for field, maximum in limits.items():
            value = payload.get(field, "")
            if field != "company_name" and not str(value).strip(): raise AppointmentError("missing_field", f"{field} is required")
            if len(str(value)) > maximum: raise AppointmentError("field_too_long", f"{field} is too long")
        if not EMAIL.fullmatch(str(payload.get("visitor_email", ""))): raise AppointmentError("invalid_email", "Enter a valid business email")
