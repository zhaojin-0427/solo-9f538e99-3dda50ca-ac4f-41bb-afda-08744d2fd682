"""串线诊断 API 测试。

场景约定：参照站 RA/RB 的曲线差异约 60 µg/m³；疑似站 SA/SB 在切换小时
之前跟随各自参照站（原配对误差 ~0.3），之后跟随对方参照站（互换配对
误差 ~0.2），模拟网关维护后接反。
"""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

DAY = "2025-06-01"
SA, RA, SB, RB = "SA", "RA", "SB", "RB"


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(database_path=str(tmp_path / "test.db")))
    with TestClient(app) as c:
        yield c


def rec(hour, value=None, station=SA, revision=1, date=DAY, retracted=False):
    body = {
        "station_id": station,
        "date": date,
        "hour": hour,
        "revision": revision,
        "retracted": retracted,
    }
    if not retracted:
        body["value"] = value
    return body


def post(client, records):
    return client.post("/api/v1/readings/batch", json={"records": records})


def diagnose(client, threshold=5.0, date=DAY, **stations):
    payload = {
        "suspect_a": stations.get("suspect_a", SA),
        "reference_a": stations.get("reference_a", RA),
        "suspect_b": stations.get("suspect_b", SB),
        "reference_b": stations.get("reference_b", RB),
        "date": date,
        "threshold": threshold,
    }
    return client.post("/api/v1/diagnostics/crossed-lines", json=payload)


def ref_values(hour):
    """两座参照站的真实曲线，差异约 60，噪声项让每小时略有不同。"""
    return 80.0 + (hour % 3), 20.0 + (hour % 2)


def crossed_values(hour, switch_hour):
    """switch_hour 前 SA/SB 跟随各自参照站，之后跟随对方参照站。"""
    ra, rb = ref_values(hour)
    if hour < switch_hour:
        return ra + 0.4, rb + 0.2
    return rb + 0.3, ra + 0.1


def seed_crossed(client, switch_hour, missing=(), date=DAY):
    records = []
    for h in range(24):
        if h in missing:
            continue
        ra, rb = ref_values(h)
        va, vb = crossed_values(h, switch_hour)
        records += [
            rec(h, ra, station=RA, date=date),
            rec(h, rb, station=RB, date=date),
            rec(h, va, station=SA, date=date),
            rec(h, vb, station=SB, date=date),
        ]
    res = post(client, records)
    assert res.status_code == 200, res.text


def seed_aligned(client, date=DAY):
    """全天原配对更优（无串线）。"""
    records = []
    for h in range(24):
        ra, rb = ref_values(h)
        records += [
            rec(h, ra, station=RA, date=date),
            rec(h, rb, station=RB, date=date),
            rec(h, ra + 0.4, station=SA, date=date),
            rec(h, rb + 0.2, station=SB, date=date),
        ]
    res = post(client, records)
    assert res.status_code == 200, res.text


# ---------------------------------------------------------------- 判定逻辑
def test_crossed_lines_unique_start(client):
    seed_crossed(client, switch_hour=13)
    res = diagnose(client)
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["suspected"] is True
    assert body["reason_code"] == "crossed_lines_suspected"
    assert body["start_unique"] is True
    assert body["suspected_start_hour"] == 13
    assert body["possible_start_window"] == {"earliest_hour": 13, "latest_hour": 13}

    assert body["before"]["hours"] == list(range(13))
    assert body["after"]["hours"] == list(range(13, 24))
    assert body["before"]["advantage"] > 5.0
    assert body["after"]["advantage"] > 5.0
    assert body["before"]["mae_original"] < 1.0
    assert body["after"]["mae_swapped"] < 1.0

    assert body["common_hours"] == list(range(24))
    assert body["common_hour_count"] == 24
    assert body["valid_hour_counts"] == {SA: 24, RA: 24, SB: 24, RB: 24}
    assert body["stations"] == {
        "suspect_a": SA,
        "reference_a": RA,
        "suspect_b": SB,
        "reference_b": RB,
    }
    assert body["min_segment_hours"] == 6
    assert body["timezone"] == "Asia/Shanghai"

    # 证据小时及修订号
    assert len(body["evidence"]) == 24
    first = body["evidence"][0]
    assert first["hour"] == 0
    assert set(first["stations"]) == {SA, RA, SB, RB}
    assert first["stations"][SA]["revision"] == 1
    assert first["stations"][SA]["value"] == pytest.approx(80.4)


