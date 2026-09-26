"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable

from .clock import (
    academic_day,
    elapsed_seconds,
    merge_intervals,
    split_by_academic_day,
    to_utc,
    union_seconds,
)
from .regional import Activity, TrackPoint


class EventType(StrEnum):
    CHECKIN = "checkin"
    MENTOR_CONFIRM = "mentor_confirm"
    LEAVE_CORRECTION = "leave_correction"


class CheckinStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"


INTERNSHIP_TYPE = "internship"


@dataclass(frozen=True)
class Event:
    """封装领域状态与业务约束。"""

    event_id: str
    plan_version: str
    event_type: EventType
    student_id: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass
class CheckinRecord:
    event_id: str
    student_id: str
    activity_id: str
    activity_type: str
    start_utc: datetime
    end_utc: datetime
    status: CheckinStatus
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)

    @property
    def counts(self) -> bool:
        return self.status == CheckinStatus.CONFIRMED


@dataclass
class Adjustment:
    event_id: str
    student_id: str
    seconds: int
    reason: str


@dataclass
class DayTotal:
    academic_day: str
    seconds: int


@dataclass
class StudentProgress:
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DayTotal] = field(default_factory=list)
    checkins: list[CheckinRecord] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)


@dataclass
class ReplayState:
    plan_version: str
    timezone: str
    required_seconds: int
    students: dict[str, StudentProgress]


def _parse_checkin(
    event: Event, tz_name: str
) -> CheckinRecord:
    start = to_utc(datetime.fromisoformat(event.payload["check_in_at"]))
    end = to_utc(datetime.fromisoformat(event.payload["check_out_at"]))
    activity_type = event.payload.get("activity_type", "regular")
    requires_confirmation = activity_type == INTERNSHIP_TYPE
    status = (
        CheckinStatus.PENDING if requires_confirmation else CheckinStatus.CONFIRMED
    )
    return CheckinRecord(
        event_id=event.event_id,
        student_id=event.student_id,
        activity_id=event.payload.get("activity_id", ""),
        activity_type=activity_type,
        start_utc=start,
        end_utc=end,
        status=status,
        payload=dict(event.payload),
    )


def replay(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    up_to_event_id: str | None = None,
) -> ReplayState:
    """执行确定性的业务处理。"""
    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: e.event_id,
    )
    if up_to_event_id is not None:
        sorted_events = [e for e in sorted_events if e.event_id <= up_to_event_id]

    checkins_by_student: dict[str, list[CheckinRecord]] = {}
    checkin_index: dict[str, CheckinRecord] = {}
    adjustments_by_student: dict[str, list[Adjustment]] = {}

    for event in sorted_events:
        if event.event_type == EventType.CHECKIN:
            record = _parse_checkin(event, timezone_name)
            checkins_by_student.setdefault(event.student_id, []).append(record)
            checkin_index[event.event_id] = record
        elif event.event_type == EventType.MENTOR_CONFIRM:
            target_id = event.payload.get("checkin_event_id")
            target = checkin_index.get(target_id)
            if target is not None and target.student_id == event.student_id:
                target.status = CheckinStatus.CONFIRMED
        elif event.event_type == EventType.LEAVE_CORRECTION:
            seconds = int(event.payload.get("adjustment_seconds", 0))
            adjustments_by_student.setdefault(event.student_id, []).append(
                Adjustment(
                    event_id=event.event_id,
                    student_id=event.student_id,
                    seconds=seconds,
                    reason=str(event.payload.get("reason", "")),
                )
            )

    all_students = set(checkins_by_student) | set(adjustments_by_student)
    students: dict[str, StudentProgress] = {}
    for student_id in all_students:
        records = checkins_by_student.get(student_id, [])
        adjustments = adjustments_by_student.get(student_id, [])

        confirmed_intervals = [
            (r.start_utc, r.end_utc) for r in records if r.counts
        ]
        pending_intervals = [
            (r.start_utc, r.end_utc)
            for r in records
            if r.status == CheckinStatus.PENDING
        ]

        confirmed_seconds = union_seconds(confirmed_intervals)
        pending_seconds = union_seconds(pending_intervals)
        adjustment_seconds = sum(a.seconds for a in adjustments)
        total_seconds = confirmed_seconds + adjustment_seconds
        if total_seconds < 0:
            total_seconds = 0

        day_totals: dict[str, int] = {}
        for start, end in merge_intervals(confirmed_intervals):
            for day, seg_start, seg_end in split_by_academic_day(
                start, end, timezone_name
            ):
                key = day.isoformat()
                day_totals[key] = day_totals.get(key, 0) + elapsed_seconds(
                    seg_start, seg_end
                )
        daily = [
            DayTotal(academic_day=day, seconds=secs)
            for day, secs in sorted(day_totals.items())
        ]

        students[student_id] = StudentProgress(
            student_id=student_id,
            confirmed_seconds=confirmed_seconds,
            pending_seconds=pending_seconds,
            adjustment_seconds=adjustment_seconds,
            total_seconds=total_seconds,
            lesson_units=total_seconds // (45 * 60),
            pending_lesson_units=pending_seconds // (45 * 60),
            meets_requirement=total_seconds >= required_seconds,
            daily=daily,
            checkins=sorted(records, key=lambda r: r.start_utc),
            adjustments=sorted(adjustments, key=lambda a: a.event_id),
        )

    return ReplayState(
        plan_version=plan_version,
        timezone=timezone_name,
        required_seconds=required_seconds,
        students=students,
    )


