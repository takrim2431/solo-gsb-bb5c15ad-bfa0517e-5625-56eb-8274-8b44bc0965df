"""核心核算逻辑测试。"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.assessment import MAX_GAP, AssessmentError, assess, elapsed, get_timezone, night_window

CST = ZoneInfo("Asia/Shanghai")
D = date(2026, 9, 25)

# at(0) = 北京 22:00（窗口起点，UTC 14:00）；at(8) = 次日北京 06:00（窗口终点）
_BASE = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)


def at(h: float, tz=timezone.utc) -> datetime:
    return (_BASE + timedelta(hours=h)).astimezone(tz)


def flat_readings(level: float = 55.0, step_min: int = 10):
    """窗口两端各外延一个步长、按绝对时刻等间隔采样的恒声级读数。"""
    start = at(-step_min / 60)   # 北京 21:50
    end = at(8 + step_min / 60)  # 北京 06:10
    n = round((end - start) / timedelta(minutes=step_min)) + 1
    return [(start + timedelta(minutes=step_min * i), level) for i in range(n)]


def test_flat_level_leq_equals_level():
    res = assess(CST, D, flat_readings(55.0), limit_db=55.0)
    assert res.sufficient
    assert res.leq == pytest.approx(55.0, abs=1e-9)
    assert res.exceeds is False  # 恰好等于限值不算超限


def test_exceeds_uses_strict_greater_than():
    res = assess(CST, D, flat_readings(70.1), limit_db=70.0)
    assert res.leq == pytest.approx(70.1, abs=1e-9)
    assert res.exceeds is True


def test_weighted_leq_two_levels():
    # 以窗口中点 02:00 为界：前半窗 50 dBA、后半窗 70 dBA（各 4 小时）
    mid = at(4)
    readings = [(t, 70.0 if t >= mid else 50.0) for t, _ in flat_readings(0.0)]
    res = assess(CST, D, readings, limit_db=60.0)
    expected = 10 * math.log10((10 ** 5 + 10 ** 7) / 2)
    assert res.leq == pytest.approx(expected, abs=1e-6)
    assert res.leq == pytest.approx(67.0329, abs=1e-3)
    assert res.exceeds is True
    # 裁剪后的段应完整拼出 8 小时
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(8 * 3600, abs=1e-6)


def test_segment_uses_previous_reading_level():
    # 读数 22:00 为 60、22:10 起降为 40：第一段（22:00-22:10）须按 60 计权
    readings = flat_readings(0.0)
    jump = at(0) + timedelta(seconds=1)
    times = [t for t, _ in readings]
    levels = [40.0 if t >= jump else 60.0 for t in times]
    res = assess(CST, D, list(zip(times, levels)), limit_db=55.0)
    assert res.sufficient
    first_seg = res.segments[0]
    assert first_seg.level_db == 60.0
    assert first_seg.duration_seconds == pytest.approx(600.0, abs=1e-6)
    expected = 10 * math.log10((600 * 10**6 + 28200 * 10**4) / 28800)
    assert res.leq == pytest.approx(expected, abs=1e-6)


def test_head_not_covered():
    readings = [(t, l) for t, l in flat_readings(50.0) if t > at(0)]  # 首个读数 22:10
    res = assess(CST, D, readings, limit_db=55.0)
    assert not res.sufficient
    assert res.reason == "head_not_covered"


def test_tail_not_covered():
    readings = [(t, l) for t, l in flat_readings(50.0) if t < at(8)]  # 末个读数 05:50
    res = assess(CST, D, readings, limit_db=55.0)
    assert not res.sufficient
    assert res.reason == "tail_not_covered"


def test_exact_15_min_gap_allowed_but_15_plus_1s_rejected():
    # 整窗恰好 15 分钟等间隔：充分
    grid = [(at(-0.25 + 0.25 * i), 55.0) for i in range(35)]  # 21:45 .. 06:15
    assert assess(CST, D, grid, limit_db=55.0).sufficient

    # 将中点之后的读数整体后移 6 分钟，制造一个 16 分钟空档：数据不足
    split = at(4)
    widened = [(t + timedelta(minutes=6) if t > split else t, l) for t, l in grid]
    res = assess(CST, D, widened, limit_db=55.0)
    assert not res.sufficient
    assert res.reason == "gap_too_large"


def test_gap_outside_window_does_not_matter():
    # 白天（20:00-22:00）的大间隔不影响夜间评估
    readings = [(at(-2), 50.0), (at(0), 50.0)]
    readings += [(at(10 / 60 * i), 50.0) for i in range(1, 49)]  # 22:10 .. 06:00
    res = assess(CST, D, readings, limit_db=55.0)
    assert res.sufficient


def test_exact_boundary_coverage_ok():
    # 首尾读数恰好落在窗口边界
    readings = [(at(10 / 60 * i), 55.0) for i in range(49)]  # 22:00 .. 06:00
    res = assess(CST, D, readings, limit_db=55.0)
    assert res.sufficient
    w = night_window(CST, D)
    assert res.segments[0].start == w.start
    assert res.segments[-1].end == w.end


def test_duplicate_timestamp_rejected():
    readings = flat_readings(50.0)
    readings[5] = (readings[4][0], 60.0)
    with pytest.raises(AssessmentError, match="重复"):
        assess(CST, D, readings, limit_db=55.0)


def test_out_of_order_rejected():
    readings = flat_readings(50.0)
    readings[3], readings[4] = readings[4], readings[3]
    with pytest.raises(AssessmentError, match="乱序"):
        assess(CST, D, readings, limit_db=55.0)


def test_naive_timestamp_rejected():
    readings = [(datetime(2026, 9, 25, 22, 0), 50.0), (datetime(2026, 9, 26, 6, 0), 50.0)]
    with pytest.raises(AssessmentError, match="时区"):
        assess(CST, D, readings, limit_db=55.0)


def test_readings_in_other_timezone_offsets_work():
    # 读数以 +09:00（东京）给出，窗口按上海时区裁剪，绝对时刻一致即可
    tokyo = timezone(timedelta(hours=9))
    readings = [(t.astimezone(tokyo), l) for t, l in flat_readings(55.0)]
    res = assess(CST, D, readings, limit_db=55.0)
    assert res.sufficient
    assert res.leq == pytest.approx(55.0, abs=1e-9)
    # 输出的裁剪段时间戳换算到施工地时区，从当地 22:00 起
    assert res.segments[0].start.utcoffset() == timedelta(hours=8)


def test_invalid_timezone_rejected():
    with pytest.raises(AssessmentError, match="时区"):
        get_timezone("Asia/NoSuchCity")


def test_single_reading_insufficient():
    res = assess(CST, D, [(at(4), 50.0)], limit_db=55.0)
    assert not res.sufficient
    assert res.reason == "insufficient_readings"


def test_dst_spring_forward_window_shortened():
    # 美东 2026 年春令时跳变发生在 3 月 9 日 02:00（时钟跳到 03:00），
    # 故 3 月 7 日晚 22:00 -> 3 月 8 日 06:00 的窗口实际只有 7 小时
    ny = ZoneInfo("America/New_York")
    d = date(2026, 3, 7)
    w = night_window(ny, d)
    assert elapsed(w.start, w.end) == timedelta(hours=7)

    # 用 UTC 绝对时刻每 10 分钟一个读数覆盖该窗口
    start = w.start - timedelta(minutes=10)
    end = w.end + timedelta(minutes=10)
    n = round((end - start) / timedelta(minutes=10)) + 1
    readings = [(start + timedelta(minutes=10 * i), 55.0) for i in range(n)]
    res = assess(ny, d, readings, limit_db=55.0)
    assert res.sufficient
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(7 * 3600, abs=1e-6)
    assert res.leq == pytest.approx(55.0, abs=1e-9)


def test_dst_fall_back_window_lengthened():
    # 美东 2026 年秋令时回拨发生在 11-01 02:00（EDT 倒回 EST），
    # 故 10-31 晚 22:00 -> 11-01 06:00 的窗口实际有 9 小时
    from datetime import timezone as _tz

    ny = ZoneInfo("America/New_York")
    d = date(2026, 10, 31)
    w = night_window(ny, d)
    assert elapsed(w.start, w.end) == timedelta(hours=9)

    # 以 UTC 绝对时刻每 10 分钟布点（回拨时同一挂钟会出现两次，绝对时刻不同，属正常）
    start = w.start.astimezone(_tz.utc) - timedelta(minutes=10)
    n = round(elapsed(w.start, w.end).total_seconds() / 600) + 3
    readings = [(start + timedelta(minutes=10 * i), 55.0) for i in range(n)]
    res = assess(ny, d, readings, limit_db=55.0)
    assert res.sufficient
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(9 * 3600, abs=1e-6)
    assert res.leq == pytest.approx(55.0, abs=1e-9)


def test_extreme_level_rejected():
    readings = flat_readings(5000.0)
    with pytest.raises(AssessmentError, match="可计算范围"):
        assess(CST, D, readings, limit_db=55.0)
