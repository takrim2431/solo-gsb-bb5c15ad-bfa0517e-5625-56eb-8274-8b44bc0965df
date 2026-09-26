"""采样补点规划核心逻辑测试。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.assessment import AssessmentError, elapsed, night_window
from app.sampling import plan_sampling

CST = ZoneInfo("Asia/Shanghai")
D = date(2026, 9, 25)
GAP = timedelta(minutes=15)

# at(0) = 北京 22:00（窗口起点，UTC 14:00）；at(8) = 次日北京 06:00（窗口终点）
_BASE = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc)


def at(h: float, tz=timezone.utc) -> datetime:
    return (_BASE + timedelta(hours=h)).astimezone(tz)


def grid(step_min: int = 10):
    """窗口两端各外延一个步长、按绝对时刻等间隔的已采样时刻。"""
    start = at(-step_min / 60)
    end = at(8 + step_min / 60)
    n = round((end - start) / timedelta(minutes=step_min)) + 1
    return [start + timedelta(minutes=step_min * i) for i in range(n)]


def assert_chain_valid(plan_added, existing, tz, night_date):
    """合并已有与新增时刻后：首尾覆盖、窗内相邻间隔均 ≤ 15 分钟（真实秒数）。"""
    window = night_window(tz, night_date)
    merged = sorted(t.astimezone(timezone.utc) for t in [*existing, *plan_added])
    assert merged[0] <= window.start.astimezone(timezone.utc)
    assert merged[-1] >= window.end.astimezone(timezone.utc)
    ws = window.start.astimezone(timezone.utc)
    we = window.end.astimezone(timezone.utc)
    for a, b in zip(merged, merged[1:]):
        if a < we and b > ws:  # 与窗口有正重叠
            assert elapsed(a, b) <= GAP
    # 补点只能落在窗口内
    for t in plan_added:
        assert ws <= t.astimezone(timezone.utc) <= we


def test_empty_samples_fill_whole_window():
    plan = plan_sampling(CST, D, [])
    assert not plan.already_qualified
    # 8 小时窗口：33 个补点（含首尾），间隔恰好 15 分钟
    assert len(plan.added) == 33
    assert plan.added[0] == night_window(CST, D).start
    assert plan.added[-1] == night_window(CST, D).end
    for a, b in zip(plan.added, plan.added[1:]):
        assert elapsed(a, b) == GAP
    # 带当地偏移
    assert plan.added[0].utcoffset() == timedelta(hours=8)


def test_already_qualified_grid():
    plan = plan_sampling(CST, D, grid(10))
    assert plan.already_qualified
    assert plan.added == []


def test_exact_15_min_grid_qualified():
    times = [at(-0.25 + 0.25 * i) for i in range(35)]  # 21:45 .. 06:15
    plan = plan_sampling(CST, D, times)
    assert plan.already_qualified
    assert plan.added == []


def test_head_not_covered_adds_window_start():
    times = [t for t in grid(10) if t > at(0) and t <= at(8)]  # 22:10 .. 06:00
    plan = plan_sampling(CST, D, times)
    assert not plan.already_qualified
    assert plan.added == [night_window(CST, D).start]


def test_tail_not_covered_adds_window_end():
    times = [t for t in grid(10) if t >= at(0) and t < at(8)]  # 22:00 .. 05:50
    plan = plan_sampling(CST, D, times)
    assert not plan.already_qualified
    assert plan.added == [night_window(CST, D).end]


def test_middle_gap_filled_earliest():
    # 22:00 与 22:40 之间 40 分钟空档：最少 2 点，取较早可行时刻 22:10、22:25
    times = [at(0), at(2 / 3), at(8)]
    plan = plan_sampling(CST, D, times)
    assert plan.added[0] == at(1 / 6)   # 22:10
    assert plan.added[1] == at(5 / 12)  # 22:25
    assert_chain_valid(plan.added, times, CST, D)


def test_close_anchors_reused():
    # 窗口外紧邻边界的已有时刻作锚点：21:50 / 06:10
    times = [at(-1 / 6), at(8 + 1 / 6)]
    plan = plan_sampling(CST, D, times)
    assert len(plan.added) == 33
    assert plan.added[0] == at(0)        # 22:00（解耦左锚点）
    assert plan.added[1] == at(1 / 6)    # 22:10（较早可行时刻优先）
    assert plan.added[-1] == at(8 - 1 / 12)  # 05:55
    assert_chain_valid(plan.added, times, CST, D)


def test_far_anchors_disconnected_at_boundaries():
    # 锚点过远（21:00 / 06:20）：在窗口边界补点解耦，窗内等距 15 分钟
    times = [at(-1), at(8 + 1 / 3)]
    plan = plan_sampling(CST, D, times)
    assert len(plan.added) == 33
    assert plan.added[0] == at(0)
    assert plan.added[-1] == at(8)
    for a, b in zip(plan.added, plan.added[1:]):
        assert elapsed(a, b) == GAP
    assert_chain_valid(plan.added, times, CST, D)


def test_samples_outside_window_only_still_fill_window():
    times = [at(-3), at(11)]  # 19:00 与次日 09:00，均远离窗口
    plan = plan_sampling(CST, D, times)
    assert len(plan.added) == 33
    assert_chain_valid(plan.added, times, CST, D)


def test_sparse_samples_minimal_count():
    # 窗内 4 个稀疏时刻（22:00、23:18、01:42、06:00）：各空档最少补点 5 + 9 + 17
    times = [at(0), at(1.3), at(3.7), at(8)]
    plan = plan_sampling(CST, D, times)
    assert len(plan.added) == 31
    assert_chain_valid(plan.added, times, CST, D)


def test_duplicate_timestamp_rejected():
    times = grid(10)
    times[5] = times[4]
    with pytest.raises(AssessmentError) as exc:
        plan_sampling(CST, D, times)
    assert exc.value.code == "duplicate_timestamp"


def test_out_of_order_rejected():
    times = grid(10)
    times[3], times[4] = times[4], times[3]
    with pytest.raises(AssessmentError) as exc:
        plan_sampling(CST, D, times)
    assert exc.value.code == "out_of_order"


def test_naive_timestamp_rejected():
    times = [datetime(2026, 9, 25, 22, 0), datetime(2026, 9, 26, 6, 0)]
    with pytest.raises(AssessmentError) as exc:
        plan_sampling(CST, D, times)
    assert exc.value.code == "naive_timestamp"


def test_samples_in_other_offsets():
    # 已采样时刻以 UTC 给出，补点仍按施工地时区返回
    times = [t.astimezone(timezone.utc) for t in grid(10)]
    plan = plan_sampling(CST, D, times)
    assert plan.already_qualified
    assert plan.added == []


def test_dst_spring_forward_real_seconds():
    # 美东 2026-03-07 晚窗口实际 7 小时：空样本需 29 个补点
    ny = ZoneInfo("America/New_York")
    d = date(2026, 3, 7)
    plan = plan_sampling(ny, d, [])
    assert len(plan.added) == 29
    for a, b in zip(plan.added, plan.added[1:]):
        assert elapsed(a, b) == GAP  # 真实秒数间隔 15 分钟
    # 当地不存在 02:xx（春季跳变），偏移从 -05:00 变为 -04:00
    offsets = {t.utcoffset() for t in plan.added}
    assert offsets == {timedelta(hours=-5), timedelta(hours=-4)}
    assert all(not (t.hour == 2) for t in plan.added)
    assert plan.added[0].isoformat() == "2026-03-07T22:00:00-05:00"
    assert plan.added[-1].isoformat() == "2026-03-08T06:00:00-04:00"


def test_dst_fall_back_real_seconds():
    # 美东 2026-10-31 晚窗口实际 9 小时：空样本需 37 个补点
    ny = ZoneInfo("America/New_York")
    d = date(2026, 10, 31)
    plan = plan_sampling(ny, d, [])
    assert len(plan.added) == 37
    for a, b in zip(plan.added, plan.added[1:]):
        assert elapsed(a, b) == GAP
    # 回拨当晚 01:00 出现两次，偏移不同（-04:00 与 -05:00）
    one_am = [t for t in plan.added if t.hour == 1 and t.minute == 0]
    assert len(one_am) == 2
    assert {t.utcoffset() for t in one_am} == {timedelta(hours=-4), timedelta(hours=-5)}
