"""② 盈利能力评估引擎。

从利润表 / 资产负债表推导毛利率 / 净利率 / ROE / ROA 四个核心盈利能力指标，
并按报告类型预生成多套视图（默认 / 年报 / 中报 / 单季度），
供前端下拉框 + 指标按钮实时切换。

- 默认视图：最近 5 期（混合，保持原行为）。
- 年报：每年 12 月；中报：每年 06 月；单季度：季末(03/06/09/12)。各取最近 3 期。
- 卡片 delta：默认视图用同比(YoY)，其余视图用同类型上一期对比。

说明：四个比率统一从利润表/资产负债表推算（而非 AkShare 指标表的散点列），
以保证四个指标在同一视图内周期完全一致、且覆盖到最新报告期。
"""

from __future__ import annotations

from .base import BaseEngine
from ._util import num, pct, ppx
from ..models import Metric, ModuleResult

# 指标定义：(key, 显示名)
_METRIC_DEFS = [
    ("gross", "毛利率"),
    ("net", "净利率"),
    ("roe", "ROE 净资产收益率"),
    ("roa", "ROA 总资产收益率"),
]


def _col_month(c):
    c = str(c).strip()
    if len(c) == 8 and c.isdigit():
        try:
            return int(c[4:6])
        except ValueError:
            return None
    if len(c) >= 10 and c[4] == "-" and c[7] == "-":
        try:
            return int(c[5:7])
        except ValueError:
            return None
    return None


def _label(c, kind):
    c = str(c).strip()
    if len(c) == 8 and c.isdigit():
        y, m = c[:4], c[4:6]
    elif len(c) >= 10 and c[4] == "-" and c[7] == "-":
        y, m = c[:4], c[5:7]
    else:
        return c[:7]
    if kind == "quarterly":
        return {"03": f"{y}Q1", "06": f"{y}Q2", "09": f"{y}Q3", "12": f"{y}Q4"}.get(m, f"{y}Q?")
    if kind == "interim":
        return f"{y}H1" if m == "06" else f"{y}-{m}"
    if kind == "annual":
        return y
    return {"12": y, "06": f"{y}H1", "03": f"{y}Q1", "09": f"{y}Q3"}.get(m, y)


def _select_periods(cols, kind, n=3):
    cols = sorted(str(c) for c in cols)
    if kind == "annual":
        sel = [c for c in cols if _col_month(c) == 12][-n:]
        return sel, [_label(c, kind) for c in sel]
    if kind == "interim":
        sel = [c for c in cols if _col_month(c) == 6][-n:]
        return sel, [_label(c, kind) for c in sel]
    if kind == "quarterly":
        q = [c for c in cols if _col_month(c) in (3, 6, 9, 12)]
        sel = q[-n:]
        return sel, [_label(c, kind) for c in sel]
    sel = cols[-n:]
    return sel, [_label(c, kind) for c in sel]


def _idx(sdf, kws):
    for kw in kws:
        for i in sdf.index:
            if kw in str(i):
                return str(i)
    return None


def _val(sdf, idx, key):
    if not idx or key not in sdf.columns:
        return None
    return num(sdf.loc[idx, key])


def _equity(bal, ta_idx, key):
    """净资产（所有者权益）：优先直接科目，否则 资产总计 − 负债合计。"""
    for kw in ("所有者权益合计", "股东权益合计", "所有者权益"):
        ei = _idx(bal, [kw])
        if ei:
            v = _val(bal, ei, key)
            if v is not None:
                return v
    ta = _val(bal, ta_idx, key) if ta_idx else None
    li = _idx(bal, ["负债合计"])
    lb = _val(bal, li, key) if li else None
    if ta is not None and lb is not None:
        return ta - lb
    return None


