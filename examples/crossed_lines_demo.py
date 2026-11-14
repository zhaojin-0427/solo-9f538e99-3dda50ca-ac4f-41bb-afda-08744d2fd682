#!/usr/bin/env python3
"""串线诊断调用示例（仅用标准库）。

构造两座「疑似站」SA/SB 与各自的同址参照站 RA/RB：13:00 前 SA/SB 跟随
各自参照站，13:00 起跟随对方参照站（模拟网关维护后接反），然后调用
POST /api/v1/diagnostics/crossed-lines 让服务定位串线起点；最后撤回
后半段数据再诊断一次，展示证据不足时的响应。

用法：
    python3 examples/crossed_lines_demo.py                      # 默认 http://localhost:8000
    python3 examples/crossed_lines_demo.py http://localhost:9000
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
DATE = "2025-06-01"
SWITCH_HOUR = 13
SA, RA, SB, RB = "DEMO-SA", "DEMO-RA", "DEMO-SB", "DEMO-RB"


def post(path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def diagnose() -> dict:
    status, body = post(
        "/api/v1/diagnostics/crossed-lines",
        {
            "suspect_a": SA,
            "reference_a": RA,
            "suspect_b": SB,
            "reference_b": RB,
            "date": DATE,
            "threshold": 5.0,
        },
    )
    assert status == 200, body
    return body


def summarize(label: str, body: dict) -> None:
    print(f"{label}: suspected={body['suspected']} reason={body['reason_code']}")
    print(f"  {body['message']}")
    if body["suspected"]:
        if body["start_unique"]:
            print(f"  串线起点（唯一）: {body['suspected_start_hour']}:00")
        print(f"  可能时段: {body['possible_start_window']['earliest_hour']}:00"
              f" ~ {body['possible_start_window']['latest_hour']}:00")
        print(f"  前段优势={body['before']['advantage']} 后段优势={body['after']['advantage']}")
    print(f"  共同小时数={body['common_hour_count']} 证据条目数={len(body['evidence'])}")


def main() -> None:
    records = []
    for h in range(24):
        ra, rb = 80.0 + h % 3, 20.0 + h % 2
        if h < SWITCH_HOUR:
            va, vb = ra + 0.4, rb + 0.2   # 正常：跟随各自参照站
        else:
            va, vb = rb + 0.3, ra + 0.1   # 接反：跟随对方参照站
        for sid, value in ((RA, ra), (RB, rb), (SA, va), (SB, vb)):
            records.append(
                {"station_id": sid, "date": DATE, "hour": h, "revision": 1, "value": value}
            )
    status, body = post("/api/v1/readings/batch", {"records": records})
    print(f"写入四站 24 小时数据 -> HTTP {status} created={body.get('created')}")

    summarize(f"诊断（{SWITCH_HOUR}:00 起接反）", diagnose())

    status, body = post(
        "/api/v1/readings/batch",
        {
            "records": [
                {"station_id": SA, "date": DATE, "hour": h, "revision": 2, "retracted": True}
                for h in range(SWITCH_HOUR - 1, 24)
            ]
        },
    )
    print(f"撤回 {SA} 后半段 -> HTTP {status} created={body.get('created')}")
    summarize("撤回后重查（证据随新数据变化）", diagnose())


if __name__ == "__main__":
    main()
