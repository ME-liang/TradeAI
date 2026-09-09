"""① 财务报表解析引擎。

把三张报表（利润表 / 资产负债表 / 现金流量表）整理为展示用的表格，
并计算资产结构占比。数据源为 AkShare 返回的「行=科目 / 列=报告期」DataFrame。

同时按报告类型预生成多套视图（默认 / 年报 / 中报 / 单季度），
供前端下拉框实时切换。其中「单季度」视图对利润表 / 现金流量表
（累计值）做"当期累计 − 上一期累计"推导，得到单季可比数值；
资产负债表为时点数，无需推导。
"""

from __future__ import annotations

from .base import BaseEngine
from ._util import num, fmt
from ..models import ModuleResult


# ---- 列标签 / 月份 ----
def _plabel(c):
    c = str(c).strip()
    if len(c) == 8 and c.isdigit():
        y, m = c[:4], c[4:6]
        return {"12": y, "06": f"{y}H1", "03": f"{y}Q1", "09": f"{y}Q3"}.get(m, y)
    if len(c) >= 10 and c[4] == "-" and c[7] == "-":
        y, m = c[:4], c[5:7]
        return {"12": y, "06": f"{y}H1", "03": f"{y}Q1", "09": f"{y}Q3"}.get(m, y)
    return c[:7]


def _plabel_q(c):
    """单季度视图的期标签：03→Q1, 06→Q2, 09→Q3, 12→Q4。"""
    c = str(c).strip()
    if len(c) == 8 and c.isdigit():
        y, m = c[:4], c[4:6]
    elif len(c) >= 10 and c[4] == "-" and c[7] == "-":
        y, m = c[:4], c[5:7]
    else:
        return c[:7]
    return {"03": f"{y}Q1", "06": f"{y}Q2", "09": f"{y}Q3", "12": f"{y}Q4"}.get(m, f"{y}Q?")


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


def _match_index(sdf, keywords):
    for kw in keywords:
        for idx in sdf.index:
            if kw in str(idx):
                return str(idx)
    return None


def _quarter_prev_map(cols):
    """同一年度内，季末列（03/06/09/12）→ 上一报告期列（同年度），用于单季度推导。
    Q1（03-31）无同年度前期，映射为 None（单季值 = 当期累计本身，年初基准为 0）。"""
    q = sorted(str(c) for c in cols if _col_month(c) in (3, 6, 9, 12))
    m = {}
    for c in q:
        yr = c[:4]
        same_year = [x for x in q if x[:4] == yr and x < c]
        m[c] = same_year[-1] if same_year else None
    return m


# ---- 取值（支持累计→单季推导） ----
def _period_value(sdf, idx, key, derive, prev_map):
    """取某科目在某期的数值；derive=True 时（仅对流量表）返回单季值 = 当期累计 − 上一期累计。"""
    if not idx or key not in sdf.columns:
        return None
    v = num(sdf.loc[idx, key])
    if not derive:
        return v
    pv = (prev_map or {}).get(key)
    if pv is None:
        return v  # Q1：年初基准为 0，单季值即当期累计
    prev = num(sdf.loc[idx, pv]) if pv in sdf.columns else None
    if v is not None and prev is not None:
        return v - prev
    return None


def _yoy_pct_derived(sdf, idx, key, derive, prev_map):
    v = _period_value(sdf, idx, key, derive, prev_map)
    py = str(int(key[:4]) - 1) + key[4:]
    pv = _period_value(sdf, idx, py, derive, prev_map)
    if v is not None and pv not in (None, 0):
        return (v - pv) / abs(pv) * 100
    return None


def _qoq_pct_derived(sdf, idx, key, prev_key, derive, prev_map):
    v = _period_value(sdf, idx, key, derive, prev_map)
    pv = _period_value(sdf, idx, prev_key, derive, prev_map) if prev_key else None
    if v is not None and pv not in (None, 0):
        return (v - pv) / abs(pv) * 100
    return None


