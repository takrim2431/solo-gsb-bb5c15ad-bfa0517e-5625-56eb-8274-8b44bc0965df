"""夜间施工噪声核算核心逻辑（与 Web 层解耦，便于测试）。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

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


def normalize_readings(
    raw_readings: list[tuple[datetime, float]],
    *,
    label: str = "读数",
) -> list[Reading] | None:
    """校验并规整单条读数序列：

    - 少于 2 条返回 None（调用方据此给 insufficient_readings 结论）；
    - 时间必须带时区偏移，序列须按绝对时刻严格递增，否则抛 AssessmentError；
    - 内部统一换算为 UTC 后参与比较——Python 的 aware datetime 排序在
      回拨夜混合 tzinfo 时可能出错（fold 歧义），统一时区可规避；
    - label 用于区分总噪声 / 背景噪声序列的错误信息。
    """
    if len(raw_readings) < 2:
        return None

    readings: list[Reading] = []
    for t, level in raw_readings:
        if t.tzinfo is None or t.utcoffset() is None:
            raise AssessmentError(
                "naive_timestamp", f"{label}时间缺少时区偏移：{t.isoformat()}"
            )
        readings.append(Reading(t=t, level=float(level)))

    # 拒绝重复时间与乱序。必须按原始 aware datetime 比较、不能先换算到 UTC：
    # 春令时当晚若用挂钟时间做等差构造，读数会落到不存在的 02:xx（Python 归一
    # 后与下一条得到同一绝对时刻），其挂钟分量不同、属合法的两笔读数；而真正
    # 的重复输入挂钟分量也相同。任何逆序必出现在相邻对中。
    for (raw_t, _), prev, cur in zip(raw_readings[1:], readings, readings[1:]):
        if cur.t == prev.t:
            raise AssessmentError(
                "duplicate_timestamp", f"{label}时间重复：{raw_t.isoformat()}"
            )
        if cur.t < prev.t:
            raise AssessmentError(
                "out_of_order",
                f"{label}时间乱序：{raw_t.isoformat()} 早于前一读数",
            )

    # 校验通过后再统一换算到 UTC：回拨夜混合不同 tzinfo 时 aware datetime 的
    # 直接比较会受 fold 歧义影响，统一时区后一律按绝对时刻处理（含夏令时）。
    return [Reading(t=r.t.astimezone(UTC), level=r.level) for r in readings]


def coverage_reason(readings: list[Reading], window: Window) -> Reason | None:
    """检查单条读数序列对夜间窗口的首尾覆盖与 15 分钟间隔。

    未通过时返回原因代码；通过返回 None。窗口之外（白天）的大间隔不参与判定。
    读数已统一为 UTC，窗口边界同样按 UTC 比较。
    """
    win_start = window.start.astimezone(UTC)
    win_end = window.end.astimezone(UTC)
    if readings[0].t > win_start:
        return "head_not_covered"
    if readings[-1].t < win_end:
        return "tail_not_covered"
    for prev, cur in zip(readings, readings[1:]):
        clip_start = max(prev.t, win_start)
        clip_end = min(cur.t, win_end)
        if clip_end <= clip_start:
            continue  # 该区间与窗口无正重叠
        if elapsed(prev.t, cur.t) > MAX_GAP:
            return "gap_too_large"
    return None


def clipped_segments(readings: list[Reading], window: Window) -> list[Segment]:
    """把相邻读数区间裁剪到夜间窗口，声级以前一读数代表。读数与窗口均按 UTC 比较。"""
    tz = window.start.tzinfo
    win_start = window.start.astimezone(UTC)
    win_end = window.end.astimezone(UTC)
    segments: list[Segment] = []
    for prev, cur in zip(readings, readings[1:]):
        clip_start = max(prev.t, win_start)
        clip_end = min(cur.t, win_end)
        if clip_end <= clip_start:
            continue  # 该区间与窗口无正重叠
        duration = elapsed(clip_start, clip_end).total_seconds()
        segments.append(
            Segment(
                start=clip_start.astimezone(tz),
                end=clip_end.astimezone(tz),
                level_db=prev.level,
                duration_seconds=duration,
            )
        )
    return segments


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

    readings = normalize_readings(raw_readings)
    if readings is None:
        return Assessment(False, window, reason="insufficient_readings")

    # 窗口首尾必须被覆盖（读数区间 [first.t, last.t] 需覆盖整个窗口）
    reason = coverage_reason(readings, window)
    if reason is not None:
        return Assessment(False, window, reason=reason)

    segments = clipped_segments(readings, window)

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
