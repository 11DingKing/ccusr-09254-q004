"""跨地区合规 API 集成测试。

覆盖端到端链路：地区与规则版本（发布/退役/回滚）、地点证据、
静止与移动打卡、预览、人工例外复核、统计，以及冻结快照对每段规则的固化。
"""

from __future__ import annotations

from tests.conftest import SHANGHAI_PLAN

PV = SHANGHAI_PLAN["plan_version"]


def _plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def _regions(client):
    for code, tz, label in [
        ("US-NY", "America/New_York", "纽约"),
        ("CN-SH", "Asia/Shanghai", "上海"),
        ("DE-BE", "Europe/Berlin", "柏林"),
    ]:
        resp = client.put(
            f"/api/plans/{PV}/regions/{code}",
            json={"region_code": code, "iana_timezone": tz, "label": label},
        )
        assert resp.status_code == 200, resp.text


def _publish_rule(client, rule_id, region, cap, now, created_by="coord"):
    resp = client.post(
        f"/api/plans/{PV}/cap-rules",
        json={
            "rule_id": rule_id,
            "region_code": region,
            "daily_cap_seconds": cap,
            "created_by": created_by,
            "reason": "学期初上限",
        },
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        f"/api/plans/{PV}/cap-rules/{rule_id}/transition",
        json={"status": "published", "now": now},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _checkin(client, eid, student, start, end, *, location=None, track=None,
             activity_type="regular", activity_id="A1"):
    payload = {
        "activity_id": activity_id,
        "activity_type": activity_type,
        "check_in_at": start,
        "check_out_at": end,
    }
    if location is not None:
        payload["location"] = location
    if track is not None:
        payload["track"] = track
    resp = client.post(
        f"/api/plans/{PV}/events",
        json={"events": [{"event_id": eid, "event_type": "checkin",
                          "student_id": student, "payload": payload}]},
    )
    assert resp.status_code == 201, resp.text
    return resp


# ---------------------------------------------------------------------------
# 基础设置校验
# ---------------------------------------------------------------------------

def test_regional_endpoints_require_existing_plan(client):
    resp = client.put(
        "/api/plans/NOPE/regions/US-NY",
        json={"region_code": "US-NY", "iana_timezone": "America/New_York"},
    )
    assert resp.status_code == 404


def test_region_timezone_must_be_valid_iana(client):
    _plan(client)
    resp = client.put(
        f"/api/plans/{PV}/regions/BAD",
        json={"region_code": "BAD", "iana_timezone": "Mars/Olympus"},
    )
    assert resp.status_code == 400


def test_rule_requires_registered_region(client):
    _plan(client)
    resp = client.post(
        f"/api/plans/{PV}/cap-rules",
        json={"rule_id": "R1", "region_code": "US-NY",
              "daily_cap_seconds": 4 * 3600},
    )
    assert resp.status_code == 400


def test_draft_cap_editable_published_is_not(client):
    _plan(client)
    _regions(client)
    client.post(
        f"/api/plans/{PV}/cap-rules",
        json={"rule_id": "R1", "region_code": "US-NY",
              "daily_cap_seconds": 4 * 3600},
    )
    ok = client.patch(
        f"/api/plans/{PV}/cap-rules/R1",
        json={"daily_cap_seconds": 5 * 3600},
    )
    assert ok.status_code == 200 and ok.json()["daily_cap_seconds"] == 5 * 3600

    client.post(
        f"/api/plans/{PV}/cap-rules/R1/transition",
        json={"status": "published", "now": "2024-01-01T00:00:00Z"},
    )
    blocked = client.patch(
        f"/api/plans/{PV}/cap-rules/R1",
        json={"daily_cap_seconds": 9 * 3600},
    )
    assert blocked.status_code == 400


# ---------------------------------------------------------------------------
# 规则回滚
# ---------------------------------------------------------------------------

def test_rule_rollback_closes_and_reopens_windows(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R-OLD", "US-NY", 4 * 3600, "2024-01-01T00:00:00Z")
    _publish_rule(client, "R-NEW", "US-NY", 8 * 3600, "2024-06-01T00:00:00Z")

    # 6 月发布 R-NEW 时，R-OLD 的开放窗口应在 2024-06-01 关闭。
    old = client.get(f"/api/plans/{PV}/cap-rules/R-OLD").json()
    assert old["windows"][0]["effective_to_utc"] == "2024-06-01T00:00:00Z"
    new = client.get(f"/api/plans/{PV}/cap-rules/R-NEW").json()
    assert new["windows"][0]["effective_to_utc"] is None

    # 9 月退役 R-NEW 并恢复（回滚）R-OLD。
    r = client.post(
        f"/api/plans/{PV}/cap-rules/R-NEW/transition",
        json={"status": "retired", "now": "2024-09-01T00:00:00Z"},
    )
    assert r.status_code == 200 and r.json()["status"] == "retired"
    r = client.post(
        f"/api/plans/{PV}/cap-rules/R-OLD/transition",
        json={"status": "restored", "now": "2024-09-01T00:00:00Z"},
    )
    assert r.status_code == 200
    old = client.get(f"/api/plans/{PV}/cap-rules/R-OLD").json()
    assert old["status"] == "restored"
    assert len(old["windows"]) == 2
    assert old["windows"][1]["effective_from_utc"] == "2024-09-01T00:00:00Z"
    assert old["windows"][1]["effective_to_utc"] is None

    # 非法跃迁被拒绝：草稿不能直接恢复。
    bad = client.post(
        f"/api/plans/{PV}/cap-rules/R-OLD/transition",
        json={"status": "published", "now": "2024-09-02T00:00:00Z"},
    )
    assert bad.status_code == 400


def test_rollback_changes_live_evaluation_but_not_frozen_snapshot(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R1", "US-NY", 8 * 3600, "2024-01-01T00:00:00Z")

    # 7 月某天在纽约打卡 6 小时（08:00-14:00 EDT = 12:00-18:00 UTC）。
    _checkin(
        client, "E1", "S1",
        "2024-07-02T08:00:00-04:00", "2024-07-02T14:00:00-04:00",
        location={"region_code": "US-NY"},
    )
    # 8h 上限下合规。
    overview = client.get(f"/api/plans/{PV}/regional/overview").json()
    s1 = overview["students"]["S1"]
    assert s1["totals"]["has_violation"] is False
    bucket = s1["buckets"][0]
    assert bucket["rule_id"] == "R1"
    assert bucket["cap_seconds"] == 8 * 3600

    # 冻结 F1：解释每段采用的规则。
    f1 = client.post(f"/api/plans/{PV}/freezes/F1", json={})
    assert f1.status_code == 201, f1.text
    frozen = f1.json()["regional_compliance"]
    frozen_bucket = frozen["students"]["S1"]["buckets"][0]
    assert frozen_bucket["rule_id"] == "R1"
    assert frozen_bucket["cap_seconds"] == 8 * 3600
    catalog = {r["rule_id"]: r for r in frozen["rule_catalog"]}
    assert catalog["R1"]["status"] == "published"

    # 9 月把上限收紧到 4h：发布 R2，回滚场景下再退役 R2、恢复 R1 不影响 F1；
    # 这里先直接发布更严的 R2。
    _publish_rule(client, "R2", "US-NY", 4 * 3600, "2024-09-01T00:00:00Z")
    overview = client.get(f"/api/plans/{PV}/regional/overview").json()
    # 7 月的活动仍由历史窗口的 R1（8h，已在 9 月关闭）裁定 -> 依旧合规，
    # 因为规则按活动时刻选择。
    s1 = overview["students"]["S1"]
    assert s1["buckets"][0]["rule_id"] == "R1"
    assert s1["totals"]["has_violation"] is False

    # F1 冻结内容不变。
    f1_again = client.get(f"/api/plans/{PV}/freezes/F1").json()
    fb = f1_again["regional_compliance"]["students"]["S1"]["buckets"][0]
    assert fb["rule_id"] == "R1" and fb["cap_seconds"] == 8 * 3600


# ---------------------------------------------------------------------------
# 地点证据 + 静止 / 移动活动
# ---------------------------------------------------------------------------

def test_static_checkin_evaluated_against_region_cap(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R-NY", "US-NY", 4 * 3600, "2024-01-01T00:00:00Z")
    _checkin(
        client, "E1", "S1",
        "2024-07-02T08:00:00-04:00", "2024-07-02T14:00:00-04:00",
        location={"region_code": "US-NY", "evidence_id": "V1"},
    )
    # 登记证据：观察时刻落在活动窗口内且地区一致。
    ev = client.put(
        f"/api/plans/{PV}/evidence/V1",
        json={
            "evidence_id": "V1", "student_id": "S1", "region_code": "US-NY",
            "observed_at": "2024-07-02T10:00:00-04:00",
            "source": "campus-gps", "detail": {"accuracy_m": 12},
        },
    )
    assert ev.status_code == 200
    detail = client.get(
        f"/api/plans/{PV}/regional/students/S1"
    ).json()
    bucket = detail["buckets"][0]
    assert bucket["region_code"] == "US-NY"
    assert bucket["local_day"] == "2024-07-02"
    assert bucket["covered_seconds"] == 6 * 3600
    assert bucket["over_seconds"] == 2 * 3600
    assert bucket["slices"][0]["evidence_status"] == "verified"
    assert detail["totals"]["violation_days"] == 1


def test_cross_border_mobile_checkin_segments_at_change_point(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R-NY", "US-NY", 6 * 3600, "2024-01-01T00:00:00Z")
    _publish_rule(client, "R-SH", "CN-SH", 3 * 3600, "2024-01-01T00:00:00Z")

    # 12:00-20:00 UTC：纽约 4h，随后上海（次日）4h。
    _checkin(
        client, "E1", "S1",
        "2024-07-01T12:00:00Z", "2024-07-01T20:00:00Z",
        track=[
            {"at": "2024-07-01T12:00:00Z", "region_code": "US-NY"},
            {"at": "2024-07-01T16:00:00Z", "region_code": "CN-SH"},
        ],
        activity_id="TRIP-1",
    )
    detail = client.get(
        f"/api/plans/{PV}/regional/students/S1"
    ).json()
    by_region = {b["region_code"]: b for b in detail["buckets"]}
    assert by_region["US-NY"]["covered_seconds"] == 4 * 3600
    assert by_region["US-NY"]["over_seconds"] == 0
    assert by_region["CN-SH"]["local_day"] == "2024-07-02"
    assert by_region["CN-SH"]["covered_seconds"] == 4 * 3600
    assert by_region["CN-SH"]["over_seconds"] == 3600

    stats = client.get(f"/api/plans/{PV}/regional/statistics").json()
    assert stats["students_with_violation"] == 1
    region_stats = {r["region_code"]: r for r in stats["by_region"]}
    assert region_stats["CN-SH"]["violation_days"] == 1
    assert region_stats["US-NY"]["violation_days"] == 0


def test_location_and_track_together_rejected(client):
    _plan(client)
    _regions(client)
    resp = client.post(
        f"/api/plans/{PV}/events",
        json={"events": [{
            "event_id": "E1", "event_type": "checkin", "student_id": "S1",
            "payload": {
                "check_in_at": "2024-07-01T12:00:00Z",
                "check_out_at": "2024-07-01T14:00:00Z",
                "location": {"region_code": "US-NY"},
                "track": [{"at": "2024-07-01T12:00:00Z",
                           "region_code": "US-NY"}],
            },
        }]},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 夏令时
# ---------------------------------------------------------------------------

def test_dst_fall_back_night_uses_real_hours(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R-NY", "US-NY", 3 * 3600, "2024-01-01T00:00:00Z")
    # 秋退：00:30 EDT -> 03:30 EST，真实 4 小时，上限 3h -> 超 1h。
    _checkin(
        client, "E1", "S1",
        "2024-11-03T00:30:00-04:00", "2024-11-03T03:30:00-05:00",
        location={"region_code": "US-NY"},
    )
    detail = client.get(
        f"/api/plans/{PV}/regional/students/S1"
    ).json()
    bucket = detail["buckets"][0]
    assert bucket["covered_seconds"] == 4 * 3600
    assert bucket["over_seconds"] == 3600


# ---------------------------------------------------------------------------
# 重叠活动
# ---------------------------------------------------------------------------

def test_overlapping_activities_counted_once_and_attributed(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R-NY", "US-NY", 6 * 3600, "2024-01-01T00:00:00Z")
    _checkin(
        client, "E1", "S1",
        "2024-07-01T12:00:00Z", "2024-07-01T15:00:00Z",
        location={"region_code": "US-NY"}, activity_id="A1",
    )
    _checkin(
        client, "E2", "S1",
        "2024-07-01T14:00:00Z", "2024-07-01T19:00:00Z",
        location={"region_code": "US-NY"}, activity_id="A2",
    )
    detail = client.get(
        f"/api/plans/{PV}/regional/students/S1"
    ).json()
    bucket = detail["buckets"][0]
    assert bucket["covered_seconds"] == 7 * 3600
    assert bucket["over_seconds"] == 3600
    assert {s["event_id"] for s in bucket["slices"]
            if s["status"] == "over_limit"} == {"E2"}


# ---------------------------------------------------------------------------
# 预览
# ---------------------------------------------------------------------------

def test_preview_draft_checkin_does_not_persist_events(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R-NY", "US-NY", 4 * 3600, "2024-01-01T00:00:00Z")
    resp = client.post(
        f"/api/plans/{PV}/regional/preview",
        json={
            "include_confirmed": False,
            "events": [{
                "event_id": "D1",
                "student_id": "S9",
                "check_in_at": "2024-07-01T12:00:00Z",
                "check_out_at": "2024-07-01T19:00:00Z",
                "location": {"region_code": "US-NY"},
            }],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["preview"] is True
    s9 = body["students"]["S9"]
    assert s9["totals"]["over_seconds"] == 3 * 3600

    # 草稿未落库：正式评估与事件导入记录中都没有 S9。
    missing = client.get(f"/api/plans/{PV}/regional/students/S9")
    assert missing.status_code == 404


def test_preview_includes_confirmed_when_requested(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R-NY", "US-NY", 8 * 3600, "2024-01-01T00:00:00Z")
    _checkin(
        client, "E1", "S1",
        "2024-07-01T12:00:00Z", "2024-07-01T18:00:00Z",
        location={"region_code": "US-NY"},
    )
    resp = client.post(
        f"/api/plans/{PV}/regional/preview",
        json={
            "include_confirmed": True,
            "events": [{
                "event_id": "D1",
                "student_id": "S1",
                "check_in_at": "2024-07-01T17:00:00Z",
                "check_out_at": "2024-07-01T20:00:00Z",
                "location": {"region_code": "US-NY"},
            }],
        },
    )
    body = resp.json()
    # 已确认 6h + 草稿与最后 1h 重叠 -> 并集 8h，恰好上限。
    s1 = body["students"]["S1"]
    assert s1["buckets"][0]["covered_seconds"] == 8 * 3600
    assert s1["totals"]["over_seconds"] == 0


# ---------------------------------------------------------------------------
# 人工例外复核
# ---------------------------------------------------------------------------

def test_exception_review_workflow_scope_and_period_required(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R-NY", "US-NY", 6 * 3600, "2024-01-01T00:00:00Z")
    _checkin(
        client, "E1", "S1",
        "2024-07-01T12:00:00Z", "2024-07-01T15:00:00Z",
        location={"region_code": "US-NY"}, activity_id="A1",
    )
    _checkin(
        client, "E2", "S1",
        "2024-07-01T14:00:00Z", "2024-07-01T19:00:00Z",
        location={"region_code": "US-NY"}, activity_id="A2",
    )
    # 超限 1h 先被识别。
    assert client.get(
        f"/api/plans/{PV}/regional/students/S1"
    ).json()["totals"]["over_seconds"] == 3600

    # 期限非法（止早于起）被拒绝。
    bad = client.post(
        f"/api/plans/{PV}/exceptions",
        json={
            "exception_id": "X1", "student_id": "S1", "region_code": "US-NY",
            "valid_from_utc": "2024-07-02T00:00:00Z",
            "valid_to_utc": "2024-07-01T00:00:00Z",
            "reason": "时间填反",
        },
    )
    assert bad.status_code == 400

    # 申请（尚未批准，不生效）。
    req = client.post(
        f"/api/plans/{PV}/exceptions",
        json={
            "exception_id": "X1", "student_id": "S1", "region_code": "US-NY",
            "valid_from_utc": "2024-07-01T00:00:00Z",
            "valid_to_utc": "2024-07-02T00:00:00Z",
            "cap_override_seconds": 8 * 3600,
            "reason": "校企联合活动日",
        },
    )
    assert req.status_code == 201 and req.json()["status"] == "requested"
    assert client.get(
        f"/api/plans/{PV}/regional/students/S1"
    ).json()["totals"]["over_seconds"] == 3600

    # 批准：上限抬高到 8h，超限消除。
    rev = client.post(
        f"/api/plans/{PV}/exceptions/X1/review",
        json={"approve": True, "reviewed_by": "dean-chen",
              "reason": "情况属实"},
    )
    assert rev.status_code == 200 and rev.json()["status"] == "approved"
    detail = client.get(
        f"/api/plans/{PV}/regional/students/S1"
    ).json()
    bucket = detail["buckets"][0]
    assert bucket["cap_seconds"] == 8 * 3600
    assert bucket["over_seconds"] == 0
    assert "X1" in bucket["exceptions_applied"]

    # 撤销后恢复超限。
    cancel = client.post(
        f"/api/plans/{PV}/exceptions/X1/revoke",
        json={"reviewed_by": "dean-chen"},
    )
    assert cancel.status_code == 200
    assert client.get(
        f"/api/plans/{PV}/regional/students/S1"
    ).json()["totals"]["over_seconds"] == 3600


def test_exception_reject_and_repeat_review_blocked(client):
    _plan(client)
    _regions(client)
    client.post(
        f"/api/plans/{PV}/exceptions",
        json={
            "exception_id": "X1", "student_id": "S1",
            "valid_from_utc": "2024-07-01T00:00:00Z",
            "valid_to_utc": "2024-07-02T00:00:00Z",
        },
    )
    rev = client.post(
        f"/api/plans/{PV}/exceptions/X1/review",
        json={"approve": False, "reviewed_by": "dean-chen",
              "reason": "材料不足"},
    )
    assert rev.json()["status"] == "rejected"
    again = client.post(
        f"/api/plans/{PV}/exceptions/X1/review",
        json={"approve": True, "reviewed_by": "dean-chen"},
    )
    assert again.status_code == 400


# ---------------------------------------------------------------------------
# 冻结快照解释
# ---------------------------------------------------------------------------

def test_freeze_snapshot_explains_rule_used_for_every_segment(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R-NY", "US-NY", 6 * 3600, "2024-01-01T00:00:00Z")
    _publish_rule(client, "R-SH", "CN-SH", 3 * 3600, "2024-01-01T00:00:00Z")
    _checkin(
        client, "E1", "S1",
        "2024-07-01T12:00:00Z", "2024-07-01T20:00:00Z",
        track=[
            {"at": "2024-07-01T12:00:00Z", "region_code": "US-NY"},
            {"at": "2024-07-01T16:00:00Z", "region_code": "CN-SH"},
        ],
    )
    frozen = client.post(
        f"/api/plans/{PV}/freezes/F1", json={}
    ).json()["regional_compliance"]

    # 快照内嵌规则目录、地区、统计与逐段解释。
    catalog_ids = {r["rule_id"] for r in frozen["rule_catalog"]}
    assert {"R-NY", "R-SH"} <= catalog_ids
    assert {r["region_code"] for r in frozen["regions"]} >= {"US-NY", "CN-SH"}

    s1 = frozen["students"]["S1"]
    rules_by_region = {b["region_code"]: b["rule_id"] for b in s1["buckets"]}
    assert rules_by_region == {"US-NY": "R-NY", "CN-SH": "R-SH"}
    # 每段切片都带有所采用规则归属桶的状态。
    for bucket in s1["buckets"]:
        assert bucket["window_id"] is not None
        assert bucket["rule_status"] == "published"
    assert frozen["statistics"]["students_with_violation"] == 1


# ---------------------------------------------------------------------------
# 补充边界场景
# ---------------------------------------------------------------------------

def test_pending_internship_not_evaluated_until_confirmed(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R-NY", "US-NY", 2 * 3600, "2024-01-01T00:00:00Z")
    # internship 打卡需要导师确认，之前为 PENDING，不参与上限评估。
    _checkin(
        client, "E1", "S1",
        "2024-07-01T12:00:00Z", "2024-07-01T18:00:00Z",
        location={"region_code": "US-NY"}, activity_type="internship",
    )
    missing = client.get(f"/api/plans/{PV}/regional/students/S1")
    assert missing.status_code == 404

    # 导师确认后进入评估，6h 超 2h 上限。
    resp = client.post(
        f"/api/plans/{PV}/events",
        json={"events": [{
            "event_id": "E2", "event_type": "mentor_confirm", "student_id": "S1",
            "payload": {"checkin_event_id": "E1"},
        }]},
    )
    assert resp.status_code == 201
    detail = client.get(f"/api/plans/{PV}/regional/students/S1").json()
    assert detail["buckets"][0]["covered_seconds"] == 6 * 3600
    assert detail["buckets"][0]["over_seconds"] == 4 * 3600


def test_local_midnight_split_uses_independent_daily_caps(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R-SH", "CN-SH", 3 * 3600, "2024-01-01T00:00:00Z")
    # 上海当地 22:00 - 次日 02:00（UTC 14:00-18:00），每天 2h，均不超 3h。
    _checkin(
        client, "E1", "S1",
        "2024-07-01T22:00:00+08:00", "2024-07-02T02:00:00+08:00",
        location={"region_code": "CN-SH"},
    )
    detail = client.get(f"/api/plans/{PV}/regional/students/S1").json()
    days = {b["local_day"]: b for b in detail["buckets"]}
    assert set(days) == {"2024-07-01", "2024-07-02"}
    assert all(b["over_seconds"] == 0 for b in days.values())
    assert sum(b["covered_seconds"] for b in days.values()) == 4 * 3600


def test_repeated_rollbacks_append_windows(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R1", "US-NY", 4 * 3600, "2024-01-01T00:00:00Z")
    _publish_rule(client, "R2", "US-NY", 8 * 3600, "2024-06-01T00:00:00Z")
    # 9 月回滚 R1（w2）。
    client.post(f"/api/plans/{PV}/cap-rules/R2/transition",
                json={"status": "retired", "now": "2024-09-01T00:00:00Z"})
    client.post(f"/api/plans/{PV}/cap-rules/R1/transition",
                json={"status": "restored", "now": "2024-09-01T00:00:00Z"})
    # 12 月再退役 R1、发布 R3，次年 3 月再次恢复 R1（w3）。
    client.post(f"/api/plans/{PV}/cap-rules/R1/transition",
                json={"status": "retired", "now": "2024-12-01T00:00:00Z"})
    _publish_rule(client, "R3", "US-NY", 10 * 3600, "2024-12-01T00:00:00Z")
    client.post(f"/api/plans/{PV}/cap-rules/R3/transition",
                json={"status": "retired", "now": "2025-03-01T00:00:00Z"})
    client.post(f"/api/plans/{PV}/cap-rules/R1/transition",
                json={"status": "restored", "now": "2025-03-01T00:00:00Z"})
    r1 = client.get(f"/api/plans/{PV}/cap-rules/R1").json()
    assert [w["seq"] for w in r1["windows"]] == [1, 2, 3]
    assert r1["windows"][2]["effective_from_utc"] == "2025-03-01T00:00:00Z"


def test_freeze_regional_block_respects_event_cutoff(client):
    _plan(client)
    _regions(client)
    _publish_rule(client, "R-NY", "US-NY", 4 * 3600, "2024-01-01T00:00:00Z")
    _checkin(
        client, "E1", "S1",
        "2024-07-01T12:00:00Z", "2024-07-01T15:00:00Z",
        location={"region_code": "US-NY"},
    )
    client.post(f"/api/plans/{PV}/freezes/F1", json={})
    # 冻结后新增超限打卡。
    _checkin(
        client, "E9", "S1",
        "2024-07-02T12:00:00Z", "2024-07-02T19:00:00Z",
        location={"region_code": "US-NY"},
    )
    frozen = client.get(f"/api/plans/{PV}/freezes/F1").json()["regional_compliance"]
    assert frozen["event_cutoff_id"] == "E1"
    # 冻结块只含 E1 当天，无 E9 带来的违规。
    s1 = frozen["students"]["S1"]
    assert len(s1["buckets"]) == 1
    assert s1["totals"]["over_seconds"] == 0

    # 实时总览则包含 E9 的 3h 超限。
    live = client.get(f"/api/plans/{PV}/regional/overview").json()
    assert live["students"]["S1"]["totals"]["over_seconds"] == 3 * 3600
