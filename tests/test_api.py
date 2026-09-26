"""FastAPI 接口测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# at(0)=北京22:00, at(8)=北京06:00（同核心测试基准）
_BASE = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)


def at(h: float) -> datetime:
    return _BASE + timedelta(hours=h)


def payload(level=55.0, step_min=10, **overrides):
    start = at(-step_min / 60)
    n = int((8 * 60 + 2 * step_min) / step_min) + 1
    readings = [
        {"time": (start + timedelta(minutes=step_min * i)).isoformat(), "db": level}
        for i in range(n)
    ]
    p = {
        "timezone": "Asia/Shanghai",
        "night_date": "2026-09-25",
        "limit_db": 55.0,
        "readings": readings,
    }
    p.update(overrides)
    return p


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_assess_sufficient_ok():
    r = client.post("/assess", json=payload(57.0, limit_db=55.0))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sufficient"] is True
    assert body["leq"] == 57.0
    assert body["exceeds"] is True
    assert body["conclusion"] == "超限"
    assert body["window"]["duration_seconds"] == 28800
    assert body["window"]["start"] == "2026-09-25T22:00:00+08:00"
    assert body["window"]["end"] == "2026-09-26T06:00:00+08:00"
    # 恒声级下裁剪段数 = 窗内 10 分钟间隔数 48
    assert len(body["segments"]) == 48


def test_assess_not_exceeding():
    r = client.post("/assess", json=payload(54.9, limit_db=55.0))
    body = r.json()
    assert body["conclusion"] == "未超限"
    assert body["exceeds"] is False


def test_insufficient_head():
    p = payload()
    p["readings"] = [
        rd for i, rd in enumerate(p["readings"]) if i >= 2  # 前两点去掉
    ]
    r = client.post("/assess", json=p)
    body = r.json()
    assert body["sufficient"] is False
    assert body["conclusion"] == "数据不足"
    assert body["reason_code"] == "head_not_covered"
    assert body["leq"] is None
    assert body["exceeds"] is None


def test_duplicate_timestamp_400():
    p = payload()
    p["readings"][5]["time"] = p["readings"][4]["time"]
    r = client.post("/assess", json=p)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "duplicate_timestamp"


def test_out_of_order_400():
    p = payload()
    p["readings"][3], p["readings"][4] = p["readings"][4], p["readings"][3]
    r = client.post("/assess", json=p)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "out_of_order"


def test_non_numeric_db_400():
    p = payload()
    p["readings"][3]["db"] = "loud"
    r = client.post("/assess", json=p)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


def test_nan_db_400():
    # 直接构造含 NaN 标记的 JSON 文本（标准 JSON 不允许，服务端须拒绝）
    raw = (
        '{"timezone":"Asia/Shanghai","night_date":"2026-09-25","limit_db":55,'
        '"readings":[{"time":"2026-09-25T22:00:00+08:00","db":NaN},'
        '{"time":"2026-09-26T06:00:00+08:00","db":55}]}'
    )
    r = client.post("/assess", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_bool_db_400():
    p = payload()
    p["readings"][0]["db"] = True
    r = client.post("/assess", json=p)
    assert r.status_code == 400


def test_naive_time_400():
    p = payload()
    p["readings"][0]["time"] = "2026-09-25T21:50:00"
    r = client.post("/assess", json=p)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


def test_bad_timezone_400():
    r = client.post("/assess", json=payload(timezone="Mars/Olympus"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_timezone"


def test_empty_readings_400():
    p = payload()
    p["readings"] = []
    r = client.post("/assess", json=p)
    assert r.status_code == 400


def test_missing_field_400():
    r = client.post("/assess", json={"timezone": "Asia/Shanghai"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


# ---------- POST /sampling-plan ----------


def plan_payload(**overrides):
    p = {
        "timezone": "Asia/Shanghai",
        "night_date": "2026-09-25",
        "sampled_times": [
            (at(-1 / 6) + timedelta(minutes=10 * i)).isoformat() for i in range(50)
        ],  # 21:50 .. 06:00 每 10 分钟（已合格）
    }
    p.update(overrides)
    return p


def test_sampling_plan_already_qualified():
    r = client.post("/sampling-plan", json=plan_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already_qualified"] is True
    assert body["added_count"] == 0
    assert body["added_times"] == []


def test_sampling_plan_empty_samples():
    # sampled_times 缺省 / null / 空数组 等价：整窗需 33 个补点
    for value in ("omit", None, []):
        p = {"timezone": "Asia/Shanghai", "night_date": "2026-09-25"}
        if value != "omit":
            p["sampled_times"] = value
        r = client.post("/sampling-plan", json=p)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["already_qualified"] is False
        assert body["added_count"] == 33
        assert len(body["added_times"]) == 33
        assert body["added_times"][0] == "2026-09-25T22:00:00+08:00"
        assert body["added_times"][-1] == "2026-09-26T06:00:00+08:00"


def test_sampling_plan_fills_gaps_earliest():
    p = plan_payload(sampled_times=[
        "2026-09-25T22:00:00+08:00",
        "2026-09-25T22:40:00+08:00",
        "2026-09-26T06:00:00+08:00",
    ])
    r = client.post("/sampling-plan", json=p)
    body = r.json()
    assert body["already_qualified"] is False
    assert body["added_count"] == 31
    # 40 分钟空档取较早可行时刻：22:10、22:25
    assert body["added_times"][0] == "2026-09-25T22:10:00+08:00"
    assert body["added_times"][1] == "2026-09-25T22:25:00+08:00"


def test_sampling_plan_anchors_outside_window():
    p = plan_payload(sampled_times=[
        "2026-09-25T21:50:00+08:00",
        "2026-09-26T06:10:00+08:00",
    ])
    r = client.post("/sampling-plan", json=p)
    body = r.json()
    assert body["added_count"] == 33
    assert body["added_times"][0] == "2026-09-25T22:00:00+08:00"
    assert body["added_times"][1] == "2026-09-25T22:10:00+08:00"
    assert body["added_times"][-1] == "2026-09-26T05:55:00+08:00"


def test_sampling_plan_dst_spring_forward():
    p = plan_payload(timezone="America/New_York", night_date="2026-03-07", sampled_times=[])
    r = client.post("/sampling-plan", json=p)
    body = r.json()
    # 春季跳变当晚窗口实际 7 小时：29 个补点，按真实秒数等距
    assert body["added_count"] == 29
    assert body["added_times"][0] == "2026-03-07T22:00:00-05:00"
    assert body["added_times"][-1] == "2026-03-08T06:00:00-04:00"


def test_sampling_plan_duplicate_400():
    p = plan_payload()
    p["sampled_times"][5] = p["sampled_times"][4]
    r = client.post("/sampling-plan", json=p)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "duplicate_timestamp"


def test_sampling_plan_out_of_order_400():
    p = plan_payload()
    p["sampled_times"][3], p["sampled_times"][4] = (
        p["sampled_times"][4],
        p["sampled_times"][3],
    )
    r = client.post("/sampling-plan", json=p)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "out_of_order"


def test_sampling_plan_naive_time_400():
    p = plan_payload(sampled_times=["2026-09-25T22:00:00"])
    r = client.post("/sampling-plan", json=p)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


def test_sampling_plan_bad_timezone_400():
    r = client.post("/sampling-plan", json=plan_payload(timezone="Mars/Olympus"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_timezone"


def test_sampling_plan_missing_field_400():
    r = client.post("/sampling-plan", json={"timezone": "Asia/Shanghai"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