def test_missing_hours_around_start_only_window_reported(client):
    """分界前后缺测 → 起点无法唯一确定，只报可能时段。"""
    seed_crossed(client, switch_hour=13, missing=(12, 13))
    body = diagnose(client).json()

    assert body["suspected"] is True
    assert body["start_unique"] is False
    assert body["suspected_start_hour"] is None
    # 共同小时 0..11 与 14..23，起点可能是 12、13、14 任意一小时。
    assert body["possible_start_window"] == {"earliest_hour": 12, "latest_hour": 14}
    assert body["before"]["hours"] == list(range(12))
    assert body["after"]["hours"] == list(range(14, 24))
    assert 12 not in body["common_hours"]
    assert 13 not in body["common_hours"]


def test_insufficient_common_hours(client):
    seed_crossed(client, switch_hour=13, missing=tuple(range(14)))
    body = diagnose(client).json()

    assert body["suspected"] is False
    assert body["reason_code"] == "insufficient_common_hours"
    assert body["common_hour_count"] == 10
    assert "不足" in body["message"]
    assert body["before"] is None
    assert body["after"] is None
    assert body["best_candidate"] is None
    assert body["possible_start_window"] is None
    assert len(body["evidence"]) == 10


def test_no_data_at_all(client):
    body = diagnose(client).json()
    assert body["suspected"] is False
    assert body["reason_code"] == "insufficient_common_hours"
    assert body["common_hour_count"] == 0
    assert body["evidence"] == []
    assert body["valid_hour_counts"] == {SA: 0, RA: 0, SB: 0, RB: 0}


def test_no_crossing_no_qualifying_boundary(client):
    seed_aligned(client)
    body = diagnose(client).json()

    assert body["suspected"] is False
    assert body["reason_code"] == "no_qualifying_boundary"
    assert body["suspected_start_hour"] is None
    assert body["possible_start_window"] is None
    # 给出最接近的候选分界供人工参考：后段互换配对并不占优。
    assert body["best_candidate"] is not None
    assert body["best_candidate"]["after"]["advantage"] < 5.0
    assert body["best_candidate"]["before"]["advantage"] > 5.0


def test_threshold_too_strict_rejects_boundary(client):
    seed_crossed(client, switch_hour=13)
    body = diagnose(client, threshold=100.0).json()
    assert body["suspected"] is False
    assert body["reason_code"] == "no_qualifying_boundary"
    # 优势约 60，门槛 100 时不满足。
    assert 50.0 < body["best_candidate"]["after"]["advantage"] < 100.0


def test_crossing_at_earliest_allowed_boundary(client):
    """分界前后恰好各 6 个共同小时时也能判定。"""
    seed_crossed(client, switch_hour=6)
    body = diagnose(client).json()
    assert body["suspected"] is True
    assert body["suspected_start_hour"] == 6
    assert body["before"]["hour_count"] == 6
    assert body["after"]["hour_count"] == 18


# ---------------------------------------------------------------- 修订与快照
def test_latest_revision_used_and_revisions_returned(client):
    """先用 rev1 报全天正常数据（无串线），再用 rev2 把 13 点起改为接反数据。"""
    seed_aligned(client)
    first = diagnose(client).json()
    assert first["suspected"] is False

    records = []
    for h in range(13, 24):
        va, vb = crossed_values(h, switch_hour=13)
        records += [
            rec(h, va, station=SA, revision=2),
            rec(h, vb, station=SB, revision=2),
        ]
    assert post(client, records).status_code == 200

    body = diagnose(client).json()
    assert body["suspected"] is True
    assert body["suspected_start_hour"] == 13

    by_hour = {e["hour"]: e["stations"] for e in body["evidence"]}
    assert by_hour[0][SA]["revision"] == 1
    assert by_hour[13][SA]["revision"] == 2
    assert by_hour[23][SB]["revision"] == 2
    assert by_hour[13][RA]["revision"] == 1
    # 证据值是 rev2 的接反值，而不是 rev1。
    ra, rb = ref_values(13)
    assert by_hour[13][SA]["value"] == pytest.approx(rb + 0.3)


