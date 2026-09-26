"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinLocation(BaseModel):
    region_code: str = Field(..., min_length=1, max_length=64)
    evidence_id: str | None = Field(None, max_length=128)


class CheckinTrackPoint(BaseModel):
    at: datetime
    region_code: str = Field(..., min_length=1, max_length=64)
    evidence_id: str | None = Field(None, max_length=128)

    @field_validator("at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("track point at must be timezone-aware")
        return v


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime
    location: CheckinLocation | None = None
    track: list[CheckinTrackPoint] | None = None

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        if self.location is not None and self.track is not None:
            raise ValueError("location and track are mutually exclusive")
        if self.track is not None:
            if not self.track:
                raise ValueError("track must not be empty")
            ats = sorted(p.at for p in self.track)
            if any(ats[i] == ats[i - 1] for i in range(1, len(ats))):
                raise ValueError("track point timestamps must be unique")
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

    @model_validator(mode="after")
    def _validate_payload(self) -> "EventIn":
        model = {
            "checkin": CheckinPayload,
            "mentor_confirm": MentorConfirmPayload,
            "leave_correction": LeaveCorrectionPayload,
        }[self.event_type]
        model.model_validate(self.payload)
        return self


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
    regional_compliance: dict[str, Any] | None = None


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
# 跨地区合规
# ---------------------------------------------------------------------------

class RegionIn(BaseModel):
    region_code: str = Field(..., min_length=1, max_length=64)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    label: str = Field("", max_length=256)


class RegionOut(BaseModel):
    plan_version: str
    region_code: str
    iana_timezone: str
    label: str


class CapRuleIn(BaseModel):
    rule_id: str = Field(..., min_length=1, max_length=128)
    region_code: str = Field(..., min_length=1, max_length=64)
    daily_cap_seconds: int = Field(..., ge=0)
    reason: str = Field("", max_length=512)
    created_by: str = Field("", max_length=128)
    effective_from_utc: datetime | None = None

    @field_validator("effective_from_utc")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("effective_from_utc 必须携带时区")
        return v


class CapRulePatchIn(BaseModel):
    daily_cap_seconds: int = Field(..., ge=0)


class RuleTransitionIn(BaseModel):
    status: Literal["published", "retired", "restored"]
    now: datetime | None = None

    @field_validator("now")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("now 必须携带时区")
        return v


class CapRuleWindowOut(BaseModel):
    seq: int
    effective_from_utc: str
    effective_to_utc: str | None


class CapRuleOut(BaseModel):
    rule_id: str
    region_code: str
    daily_cap_seconds: int
    status: str
    reason: str
    created_by: str
    windows: list[CapRuleWindowOut]


class EvidenceIn(BaseModel):
    evidence_id: str = Field(..., min_length=1, max_length=128)
    student_id: str = Field(..., min_length=1, max_length=128)
    region_code: str = Field(..., min_length=1, max_length=64)
    observed_at: datetime
    source: str = Field("", max_length=128)
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("observed_at 必须携带时区")
        return v


class EvidenceOut(BaseModel):
    evidence_id: str
    student_id: str
    region_code: str
    observed_at_utc: str
    source: str
    detail: dict[str, Any]


class ExceptionIn(BaseModel):
    exception_id: str = Field(..., min_length=1, max_length=128)
    student_id: str = Field(..., min_length=1, max_length=128)
    region_code: str | None = Field(None, max_length=64)
    activity_id: str | None = Field(None, max_length=128)
    valid_from_utc: datetime
    valid_to_utc: datetime
    cap_override_seconds: int | None = Field(None, ge=0)
    reason: str = Field("", max_length=512)

    @field_validator("valid_from_utc", "valid_to_utc")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("例外期限必须携带时区")
        return v


class ExceptionOut(BaseModel):
    exception_id: str
    student_id: str
    region_code: str | None
    activity_id: str | None
    valid_from_utc: str
    valid_to_utc: str
    cap_override_seconds: int | None
    status: str
    reason: str
    reviewed_by: str | None
    reviewed_at: str | None


class ExceptionReviewIn(BaseModel):
    approve: bool
    reviewed_by: str = Field(..., min_length=1, max_length=128)
    reason: str = Field("", max_length=512)
    cap_override_seconds: int | None = Field(None, ge=0)


class ExceptionRevokeIn(BaseModel):
    reviewed_by: str = Field(..., min_length=1, max_length=128)
    now: datetime | None = None


class LocationIn(BaseModel):
    region_code: str
    evidence_id: str | None = None


class TrackPointIn(BaseModel):
    at: datetime
    region_code: str
    evidence_id: str | None = None

    @field_validator("at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("轨迹点时间必须携带时区")
        return v


class DraftCheckinIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    student_id: str = Field(..., min_length=1, max_length=128)
    activity_id: str = Field("", max_length=128)
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime
    location: LocationIn | None = None
    track: list[TrackPointIn] | None = None

    @model_validator(mode="after")
    def _validate(self) -> "DraftCheckinIn":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at 必须晚于 check_in_at")
        if self.check_in_at.tzinfo is None or self.check_out_at.tzinfo is None:
            raise ValueError("打卡时间必须携带时区")
        if self.location is not None and self.track is not None:
            raise ValueError("location 与 track 互斥")
        if self.track is not None:
            if not self.track:
                raise ValueError("track 不能为空")
            ats = sorted(p.at for p in self.track)
            if any(ats[i] == ats[i - 1] for i in range(1, len(ats))):
                raise ValueError("轨迹点时间不能重复")
        return self


class PreviewIn(BaseModel):
    events: list[DraftCheckinIn]
    include_confirmed: bool = True


class RegionalStatisticsOut(BaseModel):
    plan_version: str
    students_total: int
    students_with_violation: int
    students_with_unresolved_location: int
    covered_seconds: int
    over_seconds: int
    exempt_seconds: int
    unresolved_seconds: int
    by_region: list[dict[str, Any]]
