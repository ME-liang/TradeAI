#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TradeAI 前端本地服务 + 股票数据代理。

职责:
  1. 同源静态服务: index.html / stock.html / css / js / data(兜底 JSON)
  2. /api/stocks 接口: 代理东方财富 push2delay 真实行情，按「交易日」缓存全市场列表，
     支持 排序(sort) + 方向(order) + 分页(page/pageSize) + 关键词过滤(q)。

为什么需要后端: 浏览器直连东方财富接口会被 CORS 拦截；由本服务在服务端取数，
前端只调同源 /api/stocks 即可，无需暴露第三方接口、也不受跨域限制。

用法:
    python server.py [端口, 默认 8000]
访问:
    http://127.0.0.1:8000/stock.html
    http://127.0.0.1:8000/api/stocks?sort=mcap&order=desc&page=1&pageSize=6
"""
import json
import os
import re
import sys
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REF = "https://quote.eastmoney.com/"

# 复用 fetch_stocks 的取数逻辑(已抽取为 fetch_market_records)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_stocks  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 报告生成服务(FastAPI)地址; 由前端同源反向代理 /api/report*, 免 CORS。
# 启动时需另起: uvicorn src.fundamental_agent.api.server:app --host 127.0.0.1 --port 8002
# 端口口径: 8000=前端(本服务) / 8002=报告 API。
# 原默认值 8001 与线上实际部署不符且已确死（P3-4 实测直连 WinError 10061 连接被拒），
# 一旦漏设环境变量就会出现「一键生成」报「报告服务不可达」的假故障，故默认值改为 8002。
REPORT_API_BASE = os.environ.get("REPORT_API_BASE", "http://127.0.0.1:8002").rstrip("/")

# ===== 登录态路由访问控制（AUTH-A 2026-09-11） =====
# 会话 Cookie 名与 FastAPI auth_service.SESSION_COOKIE 一致; 会话校验经 8002 的
# /api/auth/me（SQLite 会话表是单一事实源, 过期/无效 → 401）。未登录访问受保护
# 页面一律 302 → /login.html?next=…；API 一律 401 JSON。
SESSION_COOKIE_NAME = "ta_session"
_PUBLIC_EXACT = {"/login.html", "/register.html", "/reset_password.html", "/favicon.ico"}
_PUBLIC_PREFIXES = ("/css/", "/js/", "/api/auth")


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)

# ===== 交易日缓存 =====
_cache_lock = threading.Lock()
_cache = {"date": None, "records": None}

# 排序字段映射: 前端传 mcap/fcap/chg -> 后端 record 字段
SORT_FIELDS = {
    "mcap": "总市值",
    "fcap": "流通市值",
    "chg": "涨跌幅",
}


def _trading_day():
    """以自然日作为缓存键(收盘后数据当日不变, 盘中刷新只需次日)。"""
    return datetime.date.today().isoformat()


def get_records():
    """返回全市场 record 列表, 按交易日缓存, 避免每次请求都拉全量。"""
    day = _trading_day()
    with _cache_lock:
        if _cache["date"] == day and _cache["records"] is not None:
            return _cache["records"]
    # 缓存未命中: 重新拉取(可能在首请求/跨日时发生, 放锁外避免阻塞其它请求)
    try:
        recs = fetch_stocks.fetch_market_records()
    except Exception as e:
        # 拉取失败时, 若已有旧缓存则降级使用, 否则返回空
        with _cache_lock:
            if _cache["records"] is not None:
                print("[warn] 实时拉取失败, 使用旧缓存:", e)
                return _cache["records"]
        print("[error] 实时拉取失败:", e)
        return []
    with _cache_lock:
        _cache["date"] = day
        _cache["records"] = recs
    return recs


def _prefetch():
    """启动时后台预热缓存, 让首个前端请求不必等待全市场拉取。"""
    try:
        get_records()
        print("[cache] 已预热全市场股票缓存")
    except Exception as e:
        print("[cache] 预热失败(将在首次请求时重试):", e)


# ===== /api/stocks 业务 =====
def handle_api_stocks(params):
    sort = (params.get("sort") or ["mcap"])[0]
    order = (params.get("order") or ["desc"])[0]
    page = _to_int(params.get("page", ["1"])[0], 1)
    pageSize = _to_int(params.get("pageSize", ["6"])[0], 6)
    q = (params.get("q") or [""])[0].strip().lower()

    # 兼容前端旧式 "mcap_desc" 取值
    if "_" in sort:
        sort, order = sort.split("_", 1)
    if sort not in SORT_FIELDS:
        sort = "mcap"
    if order not in ("asc", "desc"):
        order = "desc"
    if page < 1:
        page = 1
    if pageSize < 1 or pageSize > 100:
        pageSize = 6

    recs = get_records()
    if q:
        recs = [r for r in recs if q in r["name"].lower()
                or q in r["code"].lower()
                or q in (r["ind"] or "").lower()]

    def sort_key(r):
        v = r.get(sort)
        # 缺失值恒排末尾(升降序均如此)
        return v if v is not None else (float("-inf") if order == "desc" else float("inf"))

    recs = sorted(recs, key=sort_key, reverse=(order == "desc"))
    total = len(recs)
    start = (page - 1) * pageSize
    page_items = recs[start:start + pageSize]

    return {
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total": total,
        "page": page,
        "pageSize": pageSize,
        "sort": sort,
        "order": order,
        "keyword": q,
        "stocks": page_items,
    }


def _to_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ===== 单股快照 + K线 =====
KLINE_HOSTS = ["https://push2his.eastmoney.com", "https://push2.eastmoney.com"]


def _secid(code):
    """东方财富 K 线接口需要 市场.代码: 沪市(60/68/9)->1, 其余(深/京)->0。"""
    if code[:1] in ("6", "9"):
        return "1." + code
    return "0." + code


def _tsymbol(code):
    """腾讯/新浪 K 线接口需要 市场前缀+代码: 沪市(6/9)->sh, 其余->sz。与 _secid 同规则。"""
    return ("sh" if (code or "")[:1] in ("6", "9") else "sz") + (code or "")


# ===== 单股真实基本面（ROE / 毛利率 / 资产负债率 / 营收同比）=====
# 行情快照接口(push2delay clist) 不提供这四项; 改为按个股实时获取:
#   - 毛利率(f186) / 资产负债率(f188) / 营收同比(f162): 来自 push2 单股接口(行情, 稳定)
#   - ROE(加权 ROEJQ): 来自东方财富 F10 主要财务指标(RPT_F10_FINANCE_MAINFINADATA)
def _curl_json(url, timeout=20, ref=None):
    try:
        out = subprocess.check_output(
            ["curl", "-s", "-m", str(timeout), "-A", UA, "-e", (ref or REF), url],
            timeout=timeout + 5,
        )
        if out.strip():
            return json.loads(out.decode("utf-8"))
    except Exception:
        return None
    return None


def _curl_json_hosts(hosts, path, timeout=20):
    """依次尝试多个主机, 任一个返回非空 JSON 即采用; 全部失败返回 None。

    用途: 东方财富按主机限流, 单主机被掐时降级到同族其它主机(如 push2 -> push2delay),
    避免四项基本面指标整组变空。
    """
    for host in hosts:
        try:
            d = _curl_json(host.rstrip("/") + path, timeout=timeout)
            if d is not None:
                return d
        except Exception:
            continue
    return None


def _to_num(v):
    if v is None or v in ("-", ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fund_secucode(code):
    p = code[0]
    if p in ("6", "9"):
        suf = "SH"
    elif p in ("4", "8"):
        suf = "BJ"
    else:
        suf = "SZ"
    return code + "." + suf


# akshare 备源熔断: 连续失败达到阈值后当日不再调用(避免每次请求都付出 2-5s 冷导入/网络代价)
_ROE_AK_FAILS = 0
_ROE_AK_DISABLED_DATE = None
_ROE_AK_FAIL_LIMIT = 3


def _prewarm_akshare():
    """启动后后台预热 import akshare(冷导入实测 2-5s), 避免首次落到用户请求上。"""
    def _run():
        try:
            import akshare  # noqa: F401
            print("[info] akshare 预热完成")
        except Exception as e:
            print("[warn] akshare 预热失败(不影响主流程): %s" % e)
    threading.Thread(target=_run, daemon=True).start()


def _fetch_roe_akshare(code):
    """AKShare 财务分析指标取 ROE, 作为东方财富被限流时的备用源(非东财数据, 互不干扰)。

    返回 最新报告期「加权净资产收益率(%)」(语义对齐 F10 的 ROEJQ) 或 None。
    服务启动时会后台预热 import; 连续失败达阈值后当日熔断, 避免拖慢请求。
    """
    global _ROE_AK_FAILS, _ROE_AK_DISABLED_DATE
    today = datetime.date.today().isoformat()
    if _ROE_AK_DISABLED_DATE == today:
        return None
    try:
        import akshare as ak
    except Exception:
        return None
    try:
        start_year = str(datetime.datetime.now().year - 1)
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=start_year)
    except Exception:
        _ROE_AK_FAILS += 1
        if _ROE_AK_FAILS >= _ROE_AK_FAIL_LIMIT:
            _ROE_AK_DISABLED_DATE = today
            print("[warn] akshare ROE 备源连续失败%d次, 当日熔断" % _ROE_AK_FAILS)
        return None
    if df is None or len(df) == 0:
        return None
    _ROE_AK_FAILS = 0  # 成功即重置计数
    # 优先取「加权净资产收益率(%)」(对齐 F10 加权ROE); 退而取「净资产收益率(%)」
    cand = [c for c in df.columns if "加权净资产收益率" in c]
    col = cand[0] if cand else None
    if col is None:
        cand2 = [c for c in df.columns if "净资产收益率" in c]
        col = cand2[0] if cand2 else None
    if col is None:
        return None
    return _to_num(df.iloc[-1][col])


# 单股基本面缓存: {code: (时间戳, 结果字典)}。财务指标低频变化, 短缓存避免每次页面加载重付外网取数成本
_FUND_CACHE = {}
_FUND_LOCK = threading.Lock()
FUND_TTL_OK_S = 600      # 成功结果缓存 10 分钟
FUND_TTL_FAIL_S = 120    # 全失败结果只缓存 2 分钟(允许尽快重试恢复)


def fetch_fundamentals(code):
    """返回单股真实基本面四指标(含展示字符串), 任一数据源失败则该指标为 None。

    来源优先级: 毛利率/资产负债率/营收同比 -> push2 单股接口; ROE -> F10 主要财务指标。
    带 TTL 缓存: 成功 10 分钟, 全失败 2 分钟(限流恢复后可重试)。
    """
    now = time.time()
    with _FUND_LOCK:
        hit = _FUND_CACHE.get(code)
        if hit and now - hit[0] < (FUND_TTL_OK_S if hit[1]["gross"] is not None or hit[1]["roe"] is not None
                                   else FUND_TTL_FAIL_S):
            return dict(hit[1])
    res = _fetch_fundamentals_impl(code)
    with _FUND_LOCK:
        _FUND_CACHE[code] = (now, dict(res))
    return res


def _fetch_fundamentals_impl(code):
    res = {"roe": None, "roe_disp": "--", "gross": None, "gross_disp": "--",
           "debt_ratio": None, "debt_ratio_disp": "--", "rev_growth": None, "rev_growth_disp": "--"}
    secid = _secid(code)
    # 1) push2 单股接口: 毛利率 / 资产负债率 / 营收同比(均为近期报告期口径)
    #    主机降级: push2 被限流时自动切到 push2delay(同族接口, 字段一致)
    PUSH2_HOSTS = ["https://push2.eastmoney.com", "https://push2delay.eastmoney.com"]
    q = urllib.parse.urlencode({"secid": secid, "fields": "f162,f186,f188", "invt": "2", "fltt": "2"})

    def _push2_task():
        # timeout=4: 串行时该步曾占满 8s 预算并拖累整条链路
        d = _curl_json_hosts(PUSH2_HOSTS, "/api/qt/stock/get?" + q, timeout=4)
        row = (d or {}).get("data") or {}
        return {
            "gross": _to_num(row.get("f186")),
            "debt_ratio": _to_num(row.get("f188")),
            "rev_growth": _to_num(row.get("f162")),
        }

    # 2) ROE 主源: 东方财富 F10 主要财务指标(ROEJQ, 加权); 主机降级 datacenter -> datacenter-web
    def _f10_task():
        try:
            sc = _fund_secucode(code)
            fq = urllib.parse.urlencode({"type": "RPT_F10_FINANCE_MAINFINADATA", "secucode": sc,
                                         "client": "PC", "source": "HSF10"})
            F10_HOSTS = ["https://datacenter.eastmoney.com", "https://datacenter-web.eastmoney.com"]
            fd = _curl_json_hosts(F10_HOSTS, "/securities/api/data/get?" + fq, timeout=5)
            fdata = ((fd or {}).get("result") or {}).get("data") or []
            return _to_num(fdata[0].get("ROEJQ")) if fdata else None
        except Exception as e:
            print("[warn] F10 ROE 获取失败(code=%s): %s" % (code, e))
            return None

    # push2 与 F10 两个外部请求互不依赖——串行会让耗时叠加(实测冷请求 4-5s),
    # 改为并行后总耗时≈两者较慢者。
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_push = ex.submit(_push2_task)
            f_roe = ex.submit(_f10_task)
            try:
                p = f_push.result()
                if p.get("gross") is not None:
                    res["gross"] = p["gross"]
                if p.get("debt_ratio") is not None:
                    res["debt_ratio"] = p["debt_ratio"]
                if p.get("rev_growth") is not None:
                    res["rev_growth"] = p["rev_growth"]
            except Exception as e:
                print("[warn] push2 基本面获取失败(code=%s): %s" % (code, e))
            try:
                roe = f_roe.result()
                if roe is not None:
                    res["roe"] = roe
            except Exception as e:
                print("[warn] F10 ROE 获取失败(code=%s): %s" % (code, e))
    except Exception as e:
        print("[warn] 并行取数异常, 回退串行(code=%s): %s" % (code, e))
        p = _push2_task()
        if p.get("gross") is not None:
            res["gross"] = p["gross"]
        if p.get("debt_ratio") is not None:
            res["debt_ratio"] = p["debt_ratio"]
        if p.get("rev_growth") is not None:
            res["rev_growth"] = p["rev_growth"]
        if res["roe"] is None:
            res["roe"] = _f10_task()
    # 3) ROE 备用源: AKShare 财务分析指标(非东方财富, 不受东财限流影响)
    #    仅当主源未取得时才启用, 避免不必要的外部依赖开销
    if res["roe"] is None:
        try:
            roe = _fetch_roe_akshare(code)
            if roe is not None:
                res["roe"] = roe
        except Exception as e:
            print("[warn] AKShare ROE 获取失败(code=%s): %s" % (code, e))
    # 生成展示字符串
    res["roe_disp"] = "--" if res["roe"] is None else ("%.2f%%" % res["roe"])
    res["gross_disp"] = "--" if res["gross"] is None else ("%.2f%%" % res["gross"])
    res["debt_ratio_disp"] = "--" if res["debt_ratio"] is None else ("%.2f%%" % res["debt_ratio"])
    if res["rev_growth"] is None:
        res["rev_growth_disp"] = "--"
    else:
        res["rev_growth_disp"] = ("+%.2f%%" % res["rev_growth"]) if res["rev_growth"] > 0 else ("%.2f%%" % res["rev_growth"])
    return res


def _kline_eastmoney(code, klt, n):
    """东方财富: 历史最全, 但 push2his 主机常被按 IP 限流/封禁(exit 56)。"""
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": str(klt),
        "secid": _secid(code),
        "beg": "0",
        "end": "20500101",
        "lmt": str(n),
        "fqt": "1",
    }
    path = "/api/qt/stock/kline/get?" + urllib.parse.urlencode(params)
    data = None
    for host in KLINE_HOSTS:
        data = _curl_json(host.rstrip("/") + path, timeout=20)
        if data:
            break
    if not data:
        raise RuntimeError("东方财富K线: 无响应")
    klines = (data.get("data") or {}).get("klines") or []
    if not klines:
        raise RuntimeError("东方财富K线: 返回为空")
    klines = klines[-n:] if len(klines) > n else klines
    dates, candle, vols = [], [], []
    for k in klines:
        p = k.split(",")
        o, c, h, l, v = float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])
        dates.append(p[0][:10])
        candle.append([o, c, l, h])
        vols.append(int(float(v)))
    return {"dates": dates, "candle": candle, "vols": vols}


def _kline_tencent(code, klt, n):
    """腾讯财经: 日/周/月 + 沪深 + 前复权。

    返回 data.<symbol>.qfqday / qfqweek / qfqmonth, 每项结构为
    [日期, 开, 收, 高, 低, 成交量(手)] —— 字段顺序与东方财富 klines 前 6 位一致, 可直接复用。
    """
    period = {101: "day", 102: "week", 103: "month"}.get(_to_int(klt, 101), "day")
    sym = _tsymbol(code)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + urllib.parse.urlencode(
        {"param": "%s,%s,,,%d,qfq" % (sym, period, n)}
    )
    data = _curl_json(url, timeout=20, ref="https://gu.qq.com/")
    if not data:
        raise RuntimeError("腾讯K线: 无响应")
    root = (data.get("data") or {}).get(sym) or {}
    arr = root.get("qfq" + period) or root.get(period) or []
    if not arr:
        raise RuntimeError("腾讯K线: 返回为空")
    arr = arr[-n:] if len(arr) > n else arr
    dates, candle, vols = [], [], []
    for a in arr:
        o, c, h, l, v = float(a[1]), float(a[2]), float(a[3]), float(a[4]), float(a[5])
        dates.append(str(a[0])[:10])
        candle.append([o, c, l, h])
        vols.append(int(float(v)))
    return {"dates": dates, "candle": candle, "vols": vols}


def _kline_sina(code, klt, n):
    """新浪财经: 最后兜底。

    字段为 {day, open, high, low, close, volume(股)} —— 顺序与东财不同, 且量为「股」需换算成「手」。
    """
    scale = {101: 240, 102: 1200, 103: 7200}.get(_to_int(klt, 101), 240)
    sym = _tsymbol(code)
    url = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?" + urllib.parse.urlencode(
        {"symbol": sym, "scale": str(scale), "ma": "no", "datalen": str(n)}
    )
    arr = _curl_json(url, timeout=20, ref="https://finance.sina.com.cn/")
    if not arr:
        raise RuntimeError("新浪K线: 返回为空")
    arr = arr[-n:] if len(arr) > n else arr
    dates, candle, vols = [], [], []
    for a in arr:
        o, h, l, c = float(a["open"]), float(a["high"]), float(a["low"]), float(a["close"])
        dates.append(str(a["day"])[:10])
        candle.append([o, c, l, h])
        vols.append(int(float(a["volume"]) / 100.0))
    return {"dates": dates, "candle": candle, "vols": vols}


# K 线数据源降级顺序: 东方财富历史最全但 push2his 常被限流封禁, 失败时依次降级
KLINE_SOURCES = [
    ("eastmoney", _kline_eastmoney),
    ("tencent", _kline_tencent),
    ("sina", _kline_sina),
]

# K线结果短缓存: 详情页每次进页/切周期都会请求, 而上游单次 1-2s 且常被限流。
# 历史 K 线当日不变, 只有最后一根随盘口变动 —— 60s TTL 下最后一根最多滞后 1 分钟,
# 对图表展示可接受(若需盘口实时, 走行情快照接口而非 K 线)。
_KLINE_CACHE = {}          # {(code, klt, n): (ts, data)}
_KLINE_CACHE_TTL = 60      # 秒
_KLINE_LOCK = threading.Lock()


def _kline_cache_get(key):
    now = time.time()
    with _KLINE_LOCK:
        hit = _KLINE_CACHE.get(key)
        if hit and now - hit[0] < _KLINE_CACHE_TTL:
            return hit[1]
    return None


def _kline_cache_put(key, data):
    with _KLINE_LOCK:
        _KLINE_CACHE[key] = (time.time(), data)


# ===== 公司资讯（公告 + 资讯, 服务端 curl 取东方财富, 与其他接口同范式） =====
NEWS_CACHE = {}    # code -> (时间戳, items); 列表短缓存, 避免频繁打上游
NEWS_TTL = 300     # 列表缓存 5 分钟


def _strip_html(t):
    import html as _htmlmod
    t = re.sub(r"<[^>]+>", "", t or "")
    return _htmlmod.unescape(t).strip()


def _fetch_news_list(code):
    """合并 公告+资讯 两个列表接口, 按发布时间倒序, 最多 20 条。

    返回 items 列表; 两个源都失败时返回 None(前端据此区分「失败」与「暂无」)。
    每条: {id, type, title, time}; id 以 A(公告)/N(资讯) 前缀区分详情通道。
    """
    now = time.time()
    cached = NEWS_CACHE.get(code)
    if cached and now - cached[0] < NEWS_TTL:
        return cached[1]
    items = []
    # 1) 公告列表(np-anotice): 标题/展示时间/art_code, 详情走 np-cnotice
    q1 = urllib.parse.urlencode({"sr": "-1", "page_size": "15", "page_index": "1",
                                 "ann_type": "A", "client_source": "web", "stock_list": code})
    d1 = _curl_json_hosts(["https://np-anotice-stock.eastmoney.com"],
                          "/api/security/ann?" + q1, timeout=10)
    for it in ((d1 or {}).get("data") or {}).get("list") or []:
        items.append({"id": "A" + (it.get("art_code") or ""), "type": "公告",
                      "title": (it.get("title") or "").strip(),
                      "time": (it.get("display_time") or "")[:19]})
    # 2) 资讯列表(np-listapi): 个股相关新闻, Art_* 字段, 详情走文章页正文提取
    q2 = urllib.parse.urlencode({"client": "web", "mTypeAndCode": _secid(code),
                                 "type": "1", "pageSize": "15", "pageIndex": "1"})
    d2 = _curl_json_hosts(["https://np-listapi.eastmoney.com"],
                          "/comm/web/getListInfo?" + q2, timeout=10)
    for it in ((d2 or {}).get("data") or {}).get("list") or []:
        items.append({"id": "N" + (it.get("Art_Code") or ""), "type": "资讯",
                      "title": (it.get("Art_Title") or "").strip(),
                      "time": (it.get("Art_ShowTime") or "")[:19]})
    # 双源都失败 -> None; 有任一源成功 -> 正常返回(可能为空)
    if d1 is None and d2 is None:
        return None
    items = [x for x in items if x["id"] and x["title"]]
    items.sort(key=lambda x: x["time"], reverse=True)
    items = items[:20]
    NEWS_CACHE[code] = (now, items)
    return items


# 资讯文章页尾部固定模板内容(与正文无关, 提取后剔除): 文末来源标注、微信分享引导、二维码关注等
_NEWS_JUNK_RE = re.compile(
    r"文章来源[:：]|打开微信|点击底部|扫一扫|朋友圈|扫描二维码|官网微信|微信公众号|扫码关注|责任编辑"
)


def _clean_news_text(txt):
    """剔除资讯正文中的固定模板内容(逐行过滤 + 去掉行尾'（文章来源：…）'后缀)。"""
    lines = []
    for x in txt.split("\n"):
        x = x.strip()
        # 去掉行尾的来源标注后缀, 如 "……（文章来源：界面新闻）"
        x = re.sub(r"[（(]文章来源[^）)]*[）)]\s*$", "", x).strip()
        if x and not _NEWS_JUNK_RE.search(x):
            lines.append(x)
    return "\n".join(lines)


def _fetch_news_detail(code, nid):
    """按 id 前缀取单条详情正文(懒加载, 仅在用户展开时调用)。

    A 前缀=公告: np-cnotice 的 notice_content(全文, 已验证)。
    N 前缀=资讯: 抓文章页 HTML, 提取 ContentBody 内 <p> 文本(已验证质量)。
    失败返回 None。
    """
    if not nid or len(nid) < 2:
        return None
    kind, art = nid[0], nid[1:]
    if kind == "A":
        q = urllib.parse.urlencode({"art_code": art, "client_source": "web", "page_index": "1"})
        d = _curl_json_hosts(["https://np-cnotice-stock.eastmoney.com"],
                             "/api/content/ann?" + q, timeout=10)
        content = (((d or {}).get("data") or {}).get("notice_content") or "").strip()
        return content or None
    if kind == "N":
        url = "http://finance.eastmoney.com/a/%s.html" % art
        try:
            out = subprocess.check_output(
                ["curl", "-s", "-L", "-m", "12", "-A", UA, "-e", "https://finance.eastmoney.com/", url],
                timeout=17,
            )
            raw = out.decode("utf-8", errors="ignore")
        except Exception:
            return None
        m = re.search(r'<div[^>]*id="ContentBody"[^>]*>(.*)', raw, re.S)
        if not m:
            m = re.search(r'<div[^>]*class="txtinfos"[^>]*>(.*)', raw, re.S)
        seg = m.group(1) if m else ""
        ps = re.findall(r"<p[^>]*>(.*?)</p>", seg, re.S)
        lines = [_strip_html(p) for p in ps]
        txt = _clean_news_text("\n".join(x for x in lines if x))
        return txt or None
    return None


def handle_api_news(qs):
    """GET /api/news?code=600519            -> 资讯列表(公告+资讯合并)
       GET /api/news?code=600519&id=Axxx|Nxxx -> 单条详情正文(懒加载)"""
    code = re.sub(r"\D", "", ((qs.get("code") or [""])[0] or ""))
    if len(code) != 6:
        return {"error": "参数 code 无效"}, 400
    nid = ((qs.get("id") or [""])[0] or "").strip()
    if nid:
        txt = _fetch_news_detail(code, nid)
        if txt is None:
            return {"error": "详情获取失败"}, 502
        return {"content": txt}, 200
    items = _fetch_news_list(code)
    if items is None:
        return {"error": "资讯获取失败"}, 502
    return {"news": items}, 200


def fetch_kline(code, klt, n):
    """拉取真实 K 线: 返回 {dates, candle:[o,c,l,h], vols, source}。

    优先命中 60s 缓存; 未命中时东财+腾讯双源并行竞速(口径一致: 均前复权、量为手,
    输出结构已各自归一), 首个非空者胜 —— 东财历史最全为主选, 但其主机常被限流挂起,
    竞速以一次额外请求为代价砍掉尾延迟; 双源均失败再按 KLINE_SOURCES 串行兜底(含新浪)。
    全部失败才抛异常(前端此时退化为合成数据并显式提示, 不再静默)。
    """
    key = (code, klt, n)
    hit = _kline_cache_get(key)
    if hit is not None:
        return hit

    def _try(name, fn):
        try:
            d = fn(code, klt, n)
            return d if (d and d.get("dates")) else None
        except Exception:
            return None

    ex = ThreadPoolExecutor(max_workers=2)
    try:
        futs = {ex.submit(_try, name, fn): name
                for name, fn in (("eastmoney", _kline_eastmoney), ("tencent", _kline_tencent))}
        for fut in as_completed(futs):
            d = fut.result()
            if d:
                d["source"] = futs[fut]
                _kline_cache_put(key, d)
                return d
    finally:
        ex.shutdown(wait=False, cancel_futures=True)   # 不等待落败方, 立即返回
    last_err = None
    for name, fn in KLINE_SOURCES:
        try:
            d = fn(code, klt, n)
            if d and d.get("dates"):
                d["source"] = name
                _kline_cache_put(key, d)
                return d
            last_err = RuntimeError("%s: 返回为空" % name)
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("K线数据源不可用")


# 行业基本面中位数缓存: {行业: (日期, {roe/gross/debt_ratio/rev_growth: 中位数, sample_n: 样本数})}
_IND_FUND_CACHE = {}
_IND_FUND_LOCK = threading.Lock()
# 样本数从 20 降到 12: 中位数统计上仍稳定, 请求量减少 40%(详情页首屏已不等待该取数,
# 改为异步补齐, 故预算保持宽裕以保证成功率)。
IND_FUND_SAMPLE_N = 12   # 按总市值取前 N 只同行股票做样本(控制请求量, 中位数足够稳定)
IND_FUND_WORKERS = 12    # 并发取数线程数
IND_FUND_BUDGET_S = 20   # 行业取数总时长预算(秒), 超时用已完成部分计算中位数


def _industry_fund_medians(ind, peers):
    """按行业按需取真实基本面中位数(按「行业+日期」缓存)。

    行情快照不含基本面字段(stocks.json 中 roe/gross 恒为 None), 行业中位数只能
    逐股实时取数: 按总市值取前 N 只同行做样本, 线程池并发调 fetch_fundamentals。
    全部失败时不写缓存(下次请求重试), 避免限流期间污染整日数据。
    """
    today = datetime.date.today().isoformat()
    with _IND_FUND_LOCK:
        hit = _IND_FUND_CACHE.get(ind)
        if hit and hit[0] == today:
            return hit[1]
    sample = sorted(
        (r for r in peers if r.get("mcap") is not None),
        key=lambda r: r["mcap"], reverse=True,
    )[:IND_FUND_SAMPLE_N]
    vals = {"roe": [], "gross": [], "debt_ratio": [], "rev_growth": []}
    if sample:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _safe_fund(c):
            try:
                return fetch_fundamentals(c)
            except Exception:
                return {}

        codes = [r["code"] for r in sample]
        ex = ThreadPoolExecutor(max_workers=IND_FUND_WORKERS)
        try:
            # 总时长预算: 限流/慢网环境下超时即用已完成部分算中位数, 不让用户干等
            deadline = time.time() + IND_FUND_BUDGET_S
            futs = [ex.submit(_safe_fund, c) for c in codes]
            for fut in as_completed(futs):
                fund = fut.result()
                for k in vals:
                    v = fund.get(k)
                    if v is not None:
                        vals[k].append(v)
                if time.time() > deadline:
                    break
        except Exception as e:
            print("[warn] 行业基本面并发取数异常(ind=%s): %s" % (ind, e))
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    def median(xs):
        if not xs:
            return None
        xs = sorted(xs)
        n = len(xs)
        return round(xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2, 2)
    out = {k: median(v) for k, v in vals.items()}
    out["sample_n"] = len(sample)
    # 至少毛利率中位数取到了才缓存(毛利率来源最稳), 否则不缓存、下次重试
    if out["gross"] is not None:
        with _IND_FUND_LOCK:
            _IND_FUND_CACHE[ind] = (today, out)
    return out


def handle_api_stock(params):
    code = (params.get("code") or [""])[0].strip()
    if not code:
        return ({"error": "缺少 code 参数"}, 400)
    recs = get_records()
    rec = None
    for r in recs:
        rc = r.get("code") or ""
        if rc == code or code in rc:
            rec = r
            break
    if not rec:
        return ({"error": "未找到该股票(code=%s)" % code}, 404)
    # 同行业市盈率/市净率中位数(取正值, 避免亏损股拉低均值)
    ind = rec.get("ind")
    peers = [r for r in recs if r.get("ind") == ind]
    pe_vals = sorted(r["pe"] for r in peers if r.get("pe") is not None and r["pe"] > 0)
    pb_vals = sorted(r["pb"] for r in peers if r.get("pb") is not None and r["pb"] > 0)
    def median(vals):
        if not vals:
            return None
        n = len(vals)
        return round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2, 2)
    rec = dict(rec)
    # 补充真实基本面(ROE/毛利率/资产负债率/营收同比), 覆盖行情快照中的空值
    try:
        fund = fetch_fundamentals(code)
        rec.update(fund)
    except Exception as e:
        print("[warn] 基本面补充失败(code=%s): %s" % (code, e))
    rec["ind_pe_avg"] = median(pe_vals)
    rec["ind_pb_avg"] = median(pb_vals)
    # 基本面四指标行业中位数(roe/gross/debt/rev_growth)需要逐股取数(最慢的一环),
    # 已从本接口移出: 改为前端异步请求 /api/industry_fund 补齐, 保证本接口 <1s 返回。
    # 首次渲染时这几项留空, 由 fetchIndFund 回填(ind_roe_avg/ind_gross_avg/...)。
    rec["ind_fund_pending"] = True
    return (rec, 200)


def handle_api_industry_fund(params):
    """按行业返回基本面四指标中位数(异步补齐用)。

    与原先内嵌在 /api/stock 中的逻辑完全一致(复用 _industry_fund_medians),
    仅是调用时机改为异步: 首屏不再等待这 12 只同行的逐股取数。
    """
    ind = (params.get("ind") or [""])[0].strip()
    if not ind:
        return ({"error": "缺少 ind 参数"}, 400)
    recs = get_records()
    peers = [r for r in recs if r.get("ind") == ind]
    if not peers:
        return ({"error": "未找到该行业(ind=%s)" % ind}, 404)
    im = _industry_fund_medians(ind, peers)
    return ({
        "ind": ind,
        "ind_roe_avg": im.get("roe"),
        "ind_gross_avg": im.get("gross"),
        "ind_debt_avg": im.get("debt_ratio"),
        "ind_rev_growth_avg": im.get("rev_growth"),
        "ind_fund_sample_n": im.get("sample_n"),
    }, 200)


# ===== 板块关联与板块对比（行业对比 tab） =====
# 数据源: push2 slist(spt=3) 取个股关联板块; clist m:90+t:2/t:3 板块分类清单做「行业/概念」归类。
# 板块成员指标: clist fs=b:{bk} 一次取齐 PE-TTM(f115)/PB(f23)/ROE(f37)/市值(f20);
# 毛利率(f186)在 clist 中大多为'-', 不足时对前 N 只成员逐股 fetch_fundamentals 兜底。
_BOARD_IND_CACHE = {}      # {date_iso: {bk_code: name}} 行业板块清单
_BOARD_CON_CACHE = {}      # {date_iso: {bk_code: name}} 概念板块清单
_BOARD_LIST_LOCK = threading.Lock()
_STOCK_BOARDS_CACHE = {}   # {code: (date_iso, {"industry": [...], "concept": [...]})}
_STOCK_BOARDS_LOCK = threading.Lock()
_BOARD_CMP_CACHE = {}      # {(bk, date_iso): {"metrics":..., "sample_n":...}}
_BOARD_CMP_LOCK = threading.Lock()
BOARD_SAMPLE_N = 12        # 板块对比样本: 按市值前 N 只(与行业指标口径一致, 中位数足够稳定)
BOARD_FUND_WORKERS = 12
BOARD_FUND_BUDGET_S = 12   # 毛利率逐股兜底总时长预算(秒), 超时用已完成部分计算中位数


def _board_class_lists():
    """板块分类清单(当日缓存): 行业(m:90+t:2) 与 概念(m:90+t:3)。

    正常规模: 行业约 300 个 / 概念约 500 个。限流时上游可能返回残缺清单,
    规模不达标则不缓存并重试(最多 3 次), 避免整天归类失败。
    """
    today = datetime.date.today().isoformat()
    with _BOARD_LIST_LOCK:
        if _BOARD_IND_CACHE.get("date") == today and _BOARD_CON_CACHE.get("date") == today:
            return _BOARD_IND_CACHE["map"], _BOARD_CON_CACHE["map"]

    def _fetch_map(fs):
        # 部分主机把 pz 钳制在 100, 必须按 total 标准分页拉全, 否则清单残缺导致归类大量失败
        out = {}
        pn, total = 1, None
        while pn <= 30:
            q = urllib.parse.urlencode({"fs": fs, "fields": "f12,f14", "pn": str(pn),
                                        "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2"})
            d = None
            for host in ("https://push2delay.eastmoney.com", "https://push2.eastmoney.com"):
                d = _curl_json(host + "/api/qt/clist/get?" + q, timeout=12)
                if not d:
                    continue
                data = d.get("data") or {}
                diff = data.get("diff") or []
                t = data.get("total")
                # 截断页(条数明显少于 total 应有的一页)→ 换主机重试
                if diff and (not t or len(diff) >= min(100, int(t))):
                    break
                d = None
            if not d:
                break  # 该页两个主机都失败, 用已拿到的部分(不缓存, 见外层)
            data = d.get("data") or {}
            for v in data.get("diff") or []:
                code = str(v.get("f12") or "")
                if code:
                    out[code] = str(v.get("f14") or "")
            if total is None:
                total = int(data.get("total") or 0)
            if (total and len(out) >= total) or not data.get("diff"):
                break
            pn += 1
        return out

    ind_map, con_map = {}, {}
    for attempt in range(3):
        ind_map = _fetch_map("m:90+t:2")
        con_map = _fetch_map("m:90+t:3")
        if len(ind_map) >= 50 and len(con_map) >= 200:
            break
        time.sleep(1.5)
    if len(ind_map) >= 50 and len(con_map) >= 200:
        with _BOARD_LIST_LOCK:
            _BOARD_IND_CACHE.clear(); _BOARD_IND_CACHE.update({"date": today, "map": ind_map})
            _BOARD_CON_CACHE.clear(); _BOARD_CON_CACHE.update({"date": today, "map": con_map})
    return ind_map, con_map


def handle_api_boards(params):
    """个股关联板块(归类为行业/概念两组)。响应: {"industry":[{code,name}], "concept":[...]}"""
    code = (params.get("code") or [""])[0].strip()
    if not code:
        return ({"error": "缺少 code 参数"}, 400)
    secid = _secid(code)
    if not secid:
        return ({"error": "无法识别该股票代码"}, 400)
    today = datetime.date.today().isoformat()
    with _STOCK_BOARDS_LOCK:
        hit = _STOCK_BOARDS_CACHE.get(code)
        if hit and hit[0] == today:
            return (hit[1], 200)
    q = urllib.parse.urlencode({"secid": secid, "spt": "3", "fields": "f12,f14",
                                "pn": "1", "pz": "200", "np": "1", "fltt": "2", "invt": "2"})
    out = {"code": code, "industry": [], "concept": []}
    # slist 在被限流的主机上会返回半截数据(正常个股关联板块有 20~40 个):
    #   - 优先 push2delay(无行情预取流量、不被限流);
    #   - 用响应自带 total 校验完整性, len(diff)<total 视为截断, 换主机/重试。
    for attempt in range(3):
        hosts = ["https://push2delay.eastmoney.com", "https://push2.eastmoney.com"]
        d = None
        for host in hosts:
            d = _curl_json(host + "/api/qt/slist/get?" + q, timeout=12)
            if d is not None:
                diff = ((d or {}).get("data") or {}).get("diff") or []
                total = ((d or {}).get("data") or {}).get("total") or 0
                if diff and len(diff) >= total:
                    break
                d = None  # 截断响应, 试下一个主机
        ind_map, con_map = _board_class_lists()
        industry, concept = [], []
        for v in (diff if d is not None else []):
            bk = str(v.get("f12") or "")
            name = str(v.get("f14") or "")
            if not bk or not name:
                continue
            if bk in ind_map:
                industry.append({"code": bk, "name": name})
            elif bk in con_map:
                concept.append({"code": bk, "name": name})
            # 都不属于(地域/指数等)则不展示
        out = {"code": code, "industry": industry, "concept": concept}
        if len(industry) + len(concept) >= 10 or d is None:
            break
        time.sleep(1.2)
    if len(out["industry"]) + len(out["concept"]) >= 10:
        # 样本过少视为限流半截响应, 不缓存、下次请求重试
        with _STOCK_BOARDS_LOCK:
            _STOCK_BOARDS_CACHE[code] = (today, out)
    return (out, 200)


def _median(vals):
    if not vals:
        return None
    xs = sorted(vals)
    n = len(xs)
    return round(xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2, 2)


def _board_gross_medians(bk, codes):
    """板块成员毛利率兜底: clist 的 f186 大多为'-', 对前 N 只逐股取数算中位数(当日缓存)。"""
    vals = {"gross": [], "sample_n": 0}

    def _safe(c):
        try:
            return fetch_fundamentals(c)
        except Exception:
            return {}

    ex = ThreadPoolExecutor(max_workers=BOARD_FUND_WORKERS)
    try:
        deadline = time.time() + BOARD_FUND_BUDGET_S
        futs = [ex.submit(_safe, c) for c in codes]
        for fut in as_completed(futs):
            g = (fut.result() or {}).get("gross")
            if g is not None:
                vals["gross"].append(g)
            if time.time() > deadline:
                break
    except Exception as e:
        print("[warn] 板块毛利率兜底取数异常(bk=%s): %s" % (bk, e))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    vals["sample_n"] = len(vals["gross"])
    vals["gross"] = _median(vals["gross"])
    return vals


def handle_api_board_compare(params):
    """板块基准对比: 本公司 vs 板块按市值前 N 只成员的中位数。

    响应: {"bk","bk_name","sample_n","self":{pe,pb,roe,gross},"median":{pe,pb,roe,gross}}
    """
    code = (params.get("code") or [""])[0].strip()
    bk = (params.get("bk") or [""])[0].strip()
    if not code or not bk.startswith("BK"):
        return ({"error": "缺少 code 或 bk 参数"}, 400)
    today = datetime.date.today().isoformat()
    with _BOARD_CMP_LOCK:
        hit = _BOARD_CMP_CACHE.get((bk, today))
    # 本公司指标(快照 pe/pb + 实时 roe/gross), 不随板块缓存
    recs = get_records()
    rec = next((r for r in recs if (r.get("code") or "") == code), None)
    try:
        fund = fetch_fundamentals(code)
    except Exception:
        fund = {}
    self_pe = rec.get("pe") if rec else None
    self_pb = rec.get("pb") if rec else None
    self_metrics = {"pe": self_pe, "pb": self_pb,
                    "roe": fund.get("roe"), "gross": fund.get("gross")}
    if hit:
        return ({"bk": bk, "bk_name": hit.get("bk_name"), "sample_n": hit.get("sample_n"),
                 "self": self_metrics, "median": hit.get("metrics")}, 200)

    # 板块成员(按市值前 N)
    q = urllib.parse.urlencode({"fs": "b:" + bk,
                                "fields": "f12,f14,f9,f115,f23,f20,f37,f186",
                                "pn": "1", "pz": str(BOARD_SAMPLE_N), "po": "1", "np": "1",
                                "fid": "f20", "fltt": "2", "invt": "2"})
    d = _curl_json_hosts(["https://push2.eastmoney.com", "https://push2delay.eastmoney.com"],
                         "/api/qt/clist/get?" + q, timeout=12)
    diff = ((d or {}).get("data") or {}).get("diff") or []
    if not diff:
        return ({"error": "未获取到板块成员"}, 502)

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    pe_vals, pb_vals, roe_vals, gross_vals = [], [], [], []
    for v in diff:
        pe = _num(v.get("f115")) or _num(v.get("f9"))
        pb = _num(v.get("f23"))
        roe = _num(v.get("f37"))
        gross = _num(v.get("f186"))
        if pe is not None and pe > 0:
            pe_vals.append(pe)
        if pb is not None and pb > 0:
            pb_vals.append(pb)
        if roe is not None:
            roe_vals.append(roe)
        if gross is not None:
            gross_vals.append(gross)
    metrics = {"pe": _median(pe_vals), "pb": _median(pb_vals), "roe": _median(roe_vals)}
    # 毛利率来源太稀就逐股兜底(取数预算内能拿多少算多少)。
    # 例外: clist 的 f186 全部为'-'时(银行/保险等无毛利率口径的板块), 兜底也只会
    # 拿到无意义值, 直接置 None 显示'—', 省掉整轮逐股取数(冷缓存最坏 10-20s)。
    if not gross_vals:
        metrics["gross"] = None
    elif len(gross_vals) < max(3, len(diff) // 3):
        g = _board_gross_medians(bk, [str(v.get("f12")) for v in diff if v.get("f12")])
        metrics["gross"] = g["gross"] if g["gross"] is not None else _median(gross_vals)
    else:
        metrics["gross"] = _median(gross_vals)
    bk_name = ""
    ind_map, con_map = _board_class_lists()
    bk_name = ind_map.get(bk) or con_map.get(bk) or ""
    sample_n = len(diff)
    if metrics["gross"] is not None or metrics["roe"] is not None:
        with _BOARD_CMP_LOCK:
            _BOARD_CMP_CACHE[(bk, today)] = {"bk_name": bk_name, "metrics": metrics,
                                             "sample_n": sample_n}
    return ({"bk": bk, "bk_name": bk_name, "sample_n": sample_n,
             "self": self_metrics, "median": metrics}, 200)


# ===== 板块列表（行业/概念板块行情, 供 sector.html 列表页使用） =====
# 数据源: push2 clist, fs=m:90+t:2(行业板块) / m:90+t:3(概念板块)。
# 字段: f12板块代码 f14名称 f2最新点位 f3涨跌幅 f104上涨家数 f105下跌家数
#       f128领涨股 f136领涨股涨跌幅 f20总市值
_BOARD_QUOTE_CACHE = {}    # {kind: (date_iso, [record,...])}
_BOARD_QUOTE_LOCK = threading.Lock()
_BOARD_QUOTE_FS = {"industry": "m:90+t:2", "concept": "m:90+t:3"}


def _fmt_mcap(v):
    """总市值展示: >=1万亿 显示万亿, 其余显示亿。"""
    if v is None:
        return "--"
    if v >= 1e12:
        return "%.2f万亿" % (v / 1e12)
    return "%.2f亿" % (v / 1e8)


def _board_rec_from_row(v):
    """单条板块行情记录映射(全量路径与按需窗口路径共用)。"""
    code = str(v.get("f12") or "")
    name = str(v.get("f14") or "")
    if not code or not name:
        return None
    chg = _to_num(v.get("f3"))
    price = _to_num(v.get("f2"))
    leader_chg = _to_num(v.get("f136"))
    mcap = _to_num(v.get("f20"))
    return {
        "name": name,
        "code": code,
        "price": "--" if price is None else "%.2f" % price,
        "price_num": price,
        "chg": "--" if chg is None else "%+.2f%%" % chg,
        "chg_num": chg,
        "dir": ("flat" if chg == 0 else ("up" if chg > 0 else "down")) if chg is not None else "flat",
        "up_n": _to_num(v.get("f104")),
        "down_n": _to_num(v.get("f105")),
        "leader": str(v.get("f128") or "") or "--",
        "leader_chg": "--" if leader_chg is None else "%+.2f%%" % leader_chg,
        "mcap": mcap,
        "mcap_disp": _fmt_mcap(mcap),
    }


def get_board_records(kind):
    """返回指定类型板块的全量行情记录, 按「类型+自然日」缓存(收盘后当日不变)。"""
    today = datetime.date.today().isoformat()
    with _BOARD_QUOTE_LOCK:
        hit = _BOARD_QUOTE_CACHE.get(kind)
        if hit and hit[0] == today:
            return hit[1]
    fs = _BOARD_QUOTE_FS.get(kind)
    if not fs:
        return []
    base = {"fs": fs, "fields": "f12,f14,f2,f3,f104,f105,f128,f136,f20",
            "po": "1", "np": "1", "fltt": "2", "invt": "2", "fid": "f3"}
    out, complete = [], False
    # 最多 3 轮拉取: 中途某页两个主机都失败会得到残缺清单, 必须拉全(total)才缓存,
    # 否则「半导体」等板块会整天缺失(与 _board_class_lists 同范式)。
    for attempt in range(3):
        out, pn, total = [], 1, None
        # 标准分页拉全: 部分主机把 pz 钳制在 100
        while pn <= 30:
            q = dict(base)
            q["pn"] = str(pn)
            q["pz"] = "100"
            d = None
            # 单页重试: 两轮 × 双主机; 末页(累计达 total)同样放行, 避免末页被误判截断
            for _round in range(2):
                for host in ("https://push2delay.eastmoney.com", "https://push2.eastmoney.com"):
                    d = _curl_json(host + "/api/qt/clist/get?" + urllib.parse.urlencode(q), timeout=12)
                    if not d:
                        continue
                    data = d.get("data") or {}
                    diff = data.get("diff") or []
                    t = data.get("total")
                    if diff and (not t or len(diff) >= 100 or len(out) + len(diff) >= int(t)):
                        break
                    d = None
                if d:
                    break
                time.sleep(0.8)
            if not d:
                break  # 该页多轮重试均失败, 本轮终止, 视 complete 判断是否重试
            data = d.get("data") or {}
            for v in data.get("diff") or []:
                r = _board_rec_from_row(v)
                if r:
                    out.append(r)
            if total is None:
                total = int(data.get("total") or 0)
            if (total and len(out) >= total) or not data.get("diff"):
                break
            pn += 1
        complete = bool(out) and ((not total) or len(out) >= total)
        if complete:
            break
        time.sleep(1.5)
    # 拉全且规模达标才缓存; 残缺响应不缓存、下次请求重试
    if complete and len(out) >= 50:
        with _BOARD_QUOTE_LOCK:
            _BOARD_QUOTE_CACHE[kind] = (today, out)
    return out


# ===== 板块列表按需窗口取数 =====
# 页面每页只展示 6 个板块, 但旧实现为了排序必须串行拉全量(行业 496 / 概念 504, 每页 100 →
# 5~6 次串行请求, 冷拉取实测 9~25s)。东财 clist 本身支持服务端排序(fid+po), 因此非搜索场景
# 改为: 只拉「当前页所在的 100 条块」, 排序正确性由服务端保证, 深翻页按需取对应块即可。
_BOARD_QUOTE_BLOCK = {}      # {(kind, fid, po, pn): (date_iso, [record,...])} 排序窗口块缓存
_BOARD_TOTAL_CACHE = {}      # {kind: (date_iso, total)} 板块总数(供「共 N 个板块」与分页器)
_BOARD_BLOCK_LOCK = threading.Lock()
_BOARD_INFLIGHT = {}         # {block_key: Event} 单飞: 同块并发只打一次外网
_BOARD_SORT_FID = {"chg": "f3", "price": "f2", "mcap": "f20"}


def _board_total_cached(kind):
    today = datetime.date.today().isoformat()
    hit = _BOARD_TOTAL_CACHE.get(kind)
    return hit[1] if (hit and hit[0] == today) else None


def _fetch_board_block(fs, fid, po, pn, tries=2):
    """拉单个 100 条块(双主机 + 重试), 返回 (items, total) 或 (None, None)。"""
    q = urllib.parse.urlencode({"fs": fs, "fields": "f12,f14,f2,f3,f104,f105,f128,f136,f20",
                                "pn": str(pn), "pz": "100", "po": po, "np": "1",
                                "fltt": "2", "invt": "2", "fid": fid})
    for _ in range(tries):
        for host in ("https://push2delay.eastmoney.com", "https://push2.eastmoney.com"):
            d = _curl_json(host + "/api/qt/clist/get?" + q, timeout=10)
            if not d:
                continue
            data = d.get("data") or {}
            diff = data.get("diff") or []
            if not diff:
                continue
            items = [r for r in (_board_rec_from_row(v) for v in diff) if r]
            if not items:
                continue
            try:
                total = int(data.get("total") or 0) or None
            except (TypeError, ValueError):
                total = None
            return (items, total)
    return (None, None)


def _board_block(kind, fid, po, pn, today):
    """取一个排序窗口块(当日缓存 + 单飞去重), 返回 (items, total) 或 (None, None)。"""
    key = (kind, fid, po, pn)
    with _BOARD_BLOCK_LOCK:
        hit = _BOARD_QUOTE_BLOCK.get(key)
        if hit and hit[0] == today:
            return (hit[1], _board_total_cached(kind))
        ev = _BOARD_INFLIGHT.get(key)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _BOARD_INFLIGHT[key] = ev
    if not owner:
        ev.wait(timeout=30)          # 同块并发: 等首个线程的结果, 不重复打外网
        with _BOARD_BLOCK_LOCK:
            hit = _BOARD_QUOTE_BLOCK.get(key)
        return (hit[1], _board_total_cached(kind)) if hit else (None, None)
    try:
        items, total = _fetch_board_block(_BOARD_QUOTE_FS.get(kind), fid, po, pn)
        if items is not None:
            with _BOARD_BLOCK_LOCK:
                _BOARD_QUOTE_BLOCK[key] = (today, items)
                if total:
                    _BOARD_TOTAL_CACHE[kind] = (today, total)
        return (items, total)
    finally:
        with _BOARD_BLOCK_LOCK:
            _BOARD_INFLIGHT.pop(key, None)
        ev.set()


def get_board_window(kind, sort, order, page, pageSize):
    """按需窗口取数: 只拉当前页所在的 100 条块(东财服务端排序)。

    返回 (page_items, total); 数据不足(限流/末页)时返回 (None, total), 由调用方回退全量路径。
    """
    if not _BOARD_QUOTE_FS.get(kind):
        return (None, None)
    fid = _BOARD_SORT_FID.get(sort, "f3")
    po = "1" if order == "desc" else "0"
    start = max(0, (page - 1) * pageSize)
    today = datetime.date.today().isoformat()
    first_pn = start // 100 + 1
    last_pn = (start + max(pageSize, 1) - 1) // 100 + 1
    recs, total = [], _board_total_cached(kind)
    for pn in range(first_pn, last_pn + 1):
        items, t = _board_block(kind, fid, po, pn, today)
        if items is None:
            return (None, total)
        if t:
            total = t
        recs.extend(items)
    offset = start % 100
    if len(recs) < offset + pageSize:
        return (None, total)         # 块内不足一页(末页或限流), 交回退路径
    return (recs[offset:offset + pageSize], total)


def handle_api_boards_list(params):
    """GET /api/boards_list?type=industry|concept&sort=chg|price|mcap&order=asc|desc
              &page=1&pageSize=6&q=关键词

    返回结构与 /api/stocks 对齐, 便于前端复用同一套列表/分页渲染逻辑。
    """
    kind = (params.get("type") or ["industry"])[0].strip().lower()
    if kind not in ("industry", "concept"):
        kind = "industry"
    sort = (params.get("sort") or ["chg"])[0]
    order = (params.get("order") or ["desc"])[0]
    # 兼容前端 "chg_desc" 式取值
    if "_" in sort:
        sort, order = sort.split("_", 1)
    if sort not in ("chg", "price", "mcap"):
        sort = "chg"
    if order not in ("asc", "desc"):
        order = "desc"
    page = _to_int(params.get("page", ["1"])[0], 1)
    pageSize = _to_int(params.get("pageSize", ["6"])[0], 6)
    q = (params.get("q") or [""])[0].strip().lower()
    if page < 1:
        page = 1
    if pageSize < 1 or pageSize > 100:
        pageSize = 6

    def sort_key(r):
        v = r.get(sort + "_num") if sort in ("chg", "price") else r.get("mcap")
        # 缺失值恒排末尾(升降序均如此)
        return v if v is not None else (float("-inf") if order == "desc" else float("inf"))

    if q:
        # 关键词搜索必须全量匹配(命中全量当日缓存时无外网开销)
        recs = [r for r in get_board_records(kind) if q in r["name"].lower()
                or q in r["code"].lower()
                or q in (r["leader"] or "").lower()]
        recs = sorted(recs, key=sort_key, reverse=(order == "desc"))
        total = len(recs)
        start = (page - 1) * pageSize
        page_items = recs[start:start + pageSize]
    else:
        # 非搜索: 按需窗口(东财服务端排序, 只拉当前页所在块); 不足时回退全量本地排序
        page_items, total = get_board_window(kind, sort, order, page, pageSize)
        if page_items is None:
            recs = sorted(get_board_records(kind), key=sort_key, reverse=(order == "desc"))
            total = len(recs) or (total or 0)
            start = (page - 1) * pageSize
            page_items = recs[start:start + pageSize]
        elif not total:
            total = _board_total_cached(kind) or len(page_items)

    return {
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "type": kind,
        "total": total,
        "page": page,
        "pageSize": pageSize,
        "sort": sort,
        "order": order,
        "keyword": q,
        "boards": page_items,
    }


# ===== 板块成员个股(sector_detail.html: 点击板块 -> 板块下全部个股) =====
# 数据源: push2 clist fs=b:{BK代码}, 字段与全市场个股快照同源:
#   f12代码 f14名称 f2最新价 f3涨跌幅 f100申万行业 f20总市值 f21流通市值
# 记录格式复用 fetch_stocks 的 to_num/fmt_mcap/norm_industry, 与 /api/stocks 行结构对齐。
_BOARD_MEMBERS_CACHE = {}   # {bk: (date_iso, [record,...])}
_BOARD_MEMBERS_LOCK = threading.Lock()
_BOARD_MEMBERS_FIELDS = "f12,f14,f2,f3,f100,f20,f21"


def _board_name_of(bk):
    """从当日板块行情缓存中反查板块名称与类型; 查不到返回 ("", None)。"""
    for kind in ("industry", "concept"):
        for r in get_board_records(kind):
            if r["code"] == bk:
                return r["name"], kind
    return "", None


def get_board_members(bk):
    """返回板块成员个股记录列表, 按「板块+自然日」缓存(成分当日基本不变)。"""
    today = datetime.date.today().isoformat()
    with _BOARD_MEMBERS_LOCK:
        hit = _BOARD_MEMBERS_CACHE.get(bk)
        if hit and hit[0] == today:
            return hit[1]
    base = {"fs": "b:" + bk, "fields": _BOARD_MEMBERS_FIELDS,
            "po": "1", "np": "1", "fltt": "2", "invt": "2", "fid": "f20"}
    rows, complete = [], False
    # 与 get_board_records 同范式: 分页拉全才缓存; 单页两轮×双主机重试
    for attempt in range(3):
        rows, pn, total = [], 1, None
        while pn <= 100:
            q = dict(base)
            q["pn"] = str(pn)
            q["pz"] = "100"
            d = None
            for _round in range(2):
                for host in ("https://push2delay.eastmoney.com", "https://push2.eastmoney.com"):
                    d = _curl_json(host + "/api/qt/clist/get?" + urllib.parse.urlencode(q), timeout=12)
                    if not d:
                        continue
                    data = d.get("data") or {}
                    diff = data.get("diff") or []
                    t = data.get("total")
                    if diff and (not t or len(diff) >= 100 or len(rows) + len(diff) >= int(t)):
                        break
                    d = None
                if d:
                    break
                time.sleep(0.8)
            if not d:
                break
            data = d.get("data") or {}
            diff = data.get("diff") or []
            rows.extend(v for v in diff if isinstance(v, dict))
            if total is None:
                total = int(data.get("total") or 0)
            if (total and len(rows) >= total) or not diff:
                break
            pn += 1
        complete = bool(rows) or (total == 0)   # total=0 是合法空板块, 也算完整
        if complete:
            break
        time.sleep(1.5)
    _ts = fetch_stocks.to_num
    records = []
    for v in rows:
        code = str(v.get("f12") or "")
        name = (v.get("f14") or "").strip()
        if not code or not name:
            continue
        pv = _ts(v.get("f2"))
        cv = _ts(v.get("f3"))
        records.append({
            "name": name,
            "code": code,
            "ind": fetch_stocks.norm_industry(v.get("f100")),
            "price": ("¥%.2f" % pv) if pv is not None else "--",
            "price_num": pv,
            "chg": "--" if cv is None else (("+" if cv > 0 else "") + "%.2f%%" % cv),
            "chg_num": cv,
            "dir": "up" if (cv or 0) > 0 else ("down" if (cv or 0) < 0 else "flat"),
            "mcap": _ts(v.get("f20")),
            "mcap_disp": fetch_stocks.fmt_mcap(_ts(v.get("f20"))),
            "fcap": _ts(v.get("f21")),
            "fcap_disp": fetch_stocks.fmt_mcap(_ts(v.get("f21"))),
        })
    # 拉全才缓存: total 已知时必须取满(total=0 的空板块同样合法), 残缺清单不缓存、下次请求重试
    if complete and (total is None or len(records) >= total):
        with _BOARD_MEMBERS_LOCK:
            _BOARD_MEMBERS_CACHE[bk] = (today, records)
    return records


_BOARD_MEMBER_SORTS = {"mcap", "fcap", "chg", "price"}


def handle_api_board_members(params):
    """GET /api/board_members?bk=BK1036&sort=mcap|fcap|chg|price&order=asc|desc
              &page=1&pageSize=6&q=关键词

    返回结构与 /api/stocks 对齐(stocks 字段), 便于前端复用个股列表渲染逻辑。
    """
    bk = (params.get("bk") or params.get("code") or [""])[0].strip().upper()
    if not re.match(r"^BK\d+$", bk):
        return ({"error": "缺少或无效的板块代码(bk=BKxxxx)"}, 400)
    sort = (params.get("sort") or ["mcap"])[0]
    order = (params.get("order") or ["desc"])[0]
    # 兼容前端 "mcap_desc" 式取值
    if "_" in sort:
        sort, order = sort.split("_", 1)
    if sort not in _BOARD_MEMBER_SORTS:
        sort = "mcap"
    if order not in ("asc", "desc"):
        order = "desc"
    page = _to_int(params.get("page", ["1"])[0], 1)
    pageSize = _to_int(params.get("pageSize", ["6"])[0], 6)
    q = (params.get("q") or [""])[0].strip().lower()
    if page < 1:
        page = 1
    if pageSize < 1 or pageSize > 100:
        pageSize = 6

    recs = get_board_members(bk)
    if q:
        recs = [r for r in recs if q in r["name"].lower()
                or q in r["code"].lower()
                or q in (r["ind"] or "").lower()]

    def sort_key(r):
        v = r.get(sort + "_num") if sort in ("chg", "price") else r.get(sort)
        # 缺失值恒排末尾(升降序均如此)
        return v if v is not None else (float("-inf") if order == "desc" else float("inf"))

    recs = sorted(recs, key=sort_key, reverse=(order == "desc"))
    total = len(recs)
    start = (page - 1) * pageSize
    page_items = recs[start:start + pageSize]

    bk_name, kind = _board_name_of(bk)
    return ({
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "bk": bk,
        "bk_name": bk_name,
        "kind": kind,
        "total": total,
        "page": page,
        "pageSize": pageSize,
        "sort": sort,
        "order": order,
        "keyword": q,
        "stocks": page_items,
    }, 200)


def handle_api_kline(params):
    code = (params.get("code") or [""])[0].strip()
    if not code:
        return ({"error": "缺少 code 参数"}, 400)
    klt = _to_int(params.get("klt", ["101"])[0], 101)
    n = _to_int(params.get("n", ["120"])[0], 120)
    try:
        return (fetch_kline(code, klt, n), 200)
    except Exception as e:
        return ({"error": "K线获取失败: %s" % e}, 502)


# ===== 静态服务 =====
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, fpath):
        ext = os.path.splitext(fpath)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        try:
            with open(fpath, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # HTML 一律禁缓存: 改版后浏览器必须拿到最新页, 否则会残留旧 JS(实测踩坑:
        # login.html 换真实登录接口后, 缓存页仍弹「演示环境」toast 且不跳转)
        if ext in (".html", ".htm"):
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_cookie(self, name: str):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _session_ok(self) -> bool:
        """ta_session 会话校验: 经 FastAPI /api/auth/me（SQLite 会话表单一事实源）。

        校验为同步内网调用（本机 8002, timeout 3s）；服务未启动时 fail-closed
        （重定向登录页），与「后端未启动时页面功能本就不可用」的整体行为一致。
        """
        token = self._get_cookie(SESSION_COOKIE_NAME)
        if not token:
            return False
        try:
            req = urllib.request.Request(
                REPORT_API_BASE + "/api/auth/me",
                headers={"Cookie": "%s=%s" % (SESSION_COOKIE_NAME, token)},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # ===== 登录态路由拦截（AUTH-A 2026-09-11） =====
        if not _is_public_path(parsed.path):
            if not self._session_ok():
                if parsed.path.startswith("/api/"):
                    self._send_json({"detail": "未登录或会话已过期"}, status=401)
                else:
                    _nxt = urllib.parse.quote(
                        parsed.path + (("?" + parsed.query) if parsed.query else ""), safe="")
                    self._redirect("/login.html?next=" + _nxt)
                return
        elif parsed.path == "/login.html" and self._session_ok():
            # 已登录访问登录页 → 自动进入首页（或 next 指定的同站路径）
            _qs = urllib.parse.parse_qs(parsed.query)
            _nxt = (_qs.get("next") or [""])[0]
            _safe = _nxt.startswith("/") and not _nxt.startswith("//")
            self._redirect(_nxt if _safe else "/index.html")
            return

        if parsed.path == "/api/stocks":
            try:
                self._send_json(handle_api_stocks(urllib.parse.parse_qs(parsed.query)))
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if parsed.path == "/api/stock":
            try:
                obj, status = handle_api_stock(urllib.parse.parse_qs(parsed.query))
                self._send_json(obj, status)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if parsed.path == "/api/industry_fund":
            try:
                obj, status = handle_api_industry_fund(urllib.parse.parse_qs(parsed.query))
                self._send_json(obj, status)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if parsed.path == "/api/board_members":
            try:
                obj, status = handle_api_board_members(urllib.parse.parse_qs(parsed.query))
                self._send_json(obj, status)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if parsed.path == "/api/boards_list":
            try:
                self._send_json(handle_api_boards_list(urllib.parse.parse_qs(parsed.query)))
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if parsed.path == "/api/boards":
            try:
                obj, status = handle_api_boards(urllib.parse.parse_qs(parsed.query))
                self._send_json(obj, status)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if parsed.path == "/api/board_compare":
            try:
                obj, status = handle_api_board_compare(urllib.parse.parse_qs(parsed.query))
                self._send_json(obj, status)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if parsed.path.startswith("/api/kline"):
            try:
                obj, status = handle_api_kline(urllib.parse.parse_qs(parsed.query))
                self._send_json(obj, status)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if parsed.path == "/api/news":
            try:
                obj, status = handle_api_news(urllib.parse.parse_qs(parsed.query))
                self._send_json(obj, status)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if parsed.path.startswith("/api/report"):
            if not self._session_ok():
                self._send_json({"detail": "未登录或会话已过期"}, status=401)
                return
            self._proxy_report("GET")
            return

        if parsed.path.startswith("/api/auth"):
            self._proxy_report("GET")
            return

        # 静态资源
        rel = parsed.path.lstrip("/")
        if rel == "" or rel == "index.html":
            rel = "index.html"
        fpath = os.path.normpath(os.path.join(BASE_DIR, rel))
        # 路径穿越防护: 必须仍在 BASE_DIR 内
        if not fpath.startswith(BASE_DIR):
            self.send_error(403)
            return
        if os.path.isfile(fpath):
            self._send_file(fpath)
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        # 精简日志, 避免刷屏
        if "api/stocks" in (self.path or ""):
            sys.stderr.write("[api] %s - %s\n" % (self.address_string(), fmt % args))

    # ===== /api/report、/api/auth 反向代理到 FastAPI 报告/认证服务 =====
    def _proxy_report(self, method):
        """将 /api/report* 与 /api/auth* 同源代理到 REPORT_API_BASE(默认 127.0.0.1:8002)。

        - 后端未启动时返回 502 + 友好提示(前端据此显示「服务未启动」)
        - 透传 JSON 请求体(GET 无体)与响应体(含 /html 的 text/html 报告文件)
        - 透传响应头 Set-Cookie: 登录接口经由此代理下发 HttpOnly 会话 Cookie
        """
        target = REPORT_API_BASE + self.path  # self.path 形如 /api/report/abc/html
        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else None
        req = urllib.request.Request(target, data=body, method=method)
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in ("host", "content-length", "connection", "accept-encoding"):
                continue
            req.add_header(k, v)
        try:
            # 本机回环调用必须绕过 HTTP_PROXY：环境中若设了 http_proxy（沙箱/公司代理），
            # urlopen 会把 127.0.0.1 的请求一并发往代理，代理回 502「upstream connect failed」，
            # 表现为「报告服务不可达」的假故障（P3-4 实测：HTTP_PROXY=127.0.0.1:8431 正是如此）。
            _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            resp = _opener.open(req, timeout=120)
        except urllib.error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/octet-stream"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        except Exception as e:
            self._send_json({"error": "报告服务不可达(%s)：%s" % (REPORT_API_BASE, e)}, status=502)
            return
        payload = resp.read()
        ctype = resp.headers.get("Content-Type", "application/json")
        self.send_response(resp.status)
        self.send_header("Content-Type", ctype)
        # 登录/注销接口的会话 Cookie 必须原样透传, 浏览器才能保存 ta_session
        set_cookie = resp.headers.get("Set-Cookie")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/report"):
            if not self._session_ok():
                self._send_json({"detail": "未登录或会话已过期"}, status=401)
                return
            self._proxy_report("POST")
            return
        if parsed.path.startswith("/api/auth"):
            self._proxy_report("POST")
            return
        self.send_error(405)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    t = threading.Thread(target=_prefetch, daemon=True)
    t.start()
    # 预热 akshare(冷导入 2-5s), 避免首次落到个股详情请求上
    _prewarm_akshare()

    # 预热板块分类清单(行业约300+概念约500, 分页拉取, 当日缓存):
    # 冷构建实测 5-15s, 放后台跑掉, 用户点「行业对比」时清单已就绪。
    def _prewarm_boards():
        try:
            time.sleep(3)   # 让出主预取与 akshare 预热, 错峰打外网
            _board_class_lists()
            print("[info] 板块分类清单预热完成")
        except Exception as e:
            print("[warn] 板块分类清单预热失败(首次请求时会重试): %s" % e)
    threading.Thread(target=_prewarm_boards, daemon=True).start()

    # 预热板块列表页行情(sector.html): 行业+概念两种的默认排序窗口块, 冷拉取实测 9-25s,
    # 放后台跑掉, 用户进板块页时直接命中当日缓存。sleep 更长以避开上面的清单预热。
    def _prewarm_board_quotes():
        try:
            time.sleep(8)
            # 1) 默认排序窗口: 覆盖列表页首屏与常规翻页
            for kind in ("industry", "concept"):
                get_board_window(kind, "chg", "desc", 1, 6)
            print("[info] 板块行情列表预热完成(行业+概念)")
            # 2) 全量清单: 供关键词搜索路径(必须全量匹配), 否则首次搜索要等 10-20s
            time.sleep(2)
            for kind in ("industry", "concept"):
                get_board_records(kind)
            print("[info] 板块全量清单预热完成(行业+概念)")
        except Exception as e:
            print("[warn] 板块行情列表预热失败(初次请求时会重试): %s" % e)
    threading.Thread(target=_prewarm_board_quotes, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("TradeAI 本地服务已启动: http://127.0.0.1:%d/" % port)
    print("  股票页: http://127.0.0.1:%d/stock.html" % port)
    print("  接口:   http://127.0.0.1:%d/api/stocks?sort=mcap&order=desc&page=1&pageSize=6" % port)
    print("  报告API(需另起): uvicorn fundamental_agent.api.server:app --host 127.0.0.1 --port 8001  (或设环境变量 REPORT_API_BASE)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
