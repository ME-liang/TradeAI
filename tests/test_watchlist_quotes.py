# -*- coding: utf-8 -*-
"""自选股行情拼装（F-03 · 8000 侧）测试。

覆盖 `frontend/server.py::handle_api_watchlist_quotes` 的四种 data_status，
以及「行情名称优先于快照」「未登录透传 401」等关键口径。

全部离线：通过 monkeypatch 替换两个外部依赖（自选取数 / 全市场缓存）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend"))

import server as fe  # noqa: E402


RECS = {
    "600519": {"name": "贵州茅台", "code": "600519", "ind": "白酒", "price": "¥1685.00",
               "chg": "+0.85%", "chg_num": 0.85, "dir": "up", "mcap": 2.1e12,
               "mcap_disp": "2.10万亿", "fcap": 2.1e12, "fcap_disp": "2.10万亿"},
    "000001": {"name": "平安银行", "code": "000001", "ind": "银行", "price": "¥11.20",
               "chg": "-1.10%", "chg_num": -1.10, "dir": "down", "mcap": 2.1e11,
               "mcap_disp": "2100.00亿", "fcap": 2.1e11, "fcap_disp": "2100.00亿"},
}

ITEMS = [{"code": "600519", "name": "贵州茅台"}, {"code": "000001", "name": ""}]


@pytest.fixture()
def quotes(monkeypatch):
    """返回一个 (set_items, set_records, set_quote_only, call) 的操作集。

    注意：被测代码经 `get_market_data()` 取全市场数据（它同时返回
    data_status 与 source，比旧的 get_records() 多带了来源信息），
    因此这里 patch 的是 `get_market_data` 而非 `get_records`。
    """
    def set_items(items, err=None):
        monkeypatch.setattr(fe, "_watchlist_from_report_api", lambda ck: (items, err))

    def set_records(recs, market_status="ok", meta=None):
        monkeypatch.setattr(fe, "get_market_data",
                            lambda: (recs, market_status, meta or {}))

    def set_quote_only(fn):
        monkeypatch.setattr(fe, "_quote_only_record", fn)

    def call():
        return fe.handle_api_watchlist_quotes({}, "ta_session=x")

    return set_items, set_records, set_quote_only, call


def test_ok_status_shape_and_order(quotes):
    set_items, set_records, _, call = quotes
    set_items(ITEMS)
    set_records(list(RECS.values()))
    obj, status = call()
    assert status == 200
    assert obj["data_status"] == "ok"
    assert obj["total"] == 2
    assert [r["code"] for r in obj["stocks"]] == ["600519", "000001"]
    # 与 /api/stocks 同构：分页字段齐备，便于前端复用同一套渲染
    for k in ("updated", "total", "page", "pageSize", "stocks"):
        assert k in obj


def test_name_snapshot_fills_empty_but_loses_to_live_quote(quotes):
    set_items, set_records, _, call = quotes
    set_items(ITEMS)                              # 000001 的快照名为空
    set_records(list(RECS.values()))
    obj, _ = call()
    assert obj["stocks"][1]["name"] == "平安银行"   # 由行情补齐

    recs = [dict(RECS["600519"]), dict(RECS["000001"])]
    recs[1]["name"] = "平安银行(行情)"
    set_records(recs)
    obj, _ = call()
    assert obj["stocks"][1]["name"] == "平安银行(行情)"   # 行情名优先


def test_empty_when_watchlist_empty(quotes):
    set_items, set_records, _, call = quotes
    set_items([])
    set_records(list(RECS.values()))
    obj, status = call()
    assert status == 200 and obj["data_status"] == "empty" and obj["stocks"] == []


def test_unavailable_is_not_empty(quotes):
    """自选非空 + 行情源全空 + 降级也失败 —— 必须报 unavailable，绝不能报 empty。"""
    set_items, set_records, set_quote_only, call = quotes
    set_items(ITEMS)
    set_records([])
    set_quote_only(lambda code, name_fallback="": None)
    obj, status = call()
    assert status == 200
    assert obj["data_status"] == "unavailable"
    assert obj["data_status"] != "empty"


def test_degraded_falls_back_to_single_quote(quotes):
    set_items, set_records, set_quote_only, call = quotes
    set_items(ITEMS)
    set_records([])

    def fake_quote(code, name_fallback=""):
        return {"name": name_fallback or "行情名", "code": code, "ind": "",
                "price": "¥1.00", "chg": "+1.00%", "chg_num": 1.0, "dir": "up",
                "mcap": None, "mcap_disp": "", "fcap": None, "fcap_disp": ""}

    set_quote_only(fake_quote)
    obj, status = call()
    assert status == 200
    assert obj["data_status"] == "degraded"
    # 快照名用于兜底；行业/市值缺失时留空（由前端不渲染该列）
    assert obj["stocks"][0]["name"] == "贵州茅台"
    assert obj["stocks"][0]["ind"] == ""
    assert obj["stocks"][0]["mcap_disp"] == ""


def test_market_source_is_surfaced_for_labeling(quotes):
    """自选本身正常时，行情仍可能来自备用源/旧缓存/离线快照。

    必须把市场层级单独透出（market_status / market_updated），
    否则前端会把过期价格当实时价格展示。
    """
    set_items, set_records, _, call = quotes
    set_items(ITEMS)
    set_records(list(RECS.values()), market_status="snapshot",
                meta={"updated": "2026-08-30 20:50"})
    obj, status = call()
    assert status == 200
    assert obj["data_status"] == "ok"                       # 自选读取本身正常
    assert obj["market_status"] == "snapshot"               # 但行情来自快照
    assert obj["market_updated"] == "2026-08-30 20:50"


def test_401_and_502_are_forwarded(quotes):
    set_items, _, _, call = quotes
    set_items([], 401)
    obj, status = call()
    assert status == 401 and obj["data_status"] == "unavailable"

    set_items([], 502)
    obj, status = call()
    assert status == 502 and obj["data_status"] == "unavailable"


def test_degrade_limit_caps_external_calls(quotes, monkeypatch):
    """降级路径逐只取价，必须有上限，避免一个请求打爆上游。"""
    set_items, set_records, set_quote_only, call = quotes
    monkeypatch.setattr(fe, "WATCH_DEGRADE_MAX", 2)
    set_items([{"code": c, "name": ""} for c in ("600519", "000001", "300750", "601318")])
    set_records([])
    seen = []

    def fake_quote(code, name_fallback=""):
        seen.append(code)
        return {"name": code, "code": code, "ind": "", "price": "¥1.00", "chg": "0.00%",
                "chg_num": 0.0, "dir": "flat", "mcap": None, "mcap_disp": "",
                "fcap": None, "fcap_disp": ""}

    set_quote_only(fake_quote)
    obj, _ = call()
    assert seen == ["600519", "000001"]      # 只取前 2 只
    assert obj["total"] == 2
