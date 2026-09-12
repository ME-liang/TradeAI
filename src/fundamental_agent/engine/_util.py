"""引擎共用的数值工具。

从 AkShare 返回的 DataFrame 中抽取指标、计算同比/趋势/归一化柱高等，
全部做 None 容错，缺失即返回 None / 空，由渲染层回退到占位数据。
"""

from __future__ import annotations

import pandas as pd


def num(x):
    try:
        if x is None or pd.isna(x):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt(x, nd=1, suffix=""):
    v = num(x)
    return "—" if v is None else f"{v:.{nd}f}{suffix}"


def pct(x, nd=1):
    return fmt(x, nd, "%")


def ppx(delta, nd=1):
    """带符号的百分点差值：+1.2pp / -0.8pp。"""
    v = num(delta)
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "-"
    return f"{sign}{abs(v):.{nd}f}pp"


def x_str(x, nd=2, suffix=""):
    return fmt(x, nd, suffix)


def series_last(df, col, n=5):
    """返回最近 n 个非空样本 [(日期串, 值), ...]。"""
    if df is None or df.empty or col not in df.columns:
        return []
    sub = df[["日期", col]].dropna(subset=[col]).tail(n)
    out = []
    for _, r in sub.iterrows():
        d = r["日期"]
        ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
        out.append((ds, float(r[col])))
    return out


def latest_yoy(df, col):
    """返回 (最新值, 去年同期值)。"""
    if df is None or df.empty or col not in df.columns:
        return (None, None)
    d = df.dropna(subset=[col])
    if d.empty:
        return (None, None)
    last = d.iloc[-1]
    cur = num(last[col])
    ld = last["日期"]
    prev = d[d["日期"] == ld - pd.DateOffset(years=1)]
    pv = num(prev.iloc[0][col]) if not prev.empty else None
    return (cur, pv)


def _period_label(ds):
    ds = str(ds)
    if len(ds) >= 10 and ds[4] == "-" and ds[7] == "-":
        y, m = ds[:4], ds[5:7]
        return {"12": y, "06": f"{y}H1", "03": f"{y}Q1", "09": f"{y}Q3"}.get(m, y)
    return ds[:7]


def bars_from_series(series, nd=1, deep_last=True):
    """series: [(日期, 值)] → 渲染用柱状数据（高度按最大值归一化）。"""
    if not series:
        return []
    mx = max([abs(v) for _, v in series] + [1e-9])
    bars = []
    for i, (ds, v) in enumerate(series):
        h = round(abs(v) / mx * 100, 1)
        bars.append(
            {
                "v": f"{v:.{nd}f}",
                "h": h,
                "deep": (deep_last and i == len(series) - 1),
                "yr": _period_label(ds),
            }
        )
    return bars


def cagr(statement_df, keyword, years=3):
    """依据绝对额报表，计算关键字科目的 years 年复合增速。"""
    if statement_df is None or statement_df.empty:
        return None
    cand = [i for i in statement_df.index if keyword in str(i)]
    if not cand:
        return None
    row = statement_df.loc[cand[0]]
    vals = [(str(c), num(row[c])) for c in row.index if num(row[c]) is not None]
    if len(vals) < 2:
        return None
    vals.sort(key=lambda x: x[0])
    annual = [v for v in vals if v[0].endswith("12-31")]
    if len(annual) >= years:
        start = annual[-years][1]
        end = annual[-1][1]
        n = years
    else:
        start = vals[0][1]
        end = vals[-1][1]
        n = len(vals) - 1
    if start is None or end is None or start <= 0 or n <= 0:
        return None
    return (end / start) ** (1 / n) - 1


def median_of(peers_df, col):
    """取 peers DataFrame 某列的中位数（忽略 None）。"""
    if peers_df is None or peers_df.empty or col not in peers_df.columns:
        return None
    vals = [num(v) for v in peers_df[col].values if num(v) is not None]
    if not vals:
        return None
    return float(pd.Series(vals).median())


