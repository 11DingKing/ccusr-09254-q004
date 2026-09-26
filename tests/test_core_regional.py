"""地区合规核心逻辑的纯函数测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.regional import (
    FALLBACK_REGION,
    ActivityInterval,
    ExceptionGrant,
    LocationPoint,
    RegionRuleVersion,
    evaluate_student,
    resolve_rule,
    segment_by_location,
)

UTC = timezone.utc


def _dt(hour, minute=0, day=15, month=3, year=2024):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _rule(rule_id, region, version, cap, effective_from, effective_to=None,
          status="published"):
    return RegionRuleVersion(
        rule_id=rule_id,
        region=region,
        version=version,
        daily_cap_seconds=cap,
        effective_from=effective_from,
        effective_to=effective_to,
        status=status,
    )


def test_segment_without_evidence_uses_fallback():
    segments = segment_by_location(_dt(8), _dt(12), [], "Asia/Shanghai")
    assert segments == [(FALLBACK_REGION, "Asia/Shanghai", _dt(8), _dt(12))]


def test_evidence_before_start_sets_initial_location():
    points = [
        LocationPoint("L1", "A1", None, "cn-sh", "Asia/Shanghai", _dt(0)),
    ]
    segments = segment_by_location(_dt(8), _dt(10), points, "Asia/Shanghai")
    assert len(segments) == 1
    assert segments[0][0] == "cn-sh"


def test_evidence_after_start_splits_at_change_point():
    points = [
        LocationPoint("L1", "A1", None, "cn-sh", "Asia/Shanghai", _dt(0)),
        LocationPoint("L2", "A1", None, "de-be", "Europe/Berlin", _dt(9)),
    ]
    segments = segment_by_location(_dt(8), _dt(11), points, "Asia/Shanghai")
    assert [(s[0], s[2], s[3]) for s in segments] == [
        ("cn-sh", _dt(8), _dt(9)),
        ("de-be", _dt(9), _dt(11)),
    ]


def test_redundant_same_location_points_are_coalesced():
    points = [
        LocationPoint("L1", "A1", None, "cn-sh", "Asia/Shanghai", _dt(0)),
        LocationPoint("L2", "A1", None, "cn-sh", "Asia/Shanghai", _dt(9)),
    ]
    segments = segment_by_location(_dt(8), _dt(11), points, "Asia/Shanghai")
    assert len(segments) == 1
    assert segments[0][0] == "cn-sh"


def test_evidence_exactly_at_start_applies_immediately():
    points = [
        LocationPoint("L1", "A1", None, "de-be", "Europe/Berlin", _dt(8)),
    ]
    segments = segment_by_location(_dt(8), _dt(10), points, "Asia/Shanghai")
    assert len(segments) == 1
    assert segments[0][0] == "de-be"


def test_resolve_rule_picks_highest_covering_published_version():
    rules = [
        _rule("R1", "cn-sh", 1, 100, _dt(0, day=1)),
        _rule("R2", "cn-sh", 2, 200, _dt(0, day=1)),
        _rule("R3", "cn-sh", 3, 300, _dt(0, day=1), status="draft"),
        _rule("R4", "cn-sh", 4, 400, _dt(0, day=1), status="retired"),
        _rule("R5", "de-be", 5, 500, _dt(0, day=1)),
    ]
    resolved = resolve_rule(rules, "cn-sh", _dt(12))
    assert resolved is not None
    assert resolved.rule_id == "R2"  # 草稿与已退役版本不参与解析
    assert resolve_rule(rules, "de-be", _dt(12)).rule_id == "R5"
    assert resolve_rule(rules, "cn-sh", _dt(12, day=1, month=1, year=2020)) is None


def test_exception_requires_active_status_and_covering_window():
    # 活动 08:00-12:00 UTC，按上海时区属于 2024-03-15，
    # 该当地日的开始时刻是 2024-03-14T16:00:00Z。
    activity = ActivityInterval("E1", "A1", _dt(8), _dt(12))
    rules = [_rule("R1", "home", 1, 3600, _dt(0, day=1))]
    base = dict(
        student_id="S1",
        region="home",
        activity_id=None,
        local_day=None,
        valid_from=_dt(0, day=14),
        valid_until=_dt(23, 59),
    )
    active = ExceptionGrant(exception_id="X1", status="active", **base)
    revoked = ExceptionGrant(exception_id="X2", status="revoked", **base)
    expired = ExceptionGrant(
        exception_id="X3", status="active",
        **{**base, "valid_from": _dt(0, day=13), "valid_until": _dt(12, day=14)},
    )

    report = evaluate_student("S1", [activity], [], rules, [active], "Asia/Shanghai")
    assert report.days[0].waived_seconds == report.days[0].excess_seconds

    report = evaluate_student("S1", [activity], [], rules, [revoked], "Asia/Shanghai")
    assert report.days[0].waived_seconds == 0

    report = evaluate_student("S1", [activity], [], rules, [expired], "Asia/Shanghai")
    assert report.days[0].waived_seconds == 0


def test_evaluation_is_deterministic_regardless_of_input_order():
    activities = [
        ActivityInterval("E1", "A1", _dt(8), _dt(10)),
        ActivityInterval("E2", "A2", _dt(9, 30), _dt(11)),
    ]
    points = [
        LocationPoint("L1", "A1", None, "cn-sh", "Asia/Shanghai", _dt(0)),
        LocationPoint("L2", "A2", None, "cn-sh", "Asia/Shanghai", _dt(0)),
    ]
    rules = [_rule("R1", "cn-sh", 1, 3600, _dt(0, day=1))]
    first = evaluate_student("S1", activities, points, rules, [], "Asia/Shanghai")
    second = evaluate_student(
        "S1",
        list(reversed(activities)),
        list(reversed(points)),
        rules,
        [],
        "Asia/Shanghai",
    )
    assert first.to_dict() == second.to_dict()
