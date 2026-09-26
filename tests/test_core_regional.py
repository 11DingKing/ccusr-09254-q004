"""跨地区每日工时上限引擎的单元测试。

覆盖：夏令时春进/秋退、跨境移动分段、重叠活动合并与归因、
规则版本日中切换与回滚窗口、人工例外范围与期限、地点证据校验。
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.core.regional import (
    Activity,
    EvidenceRecord,
    ExceptionGrant,
    Region,
    RuleVersion,
    TrackPoint,
    evaluate_student,
    explain_segmentation,
    resolve_activity,
)

UTC = timezone.utc
NY_TZ = ZoneInfo("America/New_York")
SH_TZ = ZoneInfo("Asia/Shanghai")

NY = Region("US-NY", "America/New_York", "纽约")
SH = Region("CN-SH", "Asia/Shanghai", label="上海")


def rule(rule_id, region, cap, frm, to=None, status="published", wid=None):
    return RuleVersion(
        rule_id=rule_id,
        region_code=region,
        daily_cap_seconds=cap,
        status=status,
        effective_from_utc=frm,
        effective_to_utc=to,
        window_id=wid or f"{rule_id}#w1",
    )


R_NY_4 = rule("R-NY-4", "US-NY", 4 * 3600, datetime(2024, 1, 1, tzinfo=UTC))
R_NY_6 = rule("R-NY-6", "US-NY", 6 * 3600, datetime(2024, 1, 1, tzinfo=UTC))
R_SH_3 = rule("R-SH-3", "CN-SH", 3 * 3600, datetime(2024, 1, 1, tzinfo=UTC))


# ---------------------------------------------------------------------------
# 夏令时
# ---------------------------------------------------------------------------

def test_dst_fall_back_real_hours_counted_once():
    # 秋退夜：2024-11-03 00:30 -> 03:30 纽约本地时间 = 真实 4 小时（含重复的 01 点）。
    activity = Activity(
        "E1", "S1", "A1",
        datetime(2024, 11, 3, 0, 30, tzinfo=NY_TZ),
        datetime(2024, 11, 3, 3, 30, tzinfo=NY_TZ),
        region_code="US-NY",
    )
    result = evaluate_student("S1", [activity], regions=[NY], rules=[R_NY_4],
                              exceptions=[], evidence=[])
    assert len(result["buckets"]) == 1
    bucket = result["buckets"][0]
    assert bucket["local_day"] == "2024-11-03"
    assert bucket["covered_seconds"] == 4 * 3600
    assert bucket["over_seconds"] == 0
    assert bucket["status"] == "within_cap"


def test_dst_fall_back_over_cap_is_real_hours():
    # 同一个秋退夜，上限 3 小时：真实 4 小时中超出 1 小时，而不是按本地钟面误判。
    cap3 = rule("R3", "US-NY", 3 * 3600, datetime(2024, 1, 1, tzinfo=UTC))
    activity = Activity(
        "E1", "S1", "A1",
        datetime(2024, 11, 3, 0, 30, tzinfo=NY_TZ),
        datetime(2024, 11, 3, 3, 30, tzinfo=NY_TZ),
        region_code="US-NY",
    )
    result = evaluate_student("S1", [activity], regions=[NY], rules=[cap3],
                              exceptions=[], evidence=[])
    bucket = result["buckets"][0]
    assert bucket["covered_seconds"] == 4 * 3600
    assert bucket["over_seconds"] == 3600


def test_dst_spring_forward_short_day():
    # 春进夜：2024-03-10 00:00 -> 04:00 纽约本地只流逝 3 个真实小时。
    activity = Activity(
        "E1", "S1", "A1",
        datetime(2024, 3, 10, 0, 0, tzinfo=NY_TZ),
        datetime(2024, 3, 10, 4, 0, tzinfo=NY_TZ),
        region_code="US-NY",
    )
    result = evaluate_student("S1", [activity], regions=[NY], rules=[R_NY_4],
                              exceptions=[], evidence=[])
    bucket = result["buckets"][0]
    assert bucket["covered_seconds"] == 3 * 3600  # 本地 4 小时钟面 - 消失的 1 小时


# ---------------------------------------------------------------------------
# 跨境移动
# ---------------------------------------------------------------------------

def test_mobile_activity_segments_at_region_change_points():
    # 12:00-20:00 UTC：12:00 在纽约（08:00 EDT），16:00 切到上海（次日 00:00 CST）。
    track = (
        TrackPoint(datetime(2024, 7, 1, 12, tzinfo=UTC), "US-NY"),
        TrackPoint(datetime(2024, 7, 1, 16, tzinfo=UTC), "CN-SH"),
    )
    activity = Activity(
        "E9", "S1", "TRIP",
        datetime(2024, 7, 1, 12, tzinfo=UTC),
        datetime(2024, 7, 1, 20, tzinfo=UTC),
        track=track,
    )
    result = evaluate_student(
        "S1", [activity], regions=[NY, SH], rules=[R_NY_6, R_SH_3],
        exceptions=[], evidence=[]
    )
    by_region = {b["region_code"]: b for b in result["buckets"]}
    assert by_region["US-NY"]["local_day"] == "2024-07-01"
    assert by_region["US-NY"]["covered_seconds"] == 4 * 3600
    assert by_region["CN-SH"]["local_day"] == "2024-07-02"
    assert by_region["CN-SH"]["covered_seconds"] == 4 * 3600
    assert by_region["CN-SH"]["over_seconds"] == 3600  # 上海上限 3h

    seg = explain_segmentation(activity, [NY, SH])
    assert [s["region_code"] for s in seg["segments"]] == ["US-NY", "CN-SH"]
    assert seg["segments"][0]["timezone"] == "America/New_York"


def test_mobile_track_starting_after_activity_beginning_is_unresolved_lead():
    track = (
        TrackPoint(datetime(2024, 7, 1, 13, tzinfo=UTC), "US-NY"),
    )
    activity = Activity(
        "E9", "S1", "TRIP",
        datetime(2024, 7, 1, 12, tzinfo=UTC),
        datetime(2024, 7, 1, 18, tzinfo=UTC),
        track=track,
    )
    result = evaluate_student("S1", [activity], regions=[NY], rules=[R_NY_6],
                              exceptions=[], evidence=[])
    reasons = {(u["reason"], u["seconds"]) for u in result["unresolved"]}
    assert ("missing_lead_region", 3600) in reasons
    # 13:00 之后的 5 小时正常归桶。
    assert result["buckets"][0]["covered_seconds"] == 5 * 3600


def test_static_location_and_track_conflict_is_parse_error():
    activity = Activity(
        "E1", "S1", "A1",
        datetime(2024, 7, 1, 12, tzinfo=UTC),
        datetime(2024, 7, 1, 14, tzinfo=UTC),
        region_code="US-NY",
        track=(TrackPoint(datetime(2024, 7, 1, 12, tzinfo=UTC), "US-NY"),),
    )
    resolved = resolve_activity(activity)
    assert resolved[0].error == "ambiguous_location_track_and_static"


def test_unknown_region_and_missing_location_are_unresolved():
    a1 = Activity("E1", "S1", "A1",
                  datetime(2024, 7, 1, 12, tzinfo=UTC),
                  datetime(2024, 7, 1, 13, tzinfo=UTC),
                  region_code="XX")
    a2 = Activity("E2", "S1", "A2",
                  datetime(2024, 7, 2, 12, tzinfo=UTC),
                  datetime(2024, 7, 2, 13, tzinfo=UTC))
    result = evaluate_student("S1", [a1, a2], regions=[NY], rules=[R_NY_6],
                              exceptions=[], evidence=[])
    reasons = {u["reason"] for u in result["unresolved"]}
    assert reasons == {"unknown_region", "missing_location"}
    assert result["totals"]["compliant"] is False
    assert result["totals"]["unresolved_seconds"] == 2 * 3600


# ---------------------------------------------------------------------------
# 重叠活动
# ---------------------------------------------------------------------------

def test_overlapping_activities_union_then_over_attributed_to_latest():
    # 12:00-15:00（3h）与 14:00-19:00（5h）重叠 1h -> 并集 7h，上限 6h，超 1h。
    a1 = Activity("E1", "S1", "A1",
                  datetime(2024, 7, 1, 12, tzinfo=UTC),
                  datetime(2024, 7, 1, 15, tzinfo=UTC),
                  region_code="US-NY")
    a2 = Activity("E2", "S1", "A2",
                  datetime(2024, 7, 1, 14, tzinfo=UTC),
                  datetime(2024, 7, 1, 19, tzinfo=UTC),
                  region_code="US-NY")
    result = evaluate_student("S1", [a1, a2], regions=[NY], rules=[R_NY_6],
                              exceptions=[], evidence=[])
    bucket = result["buckets"][0]
    assert bucket["covered_seconds"] == 7 * 3600
    assert bucket["over_seconds"] == 3600
    over = [s for s in bucket["slices"] if s["status"] == "over_limit"]
    assert sum(s["seconds"] for s in over) == 3600
    # 最晚开始的 E2 承担超限。
    assert {s["event_id"] for s in over} == {"E2"}


def test_exactly_at_cap_is_compliant_and_slices_contiguous():
    a1 = Activity("E1", "S1", "A1",
                  datetime(2024, 7, 1, 12, tzinfo=UTC),
                  datetime(2024, 7, 1, 15, tzinfo=UTC),
                  region_code="US-NY")
    a2 = Activity("E2", "S1", "A2",
                  datetime(2024, 7, 1, 15, tzinfo=UTC),
                  datetime(2024, 7, 1, 18, tzinfo=UTC),
                  region_code="US-NY")
    result = evaluate_student("S1", [a1, a2], regions=[NY], rules=[R_NY_6],
                              exceptions=[], evidence=[])
    bucket = result["buckets"][0]
    assert bucket["covered_seconds"] == 6 * 3600
    assert bucket["over_seconds"] == 0
    assert all(s["status"] != "over_limit" for s in bucket["slices"])


def test_separate_regions_have_independent_caps():
    a1 = Activity("E1", "S1", "A1",
                  datetime(2024, 7, 1, 12, tzinfo=UTC),
                  datetime(2024, 7, 1, 18, tzinfo=UTC),
                  region_code="US-NY")  # 6h 纽约
    track = (TrackPoint(datetime(2024, 7, 1, 20, tzinfo=UTC), "CN-SH"),)
    a2 = Activity("E2", "S1", "A2",
                  datetime(2024, 7, 1, 20, tzinfo=UTC),
                  datetime(2024, 7, 1, 23, tzinfo=UTC),
                  track=track)  # 3h 上海（恰满）
    result = evaluate_student("S1", [a1, a2], regions=[NY, SH],
                              rules=[R_NY_6, R_SH_3],
                              exceptions=[], evidence=[])
    assert all(b["over_seconds"] == 0 for b in result["buckets"])


# ---------------------------------------------------------------------------
# 规则版本切换与回滚
# ---------------------------------------------------------------------------

def test_activity_spanning_rule_change_at_noon_uses_both_rules():
    rules = [
        rule("OLD", "US-NY", 3 * 3600,
             datetime(2024, 7, 1, tzinfo=UTC),
             datetime(2024, 7, 1, 15, tzinfo=UTC), status="retired", wid="OLD#w1"),
        rule("NEW", "US-NY", 10 * 3600,
             datetime(2024, 7, 1, 15, tzinfo=UTC), wid="NEW#w1"),
    ]
    activity = Activity("E1", "S1", "A1",
                        datetime(2024, 7, 1, 12, tzinfo=UTC),
                        datetime(2024, 7, 1, 18, tzinfo=UTC),
                        region_code="US-NY")
    result = evaluate_student("S1", [activity], regions=[NY], rules=rules,
                              exceptions=[], evidence=[])
    by_rule = {b["rule_id"]: b for b in result["buckets"]}
    assert set(by_rule) == {"OLD", "NEW"}
    assert by_rule["OLD"]["covered_seconds"] == 3 * 3600
    assert by_rule["NEW"]["covered_seconds"] == 3 * 3600
    assert by_rule["OLD"]["status"] == "within_cap"  # 恰好 3h


def test_rule_rollback_opens_new_window_for_same_rule_id():
    # R1(4h) 1-6 月生效 -> R2(6h) 6-9 月 -> R1 回滚 9 月起以新窗口重新生效。
    rules = [
        rule("R1", "US-NY", 4 * 3600,
             datetime(2024, 1, 1, tzinfo=UTC),
             datetime(2024, 6, 1, tzinfo=UTC), status="retired", wid="R1#w1"),
        rule("R2", "US-NY", 6 * 3600,
             datetime(2024, 6, 1, tzinfo=UTC),
             datetime(2024, 9, 1, tzinfo=UTC), status="retired", wid="R2#w1"),
        rule("R1", "US-NY", 4 * 3600,
             datetime(2024, 9, 1, tzinfo=UTC),
             status="restored", wid="R1#w2"),
    ]

    def evaluate(at_day):
        activity = Activity("E1", "S1", "A1",
                            datetime(2024, at_day[0], at_day[1], 12, tzinfo=UTC),
                            datetime(2024, at_day[0], at_day[1], 18, tzinfo=UTC),
                            region_code="US-NY")
        return evaluate_student("S1", [activity], regions=[NY], rules=rules,
                                exceptions=[], evidence=[])

    # 7 月：R2 上限 6h，6h 恰好合规。
    july = evaluate((7, 2))["buckets"][0]
    assert (july["rule_id"], july["window_id"], july["over_seconds"]) == (
        "R2", "R2#w1", 0
    )
    # 9 月：回滚后的 R1 新窗口，上限 4h，6h 超出 2h。
    sept = evaluate((9, 2))["buckets"][0]
    assert (sept["rule_id"], sept["window_id"], sept["cap_seconds"],
            sept["over_seconds"]) == ("R1", "R1#w2", 4 * 3600, 2 * 3600)
    assert sept["rule_status"] == "restored"


def test_draft_rule_never_governs():
    draft = rule("DRAFT", "US-NY", 1 * 3600,
                 datetime(2024, 1, 1, tzinfo=UTC), status="draft")
    activity = Activity("E1", "S1", "A1",
                        datetime(2024, 7, 1, 12, tzinfo=UTC),
                        datetime(2024, 7, 1, 13, tzinfo=UTC),
                        region_code="US-NY")
    result = evaluate_student("S1", [activity], regions=[NY], rules=[draft],
                              exceptions=[], evidence=[])
    bucket = result["buckets"][0]
    assert bucket["rule_id"] is None
    assert bucket["status"] == "no_rule"
    assert bucket["over_seconds"] == 0


# ---------------------------------------------------------------------------
# 人工例外：范围 + 期限
# ---------------------------------------------------------------------------

def _overlapping_pair():
    return [
        Activity("E1", "S1", "A1",
                 datetime(2024, 7, 1, 12, tzinfo=UTC),
                 datetime(2024, 7, 1, 15, tzinfo=UTC),
                 region_code="US-NY"),
        Activity("E2", "S1", "A2",
                 datetime(2024, 7, 1, 14, tzinfo=UTC),
                 datetime(2024, 7, 1, 19, tzinfo=UTC),
                 region_code="US-NY"),
    ]


def test_full_exemption_requires_scope_and_valid_period():
    grant = ExceptionGrant(
        "X1", "S1", "US-NY", None,
        datetime(2024, 7, 1, tzinfo=UTC), datetime(2024, 7, 2, tzinfo=UTC),
        "approved",
    )
    result = evaluate_student("S1", _overlapping_pair(), regions=[NY],
                              rules=[R_NY_6], exceptions=[grant], evidence=[])
    bucket = result["buckets"][0]
    assert bucket["exempt_seconds"] == 7 * 3600
    assert bucket["covered_seconds"] == 0
    assert bucket["over_seconds"] == 0
    assert "X1" in bucket["exceptions_applied"]
    assert all(s["status"] == "exempt" for s in bucket["slices"])


def test_exception_outside_period_does_not_apply():
    grant = ExceptionGrant(
        "X1", "S1", None, None,
        datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 6, 2, tzinfo=UTC),
        "approved",
    )
    result = evaluate_student("S1", _overlapping_pair(), regions=[NY],
                              rules=[R_NY_6], exceptions=[grant], evidence=[])
    assert result["buckets"][0]["over_seconds"] == 3600


def test_exception_wrong_region_scope_does_not_apply():
    grant = ExceptionGrant(
        "X1", "S1", "CN-SH", None,
        datetime(2024, 7, 1, tzinfo=UTC), datetime(2024, 7, 2, tzinfo=UTC),
        "approved",
    )
    result = evaluate_student("S1", _overlapping_pair(), regions=[NY, SH],
                              rules=[R_NY_6], exceptions=[grant], evidence=[])
    assert result["buckets"][0]["exceptions_applied"] == []
    assert result["buckets"][0]["over_seconds"] == 3600


def test_exception_activity_scope_and_cap_override():
    # 仅 A2 抬高上限到 8h；A1 不受影响，但桶按整日取最大 override -> 整日 8h。
    grant = ExceptionGrant(
        "X1", "S1", "US-NY", "A2",
        datetime(2024, 7, 1, tzinfo=UTC), datetime(2024, 7, 2, tzinfo=UTC),
        "approved", cap_override_seconds=8 * 3600,
    )
    result = evaluate_student("S1", _overlapping_pair(), regions=[NY],
                              rules=[R_NY_6], exceptions=[grant], evidence=[])
    bucket = result["buckets"][0]
    assert bucket["cap_seconds"] == 8 * 3600
    assert bucket["over_seconds"] == 0
    # 例外切片只盖在 A2 上。
    tagged = {s["activity_id"] for s in bucket["slices"]
              if s["exception_id"] == "X1"}
    assert tagged == {"A2"}


def test_requested_exception_has_no_effect_until_approved():
    grant = ExceptionGrant(
        "X1", "S1", None, None,
        datetime(2024, 7, 1, tzinfo=UTC), datetime(2024, 7, 2, tzinfo=UTC),
        "requested",
    )
    result = evaluate_student("S1", _overlapping_pair(), regions=[NY],
                              rules=[R_NY_6], exceptions=[grant], evidence=[])
    assert result["buckets"][0]["over_seconds"] == 3600


def test_exception_does_not_leak_to_other_student():
    grant = ExceptionGrant(
        "X1", "OTHER", None, None,
        datetime(2024, 7, 1, tzinfo=UTC), datetime(2024, 7, 2, tzinfo=UTC),
        "approved",
    )
    result = evaluate_student("S1", _overlapping_pair(), regions=[NY],
                              rules=[R_NY_6], exceptions=[grant], evidence=[])
    assert result["buckets"][0]["over_seconds"] == 3600


# ---------------------------------------------------------------------------
# 地点证据
# ---------------------------------------------------------------------------

def test_evidence_verified_when_region_and_time_match():
    evidence = [EvidenceRecord(
        "V1", "S1", "US-NY", datetime(2024, 7, 1, 13, tzinfo=UTC), "gps"
    )]
    activity = Activity("E1", "S1", "A1",
                        datetime(2024, 7, 1, 12, tzinfo=UTC),
                        datetime(2024, 7, 1, 15, tzinfo=UTC),
                        region_code="US-NY", evidence_id="V1")
    result = evaluate_student("S1", [activity], regions=[NY], rules=[R_NY_6],
                              exceptions=[], evidence=evidence)
    assert result["buckets"][0]["slices"][0]["evidence_status"] == "verified"


def test_evidence_invalid_when_region_mismatches():
    evidence = [EvidenceRecord(
        "V2", "S1", "CN-SH", datetime(2024, 7, 1, 13, tzinfo=UTC), "gps"
    )]
    activity = Activity("E1", "S1", "A1",
                        datetime(2024, 7, 1, 12, tzinfo=UTC),
                        datetime(2024, 7, 1, 15, tzinfo=UTC),
                        region_code="US-NY", evidence_id="V2")
    result = evaluate_student("S1", [activity], regions=[NY, SH],
                              rules=[R_NY_6], exceptions=[], evidence=evidence)
    assert result["buckets"][0]["slices"][0]["evidence_status"] == "invalid"


def test_evidence_invalid_when_observed_outside_activity_window():
    evidence = [EvidenceRecord(
        "V3", "S1", "US-NY", datetime(2024, 7, 1, 20, tzinfo=UTC), "gps"
    )]
    activity = Activity("E1", "S1", "A1",
                        datetime(2024, 7, 1, 12, tzinfo=UTC),
                        datetime(2024, 7, 1, 15, tzinfo=UTC),
                        region_code="US-NY", evidence_id="V3")
    result = evaluate_student("S1", [activity], regions=[NY], rules=[R_NY_6],
                              exceptions=[], evidence=evidence)
    assert result["buckets"][0]["slices"][0]["evidence_status"] == "invalid"


def test_missing_evidence_reference_is_invalid_but_still_evaluated():
    activity = Activity("E1", "S1", "A1",
                        datetime(2024, 7, 1, 12, tzinfo=UTC),
                        datetime(2024, 7, 1, 15, tzinfo=UTC),
                        region_code="US-NY", evidence_id="GHOST")
    result = evaluate_student("S1", [activity], regions=[NY], rules=[R_NY_6],
                              exceptions=[], evidence=[])
    bucket = result["buckets"][0]
    assert bucket["slices"][0]["evidence_status"] == "invalid"
    assert bucket["covered_seconds"] == 3 * 3600
