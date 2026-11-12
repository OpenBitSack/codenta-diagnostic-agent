from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

CdtCode = Annotated[str, StringConstraints(pattern=r"^D\d{4}$")]
Ident = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:@-]{2,64}$")]
Severity = Literal["low", "medium", "high", "critical"]
SEVERITY_PRIORITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class StageTraceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal["extraction", "coding", "qa"]
    stage_version: Ident
    inputs: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any]


class IssueReportIn(BaseModel):
    """Failure report submitted by a practice user, support engineer, or dental expert."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticket_id: Ident
    practice_id: Annotated[str, StringConstraints(pattern=r"^P-[A-Z0-9-]{2,30}$")]
    source_note_id: Ident
    pipeline_run_id: Ident
    predicted_codes: list[CdtCode] = Field(..., max_length=50)
    expected_codes: list[CdtCode] | None = Field(None, max_length=50)
    error_description: str = Field("", max_length=2000)
    policy_version: Ident | None = None
    pipeline_version: Annotated[str, StringConstraints(pattern=r"^v\d+(\.\d+){0,3}$")]
    stage_traces: list[StageTraceIn] | None = Field(None, max_length=10)
    severity: Severity = "medium"
    reporter_id: Ident
    reported_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("reported_at")
    @classmethod
    def _not_future(cls, v: datetime) -> datetime:
        aware = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        if aware > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("reported_at is in the future")
        return aware.astimezone(timezone.utc).replace(tzinfo=None)


class IntakeResult(BaseModel):
    report_id: str
    investigation_id: str
    duplicate: bool
    idempotent_replay: bool = False
    status: str
    report_count: int
    similar_candidates: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ""
