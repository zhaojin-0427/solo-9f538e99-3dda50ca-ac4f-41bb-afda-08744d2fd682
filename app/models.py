"""请求 / 响应模型。"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import HOURS_PER_DAY, MAX_BATCH_RECORDS, MIN_SEGMENT_HOURS, REQUIRED_VALID_HOURS


class ReadingRecord(BaseModel):
    """一条 PM2.5 小时值修订记录。

    ``date`` 与 ``hour`` 均为北京时间（Asia/Shanghai）下的观测日期与整点小时。
    撤回记录必须不含浓度；非撤回记录必须包含浓度。
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    station_id: str = Field(min_length=1, max_length=128)
    date: dt.date
    hour: int = Field(ge=0, le=HOURS_PER_DAY - 1)
    revision: int = Field(ge=1)
    value: float | None = Field(default=None, ge=0)
    retracted: bool = False

    @field_validator("hour", "revision", "value", "retracted", mode="before")
    @classmethod
    def _reject_cross_type_coercion(cls, v: object, info) -> object:
        """拒绝让 bool/字符串被悄悄当成数字（如 ``"hour": true`` → 1）。

        JSON 里的数字类型必须写对；布尔字段只接受 JSON 布尔值。
        """
        if info.field_name == "retracted":
            if not isinstance(v, bool):
                raise ValueError("retracted must be a JSON boolean")
            return v
        if isinstance(v, bool):
            raise ValueError(f"{info.field_name} must be a number, not a boolean")
        if isinstance(v, str):
            raise ValueError(f"{info.field_name} must be a number, not a string")
        return v

    @field_validator("station_id")
    @classmethod
    def _normalize_station_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("station_id must not be blank")
        return v

    @field_validator("date", mode="before")
    @classmethod
    def _reject_datetime_for_date(cls, v: object) -> object:
        """只接受 YYYY-MM-DD；拒绝带时间部分的字符串，避免隐式截断。"""
        if isinstance(v, str) and ("T" in v or ":" in v):
            raise ValueError("date must be YYYY-MM-DD (Beijing date, no time component)")
        return v

    @model_validator(mode="after")
    def _check_value_matches_retraction(self) -> "ReadingRecord":
        # 撤回时 value 必须为空，非撤回时必须有值，二者不能矛盾。
        if self.retracted and self.value is not None:
            raise ValueError("retracted record must not include a value")
        if not self.retracted and self.value is None:
            raise ValueError("non-retracted record must include a value")
        return self

    def content(self) -> tuple[float | None, bool]:
        """用于判断同一修订号内容是否一致的载荷。"""
        return (self.value, self.retracted)


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[ReadingRecord] = Field(min_length=1, max_length=MAX_BATCH_RECORDS)


class BatchResult(BaseModel):
    received: int
    created: int
    unchanged: int
    stations: list[str]
    dates: list[str]


class ConflictDetail(BaseModel):
    station_id: str
    date: str
    hour: int
    revision: int
    existing: dict
    submitted: dict


class ConflictResponse(BaseModel):
    code: Literal["revision_conflict"] = "revision_conflict"
    message: str
    conflicts: list[ConflictDetail]
    rolled_back: Literal[True] = True


class HourState(BaseModel):
    hour: int
    state: Literal["valid", "missing", "retracted"]
    value: float | None = None
    revision: int | None = None


class DailyReport(BaseModel):
    """站点某北京日期的日报。

    ``missing_hours`` 是「没有有效值」的全部小时（未报 + 撤回），
    ``retracted_hours`` 是其中因最新修订为撤回而无效的小时，二者为子集关系。
    因此 ``valid_hours + len(missing_hours) == 24``。
    """

    station_id: str
    date: str
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    required_valid_hours: int = REQUIRED_VALID_HOURS
    valid_hours: int
    daily_mean: float | None
    status: Literal["ok", "insufficient_valid_hours"]
    missing_hours: list[int]
    retracted_hours: list[int]
    hours: list[HourState]


# ---------------------------------------------------------------- 串线诊断


