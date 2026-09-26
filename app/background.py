"""背景噪声扣除核算（POST /background-assess 核心逻辑）。

在同一夜间窗口内分别测得总噪声序列与背景噪声序列：
- 两序列各自满足首尾覆盖窗口、相邻读数间隔 ≤ 15 分钟；
- 按两序列“全部分段边界”合并切段：任一序列出现新读数即开新段，
  每段内两序列的代表声级均取各自前一读数（读数之后才生效）；
- 逐段以声能差扣除背景：E = 10^(总db/10) - 10^(背景db/10)，
  再按各段真实秒数加权求整夜施工声级 Leq；
- 任一段 E ≤ 0（背景读数不低于总读数）均视为该夜无法完成背景扣除，
  不跳过该段，Leq 与超限结论为 null。

所有时刻比较、切段与时长均按绝对时刻（UTC）进行，正确处理夏令时。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from .assessment import (
    AssessmentError,
    Window,
    coverage_reason,
    elapsed,
    night_window,
    normalize_readings,
)

UTC = timezone.utc

# 覆盖 / 间隔类原因与 /assess 共用文案；背景扣除专有原因：
Reason = Literal[
    "head_not_covered",
    "tail_not_covered",
    "gap_too_large",
    "insufficient_readings",
    "incomplete_coverage",
    "background_exceeds_total",
]

REASON_TEXT: dict[str, str] = {
    "head_not_covered": "首个读数晚于夜间窗口起点 22:00，窗口首端未覆盖",
    "tail_not_covered": "末个读数早于夜间窗口终点 06:00，窗口尾端未覆盖",
    "gap_too_large": "夜间窗口内存在相邻读数间隔超过 15 分钟",
    "insufficient_readings": "读数数量不足，无法覆盖夜间窗口",
    "incomplete_coverage": "读数未能完整覆盖整个夜间窗口",
    "background_exceeds_total": "存在背景声级不低于总声级的分段，声能差 E ≤ 0，无法扣除背景",
}


@dataclass(frozen=True)
class BackgroundSegment:
    """合并切段后的一段 [start, end]：两序列各取段首前一读数。"""

    start: datetime
    end: datetime
    duration_seconds: float
    total_db: float        # 该段总噪声代表声级（前一总读数，dBA）
    background_db: float   # 该段背景噪声代表声级（前一背景读数，dBA）
    residual_energy: float | None  # E = 10^(总/10) - 10^(背景/10)；E ≤ 0 时为 None


@dataclass(frozen=True)
class BackgroundAssessment:
    sufficient: bool
    window: Window
    segments: list[BackgroundSegment] = field(default_factory=list)
    reason: Reason | None = None
    leq: float | None = None
    limit_db: float | None = None
    exceeds: bool | None = None
    total_readings_count: int = 0
    background_readings_count: int = 0

    @property
    def reason_text(self) -> str | None:
        return REASON_TEXT[self.reason] if self.reason else None


def _energy(level_db: float) -> float:
    """声能权重 10^(db/10)，数值越界时按 /assess 同款错误拒绝。"""
    try:
        return math.pow(10.0, level_db / 10.0)
    except (OverflowError, ValueError):
        raise AssessmentError(
            "level_out_of_range", "声级数值超出可计算范围，请提供实际 dBA 数值（约 -300~300）"
        )


def _prev_level(times: list[datetime], levels: list[float], seg_start: datetime) -> float:
    """该段的代表声级：不晚于段首 seg_start 的最后一个读数（前一读数）。

    读数在 seg_start 处生效、持续到下一同序列读数，故按段首取
    bisect_right(...)-1；窗口起点恰有读数时取该读数。
    """
    i = bisect.bisect_right(times, seg_start) - 1
    return levels[i]


def assess_background(
    timezone: ZoneInfo,
    night_date,
    total_raw: list[tuple[datetime, float]],
    background_raw: list[tuple[datetime, float]],
    limit_db: float,
) -> BackgroundAssessment:
    """总噪声扣除背景噪声后的整夜施工声级 Leq 核算。"""
    window = night_window(timezone, night_date)

    total = normalize_readings(total_raw, label="总噪声")
    background = normalize_readings(background_raw, label="背景噪声")
    counts = (len(total_raw), len(background_raw))
    if total is None or background is None:
        return BackgroundAssessment(
            False, window, reason="insufficient_readings",
            total_readings_count=counts[0], background_readings_count=counts[1],
        )

    # 两序列各自独立检查首尾覆盖与 15 分钟间隔（先总噪声后背景噪声）
    for readings in (total, background):
        reason = coverage_reason(readings, window)
        if reason is not None:
            return BackgroundAssessment(
                False, window, reason=reason,
                total_readings_count=len(total), background_readings_count=len(background),
            )

    # 合并切段：任一系列落在窗口内部的读数时刻都是切段边界；
    # 同序相邻边界间的代表声级恒定，故取段首即可命中“前一读数”。
    # 读数已统一为 UTC，窗口边界也换算到 UTC（回拨夜混合 tzinfo 直接比较会出错）。
    win_start = window.start.astimezone(UTC)
    win_end = window.end.astimezone(UTC)
    boundaries = sorted(
        {r.t for r in (*total, *background) if win_start < r.t < win_end}
        | {win_start, win_end}
    )

    total_times = [r.t for r in total]
    total_levels = [r.level for r in total]
    bg_times = [r.t for r in background]
    bg_levels = [r.level for r in background]

    segments: list[BackgroundSegment] = []
    any_nonpositive = False
    for b_start, b_end in zip(boundaries, boundaries[1:]):
        duration = elapsed(b_start, b_end).total_seconds()
        if duration <= 0:
            continue
        total_db = _prev_level(total_times, total_levels, b_start)
        background_db = _prev_level(bg_times, bg_levels, b_start)
        e_total = _energy(total_db)
        e_bg = _energy(background_db)
        residual = e_total - e_bg
        # E ≤ 0 不跳过：该段仍原样返回，residual_energy 置空
        if not math.isfinite(residual) or residual <= 0.0:
            residual_value = None
            any_nonpositive = True
        else:
            residual_value = residual
        segments.append(
            BackgroundSegment(
                start=b_start.astimezone(timezone),
                end=b_end.astimezone(timezone),
                duration_seconds=duration,
                total_db=total_db,
                background_db=background_db,
                residual_energy=residual_value,
            )
        )

    covered = sum(s.duration_seconds for s in segments)
    window_duration = elapsed(window.start, window.end).total_seconds()
    if not segments or not math.isclose(covered, window_duration, abs_tol=1.0):
        # 兜底：正常情况下两序列各自首尾覆盖已保证完整覆盖
        return BackgroundAssessment(
            False, window, segments=segments, reason="incomplete_coverage",
            total_readings_count=len(total), background_readings_count=len(background),
        )

    if any_nonpositive:
        return BackgroundAssessment(
            False, window, segments=segments, reason="background_exceeds_total",
            total_readings_count=len(total), background_readings_count=len(background),
        )

    # Leq = 10 lg( Σ Δtᵢ·Eᵢ / Σ Δtᵢ )，Eᵢ 为扣除背景后的声能
    try:
        energy_sum = sum(s.duration_seconds * s.residual_energy for s in segments)
        leq = 10.0 * math.log10(energy_sum / window_duration)
    except (OverflowError, ValueError, ZeroDivisionError):
        raise AssessmentError(
            "level_out_of_range", "声级数值超出可计算范围，请提供实际 dBA 数值（约 -300~300）"
        )
    if not math.isfinite(leq):
        raise AssessmentError(
            "level_out_of_range", "声级数值超出可计算范围，请提供实际 dBA 数值（约 -300~300）"
        )

    return BackgroundAssessment(
        sufficient=True,
        window=window,
        segments=segments,
        leq=leq,
        limit_db=float(limit_db),
        # 以未舍入 Leq 与限值严格比较
        exceeds=leq > float(limit_db),
        total_readings_count=len(total),
        background_readings_count=len(background),
    )
