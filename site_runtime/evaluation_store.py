"""Transactional evaluation definitions with one-time JSON migration."""
from contextlib import closing
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .database_maintenance import open_database, apply_migrations, Migration
from .evaluation_cases import EvaluationCase, EvaluationSuite, load_evaluation_suite


class EvaluationConflict(ValueError):
    pass


class EvaluationChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=0)
    operation: Literal["create", "update", "delete"]
    case_id: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]+$")
    case: EvaluationCase | None = None

    @model_validator(mode="after")
    def valid_case(self):
        if self.operation != "delete" and (self.case is None or self.case.case_id != self.case_id):
            raise ValueError("A matching test definition is required")
        if self.operation == "delete" and self.case is not None:
            raise ValueError("Delete does not accept a test definition")
        return self


def schema(db):
    db.execute("CREATE TABLE evaluation_cases (case_id TEXT PRIMARY KEY, definition TEXT NOT NULL)")
    db.execute("CREATE TABLE evaluation_meta (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)")


class EvaluationStore:
    def __init__(self, path, legacy=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(open_database(self.path)) as db, db:
            apply_migrations(db, "evaluations", (Migration(1, "evaluation_definitions", schema),))
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT revision FROM evaluation_meta WHERE id=1").fetchone() is None:
                suite = load_evaluation_suite(legacy) if legacy and Path(legacy).is_file() else EvaluationSuite(version="empty", cases=[])
                db.executemany("INSERT INTO evaluation_cases VALUES (?, ?)",
                               [(c.case_id, c.model_dump_json()) for c in suite.cases])
                db.execute("INSERT INTO evaluation_meta VALUES (1, 0)")

    def load(self):
        with closing(open_database(self.path)) as db, db:
            db.execute("BEGIN")
            revision = db.execute("SELECT revision FROM evaluation_meta WHERE id=1").fetchone()[0]
            cases = [EvaluationCase.model_validate_json(row[0]) for row in db.execute(
                "SELECT definition FROM evaluation_cases ORDER BY case_id")]
        return EvaluationSuite(version=str(revision), cases=cases)

    def change(self, change):
        with closing(open_database(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            revision = db.execute("SELECT revision FROM evaluation_meta WHERE id=1").fetchone()[0]
            if revision != change.revision:
                raise EvaluationConflict("Tests changed. Refresh the test list before saving again.")
            exists = db.execute("SELECT 1 FROM evaluation_cases WHERE case_id=?", (change.case_id,)).fetchone()
            if change.operation == "create":
                if exists:
                    raise EvaluationConflict("That test ID already exists.")
                if db.execute("SELECT COUNT(*) FROM evaluation_cases").fetchone()[0] >= 100:
                    raise ValueError("At most 100 evaluation tests are supported.")
                db.execute("INSERT INTO evaluation_cases VALUES (?, ?)", (change.case_id, change.case.model_dump_json()))
            else:
                if not exists:
                    raise EvaluationConflict("This test no longer exists. Refresh the test list.")
                if change.operation == "delete":
                    db.execute("DELETE FROM evaluation_cases WHERE case_id=?", (change.case_id,))
                else:
                    db.execute("UPDATE evaluation_cases SET definition=? WHERE case_id=?", (change.case.model_dump_json(), change.case_id))
            db.execute("UPDATE evaluation_meta SET revision=revision+1 WHERE id=1")
        return self.load()
