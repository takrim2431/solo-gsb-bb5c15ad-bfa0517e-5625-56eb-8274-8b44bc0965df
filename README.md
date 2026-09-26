# 夜间施工噪声核算 API

基于 Python + FastAPI 的夜间施工噪声等效声级（Leq）核算与采样补点规划服务。

按施工当地 **22:00 至次日 06:00** 的夜间窗口，对相邻带时分贝读数覆盖的时段进行裁剪；
每段以**前一读数**为代表声级，按持续秒数对声能 10^(dBA/10) 加权，计算时间加权等效声级：

```
Leq = 10 · lg( Σ (Δtᵢ · 10^(Lᵢ/10)) / Σ Δtᵢ )
```

并与噪声限值比较，给出 **超限 / 未超限 / 数据不足** 的结论。

`/background-assess` 在此基础上接收**同一夜间**的总噪声与背景噪声两组读数，
按两序列的全部分段边界合并切段，逐段以声能差
`E = 10^(总db/10) − 10^(背景db/10)` 扣除背景，再按真实秒数加权计算施工声级整夜 Leq。

此外提供采样补点规划接口：在已有采样时刻的基础上，为夜间窗口生成**最少数量**的新增采样时刻，
使窗口首尾被覆盖、且所有与窗口有正重叠的相邻采样间隔不超过 15 分钟。

---

## 一条命令启动

需要本机已安装 Docker（含 Compose 插件 v2）：

```bash
docker compose up -d --build
```

服务随后监听在 **http://localhost:8000**。

停止：

```bash
docker compose down
```

> 无需 Docker 时也可本地运行：`pip install -r requirements.txt`，
> 然后 `uvicorn app.main:app --host 0.0.0.0 --port 8000`。

## 必要配置

| 配置项 | 位置 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `HOST_PORT` | `docker compose up` 的环境变量 | `8000` | 宿主机映射端口，如 8080：`HOST_PORT=8080 docker compose up -d` |
| `PORT` | compose 的 `environment` | `8000` | 容器内监听端口（一般无需修改） |

容器内已内置健康检查，镜像基于 `python:3.12-slim`，无需挂载任何卷。

## 接口地址与端口

- 服务地址：`http://localhost:8000`（使用 `HOST_PORT` 时替换端口）
- 健康检查：`GET http://localhost:8000/health`
- 噪声核算：`POST http://localhost:8000/assess`
- 背景扣除核算：`POST http://localhost:8000/background-assess`
- 采样补点规划：`POST http://localhost:8000/sampling-plan`
- 交互文档（Swagger UI）：`http://localhost:8000/docs`
- OpenAPI 描述：`http://localhost:8000/openapi.json`

## 请求

`POST /assess`，`Content-Type: application/json`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `timezone` | string | 施工地 **IANA 时区**，如 `Asia/Shanghai`、`America/New_York` |
| `night_date` | string | 夜间**起始日期**（`YYYY-MM-DD`，当地日历日），窗口为该日 22:00 至次日 06:00 |
| `limit_db` | number | 夜间噪声限值（dBA）；Leq **严格大于**该值判定为超限（等于不算超限） |
| `readings` | array | 按时间排列的读数，至少 1 条；元素含 `time` 与 `db` |
| `readings[].time` | string | ISO 8601 时间，**必须带时区偏移**，如 `2026-09-25T22:00:00+08:00`（可用任意偏移，内部按绝对时刻统一计算） |
| `readings[].db` | number | 该时刻 A 声级，单位 dBA |

### 数据充分性规则（返回“数据不足”的条件）

- `head_not_covered`：首个读数晚于窗口起点 22:00（窗口**首端**未覆盖）；
- `tail_not_covered`：末个读数早于窗口终点 06:00（窗口**尾端**未覆盖）；
- `gap_too_large`：与夜间窗口相交的相邻读数区间中，存在间隔 **> 15 分钟**（恰好 15 分钟允许）；
- `insufficient_readings`：读数少于 2 条；
- `incomplete_coverage`：兜底情况，裁剪后总时长不足整个窗口（正常输入不会出现）。

窗口之外（白天）的大间隔不参与判定。首尾读数恰好落在 22:00 / 06:00 视为覆盖。

#### 生成一份完整读数

窗口为 8 小时，只要首读数 ≤ 22:00、末读数 ≥ 06:00，且窗口内相邻间隔 ≤ 15 分钟即可。
例如用 Python 生成 21:50–06:10 每 10 分钟一条的恒声级数据：

