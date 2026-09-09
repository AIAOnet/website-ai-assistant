"""Privacy-safe administration view of the deterministic demo calendar."""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, time, timedelta

from .appointments import AppointmentError, AppointmentTools, DemoCalendarProvider
from .appointments import STOCKHOLM


class CalendarAdmin:
    def __init__(self, appointments: AppointmentTools):
        self.appointments = appointments

    def status(self, contact_id=None, week_start=None):
        contacts = [{key: contact[key] for key in ("contact_id", "name", "job_title", "company")}
                    for contact in self.appointments.contacts.values()]
        contacts.sort(key=lambda item: item["contact_id"])
        result = {
            "provider": "simulated", "provider_label": "Simulated calendar",
            "connected_to_external_calendar": False, "creates_real_invitations": False,
            "timezone": "Europe/Stockholm", "duration_minutes": 30,
            "contacts": contacts, "available_slots": [],
            "booked_events": [],
            "week_start": None,
            "future_provider_boundary": "CalendarProvider",
        }
        if contact_id is not None:
            if contact_id not in self.appointments.contacts:
                raise AppointmentError("invalid_contact", "Contact is not approved")
            if week_start is None:
                selected = datetime.now(STOCKHOLM).date()
                selected -= timedelta(days=selected.weekday())
            else:
                try:
                    selected = datetime.strptime(week_start, "%Y-%m-%d").date()
                except (TypeError, ValueError) as problem:
                    raise ValueError("Invalid calendar week") from problem
                if selected.weekday() != 0:
                    raise ValueError("Calendar week must start on Monday")
                today = datetime.now(STOCKHOLM).date()
                if abs((selected - today).days) > 366:
                    raise ValueError("Calendar week is outside the demo range")
            anchor = datetime.combine(selected - timedelta(days=1), time(12), STOCKHOLM)
            result["week_start"] = selected.isoformat()
            start = datetime.combine(selected, time(0), STOCKHOLM).astimezone(UTC)
            end = datetime.combine(selected + timedelta(days=7), time(0), STOCKHOLM).astimezone(UTC)
            bookings = self.appointments.store.confirmed_for_calendar(
                contact_id, start.isoformat().replace("+00:00", "Z"),
                end.isoformat().replace("+00:00", "Z"))
            booked_slots = {item["slot_id"] for item in bookings}
            result["available_slots"] = [asdict(slot) for slot in
                                         self.appointments.calendar.get_availability(contact_id, anchor)
                                         if slot.slot_id not in booked_slots]
            result["booked_events"] = [{"request_id": item["request_id"],
                "contact_id": item["contact_id"], "slot_id": item["slot_id"],
                "starts_at": item["selected_utc_time"],
                "timezone": item["display_timezone"],
                "duration_minutes": item["duration_minutes"], "status": item["status"],
                "meeting_topic": item["meeting_topic"][:100], "demo": bool(item["demo"])}
                for item in bookings]
        return result

    @property
    def deterministic(self):
        return isinstance(self.appointments.calendar, DemoCalendarProvider)
