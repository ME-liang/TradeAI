"""六层报告装配（阶段 G 实装中）。

职责：
- 在引擎产出后触发校验闸门（缺失去标、交叉校验、LLM 数字溯源）。
- 把 AnalysisResult(+profile) 装配为可 JSON 落盘的六层契约骨架，供 LLM 生成概况内容。

数据来源（已实装 / 待补）：
- identity        ✅ cninfo 个股资料（get_identity）
- business 文字   ✅ cninfo 主营业务描述；分部占比 ✅ 东方财富主营构成接口（stock_zygc_em，按产品分类、最新报告期）
- governance      ✅ 前十大股东 / 分红（get_holders / get_dividend）；质押 ✅ 东财股权质押明细（get_pledge）
- industry        🟢 行业名称（cninfo + 申万）；同业可比（申万二级，get_industry_peers_overview，非东财源）
- financials      ✅ 六分析引擎指标（_financials_from）
- dynamics        ✅ 近期公告（get_news）

说明：骨架仅提供「事实数据」，叙述由 overview.md 提示词约束 LLM 基于这些数据生成，
缺失字段标「未获取」，严禁编造。
"""

from __future__ import annotations

from ..models import AnalysisResult
from ..schema.validate import validate_report


def _financials_from(result: AnalysisResult) -> dict:
    out: dict = {}
    for m in result.modules:
        for metric in m.metrics:
            if metric.value is not None:
                out[metric.key] = {
                    "label": metric.label,
                    "value": metric.value,
                    "unit": metric.unit,
                    "note": metric.note,
                    "source": metric.source_ref.source if metric.source_ref else "",
                    "as_of": metric.source_ref.as_of if metric.source_ref else "",
                }
    return out


def _to_float(v):
    """把数值/百分比字符串规整为 float；失败返回 0.0（汇总用，不用于展示）。"""
    try:
        if v is None:
            return 0.0
        s = str(v).replace("%", "").replace(",", "").strip()
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _name_key(name) -> str:
    """股东名归一：去空格、转大写、截去括号后缀（'MARK ZHAO(赵马克)' -> 'MARKZHAO'）。"""
    s = str(name or "").replace(" ", "").replace("\u3000", "").upper()
    return s.split("(")[0].split("（")[0]


def _pledge_summary(records: list, share_capital: int = 0) -> dict:
    """从逐笔质押明细派生治理风险摘要（供 LLM 直接引用 + 骨架兜底渲染）。

    返回 {active_count, active_shares, active_total_pct, pct_basis, top_holder,
    latest_announce_date}；无任何质押记录时返回空 dict。

    **口径（关键）**：质押明细的 `pct_of_total` 是「质押当时」的总股本口径，各笔直接
    相加会系统性高估在押比例——本公司总股本已从 2012 年约 6,700 万股增至当前约 11.49
    亿股（增发 / 资产重组所致）。因此只要拿得到当前总股本（`share_capital`），一律改用
    `未解押股数合计 / 当前总股本` 折算，并在 `pct_basis` 标明口径；拿不到才退回原口径。

    `top_holder` 给出在押最多的股东，避免把他人质押张冠李戴到实控人头上。
    """
    if not records:
        return {}
    active = [r for r in records if str(r.get("status", "")) == "未解押"]
    dates = [str(r.get("announce_date", "")) for r in records if r.get("announce_date")]
    latest = max(dates) if dates else ""

    by_holder: dict = {}
    for r in active:
        key = str(r.get("shareholder") or "").strip()
        by_holder[key] = by_holder.get(key, 0.0) + _to_float(r.get("pledge_shares"))
    active_shares = sum(by_holder.values())

    if share_capital and share_capital > 0:
        active_total_pct = round(active_shares / float(share_capital) * 100.0, 2)
        basis = "current_capital"
    else:
        active_total_pct = round(
            min(sum(_to_float(r.get("pct_of_total")) for r in active), 100.0), 2
        )
        basis = "pledge_time_capital"

    top_holder = {}
    if by_holder:
        name, shares = max(by_holder.items(), key=lambda kv: kv[1])
        top_holder = {
            "name": name,
            "shares": int(shares),
            "pct": (
                round(shares / float(share_capital) * 100.0, 2)
                if share_capital else None
            ),
        }

    return {
        "active_count": len(active),
        "active_shares": int(active_shares),
        "active_total_pct": active_total_pct,
        "pct_basis": basis,
        "top_holder": top_holder,
        "latest_announce_date": latest,
    }