```bash
curl -X POST http://localhost:8000/assess \
  -H 'Content-Type: application/json' \
  -d "$(python3 - <<'PY'
import json
from datetime import datetime, timedelta, timezone
start = datetime(2026, 9, 25, 13, 50, tzinfo=timezone.utc)  # 北京 21:50
readings = [
    {"time": (start + timedelta(minutes=10*i)).isoformat(), "db": 57}
    for i in range(51)   # 至北京次日 06:10
]
print(json.dumps({
    "timezone": "Asia/Shanghai",
    "night_date": "2026-09-25",
    "limit_db": 55,
    "readings": readings,
}))
PY
)"
```

恒声级 57 dBA 时返回 `"leq": 57.0, "exceeds": true, "conclusion": "超限"`。

### 数据合法性规则（HTTP 400，直接拒绝）

- 读数时间**乱序**（后一条早于前一条）：错误码 `out_of_order`；
- 读数时间**重复**：错误码 `duplicate_timestamp`；
- 声级为**非数值**（字符串、布尔值、NaN / Infinity）：错误码 `invalid_request`；
- 读数时间不带时区偏移、请求体不是合法 JSON 或缺少必填字段：错误码 `invalid_request`；
- IANA 时区无法识别：错误码 `invalid_timezone`。

## 响应

数据充分时 HTTP 200：

```json
{
  "sufficient": true,
  "window": {
    "start": "2026-09-25T22:00:00+08:00",
    "end": "2026-09-26T06:00:00+08:00",
    "duration_seconds": 28800.0
  },
  "readings_count": 51,
  "segments": [
    {
      "start": "2026-09-25T22:00:00+08:00",
      "end": "2026-09-25T22:10:00+08:00",
      "duration_seconds": 600.0,
      "level_db": 57.0,
      "energy_weight": 501187.23
    }
  ],
  "leq": 57.0,
  "limit_db": 55.0,
  "exceeds": true,
  "conclusion": "超限"
}
```

数据不足时同样返回 HTTP 200（属于业务结论而非请求错误），`sufficient=false`，
`leq` / `exceeds` 为 `null`，并给出 `reason_code` 与中文 `reason`，`conclusion` 为 `"数据不足"`：

```json
{
  "sufficient": false,
  "window": { "start": "...", "end": "...", "duration_seconds": 28800.0 },
  "readings_count": 3,
  "segments": [ ],
  "leq": null,
  "limit_db": null,
  "exceeds": null,
  "conclusion": "数据不足",
  "reason_code": "gap_too_large",
  "reason": "夜间窗口内存在相邻读数间隔超过 15 分钟"
}
```

请求不合法时 HTTP 400：

```json
{ "error": { "code": "out_of_order", "message": "读数时间乱序：..." } }
```

## 背景噪声扣除核算（POST /background-assess）

同一夜间分别测得**总噪声**与**背景噪声**两组读数，先逐段扣除背景声能，再按真实秒数加权求整夜施工声级：

```
Eᵢ = 10^(L总,i/10) − 10^(L背,i/10)
Leq = 10 · lg( Σ (Δtᵢ · Eᵢ) / Σ Δtᵢ )
```

夜间窗口、首尾覆盖与 15 分钟间隔约束、输入校验规则均与 `/assess` 一致，且**两组序列分别独立满足**。

### 请求

`POST /background-assess`，`Content-Type: application/json`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `timezone` | string | 施工地 **IANA 时区**，如 `Asia/Shanghai`、`America/New_York` |
| `night_date` | string | 夜间**起始日期**（`YYYY-MM-DD`，当地日历日），窗口为该日 22:00 至次日 06:00 |
| `limit_db` | number | 夜间施工噪声限值（dBA）；**未舍入** Leq **严格大于**该值判定为超限 |
| `total_readings` | array | 同一夜间的**总噪声**读数（含施工声），至少 1 条，按时间排列；元素含 `time` 与 `db` |
| `background_readings` | array | 同一夜间的**背景噪声**读数，至少 1 条，按时间排列；元素含 `time` 与 `db` |
| `*.time` | string | ISO 8601 时间，**必须带时区偏移**（两序列可用不同偏移，内部按绝对时刻统一计算） |
| `*.db` | number | 该时刻 A 声级（dBA），有限数值 |

### 切段与扣除规则

