"""③ 成长性评估引擎。

从财务分析指标表推导 营业收入增速 / 净利润增速 两个同比序列，
并从利润表推导 营收 CAGR / 净利 CAGR 两个三年复合增速，
按报告类型预生成多套视图（默认 / 年报 / 中报 / 单季度），
供前端下拉框 + 指标按钮实时切换，与盈利能力模块保持一致的交互体验。
"""

from __future__ import annotations

import re

import pandas as pd

from .base import BaseEngine
from ._util import num, pct, ppx, metric_value_at, ind_dates
from ..models import Metric, ModuleResult

# 指标定义：(key, 显示名, 取值器, 单位, 类型)
#  - 类型 "yoy"：同比序列，卡片标签用『同比加速/放缓』
#  - 类型 "cagr"：滚动复合增速，卡片标签用蓝色『三年复合』
_METRICS = [
    ("rev", "营业收入 YoY", "主营业务收入增长率(%)", "%", "yoy"),
    ("net", "归母净利润 YoY", "净利润增长率(%)", "%", "yoy"),
    ("rev_cagr", "营收 CAGR（3Y）", ("cagr", "营业收入"), "%", "cagr"),
    ("net_cagr", "净利 CAGR（3Y）", ("cagr", "归母净利润"), "%", "cagr"),
]


def _build_view(ind, income, kind):
    n = 5 if kind == "default" else 3
    date_strs, labels = ind_dates(ind, kind, n)
    if not date_strs:
        return None
    metrics, cards, latest = {}, [], {}
    for key, lab, acc, unit, mtype in _METRICS:
        bars = []
        for ds, lb in zip(date_strs, labels):
            v = metric_value_at(acc, ind, income, ds)
            if v is not None:
                if mtype == "cagr":
                    v = v * 100  # 复合增速为小数比例，转百分比显示
                bars.append({"yr": lb, "v": f"{v:.1f}", "n": v, "deep": False})
        # 银行业回退：东财指标表对银行不提供「主营业务收入增长率」（值为空），
        # 从利润表「营业收入」年报序列自算同比——与工商企业的指标口径等价。
        if not bars and key == "rev" and income is not None and not income.empty:
            # 列名兼容两种格式：2025-12-31 / 20251231（东财利润表为后者）
            _cols = sorted(str(c) for c in income.columns
                           if re.match(r"^\d{8}$", str(c)) or re.match(r"^\d{4}-\d{2}-\d{2}$", str(c)))
            _annual = [c for c in _cols if c[4:6] == "12"][-4:]
            for i in range(1, len(_annual)):
                try:
                    _cur = float(income.loc["营业收入", _annual[i]])
                    _prev = float(income.loc["营业收入", _annual[i - 1]])
                except (KeyError, TypeError, ValueError):
                    continue
                if _prev and _cur:
                    yoy = (_cur / _prev - 1) * 100
                    bars.append({"yr": _annual[i][:4] + "年报",
                                 "v": f"{yoy:.1f}", "n": yoy, "deep": False})
            if bars:
                bars[-1]["deep"] = True
        if bars:
            bars[-1]["deep"] = True
        metrics[key] = {"label": lab, "unit": unit, "title": f"{lab}趋势（{unit}）", "bars": bars}

        vals = [b["n"] for b in bars]
        cur = vals[-1] if vals else None
        # 默认视图用同比（去年同期）；其余视图用同类型上一期（对年报/中报/单季即等同同比）
        if kind == "default":
            try:
                yoy_ds = (pd.to_datetime(date_strs[-1]) - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
            except Exception:
                yoy_ds = None
            prev = metric_value_at(acc, ind, income, yoy_ds) if yoy_ds else None
        else:
            prev = vals[-2] if len(vals) >= 2 else None
        if mtype == "cagr" and prev is not None:
            prev = prev * 100

        if mtype == "cagr":
            tag = "● 高增" if (cur is not None and cur >= 20) else "● 三年复合"
            tag_cls = "blue"
            delta = None
        else:
            if cur is None:
                tag, tag_cls = "—", "blue"
            elif prev is None or cur >= prev:
                tag, tag_cls = "▲ 同比加速", "red"
            else:
                tag, tag_cls = "▲ 同比放缓", "red"
            delta = None if (cur is None or prev is None) else cur - prev
        cards.append({
            "key": key, "lab": lab, "val": pct(cur),
            "tag": tag, "tag_cls": tag_cls,
            "delta": ppx(delta),
            "delta_cls": ("up" if (delta is None or delta >= 0) else "down"),
        })
        latest[key] = cur

    rv, nv = latest.get("rev"), latest.get("net")
    rc, nc = latest.get("rev_cagr"), latest.get("net_cagr")
    insight = (
        f"营收同比增速 {pct(rv)}、净利润同比增速 {pct(nv)}，"
        f"近三年营收/净利复合增速分别为 {pct(rc)} / {pct(nc)}，"
        f"整体保持{'双位数' if (rc is not None and rc >= 0.1) else '正'}增长，"
        f"成长动能{'充足' if (rv is not None and rv > 0) else '承压'}。"
    )
    return {"cards": cards, "metrics": metrics, "insight": insight}


class GrowthEngine(BaseEngine):
    module = "growth"
    title = "③ 成长性评估"

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
        metrics = [Metric(lab, lab, None, unit) for _, lab, _, unit, _ in _METRICS]
        return ModuleResult(
            module=self.module,
            title=self.title,
            metrics=metrics,
            conclusion=default["insight"],
            extra={
                "cards": default["cards"],
                "chart": chart,
                "insight": default["insight"],
                "growth_views": views,
            },
        )
