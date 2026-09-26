"""跨地区合规服务层：编排事件回放、地区评估、预览与统计。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.regional import Activity, evaluate_student
from .core.replay import CheckinStatus, Event, build_activities, replay
from .regional_store import (
    core_regions,
    core_rules,
    evidence_to_core,
    exceptions_to_core,
    exception_to_dict,
    list_evidence,
    list_exceptions,
    list_regions,
    rule_catalog,
)
from .repository import get_plan, load_events
from .services import PlanNotFoundError


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def _load_context(db: Session, plan_version: str) -> dict[str, Any]:
    region_rows = list_regions(db, plan_version)
    evidence_rows = list_evidence(db, plan_version)
    exception_rows = list_exceptions(db, plan_version)
    return {
        "regions": core_regions(region_rows),
        "rules": core_rules(db, plan_version),
        "evidence": evidence_to_core(evidence_rows),
        "exceptions": exceptions_to_core(exception_rows),
        "region_rows": region_rows,
        "exception_rows": exception_rows,
    }


def _activities_for_students(
    db: Session, plan_version: str, up_to_event_id: str | None
) -> dict[str, list[Activity]]:
    """回放事件，取已导师确认（或无需确认）的打卡作为受管活动。"""
    events = load_events(db, plan_version)
    state = replay(
        events,
        plan_version=plan_version,
        timezone_name="UTC",  # 仅用于旧的学时视图；地区视图自带时区
        required_seconds=0,
        up_to_event_id=up_to_event_id,
    )
    result: dict[str, list[Activity]] = {}
    for student_id, progress in state.students.items():
        confirmed = [c for c in progress.checkins if c.counts]
        result[student_id] = build_activities(confirmed)
    return result


def _evaluate_all(
    db: Session,
    plan_version: str,
    *,
    up_to_event_id: str | None = None,
    extra_activities: dict[str, list[Activity]] | None = None,
) -> dict[str, Any]:
    ctx = _load_context(db, plan_version)
    by_student = _activities_for_students(db, plan_version, up_to_event_id)
    if extra_activities:
        for sid, acts in extra_activities.items():
            by_student.setdefault(sid, []).extend(acts)

    students: dict[str, dict[str, Any]] = {}
    for sid in sorted(by_student):
        if not by_student[sid]:
            continue  # 仅有待确认打卡、没有受管活动的学员不进入地区评估
        students[sid] = evaluate_student(
            sid,
            by_student[sid],
            regions=ctx["regions"],
            rules=ctx["rules"],
            exceptions=ctx["exceptions"],
            evidence=ctx["evidence"],
        )
    return {
        "students": students,
        "context": ctx,
    }


def _statistics(students: dict[str, dict[str, Any]]) -> dict[str, Any]:
    total_students = len(students)
    violation_students = 0
    unresolved_students = 0
    total_over = 0
    total_unresolved = 0
    total_exempt = 0
    total_covered = 0
    by_region: dict[str, dict[str, Any]] = {}

    for result in students.values():
        totals = result["totals"]
        if totals["has_violation"]:
            violation_students += 1
        if totals["unresolved_seconds"]:
            unresolved_students += 1
        total_over += totals["over_seconds"]
        total_unresolved += totals["unresolved_seconds"]
        total_exempt += totals["exempt_seconds"]
        total_covered += totals["covered_seconds"]
        for bucket in result["buckets"]:
            entry = by_region.setdefault(
                bucket["region_code"],
                {
                    "region_code": bucket["region_code"],
                    "timezone": bucket["timezone"],
                    "evaluated_days": 0,
                    "violation_days": 0,
                    "over_seconds": 0,
                    "covered_seconds": 0,
                },
            )
            entry["evaluated_days"] += 1
            entry["covered_seconds"] += bucket["covered_seconds"]
            if bucket["status"] == "over_limit":
                entry["violation_days"] += 1
                entry["over_seconds"] += bucket["over_seconds"]

    return {
        "students_total": total_students,
        "students_with_violation": violation_students,
        "students_with_unresolved_location": unresolved_students,
        "covered_seconds": total_covered,
        "over_seconds": total_over,
        "exempt_seconds": total_exempt,
        "unresolved_seconds": total_unresolved,
        "by_region": sorted(by_region.values(), key=lambda r: r["region_code"]),
    }


def overview(
    db: Session, plan_version: str, *, up_to_event_id: str | None = None
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    evaluated = _evaluate_all(db, plan_version, up_to_event_id=up_to_event_id)
    ctx = evaluated["context"]
    return {
        "plan_version": plan_version,
        "plan_timezone": plan.iana_timezone,
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "event_cutoff_id": up_to_event_id,
        "statistics": _statistics(evaluated["students"]),
        "students": evaluated["students"],
        "rule_catalog": rule_catalog(db, plan_version),
        "exceptions": [exception_to_dict(r) for r in ctx["exception_rows"]],
        "regions": [
            {
                "region_code": r.region_code,
                "iana_timezone": r.iana_timezone,
                "label": r.label,
            }
            for r in ctx["region_rows"]
        ],
    }


def student_detail(
    db: Session,
    plan_version: str,
    student_id: str,
    *,
    up_to_event_id: str | None = None,
) -> dict[str, Any] | None:
    _require_plan(db, plan_version)
    evaluated = _evaluate_all(db, plan_version, up_to_event_id=up_to_event_id)
    return evaluated["students"].get(student_id)


def statistics(db: Session, plan_version: str) -> dict[str, Any]:
    _require_plan(db, plan_version)
    evaluated = _evaluate_all(db, plan_version)
    return {
        "plan_version": plan_version,
        **_statistics(evaluated["students"]),
    }


def preview(
    db: Session,
    plan_version: str,
    drafts: list[dict[str, Any]],
    *,
    include_confirmed: bool = True,
) -> dict[str, Any]:
    """预览草稿活动（未落库的打卡）对每日上限的影响。"""
    _require_plan(db, plan_version)
    ctx = _load_context(db, plan_version)

    # 直接把草稿物化为已确认活动；复用 replay 的地点载荷解析。
    draft_events = [
        Event(
            event_id=d["event_id"],
            plan_version=plan_version,
            event_type="checkin",  # type: ignore[arg-type]
            student_id=d["student_id"],
            payload=d["payload"],
            created_at=datetime.now(timezone.utc),
        )
        for d in drafts
    ]
    draft_state = replay(
        draft_events,
        plan_version=plan_version,
        timezone_name="UTC",
        required_seconds=0,
    )
    extra: dict[str, list[Activity]] = {}
    for sid, progress in draft_state.students.items():
        # 草稿按“假设已确认”评估，因此实习类型也计入预览。
        for record in progress.checkins:
            record.status = CheckinStatus.CONFIRMED
        extra[sid] = build_activities(progress.checkins)

    if include_confirmed:
        by_student = _activities_for_students(db, plan_version, None)
        for sid, acts in extra.items():
            by_student.setdefault(sid, []).extend(acts)
    else:
        by_student = extra

    students = {
        sid: evaluate_student(
            sid,
            acts,
            regions=ctx["regions"],
            rules=ctx["rules"],
            exceptions=ctx["exceptions"],
            evidence=ctx["evidence"],
        )
        for sid, acts in sorted(by_student.items())
    }
    return {
        "plan_version": plan_version,
        "preview": True,
        "statistics": _statistics(students),
        "students": students,
    }


def freeze_block(
    db: Session, plan_version: str, event_cutoff_id: str | None
) -> dict[str, Any]:
    """冻结快照内嵌的地区合规块：规则目录 + 例外 + 每段规则解释。"""
    block = overview(db, plan_version, up_to_event_id=event_cutoff_id)
    return {
        "generated_at": block["generated_at"],
        "event_cutoff_id": event_cutoff_id,
        "regions": block["regions"],
        "rule_catalog": block["rule_catalog"],
        "exceptions": block["exceptions"],
        "statistics": block["statistics"],
        "students": block["students"],
    }