- **合并切段**：以两组序列“全部分段边界”（落在窗口内的全部读数时刻，加窗口首尾）的并集切段；
  每一段内两组代表声级均恒定，取各自**段首前一读数**（读数自该时刻起生效，持续到下一读数）。
- **逐段扣背景**：`E = 10^(总db/10) − 10^(背景db/10)`。**任一段 E ≤ 0**（背景读数不低于总读数）
  即整夜无法完成扣除，返回原因 `background_exceeds_total`，`leq` / `limit_db` / `exceeds` 均为 `null`；
  **该段不跳过**——响应仍返回全部对齐分段，其 `residual_energy` 为 `null`，其余段照常给出 E 值。
- **整夜 Leq**：以各段**真实秒数**（跨夏令时按绝对时刻计权，春令时 7 小时、秋令时 9 小时）对 E 加权。
- **超限判定**：以**未舍入**的 Leq 与限值严格比较（等于限值不算超限）；响应中的 `leq` 仅用于展示，**四舍五入保留两位小数**。
- **覆盖不足**：任一序列首尾未覆盖或窗内存在 > 15 分钟间隔时，沿用 `/assess` 的原因代码
  （`head_not_covered` / `tail_not_covered` / `gap_too_large` / `insufficient_readings` / `incomplete_coverage`），
  `leq` 与 `exceeds` 为 `null`，分段仍尽量返回。两序列均有问题时先报告总噪声序列的原因。
- 两组读数时刻**不必对齐**，也不必等间隔（各自满足 ≤ 15 分钟即可）。

### 响应

数据充分且扣除有效时 HTTP 200：

```json
{
  "sufficient": true,
  "window": {
    "start": "2026-09-25T22:00:00+08:00",
    "end": "2026-09-26T06:00:00+08:00",
    "duration_seconds": 28800.0
  },
  "total_readings_count": 51,
  "background_readings_count": 51,
  "segments": [
    {
      "start": "2026-09-25T22:00:00+08:00",
      "end": "2026-09-25T22:10:00+08:00",
      "duration_seconds": 600.0,
      "total_db": 60.0,
      "background_db": 50.0,
      "residual_energy": 900000.0
    }
  ],
  "leq": 59.54,
  "limit_db": 55.0,
  "exceeds": true,
  "conclusion": "超限",
  "reason_code": null,
  "reason": null
}
```

存在 E ≤ 0 的分段（或覆盖不足）时同样 HTTP 200，`sufficient=false`，
`conclusion` 为 `"数据不足"`，并给出 `reason_code` 与中文 `reason`：

```json
{
  "sufficient": false,
  "window": { "start": "...", "end": "...", "duration_seconds": 28800.0 },
  "total_readings_count": 51,
  "background_readings_count": 51,
  "segments": [
    { "start": "...", "end": "...", "duration_seconds": 600.0,
      "total_db": 55.0, "background_db": 55.0, "residual_energy": null }
  ],
  "leq": null,
  "limit_db": null,
  "exceeds": null,
  "conclusion": "数据不足",
  "reason_code": "background_exceeds_total",
  "reason": "存在背景声级不低于总声级的分段，声能差 E ≤ 0，无法扣除背景"
}
```

### 示例

```bash
curl -X POST http://localhost:8000/background-assess \
  -H 'Content-Type: application/json' \
  -d "$(python3 - <<'PY'
import json
from datetime import datetime, timedelta, timezone
start = datetime(2026, 9, 25, 13, 50, tzinfo=timezone.utc)  # 北京 21:50
times = [start + timedelta(minutes=10 * i) for i in range(51)]  # 至次日 06:10
print(json.dumps({
    "timezone": "Asia/Shanghai",
    "night_date": "2026-09-25",
    "limit_db": 55,
    "total_readings": [{"time": t.isoformat(), "db": 60} for t in times],
    "background_readings": [{"time": t.isoformat(), "db": 50} for t in times],
}))
PY
)"
```

总声级恒为 60 dBA、背景恒为 50 dBA 时返回 `"leq": 59.54, "exceeds": true`。

数据合法性规则（HTTP 400，错误码同 `/assess`：乱序 `out_of_order`、重复 `duplicate_timestamp`、
非数值/无时区/缺字段 `invalid_request`、时区 `invalid_timezone`）对两组序列分别生效，
错误信息会注明来自“总噪声”还是“背景噪声”序列。

## 采样补点规划（POST /sampling-plan）

