"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class Region(Base):
    """培养方案内可引用的地区，携带判定当地日所用的 IANA 时区。"""

    __tablename__ = "regions"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    region_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class CapRule(Base):
    """地区每日工时上限规则版本（草稿/发布/退役/恢复）。

    回滚通过追加新的生效窗口实现，因此同一 rule_id 在历史中可以对应
    多段不相交的生效区间；窗口保存在 CapRuleWindow 中。
    """

    __tablename__ = "daily_cap_rules"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    region_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    daily_cap_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("daily_cap_seconds >= 0", name="ck_cap_rules_nonneg"),
    )


class CapRuleWindow(Base):
    """规则版本的一段生效窗口：半开区间 [effective_from, effective_to)。"""

    __tablename__ = "cap_rule_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    rule_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from_utc: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_to_utc: Mapped[str | None] = mapped_column(String(40), nullable=True)

    __table_args__ = (
        UniqueConstraint("plan_version", "rule_id", "seq", name="uq_cap_windows_seq"),
    )


class LocationEvidence(Base):
    """活动地点证据：某学员在某时刻位于某地区的凭证（打卡定位、边检记录等）。"""

    __tablename__ = "location_evidence"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    region_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    observed_at_utc: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class ManualException(Base):
    """人工例外：必须有范围（学员必填，地区/活动收窄）和有效期限。"""

    __tablename__ = "manual_exceptions"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    exception_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    region_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    activity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    valid_from_utc: Mapped[str] = mapped_column(String(40), nullable=False)
    valid_to_utc: Mapped[str] = mapped_column(String(40), nullable=False)
    cap_override_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="requested"
    )  # requested / approved / rejected / revoked
    reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "cap_override_seconds IS NULL OR cap_override_seconds >= 0",
            name="ck_exceptions_override_nonneg",
        ),
    )
