# -*- coding: utf-8 -*-
"""行业对比报告「竞品识别」的市场快照链路测试（N-16）。

事故（2026-09-25）：`industry._PROJECT_ROOT` 少算一层 —— 本文件在
`src/fundamental_agent/report_types/` 下，取 3 次 parent 只到 `<repo>/src`，
于是 `_STOCKS_JSON` = `<repo>/src/frontend/data/stocks.json`（**不存在**），
`_load_market_snapshot()` 恒返回 None，竞品发现静默退化为「实时 clist 全市场分页」，
**本地快照这条主路径从未生效过**。东财 push2delay/push2 被限流当天实时兜底也失败，
用户侧表现为「竞品识别失败：未在市场快照中找到该股票」。

本文件守住三件事：
1. 快照路径必须落在仓库根（不允许再少算层级）；
2. 本地快照缺目标股时要用远程兜底，而不是直接判失败；
3. 远程兜底有 TTL 缓存、失败不抛异常、保留上次结果。

全部离线：远程取数一律 monkeypatch，不触网。
"""
from __future__ import annotations

import pytest

from fundamental_agent.report_types import industry as ind


# ===== 造数据 =====
def _rec(code, name, ind_, mcap, pe=10.0, pb=1.0):
    return {"code": code, "name": name, "ind": ind_, "mcap": mcap, "pe": pe, "pb": pb}


@pytest.fixture(autouse=True)
def _reset_remote_cache(monkeypatch):
    """每个用例都从空的远程快照缓存开始，避免 TTL 缓存跨用例串味。"""
    monkeypatch.setattr(ind, "_REMOTE_SNAP", {"ts": 0.0, "stocks": []})


# ===== 1. 快照路径护栏 =====
def test_project_root_is_repo_root():
    """_PROJECT_ROOT 必须是仓库根，绝不能是 <repo>/src。"""
    root = ind._PROJECT_ROOT
    assert (root / "src" / "fundamental_agent" / "report_types" / "industry.py").is_file()
    assert (root / "frontend" / "data").is_dir()
    assert "src" not in ind._STOCKS_JSON.parts, "_STOCKS_JSON 又落到 src/ 下面了"
    assert ind._STOCKS_JSON == root / "frontend" / "data" / "stocks.json"


def test_source_uses_parents3():
    """源码层面锁死写法：同目录 chart_assets.py 用的就是 parents[3]。"""
    src = (ind._PROJECT_ROOT / "src" / "fundamental_agent" / "report_types"
           / "industry.py").read_text(encoding="utf-8")
    assert "Path(__file__).resolve().parents[3]" in src
    assert "Path(__file__).resolve().parent.parent.parent" not in src


def test_local_snapshot_actually_loads():
    """真正的回归点：修复前这里恒为 None（路径错 → 文件不存在）。"""
    stocks = ind._load_market_snapshot()
    assert stocks, "本地快照读不到 —— 快照路径又漂了"
    assert len(stocks) > 1000
    assert all(k in stocks[0] for k in ("code", "name", "ind", "mcap"))


# ===== 2. 本地命中：不触网 =====
def test_local_hit_never_calls_remote(monkeypatch):
    snap = [_rec("300184", "力源信息", "其他电子", 1.4e10),
            _rec("000062", "深圳华强", "其他电子", 2.0e10)]
    monkeypatch.setattr(ind, "_load_market_snapshot", lambda: list(snap))

    def _boom():
        raise AssertionError("本地命中时不应调用远程快照")

    monkeypatch.setattr(ind, "_fetch_market_snapshot_remote", _boom)
    d = ind.discover_competitors("300184")
    assert d["target"]["code"] == "300184"
    assert [c["code"] for c in d["competitors"]] == ["000062"]


# ===== 3. 本地缺目标股 → 远程兜底 =====
def test_missing_target_falls_back_to_remote(monkeypatch):
    local = [_rec("600001", "甲公司", "银行", 1e10)]
    remote = local + [_rec("830799", "新上市标的", "银行", 5e9)]
    calls = {"n": 0}

    monkeypatch.setattr(ind, "_load_market_snapshot", lambda: list(local))

    def _remote():
        calls["n"] += 1
        return list(remote)

    monkeypatch.setattr(ind, "_fetch_market_snapshot_remote", _remote)
    stocks, tgt = ind._snapshot_find("830799")
    assert calls["n"] == 1, "「快照在、但缺该股」必须触发一次远程兜底"
    assert tgt is not None and tgt["code"] == "830799"
    assert len(stocks) == 2


def test_missing_target_remote_also_missing_returns_empty(monkeypatch):
    """两边都没有 → 返回空 peers（由上层给出可读错误），而不是抛异常。"""
    monkeypatch.setattr(ind, "_load_market_snapshot", lambda: [_rec("600001", "甲公司", "银行", 1e10)])
    monkeypatch.setattr(ind, "_fetch_market_snapshot_remote", lambda: [])
    d = ind.discover_competitors("999999")
    assert d == {"industry": "", "target": None, "competitors": []}


def test_snapshot_file_missing_still_uses_remote(monkeypatch):
    """快照文件整体读不到时的老路径不能退化。"""
    monkeypatch.setattr(ind, "_load_market_snapshot", lambda: None)
    monkeypatch.setattr(ind, "_fetch_market_snapshot_remote",
                        lambda: [_rec("300184", "力源信息", "其他电子", 1.4e10)])
    d = ind.discover_competitors("300184")
    assert d["target"]["code"] == "300184"


