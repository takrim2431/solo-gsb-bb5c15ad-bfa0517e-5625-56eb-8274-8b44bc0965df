"""请求/响应模型。"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .assessment import Assessment, Segment, Window, elapsed
from .background import BackgroundAssessment, BackgroundSegment


class ReadingIn(BaseModel):
    time: datetime = Field(..., description="读数时刻，ISO 8601 且必须带时区偏移，如 2026-09-25T22:00:00+08:00")
    db: float = Field(..., description="该时刻测得的 A 声级（dBA）")

    @field_validator("time")
    @classmethod
    def _time_must_be_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("时间必须带时区偏移，例如 2026-09-25T22:00:00+08:00")
        return v

    @field_validator("db", mode="before")
    @classmethod
    def _db_must_be_numeric(cls, v):
        if isinstance(v, bool):
            raise ValueError("声级必须是数值，不能是布尔值")
        return v

    @field_validator("db")
    @classmethod
    def _db_must_be_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("声级必须是有限数值，不接受 NaN / Infinity")
        return v


class AssessRequest(BaseModel):
    timezone: str = Field(..., description="施工地 IANA 时区，如 Asia/Shanghai", examples=["Asia/Shanghai"])
    night_date: date = Field(..., description="夜间起始日期（当地日历日，窗口为该日 22:00 至次日 06:00）")
    limit_db: float = Field(..., description="夜间噪声限值（dBA），Leq 严格大于该值判定为超限")
    readings: list[ReadingIn] = Field(..., min_length=1, description="按时间排列的带时分贝读数")

    @field_validator("limit_db", mode="before")
    @classmethod
    def _limit_must_be_numeric(cls, v):
        if isinstance(v, bool):
            raise ValueError("限值必须是数值，不能是布尔值")
        return v

    @field_validator("limit_db")
    @classmethod
    def _limit_must_be_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("限值必须是有限数值")
        return v


class BackgroundAssessRequest(BaseModel):
    timezone: str = Field(..., description="施工地 IANA 时区，如 Asia/Shanghai", examples=["Asia/Shanghai"])
    night_date: date = Field(..., description="夜间起始日期（当地日历日，窗口为该日 22:00 至次日 06:00）")
    limit_db: float = Field(..., description="夜间施工噪声限值（dBA），扣除背景后的 Leq 严格大于该值判定为超限")
    total_readings: list[ReadingIn] = Field(
        ..., min_length=1, description="同一夜间的总噪声读数（含施工声），按时间排列，元素含 time 与 db"
    )
    background_readings: list[ReadingIn] = Field(
        ..., min_length=1, description="同一夜间的背景噪声读数，按时间排列，元素含 time 与 db"
    )

    @field_validator("limit_db", mode="before")
    @classmethod
    def _limit_must_be_numeric(cls, v):
        if isinstance(v, bool):
            raise ValueError("限值必须是数值，不能是布尔值")
        return v

    @field_validator("limit_db")
    @classmethod
    def _limit_must_be_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("限值必须是有限数值")
        return v


class SamplingPlanRequest(BaseModel):
    timezone: str = Field(..., description="施工地 IANA 时区，如 Asia/Shanghai", examples=["Asia/Shanghai"])
    night_date: date = Field(..., description="夜间起始日期（当地日历日，窗口为该日 22:00 至次日 06:00）")
    sampled_times: list[datetime] | None = Field(
        None,
        description="已采样时刻（ISO 8601 且必须带时区偏移，按绝对时刻严格递增）；可为 null 或空数组",
    )

    @field_validator("sampled_times")
    @classmethod
    def _times_must_be_aware(cls, v: list[datetime] | None) -> list[datetime] | None:
        if v is None:
            return v
        for t in v:
            if t.tzinfo is None or t.utcoffset() is None:
                raise ValueError("时间必须带时区偏移，例如 2026-09-25T22:00:00+08:00")
        return v


class SamplingPlanResponse(BaseModel):
    added_times: list[datetime] = Field(..., description="需新增的采样时刻（换算到施工地时区，带当地偏移，按时间升序）")
    added_count: int = Field(..., description="新增采样时刻数量（最少方案）")
    already_qualified: bool = Field(..., description="原已采样时刻是否已合格（无需任何补点）")


class WindowOut(BaseModel):
    start: datetime
    end: datetime
    duration_seconds: float


class SegmentOut(BaseModel):
    start: datetime
    end: datetime
    duration_seconds: float
    level_db: float = Field(..., description="该段代表声级（取段首前一读数，dBA）")
    energy_weight: float = Field(..., description="声能权重 10^(dBA/10)")


class AssessResponse(BaseModel):
    sufficient: bool = Field(..., description="数据是否充分覆盖整个夜间窗口")
    window: WindowOut
    readings_count: int
    segments: list[SegmentOut]
    leq: float | None = Field(None, description="等效声级（dBA，保留两位小数）；数据不足时为 null")
    limit_db: float | None = None
    exceeds: bool | None = Field(None, description="Leq 是否严格超过限值；数据不足时为 null")
    conclusion: str | None = Field(None, description="结论：超限 / 未超限 / 数据不足")
    reason_code: str | None = Field(None, description="数据不足原因代码")
    reason: str | None = Field(None, description="数据不足原因说明")

    @classmethod
    def from_assessment(cls, result: Assessment, readings_count: int) -> "AssessResponse":
        window = result.window
        segments = [
            SegmentOut(
                start=s.start,
                end=s.end,
                duration_seconds=round(s.duration_seconds, 3),
                level_db=s.level_db,
                energy_weight=10.0 ** (s.level_db / 10.0),
            )
            for s in result.segments
        ]
        if not result.sufficient:
            return cls(
                sufficient=False,
                window=WindowOut(
                    start=window.start,
                    end=window.end,
                    duration_seconds=elapsed(window.start, window.end).total_seconds(),
                ),
                readings_count=readings_count,
                segments=segments,
                conclusion="数据不足",
                reason_code=result.reason,
                reason=result.reason_text,
            )
        return cls(
            sufficient=True,
            window=WindowOut(
                start=window.start,
                end=window.end,
                duration_seconds=elapsed(window.start, window.end).total_seconds(),
            ),
            readings_count=readings_count,
            segments=segments,
            leq=round(result.leq, 2),  # type: ignore[arg-type]
            limit_db=result.limit_db,
            exceeds=result.exceeds,
            conclusion="超限" if result.exceeds else "未超限",
        )


class BackgroundSegmentOut(BaseModel):
    start: datetime
    end: datetime
    duration_seconds: float
    total_db: float = Field(..., description="该段总噪声代表声级（取段首前一总读数，dBA）")
    background_db: float = Field(..., description="该段背景噪声代表声级（取段首前一背景读数，dBA）")
    residual_energy: float | None = Field(
        ..., description="扣除背景后的声能 E=10^(总db/10)-10^(背景db/10)；E≤0 时为 null，该段不跳过"
    )


class BackgroundAssessResponse(BaseModel):
    sufficient: bool = Field(..., description="数据是否充分并成功完成背景扣除")
    window: WindowOut
    total_readings_count: int
    background_readings_count: int
    segments: list[BackgroundSegmentOut] = Field(
        ..., description="按两序列全部分段边界对齐合并后的切段"
    )
    leq: float | None = Field(None, description="扣除背景后的整夜施工声级 Leq（dBA，保留两位小数）；无法计算时为 null")
    limit_db: float | None = None
    exceeds: bool | None = Field(None, description="未舍入 Leq 是否严格超过限值；无法计算时为 null")
    conclusion: str | None = Field(None, description="结论：超限 / 未超限 / 数据不足")
    reason_code: str | None = Field(None, description="无法核算原因代码")
    reason: str | None = Field(None, description="无法核算原因说明")

    @classmethod
    def from_assessment(cls, result: BackgroundAssessment) -> "BackgroundAssessResponse":
        window = result.window
        segments = [
            BackgroundSegmentOut(
                start=s.start,
                end=s.end,
                duration_seconds=round(s.duration_seconds, 3),
                total_db=s.total_db,
                background_db=s.background_db,
                residual_energy=s.residual_energy,
            )
            for s in result.segments
        ]
        window_out = WindowOut(
            start=window.start,
            end=window.end,
            duration_seconds=elapsed(window.start, window.end).total_seconds(),
        )
        common = dict(
            window=window_out,
            total_readings_count=result.total_readings_count,
            background_readings_count=result.background_readings_count,
            segments=segments,
        )
        if not result.sufficient:
            return cls(
                sufficient=False,
                **common,
                conclusion="数据不足",
                reason_code=result.reason,
                reason=result.reason_text,
            )
        return cls(
            sufficient=True,
            **common,
            leq=round(result.leq, 2),  # type: ignore[arg-type]
            limit_db=result.limit_db,
            exceeds=result.exceeds,
            conclusion="超限" if result.exceeds else "未超限",
        )
