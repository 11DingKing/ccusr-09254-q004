"""跨地区工时上限合规 API。

规则：  地区注册、规则版本创建/发布/退役/回滚
位置：  地点证据登记
预览：  草稿打卡的上限影响
复核：  人工例外的申请/批准/驳回/撤销
统计：  总览、学员明细、聚合统计
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import regional_services, regional_store
from .db import get_db
from .repository import get_plan
from .schemas import (
    CapRuleIn,
    CapRuleOut,
    CapRulePatchIn,
    EvidenceIn,
    EvidenceOut,
    ExceptionIn,
    ExceptionOut,
    ExceptionReviewIn,
    ExceptionRevokeIn,
    PreviewIn,
    RegionIn,
    RegionOut,
    RegionalStatisticsOut,
    RuleTransitionIn,
)
from .services import PlanNotFoundError


def _require_plan(plan_version: str, db: Session = Depends(get_db)) -> None:
    if get_plan(db, plan_version) is None:
        raise HTTPException(
            status_code=404,
            detail=f"plan version '{plan_version}' is not registered",
        )


router = APIRouter(
    prefix="/api/plans/{plan_version}",
    dependencies=[Depends(_require_plan)],
)


def _now(body_now: datetime | None) -> datetime:
    if body_now is not None:
        return body_now.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _guard(exc: Exception) -> HTTPException:
    if isinstance(exc, PlanNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# 地区
# ---------------------------------------------------------------------------

@router.put(
    "/regions/{region_code}",
    response_model=RegionOut,
)
def put_region(
    plan_version: str,
    region_code: str,
    body: RegionIn,
    db: Session = Depends(get_db),
) -> Any:
    if body.region_code != region_code:
        raise HTTPException(status_code=400, detail="region_code 与路径不一致")
    try:
        row = regional_store.upsert_region(
            db,
            plan_version=plan_version,
            region_code=region_code,
            iana_timezone=body.iana_timezone,
            label=body.label,
        )
    except regional_store.RegionalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "plan_version": plan_version,
        "region_code": row.region_code,
        "iana_timezone": row.iana_timezone,
        "label": row.label,
    }


@router.get("/regions", response_model=list[RegionOut])
def list_regions(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        rows = regional_store.list_regions(db, plan_version)
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [
        {
            "plan_version": plan_version,
            "region_code": r.region_code,
            "iana_timezone": r.iana_timezone,
            "label": r.label,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 规则版本
# ---------------------------------------------------------------------------

@router.post(
    "/cap-rules",
    response_model=CapRuleOut,
    status_code=status.HTTP_201_CREATED,
)
def create_cap_rule(
    plan_version: str, body: CapRuleIn, db: Session = Depends(get_db)
) -> Any:
    try:
        regional_store.create_rule(
            db,
            plan_version=plan_version,
            rule_id=body.rule_id,
            region_code=body.region_code,
            daily_cap_seconds=body.daily_cap_seconds,
            created_by=body.created_by,
            reason=body.reason,
            effective_from_utc=body.effective_from_utc,
        )
        rule = regional_store.get_rule(db, plan_version, body.rule_id)
        assert rule is not None
        windows = regional_store.list_windows(db, plan_version, body.rule_id)
    except regional_store.RegionalError as exc:
        raise _guard(exc) from exc
    return regional_store.rule_to_dict(rule, windows)


@router.get("/cap-rules", response_model=list[CapRuleOut])
def list_cap_rules(
    plan_version: str,
    region_code: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    return [
        regional_store.rule_to_dict(
            rule, regional_store.list_windows(db, plan_version, rule.rule_id)
        )
        for rule in regional_store.list_rules(db, plan_version, region_code)
    ]


@router.get("/cap-rules/{rule_id}", response_model=CapRuleOut)
def get_cap_rule(plan_version: str, rule_id: str, db: Session = Depends(get_db)) -> Any:
    rule = regional_store.get_rule(db, plan_version, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return regional_store.rule_to_dict(
        rule, regional_store.list_windows(db, plan_version, rule_id)
    )


@router.patch("/cap-rules/{rule_id}", response_model=CapRuleOut)
def patch_cap_rule(
    plan_version: str,
    rule_id: str,
    body: CapRulePatchIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        rule = regional_store.update_draft_cap(
            db,
            plan_version=plan_version,
            rule_id=rule_id,
            daily_cap_seconds=body.daily_cap_seconds,
        )
    except regional_store.RegionalError as exc:
        raise _guard(exc) from exc
    return regional_store.rule_to_dict(
        rule, regional_store.list_windows(db, plan_version, rule_id)
    )


@router.post("/cap-rules/{rule_id}/transition", response_model=CapRuleOut)
def transition_cap_rule(
    plan_version: str,
    rule_id: str,
    body: RuleTransitionIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        rule = regional_store.transition_rule(
            db,
            plan_version=plan_version,
            rule_id=rule_id,
            target_status=body.status,
            now=_now(body.now),
        )
    except regional_store.RegionalError as exc:
        raise _guard(exc) from exc
    return regional_store.rule_to_dict(
        rule, regional_store.list_windows(db, plan_version, rule_id)
    )


# ---------------------------------------------------------------------------
# 地点证据
# ---------------------------------------------------------------------------

@router.put(
    "/evidence/{evidence_id}",
    response_model=EvidenceOut,
)
def put_evidence(
    plan_version: str,
    evidence_id: str,
    body: EvidenceIn,
    db: Session = Depends(get_db),
) -> Any:
    if body.evidence_id != evidence_id:
        raise HTTPException(status_code=400, detail="evidence_id 与路径不一致")
    try:
        row = regional_store.upsert_evidence(
            db,
            plan_version=plan_version,
            evidence_id=evidence_id,
            student_id=body.student_id,
            region_code=body.region_code,
            observed_at=body.observed_at,
            source=body.source,
            detail=body.detail,
        )
    except regional_store.RegionalError as exc:
        raise _guard(exc) from exc
    return regional_store.evidence_to_dict(row)


@router.get("/evidence", response_model=list[EvidenceOut])
def list_evidence(
    plan_version: str,
    student_id: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    return [
        regional_store.evidence_to_dict(r)
        for r in regional_store.list_evidence(db, plan_version, student_id)
    ]


# ---------------------------------------------------------------------------
# 预览
# ---------------------------------------------------------------------------

@router.post("/regional/preview")
def post_preview(
    plan_version: str, body: PreviewIn, db: Session = Depends(get_db)
) -> Any:
    drafts = [
        {
            "event_id": e.event_id,
            "student_id": e.student_id,
            "payload": {
                "activity_id": e.activity_id,
                "activity_type": e.activity_type,
                "check_in_at": e.check_in_at.isoformat(),
                "check_out_at": e.check_out_at.isoformat(),
                "location": (
                    {"region_code": e.location.region_code, "evidence_id": e.location.evidence_id}
                    if e.location
                    else None
                ),
                "track": (
                    [
                        {
                            "at": p.at.isoformat(),
                            "region_code": p.region_code,
                            "evidence_id": p.evidence_id,
                        }
                        for p in e.track
                    ]
                    if e.track
                    else None
                ),
            },
        }
        for e in body.events
    ]
    try:
        return regional_services.preview(
            db, plan_version, drafts, include_confirmed=body.include_confirmed
        )
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 人工例外复核
# ---------------------------------------------------------------------------

@router.post(
    "/exceptions",
    response_model=ExceptionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_exception(
    plan_version: str, body: ExceptionIn, db: Session = Depends(get_db)
) -> Any:
    try:
        row = regional_store.create_exception(
            db,
            plan_version=plan_version,
            exception_id=body.exception_id,
            student_id=body.student_id,
            region_code=body.region_code,
            activity_id=body.activity_id,
            valid_from=body.valid_from_utc,
            valid_to=body.valid_to_utc,
            cap_override_seconds=body.cap_override_seconds,
            reason=body.reason,
        )
    except regional_store.RegionalError as exc:
        raise _guard(exc) from exc
    return regional_store.exception_to_dict(row)


@router.get("/exceptions", response_model=list[ExceptionOut])
def list_exceptions(
    plan_version: str,
    status_filter: str | None = None,
    student_id: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    return [
        regional_store.exception_to_dict(r)
        for r in regional_store.list_exceptions(
            db, plan_version, status=status_filter, student_id=student_id
        )
    ]


@router.get("/exceptions/{exception_id}", response_model=ExceptionOut)
def get_exception(
    plan_version: str, exception_id: str, db: Session = Depends(get_db)
) -> Any:
    row = regional_store.get_exception(db, plan_version, exception_id)
    if row is None:
        raise HTTPException(status_code=404, detail="exception not found")
    return regional_store.exception_to_dict(row)


@router.post("/exceptions/{exception_id}/review", response_model=ExceptionOut)
def review_exception(
    plan_version: str,
    exception_id: str,
    body: ExceptionReviewIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        row = regional_store.review_exception(
            db,
            plan_version=plan_version,
            exception_id=exception_id,
            approve=body.approve,
            reviewed_by=body.reviewed_by,
            now=datetime.now(timezone.utc),
            reason=body.reason,
            cap_override_seconds=body.cap_override_seconds,
        )
    except regional_store.RegionalError as exc:
        raise _guard(exc) from exc
    return regional_store.exception_to_dict(row)


@router.post("/exceptions/{exception_id}/revoke", response_model=ExceptionOut)
def revoke_exception(
    plan_version: str,
    exception_id: str,
    body: ExceptionRevokeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        row = regional_store.revoke_exception(
            db,
            plan_version=plan_version,
            exception_id=exception_id,
            reviewed_by=body.reviewed_by,
            now=_now(body.now),
        )
    except regional_store.RegionalError as exc:
        raise _guard(exc) from exc
    return regional_store.exception_to_dict(row)


# ---------------------------------------------------------------------------
# 评估与统计
# ---------------------------------------------------------------------------

@router.get("/regional/overview")
def get_overview(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return regional_services.overview(db, plan_version)
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/regional/students/{student_id}")
def get_regional_student(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = regional_services.student_detail(db, plan_version, student_id)
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get("/regional/statistics", response_model=RegionalStatisticsOut)
def get_statistics(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return regional_services.statistics(db, plan_version)
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
