"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.regional import (
    ActivityInterval,
    StudentRegionalReport,
    build_findings,
    build_statistics,
    evaluate_plan,
)
from .core.replay import replay
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .repository import (
    get_exception,
    get_freeze,
    get_plan,
    get_region_rule,
    get_region_rule_version,
    insert_events,
    insert_evidence,
    insert_exception,
    insert_freeze,
    insert_region_rule,
    list_evidence_rows,
    list_exception_rows,
    list_region_rules,
    list_review_decisions,
    load_events,
    load_events_up_to,
    load_evidence,
    load_exceptions,
    load_published_rules,
    max_event_id,
    update_exception,
    update_region_rule,
    upsert_plan,
    upsert_review_decision,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class RuleNotFoundError(Exception):
    pass


class RuleConflictError(Exception):
    pass


class RuleStateError(Exception):
    pass


class EvidenceConflictError(Exception):
    pass


class ManualExceptionNotFoundError(Exception):
    pass


class ExceptionConflictError(Exception):
    pass


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
    }


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    regional = _regional_section(db, plan, events)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        regional=regional,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    regional = _regional_section(db, plan, events, up_to_event_id=cutoff)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
        regional=regional,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


# ---------------------------------------------------------------------------
# 地区规则版本（规则 API）
# ---------------------------------------------------------------------------


def _rule_out(row) -> dict[str, Any]:
    return {
        "rule_id": row.rule_id,
        "region": row.region,
        "version": row.version,
        "daily_cap_seconds": row.daily_cap_seconds,
        "effective_from": row.effective_from,
        "effective_to": row.effective_to,
        "status": row.status,
        "rollback_of": row.rollback_of,
        "note": row.note,
        "created_at": row.created_at,
    }


def create_region_rule(
    db: Session,
    *,
    rule_id: str,
    region: str,
    version: int,
    daily_cap_seconds: int,
    effective_from: datetime,
    effective_to: datetime | None,
    note: str = "",
) -> dict[str, Any]:
    if version < 1:
        raise RuleStateError("version must be a positive integer")
    if effective_to is not None and effective_to <= effective_from:
        raise RuleStateError("effective_to must be after effective_from")
    if get_region_rule(db, rule_id) is not None:
        raise RuleConflictError(f"rule id '{rule_id}' already exists")
    row = insert_region_rule(
        db,
        {
            "rule_id": rule_id,
            "region": region,
            "version": version,
            "daily_cap_seconds": daily_cap_seconds,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "status": "draft",
            "rollback_of": None,
            "note": note,
        },
    )
    if row is None:
        raise RuleConflictError(
            f"region '{region}' already has a rule version {version}"
        )
    return _rule_out(row)


def list_region_rule_versions(
    db: Session, region: str | None = None
) -> list[dict[str, Any]]:
    return [_rule_out(r) for r in list_region_rules(db, region=region)]


def get_region_rule_out(db: Session, rule_id: str) -> dict[str, Any]:
    row = get_region_rule(db, rule_id)
    if row is None:
        raise RuleNotFoundError(f"region rule '{rule_id}' does not exist")
    return _rule_out(row)


def publish_region_rule(db: Session, rule_id: str) -> dict[str, Any]:
    row = get_region_rule(db, rule_id)
    if row is None:
        raise RuleNotFoundError(f"region rule '{rule_id}' does not exist")
    if row.status != "draft":
        raise RuleStateError(
            f"only a draft rule can be published (current: {row.status})"
        )
    row = update_region_rule(db, rule_id, {"status": "published"})
    assert row is not None
    return _rule_out(row)


def retire_region_rule(db: Session, rule_id: str) -> dict[str, Any]:
    row = get_region_rule(db, rule_id)
    if row is None:
        raise RuleNotFoundError(f"region rule '{rule_id}' does not exist")
    if row.status == "retired":
        raise RuleStateError("rule is already retired")
    row = update_region_rule(db, rule_id, {"status": "retired"})
    assert row is not None
    return _rule_out(row)


