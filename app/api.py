"""HTTP 接口层。"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .models import (
    BatchRequest,
    BatchResult,
    ConflictResponse,
    CrossedLinesRequest,
    CrossedLinesResponse,
    DailyReport,
)
from .service import ReadingService, RevisionConflictError

logger = logging.getLogger(__name__)

router = APIRouter()


def get_service(request: Request) -> ReadingService:
    return request.app.state.service


@router.get("/healthz", summary="健康检查")
def healthz() -> dict:
    return {"status": "ok"}


@router.post(
    "/api/v1/readings/batch",
    response_model=BatchResult,
    status_code=200,
    summary="批量补报 / 修订 / 撤回 PM2.5 小时值",
    responses={
        409: {
            "model": ConflictResponse,
            "description": "同一站点日期小时修订号已存在但内容不同；整批已回滚。",
        },
        422: {"description": "请求体校验失败。"},
    },
)
def submit_batch(
    payload: BatchRequest,
    service: ReadingService = Depends(get_service),
) -> BatchResult:
    """原子写入一批记录。

    * 每小时以最大 revision 为准；新 revision 追加，不覆盖历史。
    * 同 revision 同内容重复提交无副作用（幂等）。
    * 同 revision 不同内容 → 409，整批回滚，不留下部分写入。
    """
    try:
        outcome = service.apply_batch(payload.records)
    except RevisionConflictError as exc:
        body = ConflictResponse(
            message="one or more revisions already exist with different content; batch rolled back",
            conflicts=exc.conflicts,
        )
        raise HTTPException(status_code=409, detail=body.model_dump()) from exc

    return BatchResult(
        received=outcome.received,
        created=outcome.created,
        unchanged=outcome.unchanged,
        stations=outcome.stations,
        dates=outcome.dates,
    )


@router.get(
    "/api/v1/stations/{station_id}/daily",
    response_model=DailyReport,
    summary="查询站点某北京日期的 PM2.5 日报",
)
def get_daily_report(
    station_id: str,
    date: dt.date = Query(..., description="北京时间观测日期 YYYY-MM-DD"),
    service: ReadingService = Depends(get_service),
) -> DailyReport:
    station_id = station_id.strip()
    if not station_id:
        raise HTTPException(status_code=422, detail="station_id must not be blank")
    return service.daily_report(station_id, date)


@router.post(
    "/api/v1/diagnostics/crossed-lines",
    response_model=CrossedLinesResponse,
    status_code=200,
    summary="串线诊断：两座疑似站的 PM2.5 小时值是否在当日被接反",
    responses={
        422: {"description": "请求体校验失败（四站不互异、门槛非正数、日期非法等）。"},
    },
)
def diagnose_crossed_lines(
    payload: CrossedLinesRequest,
    service: ReadingService = Depends(get_service),
) -> CrossedLinesResponse:
    """对比两座疑似站与各自同址参照站，定位可能的串线起点。

    * 每站每小时只取最高修订号的有效读数，只在四站共同有值的小时计算误差；
    * 候选分界前后各至少 6 个共同小时，且前段原配对、后段互换配对的
      误差优势都大于 ``threshold`` 时提示疑似串线；
    * 缺测使起点无法唯一确定时只返回可能时段；
    * 一次诊断读取同一数据库快照，不做缓存，补报/撤回后重查即反映新证据。
    """
    return service.diagnose_crossed_lines(payload)
