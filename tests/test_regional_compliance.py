"""跨地区每日工时上限合规的端到端测试。

覆盖：当地时区切分、夏令时、跨境移动分段、重叠活动合并、
人工例外的范围与期限、规则回滚以及冻结快照的规则解释。
"""

from __future__ import annotations

import pytest

SH_PLAN = {"plan_version": "P-REG-SH", "iana_timezone": "Asia/Shanghai", "required_seconds": 0}


def _create_plan(client, plan=None):
    resp = client.post("/api/plans", json=plan or SH_PLAN)
    assert resp.status_code == 201, resp.text


def _publish_rule(client, rule_id, region, version, cap_seconds,
                  effective_from="2024-01-01T00:00:00Z", effective_to=None):
    body = {
        "rule_id": rule_id,
        "region": region,
        "version": version,
        "daily_cap_seconds": cap_seconds,
        "effective_from": effective_from,
        "effective_to": effective_to,
    }
    resp = client.post("/api/region-rules", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "draft"
    resp = client.post(f"/api/region-rules/{rule_id}/publish")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "published"


def _checkin(eid, student, start, end, activity_id="A1", activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": activity_id,
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _import(client, events, plan="P-REG-SH"):
    resp = client.post(f"/api/plans/{plan}/events", json={"events": events})
    assert resp.status_code == 201, resp.text


def _evidence(client, evidence_id, activity_id, region, tz, valid_from,
              student_id=None, plan="P-REG-SH"):
    body = {
        "evidence_id": evidence_id,
        "activity_id": activity_id,
        "region": region,
        "iana_timezone": tz,
        "valid_from": valid_from,
        "source": "gps",
    }
    if student_id is not None:
        body["student_id"] = student_id
    resp = client.post(f"/api/plans/{plan}/locations", json=body)
    assert resp.status_code == 201, resp.text
    return resp


def _preview(client, student_id=None, plan="P-REG-SH"):
    params = {} if student_id is None else {"student_id": student_id}
    resp = client.get(f"/api/plans/{plan}/preview", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _student(preview, sid):
    by_id = {s["student_id"]: s for s in preview["students"]}
    return by_id[sid]


def _days(student):
    return {(d["region"], d["local_day"]): d for d in student["days"]}


# ---------------------------------------------------------------------------
# 当地时区切分与超限识别
# ---------------------------------------------------------------------------


def test_daily_cap_split_by_local_day_and_excess_identified(client):
    _create_plan(client)
    _publish_rule(client, "R-HOME-1", "home", 1, 5400)  # 本校每日上限 1.5 小时
    _import(client, [
        _checkin("E-01", "S1", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00"),
    ])

    student = _student(_preview(client), "S1")
    assert student["evaluated_seconds"] == 4 * 3600
    assert student["excess_seconds"] == 3600
    days = _days(student)
    assert set(days) == {("home", "2024-03-15"), ("home", "2024-03-16")}
    for day in days.values():
        assert day["total_seconds"] == 7200
        assert day["cap_seconds"] == 5400
        assert day["excess_seconds"] == 1800
        assert day["net_excess_seconds"] == 1800
        assert day["status"] == "open"
        assert day["rule_id"] == "R-HOME-1"
        assert day["rule_version"] == 1
    # 每个分段都解释了采用的规则与超限部分
    assert len(student["segments"]) == 2
    for seg in student["segments"]:
        assert seg["seconds"] == 7200
        assert seg["allowed_seconds"] == 5400
        assert seg["excess_seconds"] == 1800
        assert seg["rule_id"] == "R-HOME-1"
        assert seg["cap_seconds"] == 5400


def test_pending_internship_hours_are_not_evaluated(client):
    _create_plan(client)
    _publish_rule(client, "R-HOME-1", "home", 1, 3600)
    _import(client, [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00",
                 "2024-03-15T12:00:00+08:00", activity_type="internship"),
    ])
    # 实习时长未确认前不参与地区上限评估
    assert _preview(client)["students"] == []
    _import(client, [{
        "event_id": "E-02",
        "event_type": "mentor_confirm",
        "student_id": "S1",
        "payload": {"checkin_event_id": "E-01"},
    }])
    student = _student(_preview(client), "S1")
    assert student["excess_seconds"] == 3 * 3600


# ---------------------------------------------------------------------------
# 夏令时
# ---------------------------------------------------------------------------


def test_dst_fallback_day_counts_real_elapsed_hours(client):
    _create_plan(client)
    _publish_rule(client, "R-US-1", "us-east", 1, 3 * 3600)
    _evidence(client, "L1", "A1", "us-east", "America/New_York",
              "2024-11-03T00:00:00-04:00")
    # 2024-11-03 美国冬令时回拨：00:30 EDT -> 03:30 EST 实际经过 4 小时
    _import(client, [
        _checkin("E-01", "S1", "2024-11-03T00:30:00-04:00", "2024-11-03T03:30:00-05:00"),
    ])

    student = _student(_preview(client), "S1")
    days = _days(student)
    # 回拨重复的一小时不会制造第二个当地日
    assert set(days) == {("us-east", "2024-11-03")}
    day = days[("us-east", "2024-11-03")]
    assert day["total_seconds"] == 4 * 3600
    assert day["excess_seconds"] == 3600
    assert len(student["segments"]) == 1
    assert student["segments"][0]["seconds"] == 4 * 3600


def test_dst_spring_forward_day_has_23_hours(client):
    _create_plan(client)
    _publish_rule(client, "R-US-1", "us-east", 1, 2 * 3600)
    _evidence(client, "L1", "A1", "us-east", "America/New_York",
              "2024-03-10T00:00:00-05:00")
    # 2024-03-10 美国夏令时开始：02:00 被跳过，00:30 EST -> 04:30 EDT 实际 3 小时
    _import(client, [
        _checkin("E-01", "S1", "2024-03-10T00:30:00-05:00", "2024-03-10T04:30:00-04:00"),
    ])

    student = _student(_preview(client), "S1")
    days = _days(student)
    assert set(days) == {("us-east", "2024-03-10")}
    day = days[("us-east", "2024-03-10")]
    assert day["total_seconds"] == 3 * 3600
    assert day["excess_seconds"] == 3600


# ---------------------------------------------------------------------------
# 跨境移动：位置变更点分段，各地按自己的时区切日
# ---------------------------------------------------------------------------


def _setup_cross_border_rules(client):
    _publish_rule(client, "R-CN-1", "cn-sh", 1, 3600)
    _publish_rule(client, "R-DE-1", "de-be", 1, 3 * 3600)


def test_mobile_activity_segmented_at_location_change(client):
    _create_plan(client)
    _setup_cross_border_rules(client)
    _evidence(client, "L1", "ATrip", "cn-sh", "Asia/Shanghai",
              "2024-06-01T00:00:00+08:00")
    _evidence(client, "L2", "ATrip", "de-be", "Europe/Berlin",
              "2024-06-01T18:00:00+08:00")
    _import(client, [
        _checkin("E-01", "S1", "2024-06-01T16:00:00+08:00", "2024-06-01T20:00:00+08:00",
                 activity_id="ATrip"),
    ])

    student = _student(_preview(client), "S1")
    segments = student["segments"]
    assert len(segments) == 2
    # 16:00-18:00 上海，适用 cn-sh 上限 1 小时 -> 超限 1 小时
    assert segments[0]["region"] == "cn-sh"
    assert segments[0]["iana_timezone"] == "Asia/Shanghai"
    assert segments[0]["local_day"] == "2024-06-01"
    assert segments[0]["seconds"] == 7200
    assert segments[0]["excess_seconds"] == 3600
    assert segments[0]["rule_id"] == "R-CN-1"
    # 18:00-20:00 上海时间 = 12:00-14:00 柏林，适用 de-be 上限 3 小时 -> 未超限
    assert segments[1]["region"] == "de-be"
    assert segments[1]["iana_timezone"] == "Europe/Berlin"
    assert segments[1]["local_day"] == "2024-06-01"
    assert segments[1]["seconds"] == 7200
    assert segments[1]["excess_seconds"] == 0
    assert segments[1]["rule_id"] == "R-DE-1"
    assert student["excess_seconds"] == 3600


def test_cross_border_local_days_diverge(client):
    _create_plan(client)
    _setup_cross_border_rules(client)
    _evidence(client, "L3", "BTrip", "cn-sh", "Asia/Shanghai",
              "2024-06-01T00:00:00+08:00")
    _evidence(client, "L4", "BTrip", "de-be", "Europe/Berlin",
              "2024-06-02T01:00:00+08:00")
    # 上海时间 23:00 -> 次日 03:00；01:00(+08) 起位置变为柏林，
    # 柏林当地还是 6 月 1 日 19:00。
    _import(client, [
        _checkin("E-02", "S2", "2024-06-01T23:00:00+08:00", "2024-06-02T03:00:00+08:00",
                 activity_id="BTrip"),
    ])

    student = _student(_preview(client), "S2")
    days = _days(student)
    assert set(days) == {
        ("cn-sh", "2024-06-01"),
        ("cn-sh", "2024-06-02"),
        ("de-be", "2024-06-01"),
    }
    assert days[("cn-sh", "2024-06-01")]["total_seconds"] == 3600
    assert days[("cn-sh", "2024-06-02")]["total_seconds"] == 3600
    # 柏林段落在柏林的 6 月 1 日，而不是上海的 6 月 2 日
    assert days[("de-be", "2024-06-01")]["total_seconds"] == 7200
    assert all(d["excess_seconds"] == 0 for d in days.values())


def test_student_scoped_evidence_overrides_shared_activity(client):
    _create_plan(client)
    _setup_cross_border_rules(client)
    _evidence(client, "L1", "ATrip", "cn-sh", "Asia/Shanghai",
              "2024-06-01T00:00:00+08:00")
    # 仅 S2 的行程证据：S2 中途去了柏林，S1 仍在上海
    _evidence(client, "L2", "ATrip", "de-be", "Europe/Berlin",
              "2024-06-01T18:00:00+08:00", student_id="S2")
    _import(client, [
        _checkin("E-01", "S1", "2024-06-01T16:00:00+08:00", "2024-06-01T20:00:00+08:00",
                 activity_id="ATrip"),
        _checkin("E-02", "S2", "2024-06-01T16:00:00+08:00", "2024-06-01T20:00:00+08:00",
                 activity_id="ATrip"),
    ])

    preview = _preview(client)
    s1 = _student(preview, "S1")
    s2 = _student(preview, "S2")
    assert {seg["region"] for seg in s1["segments"]} == {"cn-sh"}
    assert {seg["region"] for seg in s2["segments"]} == {"cn-sh", "de-be"}


# ---------------------------------------------------------------------------
# 重叠活动：合并计时，超限不重复计算
# ---------------------------------------------------------------------------


def test_overlapping_activities_union_before_cap(client):
    _create_plan(client)
    _publish_rule(client, "R-HOME-1", "home", 1, 9000)  # 2.5 小时
    _import(client, [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
                 activity_id="A1"),
        _checkin("E-02", "S1", "2024-03-15T09:30:00+08:00", "2024-03-15T11:00:00+08:00",
                 activity_id="A2"),
    ])

    student = _student(_preview(client), "S1")
    # 并集 08:00-11:00 = 3 小时，而不是 2h + 1.5h
    assert student["evaluated_seconds"] == 3 * 3600
    days = _days(student)
    day = days[("home", "2024-03-15")]
    assert day["total_seconds"] == 3 * 3600
    assert day["excess_seconds"] == 1800
    # 分段超限归因不重复计数：各分段超限之和等于当日超限
    total_segment_excess = sum(s["excess_seconds"] for s in student["segments"])
    assert total_segment_excess == 1800
    by_event = {s["event_id"]: s for s in student["segments"]}
    assert by_event["E-01"]["excess_seconds"] == 0
    assert by_event["E-02"]["excess_seconds"] == 1800


# ---------------------------------------------------------------------------
# 人工例外：必须有范围和期限
# ---------------------------------------------------------------------------


def _setup_home_excess(client, cap=3600):
    _create_plan(client)
    _publish_rule(client, "R-HOME-1", "home", 1, cap)
    _import(client, [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T11:00:00+08:00"),
    ])


def _exception_body(**overrides):
    body = {
        "exception_id": "X1",
        "student_id": "S1",
        "region": "home",
        "local_day": "2024-03-15",
        "valid_from": "2024-03-14T16:00:00Z",   # 2024-03-15 00:00 +08
        "valid_until": "2024-03-15T16:00:00Z",  # 2024-03-16 00:00 +08
        "approver": "dean-1",
        "reason": "approved competition overtime",
    }
    body.update(overrides)
    return body


def test_exception_requires_scope_and_expiry(client):
    _setup_home_excess(client)
    # 缺期限 -> 422
    body = _exception_body()
    del body["valid_until"]
    assert client.post("/api/plans/P-REG-SH/exceptions", json=body).status_code == 422
    # 期限倒置 -> 422
    body = _exception_body(valid_until="2024-03-01T00:00:00Z")
    assert client.post("/api/plans/P-REG-SH/exceptions", json=body).status_code == 422
    # 缺审批人/理由 -> 422
    body = _exception_body(approver="")
    assert client.post("/api/plans/P-REG-SH/exceptions", json=body).status_code == 422
    body = _exception_body(reason="")
    assert client.post("/api/plans/P-REG-SH/exceptions", json=body).status_code == 422
    # 非法日期 -> 422
    body = _exception_body(local_day="2024-3-5")
    assert client.post("/api/plans/P-REG-SH/exceptions", json=body).status_code == 422


def test_active_exception_waives_excess_within_scope(client):
    _setup_home_excess(client)
    resp = client.post("/api/plans/P-REG-SH/exceptions", json=_exception_body())
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "active"

    student = _student(_preview(client), "S1")
    day = _days(student)[("home", "2024-03-15")]
    assert day["excess_seconds"] == 7200
    assert day["waived_seconds"] == 7200
    assert day["net_excess_seconds"] == 0
    assert day["status"] == "waived"
    assert day["exception_ids"] == ["X1"]
    assert student["segments"][0]["waived_seconds"] == 7200

    review = client.get("/api/plans/P-REG-SH/review").json()
    finding = review["findings"][0]
    assert finding["status"] == "waived"
    assert finding["exception_ids"] == ["X1"]


def test_exception_outside_expiry_window_does_not_apply(client):
    _setup_home_excess(client)
    # 期限在当地日开始之前结束 -> 不覆盖
    body = _exception_body(
        exception_id="X-EXPIRED",
        valid_from="2024-03-10T00:00:00Z",
        valid_until="2024-03-14T16:00:00Z",  # 恰好是 2024-03-15 00:00 +08 之前
    )
    client.post("/api/plans/P-REG-SH/exceptions", json=body)
    day = _days(_student(_preview(client), "S1"))[("home", "2024-03-15")]
    assert day["status"] == "open"
    assert day["net_excess_seconds"] == 7200


def test_exception_scope_mismatch_does_not_apply(client):
    _setup_home_excess(client)
    # 活动范围不匹配
    client.post("/api/plans/P-REG-SH/exceptions",
                json=_exception_body(exception_id="X-A", activity_id="OTHER"))
    # 学员不匹配
    client.post("/api/plans/P-REG-SH/exceptions",
                json=_exception_body(exception_id="X-B", student_id="S9"))
    # 地区不匹配
    client.post("/api/plans/P-REG-SH/exceptions",
                json=_exception_body(exception_id="X-C", region="cn-sh"))
    # 日期不匹配
    client.post("/api/plans/P-REG-SH/exceptions",
                json=_exception_body(exception_id="X-D", local_day="2024-03-16"))

    day = _days(_student(_preview(client), "S1"))[("home", "2024-03-15")]
    assert day["status"] == "open"
    assert day["waived_seconds"] == 0


def test_revoked_exception_reopens_finding(client):
    _setup_home_excess(client)
    client.post("/api/plans/P-REG-SH/exceptions", json=_exception_body())
    assert _days(_student(_preview(client), "S1"))[("home", "2024-03-15")]["status"] == "waived"

    resp = client.post("/api/plans/P-REG-SH/exceptions/X1/revoke")
    assert resp.status_code == 200
    assert resp.json()["status"] == "revoked"
    assert resp.json()["revoked_at"] is not None

    day = _days(_student(_preview(client), "S1"))[("home", "2024-03-15")]
    assert day["status"] == "open"
    assert day["net_excess_seconds"] == 7200


def test_duplicate_exception_rejected(client):
    _setup_home_excess(client)
    client.post("/api/plans/P-REG-SH/exceptions", json=_exception_body())
    resp = client.post("/api/plans/P-REG-SH/exceptions", json=_exception_body())
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# 规则版本与回滚
# ---------------------------------------------------------------------------


def test_rule_rollback_restores_cap_and_freeze_is_immutable(client):
    _create_plan(client)
    _publish_rule(client, "R-CN-1", "cn-sh", 1, 8 * 3600)
    _evidence(client, "L1", "A1", "cn-sh", "Asia/Shanghai",
              "2024-01-01T00:00:00+08:00")
    _import(client, [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
    ])
    assert _student(_preview(client), "S1")["excess_seconds"] == 0

    # 新版本收紧到 1 小时：版本号更大者胜出
    _publish_rule(client, "R-CN-2", "cn-sh", 2, 3600)
    student = _student(_preview(client), "S1")
    assert student["excess_seconds"] == 3 * 3600
    assert student["segments"][0]["rule_id"] == "R-CN-2"

    # 冻结快照记录每段采用的规则
    freeze1 = client.post("/api/plans/P-REG-SH/freezes/F-1", json={}).json()
    seg = freeze1["regional"]["students"][0]["segments"][0]
    assert seg["rule_id"] == "R-CN-2"
    assert seg["rule_version"] == 2
    assert seg["cap_seconds"] == 3600
    assert seg["excess_seconds"] == 3 * 3600

    # 回滚到 v1：生成新的已发布版本，上限恢复 8 小时
    rollback = client.post("/api/regions/cn-sh/rollback",
                           json={"target_version": 1, "note": "cap too strict"})
    assert rollback.status_code == 201, rollback.text
    body = rollback.json()
    assert body["version"] == 3
    assert body["status"] == "published"
    assert body["daily_cap_seconds"] == 8 * 3600
    assert body["rollback_of"] == "R-CN-1"

    student = _student(_preview(client), "S1")
    assert student["excess_seconds"] == 0
    assert student["segments"][0]["rule_id"] == body["rule_id"]
    assert student["segments"][0]["rule_version"] == 3

    # 已冻结的快照不受回滚影响，仍解释当时采用的 R-CN-2
    frozen = client.get("/api/plans/P-REG-SH/freezes/F-1").json()
    seg = frozen["regional"]["students"][0]["segments"][0]
    assert seg["rule_id"] == "R-CN-2"
    assert seg["excess_seconds"] == 3 * 3600

    # 新冻结反映回滚后的规则
    freeze2 = client.post("/api/plans/P-REG-SH/freezes/F-2", json={}).json()
    seg2 = freeze2["regional"]["students"][0]["segments"][0]
    assert seg2["rule_version"] == 3
    assert seg2["excess_seconds"] == 0

    # 规则列表包含全部三个版本
    rules = client.get("/api/region-rules", params={"region": "cn-sh"}).json()["rules"]
    assert [(r["version"], r["status"]) for r in rules] == [
        (1, "published"), (2, "published"), (3, "published"),
    ]


def test_retired_rule_no_longer_applies(client):
    _create_plan(client)
    _publish_rule(client, "R-HOME-1", "home", 1, 3600)
    _import(client, [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
    ])
    assert _student(_preview(client), "S1")["excess_seconds"] == 3 * 3600

    resp = client.post("/api/region-rules/R-HOME-1/retire")
    assert resp.status_code == 200
    assert resp.json()["status"] == "retired"

    student = _student(_preview(client), "S1")
    assert student["excess_seconds"] == 0
    seg = student["segments"][0]
    assert seg["rule_id"] is None
    assert seg["cap_seconds"] is None


def test_rule_lifecycle_errors(client):
    _create_plan(client)
    _publish_rule(client, "R-HOME-1", "home", 1, 3600)
    # 重复发布 -> 409
    assert client.post("/api/region-rules/R-HOME-1/publish").status_code == 409
    # 重复 rule_id -> 409
    dup = client.post("/api/region-rules", json={
        "rule_id": "R-HOME-1", "region": "home", "version": 9,
        "daily_cap_seconds": 1, "effective_from": "2024-01-01T00:00:00Z",
    })
    assert dup.status_code == 409
    # 重复 (region, version) -> 409
    dupv = client.post("/api/region-rules", json={
        "rule_id": "R-HOME-9", "region": "home", "version": 1,
        "daily_cap_seconds": 1, "effective_from": "2024-01-01T00:00:00Z",
    })
    assert dupv.status_code == 409
    # 生效窗口倒置 -> 422
    bad = client.post("/api/region-rules", json={
        "rule_id": "R-BAD", "region": "home", "version": 2,
        "daily_cap_seconds": 1,
        "effective_from": "2024-02-01T00:00:00Z",
        "effective_to": "2024-01-01T00:00:00Z",
    })
    assert bad.status_code == 422
    # 回滚到不存在的版本 -> 404
    assert client.post("/api/regions/home/rollback",
                       json={"target_version": 99}).status_code == 404
    # 回滚到草稿版本 -> 409
    client.post("/api/region-rules", json={
        "rule_id": "R-DRAFT", "region": "home", "version": 5,
        "daily_cap_seconds": 1, "effective_from": "2024-01-01T00:00:00Z",
    })
    assert client.post("/api/regions/home/rollback",
                       json={"target_version": 5}).status_code == 409
    # 重复退役 -> 409
    client.post("/api/region-rules/R-HOME-1/retire")
    assert client.post("/api/region-rules/R-HOME-1/retire").status_code == 409


def test_rule_effective_window_selects_version_by_day(client):
    _create_plan(client)
    # v1 仅覆盖 3 月，v2 仅覆盖 4 月
    _publish_rule(client, "R-HOME-1", "home", 1, 3600,
                  effective_from="2024-03-01T00:00:00Z",
                  effective_to="2024-04-01T00:00:00Z")
    _publish_rule(client, "R-HOME-2", "home", 2, 7200,
                  effective_from="2024-04-01T00:00:00Z")
    _import(client, [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _checkin("E-02", "S1", "2024-04-15T08:00:00+08:00", "2024-04-15T10:00:00+08:00"),
        _checkin("E-03", "S1", "2024-02-15T08:00:00+08:00", "2024-02-15T10:00:00+08:00"),
    ])
    days = _days(_student(_preview(client), "S1"))
    assert days[("home", "2024-03-15")]["rule_id"] == "R-HOME-1"
    assert days[("home", "2024-03-15")]["excess_seconds"] == 3600
    assert days[("home", "2024-04-15")]["rule_id"] == "R-HOME-2"
    assert days[("home", "2024-04-15")]["excess_seconds"] == 0
    # 所有版本生效窗口之外的日子没有适用规则
    assert days[("home", "2024-02-15")]["rule_id"] is None
    assert days[("home", "2024-02-15")]["excess_seconds"] == 0


# ---------------------------------------------------------------------------
# 位置证据 API
# ---------------------------------------------------------------------------


def test_evidence_validation_and_idempotency(client):
    _create_plan(client)
    # 未知时区 -> 422
    bad = client.post("/api/plans/P-REG-SH/locations", json={
        "evidence_id": "LX", "activity_id": "A1", "region": "cn-sh",
        "iana_timezone": "Mars/Olympus", "valid_from": "2024-03-15T00:00:00Z",
    })
    assert bad.status_code == 422
    # 朴素时间戳 -> 422
    naive = client.post("/api/plans/P-REG-SH/locations", json={
        "evidence_id": "LX", "activity_id": "A1", "region": "cn-sh",
        "iana_timezone": "Asia/Shanghai", "valid_from": "2024-03-15T00:00:00",
    })
    assert naive.status_code == 422
    # 未知方案 -> 404
    missing = client.post("/api/plans/NOPE/locations", json={
        "evidence_id": "LX", "activity_id": "A1", "region": "cn-sh",
        "iana_timezone": "Asia/Shanghai", "valid_from": "2024-03-15T00:00:00Z",
    })
    assert missing.status_code == 404

    _evidence(client, "L1", "A1", "cn-sh", "Asia/Shanghai", "2024-03-15T00:00:00+08:00")
    # 重复 evidence_id -> 409
    dup = client.post("/api/plans/P-REG-SH/locations", json={
        "evidence_id": "L1", "activity_id": "A1", "region": "cn-sh",
        "iana_timezone": "Asia/Shanghai", "valid_from": "2024-03-15T00:00:00+08:00",
    })
    assert dup.status_code == 409

    listed = client.get("/api/plans/P-REG-SH/locations",
                        params={"activity_id": "A1"}).json()
    assert len(listed["evidence"]) == 1
    assert listed["evidence"][0]["evidence_id"] == "L1"
    assert listed["evidence"][0]["source"] == "gps"


# ---------------------------------------------------------------------------
# 复核与统计
# ---------------------------------------------------------------------------


def test_review_decision_attached_to_finding(client):
    _setup_home_excess(client)
    resp = client.post("/api/plans/P-REG-SH/review", json={
        "student_id": "S1", "region": "home", "local_day": "2024-03-15",
        "decision": "dismissed", "reviewer": "officer-1",
        "reason": "device clock skew confirmed",
    })
    assert resp.status_code == 201, resp.text

    finding = client.get("/api/plans/P-REG-SH/review").json()["findings"][0]
    assert finding["review"]["decision"] == "dismissed"
    assert finding["review"]["reviewer"] == "officer-1"

    # 同一发现再次复核会更新结论
    client.post("/api/plans/P-REG-SH/review", json={
        "student_id": "S1", "region": "home", "local_day": "2024-03-15",
        "decision": "approved", "reviewer": "officer-2", "reason": "final",
    })
    finding = client.get("/api/plans/P-REG-SH/review").json()["findings"][0]
    assert finding["review"]["decision"] == "approved"
    assert finding["review"]["reviewer"] == "officer-2"

    # 引用不存在的例外 -> 404
    missing = client.post("/api/plans/P-REG-SH/review", json={
        "student_id": "S1", "region": "home", "local_day": "2024-03-15",
        "decision": "approved", "reviewer": "o", "reason": "r",
        "exception_id": "NOPE",
    })
    assert missing.status_code == 404


def test_statistics_aggregate_by_region(client):
    _create_plan(client)
    _publish_rule(client, "R-HOME-1", "home", 1, 3600)
    _publish_rule(client, "R-US-1", "us-east", 1, 3 * 3600)
    _evidence(client, "L1", "A2", "us-east", "America/New_York",
              "2024-03-15T00:00:00-04:00")
    _import(client, [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00",
                 activity_id="A1"),
        _checkin("E-02", "S2", "2024-03-15T09:00:00-04:00", "2024-03-15T12:00:00-04:00",
                 activity_id="A2"),
    ])
    # S1 的超限被豁免
    client.post("/api/plans/P-REG-SH/exceptions", json=_exception_body())

    stats = client.get("/api/plans/P-REG-SH/statistics").json()
    totals = stats["totals"]
    assert totals["students"] == 2
    assert totals["evaluated_seconds"] == 7200 + 3 * 3600
    assert totals["excess_seconds"] == 3600
    assert totals["waived_seconds"] == 3600
    assert totals["net_excess_seconds"] == 0
    assert totals["students_with_excess"] == 1
    assert totals["open_findings"] == 0

    regions = {r["region"]: r for r in stats["regions"]}
    assert regions["home"]["evaluated_seconds"] == 7200
    assert regions["home"]["excess_seconds"] == 3600
    assert regions["home"]["students_with_excess"] == 1
    assert regions["home"]["days_with_excess"] == 0  # 已豁免，无未结超限
    assert regions["us-east"]["evaluated_seconds"] == 3 * 3600
    assert regions["us-east"]["excess_seconds"] == 0


def test_live_snapshot_and_freeze_explain_regional_rules(client):
    _setup_home_excess(client)
    live = client.get("/api/plans/P-REG-SH/snapshot").json()
    regional = live["regional"]["students"][0]
    assert regional["days"][0]["rule_id"] == "R-HOME-1"
    assert regional["days"][0]["excess_seconds"] == 7200

    client.post("/api/plans/P-REG-SH/freezes/F-1", json={})
    frozen = client.get("/api/plans/P-REG-SH/freezes/F-1").json()
    seg = frozen["regional"]["students"][0]["segments"][0]
    assert seg["rule_id"] == "R-HOME-1"
    assert seg["rule_version"] == 1
    assert seg["excess_seconds"] == 7200
    # 冻结后规则变化不影响快照
    client.post("/api/region-rules/R-HOME-1/retire")
    again = client.get("/api/plans/P-REG-SH/freezes/F-1").json()
    assert again["regional"]["students"][0]["segments"][0]["rule_id"] == "R-HOME-1"
