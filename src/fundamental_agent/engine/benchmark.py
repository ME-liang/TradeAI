"""⑥ 行业对标引擎。"""

from __future__ import annotations

from .base import BaseEngine
from ._util import num, pct, ppx, median_of, latest_yoy
from ..models import Metric, ModuleResult


def _self(ind, *cols):
    for c in cols:
        if c in ind.columns:
            cur, _ = latest_yoy(ind, c)
            if cur is not None:
                return cur
    return None


class BenchmarkEngine(BaseEngine):
    module = "benchmark"
    title = "⑥ 行业对标"

    def run(self, code: str, statements: dict) -> ModuleResult:
        ind = statements.get("indicators")
        peers = statements.get("peers")
        if ind is None or ind.empty or peers is None or peers.empty:
            return super().run(code, statements)

        gross = _self(ind, "销售毛利率(%)", "主营业务利润率(%)")
        roe = _self(ind, "净资产收益率(%)")
        rev_yoy = _self(ind, "主营业务收入增长率(%)")
        debt = _self(ind, "资产负债率(%)")
        inv = _self(ind, "存货周转率(次)")

        med_gross = median_of(peers, "销售毛利率(%)")
        med_roe = median_of(peers, "净资产收益率(%)")
        med_rev = median_of(peers, "主营业务收入增长率(%)")
        med_debt = median_of(peers, "资产负债率(%)")
        med_inv = median_of(peers, "存货周转率(次)")

        if all(v is None for v in (med_gross, med_roe, med_rev, med_debt, med_inv)):
            return super().run(code, statements)

        def _adv_card(lab, self_v, med_v, lower_better=False):
            if self_v is None or med_v is None:
                return {"lab": lab, "val": "—", "tag": "—", "tag_cls": "blue"}
            diff = self_v - med_v
            if lower_better:
                better = diff <= 0
                return {
                    "lab": lab,
                    "val": ppx(diff),
                    "tag": ("▼ 低于行业") if better else ("▲ 高于行业"),
                    "tag_cls": "down" if better else "up",
                }
            better = diff >= 0
            return {
                "lab": lab,
                "val": ppx(diff),
                "tag": ("▲ 领先行业") if better else ("▼ 落后行业"),
                "tag_cls": "up" if better else "down",
            }

        cards = [
            _adv_card("毛利率优势", gross, med_gross),
            _adv_card("ROE 优势", roe, med_roe),
            _adv_card("资产负债率优势", debt, med_debt, lower_better=True),
        ]

        def _row(name, self_v, med_v, suffix=""):
            if self_v is None or med_v is None:
                return {"name": name, "self": "—", "peer": "—", "diff": "—", "diff_cls": ""}
            diff = self_v - med_v
            better = diff >= 0
            lead = "领先" if better else "落后"
            if suffix == "%":
                diff_txt = f"{lead} {ppx(diff)}"
            else:
                diff_txt = f"{lead} {diff:+.1f}{suffix}"
            return {
                "name": name,
                "self": f"{self_v:.1f}{suffix}",
                "peer": f"{med_v:.1f}{suffix}",
                "diff": diff_txt,
                "diff_cls": "" if better else "down",
            }

        rows = [
            _row("毛利率", gross, med_gross, "%"),
            _row("ROE 净资产收益率", roe, med_roe, "%"),
            _row("营收增速", rev_yoy, med_rev, "%"),
            _row("资产负债率", debt, med_debt, "%"),
            _row("存货周转率", inv, med_inv, "x"),
        ]

        lead = sum(1 for r in rows if r["diff_cls"] == "")
        insight = (
            f"公司在盈利效率、资本回报与成长速度上"
            f"{'全面领先' if lead >= 4 else '多数优于'}行业基准，"
            f"资产负债结构{'优于' if (debt is not None and med_debt is not None and debt <= med_debt) else '接近'}同业；"
            f"综合实力处于行业{'第一梯队' if lead >= 4 else '中上水平'}。"
        )

        metrics = [
            Metric("毛利率优势", "毛利率优势", (gross - med_gross) if (gross is not None and med_gross is not None) else None, "pp"),
            Metric("ROE优势", "ROE 优势", (roe - med_roe) if (roe is not None and med_roe is not None) else None, "pp"),
            Metric("资产负债率优势", "资产负债率优势", (debt - med_debt) if (debt is not None and med_debt is not None) else None, "pp"),
        ]
        return ModuleResult(
            module=self.module,
            title=self.title,
            metrics=metrics,
            conclusion=insight,
            extra={"cards": cards, "rows": rows, "insight": insight},
        )
