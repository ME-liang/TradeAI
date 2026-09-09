#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从东方财富公开行情接口(push2delay)拉取真实 A 股全市场个股列表，
按总市值降序排序后生成 frontend/data/stocks.json。

该接口为公开 HTTP 接口，服务端请求不受浏览器跨域(CORS)限制。
用法:
    python fetch_stocks.py [单页数量, 默认 1000]
生成:
    frontend/data/stocks.json
      -> {"updated","count","stocks":[{name,code,ind,price,chg,dir,
                                       mcap,mcap_disp,fcap,fcap_disp}]}
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import re

HOSTS = [
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
]
# f12=代码 f14=名称 f2=最新价(元) f3=涨跌幅(%) f4=涨跌额 f15=最高 f16=最低 f17=今开
# f5=成交量(手) f6=成交额(元) f8=换手率(%) f9=市盈率TTM f23=市净率
# f20=总市值(元) f21=流通市值(元) f100=所属行业
# 注: ROE / 毛利率 / 资产负债率 / 营收同比 行情快照接口(push2delay clist)不提供,
# 统一不在此请求; 真实值由 server.handle_api_stock 按个股调用 fetch_fundamentals() 获取。
FIELDS = "f12,f14,f2,f3,f4,f15,f16,f17,f5,f6,f8,f9,f23,f20,f21,f100"
# 沪深京 A 股 (沪市 m:0+t:6 / 创业板 m:0+t:80 / 深市 m:1+t:2 / 科创板 m:1+t:23 / 京市 m:0+t:90)
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:90"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 接口返回的 f100 是申万二级行业, 部分名称带层级后缀(如 "银行Ⅱ" / "白酒Ⅱ")
# 主流行情软件(如同花顺行业板块)展示细分行业时不带该后缀, 此处统一清理以保持一致
def norm_industry(s):
    s = (s or "").strip()
    s = re.sub(r"[ⅠⅡⅢⅣ]+$", "", s).strip()
    return s or "—"
REF = "https://quote.eastmoney.com/"
HEADERS = {"User-Agent": UA, "Referer": REF}


def fetch(pn, pz):
    params = {
        "fs": FS,
        "fields": FIELDS,
        "pn": pn,
        "pz": pz,
        "po": "1",
        "np": "1",
        "fltt": "2",   # 接口直接返回浮点价格(元)与涨跌幅(%)
        "invt": "2",
    }
    last_err = None
    for base in HOSTS:
        url = base + "/api/qt/clist/get?" + urllib.parse.urlencode(params)
        # 优先用 curl（沙箱/部分环境 urllib 直连被重置时更稳）
        try:
            out = subprocess.check_output(
                ["curl", "-s", "-m", "20", "-A", UA, "-e", REF, url],
                timeout=25,
            )
            if out.strip():
                return json.loads(out.decode("utf-8"))
        except Exception:
            pass
        # 回退 urllib
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("所有数据源均不可用")


def fetch_all(pz):
    """分页拉取全市场，直到取满 total。

    注意：东方财富公开接口单页实际最多返回约 100 条（与请求的 pz 无关），
    因此按页循环翻页；每页最多重试 3 次以应对偶发连接重置。
    """
    rows = []
    total = 0
    pn = 1
    empty_streak = 0
    while True:
        payload = None
        for attempt in range(3):
            try:
                payload = fetch(pn, pz)
                break
            except Exception:
                time.sleep(1.0)
        if payload is None:
            break
        data = payload.get("data") or {}
        if pn == 1:
            total = data.get("total") or 0
        diff = data.get("diff") or []
        items = list(diff.values()) if isinstance(diff, dict) else list(diff)
        if not items:
            empty_streak += 1
            if empty_streak >= 2:
                break
            pn += 1
            time.sleep(0.3)
            continue
        empty_streak = 0
        rows.extend(items)
        if total and len(rows) >= total:
            break
        pn += 1
        time.sleep(0.12)   # 轻微限速，避免被重置
    return rows


def to_num(raw):
    if raw is None or raw == '-':
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def fmt_mcap(yuan):
    if yuan is None:
        return "--"
    y = float(yuan)
    if y >= 1e12:
        return "%.2f万亿" % (y / 1e12)
    if y >= 1e8:
        return "%.2f亿" % (y / 1e8)
    if y >= 1e4:
        return "%.2f万" % (y / 1e4)
    return "%.2f" % y


def fmt_vol(hand):
    """成交量接口返回单位为「手」(1 手 = 100 股)。"""
    if hand is None:
        return "--"
    v = float(hand)
    if v >= 1e8:
        return "%.2f亿手" % (v / 1e8)
    if v >= 1e4:
        return "%.2f万手" % (v / 1e4)
    return "%.0f手" % v


