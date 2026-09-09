"""④ 偿债能力评估引擎。

从财务分析指标表推导 资产负债率 / 流动比率 / 速动比率 / 利息保障倍数 四个指标，
按报告类型预生成多套视图（默认 / 年报 / 中报 / 单季度），
供前端下拉框 + 指标按钮实时切换，与盈利能力模块保持一致的交互体验。
"""

from __future__ import annotations

import pandas as pd

from .base import BaseEngine
from ._util import num, pct, fmt, ppx, metric_value_at, ind_dates
from ..models import Metric, ModuleResult

# 指标定义：(key, 显示名, 取值器(候选列名), 单位, 方向, 小数位, 后缀)
#  - 方向 "lower"：下降为改善（绿色 down）
#  - 方向 "higher"：上升为改善（红色 up）
_METRICS = [
    ("debt", "资产负债率", ["资产负债率(%)"], "%", "lower", 1, ""),
    ("cur", "流动比率", ["流动比率"], "", "higher", 2, ""),
    ("quick", "速动比率", ["速动比率"], "", "higher", 2, ""),
    ("interest", "利息保障倍数", ["已获利息倍数", "利息支付倍数"], "x", "higher", 1, "x"),
]


def _fmt_val(lab, val):
    if val is None:
        return "—"
    if lab == "资产负债率":
        return pct(val)
    if lab == "利息保障倍数":
        return fmt(val, 1, "x")
    return fmt(val, 2, "")


def _solv_tag(lab, val, delta, higher_better):
    if val is None:
        return "—", "blue"
    if higher_better:
        cls = "up" if (delta is None or delta >= 0) else "down"
        note = "优于" if val >= 1.5 else "关注"
        if lab == "利息保障倍数":
            note = "安全" if val >= 3 else "偏低"
        return ("▲ " + note) if cls == "up" else ("▼ " + note), cls
    improved = delta is not None and delta <= 0
    if improved:
        return "▼ " + ppx(delta) + " 改善", "down"
    return "▲ " + ppx(delta) + " 上升", "up"


def _build_view(ind, income, kind):
    n = 5 if kind == "default" else 3
    date_strs, labels = ind_dates(ind, kind, n)
    if not date_strs:
        return None
    metrics, cards, latest = {}, [], {}
    for key, lab, acc, unit, direction, nd, suffix in _METRICS:
        bars = []
        for ds, lb in zip(date_strs, labels):
            v = metric_value_at(acc, ind, income, ds)
            if v is not None:
                bars.append({"yr": lb, "v": f"{v:.{nd}f}", "n": v, "deep": False})
        if bars:
            bars[-1]["deep"] = True
        metrics[key] = {"label": lab, "unit": unit, "title": f"{lab}趋势（{unit}）", "bars": bars}

        vals = [b["n"] for b in bars]
        cur = vals[-1] if vals else None
        if kind == "default":
            try:
                yoy_ds = (pd.to_datetime(date_strs[-1]) - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
            except Exception:
                yoy_ds = None
            prev = metric_value_at(acc, ind, income, yoy_ds) if yoy_ds else None
        else:
            prev = vals[-2] if len(vals) >= 2 else None
        delta = None if (cur is None or prev is None) else cur - prev
        tag, tag_cls = _solv_tag(lab, cur, delta, direction == "higher")
        cards.append({
            "key": key, "lab": lab, "val": _fmt_val(lab, cur),
            "tag": tag, "tag_cls": tag_cls,
            "delta": ppx(delta),
            "delta_cls": ("up" if (delta is None or delta >= 0) else "down"),
        })
        latest[key] = cur

    debt, cur, quick, interest = (
        latest.get("debt"), latest.get("cur"), latest.get("quick"), latest.get("interest"),
    )
    if cur is None and quick is None and interest is None:
        # 银业等金融机构：无流动/速动比率概念（指标表系统性为空），偿债关注
        # 资本充足率与存款结构，避免渲染「流动比率 —」的误导性文案。
        insight = (
            f"资产负债率 {pct(debt)}。该公司为金融类报表结构，不适用流动比率/速动比率/"
            f"利息保障倍数等工商企业指标，偿债能力关注资本充足率、拨备覆盖率与存款结构。"
        )
        return {"cards": cards, "metrics": metrics, "insight": insight}
    safe = (cur is not None and cur >= 1) and (quick is not None and quick >= 0.8)
    insight = (
        f"资产负债率 {pct(debt)}，流动比率 {fmt(cur)}、速动比率 {fmt(quick)}，"
        f"短期偿债能力{'充足、安全边际较高' if safe else '需关注'}；"
        f"利息保障倍数 {fmt(interest)}x，长期偿债{'安全' if (interest is not None and interest >= 3) else '需留意'}。"
    )
    return {"cards": cards, "metrics": metrics, "insight": insight}


class SolvencyEngine(BaseEngine):
    module = "solvency"
    title = "④ 偿债能力评估"

    def run(self, code: str, statements: dict) -> ModuleResult:
        ind = statements.get("indicators")
        income = statements.get("income")
        if ind is None or ind.empty:
            return super().run(code, statements)

        views = {}
        for kind in ("default", "annual", "interim", "quarterly"):
            v = _build_view(ind, income, kind)
            if v:
                views[kind] = v
        if not views:
            return super().run(code, statements)

        default = views.get("default") or next(iter(views.values()))
        first_key = _METRICS[0][0]
        chart = {
            "title": default["metrics"][first_key]["title"],
            "bars": default["metrics"][first_key]["bars"],
            "unit": default["metrics"][first_key]["unit"],
        }
        metrics = [Metric(lab, lab, None, unit) for _, lab, _, unit, _, _, _ in _METRICS]
        return ModuleResult(
            module=self.module,
            title=self.title,
            metrics=metrics,
            conclusion=default["insight"],
            extra={
                "cards": default["cards"],
                "chart": chart,
                "insight": default["insight"],
                "solvency_views": views,
            },
        )