# ---------------------------------------------------------------------------
# 指标视图（年报 / 中报 / 单季度）共用工具
# ---------------------------------------------------------------------------
def ind_dates(df, kind, n=3):
    """从财务分析指标 DataFrame（含『日期』列）挑选视图对应的报告期。

    返回 (date_strs, labels)：date_strs 为 'YYYY-MM-DD' 字符串列表（升序），
    labels 为对应的可读标签。kind 取值：
      - default：取最后 n 期（混合，保持原行为）
      - annual ：每年 12 月，取最后 n 期
      - interim ：每年 06 月，取最后 n 期
      - quarterly：季末(03/06/09/12)，取最后 n 期
    """
    if df is None or df.empty or "日期" not in df.columns:
        return [], []
    dts = sorted(pd.to_datetime(df["日期"], errors="coerce").dropna().unique().tolist())
    if kind == "annual":
        sel = [d for d in dts if d.month == 12][-n:]
    elif kind == "interim":
        sel = [d for d in dts if d.month == 6][-n:]
    elif kind == "quarterly":
        sel = [d for d in dts if d.month in (3, 6, 9, 12)][-n:]
    else:
        sel = dts[-n:]
    labels = [_period_label(d.strftime("%Y-%m-%d")) for d in sel]
    return [d.strftime("%Y-%m-%d") for d in sel], labels


def ind_value_at(df, col, date_str):
    """返回指标表 df 在 date_str('YYYY-MM-DD') 处 col 列的值（缺失为 None）。"""
    if df is None or df.empty or col not in df.columns:
        return None
    try:
        t = pd.to_datetime(date_str)
    except Exception:
        return None
    sub = df[pd.to_datetime(df["日期"], errors="coerce") == t]
    if sub.empty:
        return None
    return num(sub[col].iloc[-1])


def cagr_at_period(income, keyword, end_period_str, years=3):
    """收入/利润在 end_period_str('YYYYMMDD') 处截止的 years 年复合增速（小数）。

    优先取截止年份之前的年报(12-31)列做窗口；不足则放宽到所有 <= 截止年的列。
    """
    if income is None or income.empty:
        return None
    cand = [i for i in income.index if keyword in str(i)]
    if not cand:
        return None
    row = income.loc[cand[0]]
    cols = []
    for c in income.columns:
        cs = str(c)
        if len(cs) == 8 and cs.isdigit():
            v = num(row[c])
            if v is not None:
                cols.append((cs, v))
    if len(cols) < 2:
        return None
    end_year = int(end_period_str[:4])
    annual = [(c, v) for c, v in cols if c.endswith("1231") and int(c[:4]) <= end_year]
    if len(annual) < 2:
        annual = [(c, v) for c, v in cols if int(c[:4]) <= end_year]
    if len(annual) < 2:
        return None
    annual.sort(key=lambda x: x[0])
    window = annual[-years:] if len(annual) >= years else annual
    start, end = window[0][1], window[-1][1]
    n = len(window) - 1
    if start is None or end is None or start <= 0 or n <= 0:
        return None
    return (end / start) ** (1 / n) - 1


def metric_value_at(acc, df, income, date_str):
    """取某一指标在指定报告期的值。

    acc 支持三种形式：
      - str：指标表列名（缺失则尝试候选列名列表中的下一个）
      - ("cagr", keyword)：由利润表推导的滚动 years 年复合增速
      - callable(df, income, date_str)：自定义（如现金循环周期 = 两周转天数之和）
    """
    if callable(acc):
        return acc(df, income, date_str)
    if isinstance(acc, (list, tuple)) and acc and acc[0] == "cagr":
        endp = str(date_str).replace("-", "")[:8]
        return cagr_at_period(income, acc[1], endp)
    cols = acc if isinstance(acc, (list, tuple)) else [acc]
    for c in cols:
        v = ind_value_at(df, c, date_str)
        if v is not None:
            return v
    return None