# ===== 4. 远程兜底的缓存与容错 =====
def test_remote_snapshot_is_cached(monkeypatch):
    calls = {"n": 0}

    def _remote():
        calls["n"] += 1
        return [_rec("600001", "甲公司", "银行", 1e10)]

    monkeypatch.setattr(ind, "_fetch_market_snapshot_remote", _remote)
    first = ind._remote_snapshot_cached()
    second = ind._remote_snapshot_cached()
    assert calls["n"] == 1, "TTL 内应命中缓存（全量拉取代价很高）"
    assert first == second and first


def test_remote_snapshot_failure_is_soft(monkeypatch):
    def _boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(ind, "_fetch_market_snapshot_remote", _boom)
    assert ind._remote_snapshot_cached() == []


def test_remote_snapshot_keeps_last_good_on_failure(monkeypatch):
    good = [_rec("600001", "甲公司", "银行", 1e10)]
    monkeypatch.setattr(ind, "_REMOTE_SNAP", {"ts": 0.0, "stocks": list(good)})
    monkeypatch.setattr(ind, "_REMOTE_SNAP_TTL_S", -1)      # 强制过期
    monkeypatch.setattr(ind, "_fetch_market_snapshot_remote",
                        lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert ind._remote_snapshot_cached() == good


def test_remote_fetch_includes_push2test_as_last_resort(monkeypatch):
    """主站被限流时三台里只有 push2test 可达，缺了它就是全盘失败。"""
    seen = {}

    def _fake(hosts, path, timeout=10):
        seen["hosts"] = list(hosts)
        return {"data": {"diff": []}}                       # 立刻结束分页

    monkeypatch.setattr(ind, "_curl_json_hosts", _fake)
    ind._fetch_market_snapshot_remote()
    assert seen["hosts"][-1] == "https://push2test.eastmoney.com"
    assert "https://push2delay.eastmoney.com" in seen["hosts"]


# ===== 5. 板块基准分支 =====
def test_board_path_uses_selected_board(monkeypatch):
    members = [_rec("300184", "力源信息", "其他电子", 1.4e10),
               _rec("000062", "深圳华强", "其他电子", 2.0e10)]
    monkeypatch.setattr(ind, "_fetch_board_members", lambda bk: list(members))
    d = ind.discover_competitors("300184", bk="BK0890", bk_name="其他电子")
    assert d["industry"] == "其他电子"
    assert d["target"]["code"] == "300184"
    assert [c["code"] for c in d["competitors"]] == ["000062"]


def test_board_without_target_backfills_from_snapshot(monkeypatch):
    """板块清单偶发未含目标股（同步延迟）→ 用市场快照补目标信息。"""
    members = [_rec("000062", "深圳华强", "其他电子", 2.0e10)]
    monkeypatch.setattr(ind, "_fetch_board_members", lambda bk: list(members))
    monkeypatch.setattr(ind, "_load_market_snapshot",
                        lambda: [_rec("300184", "力源信息", "其他电子", 1.4e10)])
    d = ind.discover_competitors("300184", bk="BK0890", bk_name="其他电子")
    assert d["target"]["code"] == "300184"
    assert d["target"]["name"] == "力源信息"


def test_board_failure_falls_back_to_industry_screen(monkeypatch):
    """板块接口失败（如东财被限流）→ 回退按东财行业筛市值前 N。"""
    snap = [_rec("300184", "力源信息", "其他电子", 1.4e10)]
    snap += [_rec("601%03d" % (100 + i), "P%d" % i, "其他电子", 1e10 * (i + 1))
             for i in range(8)]
    monkeypatch.setattr(ind, "_fetch_board_members", lambda bk: [])
    monkeypatch.setattr(ind, "_load_market_snapshot", lambda: list(snap))

    d = ind.discover_competitors("300184", bk="BK0890", bk_name="其他电子")
    codes = [c["code"] for c in d["competitors"]]
    mcaps = [c["mcap"] for c in d["competitors"]]
    assert len(codes) == ind.COMPETITOR_N
    assert mcaps == sorted(mcaps, reverse=True)
    assert "300184" not in codes
    assert d["target"]["code"] == "300184"


def test_illegal_bk_is_ignored(monkeypatch):
    def _boom(bk):
        raise AssertionError("非法板块码不应发起板块请求")

    monkeypatch.setattr(ind, "_fetch_board_members", _boom)
    monkeypatch.setattr(ind, "_load_market_snapshot",
                        lambda: [_rec("300184", "力源信息", "其他电子", 1.4e10),
                                 _rec("000062", "深圳华强", "其他电子", 2.0e10)])
    d = ind.discover_competitors("300184", bk="../../etc/passwd", bk_name="x")
    assert d["industry"] == "其他电子"          # 走行业筛选，而非被当成板块


def test_industry_display_strips_roman_suffix(monkeypatch):
    """行业名去掉申万二级罗马数字后缀，与前端展示口径一致。"""
    monkeypatch.setattr(ind, "_load_market_snapshot",
                        lambda: [_rec("300184", "力源信息", "元件Ⅱ", 1.4e10),
                                 _rec("000062", "深圳华强", "元件Ⅱ", 2.0e10)])
    d = ind.discover_competitors("300184")
    assert d["industry"] == "元件"
