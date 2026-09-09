"""⑤ 运营效率评估引擎。

从财务分析指标表推导 存货周转率 / 应收账款周转率 / 总资产周转率，
并由 存货周转天数 + 应收账款周转天数 推导 现金循环周期，
按报告类型预生成多套视图（默认 / 年报 / 中报 / 单季度），
供前端下拉框 + 指标按钮实时切换，与盈利能力模块保持一致的交互体验。
"""

from __future__ import annotations

import pandas as pd

from .base import BaseEngine
from ._util import num, fmt, ppx, metric_value_at, ind_dates, ind_value_at
from ..models import Metric, ModuleResult


def _cash_cycle(df, income, date_str):
    """现金循环周期 = 存货周转天数 + 应收账款周转天数（任一项缺失则整体缺失）。"""
    inv = ind_value_at(df, "存货周转天数(天)", date_str)
    recv = ind_value_at(df, "应收账款周转天数(天)", date_str)
    if inv is None or recv is None:
        return None
    return inv + recv


# 指标定义：(key, 显示名, 取值器, 单位, 方向, 小数位, 后缀)
_METRICS = [
    ("inv", "存货周转率", ["存货周转率(次)"], "x", "higher", 2, "x"),
    ("recv", "应收账款周转率", ["应收账款周转率(次)"], "x", "higher", 2, "x"),
    ("tot", "总资产周转率", ["总资产周转率(次)"], "x", "higher", 2, "x"),
    ("cash", "现金循环周期", _cash_cycle, "天", "lower", 0, " 天"),
]


def _eff_tag(val, delta, lower_better):
    if val is None:
        return "—", "blue"
    if lower_better:
        improved = delta is not None and delta <= 0
        return ("▼ 缩短" if improved else "▲ 延长"), ("down" if improved else "up")
    cls = "up" if (delta is None or delta >= 0) else "down"
    return ("▲ 提速" if cls == "up" else "▼ 放缓"), cls


def _fmt_val(lab, val, nd, suffix):
    return fmt(val, nd, suffix) if val is not None else "—"


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
        tag, tag_cls = _eff_tag(cur, delta, direction == "lower")
        cards.append({
            "key": key, "lab": lab, "val": _fmt_val(lab, cur, nd, suffix),
            "tag": tag, "tag_cls": tag_cls,
            "delta": ppx(delta),
            "delta_cls": ("up" if (delta is None or delta >= 0) else "down"),
        })
        latest[key] = cur

    inv, recv, tot, cash = (
        latest.get("inv"), latest.get("recv"), latest.get("tot"), latest.get("cash"),
    )
    if inv is None and recv is None and cash is None:
        # 银业等金融机构：无存货/应收概念（指标表系统性为空）
        insight = (
            f"总资产周转率 {fmt(tot)}x。该公司为金融类报表结构，不适用存货周转率/"
            f"应收账款周转率/现金循环周期等工商企业指标，运营效率关注资产规模"
            f"扩张与净利息收入的匹配。"
        )
        return {"cards": cards, "metrics": metrics, "insight": insight}
    insight = (
        f"存货周转率 {fmt(inv)}x、应收账款周转率 {fmt(recv)}x、总资产周转率 {fmt(tot)}x，"
        f"现金循环周期约 {fmt(cash, 0, ' 天')}，"
        f"整体营运效率{'改善' if (inv is not None and cash is not None and inv >= 0 and cash >= 0) else '平稳'}。"
    )
    return {"cards": cards, "metrics": metrics, "insight": insight}


class EfficiencyEngine(BaseEngine):
    module = "efficiency"
    title = "⑤ 运营效率评估"

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
                "efficiency_views": views,
            },
        )
