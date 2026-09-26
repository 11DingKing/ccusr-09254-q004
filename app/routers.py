"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    LocationEvidenceIn,
    LocationEvidenceListOut,
    LocationEvidenceOut,
    ManualExceptionIn,
    ManualExceptionListOut,
    ManualExceptionOut,
    PlanIn,
    PlanOut,
    RegionalPreviewOut,
    RegionalReviewOut,
    RegionalStatisticsOut,
    RegionRuleIn,
    RegionRuleListOut,
    RegionRuleOut,
    ReviewDecisionIn,
    ReviewDecisionOut,
    RollbackIn,
    SnapshotOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 地区规则版本
# ---------------------------------------------------------------------------


@router.post(
    "/region-rules",
    response_model=RegionRuleOut,
    status_code=status.HTTP_201_CREATED,
)
def create_region_rule(body: RegionRuleIn, db: Session = Depends(get_db)) -> Any:
    try:
        return services.create_region_rule(
            db,
            rule_id=body.rule_id,
            region=body.region,
            version=body.version,
            daily_cap_seconds=body.daily_cap_seconds,
            effective_from=body.effective_from,
            effective_to=body.effective_to,
            note=body.note,
        )
    except services.RuleConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except services.RuleStateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/region-rules", response_model=RegionRuleListOut)
def list_region_rules(
    region: str | None = None, db: Session = Depends(get_db)
) -> Any:
    return {"rules": services.list_region_rule_versions(db, region)}


@router.get("/region-rules/{rule_id}", response_model=RegionRuleOut)
def get_region_rule(rule_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.get_region_rule_out(db, rule_id)
    except services.RuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/region-rules/{rule_id}/publish", response_model=RegionRuleOut)
def publish_region_rule(rule_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.publish_region_rule(db, rule_id)
    except services.RuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RuleStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/region-rules/{rule_id}/retire", response_model=RegionRuleOut)
def retire_region_rule(rule_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.retire_region_rule(db, rule_id)
    except services.RuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RuleStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/regions/{region}/rollback",
    response_model=RegionRuleOut,
    status_code=status.HTTP_201_CREATED,
)
def rollback_region(
    region: str, body: RollbackIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.rollback_region_rule(
            db, region, body.target_version, note=body.note
        )
    except services.RuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RuleStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 活动地点证据
# ---------------------------------------------------------------------------


@router.post(
    "/plans/{plan_version}/locations",
    response_model=LocationEvidenceOut,
    status_code=status.HTTP_201_CREATED,
)
def post_location_evidence(
    plan_version: str, body: LocationEvidenceIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.add_location_evidence(
            db, plan_version, body.model_dump()
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.EvidenceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/locations",
    response_model=LocationEvidenceListOut,
)
def list_locations(
    plan_version: str,
    activity_id: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return {
            "evidence": services.list_location_evidence(
                db, plan_version, activity_id
            )
        }
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 地区合规：预览 / 复核 / 统计
# ---------------------------------------------------------------------------


@router.get("/plans/{plan_version}/preview", response_model=RegionalPreviewOut)
def get_regional_preview(
    plan_version: str,
    student_id: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.preview_regional(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/plans/{plan_version}/review", response_model=RegionalReviewOut)
def get_regional_review(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.list_regional_review(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/review",
    response_model=ReviewDecisionOut,
    status_code=status.HTTP_201_CREATED,
)
def post_review_decision(
    plan_version: str, body: ReviewDecisionIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.review_regional_finding(
            db,
            plan_version,
            student_id=body.student_id,
            region=body.region,
            local_day=body.local_day,
            decision=body.decision,
            reviewer=body.reviewer,
            reason=body.reason,
            exception_id=body.exception_id,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.ManualExceptionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RuleStateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/plans/{plan_version}/statistics", response_model=RegionalStatisticsOut)
def get_regional_statistics(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.regional_statistics(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 人工例外
# ---------------------------------------------------------------------------


@router.post(
    "/plans/{plan_version}/exceptions",
    response_model=ManualExceptionOut,
    status_code=status.HTTP_201_CREATED,
)
def post_manual_exception(
    plan_version: str, body: ManualExceptionIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.create_manual_exception(
            db, plan_version, body.model_dump()
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.ExceptionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except services.RuleStateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/exceptions",
    response_model=ManualExceptionListOut,
)
def list_manual_exceptions(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return {"exceptions": services.list_manual_exceptions(db, plan_version)}
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/exceptions/{exception_id}/revoke",
    response_model=ManualExceptionOut,
)
def revoke_manual_exception(
    plan_version: str, exception_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.revoke_manual_exception(db, plan_version, exception_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.ManualExceptionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
