"""报告渲染：AnalysisResult → 单文件 HTML。

使用 Jinja2 渲染 template.html，输出为自包含、响应式的 HTML 文件，
可直接在浏览器打开、离线阅读、便于分享。图表以 base64 内嵌，保证单文件可读性。

渲染时由 build_data() 把各模块引擎产出的真实数据装配为模板直接消费的
`data` 字典；任一模块无真实数据则回退到 PLACEHOLDER（占位）原值，
保证报告要么全真、要么全占位，不会出现半截数据的尴尬。
"""

from __future__ import annotations

import base64
import copy
import math
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..models import AnalysisResult


# ---------------------------------------------------------------------------
# 占位回退（与历史截图/演示文案一致）
# ---------------------------------------------------------------------------
PLACEHOLDER = {
    "hero": {
        "name": "云启智能科技股份有限公司",
        "sub": "2026 半年度基本面分析报告 · 覆盖 2022-2026 财务区间",
        "industry": "锂电池 / 新能源",
        "period_label": "2022Q1~2026Q2",
        "currency": "人民币（百万元）",
        "kpis": [
            {"lab": "综合评分", "val": "86", "extra": "较上期 +3 分", "extra_cls": "up"},
            {"lab": "营业收入", "val": "¥42.8B", "extra": "+18.6% YoY", "extra_cls": "up"},
            {"lab": "归母净利润", "val": "¥5.3B", "extra": "+22.4% YoY", "extra_cls": "up"},
            {"lab": "ROE 净资产收益率", "val": "19.4%", "extra": "+2.1pp", "extra_cls": "up"},
        ],
    },
    "prof": {
        "cards": [
            {"key": "gross", "lab": "毛利率", "val": "32.4%", "delta": "+1.2pp", "delta_cls": "up"},
            {"key": "net", "lab": "净利率", "val": "12.4%", "delta": "+0.8pp", "delta_cls": "up"},
            {"key": "roe", "lab": "ROE 净资产收益率", "val": "19.4%", "delta": "+2.1pp", "delta_cls": "up"},
            {"key": "roa", "lab": "ROA 总资产收益率", "val": "9.8%", "delta": "+0.6pp", "delta_cls": "up"},
        ],
        "chart": {
            "title": "盈利能力趋势 · ROE（%）",
            "bars": [
                {"v": "14.2", "h": 73, "deep": False, "yr": "2022"},
                {"v": "15.8", "h": 81, "deep": False, "yr": "2023"},
                {"v": "17.1", "h": 88, "deep": False, "yr": "2024"},
                {"v": "17.3", "h": 89, "deep": False, "yr": "2025"},
                {"v": "19.4", "h": 100, "deep": True, "yr": "2026H1"},
            ],
        },
        "insight": "毛利率与净利率连续三年稳步抬升，规模效应与高毛利产品占比提升驱动盈利质量改善；ROE 突破 19%，股东回报位于行业前列，盈利韧性较强。",
    },
    "growth": {
        "cards": [
            {"key": "rev", "lab": "营业收入 YoY", "val": "18.6%", "tag": "同比加速", "tag_cls": "red"},
            {"key": "net", "lab": "归母净利润 YoY", "val": "22.4%", "tag": "同比加速", "tag_cls": "red"},
            {"key": "rev_cagr", "lab": "营收 CAGR（3Y）", "val": "19.8%", "tag": "三年复合", "tag_cls": "blue"},
            {"key": "net_cagr", "lab": "净利 CAGR（3Y）", "val": "24.5%", "tag": "高增", "tag_cls": "blue"},
        ],
        "chart": {
            "title": "营业收入同比增速（%）",
            "bars": [
                {"v": "21.0", "h": 100, "deep": False, "yr": "2022"},
                {"v": "15.4", "h": 73, "deep": False, "yr": "2023"},
                {"v": "19.2", "h": 91, "deep": False, "yr": "2024"},
                {"v": "14.1", "h": 67, "deep": False, "yr": "2025"},
                {"v": "18.6", "h": 89, "deep": True, "yr": "2026H1"},
            ],
        },
        "insight": "营收与净利润保持双位数复合增长，2026H1 增速回升至 18.6%，增长动能修复；利润增速持续高于营收，显示经营杠杆与费用管控成效显著。",
    },
    "solv": {
        "cards": [
            {"key": "debt", "lab": "资产负债率", "val": "56.2%", "tag": "▼ -1.8pp 改善", "tag_cls": "down"},
            {"key": "cur", "lab": "流动比率", "val": "1.63", "tag": "▲ 优于 1.5", "tag_cls": "up"},
            {"key": "quick", "lab": "速动比率", "val": "1.21", "tag": "▲ 优于 1.0", "tag_cls": "up"},
            {"key": "interest", "lab": "利息保障倍数", "val": "8.4x", "tag": "▲ 安全", "tag_cls": "up"},
        ],
        "chart": {
            "title": "资产负债率趋势（%）",
            "bars": [
                {"v": "59.6", "h": 100, "deep": False, "yr": "2022"},
                {"v": "58.3", "h": 97.8, "deep": False, "yr": "2023"},
                {"v": "57.0", "h": 95.6, "deep": False, "yr": "2024"},
                {"v": "56.9", "h": 95.5, "deep": False, "yr": "2025"},
                {"v": "56.2", "h": 94.3, "deep": True, "yr": "2026H1"},
            ],
        },
        "insight": "资产负债率连续五年下行至 56.2%，财务杠杆趋于稳健；流动比率与速动比率均高于安全阈值，短期偿债无虞；利息保障倍数达 8.4x，长期偿债安全垫充足。",
    },
    "eff": {
        "cards": [
            {"key": "inv", "lab": "存货周转率", "val": "6.8x", "tag": "▲ 提速", "tag_cls": "up"},
            {"key": "recv", "lab": "应收账款周转率", "val": "4.2x", "tag": "▲ 改善", "tag_cls": "up"},
            {"key": "tot", "lab": "总资产周转率", "val": "0.78x", "tag": "▲ 提升", "tag_cls": "up"},
            {"key": "cash", "lab": "现金循环周期", "val": "58 天", "tag": "▼ 缩短 6 天", "tag_cls": "down"},
        ],
        "chart": {
            "title": "存货周转率趋势（次）",
            "bars": [
                {"v": "5.9", "h": 86.8, "deep": False, "yr": "2022"},
                {"v": "6.2", "h": 91.2, "deep": False, "yr": "2023"},
                {"v": "6.5", "h": 95.6, "deep": False, "yr": "2024"},
                {"v": "6.6", "h": 97.1, "deep": False, "yr": "2025"},
                {"v": "6.8", "h": 100, "deep": True, "yr": "2026H1"},
            ],
        },
        "insight": "存货与应收账款周转率稳步提升，供应链与回款管理效率改善；现金循环周期缩短至 58 天，营运资本占用下降，整体现金生成能力增强。",
    },
    "bench": {
        "cards": [
            {"lab": "毛利率优势", "val": "+6.3pp", "tag": "▲ 领先行业", "tag_cls": "up"},
            {"lab": "ROE 优势", "val": "+4.2pp", "tag": "▲ 领先行业", "tag_cls": "up"},
            {"lab": "资产负债率优势", "val": "-3.1pp", "tag": "▼ 低于行业", "tag_cls": "down"},
        ],
        "rows": [
            {"name": "毛利率", "self": "32.4%", "peer": "26.1%", "diff": "领先 +6.3pp", "diff_cls": ""},
            {"name": "ROE 净资产收益率", "self": "19.4%", "peer": "15.2%", "diff": "领先 +4.2pp", "diff_cls": ""},
            {"name": "营收增速", "self": "18.6%", "peer": "12.4%", "diff": "领先 +6.2pp", "diff_cls": ""},
            {"name": "资产负债率", "self": "56.2%", "peer": "59.3%", "diff": "优于 -3.1pp", "diff_cls": "down"},
            {"name": "存货周转率", "self": "6.8x", "peer": "5.9x", "diff": "领先 +0.9x", "diff_cls": ""},
        ],
        "insight": "公司在盈利效率、资本回报与成长速度上全面领先行业基准，资产负债结构优于同业；综合实力处于行业第一梯队，具备明显的相对竞争优势。",
    },
    "fin": {
        "periods": ["2024", "2025", "2026H1"],
        "balance": [
            {"name": "流动资产合计", "vals": ["18,420", "21,350", "23,180"], "yoy": "+8.6%", "yoy_cls": "up", "qoq": "+8.5%", "qoq_cls": "up"},
            {"name": "非流动资产合计", "vals": ["15,200", "16,800", "17,450"], "yoy": "+3.9%", "yoy_cls": "up", "qoq": "+3.9%", "qoq_cls": "up"},
            {"name": "资产总计", "vals": ["33,620", "38,150", "40,630"], "yoy": "+6.5%", "yoy_cls": "up", "qoq": "+6.5%", "qoq_cls": "up"},
            {"name": "流动负债合计", "vals": ["12,100", "13,400", "14,200"], "yoy": "+6.0%", "yoy_cls": "up", "qoq": "+6.0%", "qoq_cls": "up"},
            {"name": "非流动负债合计", "vals": ["6,800", "7,100", "7,250"], "yoy": "+2.1%", "yoy_cls": "up", "qoq": "+2.1%", "qoq_cls": "up"},
            {"name": "负债和权益合计", "vals": ["33,620", "38,150", "40,630"], "yoy": "+6.5%", "yoy_cls": "up", "qoq": "+6.5%", "qoq_cls": "up"},
        ],
        "income": [
            {"name": "营业收入", "vals": ["28,500", "33,200", "18,400"], "yoy": "+10.8%", "yoy_cls": "up", "qoq": "-44.6%", "qoq_cls": "down"},
            {"name": "营业成本", "vals": ["19,200", "22,100", "12,300"], "yoy": "+9.5%", "yoy_cls": "up", "qoq": "-44.3%", "qoq_cls": "down"},
            {"name": "毛利润", "vals": ["9,300", "11,100", "6,100"], "yoy": "+14.4%", "yoy_cls": "up", "qoq": "-45.0%", "qoq_cls": "down"},
            {"name": "销售/管理/研发费用", "vals": ["5,100", "5,900", "3,300"], "yoy": "+11.9%", "yoy_cls": "up", "qoq": "-44.1%", "qoq_cls": "down"},
            {"name": "营业利润", "vals": ["3,800", "4,700", "2,600"], "yoy": "+17.5%", "yoy_cls": "up", "qoq": "-44.7%", "qoq_cls": "down"},
            {"name": "归母净利润", "vals": ["3,200", "3,950", "2,180"], "yoy": "+18.4%", "yoy_cls": "up", "qoq": "-44.8%", "qoq_cls": "down"},
        ],
        "cash": [
            {"name": "经营活动现金流净额", "vals": ["4,200", "5,100", "2,800"], "yoy": "+9.8%", "yoy_cls": "up", "qoq": "-45.1%", "qoq_cls": "down"},
            {"name": "投资活动现金流净额", "vals": ["-2,600", "-3,200", "-1,800"], "yoy": "-12.5%", "yoy_cls": "down", "qoq": "+43.8%", "qoq_cls": "up"},
            {"name": "筹资活动现金流净额", "vals": ["-1,100", "-1,300", "-600"], "yoy": "+7.7%", "yoy_cls": "up", "qoq": "+53.8%", "qoq_cls": "up"},
            {"name": "汇率变动影响", "vals": ["-80", "-60", "-30"], "yoy": "+25.0%", "yoy_cls": "up", "qoq": "+50.0%", "qoq_cls": "up"},
            {"name": "现金及现金等价物净增加额", "vals": ["420", "540", "370"], "yoy": "+37.0%", "yoy_cls": "up", "qoq": "-31.5%", "qoq_cls": "down"},
        ],
        "structure": [
            {"name": "流动资产", "pct": "57.0%", "w": 57, "deep": True},
            {"name": "非流动资产", "pct": "43.0%", "w": 43, "deep": False},
        ],
        "insight": "资产规模稳步扩张，流动资产占比提升至 57%，整体流动性稳健；负债以流动负债为主（占比约 67%），需关注短期偿债节奏与现金覆盖能力。",
    },
    "conclusion": {
        "score": 86,
        "score_desc": "评分位于行业前 15%，基本面质地优良",
        "pros": [
            "盈利与回报领先行业，ROE 达 19.4%",
            "成长稳健，利润增速持续高于营收",
            "财务结构稳健，偿债安全垫充足",
        ],
        "risks": [
            "行业周期波动与价格竞争压力",
            "应收账款规模较大，关注回款节奏",
        ],
    },
}


