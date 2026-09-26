"""FastAPI 应用入口：夜间施工噪声核算。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .assessment import AssessmentError, assess, get_timezone
from .background import assess_background
from .sampling import plan_sampling
from .schemas import (
    AssessRequest,
    AssessResponse,
    BackgroundAssessRequest,
    BackgroundAssessResponse,
    SamplingPlanRequest,
    SamplingPlanResponse,
)

app = FastAPI(
    title="夜间施工噪声核算 API",
    description=(
        "按当地 22:00–次日 06:00 裁剪相邻分贝读数覆盖的时段，"
        "以持续秒数加权声能计算等效声级 Leq，并给出是否超限的结论；"
        "亦可为夜间窗口生成满足 15 分钟间隔约束的最少补点计划。"
    ),
    version="1.0.0",
)


@app.exception_handler(AssessmentError)
async def assessment_error_handler(request: Request, exc: AssessmentError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 统一校验错误格式（含 JSON 结构错误与字段约束错误，如非数值声级、缺时区时间）
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "invalid_request",
                "message": "请求数据不合法",
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    # 请求体不是合法 JSON（如标准 JSON 不允许的 NaN / Infinity 记号）
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "invalid_request", "message": f"请求体不是合法 JSON：{exc}"}},
    )


@app.get("/health", tags=["运维"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/assess",
    response_model=AssessResponse,
    tags=["噪声核算"],
    summary="核算夜间等效声级 Leq 并判定是否超限",
)
async def assess_noise(req: AssessRequest) -> AssessResponse:
    tz = get_timezone(req.timezone)
    result = assess(
        timezone=tz,
        night_date=req.night_date,
        raw_readings=[(r.time, r.db) for r in req.readings],
        limit_db=req.limit_db,
    )
    return AssessResponse.from_assessment(result, readings_count=len(req.readings))


@app.post(
    "/background-assess",
    response_model=BackgroundAssessResponse,
    tags=["噪声核算"],
    summary="扣除背景噪声后核算夜间施工声级 Leq 并判定是否超限",
)
async def assess_background_noise(req: BackgroundAssessRequest) -> BackgroundAssessResponse:
    tz = get_timezone(req.timezone)
    result = assess_background(
        timezone=tz,
        night_date=req.night_date,
        total_raw=[(r.time, r.db) for r in req.total_readings],
        background_raw=[(r.time, r.db) for r in req.background_readings],
        limit_db=req.limit_db,
    )
    return BackgroundAssessResponse.from_assessment(result)


@app.post(
    "/sampling-plan",
    response_model=SamplingPlanResponse,
    tags=["采样规划"],
    summary="生成夜间窗口的最少补点计划",
)
async def sampling_plan(req: SamplingPlanRequest) -> SamplingPlanResponse:
    tz = get_timezone(req.timezone)
    plan = plan_sampling(
        timezone=tz,
        night_date=req.night_date,
        sampled_times=req.sampled_times or [],
    )
    return SamplingPlanResponse(
        added_times=plan.added,
        added_count=len(plan.added),
        already_qualified=plan.already_qualified,
    )
