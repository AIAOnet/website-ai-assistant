from __future__ import annotations
from .configuration import setting

import os
from pathlib import Path

from .appointments import AppointmentStore


def main() -> None:
    root = Path(__file__).parents[1]
    database = Path(setting("WEBSITE_ASSISTANT_APPOINTMENT_DB", root / "data" / "appointments.db"))
    count = AppointmentStore(database).clear()
    print(f"Cleared {count} demonstration appointment request(s). Knowledge and contacts were not changed.")


if __name__ == "__main__":
    main()