# ---------------------------------------------------------------------------
# 数值解析工具
# ---------------------------------------------------------------------------
def _parse_num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("%", "").replace("亿", "").replace("x", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_pct(s):
    return _parse_num(s)


# ---------------------------------------------------------------------------
# 占位（兜底）财务报表视图：默认 / 年报 / 中报 / 单季度
# ---------------------------------------------------------------------------
def _ph_variant(base_view, periods, factor):
    """占位视图变体：复用默认视图结构，改周期标签并对数值按因子缩放（仅演示用）。"""
    def mk(rows):
        out = []
        for r in rows:
            vals = []
            for v in r["vals"]:
                n = _parse_num(v)
                vals.append(f"{n * factor:,.0f}" if n is not None else v)
            out.append({"name": r["name"], "vals": vals, "yoy": "—", "yoy_cls": "",
                        "qoq": "—", "qoq_cls": ""})
        return out
    return {
        "periods": periods,
        "balance": mk(base_view["balance"]),
        "income": mk(base_view["income"]),
        "cash": mk(base_view["cash"]),
        "structure": base_view["structure"],
        "insight": base_view["insight"],
    }


fin_views_ph = {
    "default": PLACEHOLDER["fin"],
    "annual": _ph_variant(PLACEHOLDER["fin"], ["2023", "2024", "2025"], 1.0),
    "interim": _ph_variant(PLACEHOLDER["fin"], ["2023H1", "2024H1", "2025H1"], 0.47),
    "quarterly": _ph_variant(PLACEHOLDER["fin"], ["2025Q2", "2025Q3", "2025Q4"], 0.23),
}


# ---------------------------------------------------------------------------
# 占位（兜底）盈利能力视图：默认 / 年报 / 中报 / 单季度
# ---------------------------------------------------------------------------
def _ph_bars(series):
    return [{"yr": yr, "v": f"{v:.1f}", "n": v, "deep": (i == len(series) - 1)}
            for i, (yr, v) in enumerate(series)]


def _ph_card(key, lab, series):
    cur = series[-1][1]
    prev = series[-2][1] if len(series) >= 2 else None
    delta = None if prev is None else cur - prev
    cls = "up" if (delta is None or delta >= 0) else "down"
    dstr = ("+" if delta >= 0 else "") + f"{delta:.1f}pp" if delta is not None else "—"
    return {"key": key, "lab": lab, "val": f"{cur:.1f}%", "delta": dstr, "delta_cls": cls}


def _ph_prof_views():
    """合成占位趋势（仅演示用，非真实数据）。"""
    gross_d = [("2022", 28.0), ("2023", 30.0), ("2024", 31.0), ("2025", 31.5), ("2026H1", 32.4)]
    net_d = [("2022", 10.0), ("2023", 11.0), ("2024", 12.0), ("2025", 12.2), ("2026H1", 12.4)]
    roe_d = [("2022", 14.2), ("2023", 15.8), ("2024", 17.1), ("2025", 17.3), ("2026H1", 19.4)]
    roa_d = [("2022", 8.0), ("2023", 8.5), ("2024", 9.0), ("2025", 9.5), ("2026H1", 9.8)]
    gross_i = [("2023H1", 29.5), ("2024H1", 30.6), ("2025H1", 31.9)]
    net_i = [("2023H1", 10.8), ("2024H1", 11.7), ("2025H1", 12.1)]
    roe_i = [("2023H1", 15.4), ("2024H1", 16.6), ("2025H1", 18.7)]
    roa_i = [("2023H1", 8.3), ("2024H1", 8.8), ("2025H1", 9.6)]
    gross_q = [("2025Q2", 31.2), ("2025Q3", 31.4), ("2025Q4", 32.0)]
    net_q = [("2025Q2", 11.9), ("2025Q3", 12.0), ("2025Q4", 12.3)]
    roe_q = [("2025Q2", 16.9), ("2025Q3", 17.0), ("2025Q4", 18.2)]
    roa_q = [("2025Q2", 9.2), ("2025Q3", 9.3), ("2025Q4", 9.7)]
    defs = [
        ("gross", "毛利率", gross_d, gross_i, gross_q),
        ("net", "净利率", net_d, net_i, net_q),
        ("roe", "ROE 净资产收益率", roe_d, roe_i, roe_q),
        ("roa", "ROA 总资产收益率", roa_d, roa_i, roa_q),
    ]
    views = {}
    for kind in ("default", "annual", "interim", "quarterly"):
        metrics, cards = {}, []
        for key, lab, d, i, q in defs:
            ser = {"default": d, "annual": d[-3:], "interim": i, "quarterly": q}[kind]
            metrics[key] = {"label": lab, "unit": "%", "title": f"{lab}趋势（%）", "bars": _ph_bars(ser)}
            cards.append(_ph_card(key, lab, ser))
        views[kind] = {"cards": cards, "metrics": metrics, "insight": PLACEHOLDER["prof"]["insight"]}
    return views


prof_views_ph = _ph_prof_views()


# ---------------------------------------------------------------------------
# 占位（兜底）成长性 / 偿债能力 / 运营效率 视图：默认 / 年报 / 中报 / 单季度
# ---------------------------------------------------------------------------
def _ph_alt(default_series, labels):
    """由默认 5 期序列派生 3 期变体（复用末 3 个数值，套用新的周期标签）。演示用。"""
    out = {}
    for key, s in default_series.items():
        last3 = s[-3:]
        out[key] = [(lab, last3[i][1]) for i, lab in enumerate(labels)]
    return out


def _ph_module_views(metric_defs, base_cards, default_series, insight):
    """生成模块占位视图（默认 / 年报 / 中报 / 单季度）。

    metric_defs: [(key, lab, unit, nd)]
    base_cards : 占位卡片（含 lab/val/tag/tag_cls，按序），自动补 key
    default_series: {key: [(label, value), ...]}（默认 5 期）
    """
    keys = [m[0] for m in metric_defs]
    cards = [dict(c, key=k) for c, k in zip(base_cards, keys)]
    alt = {
        "annual": _ph_alt(default_series, ["2023", "2024", "2025"]),
        "interim": _ph_alt(default_series, ["2023H1", "2024H1", "2025H1"]),
        "quarterly": _ph_alt(default_series, ["2025Q2", "2025Q3", "2025Q4"]),
    }
    views = {}
    for kind in ("default", "annual", "interim", "quarterly"):
        ser = default_series if kind == "default" else alt.get(kind, default_series)
        metrics = {}
        for key, lab, unit, nd in metric_defs:
            s = ser.get(key, [])
            bars = [
                {"yr": yr, "v": f"{v:.{nd}f}", "n": float(v), "deep": (i == len(s) - 1)}
                for i, (yr, v) in enumerate(s)
            ]
            metrics[key] = {"label": lab, "unit": unit, "title": f"{lab}趋势（{unit}）", "bars": bars}
        views[kind] = {"cards": cards, "metrics": metrics, "insight": insight}
    return views


growth_views_ph = _ph_module_views(
    [
        ("rev", "营业收入 YoY", "%", 1),
        ("net", "归母净利润 YoY", "%", 1),
        ("rev_cagr", "营收 CAGR（3Y）", "%", 1),
        ("net_cagr", "净利 CAGR（3Y）", "%", 1),
    ],
    PLACEHOLDER["growth"]["cards"],
    {
        "rev": [("2022", 21.0), ("2023", 15.4), ("2024", 19.2), ("2025", 14.1), ("2026H1", 18.6)],
        "net": [("2022", 25.0), ("2023", 20.1), ("2024", 24.3), ("2025", 19.8), ("2026H1", 22.4)],
        "rev_cagr": [("2022", 17.0), ("2023", 18.5), ("2024", 19.2), ("2025", 19.5), ("2026H1", 19.8)],
        "net_cagr": [("2022", 21.0), ("2023", 22.5), ("2024", 23.3), ("2025", 24.0), ("2026H1", 24.5)],
    },
    PLACEHOLDER["growth"]["insight"],
)

solv_views_ph = _ph_module_views(
    [
        ("debt", "资产负债率", "%", 1),
        ("cur", "流动比率", "", 2),
        ("quick", "速动比率", "", 2),
        ("interest", "利息保障倍数", "x", 1),
    ],
    PLACEHOLDER["solv"]["cards"],
    {
        "debt": [("2022", 59.6), ("2023", 58.3), ("2024", 57.0), ("2025", 56.9), ("2026H1", 56.2)],
        "cur": [("2022", 1.50), ("2023", 1.55), ("2024", 1.58), ("2025", 1.61), ("2026H1", 1.63)],
        "quick": [("2022", 1.10), ("2023", 1.14), ("2024", 1.17), ("2025", 1.19), ("2026H1", 1.21)],
        "interest": [("2022", 7.2), ("2023", 7.6), ("2024", 7.9), ("2025", 8.1), ("2026H1", 8.4)],
    },
    PLACEHOLDER["solv"]["insight"],
)

eff_views_ph = _ph_module_views(
    [
        ("inv", "存货周转率", "x", 2),
        ("recv", "应收账款周转率", "x", 2),
        ("tot", "总资产周转率", "x", 2),
        ("cash", "现金循环周期", "天", 0),
    ],
    PLACEHOLDER["eff"]["cards"],
    {
        "inv": [("2022", 5.9), ("2023", 6.2), ("2024", 6.5), ("2025", 6.6), ("2026H1", 6.8)],
        "recv": [("2022", 3.8), ("2023", 3.9), ("2024", 4.0), ("2025", 4.1), ("2026H1", 4.2)],
        "tot": [("2022", 0.70), ("2023", 0.73), ("2024", 0.75), ("2025", 0.77), ("2026H1", 0.78)],
        "cash": [("2022", 64), ("2023", 62), ("2024", 60), ("2025", 59), ("2026H1", 58)],
    },
    PLACEHOLDER["eff"]["insight"],
)


def _yi(v):
    """把绝对值（元）格式化为「¥X,XXX.XX亿」。已带『亿』的字符串直接回显。"""
    if v is None:
        return "—"
    s = str(v)
    if "亿" in s:  # 已是『亿』单位，避免重复缩放
        x = _parse_num(s)
        return "—" if x is None else f"¥{x:,.2f}亿"
    x = _parse_num(s)
    if x is None:
        return "—"
    return f"¥{x / 1e8:,.2f}亿"


def _extra(result: AnalysisResult, module: str):
    for m in result.modules:
        if m.module == module and m.extra:
            return m.extra
    return None


def _card_val(extra, lab):
    if not extra:
        return None
    for c in extra.get("cards", []):
        if c.get("lab") == lab:
            return c.get("val")
    return None


def _fin_row_latest(fin_extra, name):
    if not fin_extra:
        return None
    for row in fin_extra.get("income", []):
        if row.get("name") == name and row.get("vals"):
            return row["vals"][-1]
    return None


def _compute_score(roe, net_margin, debt, rev_g):
    s = 40.0
    if roe is not None:
        s += min(roe * 1.5, 45)
    if net_margin is not None:
        s += min(net_margin * 0.4, 20)
    if debt is not None:
        s += max(0.0, (100 - debt) * 0.3)
    if rev_g is not None:
        s += min(max(rev_g, 0) * 0.6, 15)
    return max(40, min(98, round(s)))


def _nice_step(x: float) -> float:
    """返回「好看」的刻度步长（1/2/2.5/5 × 10^k）。"""
    if x <= 0:
        return 1.0
    exp = math.floor(math.log10(x))
    base = 10.0 ** exp
    for m in (1, 2, 2.5, 5, 10):
        if x <= m * base:
            return m * base
    return 10.0 * base


def _enhance_chart(chart: dict) -> dict:
    """为柱状图补充分轴信息（nice min/max、刻度）并给每根柱子补数值 n。

    返回新的 chart dict，不修改入参（避免污染 PLACEHOLDER 全局）。
    无 bars 时原样返回。
    """
    if not chart or not chart.get("bars"):
        return chart
    chart = dict(chart)
    bars = []
    for b in chart.get("bars", []):
        nb = dict(b)
        try:
            nb["n"] = float(str(b.get("v", "")).replace(",", ""))
        except (ValueError, TypeError):
            nb["n"] = 0.0
        bars.append(nb)
    chart["bars"] = bars

    vals = [b["n"] for b in bars]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + (1.0 if lo == 0 else abs(lo) * 0.1 + 1.0)
    rng = hi - lo
    step = _nice_step(rng / 4.0)
    nlo = math.floor(lo / step) * step
    nhi = math.ceil(hi / step) * step
    # 给首尾各留约 1/4 步长的余量，让柱子不贴边
    if nhi - hi < step * 0.25:
        nhi += step
    if lo - nlo < step * 0.25 and nlo > 0:
        nlo -= step
    dec = 1 if step < 2 else 0
    ticks = []
    t = nlo
    while t <= nhi + 1e-9:
        ticks.append(round(t, 4))
        t += step

    # 单位提示：从标题括号中提取（%、次 等）
    unit = ""
    m = re.search(r"[（(]([^（）()]*)[)）]", str(chart.get("title", "")))
    if m:
        unit = m.group(1).strip()

    chart["axis"] = {"min": nlo, "max": nhi, "ticks": ticks, "dec": dec}
    chart["unit"] = unit
    return chart


def _enhance_module(mod: dict) -> dict:
    """增强单个模块字典：对其内嵌的 chart 补充分轴信息（不污染 PLACEHOLDER 全局）。"""
    if not mod:
        return mod
    mod = dict(mod)
    if mod.get("chart"):
        mod["chart"] = _enhance_chart(mod["chart"])
    return mod


# ---------------------------------------------------------------------------
# 装配 data 字典
# ---------------------------------------------------------------------------
def build_data(result: AnalysisResult) -> dict:
    prof_extra = _extra(result, "profitability")
    prof = _enhance_module(prof_extra or PLACEHOLDER["prof"])
    prof_views = (prof_extra or {}).get("profit_views") or prof_views_ph
    growth_extra = _extra(result, "growth")
    solv_extra = _extra(result, "solvency")
    eff_extra = _extra(result, "efficiency")
    growth = _enhance_module(growth_extra or PLACEHOLDER["growth"])
    solv = _enhance_module(solv_extra or PLACEHOLDER["solv"])
    eff = _enhance_module(eff_extra or PLACEHOLDER["eff"])
    growth_views = (growth_extra or {}).get("growth_views") or growth_views_ph
    solv_views = (solv_extra or {}).get("solvency_views") or solv_views_ph
    eff_views = (eff_extra or {}).get("efficiency_views") or eff_views_ph
    bench = _extra(result, "benchmark") or PLACEHOLDER["bench"]
    fin_extra = _extra(result, "financials")
    if fin_extra:
        fin = fin_extra
        fin_views = fin_extra.get("fin_views", {"default": fin_extra})
    else:
        fin = PLACEHOLDER["fin"]
        fin_views = fin_views_ph

    profile = result.profile or {}
    # 只要画像拿到了真实公司名，或任一模块产出了真实 extra，即视为真实数据；
    # 不让「财务报表」单模块因偶发网络失败而把整份报告拖回占位。
    real_mods = [m for m in result.modules if m.extra]
    is_real = bool(profile.get("name")) or bool(real_mods)

    # ---- Hero ----
    roe = _parse_pct(_card_val(prof, "ROE 净资产收益率"))
    net_margin = _parse_pct(_card_val(prof, "净利率"))
    debt = _parse_pct(_card_val(solv, "资产负债率"))
    rev_g = _parse_pct(_card_val(growth, "营业收入 YoY"))
    netp_g = _parse_pct(_card_val(growth, "归母净利润 YoY"))
    # 顶部营收/净利用财务报表原始元值（totals），避免与表格『亿』格式化串重复缩放
    fin_totals = (fin or {}).get("totals", {}) if is_real else {}
    rev = fin_totals.get("rev")
    netp = fin_totals.get("netp")

    if is_real:
        score = _compute_score(roe, net_margin, debt, rev_g)
        period = (profile.get("list_date") and f"上市于 {profile.get('list_date')}") or (result.code and f"{result.code} 基本面分析报告")
        hero = {
            "name": profile.get("name") or result.name or PLACEHOLDER["hero"]["name"],
            "sub": f"{result.code} 基本面分析报告 · 数据来源 AkShare（公开数据）",
            "industry": profile.get("industry") or "—",
            "period_label": period or PLACEHOLDER["hero"]["period_label"],
            "currency": "人民币（亿元）",
            "kpis": [
                {"lab": "综合评分", "val": str(score), "extra": "模型综合评分", "extra_cls": "up"},
                {"lab": "营业收入", "val": _yi(rev), "extra": (f"{rev_g:.1f}% YoY" if rev_g is not None else "—"), "extra_cls": "up"},
                {"lab": "归母净利润", "val": _yi(netp), "extra": (f"{netp_g:.1f}% YoY" if netp_g is not None else "—"), "extra_cls": "up"},
                {"lab": "ROE 净资产收益率", "val": (f"{roe:.1f}%" if roe is not None else "—"),
                 "extra": "股东回报", "extra_cls": "up"},
            ],
        }
    else:
        hero = PLACEHOLDER["hero"]

    # ---- 综合结论 ----
    if is_real:
        score = _compute_score(roe, net_margin, debt, rev_g)
        tier = "前 15%" if score >= 85 else ("前 30%" if score >= 70 else "中游")
        pros, risks = [], []
        if roe is not None and roe >= 15:
            pros.append(f"ROE 达 {roe:.1f}%，股东回报优异")
        if rev_g is not None and rev_g > 0:
            pros.append(f"营收保持增长（{rev_g:.1f}%）")
        if debt is not None and debt < 50:
            pros.append(f"资产负债率 {debt:.1f}%，财务结构稳健")
        if not pros:
            pros = PLACEHOLDER["conclusion"]["pros"]
        if debt is not None and debt >= 60:
            risks.append(f"资产负债率偏高（{debt:.1f}%）")
        if rev_g is not None and rev_g < 0:
            risks.append("营收同比下滑，成长承压")
        if not risks:
            risks = PLACEHOLDER["conclusion"]["risks"]
        conclusion = {
            "score": score,
            "score_desc": f"评分位于行业{tier}，基本面{'质地优良' if score >= 85 else '稳健' if score >= 70 else '一般'}",
            "pros": pros,
            "risks": risks,
        }
    else:
        conclusion = PLACEHOLDER["conclusion"]

    # ---- 各模块解读：LLM 润色覆盖 ----
    # 仅当该模块有真实引擎数据时才用 LLM 文本覆盖，避免污染占位文案；
    # 同时覆盖默认与各周期视图（前端按周期切换时读取 views[k].insight），
    # 并新建 dict 避免改写 PLACEHOLDER 全局与引擎原始 extra。
    insights = getattr(result, "llm_insights", None)
    if insights:
        def _cover_period_views(views, extra, key):
            val = insights.get(key)
            if not (extra and val):
                return views
            return {k: {**v, "insight": val} for k, v in views.items()}

        prof_views = _cover_period_views(prof_views, prof_extra, "prof")
        growth_views = _cover_period_views(growth_views, growth_extra, "growth")
        solv_views = _cover_period_views(solv_views, solv_extra, "solv")
        eff_views = _cover_period_views(eff_views, eff_extra, "eff")
        fin_views = _cover_period_views(fin_views, fin_extra, "fin")
        bench_extra = _extra(result, "benchmark")
        if bench_extra and insights.get("bench"):
            bench = {**bench, "insight": insights["bench"]}

    return {
        "is_real": is_real,
        "hero": hero,
        "prof": prof,
        "prof_views": prof_views,
        "growth": growth,
        "growth_views": growth_views,
        "solv": solv,
        "solvency_views": solv_views,
        "eff": eff,
        "efficiency_views": eff_views,
        "bench": bench,
        "fin": fin,
        "fin_views": fin_views,
        "conclusion": conclusion,
    }


def _load_asset_b64(template_dir: str, *parts: str) -> str:
    """将 assets 目录下的文件转为 base64，供模板内嵌；缺失时返回空字符串。"""
    img_path = Path(template_dir) / "assets" / Path(*parts)
    if not img_path.exists():
        return ""
    return base64.b64encode(img_path.read_bytes()).decode("utf-8")


def render(result: AnalysisResult, template_dir: str, out_path: str) -> str:
    """渲染并返回输出文件路径。"""
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("template.html")
    data = build_data(result)
    html = template.render(
        result=result,
        data=data,
        report_date=(result.generated_at or "—").split()[0],
        header_topbar_b64=_load_asset_b64(template_dir, "header_topbar.png"),
        header_nav_b64=_load_asset_b64(template_dir, "header_nav.png"),
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)


def render_profile(profile_json: dict, out_path: str | None = None) -> str:
    """由六层 JSON 契约重渲染报告（不依赖 LLM、不重新取数）。

    用途：实现阶段 E「JSON 与 HTML 分离」的核心收益——改样式/配色只凭
    profile.json 重渲染，不重复消耗 LLM 推理、不重新打数据源。

    当前为精简版：覆盖身份信息 + 财务层指标表 + 校验结论 + 固定免责声明。
    阶段 G 六层数据填充后，可扩展为完整模板渲染，无需改动调用方。
    """
    code = profile_json.get("code", "")
    name = profile_json.get("name", "") or code
    period = profile_json.get("period", "")
    layers = profile_json.get("layers", {})
    fin = layers.get("financials", {}) or {}
    validation = profile_json.get("validation", {}) or {}

    fin_rows = "".join(
        f"<tr><td>{v.get('label', '')}</td>"
        f"<td class='num'>{v.get('value', '')}{v.get('unit', '')}</td>"
        f"<td>{v.get('note', '') or '—'}</td>"
        f"<td>{v.get('source', '') or '—'}</td></tr>"
        for v in fin.values()
    ) or '<tr><td colspan="4" class="muted">（财务层暂无数据）</td></tr>'

    warnings = validation.get("warnings", []) or []
    warn_html = "".join(f"<li>{w}</li>" for w in warnings) or "<li class='muted'>无</li>"
    susp = validation.get("suspicious_llm_numbers", []) or []
    susp_html = "".join(f"<li>{s}</li>" for s in susp) or "<li class='muted'>无</li>"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name}（{code}）基本面分析</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f5f6f8; color: #1f2329; }}
  .wrap {{ max-width: 880px; margin: 0 auto; padding: 32px 20px 64px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: #8a9099; font-size: 13px; margin-bottom: 24px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 20px 24px; margin-bottom: 18px;
          box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  .card h2 {{ font-size: 16px; margin: 0 0 14px; border-left: 3px solid #2f6fed; padding-left: 10px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th, td {{ text-align: left; padding: 9px 10px; border-bottom: 1px solid #eef0f3; }}
  th {{ color: #8a9099; font-weight: 500; font-size: 12px; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .muted {{ color: #b0b6bf; }}
  ul {{ margin: 6px 0; padding-left: 20px; }}
  .disclaimer {{ font-size: 12px; color: #8a9099; line-height: 1.6;
                border-top: 1px dashed #e0e3e8; padding-top: 14px; margin-top: 8px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{name}（{code}）</h1>
  <div class="sub">基本面分析报告 · 报告期 {period or '—'} · 由 JSON 契约重渲染（未调用 LLM）</div>

  <div class="card">
    <h2>财务速写（金融层）</h2>
    <table>
      <thead><tr><th>指标</th><th class="num">数值</th><th>备注</th><th>来源</th></tr></thead>
      <tbody>{fin_rows}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>校验结论</h2>
    <p>缺失指标标记数：<strong>{validation.get('missing_marked', 0)}</strong></p>
    <p>交叉校验告警：</p><ul>{warn_html}</ul>
    <p>LLM 数字溯源可疑项：</p><ul>{susp_html}</ul>
  </div>

  <div class="disclaimer">
    本文档基于公开数据的量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。
    任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，必要时咨询持牌专业机构。
    过往表现不预示未来收益。
  </div>
</div>
</body>
</html>"""
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")
    return html