def fmt_amount(yuan):
    """成交额单位为「元」。"""
    if yuan is None:
        return "--"
    y = float(yuan)
    if y >= 1e8:
        return "%.2f亿" % (y / 1e8)
    if y >= 1e4:
        return "%.2f万" % (y / 1e4)
    return "%.2f" % y


def fmt_ratio(x):
    """市盈率 / 市净率 / 换手率等带百分号或纯数值的指标。"""
    if x is None:
        return "--"
    return "%.2f" % float(x)


def fetch_market_records():
    """拉取全市场并整理为统一 record 列表（默认按总市值降序）。

    返回: [{"name","code","ind","price","chg","dir",
            "mcap","mcap_disp","fcap","fcap_disp"}, ...]
    供 server.py 的内存缓存 / 兜底 JSON 共用，避免重复取数逻辑。
    """
    raw = fetch_all(1000)
    stocks = []
    for v in raw:
        if not isinstance(v, dict):
            continue
        pv = to_num(v.get("f2"))          # 最新价
        cv = to_num(v.get("f3"))          # 涨跌幅(%)
        mcap = to_num(v.get("f20"))        # 总市值(元)
        fcap = to_num(v.get("f21"))        # 流通市值(元)
        openp = to_num(v.get("f17"))       # 今开
        high = to_num(v.get("f15"))        # 最高
        low = to_num(v.get("f16"))         # 最低
        chg_amt = to_num(v.get("f4"))      # 涨跌额
        vol = to_num(v.get("f5"))          # 成交量(手)
        amount = to_num(v.get("f6"))       # 成交额(元)
        turnover = to_num(v.get("f8"))     # 换手率(%)
        pe = to_num(v.get("f9"))           # 市盈率TTM
        pb = to_num(v.get("f23"))          # 市净率
        # 注: 基本面四指标(ROE/毛利率/资产负债率/营收同比) 行情快照接口(push2delay clist)不提供,
        # 统一置 None; 真实值由 server.handle_api_stock 按个股调用 fetch_fundamentals() 获取。
        roe = None
        debt_ratio = None
        rev_growth = None
        gross = None                       # 毛利率: 行情快照不提供, 由 fetch_fundamentals 补充
        price_str = ("¥%.2f" % pv) if pv is not None else "--"
        if cv is None:
            chg_str, cv0 = "--", 0.0
        else:
            chg_str, cv0 = (("+" if cv > 0 else "") + ("%.2f" % cv) + "%"), cv
        if chg_amt is None:
            chg_amt_str = "--"
        elif chg_amt > 0:
            chg_amt_str = "+" + "%.2f" % chg_amt
        else:
            chg_amt_str = "%.2f" % chg_amt
        stocks.append({
            "name": (v.get("f14") or "").strip(),
            "code": v.get("f12"),
            "ind": norm_industry(v.get("f100")),
            "price": price_str,
            "chg": chg_str,
            "chg_num": cv0,         # 涨跌幅数值(%), 用于后端排序
            "dir": "up" if cv0 > 0 else ("down" if cv0 < 0 else "flat"),
            "open": openp,
            "high": high,
            "low": low,
            "chg_amount": chg_amt,
            "chg_amount_disp": chg_amt_str,
            "vol": vol,
            "vol_disp": fmt_vol(vol),
            "amount": amount,
            "amount_disp": fmt_amount(amount),
            "turnover": turnover,
            "turnover_disp": (("%.2f%%" % turnover) if turnover is not None else "--"),
            "pe": pe,
            "pe_disp": fmt_ratio(pe),
            "pb": pb,
            "pb_disp": fmt_ratio(pb),
            "mcap": mcap,            # 总市值(元)，用于排序
            "mcap_disp": fmt_mcap(mcap),
            "fcap": fcap,            # 流通市值(元)，用于排序
            "fcap_disp": fmt_mcap(fcap),
            # 基本面四指标: 行情快照接口不提供, 置 None/--; 真实值由后端 fetch_fundamentals 按个股补充
            "roe": roe,
            "roe_disp": "--",
            "gross": gross,
            "gross_disp": "--",
            "debt_ratio": debt_ratio,
            "debt_ratio_disp": "--",
            "rev_growth": rev_growth,
            "rev_growth_disp": "--",
        })
    stocks = [s for s in stocks if s["name"] and s["code"]]
    # 默认按总市值降序（无市值者排末尾）
    stocks.sort(key=lambda s: (s["mcap"] is None, -(s["mcap"] or 0)))
    return stocks


def main():
    try:
        stocks = fetch_market_records()
    except Exception as e:
        print("拉取失败:", e)
        sys.exit(1)

    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "stocks.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {"updated": time.strftime("%Y-%m-%d %H:%M"),
             "count": len(stocks), "stocks": stocks},
            f, ensure_ascii=False, indent=2,
        )
    print("已写入 %d 支股票(按总市值降序) -> %s" % (len(stocks), out_path))


if __name__ == "__main__":
    main()