def _f(v):
    """把标量 / 字符串规整为 float；None / NaN / 非数值一律返回 None（不静默补 0）。

    与 `_to_float` 的区别：后者为汇总口径，失败补 0.0；此处用于**展示**，
    缺失必须显式为 None，由上层渲染成「未获取」，绝不把缺失显示成 0。
    """
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


# 财务速写取数映射：(内部键, 财务分析指标表列名)
# 全部来自 AkShare `stock_financial_analysis_indicator`（东方财富财务分析，非 push2 封禁端点）。
_FIN_IND_MAP = (
    ("roe", "净资产收益率(%)"),
    ("gross_margin", "销售毛利率(%)"),
    ("net_margin", "销售净利率(%)"),
    ("rev_growth", "主营业务收入增长率(%)"),
    ("profit_growth", "净利润增长率(%)"),
    ("debt_ratio", "资产负债率(%)"),
    ("inventory_turn", "存货周转率(次)"),
    ("inventory_days", "存货周转天数(天)"),
    ("ar_turn", "应收账款周转率(次)"),
    ("ar_days", "应收账款周转天数(天)"),
    ("current_ratio", "流动比率"),
    ("quick_ratio", "速动比率"),
    ("cf_to_profit", "经营现金净流量与净利润的比率(%)"),
    ("cf_to_revenue", "经营现金净流量对销售收入比率(%)"),
    ("total_assets", "总资产(元)"),
)


def _row_name(df, exact: str, *fallback_kw: str):
    """在三表（行=科目）里定位科目名：优先精确匹配，再退化为包含匹配。"""
    if df is None or df.empty:
        return None
    for idx in df.index:
        if str(idx) == exact:
            return str(idx)
    for kw in fallback_kw:
        for idx in df.index:
            if kw in str(idx):
                return str(idx)
    return None


