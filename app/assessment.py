"""夜间施工噪声核算核心逻辑（与 Web 层解耦，便于测试）。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NIGHT_START = time(22, 0)       # 当地夜间起始 22:00
NIGHT_END = time(6, 0)          # 当地夜间结束 次日 06:00
MAX_GAP = timedelta(minutes=15)  # 相邻读数最大允许间隔（超过即数据不足）

Reason = Literal[
    "head_not_covered",
    "tail_not_covered",
    "gap_too_large",
    "insufficient_readings",
    "incomplete_coverage",
]

REASON_TEXT = {
    "head_not_covered": "首个读数晚于夜间窗口起点 22:00，窗口首端未覆盖",
    "tail_not_covered": "末个读数早于夜间窗口终点 06:00，窗口尾端未覆盖",
    "gap_too_large": "夜间窗口内存在相邻读数间隔超过 15 分钟",
    "insufficient_readings": "读数数量不足，无法覆盖夜间窗口",
    "incomplete_coverage": "读数未能完整覆盖整个夜间窗口",
}


class AssessmentError(ValueError):
    """请求数据语义不合法（区别于“数据不足”的业务结论）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def elapsed(a: datetime, b: datetime) -> timedelta:
    """两个 aware datetime 之间的真实时长（按绝对时刻，正确处理夏令时）。

    注意：Python 中 aware datetime 直接相减只按挂钟分量计算、
    不应用 UTC 偏移，跨 DST 或跨不同偏移时结果是错的，故统一用本函数。
    """
    return timedelta(seconds=b.timestamp() - a.timestamp())


@dataclass(frozen=True)
class Reading:
    t: datetime       # 必须带时区信息
    level: float      # dBA


@dataclass(frozen=True)
class Segment:
    """夜间窗口内被裁剪后的一段：[start, end]，声级以前一读数代表。"""

    start: datetime
    end: datetime
    level_db: float
    duration_seconds: float


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Assessment:
    sufficient: bool
    window: Window
    segments: list[Segment] = field(default_factory=list)
    reason: Reason | None = None
    leq: float | None = None
    limit_db: float | None = None
    exceeds: bool | None = None

    @property
    def reason_text(self) -> str | None:
        return REASON_TEXT[self.reason] if self.reason else None


def get_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise AssessmentError("invalid_timezone", f"无法识别的 IANA 时区：{name!r}")


def night_window(timezone: ZoneInfo, night_date: date) -> Window:
    """当地 22:00 至次日 06:00 的评估窗口（正确处理夏令时跳变）。"""
    start = datetime.combine(night_date, NIGHT_START, tzinfo=timezone)
    end = datetime.combine(night_date + timedelta(days=1), NIGHT_END, tzinfo=timezone)
    return Window(start=start, end=end)


def assess(
    timezone: ZoneInfo,
    night_date: date,
    raw_readings: list[tuple[datetime, float]],
    limit_db: float,
) -> Assessment:
    """按规范核算夜间等效声级。

    读数以绝对时刻（带偏移量的 datetime）给出，可处于任意时区，
    内部统一按绝对时刻参与裁剪与比较。
    """
    window = night_window(timezone, night_date)

    if len(raw_readings) < 2:
        return Assessment(False, window, reason="insufficient_readings")

    readings: list[Reading] = []
    for t, level in raw_readings:
        if t.tzinfo is None or t.utcoffset() is None:
            raise AssessmentError("naive_timestamp", f"读数时间缺少时区偏移：{t.isoformat()}")
        readings.append(Reading(t=t, level=float(level)))

    # 拒绝重复时间与乱序（相邻比较即可覆盖：任何逆序/重复必出现在相邻对中）
    for prev, cur in zip(readings, readings[1:]):
        if cur.t == prev.t:
            raise AssessmentError(
                "duplicate_timestamp", f"读数时间重复：{cur.t.isoformat()}"
            )
        if cur.t < prev.t:
            raise AssessmentError(
                "out_of_order",
                f"读数时间乱序：{cur.t.isoformat()} 早于前一读数 {prev.t.isoformat()}",
            )

    first, last = readings[0], readings[-1]

    # 窗口首尾必须被覆盖（读数区间 [first.t, last.t] 需覆盖整个窗口）
    if first.t > window.start:
        return Assessment(False, window, reason="head_not_covered")
    if last.t < window.end:
        return Assessment(False, window, reason="tail_not_covered")

    segments: list[Segment] = []
    for prev, cur in zip(readings, readings[1:]):
        clip_start = max(prev.t, window.start)
        clip_end = min(cur.t, window.end)
        if clip_end <= clip_start:
            continue  # 该区间与窗口无正重叠
        gap = elapsed(prev.t, cur.t)
        if gap > MAX_GAP:
            return Assessment(False, window, reason="gap_too_large")
        duration = elapsed(clip_start, clip_end).total_seconds()
        segments.append(
            Segment(
                start=clip_start.astimezone(timezone),
                end=clip_end.astimezone(timezone),
                level_db=prev.level,
                duration_seconds=duration,
            )
        )

    covered = sum(s.duration_seconds for s in segments)
    window_duration = elapsed(window.start, window.end).total_seconds()
    if not segments or not math.isclose(covered, window_duration, abs_tol=1.0):
        # 兜底：正常情况下首尾覆盖已保证完整覆盖
        return Assessment(False, window, segments=segments, reason="incomplete_coverage")

    # 能量法等效声级：Leq = 10 lg( Σ wᵈ 10^(Lᵢ/10) / Σ wᵈ )
    try:
        energy_sum = sum(
            seg.duration_seconds * math.pow(10.0, seg.level_db / 10.0) for seg in segments
        )
        leq = 10.0 * math.log10(energy_sum / window_duration)
    except (OverflowError, ValueError, ZeroDivisionError):
        raise AssessmentError(
            "level_out_of_range", "声级数值超出可计算范围，请提供实际 dBA 数值（约 -300~300）"
        )

    if not math.isfinite(leq):
        raise AssessmentError(
            "level_out_of_range", "声级数值超出可计算范围，请提供实际 dBA 数值（约 -300~300）"
        )

    return Assessment(
        sufficient=True,
        window=window,
        segments=segments,
        leq=leq,
        limit_db=float(limit_db),
        exceeds=leq > limit_db,
    )