def explain_checkin(record: CheckinRecord, tz_name: str) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    segments = split_by_academic_day(record.start_utc, record.end_utc, tz_name)
    return {
        "event_id": record.event_id,
        "activity_id": record.activity_id,
        "activity_type": record.activity_type,
        "status": record.status.value,
        "counts": record.counts,
        "check_in_at_utc": record.start_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "check_out_at_utc": record.end_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "raw_seconds": record.seconds,
        "academic_days": [
            {
                "day": day.isoformat(),
                "start_utc": seg_start.isoformat().replace("+00:00", "Z"),
                "end_utc": seg_end.isoformat().replace("+00:00", "Z"),
                "seconds": elapsed_seconds(seg_start, seg_end),
            }
            for day, seg_start, seg_end in segments
        ],
    }


# ---------------------------------------------------------------------------
# 跨地区活动提取
# ---------------------------------------------------------------------------

def parse_location_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, str | None]:
    """从打卡 payload 解析静止地点 / 移动轨迹。

    返回 (location, track, error)；二者互斥，校验失败时 error 非空。
    """
    location = payload.get("location")
    track = payload.get("track")
    if location is not None and track is not None:
        return None, None, "location_and_track_conflict"
    if location is not None:
        if not isinstance(location, dict) or not location.get("region_code"):
            return None, None, "invalid_location"
        return (
            {
                "region_code": str(location["region_code"]),
                "evidence_id": location.get("evidence_id"),
            },
            None,
            None,
        )
    if track is not None:
        if not isinstance(track, list) or not track:
            return None, None, "invalid_track"
        normalized: list[dict[str, Any]] = []
        for point in track:
            if (
                not isinstance(point, dict)
                or not point.get("at")
                or not point.get("region_code")
            ):
                return None, None, "invalid_track_point"
            normalized.append(
                {
                    "at": str(point["at"]),
                    "region_code": str(point["region_code"]),
                    "evidence_id": point.get("evidence_id"),
                }
            )
        return None, normalized, None
    return None, None, None


def build_activities(records: list[CheckinRecord]) -> list[Activity]:
    """把（已含导师确认状态的）打卡记录转成跨地区评估用的活动。"""
    activities: list[Activity] = []
    for record in records:
        location, track_points, loc_error = parse_location_payload(
            record.payload or {}
        )
        track: tuple[TrackPoint, ...] = ()
        region_code: str | None = None
        evidence_id: str | None = None
        if loc_error is None and location is not None:
            region_code = location["region_code"]
            evidence_id = location["evidence_id"]
        elif loc_error is None and track_points is not None:
            track = tuple(
                TrackPoint(
                    at_utc=to_utc(datetime.fromisoformat(point["at"])),
                    region_code=point["region_code"],
                    evidence_id=point.get("evidence_id"),
                )
                for point in track_points
            )
        activities.append(
            Activity(
                event_id=record.event_id,
                student_id=record.student_id,
                activity_id=record.activity_id,
                start_utc=record.start_utc,
                end_utc=record.end_utc,
                region_code=region_code,
                evidence_id=evidence_id,
                track=track,
                parse_error=loc_error,
                status=record.status.value,
            )
        )
    return activities
