"""服务端业务模块。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]
    regional: dict[str, Any] = {}


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


# ---------------------------------------------------------------------------
# 地区规则版本
# ---------------------------------------------------------------------------


def _require_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware (RFC 3339)")
    return v


class RegionRuleIn(BaseModel):
    rule_id: str = Field(..., min_length=1, max_length=128)
    region: str = Field(..., min_length=1, max_length=64)
    version: int = Field(..., ge=1)
    daily_cap_seconds: int = Field(..., ge=0)
    effective_from: datetime
    effective_to: datetime | None = None
    note: str = ""

    @field_validator("effective_from", "effective_to")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        return _require_aware(v)

    @model_validator(mode="after")
    def _window_order(self) -> "RegionRuleIn":
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class RegionRuleOut(BaseModel):
    rule_id: str
    region: str
    version: int
    daily_cap_seconds: int
    effective_from: datetime
    effective_to: datetime | None
    status: str
    rollback_of: str | None
    note: str
    created_at: datetime


class RegionRuleListOut(BaseModel):
    rules: list[RegionRuleOut]


class RollbackIn(BaseModel):
    target_version: int = Field(..., ge=1)
    note: str = ""


# ---------------------------------------------------------------------------
# 活动地点证据
# ---------------------------------------------------------------------------


class LocationEvidenceIn(BaseModel):
    evidence_id: str = Field(..., min_length=1, max_length=128)
    activity_id: str = Field(..., min_length=1, max_length=128)
    student_id: str | None = Field(None, max_length=128)
    region: str = Field(..., min_length=1, max_length=64)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    valid_from: datetime
    source: str = Field("manual", max_length=32)
    note: str = ""

    @field_validator("valid_from")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("iana_timezone")
    @classmethod
    def _known_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {v}") from exc
        return v


class LocationEvidenceOut(BaseModel):
    evidence_id: str
    plan_version: str
    activity_id: str
    student_id: str | None
    region: str
    iana_timezone: str
    valid_from: datetime
    source: str
    note: str
    created_at: datetime


class LocationEvidenceListOut(BaseModel):
    evidence: list[LocationEvidenceOut]


# ---------------------------------------------------------------------------
# 人工例外 / 复核
# ---------------------------------------------------------------------------


class ManualExceptionIn(BaseModel):
    exception_id: str = Field(..., min_length=1, max_length=128)
    student_id: str = Field(..., min_length=1, max_length=128)
    region: str = Field(..., min_length=1, max_length=64)
    activity_id: str | None = Field(None, max_length=128)
    local_day: str | None = Field(None, min_length=10, max_length=10)
    valid_from: datetime
    valid_until: datetime
    approver: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)

    @field_validator("valid_from", "valid_until")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v)

    @field_validator("local_day")
    @classmethod
    def _iso_day(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("local_day must be an ISO date (YYYY-MM-DD)") from exc
        return v

    @model_validator(mode="after")
    def _window_order(self) -> "ManualExceptionIn":
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        return self


class ManualExceptionOut(BaseModel):
    exception_id: str
    plan_version: str
    student_id: str
    region: str
    activity_id: str | None
    local_day: str | None
    valid_from: datetime
    valid_until: datetime
    approver: str
    reason: str
    status: str
    created_at: datetime
    revoked_at: datetime | None


class ManualExceptionListOut(BaseModel):
    exceptions: list[ManualExceptionOut]


class ReviewDecisionIn(BaseModel):
    student_id: str = Field(..., min_length=1, max_length=128)
    region: str = Field(..., min_length=1, max_length=64)
    local_day: str = Field(..., min_length=10, max_length=10)
    decision: Literal["approved", "dismissed"]
    reviewer: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)
    exception_id: str | None = Field(None, max_length=128)

    @field_validator("local_day")
    @classmethod
    def _iso_day(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("local_day must be an ISO date (YYYY-MM-DD)") from exc
        return v


class ReviewDecisionOut(BaseModel):
    plan_version: str
    student_id: str
    region: str
    local_day: str
    decision: str
    reviewer: str
    reason: str
    exception_id: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# 预览 / 复核列表 / 统计
# ---------------------------------------------------------------------------


class RegionalPreviewOut(BaseModel):
    plan_version: str
    generated_at: str
    students: list[dict[str, Any]]


class RegionalReviewOut(BaseModel):
    plan_version: str
    generated_at: str
    findings: list[dict[str, Any]]


class RegionalStatisticsOut(BaseModel):
    plan_version: str
    generated_at: str
    totals: dict[str, Any]
    regions: list[dict[str, Any]]