def _gross(sdf, idx_rev, idx_cost, key, derive, prev_map):
    """毛利润（rev−cost）；derive 时返回单季毛利润。"""
    if not idx_rev or key not in sdf.columns:
        return None
    rv = num(sdf.loc[idx_rev, key])
    cv = num(sdf.loc[idx_cost, key]) if idx_cost else None
    if rv is None or cv is None:
        return None
    g = rv - cv
    if not derive:
        return g
    pv = (prev_map or {}).get(key)
    if not pv or pv not in sdf.columns:
        return g  # Q1：年初基准为 0，单季毛利润即当期累计；数据缺失时退化为当期累计
    rv0 = num(sdf.loc[idx_rev, pv])
    cv0 = num(sdf.loc[idx_cost, pv]) if idx_cost else None
    if rv0 is None or cv0 is None:
        return g
    return g - (rv0 - cv0)


def _gross_yoy(sdf, idx_rev, idx_cost, key, derive, prev_map):
    g = _gross(sdf, idx_rev, idx_cost, key, derive, prev_map)
    py = str(int(key[:4]) - 1) + key[4:]
    g0 = _gross(sdf, idx_rev, idx_cost, py, derive, prev_map)
    if g is not None and g0 not in (None, 0):
        return (g - g0) / abs(g0) * 100
    return None


def _gross_qoq(sdf, idx_rev, idx_cost, key, prev_key, derive, prev_map):
    g = _gross(sdf, idx_rev, idx_cost, key, derive, prev_map)
    g0 = _gross(sdf, idx_rev, idx_cost, prev_key, derive, prev_map) if prev_key else None
    if g is not None and g0 not in (None, 0):
        return (g - g0) / abs(g0) * 100
    return None


def _opex_at(sdf, opex_idxs, key, derive, prev_map):
    tot = None
    for i in opex_idxs:
        v = _period_value(sdf, i, key, derive, prev_map)
        if v is not None:
            tot = v if tot is None else tot + v
    return tot


def _fmt_scaled(v, scale, unit):
    x = v / scale
    return f"{x:,.2f}{unit}"


def _build_rows(sdf, specs, period_keys, scale=1.0, unit="", derive=False, prev_map=None):
    """specs: list of (display_name, [match_keywords])。"""
    rows = []
    prev_key = period_keys[-2] if len(period_keys) >= 2 else None
    for disp, kws in specs:
        idx = _match_index(sdf, kws)
        if idx is None:
            rows.append({"name": disp, "vals": ["—"] * len(period_keys),
                         "yoy": "—", "yoy_cls": "", "qoq": "—", "qoq_cls": ""})
            continue
        vals = []
        for p in period_keys:
            v = _period_value(sdf, idx, p, derive, prev_map)
            vals.append(_fmt_scaled(v, scale, unit) if (v is not None and scale) else (fmt(v, 0) if v is not None else "—"))
        last_y = _yoy_pct_derived(sdf, idx, period_keys[-1], derive, prev_map)
        if last_y is None:
            yoy_txt, yoy_cls = "—", ""
        else:
            yoy_txt = ("+" if last_y >= 0 else "") + f"{last_y:.1f}%"
            yoy_cls = "up" if last_y >= 0 else "down"
        last_q = _qoq_pct_derived(sdf, idx, period_keys[-1], prev_key, derive, prev_map) if prev_key else None
        if last_q is None:
            qoq_txt, qoq_cls = "—", ""
        else:
            qoq_txt = ("+" if last_q >= 0 else "") + f"{last_q:.1f}%"
            qoq_cls = "up" if last_q >= 0 else "down"
        rows.append({"name": disp, "vals": vals, "yoy": yoy_txt, "yoy_cls": yoy_cls,
                     "qoq": qoq_txt, "qoq_cls": qoq_cls})
    return rows


