"""背景噪声扣除核算的核心逻辑测试。"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.assessment import AssessmentError, elapsed, night_window
from app.background import assess_background

CST = ZoneInfo("Asia/Shanghai")
NY = ZoneInfo("America/New_York")
D = date(2026, 9, 25)

# at(0) = 北京 22:00（窗口起点，UTC 14:00）；at(8) = 次日北京 06:00（窗口终点）
_BASE = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)


def at(h: float, tz=timezone.utc) -> datetime:
    return (_BASE + timedelta(hours=h)).astimezone(tz)


def flat(level: float, step_min: int = 10, tz=CST, night_date: date = D):
    """窗口两端各外延一个步长、按 UTC 绝对时刻等间隔采样的恒声级读数。"""
    w = night_window(tz, night_date)
    start = w.start.astimezone(timezone.utc) - timedelta(minutes=step_min)
    end = w.end.astimezone(timezone.utc) + timedelta(minutes=step_min)
    n = round(elapsed(start, end).total_seconds() / (step_min * 60)) + 1
    return [(start + timedelta(minutes=step_min * i), level) for i in range(n)]


def test_flat_levels_constant_residual():
    # 总 60 / 背景 50：每段声能差恒定，Leq = 10 lg(1e6 - 1e5)
    res = assess_background(CST, D, flat(60.0), flat(50.0), limit_db=55.0)
    assert res.sufficient
    expected = 10 * math.log10(10**6 - 10**5)
    assert res.leq == pytest.approx(expected, abs=1e-9)
    assert res.exceeds is True
    # 同网格合并后仍为 48 段，真实秒数拼满 8 小时
    assert len(res.segments) == 48
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(28800, abs=1e-6)
    s0 = res.segments[0]
    assert s0.total_db == 60.0 and s0.background_db == 50.0
    assert s0.residual_energy == pytest.approx(10**6 - 10**5, rel=1e-12)


def test_segments_merge_on_all_boundaries():
    # 总序列 10 分钟网格；背景序列 21:45 起 15 分钟网格
    bg = [
        (at(-0.25) + timedelta(minutes=15 * i), 50.0)
        for i in range(int(8.5 * 60 / 15) + 1)
    ]
    res = assess_background(CST, D, flat(60.0), bg, limit_db=55.0)
    assert res.sufficient
    # 窗内总网格点 47 + 背景网格点 31 - 重合点 15（每 30 分钟一处）= 63 内部边界 → 64 段
    assert len(res.segments) == 64
    # 段首尾相接、按时间升序
    for prev, cur in zip(res.segments, res.segments[1:]):
        assert prev.end == cur.start
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(28800, abs=1e-6)


def test_segment_takes_each_own_previous_reading():
    # 背景读数在 02:00 由 50 跳到 58：[02:00, 02:10] 段须按段首新值 58
    bg = [(t, 58.0 if t >= at(4) else 50.0) for t, _ in flat(50.0)]
    res = assess_background(CST, D, flat(60.0), bg, limit_db=55.0)
    assert res.sufficient
    seg_before = next(s for s in res.segments if s.end == at(4).astimezone(CST))
    seg_at = next(s for s in res.segments if s.start == at(4).astimezone(CST))
    assert seg_before.background_db == 50.0
    assert seg_at.background_db == 58.0
    assert seg_before.total_db == seg_at.total_db == 60.0


def test_e_non_positive_aborts_without_skipping_segment():
    # 总 == 背景：全部 48 段 E=0，均原样返回、residual_energy 为 null
    res = assess_background(CST, D, flat(55.0), flat(55.0), limit_db=55.0)
    assert not res.sufficient
    assert res.reason == "background_exceeds_total"
    assert res.leq is None and res.exceeds is None and res.limit_db is None
    assert len(res.segments) == 48
    assert all(s.residual_energy is None for s in res.segments)

    # 仅背景在 02:00 后高于总声级：后半段 E<0，前半段仍给出 residual_energy
    bg = [(t, 65.0 if t >= at(4) else 50.0) for t, _ in flat(50.0)]
    res = assess_background(CST, D, flat(60.0), bg, limit_db=55.0)
    assert not res.sufficient
    assert res.reason == "background_exceeds_total"
    assert res.leq is None and res.exceeds is None
    bad = [s for s in res.segments if s.residual_energy is None]
    good = [s for s in res.segments if s.residual_energy is not None]
    assert bad and good
    assert bad[0].start == at(4).astimezone(CST)


def test_exceeds_uses_unrounded_leq():
    # 限值取 round(Leq,2)：舍入值相等，未舍入值仍严格更大 → 超限
    base = assess_background(CST, D, flat(56.0), flat(55.0), limit_db=0.0)
    limit = round(base.leq, 2)
    res = assess_background(CST, D, flat(56.0), flat(55.0), limit_db=limit)
    assert res.sufficient
    assert round(res.leq, 2) == limit
    assert res.leq > limit
    assert res.exceeds is True
    assert assess_background(CST, D, flat(56.0), flat(55.0), limit_db=res.leq).exceeds is False


def test_equal_level_limit_not_exceeding():
    res = assess_background(CST, D, flat(60.0), flat(50.0), limit_db=res_leq(60.0, 50.0))
    assert res.exceeds is False


def res_leq(total_db: float, bg_db: float) -> float:
    return 10 * math.log10(10 ** (total_db / 10) - 10 ** (bg_db / 10))


def test_head_not_covered():
    bg = [(t, l) for t, l in flat(50.0) if t > at(0)]  # 背景首点 22:10
    res = assess_background(CST, D, flat(60.0), bg, limit_db=55.0)
    assert not res.sufficient and res.reason == "head_not_covered"
    assert res.leq is None and res.exceeds is None


def test_tail_not_covered():
    tot = [(t, l) for t, l in flat(60.0) if t < at(8)]  # 总末点 05:50
    res = assess_background(CST, D, tot, flat(50.0), limit_db=55.0)
    assert not res.sufficient and res.reason == "tail_not_covered"
    assert res.leq is None and res.exceeds is None


def test_gap_too_large_in_background_series():
    # 背景序列在窗内制造 16 分钟空档
    bg = flat(50.0, step_min=15)  # 恰好 15 分钟合格
    split = at(4)
    bg = [((t + timedelta(minutes=1), l) if t > split else (t, l)) for t, l in bg]
    res = assess_background(CST, D, flat(60.0, step_min=10), bg, limit_db=55.0)
    assert not res.sufficient and res.reason == "gap_too_large"


def test_exact_15_min_gap_allowed():
    res = assess_background(CST, D, flat(60.0, 15), flat(50.0, 15), limit_db=55.0)
    assert res.sufficient


def test_insufficient_readings():
    res = assess_background(CST, D, [(at(0), 60.0), (at(8), 60.0)], [(at(4), 50.0)], 55.0)
    assert not res.sufficient and res.reason == "insufficient_readings"
    assert res.background_readings_count == 1

    res = assess_background(CST, D, [(at(4), 60.0)], flat(50.0), 55.0)
    assert not res.sufficient and res.reason == "insufficient_readings"
    assert res.total_readings_count == 1


def test_naive_timestamp_rejected_with_series_label():
    with pytest.raises(AssessmentError, match="总噪声.*时区"):
        assess_background(
            CST, D,
            [(datetime(2026, 9, 25, 22, 0), 60.0), (datetime(2026, 9, 26, 6, 0), 60.0)],
            flat(50.0), 55.0,
        )
    with pytest.raises(AssessmentError, match="背景噪声.*时区"):
        assess_background(
            CST, D, flat(60.0),
            [(datetime(2026, 9, 25, 22, 0), 50.0), (datetime(2026, 9, 26, 6, 0), 50.0)],
            55.0,
        )


def test_duplicate_and_out_of_order_rejected():
    dup = flat(60.0)
    dup[5] = (dup[4][0], 61.0)
    with pytest.raises(AssessmentError, match="总噪声.*重复"):
        assess_background(CST, D, dup, flat(50.0), 55.0)

    dup_bg = flat(50.0)
    dup_bg[5] = (dup_bg[4][0], 51.0)
    with pytest.raises(AssessmentError, match="背景噪声.*重复"):
        assess_background(CST, D, flat(60.0), dup_bg, 55.0)

    ooo = flat(60.0)
    ooo[3], ooo[4] = ooo[4], ooo[3]
    with pytest.raises(AssessmentError, match="乱序"):
        assess_background(CST, D, ooo, flat(50.0), 55.0)


def test_dst_fall_back_nine_hours():
    # 美东 2026-11-01 回拨：10-31 晚窗口实际 9 小时，按真实秒数加权
    d = date(2026, 10, 31)
    w = night_window(NY, d)
    assert elapsed(w.start, w.end) == timedelta(hours=9)
    res = assess_background(
        NY, d, flat(60.0, tz=NY, night_date=d), flat(50.0, tz=NY, night_date=d), 55.0
    )
    assert res.sufficient
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(9 * 3600, abs=1e-6)
    assert res.leq == pytest.approx(res_leq(60.0, 50.0), abs=1e-9)
    assert res.window.start.utcoffset() == timedelta(hours=-4)
    assert res.window.end.utcoffset() == timedelta(hours=-5)
    # 回拨当晚同一挂钟时刻以两种偏移各出现一次，切段数多于普通 8 小时夜
    assert len(res.segments) > 48


def test_dst_spring_forward_seven_hours():
    # 美东 2026-03-08 春令时：03-07 晚窗口实际 7 小时
    d = date(2026, 3, 7)
    w = night_window(NY, d)
    assert elapsed(w.start, w.end) == timedelta(hours=7)
    res = assess_background(
        NY, d, flat(60.0, tz=NY, night_date=d), flat(50.0, tz=NY, night_date=d), 55.0
    )
    assert res.sufficient
    assert sum(s.duration_seconds for s in res.segments) == pytest.approx(7 * 3600, abs=1e-6)
    assert res.leq == pytest.approx(res_leq(60.0, 50.0), abs=1e-9)


def test_near_zero_background_matches_total():
    res = assess_background(CST, D, flat(60.0), flat(-300.0), limit_db=60.0)
    assert res.sufficient
    assert res.leq == pytest.approx(60.0, abs=1e-6)
    assert res.exceeds is False


def test_extreme_level_rejected():
    with pytest.raises(AssessmentError, match="可计算范围"):
        assess_background(CST, D, flat(5000.0), flat(40.0), 55.0)
