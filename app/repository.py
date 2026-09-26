"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.regional import ExceptionGrant, LocationPoint, RegionRuleVersion
from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import (
    Freeze,
    LocationEvidence,
    ManualException,
    Plan,
    RegionRule,
    ReviewDecision,
)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 不保留时区，读取时统一按 UTC 还原。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


# ---------------------------------------------------------------------------
# 地区规则版本
# ---------------------------------------------------------------------------


def _rule_to_core(row: RegionRule) -> RegionRuleVersion:
    return RegionRuleVersion(
        rule_id=row.rule_id,
        region=row.region,
        version=row.version,
        daily_cap_seconds=row.daily_cap_seconds,
        effective_from=_as_utc(row.effective_from),  # type: ignore[arg-type]
        effective_to=_as_utc(row.effective_to),
        status=row.status,
        rollback_of=row.rollback_of,
    )


def get_region_rule(db: Session, rule_id: str) -> RegionRule | None:
    return db.get(RegionRule, rule_id)


def get_region_rule_version(
    db: Session, region: str, version: int
) -> RegionRule | None:
    stmt = select(RegionRule).where(
        RegionRule.region == region, RegionRule.version == version
    )
    return db.execute(stmt).scalar_one_or_none()


def insert_region_rule(db: Session, values: dict[str, Any]) -> RegionRule | None:
    """插入一个规则版本；(region, version) 冲突时不覆盖。"""
    data = dict(values)
    for key in ("effective_from", "effective_to"):
        if data.get(key) is not None:
            data[key] = _as_utc(data[key])
    stmt = sqlite_insert(RegionRule).values(**data)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["region", "version"]
    ).returning(RegionRule.rule_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(RegionRule, data["rule_id"])


def update_region_rule(
    db: Session, rule_id: str, changes: dict[str, Any]
) -> RegionRule | None:
    row = db.get(RegionRule, rule_id)
    if row is None:
        return None
    data = dict(changes)
    for key in ("effective_from", "effective_to"):
        if data.get(key) is not None:
            data[key] = _as_utc(data[key])
    for key, value in data.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


def list_region_rules(
    db: Session, *, region: str | None = None, status: str | None = None
) -> list[RegionRule]:
    stmt = select(RegionRule).order_by(RegionRule.region, RegionRule.version)
    if region is not None:
        stmt = stmt.where(RegionRule.region == region)
    if status is not None:
        stmt = stmt.where(RegionRule.status == status)
    return list(db.execute(stmt).scalars().all())


def load_published_rules(db: Session) -> list[RegionRuleVersion]:
    rows = list_region_rules(db, status="published")
    return [_rule_to_core(r) for r in rows]


# ---------------------------------------------------------------------------
# 活动地点证据
# ---------------------------------------------------------------------------


def _evidence_to_core(row: LocationEvidence) -> LocationPoint:
    return LocationPoint(
        evidence_id=row.evidence_id,
        activity_id=row.activity_id,
        student_id=row.student_id,
        region=row.region,
        iana_timezone=row.iana_timezone,
        valid_from=_as_utc(row.valid_from),  # type: ignore[arg-type]
        source=row.source,
    )


def get_evidence(db: Session, plan_version: str, evidence_id: str) -> LocationEvidence | None:
    stmt = select(LocationEvidence).where(
        LocationEvidence.plan_version == plan_version,
        LocationEvidence.evidence_id == evidence_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def insert_evidence(db: Session, values: dict[str, Any]) -> LocationEvidence | None:
    data = dict(values)
    data["valid_from"] = _as_utc(data["valid_from"])
    stmt = sqlite_insert(LocationEvidence).values(**data)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "evidence_id"]
    ).returning(LocationEvidence.id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return get_evidence(db, data["plan_version"], data["evidence_id"])


def load_evidence(db: Session, plan_version: str) -> list[LocationPoint]:
    stmt = (
        select(LocationEvidence)
        .where(LocationEvidence.plan_version == plan_version)
        .order_by(LocationEvidence.evidence_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_evidence_to_core(r) for r in rows]


def list_evidence_rows(db: Session, plan_version: str) -> list[LocationEvidence]:
    stmt = (
        select(LocationEvidence)
        .where(LocationEvidence.plan_version == plan_version)
        .order_by(LocationEvidence.evidence_id)
    )
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# 人工例外
# ---------------------------------------------------------------------------


def _exception_to_core(row: ManualException) -> ExceptionGrant:
    return ExceptionGrant(
        exception_id=row.exception_id,
        student_id=row.student_id,
        region=row.region,
        activity_id=row.activity_id,
        local_day=row.local_day,
        valid_from=_as_utc(row.valid_from),  # type: ignore[arg-type]
        valid_until=_as_utc(row.valid_until),  # type: ignore[arg-type]
        status=row.status,
    )


def get_exception(
    db: Session, plan_version: str, exception_id: str
) -> ManualException | None:
    stmt = select(ManualException).where(
        ManualException.plan_version == plan_version,
        ManualException.exception_id == exception_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def insert_exception(db: Session, values: dict[str, Any]) -> ManualException | None:
    data = dict(values)
    data["valid_from"] = _as_utc(data["valid_from"])
    data["valid_until"] = _as_utc(data["valid_until"])
    stmt = sqlite_insert(ManualException).values(**data)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "exception_id"]
    ).returning(ManualException.id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return get_exception(db, data["plan_version"], data["exception_id"])


def update_exception(
    db: Session, plan_version: str, exception_id: str, changes: dict[str, Any]
) -> ManualException | None:
    row = get_exception(db, plan_version, exception_id)
    if row is None:
        return None
    for key, value in changes.items():
        setattr(row, key, _as_utc(value) if key in {"valid_from", "valid_until"} else value)
    db.commit()
    db.refresh(row)
    return row


def load_exceptions(db: Session, plan_version: str) -> list[ExceptionGrant]:
    stmt = (
        select(ManualException)
        .where(ManualException.plan_version == plan_version)
        .order_by(ManualException.exception_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_exception_to_core(r) for r in rows]


def list_exception_rows(db: Session, plan_version: str) -> list[ManualException]:
    stmt = (
        select(ManualException)
        .where(ManualException.plan_version == plan_version)
        .order_by(ManualException.exception_id)
    )
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# 复核结论
# ---------------------------------------------------------------------------


def upsert_review_decision(db: Session, values: dict[str, Any]) -> ReviewDecision:
    data = dict(values)
    stmt = sqlite_insert(ReviewDecision).values(**data)
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version", "student_id", "region", "local_day"],
        set_={
            "decision": data["decision"],
            "reviewer": data["reviewer"],
            "reason": data["reason"],
            "exception_id": data.get("exception_id"),
        },
    )
    db.execute(stmt)
    db.commit()
    return get_review_decision(
        db, data["plan_version"], data["student_id"], data["region"], data["local_day"]
    )  # type: ignore[return-value]


def get_review_decision(
    db: Session, plan_version: str, student_id: str, region: str, local_day: str
) -> ReviewDecision | None:
    stmt = select(ReviewDecision).where(
        ReviewDecision.plan_version == plan_version,
        ReviewDecision.student_id == student_id,
        ReviewDecision.region == region,
        ReviewDecision.local_day == local_day,
    )
    return db.execute(stmt).scalar_one_or_none()


def list_review_decisions(db: Session, plan_version: str) -> list[ReviewDecision]:
    stmt = (
        select(ReviewDecision)
        .where(ReviewDecision.plan_version == plan_version)
        .order_by(
            ReviewDecision.student_id, ReviewDecision.region, ReviewDecision.local_day
        )
    )
    return list(db.execute(stmt).scalars().all())