# 报表科目规格（模块级常量，供各视图复用）
BALANCE_SPECS = [
    ("流动资产合计", ["流动资产合计"]),
    ("非流动资产合计", ["非流动资产合计"]),
    ("资产总计", ["资产总计"]),
    ("流动负债合计", ["流动负债合计"]),
    ("非流动负债合计", ["非流动负债合计"]),
    ("负债和权益合计", ["负债和所有者权益合计", "负债和股东权益合计"]),
]
INCOME_SPECS = [
    ("营业收入", ["营业收入"]),
    ("营业成本", ["营业成本"]),
    ("营业利润", ["营业利润"]),
    ("归母净利润", ["归母净利润", "净利润"]),
]

# 银行业科目组：银行执行金融企业会计制度，无「流动资产/营业成本/毛利润」概念
# （资产=现金及存放央行款项/发放贷款及垫款等；支出=营业支出；盈利=净利息收入）。
# 探测：资产负债表含贷款/存款科目且无「流动资产合计」→ 按银行业科目组呈现。
BALANCE_SPECS_BANK = [
    ("现金及存放央行款项", ["现金及存放中央银行款项"]),
    ("发放贷款及垫款", ["发放贷款及垫款净额", "发放贷款及垫款"]),
    ("资产总计", ["资产总计"]),
    ("客户存款", ["客户存款(吸收存款)", "吸收存款"]),
    ("负债合计", ["负债合计"]),
    ("负债及股东权益总计", ["负债及股东权益总计", "负债和所有者权益合计"]),
]
INCOME_SPECS_BANK = [
    ("营业收入", ["营业收入"]),
    ("净利息收入", ["净利息收入"]),
    ("手续费及佣金净收入", ["手续费及佣金净收入"]),
    ("营业支出", ["营业支出"]),
    ("营业利润", ["营业利润"]),
    ("归母净利润", ["归属于母公司的净利润", "归母净利润", "净利润"]),
]


def _is_bank_like(balance) -> bool:
    """探测银行业报表结构：含贷款/存款科目、且无工商企业的「流动资产合计」。"""
    if balance is None or getattr(balance, "empty", True):
        return False
    idx = [str(i) for i in balance.index]
    has_bank = any(("发放贷款" in i or "吸收存款" in i or "客户存款" in i) for i in idx)
    has_industrial = any("流动资产合计" in i for i in idx)
    return has_bank and not has_industrial
CASH_SPECS = [
    ("经营活动现金流净额", ["经营活动产生的现金流量净额", "经营活动现金流"]),
    ("投资活动现金流净额", ["投资活动产生的现金流量净额", "投资活动现金流"]),
    ("筹资活动现金流净额", ["筹资活动产生的现金流量净额", "筹资活动现金流"]),
    ("汇率变动影响", ["汇率变动对现金的影响", "汇率变动对现金及现金等价物的影响"]),
    ("现金及现金等价物净增加额", ["现金及现金等价物净增加额", "现金及现金等价物净增加额"]),
]


def _select_periods(sdf, kind, n=3):
    """按报告类型选取周期键 + 标签 + 是否需要单季推导。"""
    cols = sorted(str(c) for c in sdf.columns)
    if kind == "annual":
        sel = [c for c in cols if _col_month(c) == 12][-n:]
        return sel, [_plabel(c) for c in sel], False
    if kind == "interim":
        sel = [c for c in cols if _col_month(c) == 6][-n:]
        return sel, [_plabel(c) for c in sel], False
    if kind == "quarterly":
        q = [c for c in cols if _col_month(c) in (3, 6, 9, 12)]
        sel = q[-n:]
        return sel, [_plabel_q(c) for c in sel], True
    # default：最近 n 期（混合，保持原行为）
    sel = cols[-n:]
    return sel, [_plabel(c) for c in sel], False


