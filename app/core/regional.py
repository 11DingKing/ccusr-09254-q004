"""跨地区每日工时上限合规评估（纯领域逻辑）。

输入已确认的签到区间、活动地点证据、地区规则版本和人工例外，
先把每个活动按位置变更点分段，再按各位置自己的时区切分当地
自然日，最后识别超出当地每日工时上限的具体部分。模块不访问
数据库，所有函数对相同输入返回相同结果，便于确定性重放与测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from .clock import (
    elapsed_seconds,
    local_midnight_as_utc,
    merge_intervals,
    split_by_academic_day,
    to_utc,
)

# 没有位置证据时回落到的“本校”地区，时区取培养方案时区。
FALLBACK_REGION = "home"

RULE_STATUS_DRAFT = "draft"
RULE_STATUS_PUBLISHED = "published"
RULE_STATUS_RETIRED = "retired"

EXCEPTION_STATUS_ACTIVE = "active"
EXCEPTION_STATUS_REVOKED = "revoked"


def _z(value: datetime) -> str:
    return to_utc(value).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RegionRuleVersion:
    """地区每日工时上限规则的一个版本。"""

    rule_id: str
    region: str
    version: int
    daily_cap_seconds: int
    effective_from: datetime
    effective_to: datetime | None
    status: str
    rollback_of: str | None = None

    def covers(self, moment_utc: datetime) -> bool:
        moment = to_utc(moment_utc)
        if moment < to_utc(self.effective_from):
            return False
        if self.effective_to is not None and moment >= to_utc(self.effective_to):
            return False
        return True


@dataclass(frozen=True)
class LocationPoint:
    """一条活动地点证据：自 valid_from 起活动位于该地区。"""

    evidence_id: str
    activity_id: str
    student_id: str | None
    region: str
    iana_timezone: str
    valid_from: datetime
    source: str = "manual"


@dataclass(frozen=True)
class ExceptionGrant:
    """人工例外：仅在期限与范围内豁免超限部分。"""

    exception_id: str
    student_id: str
    region: str
    activity_id: str | None
    local_day: str | None  # ISO 日期，按地区当地日历解释
    valid_from: datetime
    valid_until: datetime
    status: str


@dataclass(frozen=True)
class ActivityInterval:
    """一条已确认的签到区间。"""

    event_id: str
    activity_id: str
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True)
class _RawSegment:
    event_id: str
    activity_id: str
    region: str
    iana_timezone: str
    local_day: date
    start_utc: datetime
    end_utc: datetime

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)


@dataclass
class SegmentReport:
    """单个位置/自然日分段的合规解释。"""

    event_id: str
    activity_id: str
    region: str
    iana_timezone: str
    local_day: date
    start_utc: datetime
    end_utc: datetime
    seconds: int
    rule_id: str | None
    rule_version: int | None
    cap_seconds: int | None
    allowed_seconds: int
    excess_seconds: int
    waived_seconds: int
    exception_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "activity_id": self.activity_id,
            "region": self.region,
            "iana_timezone": self.iana_timezone,
            "local_day": self.local_day.isoformat(),
            "start_utc": _z(self.start_utc),
            "end_utc": _z(self.end_utc),
            "seconds": self.seconds,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "cap_seconds": self.cap_seconds,
            "allowed_seconds": self.allowed_seconds,
            "excess_seconds": self.excess_seconds,
            "waived_seconds": self.waived_seconds,
            "exception_ids": list(self.exception_ids),
        }


@dataclass
class DayReport:
    """某学员在某地区某个当地自然日的汇总。"""

    region: str
    iana_timezone: str
    local_day: date
    total_seconds: int
    cap_seconds: int | None
    rule_id: str | None
    rule_version: int | None
    excess_seconds: int
    waived_seconds: int
    net_excess_seconds: int
    exception_ids: list[str]

    @property
    def status(self) -> str:
        if self.excess_seconds <= 0:
            return "compliant"
        if self.net_excess_seconds <= 0:
            return "waived"
        return "open"

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "iana_timezone": self.iana_timezone,
            "local_day": self.local_day.isoformat(),
            "total_seconds": self.total_seconds,
            "cap_seconds": self.cap_seconds,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "excess_seconds": self.excess_seconds,
            "waived_seconds": self.waived_seconds,
            "net_excess_seconds": self.net_excess_seconds,
            "status": self.status,
            "exception_ids": list(self.exception_ids),
        }


@dataclass
class StudentRegionalReport:
    student_id: str
    evaluated_seconds: int
    excess_seconds: int
    waived_seconds: int
    net_excess_seconds: int
    days: list[DayReport]
    segments: list[SegmentReport]

    def to_dict(self) -> dict[str, Any]:
        return {
            "student_id": self.student_id,
            "evaluated_seconds": self.evaluated_seconds,
            "excess_seconds": self.excess_seconds,
            "waived_seconds": self.waived_seconds,
            "net_excess_seconds": self.net_excess_seconds,
            "days": [d.to_dict() for d in self.days],
            "segments": [s.to_dict() for s in self.segments],
        }


def resolve_rule(
    rules: Iterable[RegionRuleVersion], region: str, moment_utc: datetime
) -> RegionRuleVersion | None:
    """解析某地区在某时刻适用的规则版本。

    只考虑已发布且生效窗口覆盖该时刻的版本；若有多个（例如回滚
    版本与原版本窗口重叠），版本号最大者胜出。
    """
    candidates = [
        r
        for r in rules
        if r.region == region
        and r.status == RULE_STATUS_PUBLISHED
        and r.covers(moment_utc)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.version)


def segment_by_location(
    start_utc: datetime,
    end_utc: datetime,
    points: Iterable[LocationPoint],
    fallback_tz: str,
) -> list[tuple[str, str, datetime, datetime]]:
    """在位置变更点把活动分段，返回 (region, tz, start, end) 列表。

    每条证据自其 valid_from 起生效，直到下一条证据；首个证据之前
    的部分回落到本校地区与方案时区。相邻且位置相同的段会被合并。
    """
    start = to_utc(start_utc)
    end = to_utc(end_utc)
    if end <= start:
        return []
    ordered = sorted(points, key=lambda p: (to_utc(p.valid_from), p.evidence_id))
    cuts = sorted(
        {to_utc(p.valid_from) for p in ordered if start < to_utc(p.valid_from) < end}
    )
    bounds = [start, *cuts, end]
    segments: list[tuple[str, str, datetime, datetime]] = []
    for seg_start, seg_end in zip(bounds, bounds[1:]):
        region, tz_name = FALLBACK_REGION, fallback_tz
        for point in ordered:
            if to_utc(point.valid_from) <= seg_start:
                region, tz_name = point.region, point.iana_timezone
            else:
                break
        if (
            segments
            and segments[-1][0] == region
            and segments[-1][1] == tz_name
            and segments[-1][3] == seg_start
        ):
            segments[-1] = (region, tz_name, segments[-1][2], seg_end)
        else:
            segments.append((region, tz_name, seg_start, seg_end))
    return segments


def _excess_intervals(
    merged: list[tuple[datetime, datetime]], cap_seconds: int
) -> list[tuple[datetime, datetime]]:
    """在合并后的时间线上，按时间顺序标出超出每日上限的部分。"""
    excess: list[tuple[datetime, datetime]] = []
    accumulated = 0
    for start, end in merged:
        duration = elapsed_seconds(start, end)
        remaining = cap_seconds - accumulated
        if duration <= remaining:
            accumulated += duration
            continue
        if remaining > 0:
            cut = start + timedelta(seconds=remaining)
            excess.append((cut, end))
        else:
            excess.append((start, end))
        accumulated = cap_seconds
    return excess


def _matching_exceptions(
    exceptions: Iterable[ExceptionGrant],
    student_id: str,
    segment: _RawSegment,
    day_start_utc: datetime,
) -> list[str]:
    """返回覆盖该分段超限部分的生效中人工例外。"""
    matched: list[str] = []
    for exc in exceptions:
        if exc.status != EXCEPTION_STATUS_ACTIVE:
            continue
        if exc.student_id != student_id:
            continue
        if exc.region != segment.region:
            continue
        if exc.activity_id is not None and exc.activity_id != segment.activity_id:
            continue
        if exc.local_day is not None and exc.local_day != segment.local_day.isoformat():
            continue
        if not (to_utc(exc.valid_from) <= day_start_utc < to_utc(exc.valid_until)):
            continue
        matched.append(exc.exception_id)
    return sorted(matched)


def evaluate_student(
    student_id: str,
    activities: Iterable[ActivityInterval],
    evidence: Iterable[LocationPoint],
    rules: Iterable[RegionRuleVersion],
    exceptions: Iterable[ExceptionGrant],
    fallback_tz: str,
) -> StudentRegionalReport:
    """评估一名学员的地区每日工时合规情况。"""
    evidence_list = list(evidence)
    rules_list = list(rules)
    exceptions_list = list(exceptions)

    raw_segments: list[_RawSegment] = []
    for activity in activities:
        points = [
            p
            for p in evidence_list
            if p.activity_id == activity.activity_id
            and (p.student_id is None or p.student_id == student_id)
        ]
        for region, tz_name, seg_start, seg_end in segment_by_location(
            activity.start_utc, activity.end_utc, points, fallback_tz
        ):
            for day, day_start, day_end in split_by_academic_day(
                seg_start, seg_end, tz_name
            ):
                raw_segments.append(
                    _RawSegment(
                        event_id=activity.event_id,
                        activity_id=activity.activity_id,
                        region=region,
                        iana_timezone=tz_name,
                        local_day=day,
                        start_utc=day_start,
                        end_utc=day_end,
                    )
                )

    groups: dict[tuple[str, date], list[tuple[int, _RawSegment]]] = {}
    for idx, seg in enumerate(raw_segments):
        groups.setdefault((seg.region, seg.local_day), []).append((idx, seg))

    segment_reports: list[SegmentReport | None] = [None] * len(raw_segments)
    day_reports: list[DayReport] = []

    for (region, day), members in groups.items():
        members.sort(key=lambda pair: (pair[1].start_utc, pair[1].event_id))
        # 同一地区预期使用同一时区；若证据不一致，取当天最早分段的时区。
        tz_name = members[0][1].iana_timezone
        merged = merge_intervals([(s.start_utc, s.end_utc) for _, s in members])
        total_seconds = sum(elapsed_seconds(s, e) for s, e in merged)
        day_start_utc = local_midnight_as_utc(day, tz_name)
        rule = resolve_rule(rules_list, region, day_start_utc)
        cap_seconds = rule.daily_cap_seconds if rule is not None else None

        excess_intervals = (
            _excess_intervals(merged, cap_seconds) if cap_seconds is not None else []
        )
        day_excess = sum(elapsed_seconds(s, e) for s, e in excess_intervals)

        # 把超限区间归因到具体分段：重叠分段时归给开始最晚者，
        # 保证各分段超限之和恰好等于当日超限总量。
        segment_excess: dict[int, int] = {}
        for ex_start, ex_end in excess_intervals:
            candidates = [
                (idx, s)
                for idx, s in members
                if s.start_utc < ex_end and ex_start < s.end_utc
            ]
            if not candidates:
                continue
            chosen_idx, chosen = max(
                candidates, key=lambda pair: (pair[1].start_utc, pair[1].event_id)
            )
            overlap = elapsed_seconds(
                max(ex_start, chosen.start_utc), min(ex_end, chosen.end_utc)
            )
            segment_excess[chosen_idx] = segment_excess.get(chosen_idx, 0) + overlap

        day_waived = 0
        day_exception_ids: list[str] = []
        for idx, seg in members:
            excess = segment_excess.get(idx, 0)
            matched = (
                _matching_exceptions(exceptions_list, student_id, seg, day_start_utc)
                if excess > 0
                else []
            )
            waived = excess if matched else 0
            day_waived += waived
            for exc_id in matched:
                if exc_id not in day_exception_ids:
                    day_exception_ids.append(exc_id)
            segment_reports[idx] = SegmentReport(
                event_id=seg.event_id,
                activity_id=seg.activity_id,
                region=seg.region,
                iana_timezone=seg.iana_timezone,
                local_day=seg.local_day,
                start_utc=seg.start_utc,
                end_utc=seg.end_utc,
                seconds=seg.seconds,
                rule_id=rule.rule_id if rule is not None else None,
                rule_version=rule.version if rule is not None else None,
                cap_seconds=cap_seconds,
                allowed_seconds=seg.seconds - excess,
                excess_seconds=excess,
                waived_seconds=waived,
                exception_ids=matched,
            )

        day_reports.append(
            DayReport(
                region=region,
                iana_timezone=tz_name,
                local_day=day,
                total_seconds=total_seconds,
                cap_seconds=cap_seconds,
                rule_id=rule.rule_id if rule is not None else None,
                rule_version=rule.version if rule is not None else None,
                excess_seconds=day_excess,
                waived_seconds=day_waived,
                net_excess_seconds=day_excess - day_waived,
                exception_ids=day_exception_ids,
            )
        )

    evaluated_seconds = sum(
        elapsed_seconds(s, e)
        for s, e in merge_intervals([(seg.start_utc, seg.end_utc) for seg in raw_segments])
    )
    segments_sorted = sorted(
        (r for r in segment_reports if r is not None),
        key=lambda r: (r.start_utc, r.event_id),
    )
    days_sorted = sorted(day_reports, key=lambda d: (d.local_day, d.region))
    excess_total = sum(d.excess_seconds for d in days_sorted)
    waived_total = sum(d.waived_seconds for d in days_sorted)
    return StudentRegionalReport(
        student_id=student_id,
        evaluated_seconds=evaluated_seconds,
        excess_seconds=excess_total,
        waived_seconds=waived_total,
        net_excess_seconds=excess_total - waived_total,
        days=days_sorted,
        segments=segments_sorted,
    )


def evaluate_plan(
    activities_by_student: dict[str, list[ActivityInterval]],
    evidence: Iterable[LocationPoint],
    rules: Iterable[RegionRuleVersion],
    exceptions: Iterable[ExceptionGrant],
    fallback_tz: str,
) -> dict[str, StudentRegionalReport]:
    """对全体学员执行地区合规评估。"""
    evidence_list = list(evidence)
    rules_list = list(rules)
    exceptions_list = list(exceptions)
    reports: dict[str, StudentRegionalReport] = {}
    for student_id, activities in activities_by_student.items():
        valid = [
            a for a in activities if to_utc(a.end_utc) > to_utc(a.start_utc)
        ]
        if not valid:
            continue
        reports[student_id] = evaluate_student(
            student_id,
            valid,
            evidence_list,
            rules_list,
            exceptions_list,
            fallback_tz,
        )
    return reports


def build_findings(
    reports: dict[str, StudentRegionalReport],
) -> list[dict[str, Any]]:
    """汇总需要复核的超限发现（含已被豁免的）。"""
    findings: list[dict[str, Any]] = []
    for student_id in sorted(reports):
        report = reports[student_id]
        for day in report.days:
            if day.excess_seconds <= 0:
                continue
            findings.append(
                {
                    "student_id": student_id,
                    "region": day.region,
                    "iana_timezone": day.iana_timezone,
                    "local_day": day.local_day.isoformat(),
                    "total_seconds": day.total_seconds,
                    "cap_seconds": day.cap_seconds,
                    "rule_id": day.rule_id,
                    "rule_version": day.rule_version,
                    "excess_seconds": day.excess_seconds,
                    "waived_seconds": day.waived_seconds,
                    "net_excess_seconds": day.net_excess_seconds,
                    "status": day.status,
                    "exception_ids": list(day.exception_ids),
                }
            )
    return findings


def build_statistics(reports: dict[str, StudentRegionalReport]) -> dict[str, Any]:
    """按地区与整体聚合合规统计。"""
    region_acc: dict[str, dict[str, Any]] = {}
    total_evaluated = 0
    total_excess = 0
    total_waived = 0
    students_with_excess = 0
    open_findings = 0

    for report in reports.values():
        total_evaluated += report.evaluated_seconds
        total_excess += report.excess_seconds
        total_waived += report.waived_seconds
        if report.excess_seconds > 0:
            students_with_excess += 1
        open_findings += sum(1 for d in report.days if d.net_excess_seconds > 0)

        intervals_by_region: dict[str, list[tuple[datetime, datetime]]] = {}
        for seg in report.segments:
            intervals_by_region.setdefault(seg.region, []).append(
                (seg.start_utc, seg.end_utc)
            )
        for region, intervals in intervals_by_region.items():
            acc = region_acc.setdefault(
                region,
                {
                    "region": region,
                    "evaluated_seconds": 0,
                    "excess_seconds": 0,
                    "waived_seconds": 0,
                    "net_excess_seconds": 0,
                    "students_with_excess": 0,
                    "days_with_excess": 0,
                },
            )
            acc["evaluated_seconds"] += sum(
                elapsed_seconds(s, e) for s, e in merge_intervals(intervals)
            )
        for day in report.days:
            acc = region_acc[day.region]
            acc["excess_seconds"] += day.excess_seconds
            acc["waived_seconds"] += day.waived_seconds
            acc["net_excess_seconds"] += day.net_excess_seconds
            if day.excess_seconds > 0:
                acc["students_with_excess"] += 1
            if day.net_excess_seconds > 0:
                acc["days_with_excess"] += 1

    return {
        "totals": {
            "students": len(reports),
            "evaluated_seconds": total_evaluated,
            "excess_seconds": total_excess,
            "waived_seconds": total_waived,
            "net_excess_seconds": total_excess - total_waived,
            "students_with_excess": students_with_excess,
            "open_findings": open_findings,
        },
        "regions": [region_acc[r] for r in sorted(region_acc)],
    }