class CrossedLinesRequest(BaseModel):
    """串线诊断请求。

    怀疑网关维护后 ``suspect_a`` 与 ``suspect_b`` 的 PM2.5 小时值被接反；
    ``reference_a`` / ``reference_b`` 分别是它们的同址参照站。
    四个站点必须互不相同；``date`` 为北京时间观测日期；``threshold``
    为正的误差门槛（µg/m³）。
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    suspect_a: str = Field(min_length=1, max_length=128)
    reference_a: str = Field(min_length=1, max_length=128)
    suspect_b: str = Field(min_length=1, max_length=128)
    reference_b: str = Field(min_length=1, max_length=128)
    date: dt.date
    threshold: float = Field(gt=0)

    @field_validator("suspect_a", "reference_a", "suspect_b", "reference_b")
    @classmethod
    def _normalize_station_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("station_id must not be blank")
        return v

    @field_validator("threshold", mode="before")
    @classmethod
    def _reject_threshold_coercion(cls, v: object) -> object:
        """与写入接口一致：数字字段不接受布尔或字符串冒充。"""
        if isinstance(v, bool):
            raise ValueError("threshold must be a number, not a boolean")
        if isinstance(v, str):
            raise ValueError("threshold must be a number, not a string")
        return v

    @field_validator("date", mode="before")
    @classmethod
    def _reject_datetime_for_date(cls, v: object) -> object:
        if isinstance(v, str) and ("T" in v or ":" in v):
            raise ValueError("date must be YYYY-MM-DD (Beijing date, no time component)")
        return v

    @model_validator(mode="after")
    def _stations_must_be_distinct(self) -> "CrossedLinesRequest":
        ids = [self.suspect_a, self.reference_a, self.suspect_b, self.reference_b]
        if len(set(ids)) != len(ids):
            raise ValueError(
                "suspect_a, reference_a, suspect_b, reference_b must be four distinct stations"
            )
        return self


class DiagnosisStations(BaseModel):
    """诊断中四个角色对应的站点号。"""

    suspect_a: str
    reference_a: str
    suspect_b: str
    reference_b: str


class StationReadingEvidence(BaseModel):
    """某站某小时被采纳的证据读数：该小时最高修订号的有效值。"""

    value: float
    revision: int


class HourEvidence(BaseModel):
    """一个四站共同有效小时上的全部证据读数。"""

    hour: int
    stations: dict[str, StationReadingEvidence]


class SegmentStats(BaseModel):
    """候选分界一侧（前段或后段）的误差统计。

    ``advantage`` 是该侧「期望配对」的误差优势：前段为原配对优势
    （``mae_swapped - mae_original``），后段为互换配对优势
    （``mae_original - mae_swapped``），均大于门槛时该侧支持串线假设。
    """

    hours: list[int]
    hour_count: int
    mae_original: float
    mae_swapped: float
    advantage: float


class StartWindow(BaseModel):
    """串线起点的可能时段（北京时间整点小时，闭区间）。"""

    earliest_hour: int
    latest_hour: int


class CandidateBoundary(BaseModel):
    """一个候选分界及其两侧统计（用于证据不足时说明原因）。"""

    possible_start_window: StartWindow
    before: SegmentStats
    after: SegmentStats


class CrossedLinesResponse(BaseModel):
    """串线诊断结果。

    * ``suspected`` 为 ``true`` 时表示找到满足条件的候选分界；
      起点可唯一确定时给出 ``suspected_start_hour``，否则只给出
      ``possible_start_window``（缺测导致无法唯一确定）。
    * ``suspected`` 为 ``false`` 时 ``reason_code`` 说明证据不足的原因，
      若有候选分界则附 ``best_candidate`` 供人工参考。
    * ``evidence`` 列出每个共同有效小时四站被采纳的读数与修订号。
    """

    date: str
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    stations: DiagnosisStations
    threshold: float
    min_segment_hours: int = MIN_SEGMENT_HOURS
    valid_hour_counts: dict[str, int]
    common_hours: list[int]
    common_hour_count: int
    suspected: bool
    reason_code: Literal[
        "crossed_lines_suspected",
        "insufficient_common_hours",
        "no_qualifying_boundary",
    ]
    message: str
    start_unique: bool
    suspected_start_hour: int | None = None
    possible_start_window: StartWindow | None = None
    before: SegmentStats | None = None
    after: SegmentStats | None = None
    best_candidate: CandidateBoundary | None = None
    evidence: list[HourEvidence]