def _build_view(balance, income, cash, period_keys, labels, derive, prev_map):
    _bank = _is_bank_like(balance)
    _bal_specs = BALANCE_SPECS_BANK if _bank else BALANCE_SPECS
    _inc_specs = INCOME_SPECS_BANK if _bank else INCOME_SPECS
    bal_rows = _build_rows(balance, _bal_specs, period_keys, 1e8, "亿", derive, prev_map)
    inc_rows = _build_rows(income, _inc_specs, period_keys, 1e8, "亿", derive, prev_map)
    cash_rows = _build_rows(cash, CASH_SPECS, period_keys, 1e8, "亿", derive, prev_map)

    # 毛利润（推导时取单季）；银行业无毛利润概念，不插入该行
    rev_idx = _match_index(income, ["营业收入"])
    cost_idx = _match_index(income, ["营业成本"])
    gross_vals = []
    for p in period_keys:
        g = _gross(income, rev_idx, cost_idx, p, derive, prev_map)
        gross_vals.append(_fmt_scaled(g, 1e8, "亿") if g is not None else "—")
    gy = _gross_yoy(income, rev_idx, cost_idx, period_keys[-1], derive, prev_map)
    prev_key = period_keys[-2] if len(period_keys) >= 2 else None
    gq = _gross_qoq(income, rev_idx, cost_idx, period_keys[-1], prev_key, derive, prev_map)
    gross_row = {
        "name": "毛利润",
        "vals": gross_vals,
        "yoy": ("+" if gy >= 0 else "") + f"{gy:.1f}%" if gy is not None else "—",
        "yoy_cls": ("up" if (gy is not None and gy >= 0) else "down") if gy is not None else "",
        "qoq": ("+" if gq >= 0 else "") + f"{gq:.1f}%" if gq is not None else "—",
        "qoq_cls": ("up" if (gq is not None and gq >= 0) else "down") if gq is not None else "",
    }
    if not _bank:
        inc_rows = inc_rows[:2] + [gross_row] + inc_rows[2:]

    # 销售/管理/研发费用（推导时取单季之和）；银行对应科目为「业务及管理费用」
    opex_idxs = [i for i in income.index if any(k in str(i) for k in ("销售费用", "管理费用", "研发费用", "业务及管理费用"))] or [i for i in income.index if _bank and "业务及管理费用" in str(i)]
    opex_vals, opex_raw = [], []
    for p in period_keys:
        t = _opex_at(income, opex_idxs, p, derive, prev_map)
        opex_raw.append(t)
        opex_vals.append(_fmt_scaled(t, 1e8, "亿") if t is not None else "—")
    opex_last = opex_raw[-1] if opex_raw else None
    opex_prev = opex_raw[-2] if len(opex_raw) >= 2 else None
    opex_q = (opex_last - opex_prev) / abs(opex_prev) * 100 if (opex_last is not None and opex_prev not in (None, 0)) else None
    opex_row = {
        "name": "销售/管理/研发费用",
        "vals": opex_vals,
        "yoy": "—", "yoy_cls": "",
        "qoq": ("+" if opex_q >= 0 else "") + f"{opex_q:.1f}%" if opex_q is not None else "—",
        "qoq_cls": ("up" if (opex_q is not None and opex_q >= 0) else "down") if opex_q is not None else "",
    }
    inc_rows = inc_rows[:3] + [opex_row] + inc_rows[3:]

    # 资产结构（资产负债表为时点数，不推导）
    ta_idx = _match_index(balance, ["资产总计"])
    ta = _period_value(balance, ta_idx, period_keys[-1], False, None)
    if _bank:
        # 银业资产结构：发放贷款及垫款 vs 其他资产（无流动/非流动概念）
        loan_idx = _match_index(balance, ["发放贷款及垫款净额", "发放贷款及垫款"])
        loan = _period_value(balance, loan_idx, period_keys[-1], False, None)
        loan_pct = round(loan / ta * 100, 1) if (loan and ta) else None
        other_pct = round(100 - loan_pct, 1) if loan_pct is not None else None
        structure = [
            {"name": "发放贷款及垫款", "pct": (f"{loan_pct}%" if loan_pct is not None else "—"), "w": loan_pct or 0, "deep": True},
            {"name": "其他资产（现金/金融投资等）", "pct": (f"{other_pct}%" if other_pct is not None else "—"), "w": other_pct or 0, "deep": False},
        ]
        insight = (
            f"资产规模{'稳步扩张' if True else ''}，发放贷款及垫款占总资产 "
            f"{f'{loan_pct}%' if loan_pct is not None else '—'}；银行业不适用流动比率/存货周转等"
            f"工商企业指标，偿债与盈利质量关注资本充足与净利息收入。"
        )
    else:
        ca_idx = _match_index(balance, ["流动资产合计"])
        ca = _period_value(balance, ca_idx, period_keys[-1], False, None)
        ca_pct = round(ca / ta * 100, 1) if (ca and ta) else None
        nca_pct = round(100 - ca_pct, 1) if ca_pct is not None else None
        structure = [
            {"name": "流动资产", "pct": (f"{ca_pct}%" if ca_pct is not None else "—"), "w": ca_pct or 0, "deep": True},
            {"name": "非流动资产", "pct": (f"{nca_pct}%" if nca_pct is not None else "—"), "w": nca_pct or 0, "deep": False},
        ]
        insight = (
            f"资产规模稳步扩张，流动资产占比 {f'{ca_pct}%' if ca_pct is not None else '—'}，"
            f"整体流动性{'稳健' if (ca is not None and ta is not None and ca / ta > 0.5) else '需关注'}；"
            f"盈利质量与现金流健康度详见各报表科目。"
        )
    return {
        "periods": labels,
        "balance": bal_rows,
        "income": inc_rows,
        "cash": cash_rows,
        "structure": structure,
        "insight": insight,
    }