def build_financial_summary(statements: dict) -> dict:
    """从「财务分析指标表 + 三大报表」直接计算概况报告财务速写需要的指标。

    为何不复用六引擎的 `_financials_from(result)`：实测引擎产出的 `Metric.value` 多为
    None（各引擎把结果放在 `extra` 里供前端图表消费），经 `value is not None` 过滤后
    `layers.financials` 恒为空——概况报告拿不到任何财务数字。故在此处从原始报表直接算。

    全部为**可精确计算的事实**，不交给 LLM 生成；缺失返回 None，由渲染层显示「未获取」。
    """
    out: dict = {}
    ind = statements.get("indicators")
    income = statements.get("income")
    cash = statements.get("cash")
    balance = statements.get("balance")

    # 1) 财务分析指标表：取最新报告期一行
    if ind is not None and not getattr(ind, "empty", True):
        try:
            last = ind.iloc[-1]
            d = last.get("日期")
            out["as_of"] = (
                d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d or "")
            )
            for key, col in _FIN_IND_MAP:
                if col in ind.columns:
                    out[key] = _f(last.get(col))
        except Exception:
            pass

    # 2) 三大报表：研发费率、经营现金流净额、归母净利润、净资产、CAGR
    def _at(df, row, col):
        if df is None or row is None or col is None:
            return None
        try:
            return _f(df.loc[row, col])
        except Exception:
            return None

    cols = list(income.columns) if income is not None and not income.empty else []
    latest_col = cols[0] if cols else None

    rev_row = _row_name(income, "营业总收入", "营业收入")
    np_row = _row_name(
        income, "归属于母公司所有者的净利润", "归属于母公司", "净利润"
    )
    rd_row = _row_name(income, "研发费用", "研究开发费")
    cost_row = _row_name(income, "营业成本", "营业总成本")
    cf_row = _row_name(cash, "经营活动产生的现金流量净额", "经营活动")
    eq_row = _row_name(
        balance, "归属于母公司股东权益合计", "所有者权益(或股东权益)合计", "所有者权益"
    )

    out["revenue"] = _at(income, rev_row, latest_col)
    out["net_profit"] = _at(income, np_row, latest_col)
    out["rd_expense"] = _at(income, rd_row, latest_col)
    out["oper_cash"] = _at(cash, cf_row, latest_col)
    out["equity"] = _at(balance, eq_row, latest_col)
    _cost = _at(income, cost_row, latest_col)

    # 毛利率自算兜底（关键）：东财「销售毛利率(%)」列**存在但最新期常为 NaN**
    # （实测 300184 的 2026-06-30 该列为 NaN）。毛利率是分销商的核心指标——
    # 「低毛利约束」这一业务判断完全依赖它，缺不得，故一律用利润表自算：
    # 毛利率 = (营业总收入 - 营业成本) / 营业总收入。
    if out.get("gross_margin") is None and out.get("revenue") and _cost is not None:
        try:
            out["gross_margin"] = (
                (out["revenue"] - _cost) / out["revenue"] * 100.0
            )
        except ZeroDivisionError:
            pass

    # 现金流占比同样自算：东财「经营现金净流量与净利润的比率(%)」列名带 (%)
    # 但**实际是比值**（实测 0.341，即经营现金流仅为净利润的 34.1%），
    # 直接当百分数展示会显示成「0.34%」，与事实差 100 倍。故用绝对额重算为真正的百分数。
    if out.get("oper_cash") is not None and out.get("net_profit"):
        try:
            out["cf_to_profit"] = out["oper_cash"] / out["net_profit"] * 100.0
        except ZeroDivisionError:
            pass
    if out.get("oper_cash") is not None and out.get("revenue"):
        try:
            out["cf_to_revenue"] = out["oper_cash"] / out["revenue"] * 100.0
        except ZeroDivisionError:
            pass

    # 研发费率 = 研发费用 / 营业收入（同期）
    if out.get("rd_expense") is not None and out.get("revenue"):
        try:
            out["rd_ratio"] = out["rd_expense"] / out["revenue"] * 100.0
        except ZeroDivisionError:
            out["rd_ratio"] = None
    else:
        out["rd_ratio"] = None

    # 3) 研发费用（年报口径）及同比增速：用于概况「分部趋势」的 R&D 判断。
    #    取最近两期年报研发费用列，同比 = (本期 - 上期) / |上期|。
    out["rd_expense_annual"] = None
    out["rd_expense_yoy"] = None
    out["rd_expense_period"] = None
    if rd_row is not None:
        annual_rd_cols = sorted(
            [c for c in cols if str(c).endswith("1231") and _at(income, rd_row, c) is not None],
            reverse=True,
        )
        if len(annual_rd_cols) >= 1:
            out["rd_expense_annual"] = _at(income, rd_row, annual_rd_cols[0])
            out["rd_expense_period"] = str(annual_rd_cols[0])[:4]
        if len(annual_rd_cols) >= 2:
            v_now = _at(income, rd_row, annual_rd_cols[0])
            v_prev = _at(income, rd_row, annual_rd_cols[1])
            if v_now is not None and v_prev not in (None, 0):
                try:
                    out["rd_expense_yoy"] = (v_now - v_prev) / abs(v_prev) * 100.0
                except (ZeroDivisionError, ValueError):
                    pass

    # 4) 三年复合增速（CAGR）：仅用**年报**列（列名为 YYYYMMDD，年报以 1231 结尾），
    #    杜绝半年报与年报混算。取最新年报与其三年前的年报。
    annual = sorted(
        [c for c in cols if str(c).endswith("1231")], reverse=True
    )
    for key, row in (("rev_cagr", rev_row), ("profit_cagr", np_row)):
        out[key] = None
        if row is None or len(annual) < 2:
            continue
        v_now = _at(income, row, annual[0])
        # 基准年 = 最新年报年份 - 3；找不到正好三年前的年报时，退化为第四列（近四年最旧）
        try:
            target_year = int(str(annual[0])[:4]) - 3
        except (TypeError, ValueError):
            continue
        v_base = None
        for c in annual[1:]:
            try:
                if int(str(c)[:4]) == target_year:
                    v_base = _at(income, row, c)
                    break
            except (TypeError, ValueError):
                continue
        if v_base is None and len(annual) >= 4:
            v_base = _at(income, row, annual[3])
        if v_now is not None and v_base not in (None, 0) and v_now > 0 and v_base > 0:
            try:
                out[key] = ((v_now / v_base) ** (1.0 / 3.0) - 1.0) * 100.0
            except (ZeroDivisionError, ValueError, OverflowError):
                out[key] = None

    # 5) 关键绝对额事实（防编造 / 防混淆）：存货、应收账款，以及分报告期的营收与经营现金流。
    #    背景（力源信息 300184 实测）：概况报告曾出现「存货 20.34 亿」——实为应收账款
    #    2025 年值 2,033,678,283.82 被错标为存货；「经营现金流 -1.91 亿」与
    #    「2023 营收 56.42 亿」则是模型凭记忆编造（真值分别为 -0.91 亿 / 59.44 亿）。
    #    这些值**量级接近真值**（偏差仅 3%~5%），量级护栏（0.55x~1.8x）根本抓不到，
    #    唯一可靠解法是把真值按报告期显式喂给模型，让模型无处可编。
    inv_row = _row_name(balance, "存货")
    ar_row = _row_name(balance, "应收账款", "应收票据及应收账款", "应收票据")
    out["inventory"] = _at(balance, inv_row, latest_col)
    out["accounts_receivable"] = _at(balance, ar_row, latest_col)

    def _by_period(df, row, limit: int = 12) -> dict:
        """按报告期列（YYYYMMDD）返回**最近 limit 期**的 {报告期: 数值}。

        只取近 3 年（12 个报告期）。原因有二：
        1) 全量历史（实测达 80 期）会撑爆提示词核对表、稀释重点；
        2) 会引入巧合值放行幻觉——300184 的 20170331 经营现金流恰为 -1.91 亿，
           正是模型编造的那个数字，若纳入比对，护栏会误判为「引用了真实历史期」。
        """
        h: dict = {}
        if df is None or row is None:
            return h
        try:
            _cs = sorted(list(df.columns), key=lambda x: str(x), reverse=True)[:limit]
        except Exception:
            return h
        for c in _cs:
            v = _at(df, row, c)
            if v is not None:
                h[str(c)] = v
        return h

    out["revenue_by_period"] = _by_period(income, rev_row)
    out["profit_by_period"] = _by_period(income, np_row)
    out["oper_cash_by_period"] = _by_period(cash, cf_row)
    out["inventory_by_period"] = _by_period(balance, inv_row)

    return out