def test_retraction_is_reflected_on_rediagnosis(client):
    """撤回后重查必须反映新证据：撤回后半段数据 → 不再判定串线。"""
    seed_crossed(client, switch_hour=13)
    assert diagnose(client).json()["suspected"] is True

    records = [rec(h, station=SA, revision=2, retracted=True) for h in range(12, 24)]
    assert post(client, records).status_code == 200

    body = diagnose(client).json()
    assert body["suspected"] is False
    # SA 只剩 0..11 有效 → 共同小时 12 个，唯一候选分界两侧都是原配对更优。
    assert body["reason_code"] == "no_qualifying_boundary"
    assert body["common_hours"] == list(range(12))


def test_retraction_of_reference_hours_shrinks_common_set(client):
    seed_crossed(client, switch_hour=13, missing=(12, 13))
    assert diagnose(client).json()["suspected"] is True

    # 再撤回 RB 的 11..23 → 共同小时只剩 0..10，不足 12 个。
    records = [rec(h, station=RB, revision=2, retracted=True) for h in range(11, 24)]
    assert post(client, records).status_code == 200
    body = diagnose(client).json()
    assert body["reason_code"] == "insufficient_common_hours"
    assert body["suspected"] is False


def test_late_lower_revision_does_not_change_diagnosis(client):
    """迟到的旧修订不得影响诊断（仍取最高修订号）。"""
    date = "2025-06-02"
    records = []
    for h in range(24):
        ra, rb = ref_values(h)
        records += [
            rec(h, ra, station=RA, date=date),
            rec(h, rb, station=RB, date=date),
            # 3 点直接以 rev2 到达，其余小时 rev1。
            rec(h, ra + 0.4, station=SA, date=date, revision=2 if h == 3 else 1),
            rec(h, rb + 0.2, station=SB, date=date),
        ]
    assert post(client, records).status_code == 200
    # 迟到的 rev1（旧修订）在 rev2 之后到达，应被接受但不生效。
    assert post(client, [rec(3, 999.0, station=SA, revision=1, date=date)]).status_code == 200

    body = diagnose(client, date=date).json()
    assert body["suspected"] is False
    assert body["evidence"][3]["stations"][SA]["revision"] == 2
    assert body["evidence"][3]["stations"][SA]["value"] == pytest.approx(80.0 + 0.4)


# ---------------------------------------------------------------- 校验
@pytest.mark.parametrize(
    "stations",
    [
        {"reference_a": SA},                       # 与 suspect_a 重复
        {"suspect_b": SA},                         # 与 suspect_a 重复
        {"reference_b": RA},                       # 与 reference_a 重复
        {"suspect_b": SB, "reference_b": SB},      # 疑似站与其参照站相同
    ],
)
def test_stations_must_be_distinct(client, stations):
    res = diagnose(client, **stations)
    assert res.status_code == 422


@pytest.mark.parametrize("threshold", [0, -1, -0.5])
def test_threshold_must_be_positive(client, threshold):
    assert diagnose(client, threshold=threshold).status_code == 422


@pytest.mark.parametrize("threshold", ["5", True, None])
def test_threshold_type_rejected(client, threshold):
    assert diagnose(client, threshold=threshold).status_code == 422


def test_blank_station_rejected(client):
    assert diagnose(client, suspect_a="   ").status_code == 422


def test_date_with_time_component_rejected(client):
    res = client.post(
        "/api/v1/diagnostics/crossed-lines",
        json={
            "suspect_a": SA,
            "reference_a": RA,
            "suspect_b": SB,
            "reference_b": RB,
            "date": "2025-06-01T00:00:00",
            "threshold": 5.0,
        },
    )
    assert res.status_code == 422


def test_unknown_field_rejected(client):
    res = client.post(
        "/api/v1/diagnostics/crossed-lines",
        json={
            "suspect_a": SA,
            "reference_a": RA,
            "suspect_b": SB,
            "reference_b": RB,
            "date": DAY,
            "threshold": 5.0,
            "extra": "x",
        },
    )
    assert res.status_code == 422


def test_openapi_lists_diagnosis_endpoint(client):
    schema = client.get("/openapi.json").json()
    assert "/api/v1/diagnostics/crossed-lines" in schema["paths"]
