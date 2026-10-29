# -*- coding: utf-8 -*-
"""K 线质量护栏（N-15）的随仓回归用例。

由来：全市场日/周/月 K 曾长期由 `push2test`（测试主机）供给，而它**只对最近约 4 个
交易日返回真实数据、更早的历史是随机占位样本**（单日 ±267%、成交量个位数、时间轴被
抽样压扁）。当时全链路只有"非空即采纳"，脏数据一路进缓存、进图表，直到用户截图才发现。
本文件把两侧的防回归断言一起固化：**服务端判据 + 挑选逻辑 + 主机池硬护栏 + 前端三态**。
**零依赖（只用标准库）、零网络** —— 所有用例都用合成序列。

对照证据与阈值标定见 `docs/K线数据异常定位报告与修复方案.md`。
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
if str(FRONTEND) not in sys.path:
    sys.path.insert(0, str(FRONTEND))

import server  # noqa: E402
import fetch_stocks  # noqa: E402


# ===== 构造工具 =====

def mk_series(n=130, start=None, price=10.0, step=0.05, gap_at=None, gap_days=40):
    """生成一段正常的日 K 序列（日期为连续工作日，涨跌幅极小）。

    gap_at: 需要插入长间隔的那几根的下标集合（模拟停牌 / 抽样缺根）。
    """
    start = start or datetime.date(2026, 1, 5)
    dates, candle, vols = [], [], []
    d = start
    price = float(price)
    for i in range(n):
        if gap_at and i in gap_at:
            d = d + datetime.timedelta(days=gap_days)
        else:
            d = d + datetime.timedelta(days=1)
            while d.weekday() >= 5:
                d = d + datetime.timedelta(days=1)
        o = round(price, 2)
        c = round(price + step, 2)
        hi = round(max(o, c) + 0.10, 2)
        lo = round(min(o, c) - 0.10, 2)
        dates.append(d.isoformat())
        candle.append([o, c, lo, hi])
        vols.append(10000 + i)
        price = c
    return {"dates": dates, "candle": candle, "vols": vols}


def ck(data, klt=101, n=600, peer=None):
    return server.kline_quality_check(data, klt, n, peer=peer)


# ===== 1. 主机池与排序：根因层面的硬护栏 =====

def test_kline_hosts_excludes_push2test():
    """K 线主机池绝不能含 push2test（它只对当前值真实，历史序列是占位样本）。"""
    assert not any("push2test" in h for h in server.KLINE_HOSTS), \
        "KLINE_HOSTS 混入了 push2test —— 这正是 N-15 的根因"


def test_kline_hosts_prefers_history_host():
    """历史专用主机必须排在候选首位。"""
    assert server.KLINE_HOSTS, "KLINE_HOSTS 不能为空"
    assert "push2his" in server.KLINE_HOSTS[0], \
        "KLINE_HOSTS 首位应为 push2his（历史行情专用主机）"


def test_ordered_hosts_prefer_big_page_flag_preserves_order():
    """prefer_big_page=False 时保持调用点给定顺序 —— K 线等历史路径依赖这一点。"""
    ordered = fetch_stocks.ordered_hosts(server.KLINE_HOSTS, prefer_big_page=False)
    assert ordered == list(server.KLINE_HOSTS)


def test_ordered_hosts_prefer_big_page_keeps_clist_behaviour():
    """prefer_big_page=True（默认）时大页主机优先 —— clist 分页行为不能被误改。"""
    ordered = fetch_stocks.ordered_hosts(server.PUSH2_HOSTS, prefer_big_page=True)
    assert ordered[0] == "https://push2test.eastmoney.com"
    # 默认参数必须与显式 True 等价（否则调用点会静默换行为）
    assert fetch_stocks.ordered_hosts(server.PUSH2_HOSTS) == ordered


def test_kline_eastmoney_does_not_prefer_big_page():
    """取历史序列的调用点必须显式关掉「大页优先」。"""
    import inspect
    src = inspect.getsource(server._kline_eastmoney)
    assert "prefer_big_page=False" in src, \
        "_kline_eastmoney 未关掉 prefer_big_page，push2test 会重新排到首位"


# ===== 2. 结构类判据：整份序列 =====

def test_clean_series_passes():
    ok, det = ck(mk_series())
    assert ok, det["reason"]
    assert det["warnings"] == []
    assert det["jumps"] == 0


@pytest.mark.parametrize("data,keyword", [
    ({"dates": [], "candle": [], "vols": []}, "空"),
    ({"dates": ["2026-01-05"], "candle": [[1, 1, 1, 1]], "vols": [1, 2]}, "长度不齐"),
    ({"dates": ["2026-01-06", "2026-01-05"], "candle": [[1, 1, 1, 1]] * 2, "vols": [1, 1]}, "递增"),
    ({"dates": ["20260105"], "candle": [[1, 1, 1, 1]], "vols": [1]}, "日期格式"),
    ({"dates": ["2026-01-05"], "candle": [[1, 1, 1, 1, 1]], "vols": [1]}, "OHLC 结构"),
    ({"dates": ["2026-01-05"], "candle": [["x", 1, 1, 1]], "vols": [1]}, "非数值"),
])
def test_structural_rejections(data, keyword):
    ok, det = ck(data)
    assert not ok, "%s 应被拒绝" % keyword
    assert keyword in det["reason"]


def test_ohlc_contradiction_rejected():
    """low 高于 min(开,收) —— push2test 上真实出现过（o=4.22 c=4.26 lo=4.26 hi=4.26）。"""
    d = mk_series(5)
    d["candle"][2] = [4.22, 4.26, 4.26, 4.26]
    ok, det = ck(d)
    assert not ok and "OHLC" in det["reason"]


# ===== 3. 值域类判据：只作用于可视窗口 =====

def test_oscillating_two_level_pollution_rejected():
    """在 6 元与 22 元两档间反复横跳（N-15 的典型形态）必须被拦下。"""
    d = mk_series(40)
    for k, i in enumerate(range(10, 40, 4)):
        lvl = 22.0 if k % 2 == 0 else 6.0
        d["candle"][i] = [lvl, lvl, lvl - 0.05, lvl + 0.05]
    ok, det = ck(d)
    assert not ok
    assert "混排" in det["reason"]
    assert det["jumps"] >= server.KLINE_JUMP_MAX_COUNT


def test_three_absurd_jumps_rejected():
    """多根不可能的单日涨跌（A 股上限 30%）必须被拦下。"""
    d = mk_series(60)
    for i in (20, 30, 40):
        p = d["candle"][i - 1][1]
        d["candle"][i] = [round(p * 1.49, 2), round(p * 1.49, 2),
                          round(p * 1.48, 2), round(p * 1.50, 2)]
    ok, det = ck(d)
    assert not ok
    assert det["jumps"] >= server.KLINE_JUMP_MAX_COUNT


def test_single_absurd_jump_survives_structure_but_dies_on_cross_check():
    """单根不可能的跳变由**跨源一致性**兜住 —— 结构判据为兼容复牌股只按计数拒（≥3）。

    这是刻意的分层：结构判据宁可漏一个，也不能把真实复牌错杀；而只要对照源在场，
    任何一根错值都会被逐根比对抓出（实测两源在可视窗口内偏差恒 <0.6%）。
    """
    dirty = mk_series(60)
    p = dirty["candle"][29][1]
    dirty["candle"][30] = [round(p * 1.49, 2)] * 4
    ok, _ = ck(dirty)
    assert ok, "单根跳变不应被结构判据拒绝（避免误杀复牌股）"
    picked, det = server._kline_pick({"eastmoney": dirty, "tencent": mk_series(60)}, 101, 600)
    assert picked is not None and picked["source"] == "tencent", "应改选通过交叉校验的源"
    assert det["eastmoney"]["warnings"], "错值根应在跨源校验里被记录"


def test_single_legit_jump_allowed():
    """长期停牌复牌（一次性整体跳空）不应被误杀 —— 计数阈值是 ≥3 而非 ≥1。"""
    d = mk_series(60)
    base = d["candle"][29][1]
    for i in range(30, len(d["candle"])):
        for j in range(4):
            d["candle"][i][j] = round(d["candle"][i][j] * 1.5, 2)
    d["candle"][30] = [round(base, 2), round(base * 1.5, 2), round(base, 2), round(base * 1.5, 2)]
    ok, det = ck(d)
    assert ok, det["reason"]
    assert det["jumps"] == 1


def test_nonpositive_price_inside_window_rejected():
    d = mk_series(30)
    d["candle"][25] = [-2.07, -1.78, -2.13, -1.78]
    ok, det = ck(d)
    assert not ok and "非正价格" in det["reason"]


def test_nonpositive_price_outside_window_is_note_not_reject():
    """可视窗口之外的前复权非正价（如 002048 宁波华翔 2006 年）不该判死整份数据。"""
    d = mk_series(240)
    for i in range(5):
        d["candle"][i] = [-2.07, -1.78, -2.13, -1.78]
    ok, det = ck(d)
    assert ok, det["reason"]
    assert det["neg_outside"] == 5
    assert det["notes"], "窗口之外的非正价必须留下记录"
    assert det["warnings"] == [], "不应把窗口之外的历史伪影升级为 degraded"


def test_long_gaps_are_warning_not_reject():
    """长期停牌股（如 002450）在周/月 K 上有数处长间隔 —— 只能是提示，不能判不可用。"""
    d = mk_series(60, gap_at={10, 25, 40}, gap_days=120)
    ok, det = ck(d)
    assert ok, det["reason"]
    assert det["gaps"] >= server.KLINE_GAP_MAX_COUNT
    assert det["warnings"], "缺根应给出提示"


# ===== 4. 跨源交叉校验 =====

def test_xcheck_row_count_mismatch_rejected():
    """占位样本只给 24 根而真实源给 44 根 —— 行数比过小即判异常（实测两源恒为 1:1）。"""
    a = mk_series(24, price=100.0)
    b = mk_series(44, price=100.0)
    ok, _ = server.kline_xcheck(a, b, 101, 600)
    assert not ok


def test_xcheck_value_divergence_rejected():
    peer = mk_series(130)
    dirty = mk_series(130)
    for i in range(len(dirty["candle"])):
        c = dirty["candle"][i][1]
        dirty["candle"][i] = [round(c * 0.7, 2)] * 4      # 整体错档
    ok, out = server.kline_xcheck(dirty, peer, 101, 600)
    assert not ok and out["warnings"]


def test_xcheck_agreement_passes():
    a = mk_series(130)
    b = mk_series(130)
    ok, out = server.kline_xcheck(a, b, 101, 600)
    assert ok and out["warnings"] == []


def test_xcheck_insufficient_is_note_not_reject():
    """新上市个股可比根数很少 —— 不能因信息不足误杀。"""
    a = mk_series(3, price=100.0)
    b = mk_series(3, price=100.0)
    ok, out = server.kline_xcheck(a, b, 103, 400)
    assert ok
    assert out["xcheck"] == "insufficient"
    assert out["notes"] and not out["warnings"]


def test_xcheck_ignores_ancient_near_zero_region():
    """回归护栏：跨源校验必须限定可视窗口。

    反例（未限窗口时会误判）：高分红老股的前复权远古段价格趋于 0，两源 0.02 vs 0.20
    就能算出 900% 的"偏差"，把正常数据判死。
    """
    long_a = mk_series(300, price=0.02)
    long_b = mk_series(300, price=0.20)
    # 只让远古段不同；最近 120 根对齐
    for i in range(180, 300):
        long_b["candle"][i] = list(long_a["candle"][i])
        long_b["dates"][i] = long_a["dates"][i]
    ok, out = server.kline_xcheck(long_a, long_b, 103, 400)
    assert ok, "远古近零价区间不应参与跨源判定"
    assert out["xcheck"].endswith("根偏差>%.1f%%" % server.KLINE_XCHECK_MAX_DEV)
    assert out["xcheck"].startswith("0/"), "可视窗口内应判为完全一致"


# ===== 5. 挑选逻辑 =====

def test_pick_prefers_clean_source_over_dirty_high_priority_one():
    """脏源放在高优先级位（eastmoney）也必须被剔除，不能因为"先到"或"位置靠前"而获胜。"""
    dirty = mk_series(80)
    for k, i in enumerate(range(10, 80, 4)):
        lvl = 22.0 if k % 2 == 0 else 6.0
        dirty["candle"][i] = [lvl, lvl, lvl - 0.05, lvl + 0.05]
    clean = mk_series(600)
    picked, det = server._kline_pick({"eastmoney": dirty, "tencent": clean}, 101, 600)
    assert picked is not None
    assert picked["source"] == "tencent"
    assert det["eastmoney"]["reason"]


def test_pick_returns_none_when_all_sources_rejected():
    bad = {"dates": ["2026-01-05"], "candle": [[1, 1, 1, 1, 1]], "vols": [1]}
    picked, det = server._kline_pick({"eastmoney": bad, "tencent": bad}, 101, 600)
    assert picked is None
    assert det


def test_pick_single_source_uses_structural_check():
    """只有一个源可用时跳过跨源步骤，但结构校验仍然生效。"""
    clean = mk_series(200)
    picked, det = server._kline_pick({"tencent": clean}, 101, 600)
    assert picked is not None and picked["source"] == "tencent"
    assert det["tencent"]["xcheck"] == "skipped"


def test_pick_returns_quality_metadata():
    picked, _ = server._kline_pick({"eastmoney": mk_series(200)}, 101, 600)
    assert "quality" in picked and "checked" in picked["quality"]


# ===== 5b. 复权口径：混用不同口径必须显式声明并告警 =====

@pytest.mark.parametrize("fn_name,expected", [
    ("_kline_eastmoney", "qfq"), ("_kline_tencent", "qfq"), ("_kline_sina", "none"),
])
def test_sources_declare_fq(fn_name, expected):
    """每个源必须声明自己的复权口径（新浪是不复权，实证见 _kline_sina 注释）。"""
    import inspect
    src = inspect.getsource(getattr(server, fn_name))
    assert '"fq": "%s"' % expected in src, "%s 未声明 fq=%s" % (fn_name, expected)


def test_fq_mismatch_raises_warning():
    d = server._kline_tag_fq({"dates": [], "candle": [], "vols": [],
                              "fq": "none", "quality": {"warnings": []}})
    assert d["quality"]["fq"] == "none"
    assert any("不复权" in w for w in d["quality"]["warnings"])


def test_fq_qfq_no_warning():
    d = server._kline_tag_fq({"dates": [], "candle": [], "vols": [],
                              "fq": "qfq", "quality": {"warnings": []}})
    assert d["quality"]["warnings"] == []


def test_pick_marks_non_qfq_source_as_degraded():
    sina_like = mk_series(130)
    sina_like["fq"] = "none"
    picked, _ = server._kline_pick({"sina": sina_like}, 101, 600)
    assert picked is not None
    assert picked["quality"]["warnings"], "不复权源必须带告警"


def test_pick_prefers_qfq_over_unadjusted_when_both_available():
    """东财(前复权) 与 新浪(不复权) 同时可用时选东财 —— 口径一致者优先。"""
    em = mk_series(130)
    em["fq"] = "qfq"
    sina_like = mk_series(130)
    sina_like["fq"] = "none"
    picked, _ = server._kline_pick({"eastmoney": em, "sina": sina_like}, 101, 600)
    assert picked is not None and picked["source"] == "eastmoney"


# ===== 6. 接口契约：不可用一律 200 + data_status，绝不合成数据 =====

def test_api_kline_unavailable_returns_status_not_5xx(monkeypatch):
    def boom(code, klt, n):
        raise fetch_stocks.MarketSourceUnavailable("模拟全部源被拒")
    monkeypatch.setattr(server, "fetch_kline", boom)
    obj, status = server.handle_api_kline({"code": ["603050"]})
    assert status == 200, "不可用态应由 data_status 表达，不能返回 5xx"
    assert obj["data_status"] == "unavailable"
    assert obj["dates"] == [] and obj["candle"] == [] and obj["vols"] == []


def test_api_kline_ok_passes_through_status(monkeypatch):
    monkeypatch.setattr(server, "fetch_kline",
                        lambda code, klt, n: {**mk_series(30), "source": "eastmoney",
                                              "quality": {"warnings": []}})
    obj, status = server.handle_api_kline({"code": ["603050"], "klt": ["101"], "n": ["600"]})
    assert status == 200 and obj["data_status"] == "ok"
    assert obj["data_updated"] and obj["data_updated"].startswith("2026-")


def test_api_kline_degraded_when_warnings(monkeypatch):
    monkeypatch.setattr(server, "fetch_kline",
                        lambda code, klt, n: {**mk_series(30), "source": "tencent",
                                              "quality": {"warnings": ["取样存疑"]}})
    obj, _ = server.handle_api_kline({"code": ["603050"]})
    assert obj["data_status"] == "degraded"


def test_api_kline_missing_code():
    obj, status = server.handle_api_kline({})
    assert status == 400 and obj["data_status"] == "unavailable"


def test_cache_helper_clears():
    server._kline_cache_put(("TEST", 101, 600), mk_series(5))
    assert server._kline_cache_get(("TEST", 101, 600)) is not None
    server._kline_cache_clear()
    assert server._kline_cache_get(("TEST", 101, 600)) is None


# ===== 7. 前端三态：不得再出现合成数据 =====

@pytest.fixture(scope="module")
def detail_html():
    return (FRONTEND / "stock_detail.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("token", [
    "function genData", "function mulberry32", "function fmtDateISO",
    'id="kDemo"', "demo:true", "genData(",
])
def test_frontend_no_longer_synthesises_kline(detail_html, token):
    """合成数据兜底的代码与调用点必须彻底消失（注释里提到名字不算，故按唯一形态断言）。"""
    assert token not in detail_html, \
        "K 线前端仍残留合成数据兜底痕迹：%s（信任类问题，必须删除）" % token


def test_frontend_reads_data_status(detail_html):
    assert "data_status" in detail_html
    assert "unavailable" in detail_html and "degraded" in detail_html


def test_frontend_has_kline_hint_element(detail_html):
    assert 'id="kHint"' in detail_html
    assert "setKlineHint" in detail_html
    assert "khint" in detail_html


def test_frontend_does_not_treat_kline_error_as_chart_data(detail_html):
    """请求失败分支必须是"标不可用"，而不是"造数据"。"""
    assert "klineUnavailable" in detail_html
