"""Non-secret operational settings edited by authenticated administrators."""
import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class RagSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    result_limit: int = Field(default=4, ge=1, le=4)
    minimum_score: float = Field(default=0.2, ge=0.2, le=1)
    lexical_weight: float = Field(default=0.65, ge=0, le=1)
    use_semantic: bool = True
    ontology_depth: int = Field(default=1, ge=1, le=2)


def atomic_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            name = stream.name
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
