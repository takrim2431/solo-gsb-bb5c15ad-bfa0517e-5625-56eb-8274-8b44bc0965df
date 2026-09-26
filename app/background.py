"""夜间施工声级核算：同一夜间的总噪声扣除背景噪声（与 Web 层解耦，便于测试）。

规则概述：
- 夜间窗口、读数校验与覆盖充分性判定复用 assessment 模块（首尾覆盖 + 15 分钟间隔）；
- 总噪声与背景噪声两组读数分别独立校验，各自完整覆盖同一夜间窗口；
- 按两组分段的全部分界点合并切段，每段的总/背景声级各取各自序列的前一读数；
- 每段扣除背景：E = 10^(L总/10) − 10^(L背/10)，再按真实秒数加权求整夜施工 Leq：
  Leq = 10 · lg( Σ Δtᵢ·Eᵢ / Σ Δtᵢ )，以未舍入值严格大于限值判超限；
- 覆盖不足或任一段 E ≤ 0（背景不低于总噪声）时给出具体原因，Leq 与超限结论为
  None，但对齐分段仍完整返回（不跳过问题段）；
- 一切时长按绝对时刻（真实秒数）计算，正确处理夏令时。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from .assessment import AssessmentError, Window, check_coverage, elapsed, night_window

BackgroundReason = Literal[
    "total_insufficient_readings",
    "total_head_not_covered",
    "total_tail_not_covered",
    "total_gap_too_large",
    "total_incomplete_coverage",
    "background_insufficient_readings",
    "background_head_not_covered",
    "background_tail_not_covered",
    "background_gap_too_large",
    "background_incomplete_coverage",
    "background_not_below_total",
]

REASON_TEXT = {
    "total_insufficient_readings": "总噪声读数数量不足，无法覆盖夜间窗口",
    "total_head_not_covered": "总噪声首个读数晚于夜间窗口起点 22:00，窗口首端未覆盖",
    "total_tail_not_covered": "总噪声末个读数早于夜间窗口终点 06:00，窗口尾端未覆盖",
    "total_gap_too_large": "总噪声读数在夜间窗口内存在相邻间隔超过 15 分钟",
    "total_incomplete_coverage": "总噪声读数未能完整覆盖整个夜间窗口",
    "background_insufficient_readings": "背景噪声读数数量不足，无法覆盖夜间窗口",
    "background_head_not_covered": "背景噪声首个读数晚于夜间窗口起点 22:00，窗口首端未覆盖",
    "background_tail_not_covered": "背景噪声末个读数早于夜间窗口终点 06:00，窗口尾端未覆盖",
    "background_gap_too_large": "背景噪声读数在夜间窗口内存在相邻间隔超过 15 分钟",
    "background_incomplete_coverage": "背景噪声读数未能完整覆盖整个夜间窗口",
    "background_not_below_total": "存在背景噪声不低于总噪声的对齐分段，扣除后声能不为正，无法核算施工声级",
}

_LEVEL_OUT_OF_RANGE = "声级数值超出可计算范围，请提供实际 dBA 数值（约 -300~300）"


@dataclass(frozen=True)
class AlignedSegment:
    """两组分段按全部分界点对齐后的一段：总/背景声级各取各自序列的前一读数。"""

    start: datetime
    end: datetime
    duration_seconds: float
    total_db: float       # 该段总噪声代表声级（dBA）
    background_db: float  # 该段背景噪声代表声级（dBA）
    energy: float         # 扣除背景后的施工声能权重 E = 10^(总/10) − 10^(背/10)


@dataclass(frozen=True)
class BackgroundAssessment:
    sufficient: bool
    window: Window
    segments: list[AlignedSegment] = field(default_factory=list)
    reason: BackgroundReason | None = None
    leq: float | None = None
    limit_db: float | None = None
    exceeds: bool | None = None

    @property
    def reason_text(self) -> str | None:
        return REASON_TEXT[self.reason] if self.reason else None


def _segment_energy(total_db: float, background_db: float) -> float:
    """单段扣除背景后的施工声能权重 E = 10^(总/10) − 10^(背/10)。"""
    try:
        return math.pow(10.0, total_db / 10.0) - math.pow(10.0, background_db / 10.0)
    except OverflowError:
        raise AssessmentError("level_out_of_range", _LEVEL_OUT_OF_RANGE)


def assess_background(
    timezone: ZoneInfo,
    night_date: date,
    total_readings: list[tuple[datetime, float]],
    background_readings: list[tuple[datetime, float]],
    limit_db: float,
) -> BackgroundAssessment:
    """用同一夜间的总噪声与背景噪声读数核算施工声级 Leq。

    两组读数均以绝对时刻（带偏移量的 datetime）给出，可处于任意时区；
    分别独立校验覆盖充分性后，按两者全部分段边界合并切段、逐段扣除背景。
    """
    window = night_window(timezone, night_date)

    total_segments, total_reason = check_coverage(total_readings, window, timezone)
    if total_reason is not None:
        return BackgroundAssessment(False, window, reason=f"total_{total_reason}")  # type: ignore[arg-type]
    bg_segments, bg_reason = check_coverage(background_readings, window, timezone)
    if bg_reason is not None:
        return BackgroundAssessment(False, window, reason=f"background_{bg_reason}")  # type: ignore[arg-type]

    # 两组分段各自连续铺满窗口；取两者全部分界点合并切段，
    # 每个子段必完整落在两组各自的某一段内（双指针定位，各取各自前一读数）。
    # 注意：分界点均带施工地 ZoneInfo，同一 tzinfo 的 aware datetime 比较/排序
    # 只按挂钟分量（夏令时回拨当晚同一挂钟出现两次会交错出错），
    # 故一律按绝对时刻（timestamp，真实秒数）排序、定位与计算时长。
    by_instant: dict[float, datetime] = {}
    for seg in (*total_segments, *bg_segments):
        by_instant.setdefault(seg.start.timestamp(), seg.start)
        by_instant.setdefault(seg.end.timestamp(), seg.end)
    cuts = sorted(by_instant)

    t_ends = [seg.end.timestamp() for seg in total_segments]
    b_ends = [seg.end.timestamp() for seg in bg_segments]
    aligned: list[AlignedSegment] = []
    ti = bi = 0
    for a_ts, b_ts in zip(cuts, cuts[1:]):
        while t_ends[ti] <= a_ts:
            ti += 1
        while b_ends[bi] <= a_ts:
            bi += 1
        t_seg, b_seg = total_segments[ti], bg_segments[bi]
        aligned.append(
            AlignedSegment(
                start=by_instant[a_ts],
                end=by_instant[b_ts],
                duration_seconds=b_ts - a_ts,
                total_db=t_seg.level_db,
                background_db=b_seg.level_db,
                energy=_segment_energy(t_seg.level_db, b_seg.level_db),
            )
        )

    # 任一段扣除后声能不为正：无法核算，但对齐分段完整返回（不跳过问题段）
    if any(seg.energy <= 0 for seg in aligned):
        return BackgroundAssessment(
            False, window, segments=aligned, reason="background_not_below_total"
        )

    # 能量法等效声级：Leq = 10 lg( Σ Δtᵢ·Eᵢ / Σ Δtᵢ )
    window_duration = elapsed(window.start, window.end).total_seconds()
    try:
        energy_sum = sum(seg.duration_seconds * seg.energy for seg in aligned)
        leq = 10.0 * math.log10(energy_sum / window_duration)
    except (OverflowError, ValueError, ZeroDivisionError):
        raise AssessmentError("level_out_of_range", _LEVEL_OUT_OF_RANGE)

    if not math.isfinite(leq):
        raise AssessmentError("level_out_of_range", _LEVEL_OUT_OF_RANGE)

    return BackgroundAssessment(
        sufficient=True,
        window=window,
        segments=aligned,
        leq=leq,
        limit_db=float(limit_db),
        exceeds=leq > limit_db,
    )
