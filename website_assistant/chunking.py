"""Deterministic approved-source chunks with non-evidentiary location metadata."""
from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

MAX_CHUNK_CHARS = 1500


def _checksum(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pieces(text):
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    output = []
    for paragraph in paragraphs or [text.strip()]:
        while len(paragraph) > MAX_CHUNK_CHARS:
            cut = paragraph.rfind(" ", 0, MAX_CHUNK_CHARS + 1)
            cut = cut if cut >= 300 else MAX_CHUNK_CHARS
            output.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if paragraph:
            output.append(paragraph)
    combined = []
    for part in output:
        if combined and len(combined[-1]) + 2 + len(part) <= MAX_CHUNK_CHARS:
            combined[-1] += "\n\n" + part
        else:
            combined.append(part)
    return combined


def _locations(directory):
    path = Path(directory) / "approved_documents.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        documents = payload["documents"] if payload.get("version") == 1 else []
        return {item["source_id"]: item["extraction"].get("locations", []) for item in documents
                if item.get("status") == "active" and isinstance(item.get("extraction"), dict)}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def build_chunks(records, directory):
    locations = _locations(directory) if directory is not None else {}
    chunks = []
    for record in records:
        located_spans = record.get("sections") or locations.get(record["source_id"])
        spans = located_spans or [{"kind": "passage",
            "label": "Approved passage", "start": 0, "end": len(record["content"])}]
        number = 0
        for span in spans:
            start, end = span.get("start"), span.get("end")
            if (not isinstance(start, int) or not isinstance(end, int) or start < 0 or
                    end <= start or end > len(record["content"])):
                continue
            for content in _pieces(record["content"][start:end]):
                number += 1
                chunk = dict(record)
                chunk.update(content=content, checksum=_checksum(content),
                    chunk_id=f'{record["source_id"]}--{number:03d}')
                if located_spans:
                    chunk["source_location"] = {"kind": span.get("kind", "section"),
                                                "label": span.get("label", f"Section {number}")}
                chunks.append(chunk)
        if number == 0:
            chunk = dict(record); chunk.update(chunk_id=f'{record["source_id"]}--001')
            chunks.append(chunk)
    return chunks
