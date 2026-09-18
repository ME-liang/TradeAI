# -*- coding: utf-8 -*-
"""「三年分部趋势」期次与合计的通用回归测试（合成数据，零 LLM / 零网络）。

为什么需要这组测试：
    东财主营构成的产品名逐期漂移，而趋势筛选以「最近一期年报」为基准算匹配率，
    低于 70% 就整期剔除。历史上三家真实公司被这条规则误伤：
      - 300308 中际旭创：基准期有残差「其他」、历史期没有 → 分母虚高 → 只剩 2026H1
      - 002384 东山精密：同上 → 只剩 2025 / 2026H1
      - 688981 中芯国际：基准期有「其他」「其他(补充)」两项残差，
        最新 H1 产品名从「集成电路晶圆制造代工」漂成「集成电路晶圆代工」→ 2026H1 被剔
    另有一类同族缺陷：东财的「其中:XXX」子行与父行同值，一起计入会重复计数
    （600941 中国移动 2023 合计虚高 1457.95 亿）。

    这组测试用合成数据把这些「形态」固化下来，不依赖 output/ 下的任何真实缓存，
    因此在任何环境、任何时间都能跑，防止同类问题再次流入生产报告。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fundamental_agent.report_types.overview import build_overview_data  # noqa: E402

YI = 1e8  # 亿元


def seg(date: str, name: str, rev: float, *, pct: float | None = 0.2,
        margin: float | None = 0.30, cat: str = "按产品分类") -> dict:
    """构造一行「主营构成」。pct=None 用于模拟东财整列缺「收入比例」。"""
    row = {"报告日期": date, "分类类型": cat, "主营构成": name, "主营收入": rev}
    if pct is not None:
        row["收入比例"] = pct
        row["利润比例"] = pct
    if margin is not None:
        row["毛利率"] = margin
    return row


def build(segments: list[dict]):
    profile = {"code": "000000", "name": "合成测试",
               "layers": {"business": {"segments": segments}}}
    res = types.SimpleNamespace(code="000000", name="合成测试")
    return build_overview_data(res, profile, llm_json=None, use_llm=False)


def total_row(d):
    return next((r for r in (d.get("seg_trend") or []) if str(r.get("name")) == "合计"), None)


def total_values(d) -> list[float]:
    row = total_row(d)
    assert row is not None, "趋势表缺少合计行"
    out = []
    for c in row.get("cells") or []:
        out.append(float(str(c).split("亿")[0].strip()))
    return out


# ---------------------------------------------------------------------------
# 形态 1：残差科目只在基准期出现（300308 中际旭创 / 002384 东山精密）
# ---------------------------------------------------------------------------
def test_residual_only_in_base_period_keeps_history():
    segments = [
        seg("2023-12-31", "高速光通信模块", 97.99 * YI),
        seg("2023-12-31", "中低速光通信模块", 3.83 * YI),
        seg("2024-12-31", "光通信收发模块", 228.86 * YI),
        seg("2025-12-31", "光通信收发模块", 374.57 * YI),
        seg("2025-12-31", "其他", 7.83 * YI),          # 基准期独有
        seg("2026-06-30", "光通信收发模块及器件", 413.31 * YI),
        seg("2026-06-30", "其他", 5.80 * YI),
    ]
    d = build(segments)
    assert d["seg_trend_labels"] == ["2023", "2024", "2025", "2026H1"], (
        "残差科目「其他」只出现在基准期时，历史年报期不得被整期剔除"
    )
    # 逐段归并：2023 的高速 + 中低速必须都归到主板块（101.82 亿），不能只取最优单段
    main = next(r for r in d["seg_trend"] if "光通信" in str(r["name"]))
    assert main["cells"][0].startswith("101.82"), main["cells"]
    # 合计 == 该期真实主营收入
    assert total_values(d) == pytest.approx([101.82, 228.86, 382.40, 419.11], abs=0.02)


# ---------------------------------------------------------------------------
# 形态 2：最新 H1 产品名漂移（688981 中芯国际）
# ---------------------------------------------------------------------------
def test_latest_h1_name_drift_not_dropped():
    segments = [
        seg("2023-12-31", "集成电路晶圆制造代工", 100 * YI),
        seg("2023-12-31", "其他(补充)", 10 * YI),
        seg("2024-12-31", "集成电路晶圆制造代工", 120 * YI),
        seg("2024-12-31", "其他(补充)", 12 * YI),
        seg("2025-12-31", "集成电路晶圆制造代工", 140 * YI),
        seg("2025-12-31", "其他", 14 * YI),
        seg("2025-12-31", "其他(补充)", 14 * YI),
        # 最新 H1 改称「代工」，且残差行数变少 → 旧口径匹配率 2/3=67% < 70% 被剔
        seg("2026-06-30", "集成电路晶圆代工", 80 * YI),
        seg("2026-06-30", "其他", 8 * YI),
    ]
    d = build(segments)
    assert d["seg_trend_labels"] == ["2023", "2024", "2025", "2026H1"], (
        "产品名从「XX制造代工」漂为「XX代工」时，最新 H1 不得被剔除"
    )
    assert total_values(d) == pytest.approx([110.0, 132.0, 168.0, 88.0], abs=0.02)


# ---------------------------------------------------------------------------
# 形态 3：「其中:XXX」子行不得与父行重复计数（600941 中国移动）
# ---------------------------------------------------------------------------
def test_subrow_not_double_counted():
    segments = [
        seg("2024-12-31", "甲类业务", 100 * YI),
        seg("2024-12-31", "其他(补充)", 20 * YI),
        seg("2024-12-31", "其中:明细收入", 20 * YI),   # 与父行同值的子行
        seg("2025-12-31", "甲类业务", 110 * YI),
        seg("2025-12-31", "其他(补充)", 22 * YI),
        seg("2025-12-31", "其中:明细收入", 22 * YI),
        seg("2026-06-30", "甲类业务", 55 * YI),
        seg("2026-06-30", "其他(补充)", 11 * YI),
    ]
    d = build(segments)
    # 若子行参与归并，合计会虚高成 140 / 154 / 77
    assert total_values(d) == pytest.approx([120.0, 132.0, 66.0], abs=0.02), (
        "「其中:XXX」子行与父行同值，计入趋势合计会重复计数"
    )


# ---------------------------------------------------------------------------
# 形态 4：收入比例整列缺失时不得抛异常（旧写法 float('') 会炸掉整个装配）
# ---------------------------------------------------------------------------
def test_missing_pct_column_does_not_crash():
    segments = [
        seg("2024-12-31", "云服务收入", 50 * YI, pct=None),
        seg("2025-12-31", "云服务收入", 60 * YI, pct=None),
        seg("2025-12-31", "其他", 5 * YI, pct=None),
        seg("2026-06-30", "云服务收入", 35 * YI, pct=None),
    ]
    d = build(segments)  # 不抛异常即通过
    assert d["seg_trend_labels"] == ["2024", "2025", "2026H1"]


# ---------------------------------------------------------------------------
# 形态 5：窗口口径 = 最近 3 期年报 + 最近 1 期半年报，且合计逐期自洽
# ---------------------------------------------------------------------------
def test_window_is_three_annuals_plus_latest_h1():
    segments = []
    for i, y in enumerate((2021, 2022, 2023, 2024, 2025)):
        segments.append(seg(f"{y}-12-31", "主营产品收入", (100 + i * 10) * YI))
        segments.append(seg(f"{y}-12-31", "其他", (5 + i) * YI))
    segments.append(seg("2026-06-30", "主营产品收入", 65 * YI))
    segments.append(seg("2026-06-30", "其他", 3 * YI))
    d = build(segments)
    # 更早的 2021 / 2022 属于窗口外，不应出现
    assert d["seg_trend_labels"] == ["2023", "2024", "2025", "2026H1"]
    # 2023: 120+7 / 2024: 130+8 / 2025: 140+9 / 2026H1: 65+3
    assert total_values(d) == pytest.approx([127.0, 138.0, 149.0, 68.0], abs=0.02)


# ---------------------------------------------------------------------------
# 形态 6：非残差历史独立板块并入「其他」行，收入不得凭空消失
# ---------------------------------------------------------------------------
def test_unmatched_history_merged_into_other():
    segments = [
        seg("2023-12-31", "光通信收发模块", 100 * YI),
        seg("2023-12-31", "汽车光电子", 4 * YI),      # 基准期不存在的旧板块
        seg("2023-12-31", "光组件", 2 * YI),
        seg("2024-12-31", "光通信收发模块", 120 * YI),
        seg("2025-12-31", "光通信收发模块", 140 * YI),
        seg("2025-12-31", "其他", 3 * YI),
        seg("2026-06-30", "光通信收发模块", 70 * YI),
    ]
    d = build(segments)
    assert total_values(d) == pytest.approx([106.0, 120.0, 143.0, 70.0], abs=0.02), (
        "未匹配的历史独立板块应并入「其他」，收入不得丢失"
    )
    other = next(r for r in d["seg_trend"] if str(r["name"]) == "其他")
    assert other["cells"][0].startswith("6.00"), other["cells"]
