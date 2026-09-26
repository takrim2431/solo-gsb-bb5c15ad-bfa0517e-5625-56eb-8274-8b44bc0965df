"""夜间窗口最少补点规划核心逻辑（与 Web 层解耦，便于测试）。

规则概述：
- 窗口为当地 22:00 至次日 06:00（夏令时跳变按真实秒数处理）；
- 已采样时刻须带偏移且按绝对时刻严格递增；
- 生成最少数量的新增采样时刻，使得：
  * 窗口首尾被覆盖（首端存在 ≤ 窗口起点的时刻，尾端存在 ≥ 窗口终点的时刻）；
  * 所有与窗口有正重叠的相邻采样间隔 ≤ 15 分钟；
- 窗口外仅取紧邻边界的已有时刻作锚点，补点只能落在窗口内（含边界）；
- 多个最少方案并存时，取落点字典序最早（时刻尽量早）的方案。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .assessment import MAX_GAP, AssessmentError, night_window

UTC = timezone.utc
GAP = MAX_GAP  # 相邻采样最大允许间隔（15 分钟）


@dataclass(frozen=True)
class SamplingPlan:
    """补点规划结果。"""

    added: list[datetime]          # 需新增的采样时刻（已换算到施工地时区）
    already_qualified: bool        # 原已采样时刻是否已合格（无需任何补点）


def _fill_gap(a: datetime, b: datetime, ws: datetime, we: datetime) -> list[datetime]:
    """受约束相邻时刻对 (a, b) 内的最少补点（UTC 绝对时刻，a < b 且间隔 > GAP）。

    补点必须落在 [ws, we] 内。窗口边界上的点可使边界之外的间隔与窗口
    零重叠（不再受 15 分钟约束），据此比较各候选方案，取点数最少、
    同为最少时落点最早（字典序）的方案。
    """
    if b - a <= GAP:
        return []
    # 不借助窗口边界解耦时的最少点数；从 b 向前等距排布即落点最早的排法
    k = -(-(b - a) // GAP) - 1
    candidates: list[list[datetime]] = []
    if b - k * GAP >= ws and b - GAP <= we:
        candidates.append([b - i * GAP for i in range(k, 0, -1)])
    if a < ws:  # 在窗口起点补一点：左侧间隔与窗口零重叠，不再受约束
        candidates.append([ws] + _fill_gap(ws, b, ws, we))
    if b > we:  # 在窗口终点补一点：右侧间隔与窗口零重叠，不再受约束
        candidates.append(_fill_gap(a, we, ws, we) + [we])
    return min(candidates, key=lambda c: (len(c), c))


def plan_sampling(
    timezone: ZoneInfo,
    night_date: date,
    sampled_times: list[datetime],
) -> SamplingPlan:
    """生成夜间窗口的最少补点计划。

    已采样时刻以绝对时刻（带偏移量的 datetime）给出，可处于任意时区，
    内部统一换算到 UTC 按真实秒数计算，返回的补点换算回施工地时区。
    """
    window = night_window(timezone, night_date)
    ws = window.start.astimezone(UTC)
    we = window.end.astimezone(UTC)

    raw = list(sampled_times)
    for t in raw:
        if t.tzinfo is None or t.utcoffset() is None:
            raise AssessmentError("naive_timestamp", f"采样时间缺少时区偏移：{t.isoformat()}")

    # 拒绝重复时间与乱序（相邻比较即可覆盖：任何逆序/重复必出现在相邻对中）
    for prev, cur in zip(raw, raw[1:]):
        if cur == prev:
            raise AssessmentError(
                "duplicate_timestamp", f"采样时间重复：{cur.isoformat()}"
            )
        if cur < prev:
            raise AssessmentError(
                "out_of_order",
                f"采样时间乱序：{cur.isoformat()} 早于前一采样时刻 {prev.isoformat()}",
            )

    times = [t.astimezone(UTC) for t in raw]
    left = [t for t in times if t < ws]
    inside = [t for t in times if ws <= t <= we]
    right = [t for t in times if t > we]

    # 固定时刻序列：左锚点 + 窗口内已有时刻 + 右锚点，首尾未覆盖时在窗口边界补点
    fixed: list[datetime] = []
    forced: list[datetime] = []
    if left:
        fixed.append(left[-1])  # 窗口外仅取紧邻边界的已有时刻作锚点
    if not left and not (inside and inside[0] == ws):
        fixed.append(ws)  # 窗口首端未覆盖：补点只能落在窗口内，即补在起点
        forced.append(ws)
    fixed.extend(inside)
    if not right and not (inside and inside[-1] == we):
        fixed.append(we)  # 窗口尾端未覆盖：补在终点
        forced.append(we)
    if right:
        fixed.append(right[0])

    added = list(forced)
    for a, b in zip(fixed, fixed[1:]):
        if a < we and b > ws:  # 仅处理与窗口有正重叠的相邻间隔
            added.extend(_fill_gap(a, b, ws, we))
    added.sort()

    return SamplingPlan(
        added=[t.astimezone(timezone) for t in added],
        already_qualified=not added,
    )
