"""跨地区合规数据访问层：地区、规则版本/生效窗口、地点证据、人工例外。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.clock import to_utc
from .core.regional import (
    EvidenceRecord,
    ExceptionGrant,
    Region,
    RuleVersion,
)
from .models import CapRule, CapRuleWindow, LocationEvidence, ManualException
from .models import Region as RegionModel


# draft -> published -> retired -> restored -> retired ...
RULE_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"published"}),
    "published": frozenset({"retired"}),
    "retired": frozenset({"restored"}),
    "restored": frozenset({"retired"}),
}


class RegionalError(ValueError):
    """跨地区合规领域错误。"""


def utc_iso(value: datetime) -> str:
    return to_utc(value).isoformat().replace("+00:00", "Z")


def parse_utc_iso(value: str) -> datetime:
    return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


# ---------------------------------------------------------------------------
# 地区
# ---------------------------------------------------------------------------

def upsert_region(
    db: Session,
    *,
    plan_version: str,
    region_code: str,
    iana_timezone: str,
    label: str = "",
) -> RegionModel:
    # 提前校验时区名，避免脏数据。
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(iana_timezone)
    except Exception as exc:  # pragma: no cover - zoneinfo raises ZoneInfoNotFoundError
        raise RegionalError(f"无效的 IANA 时区: {iana_timezone}") from exc

    stmt = sqlite_insert(RegionModel).values(
        plan_version=plan_version,
        region_code=region_code,
        iana_timezone=iana_timezone,
        label=label,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version", "region_code"],
        set_={"iana_timezone": iana_timezone, "label": label},
    )
    db.execute(stmt)
    db.commit()
    row = db.get(RegionModel, (plan_version, region_code))
    assert row is not None
    return row


def list_regions(db: Session, plan_version: str) -> list[RegionModel]:
    stmt = (
        select(RegionModel)
        .where(RegionModel.plan_version == plan_version)
        .order_by(RegionModel.region_code)
    )
    return list(db.execute(stmt).scalars().all())


def core_regions(rows: Sequence[RegionModel]) -> list[Region]:
    return [
        Region(
            region_code=r.region_code,
            iana_timezone=r.iana_timezone,
            label=r.label,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 规则版本与生效窗口
# ---------------------------------------------------------------------------

def get_rule(db: Session, plan_version: str, rule_id: str) -> CapRule | None:
    return db.get(CapRule, (plan_version, rule_id))


def list_rules(
    db: Session, plan_version: str, region_code: str | None = None
) -> list[CapRule]:
    stmt = select(CapRule).where(CapRule.plan_version == plan_version)
    if region_code is not None:
        stmt = stmt.where(CapRule.region_code == region_code)
    stmt = stmt.order_by(CapRule.region_code, CapRule.rule_id)
    return list(db.execute(stmt).scalars().all())


def list_windows(
    db: Session, plan_version: str, rule_id: str
) -> list[CapRuleWindow]:
    stmt = (
        select(CapRuleWindow)
        .where(CapRuleWindow.plan_version == plan_version)
        .where(CapRuleWindow.rule_id == rule_id)
        .order_by(CapRuleWindow.seq)
    )
    return list(db.execute(stmt).scalars().all())


def _max_window_seq(db: Session, plan_version: str, rule_id: str) -> int:
    stmt = (
        select(CapRuleWindow.seq)
        .where(CapRuleWindow.plan_version == plan_version)
        .where(CapRuleWindow.rule_id == rule_id)
        .order_by(CapRuleWindow.seq.desc())
        .limit(1)
    )
    value = db.execute(stmt).scalar_one_or_none()
    return int(value) if value is not None else 0


def _open_windows(
    db: Session, plan_version: str, region_code: str
) -> list[CapRuleWindow]:
    stmt = (
        select(CapRuleWindow)
        .join(
            CapRule,
            (CapRule.plan_version == CapRuleWindow.plan_version)
            & (CapRule.rule_id == CapRuleWindow.rule_id),
        )
        .where(CapRuleWindow.plan_version == plan_version)
        .where(CapRule.region_code == region_code)
        .where(CapRuleWindow.effective_to_utc.is_(None))
        .order_by(CapRuleWindow.seq)
    )
    return list(db.execute(stmt).scalars().all())


def create_rule(
    db: Session,
    *,
    plan_version: str,
    rule_id: str,
    region_code: str,
    daily_cap_seconds: int,
    created_by: str = "",
    reason: str = "",
    effective_from_utc: datetime | None = None,
) -> CapRule:
    if daily_cap_seconds < 0:
        raise RegionalError("每日工时上限不能为负")
    if db.get(CapRule, (plan_version, rule_id)) is not None:
        raise RegionalError(f"规则版本 '{rule_id}' 已存在")
    region = db.get(RegionModel, (plan_version, region_code))
    if region is None:
        raise RegionalError(f"地区 '{region_code}' 尚未注册")

    rule = CapRule(
        plan_version=plan_version,
        rule_id=rule_id,
        region_code=region_code,
        daily_cap_seconds=daily_cap_seconds,
        status="draft",
        reason=reason,
        created_by=created_by,
    )
    db.add(rule)
    db.flush()
    # 允许创建草稿时预置一个未来生效起点；为空则发布时刻开启窗口。
    if effective_from_utc is not None:
        db.add(
            CapRuleWindow(
                plan_version=plan_version,
                rule_id=rule_id,
                seq=1,
                effective_from_utc=utc_iso(effective_from_utc),
                effective_to_utc=None,
            )
        )
    db.commit()
    db.refresh(rule)
    return rule


def update_draft_cap(
    db: Session, *, plan_version: str, rule_id: str, daily_cap_seconds: int
) -> CapRule:
    rule = get_rule(db, plan_version, rule_id)
    if rule is None:
        raise RegionalError(f"规则版本 '{rule_id}' 不存在")
    if rule.status != "draft":
        raise RegionalError("只有草稿状态可以修改上限")
    if daily_cap_seconds < 0:
        raise RegionalError("每日工时上限不能为负")
    rule.daily_cap_seconds = daily_cap_seconds
    db.commit()
    db.refresh(rule)
    return rule


def _open_window(
    db: Session,
    *,
    plan_version: str,
    rule_id: str,
    region_code: str,
    from_utc: datetime,
) -> None:
    """开启新生效窗口，同时关闭同地区其他仍开放的窗口（窗口不重叠不变量）。

    被关闭窗口的规则自动进入 retired；之后允许通过 restored 回滚。
    """
    from_iso = utc_iso(from_utc)
    for window in _open_windows(db, plan_version, region_code):
        if window.rule_id == rule_id:
            continue
        if window.effective_from_utc < from_iso:
            window.effective_to_utc = from_iso
        other = get_rule(db, plan_version, window.rule_id)
        if other is not None and other.status in {"published", "restored"}:
            other.status = "retired"
            other.updated_at = from_utc
    seq = _max_window_seq(db, plan_version, rule_id) + 1
    db.add(
        CapRuleWindow(
            plan_version=plan_version,
            rule_id=rule_id,
            seq=seq,
            effective_from_utc=from_iso,
            effective_to_utc=None,
        )
    )


def _close_rule_window(
    db: Session, *, plan_version: str, rule_id: str, to_utc_value: datetime
) -> None:
    windows = list(
        db.execute(
            select(CapRuleWindow)
            .where(CapRuleWindow.plan_version == plan_version)
            .where(CapRuleWindow.rule_id == rule_id)
            .where(CapRuleWindow.effective_to_utc.is_(None))
        ).scalars()
    )
    to_iso = utc_iso(to_utc_value)
    for window in windows:
        if window.effective_from_utc < to_iso:
            window.effective_to_utc = to_iso
        else:
            # 尚未开始就被关闭：窗口退化为空（from == to），永不会被选中。
            window.effective_to_utc = window.effective_from_utc


def transition_rule(
    db: Session,
    *,
    plan_version: str,
    rule_id: str,
    target_status: str,
    now: datetime,
) -> CapRule:
    """发布 / 退役 / 恢复（回滚）规则版本。"""
    rule = get_rule(db, plan_version, rule_id)
    if rule is None:
        raise RegionalError(f"规则版本 '{rule_id}' 不存在")
    current = rule.status
    allowed = RULE_TRANSITIONS.get(current, frozenset())
    if target_status not in allowed:
        raise RegionalError(f"不允许规则从 {current} 变更到 {target_status}")

    instant = to_utc(now)
    if target_status == "published":
        existing = list_windows(db, plan_version, rule.rule_id)
        open_existing = [w for w in existing if w.effective_to_utc is None]
        if open_existing:
            # 草稿预置了生效起点：沿用该窗口，只需在其起点关闭同地区其他窗口。
            preset = open_existing[0].effective_from_utc
            for window in _open_windows(db, plan_version, rule.region_code):
                if window.rule_id == rule.rule_id:
                    continue
                close_at = max(preset, window.effective_from_utc)
                if window.effective_from_utc < close_at:
                    window.effective_to_utc = close_at
                other = get_rule(db, plan_version, window.rule_id)
                if other is not None and other.status in {"published", "restored"}:
                    other.status = "retired"
                    other.updated_at = instant
        else:
            _open_window(
                db,
                plan_version=plan_version,
                rule_id=rule_id,
                region_code=rule.region_code,
                from_utc=instant,
            )
    elif target_status == "retired":
        _close_rule_window(
            db,
            plan_version=plan_version,
            rule_id=rule_id,
            to_utc_value=instant,
        )
    elif target_status == "restored":
        _open_window(
            db,
            plan_version=plan_version,
            rule_id=rule_id,
            region_code=rule.region_code,
            from_utc=instant,
        )

    rule.status = target_status
    rule.updated_at = instant
    db.commit()
    db.refresh(rule)
    return rule


def rule_to_dict(rule: CapRule, windows: Sequence[CapRuleWindow]) -> dict[str, Any]:
    return {
        "rule_id": rule.rule_id,
        "region_code": rule.region_code,
        "daily_cap_seconds": rule.daily_cap_seconds,
        "status": rule.status,
        "reason": rule.reason,
        "created_by": rule.created_by,
        "windows": [
            {
                "seq": w.seq,
                "effective_from_utc": w.effective_from_utc,
                "effective_to_utc": w.effective_to_utc,
            }
            for w in windows
        ],
    }


def core_rules(db: Session, plan_version: str) -> list[RuleVersion]:
    """把规则行 + 生效窗口物化为引擎输入（每个窗口一条）。"""
    out: list[RuleVersion] = []
    for rule in list_rules(db, plan_version):
        windows = list_windows(db, plan_version, rule.rule_id)
        if not windows:
            continue
        for window in windows:
            frm = parse_utc_iso(window.effective_from_utc)
            to_value = (
                parse_utc_iso(window.effective_to_utc)
                if window.effective_to_utc
                else None
            )
            if to_value is not None and to_value <= frm:
                continue  # 空窗口
            out.append(
                RuleVersion(
                    rule_id=rule.rule_id,
                    region_code=rule.region_code,
                    daily_cap_seconds=rule.daily_cap_seconds,
                    status=rule.status,
                    effective_from_utc=frm,
                    effective_to_utc=to_value,
                    window_id=f"{rule.rule_id}#w{window.seq}",
                )
            )
    return out


def rule_catalog(db: Session, plan_version: str) -> list[dict[str, Any]]:
    return [
        rule_to_dict(rule, list_windows(db, plan_version, rule.rule_id))
        for rule in list_rules(db, plan_version)
    ]


# ---------------------------------------------------------------------------
# 地点证据
# ---------------------------------------------------------------------------

def upsert_evidence(
    db: Session,
    *,
    plan_version: str,
    evidence_id: str,
    student_id: str,
    region_code: str,
    observed_at: datetime,
    source: str = "",
    detail: dict[str, Any] | None = None,
) -> LocationEvidence:
    if db.get(RegionModel, (plan_version, region_code)) is None:
        raise RegionalError(f"地区 '{region_code}' 尚未注册")
    stmt = sqlite_insert(LocationEvidence).values(
        plan_version=plan_version,
        evidence_id=evidence_id,
        student_id=student_id,
        region_code=region_code,
        observed_at_utc=utc_iso(observed_at),
        source=source,
        detail=detail or {},
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version", "evidence_id"],
        set_={
            "student_id": student_id,
            "region_code": region_code,
            "observed_at_utc": utc_iso(observed_at),
            "source": source,
            "detail": detail or {},
        },
    )
    db.execute(stmt)
    db.commit()
    row = db.get(LocationEvidence, (plan_version, evidence_id))
    assert row is not None
    return row


def list_evidence(
    db: Session, plan_version: str, student_id: str | None = None
) -> list[LocationEvidence]:
    stmt = select(LocationEvidence).where(
        LocationEvidence.plan_version == plan_version
    )
    if student_id is not None:
        stmt = stmt.where(LocationEvidence.student_id == student_id)
    stmt = stmt.order_by(LocationEvidence.evidence_id)
    return list(db.execute(stmt).scalars().all())


def evidence_to_core(rows: Sequence[LocationEvidence]) -> list[EvidenceRecord]:
    return [
        EvidenceRecord(
            evidence_id=r.evidence_id,
            student_id=r.student_id,
            region_code=r.region_code,
            observed_at_utc=parse_utc_iso(r.observed_at_utc),
            source=r.source,
        )
        for r in rows
    ]


def evidence_to_dict(row: LocationEvidence) -> dict[str, Any]:
    return {
        "evidence_id": row.evidence_id,
        "student_id": row.student_id,
        "region_code": row.region_code,
        "observed_at_utc": row.observed_at_utc,
        "source": row.source,
        "detail": row.detail,
    }


# ---------------------------------------------------------------------------
# 人工例外
# ---------------------------------------------------------------------------

def create_exception(
    db: Session,
    *,
    plan_version: str,
    exception_id: str,
    student_id: str,
    valid_from: datetime,
    valid_to: datetime,
    region_code: str | None = None,
    activity_id: str | None = None,
    cap_override_seconds: int | None = None,
    reason: str = "",
) -> ManualException:
    if not student_id.strip():
        raise RegionalError("人工例外必须指定学员范围")
    frm, to_value = to_utc(valid_from), to_utc(valid_to)
    if to_value <= frm:
        raise RegionalError("人工例外必须具备有效的起止期限")
    if cap_override_seconds is not None and cap_override_seconds < 0:
        raise RegionalError("自定义上限不能为负")
    if db.get(ManualException, (plan_version, exception_id)) is not None:
        raise RegionalError(f"例外 '{exception_id}' 已存在")
    if region_code is not None and db.get(
        RegionModel, (plan_version, region_code)
    ) is None:
        raise RegionalError(f"地区 '{region_code}' 尚未注册")

    row = ManualException(
        plan_version=plan_version,
        exception_id=exception_id,
        student_id=student_id,
        region_code=region_code,
        activity_id=activity_id,
        valid_from_utc=utc_iso(frm),
        valid_to_utc=utc_iso(to_value),
        cap_override_seconds=cap_override_seconds,
        status="requested",
        reason=reason,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_exception(
    db: Session, plan_version: str, exception_id: str
) -> ManualException | None:
    return db.get(ManualException, (plan_version, exception_id))


def list_exceptions(
    db: Session,
    plan_version: str,
    *,
    status: str | None = None,
    student_id: str | None = None,
) -> list[ManualException]:
    stmt = select(ManualException).where(
        ManualException.plan_version == plan_version
    )
    if status is not None:
        stmt = stmt.where(ManualException.status == status)
    if student_id is not None:
        stmt = stmt.where(ManualException.student_id == student_id)
    stmt = stmt.order_by(ManualException.exception_id)
    return list(db.execute(stmt).scalars().all())


def review_exception(
    db: Session,
    *,
    plan_version: str,
    exception_id: str,
    approve: bool,
    reviewed_by: str,
    now: datetime,
    reason: str = "",
    cap_override_seconds: int | None = None,
) -> ManualException:
    row = get_exception(db, plan_version, exception_id)
    if row is None:
        raise RegionalError(f"例外 '{exception_id}' 不存在")
    if row.status != "requested":
        raise RegionalError(f"例外处于 {row.status} 状态，不能复核")
    if not reviewed_by.strip():
        raise RegionalError("复核必须记录复核人")
    if approve:
        if cap_override_seconds is not None:
            if cap_override_seconds < 0:
                raise RegionalError("自定义上限不能为负")
            row.cap_override_seconds = cap_override_seconds
        row.status = "approved"
    else:
        row.status = "rejected"
    row.reviewed_by = reviewed_by
    row.reviewed_at = utc_iso(to_utc(now))
    if reason:
        row.reason = reason
    db.commit()
    db.refresh(row)
    return row


def revoke_exception(
    db: Session,
    *,
    plan_version: str,
    exception_id: str,
    reviewed_by: str,
    now: datetime,
) -> ManualException:
    row = get_exception(db, plan_version, exception_id)
    if row is None:
        raise RegionalError(f"例外 '{exception_id}' 不存在")
    if row.status not in {"approved"}:
        raise RegionalError("只有已批准的例外可以撤销")
    row.status = "revoked"
    row.reviewed_by = reviewed_by
    row.reviewed_at = utc_iso(to_utc(now))
    db.commit()
    db.refresh(row)
    return row


def exception_to_dict(row: ManualException) -> dict[str, Any]:
    return {
        "exception_id": row.exception_id,
        "student_id": row.student_id,
        "region_code": row.region_code,
        "activity_id": row.activity_id,
        "valid_from_utc": row.valid_from_utc,
        "valid_to_utc": row.valid_to_utc,
        "cap_override_seconds": row.cap_override_seconds,
        "status": row.status,
        "reason": row.reason,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": row.reviewed_at,
    }


def exceptions_to_core(rows: Sequence[ManualException]) -> list[ExceptionGrant]:
    out: list[ExceptionGrant] = []
    for row in rows:
        out.append(
            ExceptionGrant(
                exception_id=row.exception_id,
                student_id=row.student_id,
                region_code=row.region_code,
                activity_id=row.activity_id,
                valid_from_utc=parse_utc_iso(row.valid_from_utc),
                valid_to_utc=parse_utc_iso(row.valid_to_utc),
                status=row.status,
                cap_override_seconds=row.cap_override_seconds,
            )
        )
    return out