def build_profile(result: AnalysisResult) -> dict:
    """装配六层 JSON 契约（骨架）+ 校验结果。可 json.dumps 序列化。

    各层数据来源（阶段 G 实装）：
    - identity / business.main_business_text：cninfo 个股资料（get_identity）
    - governance：前十大股东 / 分红（get_holders / get_dividend）；质押（get_pledge，东财股权质押明细）
    - dynamics：近期公告（get_news）
    - industry：行业名称（cninfo + 申万），同业可比（申万二级，get_industry_peers_overview，非东财源）
    - financials：六分析引擎指标（_financials_from）
    - business.segments：东方财富主营构成接口（stock_zygc_em，按产品分类、最新报告期），真实可溯源
    """
    vr = validate_report(result)
    p = result.profile or {}
    cn = p.get("cninfo") or {}
    # 当前总股本（股）：用于股东持股占比与在押占比的口径统一，取不到时为 0（退回质押当时口径）
    _cap = int(p.get("share_capital") or 0)

    layers = {
        "identity": {
            "name": cn.get("name"),
            "short_name": cn.get("short_name"),
            "en_name": cn.get("en_name"),
            "market": cn.get("market"),
            "industry_cninfo": cn.get("industry_cninfo"),
            "industry_sw": p.get("industry"),
            "legal_rep": cn.get("legal_rep"),
            "reg_capital": cn.get("reg_capital"),
            "found_date": cn.get("found_date"),
            "list_date": cn.get("list_date"),
            "indices": cn.get("indices"),
            "website": cn.get("website"),
            "reg_addr": cn.get("reg_addr"),
            "office_addr": cn.get("office_addr"),
            "main_business": cn.get("main_business"),
            "biz_scope": cn.get("biz_scope"),
            "org_profile": cn.get("org_profile"),
        },
        "business": {
            "main_business_text": cn.get("main_business"),
            "biz_scope": cn.get("biz_scope"),
            "segments": p.get("main_business") or [],  # 分部占比：东方财富主营构成接口（stock_zygc_em）
            # 一手来源：年报 PyMuPDF 解析抽取的上下游（可能为 None，由渲染层判断是否展示）
            "business_chain": p.get("business_chain") or None,
        },
        "governance": {
            "holders": p.get("holders") or [],
            # 前十大股东名册：最新定期报告披露口径（含股东性质/总持股/质押数量），
            # 优先于东财流通快照口径的 holders（渲染层判断）
            "report_holders": p.get("report_holders") or None,
            "share_capital": _cap,
            "dividend": p.get("dividend") or [],
            # 股份回购实施结果：一手来源为年报 PDF 抽取（main.py 注入 profile["repurchase"]）。
            # 此前该字段从未被填充，导致 LLM 凭记忆编造「回购 12,425,123 股」——真值为 2,979,800 股。
            "repurchase": p.get("repurchase") or None,
            # 实际控制人：年报一手口径（含一致行动人），与上方 holders 的
            # 「最新报告期直接持股快照」是两个不同期间口径，需并存以免弱化控股识别。
            "actual_controller": p.get("actual_controller") or None,
            "pledge": p.get("pledge") or [],
            "pledge_summary": _pledge_summary(p.get("pledge") or [], _cap),
        },
        "industry": {
            "industry_cninfo": cn.get("industry_cninfo"),
            "industry_sw": p.get("industry"),
            "peers": p.get("peers") or [],  # 同业可比：申万二级（get_industry_peers_overview，非东财源）
        },
        # 财务层：引擎指标（多为 None，见 build_financial_summary 注释）+
        # 从三表/指标表直接计算的真实摘要（概况报告「财务速写」唯一可信数据源）
        "financials": {
            **_financials_from(result),
            "summary": p.get("fin_summary") or {},
        },
        "dynamics": {
            "news": p.get("news") or [],
        },
        # P2-E1：行情与资金面（延时行情 + 融资融券）。main.py 已注入 profile["market"]，
        # 此处原样装配进 layers，供 overview.py 渲染「行情与资金面」模块与估值坐标覆盖。
        "market": {
            "quote": (p.get("market") or {}).get("quote") or {},
            "margin": (p.get("market") or {}).get("margin") or {},
        },
    }
    return {
        "code": result.code,
        "name": result.name,
        "period": result.period,
        "layers": layers,
        "validation": {
            "missing_marked": vr.missing_marked,
            "warnings": vr.warnings,
            "suspicious_llm_numbers": vr.suspicious_llm_numbers,
        },
    }
