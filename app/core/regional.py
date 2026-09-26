"""跨地区每日工时上限合规引擎（纯函数，不依赖数据库）。

处理流水线：

1. 地点解析：静止活动直接归属地区；移动活动按轨迹变更点切分。
2. 当地日切分：每段按所在地区的 IANA 时区切到当地日历日（天然兼容夏令时）。
3. 规则切分：在地区规则版本的生效边界（UTC 绝对时刻）上再次切分并盖戳。
4. 例外切分：人工例外按有效期边界切分，按范围匹配并决定豁免/自定义上限。
5. 分桶评估：按（地区, 当地日, 规则版本）合桶，重叠活动取并集后与上限比较，
   超出上限的部分按“最晚开始的活动”确定性归因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from .clock import (
    elapsed_seconds,
    merge_intervals,
    split_by_academic_day,
    to_utc,
    union_seconds,
)
# 一旦发布过的规则版本都可能在历史窗口内被选中；草稿永不参与。
SELECTABLE_STATUSES = frozenset({"published", "retired", "restored"})


@dataclass(frozen=True)
class Region:
    region_code: str
    iana_timezone: str
    label: str = ""


@dataclass(frozen=True)
class RuleVersion:
    """地区每日工时上限的一个版本。生效窗口为半开区间 [from, to)。

    同一 rule_id 回滚后重新生效会产生新窗口，用 window_id 区分。
    """

    rule_id: str
    region_code: str
    daily_cap_seconds: int
    status: str
    effective_from_utc: datetime | None
    effective_to_utc: datetime | None
    window_id: str = ""

    def governs(self, moment_utc: datetime) -> bool:
        if self.status not in SELECTABLE_STATUSES:
            return False
        moment_utc = to_utc(moment_utc)
        if self.effective_from_utc is not None and moment_utc < self.effective_from_utc:
            return False
        if self.effective_to_utc is not None and moment_utc >= self.effective_to_utc:
            return False
        return True


@dataclass(frozen=True)
class TrackPoint:
    """移动活动轨迹点：``at_utc`` 为进入该地区的变更时刻。"""

    at_utc: datetime
    region_code: str | None
    evidence_id: str | None = None


@dataclass(frozen=True)
class Activity:
    event_id: str
    student_id: str
    activity_id: str
    start_utc: datetime
    end_utc: datetime
    region_code: str | None = None
    evidence_id: str | None = None
    track: tuple[TrackPoint, ...] = field(default_factory=tuple)
    parse_error: str | None = None
    status: str = "CONFIRMED"


@dataclass(frozen=True)
class ExceptionGrant:
    """人工例外：范围（学员必填，地区/活动可选）+ 有效期限（必填）。

    ``cap_override_seconds`` 为 None 表示完全豁免，否则把当天上限抬高到该值。
    """

    exception_id: str
    student_id: str
    region_code: str | None
    activity_id: str | None
    valid_from_utc: datetime
    valid_to_utc: datetime
    status: str
    cap_override_seconds: int | None = None

    def scope_matches(self, region_code: str, activity_id: str) -> bool:
        if self.region_code is not None and self.region_code != region_code:
            return False
        if self.activity_id is not None and self.activity_id != activity_id:
            return False
        return True

    def _specificity(self) -> tuple[int, int]:
        return (
            1 if self.region_code is not None else 0,
            1 if self.activity_id is not None else 0,
        )


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    student_id: str
    region_code: str
    observed_at_utc: datetime
    source: str


@dataclass(frozen=True)
class _Resolved:
    start_utc: datetime
    end_utc: datetime
    region_code: str | None
    evidence_id: str | None
    error: str | None = None


@dataclass
class _Piece:
    start_utc: datetime
    end_utc: datetime
    region_code: str
    local_day: str
    rule_id: str | None
    window_id: str
    event_id: str
    activity_id: str
    evidence_id: str | None
    evidence_status: str
    exception_id: str | None = None
    exempt: bool = False


def _iso_z(value: datetime) -> str:
    return to_utc(value).isoformat().replace("+00:00", "Z")


def _add_seconds(moment: datetime, seconds: int) -> datetime:
    return moment + timedelta(seconds=seconds)


def _split_at_boundaries(
    start: datetime, end: datetime, boundaries: Sequence[datetime]
) -> list[tuple[datetime, datetime]]:
    cuts = sorted({to_utc(b) for b in boundaries if start < to_utc(b) < end})
    out: list[tuple[datetime, datetime]] = []
    cursor = start
    for cut in cuts:
        out.append((cursor, cut))
        cursor = cut
    out.append((cursor, end))
    return out


def select_rule(
    rules: Sequence[RuleVersion], moment_utc: datetime
) -> RuleVersion | None:
    """选择某时刻生效的规则版本；正常情况下唯一（窗口不重叠不变量）。"""
    moment_utc = to_utc(moment_utc)
    candidates = [r for r in rules if r.governs(moment_utc)]
    if not candidates:
        return None
    earliest = datetime.min.replace(tzinfo=moment_utc.tzinfo)
    # 防御性：多个候选时取生效起点最晚者。
    return max(
        candidates,
        key=lambda r: (r.effective_from_utc or earliest, r.rule_id),
    )


def resolve_activity(activity: Activity) -> list[_Resolved]:
    """把一条活动解析为带地区戳的区间；移动活动在位置变更点分段。"""
    start, end = to_utc(activity.start_utc), to_utc(activity.end_utc)
    if activity.parse_error:
        return [_Resolved(start, end, None, None, activity.parse_error)]
    if end <= start:
        return [_Resolved(start, end, None, None, "empty_activity")]

    if activity.track:
        if activity.region_code:
            return [
                _Resolved(start, end, None, None, "ambiguous_location_track_and_static")
            ]
        points = sorted(activity.track, key=lambda p: to_utc(p.at_utc))
        if any(
            to_utc(points[i].at_utc) == to_utc(points[i - 1].at_utc)
            for i in range(1, len(points))
        ):
            return [_Resolved(start, end, None, None, "duplicate_track_timestamp")]

        resolved: list[_Resolved] = []
        # 首个轨迹点晚于活动开始 -> 之前的部分无地区证据。
        first_at = to_utc(points[0].at_utc)
        if first_at > start:
            resolved.append(
                _Resolved(start, min(end, first_at), None, None, "missing_lead_region")
            )
        for idx, point in enumerate(points):
            seg_start = max(start, to_utc(point.at_utc))
            if seg_start >= end:
                break
            seg_end = (
                end
                if idx + 1 == len(points)
                else min(end, to_utc(points[idx + 1].at_utc))
            )
            if seg_end > seg_start:
                resolved.append(
                    _Resolved(seg_start, seg_end, point.region_code, point.evidence_id)
                )
        return resolved

    if not activity.region_code:
        return [_Resolved(start, end, None, None, "missing_location")]
    return [_Resolved(start, end, activity.region_code, activity.evidence_id)]


def _evidence_status(
    evidence_id: str | None,
    activity: Activity,
    segment: _Resolved,
    evidence: dict[str, EvidenceRecord],
) -> str:
    if not evidence_id:
        return "none"
    record = evidence.get(evidence_id)
    if record is None:
        return "invalid"
    if record.student_id != activity.student_id:
        return "invalid"
    if segment.region_code is not None and record.region_code != segment.region_code:
        return "invalid"
    observed = to_utc(record.observed_at_utc)
    if observed < to_utc(activity.start_utc) or observed > to_utc(activity.end_utc):
        return "invalid"
    return "verified"


def _pick_grant(
    grants: Sequence[ExceptionGrant],
    *,
    region_code: str,
    activity_id: str,
    moment_utc: datetime,
) -> ExceptionGrant | None:
    moment_utc = to_utc(moment_utc)
    matching = [
        g
        for g in grants
        if g.status == "approved"
        and g.scope_matches(region_code, activity_id)
        and to_utc(g.valid_from_utc) <= moment_utc < to_utc(g.valid_to_utc)
    ]
    if not matching:
        return None
    return max(
        matching,
        key=lambda g: (*g._specificity(), to_utc(g.valid_from_utc), g.exception_id),
    )


def _rule_boundaries(rules: Sequence[RuleVersion]) -> set[datetime]:
    out: set[datetime] = set()
    for rule in rules:
        if rule.status not in SELECTABLE_STATUSES:
            continue
        if rule.effective_from_utc is not None:
            out.add(to_utc(rule.effective_from_utc))
        if rule.effective_to_utc is not None:
            out.add(to_utc(rule.effective_to_utc))
    return out


def _build_pieces(
    activity: Activity,
    regions: dict[str, Region],
    rules_by_region: dict[str, list[RuleVersion]],
    grants: Sequence[ExceptionGrant],
    evidence: dict[str, EvidenceRecord],
    unresolved: list[dict[str, Any]],
) -> list[_Piece]:
    pieces: list[_Piece] = []
    for segment in resolve_activity(activity):
        if segment.error or not segment.region_code:
            unresolved.append(
                {
                    "event_id": activity.event_id,
                    "activity_id": activity.activity_id,
                    "start_utc": _iso_z(segment.start_utc),
                    "end_utc": _iso_z(segment.end_utc),
                    "seconds": elapsed_seconds(segment.start_utc, segment.end_utc),
                    "region_code": segment.region_code,
                    "reason": segment.error or "missing_location",
                }
            )
            continue
        region = regions.get(segment.region_code)
        if region is None:
            unresolved.append(
                {
                    "event_id": activity.event_id,
                    "activity_id": activity.activity_id,
                    "start_utc": _iso_z(segment.start_utc),
                    "end_utc": _iso_z(segment.end_utc),
                    "seconds": elapsed_seconds(segment.start_utc, segment.end_utc),
                    "region_code": segment.region_code,
                    "reason": "unknown_region",
                }
            )
            continue

        region_rules = rules_by_region.get(segment.region_code, [])
        boundaries = _rule_boundaries(region_rules)
        student_grants = [g for g in grants if g.student_id == activity.student_id]
        grant_boundaries = {
            to_utc(g.valid_from_utc) for g in student_grants
        } | {to_utc(g.valid_to_utc) for g in student_grants}
        ev_status = _evidence_status(segment.evidence_id, activity, segment, evidence)

        for day, day_start, day_end in split_by_academic_day(
            segment.start_utc, segment.end_utc, region.iana_timezone
        ):
            for rule_start, rule_end in _split_at_boundaries(
                day_start, day_end, boundaries
            ):
                rule = select_rule(region_rules, rule_start)
                for exc_start, exc_end in _split_at_boundaries(
                    rule_start, rule_end, grant_boundaries
                ):
                    grant = _pick_grant(
                        student_grants,
                        region_code=segment.region_code,
                        activity_id=activity.activity_id,
                        moment_utc=exc_start,
                    )
                    pieces.append(
                        _Piece(
                            start_utc=exc_start,
                            end_utc=exc_end,
                            region_code=segment.region_code,
                            local_day=day.isoformat(),
                            rule_id=rule.rule_id if rule is not None else None,
                            window_id=rule.window_id if rule is not None else "",
                            event_id=activity.event_id,
                            activity_id=activity.activity_id,
                            evidence_id=segment.evidence_id,
                            evidence_status=ev_status,
                            exception_id=grant.exception_id if grant else None,
                            exempt=grant is not None
                            and grant.cap_override_seconds is None,
                        )
                    )
    return pieces


def _make_slice(
    start: datetime,
    end: datetime,
    status_value: str,
    piece: _Piece,
) -> dict[str, Any]:
    return {
        "start_utc": _iso_z(start),
        "end_utc": _iso_z(end),
        "seconds": elapsed_seconds(start, end),
        "status": status_value,
        "event_id": piece.event_id,
        "activity_id": piece.activity_id,
        "exception_id": piece.exception_id,
        "evidence_id": piece.evidence_id,
        "evidence_status": piece.evidence_status,
    }


def _evaluate_bucket(
    key: tuple[str, str, str, str],
    pieces: list[_Piece],
    regions: dict[str, Region],
    rules_by_window: dict[str, RuleVersion],
    grants_by_id: dict[str, ExceptionGrant],
) -> dict[str, Any]:
    region_code, local_day, rule_id, window_id = key
    region = regions[region_code]
    rule = rules_by_window.get(window_id) if window_id else None
    base_cap = rule.daily_cap_seconds if rule is not None else None

    exempt_pieces = [p for p in pieces if p.exempt]
    taxable_pieces = [p for p in pieces if not p.exempt]

    # 当天命中的自定义上限例外抬高整日上限（只升不降）。
    override_caps = [
        grants_by_id[p.exception_id].cap_override_seconds
        for p in taxable_pieces
        if p.exception_id
        and grants_by_id.get(p.exception_id) is not None
        and grants_by_id[p.exception_id].cap_override_seconds is not None
    ]
    effective_cap: int | None = base_cap
    if override_caps:
        effective_cap = (
            max(base_cap, max(override_caps))
            if base_cap is not None
            else max(override_caps)
        )

    applied_exceptions = {
        p.exception_id for p in pieces if p.exception_id
    }

    slices: list[dict[str, Any]] = []
    if exempt_pieces:
        for ex_start, ex_end in merge_intervals(
            [(p.start_utc, p.end_utc) for p in exempt_pieces]
        ):
            cuts = sorted(
                {
                    b
                    for p in exempt_pieces
                    for b in (p.start_utc, p.end_utc)
                    if ex_start < b < ex_end
                }
            )
            for span_start, span_end in zip(
                [ex_start, *cuts], [*cuts, ex_end]
            ):
                covering = [
                    p
                    for p in exempt_pieces
                    if p.start_utc <= span_start and span_end <= p.end_utc
                ]
                owner = max(
                    covering, key=lambda p: (p.start_utc, p.event_id)
                )
                slices.append(
                    _make_slice(span_start, span_end, "exempt", owner)
                )

    exempt_seconds = union_seconds(
        [(p.start_utc, p.end_utc) for p in exempt_pieces]
    )
    covered_seconds = 0
    over_seconds = 0

    if taxable_pieces:
        merged = merge_intervals([(p.start_utc, p.end_utc) for p in taxable_pieces])
        consumed = 0
        for merged_start, merged_end in merged:
            cuts = sorted(
                {
                    b
                    for p in taxable_pieces
                    for b in (p.start_utc, p.end_utc)
                    if merged_start < b < merged_end
                }
            )
            spans = list(zip([merged_start, *cuts], [*cuts, merged_end]))
            for span_start, span_end in spans:
                contributors = [
                    p
                    for p in taxable_pieces
                    if p.start_utc <= span_start and span_end <= p.end_utc
                ]
                # 归因到最晚开始的活动（并列时 event_id 较大者）。
                attributed = max(
                    contributors, key=lambda p: (p.start_utc, p.event_id)
                )
                seconds = elapsed_seconds(span_start, span_end)
                covered_seconds += seconds

                if effective_cap is None:
                    slices.append(
                        _make_slice(span_start, span_end, "unregulated", attributed)
                    )
                    continue

                if consumed >= effective_cap:
                    within_part, over_part = 0, seconds
                elif consumed + seconds <= effective_cap:
                    within_part, over_part = seconds, 0
                else:
                    within_part = effective_cap - consumed
                    over_part = seconds - within_part
                consumed += seconds

                if within_part:
                    slices.append(
                        _make_slice(
                            span_start,
                            _add_seconds(span_start, within_part),
                            "within_cap",
                            attributed,
                        )
                    )
                if over_part:
                    over_seconds += over_part
                    slices.append(
                        _make_slice(
                            _add_seconds(span_end, -over_part),
                            span_end,
                            "over_limit",
                            attributed,
                        )
                    )

    if over_seconds:
        bucket_status = "over_limit"
    elif not rule_id:
        bucket_status = "no_rule"
    else:
        bucket_status = "within_cap"

    slices.sort(key=lambda s: (s["start_utc"], s["event_id"]))
    return {
        "region_code": region_code,
        "timezone": region.iana_timezone,
        "local_day": local_day,
        "rule_id": rule_id or None,
        "window_id": window_id or None,
        "rule_status": rule.status if rule is not None else None,
        "cap_seconds": effective_cap,
        "base_cap_seconds": base_cap,
        "covered_seconds": covered_seconds,
        "exempt_seconds": exempt_seconds,
        "over_seconds": over_seconds,
        "status": bucket_status,
        "exceptions_applied": sorted(e for e in applied_exceptions if e),
        "slices": slices,
    }


def evaluate_student(
    student_id: str,
    activities: Sequence[Activity],
    *,
    regions: Sequence[Region],
    rules: Sequence[RuleVersion],
    exceptions: Sequence[ExceptionGrant],
    evidence: Sequence[EvidenceRecord],
) -> dict[str, Any]:
    """评估单个学员的跨地区每日工时合规情况。"""
    region_map = {r.region_code: r for r in regions}
    rules_by_region: dict[str, list[RuleVersion]] = {}
    for rule in rules:
        rules_by_region.setdefault(rule.region_code, []).append(rule)
    rules_by_window = {r.window_id: r for r in rules}
    grants = [g for g in exceptions if g.student_id == student_id]
    grants_by_id = {g.exception_id: g for g in grants}
    evidence_map = {e.evidence_id: e for e in evidence}

    all_pieces: list[_Piece] = []
    unresolved: list[dict[str, Any]] = []
    for activity in activities:
        all_pieces.extend(
            _build_pieces(
                activity,
                region_map,
                rules_by_region,
                grants,
                evidence_map,
                unresolved,
            )
        )

    def _key(piece: _Piece) -> tuple[str, str, str, str]:
        return (piece.region_code, piece.local_day, piece.rule_id or "", piece.window_id)

    grouped: dict[tuple[str, str, str, str], list[_Piece]] = {}
    for piece in all_pieces:
        grouped.setdefault(_key(piece), []).append(piece)

    buckets = [
        _evaluate_bucket(
            key, grouped_pieces, region_map, rules_by_window, grants_by_id
        )
        for key, grouped_pieces in sorted(grouped.items())
    ]

    covered = sum(b["covered_seconds"] for b in buckets)
    over = sum(b["over_seconds"] for b in buckets)
    exempt_seconds = sum(b["exempt_seconds"] for b in buckets)
    unresolved_seconds = sum(u["seconds"] for u in unresolved)
    violation_days = sum(1 for b in buckets if b["status"] == "over_limit")

    return {
        "student_id": student_id,
        "totals": {
            "covered_seconds": covered,
            "over_seconds": over,
            "exempt_seconds": exempt_seconds,
            "unresolved_seconds": unresolved_seconds,
            "violation_days": violation_days,
            "compliant": over == 0 and unresolved_seconds == 0,
            "has_violation": over > 0,
        },
        "buckets": buckets,
        "unresolved": unresolved,
    }


def explain_segmentation(activity: Activity, regions: Sequence[Region]) -> dict[str, Any]:
    """供预览/调试：解释一条活动如何按位置变更点分段。"""
    region_map = {r.region_code: r for r in regions}
    out_segments: list[dict[str, Any]] = []
    for segment in resolve_activity(activity):
        timezone_name = (
            region_map[segment.region_code].iana_timezone
            if segment.region_code in region_map
            else None
        )
        out_segments.append(
            {
                "start_utc": _iso_z(segment.start_utc),
                "end_utc": _iso_z(segment.end_utc),
                "seconds": elapsed_seconds(segment.start_utc, segment.end_utc),
                "region_code": segment.region_code,
                "timezone": timezone_name,
                "evidence_id": segment.evidence_id,
                "error": segment.error,
            }
        )
    return {
        "event_id": activity.event_id,
        "activity_id": activity.activity_id,
        "segments": out_segments,
    }