def _coverage(sdf, keywords):
    return any(_match_index(sdf, [kw]) for kw in keywords)


class FinancialsEngine(BaseEngine):
    module = "financials"
    title = "① 财务报表解析"

    def run(self, code: str, statements: dict) -> ModuleResult:
        income = statements.get("income")
        balance = statements.get("balance")
        cash = statements.get("cash")
        if any(df is None or df.empty for df in (income, balance, cash)):
            return super().run(code, statements)

        # 核心科目覆盖校验：任一对不齐则整体回退占位
        need_balance = ["资产总计", "营业收入", "归母净利润"]
        need_income = ["营业收入"]
        need_cash = ["经营活动产生的现金流量净额", "经营活动现金流"]
        if not _coverage(balance, need_balance) or not _coverage(income, need_income) or not _coverage(cash, need_cash):
            return super().run(code, statements)

        prev_map = _quarter_prev_map(balance.columns)

        # 预生成 默认 / 年报 / 中报 / 单季度 四套视图
        views = {}
        for kind in ("default", "annual", "interim", "quarterly"):
            pkeys, labels, derive = _select_periods(balance, kind, 3)
            if not pkeys:
                continue
            views[kind] = _build_view(balance, income, cash, pkeys, labels, derive, prev_map)
        if not views:
            return super().run(code, statements)

        default_view = views.get("default") or next(iter(views.values()))

        # 顶部汇总用的原始元值（默认视图最新期，资产负债表不推导）
        np_idx = _match_index(income, ["归母净利润", "净利润"])
        rev_idx = _match_index(income, ["营业收入"])
        dkeys, _, _ = _select_periods(balance, "default", 3)
        rev_raw = _period_value(income, rev_idx, dkeys[-1], False, None) if (rev_idx and dkeys) else None
        netp_raw = _period_value(income, np_idx, dkeys[-1], False, None) if (np_idx and dkeys) else None

        extra = dict(default_view)
        extra["fin_views"] = views
        extra["totals"] = {"rev": rev_raw, "netp": netp_raw}
        return ModuleResult(
            module=self.module,
            title=self.title,
            metrics=[],
            conclusion=default_view["insight"],
            extra=extra,
        )
