"""Strict, versioned definitions for administrator-run assistant evaluations."""
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IntentName = Literal["PRODUCT_INFORMATION", "SERVICE_INFORMATION", "COMPANY_INFORMATION",
                     "FIND_CONTACT", "CHECK_AVAILABILITY", "REQUEST_APPOINTMENT", "OUT_OF_SCOPE"]


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    case_id: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]+$")
    description: str = Field(min_length=3, max_length=200)
    enabled: bool = True
    language: Literal["sv", "en"]
    question: str = Field(min_length=2, max_length=500)
    execution_mode: Literal["retrieval", "full", "deterministic"]
    expected_intent: IntentName
    expected_generation: list[Literal["llm", "fallback", "deterministic"]] = Field(max_length=3)
    expected_source_ids: list[str] = Field(default_factory=list, max_length=4)
    expected_contact_id: str | None = Field(default=None, max_length=100,
                                             pattern=r"^[A-Za-z0-9_-]+$")
    required_phrases: list[str] = Field(default_factory=list, max_length=10)
    forbidden_phrases: list[str] = Field(default_factory=list, max_length=10)
    tags: list[str] = Field(default_factory=list, min_length=1, max_length=8)

    @field_validator("expected_source_ids")
    @classmethod
    def source_ids(cls, values):
        if any(not value or len(value) > 100 or not value.replace("-", "").replace("_", "").isalnum()
               for value in values):
            raise ValueError("Expected source IDs must use citation-safe characters")
        if len(values) != len(set(values)):
            raise ValueError("Expected source IDs must be unique")
        return values

    @field_validator("required_phrases", "forbidden_phrases", "tags")
    @classmethod
    def bounded_text_lists(cls, values):
        if any(not value.strip() or len(value) > 120 or any(ord(char) < 32 for char in value)
               for value in values):
            raise ValueError("Evaluation list values must be bounded nonempty text")
        if len(values) != len(set(values)):
            raise ValueError("Evaluation list values must be unique")
        return values

    @model_validator(mode="after")
    def consistent_mode(self):
        if self.execution_mode == "retrieval" and self.expected_generation:
            raise ValueError("Retrieval-only cases cannot expect a generation type")
        if self.execution_mode != "retrieval" and not self.expected_generation:
            raise ValueError("Response cases must expect at least one generation type")
        if set(self.required_phrases) & set(self.forbidden_phrases):
            raise ValueError("A phrase cannot be both required and forbidden")
        return self


class EvaluationSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: str = Field(min_length=1, max_length=100)
    cases: list[EvaluationCase] = Field(min_length=0, max_length=100)

    @model_validator(mode="after")
    def unique_cases(self):
        identifiers = [case.case_id for case in self.cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Evaluation case IDs must be unique")
        return self


def load_evaluation_suite(path: str | Path) -> EvaluationSuite:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return EvaluationSuite.model_validate(payload)