在已有采样时刻的基础上，为当地 22:00–次日 06:00 的夜间窗口生成**最少数量**的新增采样时刻（补点）。

### 请求

`POST /sampling-plan`，`Content-Type: application/json`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `timezone` | string | 施工地 **IANA 时区**，如 `Asia/Shanghai`、`America/New_York` |
| `night_date` | string | 夜间**起始日期**（`YYYY-MM-DD`，当地日历日），窗口为该日 22:00 至次日 06:00 |
| `sampled_times` | array \| null | 已采样时刻，**可省略、为 null 或空数组**（表示尚无采样）；元素为 ISO 8601 时间，**必须带时区偏移**，且按**绝对时刻严格递增**（可用任意偏移，内部按绝对时刻统一计算） |

### 补点规则

- **首尾覆盖**：窗口首端需存在 ≤ 22:00 的时刻、尾端需存在 ≥ 次日 06:00 的时刻；未被覆盖时在窗口起点 / 终点补点（补点只能落在窗口内，含边界）。
- **间隔约束**：合并已有与新增时刻后，所有与窗口**有正重叠**的相邻采样间隔 ≤ 15 分钟（恰好 15 分钟允许）；窗口外的间隔不参与约束。
- **锚点**：窗口外仅取紧邻边界的已有时刻（窗口前最后一个、窗口后第一个）参与间隔约束；不会在窗口外补点。
- **最少与 tie-break**：补点数量最少；多个最少方案并存时，优先从较早的可行时刻补起。
- **夏令时**：窗口时长与间隔均按**真实秒数**计算（春季跳变当晚 7 小时、秋季回拨当晚 9 小时），补点按当地偏移返回，回拨当晚同一挂钟时刻会以不同偏移各出现一次。

### 响应

HTTP 200：

```json
{
  "added_times": ["2026-09-25T22:00:00+08:00", "2026-09-25T22:15:00+08:00", "..."],
  "added_count": 33,
  "already_qualified": false
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `added_times` | array | 需新增的采样时刻，换算到施工地时区、**带当地偏移**，按时间升序 |
| `added_count` | number | 新增采样时刻数量（最少方案） |
| `already_qualified` | boolean | 原已采样时刻是否已合格（无需任何补点，此时 `added_times` 为空数组） |

### 数据合法性规则（HTTP 400，直接拒绝）

- 采样时间**乱序**（后一条早于前一条，按绝对时刻）：错误码 `out_of_order`；
- 采样时间**重复**（同一绝对时刻）：错误码 `duplicate_timestamp`；
- 采样时间不带时区偏移、请求体不是合法 JSON 或缺少必填字段：错误码 `invalid_request`；
- IANA 时区无法识别：错误码 `invalid_timezone`。

错误响应格式与 `/assess` 一致：`{ "error": { "code": "...", "message": "..." } }`。

### 示例

```bash
curl -X POST http://localhost:8000/sampling-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "timezone": "Asia/Shanghai",
    "night_date": "2026-09-25",
    "sampled_times": ["2026-09-25T21:50:00+08:00", "2026-09-26T06:10:00+08:00"]
  }'
```

返回 `added_count: 33`，补点为 22:00、22:10、22:25、…、05:55（前两个间隔利用窗外锚点，其余按 15 分钟等距）。

## 时区与夏令时

- 窗口起止按 `timezone` 指定的当地挂钟时间（22:00 / 06:00）确定，时长按**绝对时刻**计算。
  因此在实行夏令时的地区，春季跳变当晚窗口为 7 小时、秋季回拨当晚为 9 小时，
  Leq 的分母与各段时长均按真实秒数计权，不会错误地按挂钟 8 小时计算。
- 读数可用任意 UTC 偏移提交（例如现场设备用 `Z` 或 `+09:00`），只要绝对时刻正确即可；
  响应中的窗口与裁剪段时间戳统一换算到施工地时区展示。

## 项目结构

```
app/
  main.py          # FastAPI 入口、路由与异常处理
  assessment.py    # 窗口裁剪、数据充分性校验、Leq 核心算法
  background.py    # 总噪声/背景噪声合并切段与声能差扣除算法
  sampling.py      # 夜间窗口最少补点规划算法
  schemas.py       # 请求/响应模型
tests/             # 核心算法与 HTTP 接口测试
Dockerfile
docker-compose.yml
requirements.txt
```

## 本地测试

```bash
pip install -r requirements-dev.txt
pytest -q
```
