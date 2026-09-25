"""FastAPI 入口：直播导播口令编排 API。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .compiler import CompileError, compile_commands, preview_change
from .rebalance import rebalance_schedule
from .reconcile import reconcile_runs
from .routing import assign_channels
from .schemas import (
    AssignRequest,
    AssignResponse,
    CommandItem,
    CompileResponse,
    ErrorBody,
    PreviewRequest,
    PreviewResponse,
    RebalanceRequest,
    RebalanceResponse,
    ReconcileRequest,
    ReconcileResponse,
)

app = FastAPI(
    title="直播导播口令编排 API",
    version="1.4.0",
    description=(
        "接收导播口令数组，解析锚点依赖得到每条口令的起止毫秒时间，"
        "并列出同通道半开区间重叠的冲突 id 对；"
        "支持预览单条口令改动带来的时间传导与冲突对增减；"
        "并按可压缩余量比例分摊节目单延误，保证固定开场时刻不被移动；"
        "直播结束后还可将计划口令与实播记录按通道保序对账，"
        "最小化起止偏差与漏播、额外播出罚分之和；"
        "亦可在不改变任何起止时刻的前提下做通道改派，"
        "用最少的改派消除同通道时间重叠冲突。"
    ),
)


@app.exception_handler(CompileError)
async def compile_error_handler(_request: Request, exc: CompileError) -> JSONResponse:
    """业务编排错误统一返回 422 + 结构化错误码。"""
    return JSONResponse(status_code=422, content=exc.to_dict())


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体结构错误同样使用统一错误格式。"""
    return JSONResponse(
        status_code=422,
        content={
            "code": "INVALID_REQUEST",
            "message": "请求体不符合接口约定的结构",
            # errors 上下文中可能含异常对象，经 jsonable_encoder 保证可序列化
            "details": {"errors": jsonable_encoder(exc.errors())},
        },
    )


@app.get("/health", tags=["meta"], summary="健康检查")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/compile",
    response_model=CompileResponse,
    tags=["compile"],
    summary="编排口令：解析时间并检测通道冲突",
    responses={422: {"model": ErrorBody, "description": "请求非法或编排失败"}},
)
async def compile(items: list[CommandItem]) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return compile_commands([item.model_dump() for item in items])


@app.post(
    "/preview",
    response_model=PreviewResponse,
    tags=["compile"],
    summary="预览改动：对比改动前后的时间变化与冲突对增减",
    responses={422: {"model": ErrorBody, "description": "请求非法或编排失败"}},
)
async def preview(body: PreviewRequest) -> dict:
    change = body.change.model_dump(exclude_none=True)
    return preview_change(
        [item.model_dump() for item in body.commands], change
    )


@app.post(
    "/schedule/rebalance",
    response_model=RebalanceResponse,
    tags=["schedule"],
    summary="节目单延误分摊：按可压缩余量比例分摊延误，守住固定开场时刻",
    responses={422: {"model": ErrorBody, "description": "请求非法或可压缩余量不足"}},
)
async def rebalance(body: RebalanceRequest) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return rebalance_schedule([item.model_dump() for item in body.segments])


@app.post(
    "/runs/reconcile",
    response_model=ReconcileResponse,
    tags=["runs"],
    summary="实播对账：计划口令与实播记录的保序最优匹配",
    responses={422: {"model": ErrorBody, "description": "请求非法或对账失败"}},
)
async def reconcile(body: ReconcileRequest) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return reconcile_runs(
        [item.model_dump() for item in body.commands],
        [record.model_dump() for record in body.records],
        tolerance_ms=body.tolerance_ms,
        miss_penalty=body.miss_penalty,
        extra_penalty=body.extra_penalty,
    )


@app.post(
    "/routing/assign",
    response_model=AssignResponse,
    tags=["routing"],
    summary="通道改派：不改变起止时刻，用最少的改派消除同通道时间重叠冲突",
    responses={422: {"model": ErrorBody, "description": "请求非法或无可行改派方案"}},
)
async def assign(body: AssignRequest) -> dict:
    # 核心逻辑不依赖 Pydantic，直接喂入 dict 以便独立测试与复用
    return assign_channels(
        [item.model_dump() for item in body.commands],
        [alt.model_dump() for alt in body.alternatives],
    )