def _ratio(income, balance, key, pkey):
    """在指定报告期 pkey 计算某盈利能力比率（百分比）。"""
    rev_idx = _idx(income, ["营业收入"])
    cost_idx = _idx(income, ["营业成本"])
    np_idx = _idx(income, ["归母净利润", "净利润"])
    ta_idx = _idx(balance, ["资产总计"])
    rev = _val(income, rev_idx, pkey)
    cost = _val(income, cost_idx, pkey)
    npv = _val(income, np_idx, pkey)
    ta = _val(balance, ta_idx, pkey)
    ne = _equity(balance, ta_idx, pkey)
    if key == "gross":
        return (rev - cost) / rev * 100 if (rev not in (None, 0)) else None
    if key == "net":
        return npv / rev * 100 if (rev not in (None, 0)) else None
    if key == "roe":
        return npv / ne * 100 if (ne not in (None, 0)) else None
    if key == "roa":
        return npv / ta * 100 if (ta not in (None, 0)) else None
    return None


def _yoy_key(pkey):
    if len(pkey) >= 6 and pkey[:4].isdigit():
        return str(int(pkey[:4]) - 1) + pkey[4:]
    return None


def _build_view(income, balance, kind):
    n = 5 if kind == "default" else 3
    pkeys, labels = _select_periods(balance.columns, kind, n)
    if not pkeys:
        return None
    metrics, cards, latest = {}, [], {}
    for key, lab in _METRIC_DEFS:
        series = []
        for p, l in zip(pkeys, labels):
            v = _ratio(income, balance, key, p)
            if v is not None:
                series.append((l, v))
        bars = [
            {"yr": l, "v": f"{v:.1f}", "n": v, "deep": (i == len(series) - 1)}
            for i, (l, v) in enumerate(series)
        ]
        metrics[key] = {"label": lab, "unit": "%", "title": f"{lab}趋势（%）", "bars": bars}
        # 卡片：最新值 + delta
        cur = series[-1][1] if series else None
        prev = None
        if kind == "default":
            yk = _yoy_key(pkeys[-1])
            if yk and income is not None and yk in income.columns:
                prev = _ratio(income, balance, key, yk)
        else:
            prev = series[-2][1] if len(series) >= 2 else None
        delta = None if (cur is None or prev is None) else cur - prev
        cls = "up" if (delta is None or delta >= 0) else "down"
        cards.append({"key": key, "lab": lab, "val": pct(cur), "delta": ppx(delta), "delta_cls": cls})
        latest[key] = cur

    g, ne, r = latest.get("gross"), latest.get("net"), latest.get("roe")
    roe_card = next((c for c in cards if c["key"] == "roe"), None)
    roe_up = bool(roe_card and roe_card["delta_cls"] == "up")
    gp = pct(g) if g is not None else "—"
    np_ = pct(ne) if ne is not None else "—"
    rp = pct(r) if r is not None else "—"
    insight = (
        f"毛利率 {gp}、净利率 {np_}，ROE 达 {rp}%；"
        f"盈利水平{'稳中有升' if roe_up else '出现波动'}，"
        f"股东回报与盈利质量{'稳健' if (r is not None and r >= 10) else '偏弱'}。"
    )
    return {"cards": cards, "metrics": metrics, "insight": insight}


class ProfitabilityEngine(BaseEngine):
    module = "profitability"
    title = "② 盈利能力评估"

    def run(self, code: str, statements: dict) -> ModuleResult:
        income = statements.get("income")
        balance = statements.get("balance")
        if any(df is None or df.empty for df in (income, balance)):
            return super().run(code, statements)
        # 核心科目覆盖校验
        if not _idx(income, ["营业收入"]) or not _idx(balance, ["资产总计"]):
            return super().run(code, statements)

        views = {}
        for kind in ("default", "annual", "interim", "quarterly"):
            v = _build_view(income, balance, kind)
            if v:
                views[kind] = v
        if not views:
            return super().run(code, statements)

        default = views.get("default") or next(iter(views.values()))
        metrics = [
            Metric("毛利率", "毛利率", None, "%"),
            Metric("净利率", "净利率", None, "%"),
            Metric("ROE", "净资产收益率", None, "%"),
            Metric("ROA", "总资产收益率", None, "%"),
        ]
        return ModuleResult(
            module=self.module,
            title=self.title,
            metrics=metrics,
            conclusion=default["insight"],
            extra={
                "cards": default["cards"],
                "chart": {
                    "title": "盈利能力趋势 · ROE（%）",
                    "bars": default["metrics"]["roe"]["bars"],
                    "unit": "%",
                },
                "insight": default["insight"],
                "profit_views": views,
            },
        )
