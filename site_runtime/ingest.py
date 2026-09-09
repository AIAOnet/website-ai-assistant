from __future__ import annotations
from .configuration import setting

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .knowledge import validate_record
from .chunking import build_chunks


def build_index(source: Path, destination: Path) -> int:
    payload = json.loads(source.read_text(encoding="utf-8"))
    records = payload.get("records", payload) if isinstance(payload, dict) else payload
    approved = [validate_record(record) for record in records]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "records": approved,
        "chunks": build_chunks([record for record in approved
                                 if record["source_status"] == "active"], source.parent),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return len(approved)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Build the reviewed local Website index")
    parser.add_argument("--source", default="website")
    args = parser.parse_args()
    root = Path(setting("WEBSITE_ASSISTANT_DATA_PATH", "data"))
    count = build_index(root / "approved_sources.json", root / "knowledge" / "index.json")
    print(f"Indexed {count} approved records from {args.source}. No website crawling was performed.")


if __name__ == "__main__":
    main()