def rollback_region_rule(
    db: Session, region: str, target_version: int, note: str = ""
) -> dict[str, Any]:
    """回滚到指定历史版本：以其上限与生效窗口创建一个新的已发布版本。

    规则解析时版本号最大者胜出，因此回滚版本会立即对实时评估生效；
    已冻结的快照记录了当时采用的规则，不受回滚影响。
    """
    target = get_region_rule_version(db, region, target_version)
    if target is None:
        raise RuleNotFoundError(
            f"region '{region}' has no rule version {target_version}"
        )
    if target.status != "published":
        raise RuleStateError(
            f"can only roll back to a published version (current: {target.status})"
        )
    existing = list_region_rules(db, region=region)
    next_version = max((r.version for r in existing), default=0) + 1
    row = insert_region_rule(
        db,
        {
            "rule_id": f"{region}-v{next_version}",
            "region": region,
            "version": next_version,
            "daily_cap_seconds": target.daily_cap_seconds,
            "effective_from": target.effective_from,
            "effective_to": target.effective_to,
            "status": "published",
            "rollback_of": target.rule_id,
            "note": note or f"rollback to version {target_version}",
        },
    )
    if row is None:  # pragma: no cover - 版本号刚算出即冲突，理论上不会发生
        raise RuleConflictError(f"region '{region}' version {next_version} exists")
    return _rule_out(row)


# ---------------------------------------------------------------------------
# 活动地点证据（位置 API）
# ---------------------------------------------------------------------------


def _evidence_out(row) -> dict[str, Any]:
    return {
        "evidence_id": row.evidence_id,
        "plan_version": row.plan_version,
        "activity_id": row.activity_id,
        "student_id": row.student_id,
        "region": row.region,
        "iana_timezone": row.iana_timezone,
        "valid_from": row.valid_from,
        "source": row.source,
        "note": row.note,
        "created_at": row.created_at,
    }


