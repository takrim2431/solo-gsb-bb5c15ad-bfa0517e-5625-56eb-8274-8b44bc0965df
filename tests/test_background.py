"""总噪声扣除背景噪声的核心核算逻辑测试。"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.assessment import AssessmentError, elapsed, night_window
from app.background import assess_background

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


def test_flat_levels_leq():
    res = assess_background(CST, D, flat_readings(60.0), flat_readings(50.0), limit_db=55.0)
    assert res.sufficient
    expected = 10 * math.log10(10**6 - 10**5)  # ≈ 59.5424
    assert res.leq == pytest.approx(expected, abs=1e-9)
    assert res.leq == pytest.approx(59.5424, abs=1e-3)
    assert res.exceeds is True
    # 两组网格一致：对齐分段数与单序列相同，完整铺满 8 小时
    assert len(res.segments) == 48
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(28800, abs=1e-6)
    for s in res.segments:
        assert (s.total_db, s.background_db) == (60.0, 50.0)
        assert s.energy == pytest.approx(10**6 - 10**5)


def test_exceeds_uses_unrounded_leq():
    # 未舍入 Leq ≈ 59.5424：限值取舍入值 59.54 仍判超限（严格大于未舍入值）
    res = assess_background(CST, D, flat_readings(60.0), flat_readings(50.0), limit_db=59.54)
    assert res.exceeds is True
    res = assess_background(CST, D, flat_readings(60.0), flat_readings(50.0), limit_db=59.55)
    assert res.exceeds is False
    # 恰好等于未舍入 Leq 不算超限
    exact = assess_background(CST, D, flat_readings(60.0), flat_readings(50.0), limit_db=0.0)
    res = assess_background(CST, D, flat_readings(60.0), flat_readings(50.0), limit_db=exact.leq)
    assert res.exceeds is False


def test_weighted_leq_two_total_levels():
    # 前半夜总噪声 70、后半夜 50，背景恒定 40
    mid = at(4)
    total = [(t, 70.0 if t >= mid else 50.0) for t, _ in flat_readings(0.0)]
    res = assess_background(CST, D, total, flat_readings(40.0), limit_db=55.0)
    e1 = 10**7 - 10**4  # 前半段扣除背景后的声能权重
    e2 = 10**5 - 10**4  # 后半段
    expected = 10 * math.log10((e1 + e2) / 2)
    assert res.leq == pytest.approx(expected, abs=1e-6)
    assert res.leq == pytest.approx(67.0243, abs=1e-3)


def test_merged_boundaries_take_own_previous_reading():
    # 总噪声 10 分钟网格（22:00 读数 60，22:10 起 70）；
    # 背景 22:00 读数 40、22:07 起 45：合并分界点 22:00、22:07、22:10、22:20、…
    total = [(t, 60.0 if t <= at(0) else 70.0) for t, _ in flat_readings(0.0)]
    bg_times = [at(-1 / 6), at(0), at(7 / 60)] + [at(1 / 3 + i / 6) for i in range(47)]
    bg = [(t, 40.0 if t <= at(0) else 45.0) for t in bg_times]
    res = assess_background(CST, D, total, bg, limit_db=55.0)
    assert res.sufficient
    w = night_window(CST, D)
    assert res.segments[0].start == w.start
    assert res.segments[-1].end == w.end
    assert len(res.segments) == 49  # 总 48 段 + 背景多出的 22:07 分界点
    s1, s2, s3 = res.segments[:3]
    # [22:00, 22:07)：总取 22:00 读数 60，背景取 22:00 读数 40
    assert (s1.total_db, s1.background_db) == (60.0, 40.0)
    assert s1.duration_seconds == pytest.approx(420.0)
    # [22:07, 22:10)：总仍取 22:00 读数 60，背景取 22:07 读数 45
    assert (s2.total_db, s2.background_db) == (60.0, 45.0)
    assert s2.duration_seconds == pytest.approx(180.0)
    # [22:10, 22:20)：总取 22:10 读数 70，背景取 22:07 读数 45
    assert (s3.total_db, s3.background_db) == (70.0, 45.0)
    assert s3.duration_seconds == pytest.approx(600.0)
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(28800, abs=1e-6)


def test_different_grids_merge_all_boundaries():
    # 总 10 分钟网格、背景 15 分钟网格：每小时 8 个对齐分段，整夜 64 段
    res = assess_background(
        CST, D, flat_readings(60.0, 10), flat_readings(50.0, 15), limit_db=55.0
    )
    assert res.sufficient
    assert len(res.segments) == 64
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(28800, abs=1e-6)
    assert res.leq == pytest.approx(10 * math.log10(10**6 - 10**5), abs=1e-9)


def test_background_equal_to_total_segment_not_skipped():
    # 背景在 02:00 的读数等于总噪声：该段 E = 0，无法核算但对齐分段完整返回
    bg = [(t, 55.0 if t == at(4) else 50.0) for t, _ in flat_readings(0.0)]
    res = assess_background(CST, D, flat_readings(55.0), bg, limit_db=55.0)
    assert not res.sufficient
    assert res.reason == "background_not_below_total"
    assert res.leq is None
    assert res.exceeds is None
    # 不跳过问题段：48 段完整铺满窗口，问题段 E = 0
    assert len(res.segments) == 48
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(28800, abs=1e-6)
    bad = [s for s in res.segments if s.energy <= 0]
    assert len(bad) == 1
    assert bad[0].start == at(4).astimezone(CST)
    assert bad[0].energy == 0.0


def test_background_above_total_rejected():
    bg = [(t, 70.0 if t == at(4) else 50.0) for t, _ in flat_readings(0.0)]
    res = assess_background(CST, D, flat_readings(55.0), bg, limit_db=55.0)
    assert not res.sufficient
    assert res.reason == "background_not_below_total"
    bad = [s for s in res.segments if s.energy <= 0]
    assert len(bad) == 1
    assert bad[0].energy < 0


def test_total_coverage_reasons():
    # 总噪声序列自身的覆盖问题：原因带 total_ 前缀
    total = [(t, l) for t, l in flat_readings(55.0) if t > at(0)]  # 首个读数 22:10
    res = assess_background(CST, D, total, flat_readings(50.0), limit_db=55.0)
    assert not res.sufficient
    assert res.reason == "total_head_not_covered"
    assert res.leq is None and res.exceeds is None

    total = [(t, l) for t, l in flat_readings(55.0) if t < at(8)]  # 末个读数 05:50
    res = assess_background(CST, D, total, flat_readings(50.0), limit_db=55.0)
    assert res.reason == "total_tail_not_covered"

    # 02:00–03:00 之间挖空，制造 60 分钟空档
    total = [(t, l) for t, l in flat_readings(55.0) if not (at(2) < t < at(3))]
    res = assess_background(CST, D, total, flat_readings(50.0), limit_db=55.0)
    assert res.reason == "total_gap_too_large"

    res = assess_background(CST, D, [(at(4), 55.0)], flat_readings(50.0), limit_db=55.0)
    assert res.reason == "total_insufficient_readings"


def test_background_coverage_reasons():
    # 背景噪声序列自身的覆盖问题：原因带 background_ 前缀
    bg = [(t, l) for t, l in flat_readings(50.0) if t > at(0)]
    res = assess_background(CST, D, flat_readings(55.0), bg, limit_db=55.0)
    assert res.reason == "background_head_not_covered"

    bg = [(t, l) for t, l in flat_readings(50.0) if t < at(8)]
    res = assess_background(CST, D, flat_readings(55.0), bg, limit_db=55.0)
    assert res.reason == "background_tail_not_covered"

    bg = [(t, l) for t, l in flat_readings(50.0) if not (at(2) < t < at(3))]
    res = assess_background(CST, D, flat_readings(55.0), bg, limit_db=55.0)
    assert res.reason == "background_gap_too_large"

    res = assess_background(CST, D, flat_readings(55.0), [(at(4), 50.0)], limit_db=55.0)
    assert res.reason == "background_insufficient_readings"


def test_duplicate_and_out_of_order_rejected_in_either_series():
    total = flat_readings(55.0)
    total[5] = (total[4][0], 60.0)
    with pytest.raises(AssessmentError, match="重复"):
        assess_background(CST, D, total, flat_readings(50.0), limit_db=55.0)

    bg = flat_readings(50.0)
    bg[3], bg[4] = bg[4], bg[3]
    with pytest.raises(AssessmentError, match="乱序"):
        assess_background(CST, D, flat_readings(55.0), bg, limit_db=55.0)


def test_naive_timestamp_rejected():
    bg = flat_readings(50.0)
    bg[0] = (datetime(2026, 9, 25, 21, 50), 50.0)
    with pytest.raises(AssessmentError, match="时区"):
        assess_background(CST, D, flat_readings(55.0), bg, limit_db=55.0)


def test_extreme_level_rejected():
    with pytest.raises(AssessmentError, match="可计算范围"):
        assess_background(CST, D, flat_readings(5000.0), flat_readings(50.0), limit_db=55.0)
    with pytest.raises(AssessmentError, match="可计算范围"):
        assess_background(CST, D, flat_readings(55.0), flat_readings(5000.0), limit_db=55.0)


def test_readings_in_other_timezone_offsets_work():
    # 总噪声 +09:00、背景 UTC 给出，窗口按上海时区裁剪，绝对时刻一致即可
    tokyo = timezone(timedelta(hours=9))
    total = [(t.astimezone(tokyo), l) for t, l in flat_readings(60.0)]
    bg = [(t.astimezone(timezone.utc), l) for t, l in flat_readings(50.0)]
    res = assess_background(CST, D, total, bg, limit_db=55.0)
    assert res.sufficient
    assert res.leq == pytest.approx(10 * math.log10(10**6 - 10**5), abs=1e-9)
    # 输出的对齐段时间戳换算到施工地时区，从当地 22:00 起
    assert res.segments[0].start.utcoffset() == timedelta(hours=8)


def _utc_grid(window, step_min: int = 10, margin_min: int = 10):
    """以 UTC 绝对时刻等间隔布点，覆盖窗口两端各外延 margin。"""
    start = window.start.astimezone(timezone.utc) - timedelta(minutes=margin_min)
    end = window.end.astimezone(timezone.utc) + timedelta(minutes=margin_min)
    span = (end - start).total_seconds()
    n = math.ceil(span / (step_min * 60))
    return [start + timedelta(minutes=step_min * i) for i in range(n + 1)]


def test_dst_spring_forward_window_shortened():
    # 美东 2026 年春令时跳变发生在 3 月 8 日 02:00，3 月 7 日晚窗口实际 7 小时
    ny = ZoneInfo("America/New_York")
    d = date(2026, 3, 7)
    w = night_window(ny, d)
    assert elapsed(w.start, w.end) == timedelta(hours=7)

    times = _utc_grid(w)
    total = [(t, 65.0) for t in times]
    bg = [(t, 55.0) for t in times]
    res = assess_background(ny, d, total, bg, limit_db=55.0)
    assert res.sufficient
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(7 * 3600, abs=1e-6)
    # Leq = 65 + 10·lg(1 − 10^(−(65−55)/10)) = 65 + 10·lg(0.9)
    assert res.leq == pytest.approx(65 + 10 * math.log10(0.9), abs=1e-9)


def test_dst_fall_back_window_lengthened():
    # 美东 2026 年秋令时回拨发生在 11-01 02:00，10-31 晚窗口实际 9 小时，
    # 同一挂钟 01:xx 以不同偏移各出现一次，合并切段须按绝对时刻排序
    ny = ZoneInfo("America/New_York")
    d = date(2026, 10, 31)
    w = night_window(ny, d)
    assert elapsed(w.start, w.end) == timedelta(hours=9)

    times = _utc_grid(w)
    total = [(t, 65.0) for t in times]
    bg = [(t, 55.0) for t in times]
    res = assess_background(ny, d, total, bg, limit_db=55.0)
    assert res.sufficient
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(9 * 3600, abs=1e-6)
    assert res.leq == pytest.approx(65 + 10 * math.log10(0.9), abs=1e-9)
    # 所有对齐分段时长为正且按绝对时刻递增
    for s in res.segments:
        assert s.duration_seconds > 0
    stamps = [s.start.timestamp() for s in res.segments]
    assert stamps == sorted(stamps)
    # 回拨当晚同一挂钟 01:00 以 -04:00 与 -05:00 各出现一次
    one_am = [s.start for s in res.segments if s.start.hour == 1 and s.start.minute == 0]
    assert len(one_am) == 2
    assert {t.utcoffset() for t in one_am} == {timedelta(hours=-4), timedelta(hours=-5)}


def test_dst_fall_back_mixed_grids_real_seconds():
    # 回拨当晚总/背景不同网格（10 分钟 / 15 分钟）：合并切段仍按真实秒数加权
    ny = ZoneInfo("America/New_York")
    d = date(2026, 10, 31)
    w = night_window(ny, d)
    total = [(t, 60.0) for t in _utc_grid(w, 10)]
    bg = [(t, 50.0) for t in _utc_grid(w, 15)]
    res = assess_background(ny, d, total, bg, limit_db=55.0)
    assert res.sufficient
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(9 * 3600, abs=1e-6)
    assert res.leq == pytest.approx(10 * math.log10(10**6 - 10**5), abs=1e-9)
