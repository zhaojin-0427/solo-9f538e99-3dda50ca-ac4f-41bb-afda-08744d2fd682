"""业务逻辑：批量写入与日报查询。"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass

from .config import HOURS_PER_DAY, MIN_SEGMENT_HOURS, REQUIRED_VALID_HOURS
from .db import Database, utc_timestamp
from .models import (
    CandidateBoundary,
    ConflictDetail,
    CrossedLinesRequest,
    CrossedLinesResponse,
    DailyReport,
    DiagnosisStations,
    HourEvidence,
    HourState,
    ReadingRecord,
    SegmentStats,
    StartWindow,
    StationReadingEvidence,
)


class RevisionConflictError(Exception):
    """同号不同内容：整批拒绝。"""

    def __init__(self, conflicts: list[ConflictDetail]) -> None:
        self.conflicts = conflicts
        super().__init__(f"{len(conflicts)} revision conflict(s)")


@dataclass(frozen=True)
class BatchOutcome:
    received: int
    created: int
    unchanged: int
    stations: list[str]
    dates: list[str]


@dataclass(frozen=True)
class _SegmentEval:
    """候选分界一侧的原始（未舍入）误差统计。"""

    hours: list[int]
    mae_original: float
    mae_swapped: float
    advantage: float


@dataclass(frozen=True)
class _BoundaryEval:
    """一个候选分界：common[:split] 为前段，common[split:] 为后段。"""

    split: int
    before: _SegmentEval
    after: _SegmentEval


class ReadingService:
    def __init__(self, database: Database) -> None:
        self.db = database

    # -- 批量写入 -------------------------------------------------------
    def apply_batch(self, records: list[ReadingRecord]) -> BatchOutcome:
        """原子地应用一批修订。

        校验规则（全部在同一写事务内、持有写锁时完成）：
        1. 同号同内容 → 幂等跳过，计为 unchanged；
        2. 同号不同内容 → 抛 RevisionConflictError，整批回滚；
        3. 新修订号 → 插入新行（不覆盖旧修订）。
        """
        if not records:
            raise ValueError("batch must contain at least one record")

        unique_keys = {(r.station_id, r.date.isoformat(), r.hour, r.revision) for r in records}
        stations = sorted({r.station_id for r in records})
        dates = sorted({r.date.isoformat() for r in records})

        conn = self.db.connect()
        try:
            with Database.write_transaction(conn):
                # 先在同一事务内查已有修订，避免校验与写入之间的竞态。
                existing = self._load_existing(conn, unique_keys)

                # 批内重复检查：同一 batch 中同一 key 出现两次且内容不同 → 冲突。
                seen: dict[tuple[str, str, int, int], tuple[float | None, bool]] = {}
                conflicts: list[ConflictDetail] = []
                created = 0
                unchanged = 0
                now = utc_timestamp()

                for record in records:
                    key = (record.station_id, record.date.isoformat(), record.hour, record.revision)
                    submitted = record.content()

                    if key in seen and seen[key] != submitted:
                        conflicts.append(
                            self._conflict(
                                record,
                                existing_payload=seen[key],
                                submitted_payload=submitted,
                            )
                        )
                        continue
                    if key in seen:
                        unchanged += 1
                        continue
                    seen[key] = submitted

                    stored = existing.get(key)
                    if stored is not None:
                        if stored != submitted:
                            conflicts.append(
                                self._conflict(
                                    record,
                                    existing_payload=stored,
                                    submitted_payload=submitted,
                                )
                            )
                        else:
                            unchanged += 1
                        continue

                    conn.execute(
                        """
                        INSERT INTO pm25_readings
                            (station_id, obs_date, hour, revision, value, retracted, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.station_id,
                            record.date.isoformat(),
                            record.hour,
                            record.revision,
                            record.value,
                            1 if record.retracted else 0,
                            now,
                        ),
                    )
                    created += 1

                if conflicts:
                    # 让 with 块内抛异常 → ROLLBACK，本批任何插入都不生效。
                    raise RevisionConflictError(conflicts)
        finally:
            conn.close()

        return BatchOutcome(
            received=len(records),
            created=created,
            unchanged=unchanged,
            stations=stations,
            dates=dates,
        )

    @staticmethod
    def _load_existing(
        conn: sqlite3.Connection,
        keys: set[tuple[str, str, int, int]],
    ) -> dict[tuple[str, str, int, int], tuple[float | None, bool]]:
        if not keys:
            return {}
        existing: dict[tuple[str, str, int, int], tuple[float | None, bool]] = {}

        ordered = sorted(keys)
        # 用 row-value IN 一次查询多组 (station_id, obs_date, hour, revision)，
        # 只取批次涉及的键；分块以避开 SQLite 的绑定变量上限（每键 4 个参数）。
        chunk_size = 200
        for start in range(0, len(ordered), chunk_size):
            chunk = ordered[start : start + chunk_size]
            placeholders = ",".join("(?,?,?,?)" for _ in chunk)
            params = [value for key in chunk for value in key]
            rows = conn.execute(
                f"""
                SELECT station_id, obs_date, hour, revision, value, retracted
                FROM pm25_readings
                WHERE (station_id, obs_date, hour, revision) IN (VALUES {placeholders})
                """,
                params,
            ).fetchall()
            for row in rows:
                existing[
                    (row["station_id"], row["obs_date"], row["hour"], row["revision"])
                ] = (row["value"], bool(row["retracted"]))
        return existing

    @staticmethod
    def _conflict(
        record: ReadingRecord,
        *,
        existing_payload: tuple[float | None, bool],
        submitted_payload: tuple[float | None, bool],
    ) -> ConflictDetail:
        return ConflictDetail(
            station_id=record.station_id,
            date=record.date.isoformat(),
            hour=record.hour,
            revision=record.revision,
            existing=_payload_dict(existing_payload),
            submitted=_payload_dict(submitted_payload),
        )

    # -- 日报查询 -------------------------------------------------------
    def daily_report(self, station_id: str, day: dt.date) -> DailyReport:
        """按每小时最大修订号计算日报。"""
        obs_date = day.isoformat()
        conn = self.db.connect()
        try:
            # 单条 SELECT 在 WAL 模式下读到的是一致快照，不会看到写入中途的状态。
            # 子查询取每小时的最大修订号，再与该修订行关联，保证「旧修订不覆盖新值」。
            rows = conn.execute(
                """
                SELECT r.hour, r.value, r.retracted, r.revision
                FROM pm25_readings AS r
                WHERE r.station_id = ?
                  AND r.obs_date = ?
                  AND r.revision = (
                      SELECT MAX(revision)
                      FROM pm25_readings AS m
                      WHERE m.station_id = r.station_id
                        AND m.obs_date = r.obs_date
                        AND m.hour = r.hour
                  )
                ORDER BY r.hour
                """,
                (station_id, obs_date),
            ).fetchall()
        finally:
            conn.close()

        latest = {row["hour"]: row for row in rows}
        hours: list[HourState] = []
        missing_hours: list[int] = []
        retracted_hours: list[int] = []
        valid_values: list[float] = []

        for hour in range(HOURS_PER_DAY):
            row = latest.get(hour)
            if row is None:
                # 从未收到过数据。
                missing_hours.append(hour)
                hours.append(HourState(hour=hour, state="missing"))
            elif row["retracted"]:
                # 最新修订是撤回：该小时同样没有有效值，计入缺失小时，
                # 同时单独列在 retracted_hours 中以便区分「撤回」与「未报」。
                missing_hours.append(hour)
                retracted_hours.append(hour)
                hours.append(HourState(hour=hour, state="retracted", revision=row["revision"]))
            else:
                valid_values.append(row["value"])
                hours.append(
                    HourState(
                        hour=hour,
                        state="valid",
                        value=row["value"],
                        revision=row["revision"],
                    )
                )

        valid_hours = len(valid_values)
        if valid_hours >= REQUIRED_VALID_HOURS:
            daily_mean = round(sum(valid_values) / valid_hours, 2)
            status = "ok"
        else:
            daily_mean = None
            status = "insufficient_valid_hours"

        return DailyReport(
            station_id=station_id,
            date=obs_date,
            required_valid_hours=REQUIRED_VALID_HOURS,
            valid_hours=valid_hours,
            daily_mean=daily_mean,
            status=status,
            missing_hours=missing_hours,
            retracted_hours=retracted_hours,
            hours=hours,
        )

    # -- 串线诊断 -------------------------------------------------------
    def diagnose_crossed_lines(self, request: CrossedLinesRequest) -> CrossedLinesResponse:
        """诊断两座疑似站在指定北京日期内是否发生 PM2.5 小时值串线。

        口径与判定规则：
        * 每站每小时只取最高修订号的有效（非撤回）读数；
        * 只在四站都有有效值的「共同小时」上计算误差；
        * 原配对误差 = (|A-refA| + |B-refB|) / 2，
          互换配对误差 = (|A-refB| + |B-refA|) / 2；
        * 候选分界前后各至少 MIN_SEGMENT_HOURS 个共同小时，且前段原配对、
          后段互换配对的误差优势都严格大于门槛时，提示疑似串线；
        * 多个候选分界满足条件时取「最稳健」（两侧优势的最小值最大）者；
          分界两侧共同小时相邻则起点唯一，否则（缺测）只报可能时段。

        整个诊断在同一只读事务（同一数据库快照）内完成，不做缓存；
        补报或撤回后重新调用即可看到新证据。
        """
        roles = DiagnosisStations(
            suspect_a=request.suspect_a,
            reference_a=request.reference_a,
            suspect_b=request.suspect_b,
            reference_b=request.reference_b,
        )
        station_ids = [roles.suspect_a, roles.reference_a, roles.suspect_b, roles.reference_b]
        obs_date = request.date.isoformat()

        conn = self.db.connect()
        try:
            with Database.read_transaction(conn):
                rows = conn.execute(
                    """
                    SELECT r.station_id, r.hour, r.value, r.revision
                    FROM pm25_readings AS r
                    WHERE r.obs_date = ?
                      AND r.station_id IN (?, ?, ?, ?)
                      AND r.retracted = 0
                      AND r.revision = (
                          SELECT MAX(revision)
                          FROM pm25_readings AS m
                          WHERE m.station_id = r.station_id
                            AND m.obs_date = r.obs_date
                            AND m.hour = r.hour
                      )
                    ORDER BY r.hour, r.station_id
                    """,
                    (obs_date, *station_ids),
                ).fetchall()
        finally:
            conn.close()

        latest: dict[str, dict[int, tuple[float, int]]] = {sid: {} for sid in station_ids}
        for row in rows:
            latest[row["station_id"]][row["hour"]] = (row["value"], row["revision"])

        valid_hour_counts = {sid: len(hours) for sid, hours in latest.items()}
        sus_a, ref_a = latest[roles.suspect_a], latest[roles.reference_a]
        sus_b, ref_b = latest[roles.suspect_b], latest[roles.reference_b]
        common = sorted(set(sus_a) & set(ref_a) & set(sus_b) & set(ref_b))

        evidence = [
            HourEvidence(
                hour=hour,
                stations={
                    sid: StationReadingEvidence(
                        value=latest[sid][hour][0],
                        revision=latest[sid][hour][1],
                    )
                    for sid in station_ids
                },
            )
            for hour in common
        ]

        base = {
            "date": obs_date,
            "stations": roles,
            "threshold": request.threshold,
            "valid_hour_counts": valid_hour_counts,
            "common_hours": common,
            "common_hour_count": len(common),
            "evidence": evidence,
        }

        n = len(common)
        if n < 2 * MIN_SEGMENT_HOURS:
            return CrossedLinesResponse(
                **base,
                suspected=False,
                reason_code="insufficient_common_hours",
                message=(
                    f"四站共同有效小时仅 {n} 个，不足 {2 * MIN_SEGMENT_HOURS} 个"
                    f"（候选分界前后各需至少 {MIN_SEGMENT_HOURS} 个），"
                    "证据不足，无法评估串线分界。"
                ),
                start_unique=False,
            )

        # 每个共同小时上两种配对的绝对误差。
        err_original: dict[int, float] = {}
        err_swapped: dict[int, float] = {}
        for hour in common:
            va, vra = sus_a[hour][0], ref_a[hour][0]
            vb, vrb = sus_b[hour][0], ref_b[hour][0]
            err_original[hour] = (abs(va - vra) + abs(vb - vrb)) / 2.0
            err_swapped[hour] = (abs(va - vrb) + abs(vb - vra)) / 2.0

        candidates: list[_BoundaryEval] = []
        for split in range(MIN_SEGMENT_HOURS, n - MIN_SEGMENT_HOURS + 1):
            before_hours = common[:split]
            after_hours = common[split:]
            mo_before = sum(err_original[h] for h in before_hours) / split
            ms_before = sum(err_swapped[h] for h in before_hours) / split
            mo_after = sum(err_original[h] for h in after_hours) / (n - split)
            ms_after = sum(err_swapped[h] for h in after_hours) / (n - split)
            candidates.append(
                _BoundaryEval(
                    split=split,
                    before=_SegmentEval(
                        hours=before_hours,
                        mae_original=mo_before,
                        mae_swapped=ms_before,
                        advantage=ms_before - mo_before,
                    ),
                    after=_SegmentEval(
                        hours=after_hours,
                        mae_original=mo_after,
                        mae_swapped=ms_after,
                        advantage=mo_after - ms_after,
                    ),
                )
            )

        def rank(candidate: _BoundaryEval) -> tuple[float, float, int]:
            # 先看两侧优势的较小者（越稳健越好），再看总优势，最后取最早分界。
            return (
                min(candidate.before.advantage, candidate.after.advantage),
                candidate.before.advantage + candidate.after.advantage,
                -candidate.split,
            )

        qualifying = [
            c
            for c in candidates
            if c.before.advantage > request.threshold
            and c.after.advantage > request.threshold
        ]

        if not qualifying:
            best = max(candidates, key=rank)
            return CrossedLinesResponse(
                **base,
                suspected=False,
                reason_code="no_qualifying_boundary",
                message=(
                    "共同小时数量足够，但没有任何候选分界同时满足「前段原配对误差优势、"
                    "后段互换配对误差优势均大于门槛」，证据不足，不能判定串线；"
                    "best_candidate 给出最接近的分界供人工参考。"
                ),
                start_unique=False,
                best_candidate=_candidate_model(common, best),
            )

        primary = max(qualifying, key=rank)
        window = _window_for(common, primary.split)
        unique = window.earliest_hour == window.latest_hour
        if unique:
            message = (
                "疑似串线：分界前原配对显著更优、分界后互换配对显著更优，"
                "两侧误差优势均超过门槛，且分界两侧共同小时相邻，串线起点可唯一确定。"
            )
        else:
            message = (
                "疑似串线：两侧误差优势均超过门槛，但分界前后存在缺测小时，"
                "串线起点无法唯一确定，仅给出可能时段。"
            )

        return CrossedLinesResponse(
            **base,
            suspected=True,
            reason_code="crossed_lines_suspected",
            message=message,
            start_unique=unique,
            suspected_start_hour=window.earliest_hour if unique else None,
            possible_start_window=window,
            before=_segment_model(primary.before),
            after=_segment_model(primary.after),
        )


def _payload_dict(payload: tuple[float | None, bool]) -> dict:
    value, retracted = payload
    return {"value": value, "retracted": retracted}


def _window_for(common: list[int], split: int) -> StartWindow:
    """分界 split 对应的起点可能时段（闭区间，北京小时）。

    串线起点在「前段最后一个共同小时之后、后段第一个共同小时（含）之前」；
    两者相邻时区间退化为单一小时，起点唯一。
    """
    return StartWindow(earliest_hour=common[split - 1] + 1, latest_hour=common[split])


def _segment_model(segment: _SegmentEval) -> SegmentStats:
    return SegmentStats(
        hours=segment.hours,
        hour_count=len(segment.hours),
        mae_original=round(segment.mae_original, 3),
        mae_swapped=round(segment.mae_swapped, 3),
        advantage=round(segment.advantage, 3),
    )


def _candidate_model(common: list[int], candidate: _BoundaryEval) -> CandidateBoundary:
    return CandidateBoundary(
        possible_start_window=_window_for(common, candidate.split),
        before=_segment_model(candidate.before),
        after=_segment_model(candidate.after),
    )