def add_location_evidence(
    db: Session, plan_version: str, evidence: dict[str, Any]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    row = insert_evidence(db, {"plan_version": plan_version, **evidence})
    if row is None:
        raise EvidenceConflictError(
            f"evidence '{evidence['evidence_id']}' already exists for this plan"
        )
    return _evidence_out(row)


def list_location_evidence(
    db: Session, plan_version: str, activity_id: str | None = None
) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    rows = list_evidence_rows(db, plan_version)
    out = [
        _evidence_out(row)
        for row in rows
        if activity_id is None or row.activity_id == activity_id
    ]
    out.sort(key=lambda e: (e["activity_id"], e["valid_from"], e["evidence_id"]))
    return out


# ---------------------------------------------------------------------------
# 地区合规评估（预览 / 复核 / 统计）
# ---------------------------------------------------------------------------


def _regional_reports(
    db: Session,
    plan,
    events: list | None = None,
    up_to_event_id: str | None = None,
) -> dict[str, StudentRegionalReport]:
    """重放事件并按当前规则/证据/例外评估地区每日工时合规。"""
    if events is None:
        events = load_events(db, plan.plan_version)
    state = replay(
        events,
        plan_version=plan.plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        up_to_event_id=up_to_event_id,
    )
    activities_by_student: dict[str, list[ActivityInterval]] = {}
    for student_id, progress in state.students.items():
        intervals = [
            ActivityInterval(
                event_id=record.event_id,
                activity_id=record.activity_id,
                start_utc=record.start_utc,
                end_utc=record.end_utc,
            )
            for record in progress.checkins
            if record.counts
        ]
        if intervals:
            activities_by_student[student_id] = intervals
    return evaluate_plan(
        activities_by_student,
        evidence=load_evidence(db, plan.plan_version),
        rules=load_published_rules(db),
        exceptions=load_exceptions(db, plan.plan_version),
        fallback_tz=plan.iana_timezone,
    )


def _regional_section(
    db: Session,
    plan,
    events: list | None = None,
    up_to_event_id: str | None = None,
) -> dict[str, Any]:
    reports = _regional_reports(db, plan, events, up_to_event_id)
    return {
        "students": [
            reports[sid].to_dict() for sid in sorted(reports)
        ]
    }


def preview_regional(
    db: Session, plan_version: str, student_id: str | None = None
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    reports = _regional_reports(db, plan)
    students = [
        reports[sid].to_dict()
        for sid in sorted(reports)
        if student_id is None or sid == student_id
    ]
    return {
        "plan_version": plan_version,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "students": students,
    }


def _review_index(db: Session, plan_version: str) -> dict[tuple, Any]:
    return {
        (d.student_id, d.region, d.local_day): d
        for d in list_review_decisions(db, plan_version)
    }


def list_regional_review(db: Session, plan_version: str) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    reports = _regional_reports(db, plan)
    decisions = _review_index(db, plan_version)
    findings = []
    for finding in build_findings(reports):
        key = (finding["student_id"], finding["region"], finding["local_day"])
        decision = decisions.get(key)
        if decision is not None:
            finding["review"] = {
                "decision": decision.decision,
                "reviewer": decision.reviewer,
                "reason": decision.reason,
                "exception_id": decision.exception_id,
            }
        else:
            finding["review"] = None
        findings.append(finding)
    return {
        "plan_version": plan_version,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "findings": findings,
    }


def review_regional_finding(
    db: Session,
    plan_version: str,
    *,
    student_id: str,
    region: str,
    local_day: str,
    decision: str,
    reviewer: str,
    reason: str,
    exception_id: str | None = None,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    if decision not in ("approved", "dismissed"):
        raise RuleStateError("decision must be 'approved' or 'dismissed'")
    if exception_id is not None:
        exc = get_exception(db, plan_version, exception_id)
        if exc is None:
            raise ManualExceptionNotFoundError(
                f"exception '{exception_id}' does not exist"
            )
    row = upsert_review_decision(
        db,
        {
            "plan_version": plan_version,
            "student_id": student_id,
            "region": region,
            "local_day": local_day,
            "decision": decision,
            "reviewer": reviewer,
            "reason": reason,
            "exception_id": exception_id,
        },
    )
    return {
        "plan_version": row.plan_version,
        "student_id": row.student_id,
        "region": row.region,
        "local_day": row.local_day,
        "decision": row.decision,
        "reviewer": row.reviewer,
        "reason": row.reason,
        "exception_id": row.exception_id,
        "created_at": row.created_at,
    }


def regional_statistics(db: Session, plan_version: str) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    reports = _regional_reports(db, plan)
    stats = build_statistics(reports)
    return {
        "plan_version": plan_version,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **stats,
    }


# ---------------------------------------------------------------------------
# 人工例外（必须有范围和期限）
# ---------------------------------------------------------------------------


def _exception_out(row) -> dict[str, Any]:
    return {
        "exception_id": row.exception_id,
        "plan_version": row.plan_version,
        "student_id": row.student_id,
        "region": row.region,
        "activity_id": row.activity_id,
        "local_day": row.local_day,
        "valid_from": row.valid_from,
        "valid_until": row.valid_until,
        "approver": row.approver,
        "reason": row.reason,
        "status": row.status,
        "created_at": row.created_at,
        "revoked_at": row.revoked_at,
    }


def create_manual_exception(
    db: Session, plan_version: str, body: dict[str, Any]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    if body["valid_until"] <= body["valid_from"]:
        raise RuleStateError("valid_until must be after valid_from")
    row = insert_exception(db, {"plan_version": plan_version, "status": "active", **body})
    if row is None:
        raise ExceptionConflictError(
            f"exception '{body['exception_id']}' already exists for this plan"
        )
    return _exception_out(row)


def list_manual_exceptions(db: Session, plan_version: str) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    return [_exception_out(r) for r in list_exception_rows(db, plan_version)]


def revoke_manual_exception(
    db: Session, plan_version: str, exception_id: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    row = get_exception(db, plan_version, exception_id)
    if row is None:
        raise ManualExceptionNotFoundError(
            f"exception '{exception_id}' does not exist"
        )
    if row.status == "revoked":
        return _exception_out(row)
    row = update_exception(
        db,
        plan_version,
        exception_id,
        {"status": "revoked", "revoked_at": datetime.now(timezone.utc)},
    )
    assert row is not None
    return _exception_out(row)
