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
import random
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import re
from collections import Counter

# ===== 东方财富行情主机池（集中定义，勿在各处再硬编码）=====
# 背景（2026-09-23 实测）：东财对 push2* 集群的 **/api/qt/clist/get** 路径按 IP 定向限流，
# push2delay / push2 / 82.push2 / 1.push2 / push2his 全部连接被重置（curl exit 56），
# 但同一时刻 **push2test.eastmoney.com 的 clist 完全可用**（行业 496 / 概念 504 / 全市场
# 5919 条，字段与 BK 代码与 push2delay 完全一致），且 push2delay 的 /api/qt/slist/get 仍可用
# —— 说明封锁是**按路径**而非按整站。此前把可用主机写死在 10 处，一处被封即全站失灵。
# 现集中在此，新增主机只改这一行。
HOSTS = [
    "https://push2delay.eastmoney.com",
    "https://push2test.eastmoney.com",
    "https://push2.eastmoney.com",
]

# 支持「大页」的主机：实测同一请求（pz=1000）push2test 真的返回 1000 条，
# push2delay / push2 会被静默钳制到 100 条 —— 同一次全量拉取因此从 6 页变成 56 页。
# 页数越多，被判定为爬虫的概率越高，而「翻页中途被限流」正是残缺数据集的来源。
# 故健康度相同时让大页主机优先（见 ordered_hosts 的 prefer_big_page）。
# ⚠️ 该优先**只适用于 clist 分页取数**；K 线等「取历史序列」的路径必须传
#    prefer_big_page=False —— push2test 的历史 K 线是随机占位样本（2026-09-25 定位）。
BIG_PAGE_HOSTS = frozenset({"https://push2test.eastmoney.com"})

# ===== 主机级健康度 =====
# 被封锁的主机失败很快（实测 ~0.6s/次，非超时），但每次都白撞一遍仍是不必要的延迟；
# 更关键的是**不要因为第一台死了就认定整条链路死**。失败过的主机进入冷却期，
# 期间排到候选末尾（不删除），冷却结束自动回归，无需重启。
HOST_COOLDOWN = 90             # 秒；见下方 _env_float 定义后按环境变量覆盖
_host_lock = threading.Lock()
_host_down_until = {}          # {host: 冷却截止时间戳}


def ordered_hosts(hosts=None, prefer_big_page=True):
    """按「健康度 → 页容量」返回主机顺序。

    1. 冷却期内（刚失败过）的排到最后，但**不删除**——封锁随时段变化，冷却≠判死；
    2. ``prefer_big_page=True`` 时，健康主机中支持大页（pz=1000）的优先。

    第 2 条是 2026-09-24 补的：实测同一次全量拉取，走 push2test 只需 6 页，
    走 push2delay（被钳制到 100 条/页）要 56 页。页数越少越不像爬虫，
    这是从源头降低「翻页中途被限流」概率的动作。

    **但它只对「翻页拉列表」成立**（2026-09-25 修正，见 docs/K线数据异常定位报告与修复方案.md）：
    页容量只与 clist 分页有关，对 K 线这类单次请求毫无价值，却会把 push2test 排到第一位
    ——而该主机**只对最近约 4 个交易日返回真实数据，更早的历史是随机占位样本**，
    直接导致全市场日/周/月 K 线被污染。故 K 线等「取历史序列」的调用点必须传
    ``prefer_big_page=False``，退回调用点给定的原序（历史专用主机在前）。
    """
    hosts = list(hosts or HOSTS)
    now = time.time()
    with _host_lock:
        def rank(h):
            cooling = 1 if _host_down_until.get(h, 0.0) > now else 0
            big = (0 if h in BIG_PAGE_HOSTS else 1) if prefer_big_page else 0
            return (cooling, big)

        return sorted(hosts, key=rank)   # sorted 稳定：同档内保持原序


def mark_host_down(host):
    """记录一次主机级失败（进入冷却，期间不再优先尝试）。

    只在「首次进入冷却」时打日志：一次失败的重试循环会对同一台主机调用十几次，
    逐次打印会把日志刷满，反而盖掉真正的错误信息。
    """
    now = time.time()
    with _host_lock:
        first = _host_down_until.get(host, 0.0) <= now
        _host_down_until[host] = now + HOST_COOLDOWN
    if first:
        print("[warn] 行情主机进入冷却 %ds: %s" % (HOST_COOLDOWN, host))


def mark_host_up(host):
    """成功即清除冷却，主机恢复正常优先级。"""
    with _host_lock:
        _host_down_until.pop(host, None)


def all_hosts_cooling(hosts=None):
    """是否**全部**候选主机都在冷却期内。

    用途：整条链路刚被上游拒掉时，继续逐台重试只是白打请求（既慢又更像爬虫）。
    调用方据此提前收手，直接走降级数据；冷却结束后自动恢复尝试。
    """
    hosts = list(hosts or HOSTS)
    if not hosts:
        return False
    now = time.time()
    with _host_lock:
        return all(_host_down_until.get(h, 0.0) > now for h in hosts)
# f12=代码 f14=名称 f2=最新价(元) f3=涨跌幅(%) f4=涨跌额 f15=最高 f16=最低 f17=今开
# f5=成交量(手) f6=成交额(元) f8=换手率(%) f9=市盈率TTM f23=市净率
# f20=总市值(元) f21=流通市值(元) f100=所属行业
# 注: ROE / 毛利率 / 资产负债率 / 营收同比 行情快照接口(push2delay clist)不提供,
# 统一不在此请求; 真实值由 server.handle_api_stock 按个股调用 fetch_fundamentals() 获取。
FIELDS = "f12,f14,f2,f3,f4,f15,f16,f17,f5,f6,f8,f9,f23,f20,f21,f100"
# 沪深京 A 股 (沪市 m:0+t:6 / 创业板 m:0+t:80 / 深市 m:1+t:2 / 科创板 m:1+t:23 / 京市 m:0+t:90)
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:90"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ===== 取数节奏（集中在此，便于统一治理被上游限流的风险）=====
# 背景：全市场约 5554 只、接口每页实际只给 ~100 条，一次全量要翻 ~56 页。
# 此前的 0.12s 间隔 + 每页 3 次重试极易被 push2 集群判定为爬虫并限流/封锁，
# 而东财只封 push2* 节点（其余子域正常），说明是**针对性限流**而非整站故障。
# 因此这里整体放慢节奏，并允许用环境变量覆盖，避免再"调一次代码"。
def _env_float(name, default):
    try:
        return float(os.environ.get(name, "").strip())
    except ValueError:
        return default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip())
    except ValueError:
        return default


PAGE_RETRIES = _env_int("MARKET_PAGE_RETRIES", 2)          # 单页最多尝试次数
RETRY_BACKOFF = _env_float("MARKET_RETRY_BACKOFF", 2.0)    # 重试间隔基数(秒)
PAGE_INTERVAL = _env_float("MARKET_PAGE_INTERVAL", 0.35)   # 翻页间隔(秒)
PAGE_JITTER = _env_float("MARKET_PAGE_JITTER", 0.15)       # 翻页间隔抖动上限(秒)
PAGE_SIZE = 1000          # 请求页大小(接口实际每页约返回 100 条)

# 主机级冷却时长（见文件顶部的 ordered_hosts / mark_host_down）
HOST_COOLDOWN = _env_float("MARKET_HOST_COOLDOWN", 90)


def _page_sleep():
    """翻页间隔 + 随机抖动：固定间隔本身就是典型的爬虫特征。"""
    time.sleep(PAGE_INTERVAL + random.random() * PAGE_JITTER)


class MarketSourceUnavailable(RuntimeError):
    """全市场行情源整体不可用。

    与「查询结果为空」严格区分：A 股市场永远有数千只标的，因此「一只都没取到」
    只可能是取数失败，必须抛异常交由调用方决策（回退旧缓存 / 离线快照 / 明确
    告知用户不可用），**绝不能**当成「今天没有股票」写入缓存。

    背景：此前 fetch_all 失败时静默 ``return []``，上层把它当作当日有效结果缓存，
    前端见 total=0 便渲染「未找到匹配」，导致整日不可自愈、重启无效。
    """


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
    # 按健康度排序：刚失败过的主机（冷却期内）排到末尾，但**不删除**——
    # 上游限流是按路径/时段变化的（实测 push2delay 的 clist 被封时 slist 仍可用），
    # 冷却是为了让下一次请求走通，而不是永久判死。
    for base in ordered_hosts():
        url = base + "/api/qt/clist/get?" + urllib.parse.urlencode(params)
        # 优先用 curl（沙箱/部分环境 urllib 直连被重置时更稳）
        try:
            out = subprocess.check_output(
                ["curl", "-s", "-m", "20", "-A", UA, "-e", REF, url],
                timeout=25,
            )
            if out.strip():
                mark_host_up(base)
                return json.loads(out.decode("utf-8"))
        except Exception:
            pass
        # 回退 urllib
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                mark_host_up(base)
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
        mark_host_down(base)
    raise last_err or RuntimeError("所有数据源均不可用")


def fetch_all(pz, stats=None):
    """分页拉取全市场，直到取满 total。

    **全部失败时抛 MarketSourceUnavailable**，不静默返回空列表 —— 这是
    「股票页空列表」事故的根因。

    本函数只保证「拿到的部分按代码去重、分页与服务端页容量对齐」，
    **完整性由调用方用 coverage_report() 判定**（2026-09-24 事故：残缺集被
    当成完整数据落盘，导致创业板整段搜不到）。`stats` 带出 total / fetched /
    failures / page_size，供上层打日志与判定。

    两个容易踩的坑，都在这里处理：

    1. **页容量随主机变化**。实测 `pz=1000` 时 push2test 真给 1000 条，
       push2delay / push2 会被钳制到 100 条。若机械地 `pn += 1` 翻页，
       中途换主机就会因页容量变化而**缺页或整段重复**。
       故下一页的 pn 由「已取条数 ÷ 实际页容量」推出。
    2. **页间可能重叠**。按 code 去重（dict 保序），并记录「整页无新增」的
       收手条件，避免在重叠视野里空转。
    """
    rows = {}              # code -> row（天然去重）
    order = []             # 首次出现顺序
    total = 0
    pn = 1
    page_size = pz
    empty_streak = 0
    failures = []          # 每页失败原因，便于异常信息与日志定位
    while True:
        payload = None
        last_err = None
        for attempt in range(PAGE_RETRIES):
            try:
                payload = fetch(pn, page_size)
                break
            except Exception as e:
                last_err = e
                time.sleep(RETRY_BACKOFF * (attempt + 1))
        if payload is None:
            failures.append("第 %d 页: %s" % (pn, last_err))
            break
        data = payload.get("data") or {}
        if not total:
            total = data.get("total") or 0
        diff = data.get("diff") or []
        items = list(diff.values()) if isinstance(diff, dict) else list(diff)
        if not items:
            failures.append("第 %d 页: 返回空列表" % pn)
            empty_streak += 1
            if empty_streak >= 2:
                break
            pn += 1
            time.sleep(0.3)
            continue
        empty_streak = 0
        if len(items) != page_size:        # 页容量以实际返回为准
            page_size = len(items)
        added = 0
        for v in items:
            code = str((v or {}).get("f12") or "")
            if not code or code in rows:
                continue
            rows[code] = v
            order.append(code)
            added += 1
        got = len(order)
        if total and got >= total:
            break
        if added == 0:
            # 整页都是已取过的记录：该主机的分页视野已走完（或页容量缩小导致
            # 视野重叠），继续翻只会重复。收手，把完整性交给上层判定。
            failures.append("第 %d 页起无新增(已取 %d/%s)" % (pn, got, total or "?"))
            break
        pn = got // page_size + 1          # 按实际页容量对齐，避免缺页/重复
        _page_sleep()   # 限速 + 抖动，避免被判定为爬虫

    out = [rows[c] for c in order]
    if stats is not None:
        stats.update({"total": total, "fetched": len(out),
                      "failures": failures, "page_size": page_size})
    if not out:
        raise MarketSourceUnavailable(
            "全市场行情源不可用(total=%s): %s"
            % (total or "?", "; ".join(failures[:3]) or "未知原因"))
    return out


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


# ===== 覆盖率护栏 =====
# 2026-09-24 事故：全市场取数中途断页，只拿到 1961/5561 只（创业板、深市 002
# 整段缺失、沪市主板只到 51%）。当时这条链路上**没有任何规模校验**，残缺集被
# 标 ok 落盘，之后整个交易日复用它 —— 表现为「搜力源信息 / 比亚迪搜不到」
# 却显示数据正常，且重启无效（同日必命中落盘缓存）。
#
# 结论：**全市场行情必须「接近完整」才可当作有效数据使用与落盘。**
# 「有部分数据优于报空」只在**当次展示**成立；一旦落盘并标 ok，残缺就被固化，
# 比明确报「不可用」危险得多。
#
# 阈值可用环境变量覆盖，避免「调一次参数改一次代码」。
MIN_MARKET_COVERAGE = _env_float("MARKET_MIN_COVERAGE", 0.8)        # 总量覆盖率下限
MIN_SEGMENT_COVERAGE = _env_float("MARKET_MIN_SEG_COVERAGE", 0.5)   # 单段覆盖率下限
MIN_SEGMENT_SIZE = _env_int("MARKET_MIN_SEG_SIZE", 50)              # 段规模小于此值不校验
MIN_MARKET_RECORDS = _env_int("MARKET_MIN_RECORDS", 3000)           # 无代码宇宙时的绝对下限

# 市场分段：用于**分段**覆盖率校验。
# 为什么非要分段：整段缺失（如创业板 0 条）是分页中断的典型签名，而总量的
# 小缺口（如 98%）可能只是退市股差异。只看总量会漏掉「整段没了」这种情况。
SEGMENTS = (
    ("沪市主板", ("600", "601", "603", "605")),
    ("科创板", ("688", "689")),
    ("创业板", ("300", "301", "302")),
    ("深市中小", ("002",)),
    ("深市主板", ("000", "001", "003")),
    ("北交所", ("4", "8", "9")),
)


def market_segment(code):
    """把 6 位代码归入市场分段（用于覆盖率对账）。"""
    c = str(code or "")
    for label, prefixes in SEGMENTS:
        if c.startswith(prefixes):
            return label
    return "其他"


def coverage_report(records, universe=None):
    """校验全市场结果的完整度，返回 (ok, detail)。

    records  : 待校验的 record 列表（每项含 "code"）
    universe : 代码宇宙（通常取自离线快照）；为空则退回绝对条数下限

    判定规则（任一不满足即视为不完整）：
      1. 总量 >= MIN_MARKET_COVERAGE × len(universe)
      2. 每个规模 >= MIN_SEGMENT_SIZE 的市场段，其覆盖率 >= MIN_SEGMENT_COVERAGE

    设计取向是**宁可判不完整**：误判成不完整的代价是这一次降级到备用源或
    旧快照（仍能搜到全部标的），而漏判的代价是用户搜不到股票却被告知「数据正常」。
    """
    codes = [str((r or {}).get("code") or "") for r in (records or [])]
    n = len(codes)
    if not n:
        return False, "结果为空"

    if universe:
        uni = [str((u or {}).get("code") or "") for u in universe]
        need = len(uni)
        if need:
            ratio = n / float(need)
            if ratio < MIN_MARKET_COVERAGE:
                return False, "总量 %d/%d = %.1f%% < %.0f%%" % (
                    n, need, ratio * 100, MIN_MARKET_COVERAGE * 100)
            need_by = Counter(market_segment(c) for c in uni)
            have_by = Counter(market_segment(c) for c in codes)
            for seg, cnt in need_by.items():
                if cnt < MIN_SEGMENT_SIZE:
                    continue
                got = have_by.get(seg, 0)
                if got < MIN_SEGMENT_COVERAGE * cnt:
                    return False, "%s 段 %d/%d = %.1f%% < %.0f%%" % (
                        seg, got, cnt, got * 100.0 / cnt, MIN_SEGMENT_COVERAGE * 100)
            return True, "总量 %d/%d = %.1f%%，分段完整" % (n, need, ratio * 100)

    # 无代码宇宙可比对（快照缺失）时只能做绝对下限校验，如实说明
    if n < MIN_MARKET_RECORDS:
        return False, "总量 %d < %d（无代码宇宙可比对）" % (n, MIN_MARKET_RECORDS)
    return True, "总量 %d（无代码宇宙可比对）" % n


def fetch_market_records(stats=None):
    """拉取全市场并整理为统一 record 列表（默认按总市值降序）。

    返回: [{"name","code","ind","price","chg","dir",
            "mcap","mcap_disp","fcap","fcap_disp"}, ...]
    供 server.py 的内存缓存 / 兜底 JSON 共用，避免重复取数逻辑。

    取数失败抛 MarketSourceUnavailable；返回的列表保证非空。
    **非空 ≠ 完整**：调用方必须再用 coverage_report() 判定，
    不完整时不得当作 ok 数据使用或落盘。`stats` 透传 fetch_all 的诊断信息。
    """
    raw = fetch_all(PAGE_SIZE, stats=stats)
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
    if not stocks:
        # 上游返回了行但全被过滤掉（字段结构变化/风控页），同样按「不可用」处理，
        # 避免把结构性故障伪装成「市场为空」。
        raise MarketSourceUnavailable(
            "行情源返回 %d 行但无有效记录(字段结构可能已变化)" % len(raw))
    # 默认按总市值降序（无市值者排末尾）
    stocks.sort(key=lambda s: (s["mcap"] is None, -(s["mcap"] or 0)))
    return stocks


# ===== 备用行情源：腾讯 qt.gtimg.cn =====
# 仅在东方财富 push2* 不可用时启用（实测 push2* 集群会单独被限流/封锁，
# 而腾讯行情通道独立可用）。腾讯只提供行情，不提供行业分类，因此需要
# 一个「代码宇宙」来批量询价——最合适的现成来源就是离线快照。
#
# 实测（2026-09-23）：
#   - 单请求 900 只 OK（URL 8121 字符）；1000 只 -> HTTP 414 Request-URI Too Large
#     => 腾讯的 URL 上限约 8KB，故批量取 800（URL≈7.2KB）留足余量。
#   - 经 curl 传超长 URL 会撞 Windows 命令行长度上限（WinError 206），
#     因此本通道**只用 urllib**（进程内，不受命令行长度限制）。
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
TENCENT_BATCH = 800         # 单请求代码数（URL≈7.2KB，安全区）
TENCENT_INTERVAL = 0.08     # 批次间隔(秒)
TENCENT_MAX_BATCHES = 12    # 批次数上限（800*12=9600，覆盖全市场且防失控）
TENCENT_REF = "https://gu.qq.com/"

# 腾讯行情字段位序（实测确认，共 88 个字段，GBK 编码）
TX_NAME, TX_CODE, TX_PRICE, TX_OPEN = 1, 2, 3, 5
TX_CHG_AMT, TX_CHG_PCT = 31, 32
TX_HIGH, TX_LOW = 33, 34
TX_VOL, TX_AMOUNT_WAN, TX_TURNOVER = 36, 37, 38
TX_PE, TX_FCAP_YI, TX_MCAP_YI, TX_PB = 39, 44, 45, 46
TX_MIN_FIELDS = 47          # 至少要覆盖到市净率


def tx_symbol(code):
    """腾讯行情需要「市场前缀+代码」。比东财 secid 多一个北交所 bj。"""
    c = (code or "").strip()
    if not c:
        return ""
    if c[:3] == "920" or c[:1] in ("4", "8"):
        return "bj" + c
    if c[:1] in ("6", "9"):
        return "sh" + c
    return "sz" + c


def _tx_parse(text):
    """解析腾讯返回文本 -> {纯代码: 字段列表}。"""
    out = {}
    for line in (text or "").split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        _, val = line.split("=", 1)
        fields = val.strip().strip('"').split("~")
        if len(fields) < TX_MIN_FIELDS:
            continue
        code = fields[TX_CODE].strip()
        if code:
            out[code] = fields
    return out


def _tx_fetch_batch(symbols):
    """取一批腾讯行情。失败返回 {}（由上层判定是否整体不可用）。"""
    if not symbols:
        return {}
    url = TENCENT_QUOTE_URL + ",".join(symbols)
    if len(url) > 8000:      # 兜底护栏：宁可不发，也不要换来一个 414
        raise MarketSourceUnavailable("腾讯行情 URL 过长(%d 字符)，请调小 TENCENT_BATCH" % len(url))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": TENCENT_REF})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return _tx_parse(resp.read().decode("gbk", "replace"))
    except Exception:
        return {}


def _tx_record(fields, ind, name_fallback=""):
    """把腾讯字段转成与东财 fetch_market_records 完全同构的 record。"""
    pv = to_num(fields[TX_PRICE])
    cv = to_num(fields[TX_CHG_PCT])
    # 腾讯市值单位是「亿元」，统一换算成「元」，与东财 f20/f21 口径一致
    mcap = to_num(fields[TX_MCAP_YI])
    fcap = to_num(fields[TX_FCAP_YI])
    mcap = mcap * 1e8 if mcap is not None else None
    fcap = fcap * 1e8 if fcap is not None else None
    vol = to_num(fields[TX_VOL])
    amount_wan = to_num(fields[TX_AMOUNT_WAN])
    amount = amount_wan * 1e4 if amount_wan is not None else None
    chg_amt = to_num(fields[TX_CHG_AMT])
    turnover = to_num(fields[TX_TURNOVER])
    pe = to_num(fields[TX_PE])
    pb = to_num(fields[TX_PB])
    name = (fields[TX_NAME] or "").strip() or name_fallback
    if not name:
        return None
    if cv is None:
        chg_str, cv0 = "--", 0.0
    else:
        chg_str, cv0 = (("+" if cv > 0 else "") + ("%.2f" % cv) + "%"), cv
    if chg_amt is None:
        chg_amt_str = "--"
    else:
        chg_amt_str = (("+" if chg_amt > 0 else "") + "%.2f" % chg_amt)
    return {
        "name": name,
        "code": fields[TX_CODE].strip(),
        "ind": ind,
        "price": ("¥%.2f" % pv) if pv is not None else "--",
        "chg": chg_str,
        "chg_num": cv0,          # 数值型，供排序使用
        "dir": "up" if cv0 > 0 else ("down" if cv0 < 0 else "flat"),
        "open": to_num(fields[TX_OPEN]),
        "high": to_num(fields[TX_HIGH]),
        "low": to_num(fields[TX_LOW]),
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
        "mcap": mcap,
        "mcap_disp": fmt_mcap(mcap),
        "fcap": fcap,
        "fcap_disp": fmt_mcap(fcap),
        # 基本面四指标与东财口径一致：行情快照不提供，置空由后端按需补
        "roe": None, "roe_disp": "--",
        "gross": None, "gross_disp": "--",
        "debt_ratio": None, "debt_ratio_disp": "--",
        "rev_growth": None, "rev_growth_disp": "--",
    }


def fetch_market_records_tencent(universe):
    """用腾讯行情刷新 universe 的实时价格，返回与东财同构的 record 列表。

    universe: [{"code","ind","name"}, ...]（通常来自离线快照）。
    腾讯不提供行业分类，故 ind 沿用 universe 的快照值——这不是缺陷而是
    明确取舍：宁可行业信息略旧，也要拿到**实时价格**。

    取到 0 条时抛 MarketSourceUnavailable，交由上层继续降级。
    """
    codes = [str(u.get("code") or "").strip() for u in universe if u.get("code")]
    if not codes:
        raise MarketSourceUnavailable("腾讯备用源缺少代码宇宙（无可用快照）")
    ind_by_code = {str(u.get("code") or "").strip(): u.get("ind") or "" for u in universe}
    name_by_code = {str(u.get("code") or "").strip(): u.get("name") or "" for u in universe}

    rows = []
    batches = 0
    for i in range(0, len(codes), TENCENT_BATCH):
        if batches >= TENCENT_MAX_BATCHES:
            break
        chunk = codes[i:i + TENCENT_BATCH]
        got = _tx_fetch_batch([tx_symbol(c) for c in chunk])
        batches += 1
        for c in chunk:
            f = got.get(c)
            if not f:
                continue
            rec = _tx_record(f, ind_by_code.get(c) or "—", name_by_code.get(c, ""))
            if rec:
                rows.append(rec)
        if i + TENCENT_BATCH < len(codes):
            time.sleep(TENCENT_INTERVAL)

    if not rows:
        raise MarketSourceUnavailable("腾讯备用行情源不可用(%d 批均无数据)" % batches)
    rows.sort(key=lambda s: (s["mcap"] is None, -(s["mcap"] or 0)))
    return rows


# ===== 离线快照（最后一道可用数据来源）=====
# 由本模块 main() 生成：python fetch_stocks.py
SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stocks.json")


_snapshot_lock = threading.Lock()
_snapshot_mirror = {"mtime": None, "records": None, "updated": None}


def load_snapshot():
    """读取离线快照 frontend/data/stocks.json。

    返回 (records, updated)；文件缺失/损坏/为空时返回 ([], None)。

    两个用途：
      1. 上游不可用时的兜底数据源（快照虽非实时，但「浏览 / 搜索标的」完全够用，
         明显优于把数据源故障报成「未找到匹配」）；
      2. **覆盖率护栏的「代码宇宙」基准** —— coverage_report 需要一份
         「A 股本该有哪些代码」的参照。

    读取失败一律返回空而不抛异常——兜底路径不应再抛。
    带 mtime 镜像：护栏在每次取数、每次读落盘缓存时都要取 universe，
    无缓存就等于反复解析 ~1.3MB JSON。**返回浅拷贝**，避免调用方 sort 污染镜像。
    """
    try:
        mtime = os.path.getmtime(SNAPSHOT_PATH)
    except OSError:
        mtime = None                       # 文件不存在
    with _snapshot_lock:
        if _snapshot_mirror["mtime"] == mtime and _snapshot_mirror["records"] is not None:
            return list(_snapshot_mirror["records"]), _snapshot_mirror["updated"]

    records, updated = [], None
    if mtime is not None:
        try:
            with open(SNAPSHOT_PATH, encoding="utf-8") as f:
                obj = json.load(f)
            stocks = obj.get("stocks") if isinstance(obj, dict) else None
            if isinstance(stocks, list) and stocks:
                # 快照可能生成于旧版本，补齐新字段缺失的键，避免前端取值报错
                for s in stocks:
                    if isinstance(s, dict):
                        s.setdefault("chg_num", 0.0)
                        s.setdefault("dir", "flat")
                        s.setdefault("ind", "—")
                records, updated = stocks, obj.get("updated")
        except Exception as e:
            print("[warn] 离线快照读取失败:", e)
            records, updated = [], None
    with _snapshot_lock:
        _snapshot_mirror.update({"mtime": mtime, "records": records,
                                 "updated": updated})
    return list(records), updated


def main():
    try:
        stocks = fetch_market_records()
    except Exception as e:
        print("拉取失败:", e)
        sys.exit(1)

    out_dir = os.path.dirname(SNAPSHOT_PATH)
    os.makedirs(out_dir, exist_ok=True)
    out_path = SNAPSHOT_PATH
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {"updated": time.strftime("%Y-%m-%d %H:%M"),
             "count": len(stocks), "stocks": stocks},
            f, ensure_ascii=False, indent=2,
        )
    print("已写入 %d 支股票(按总市值降序) -> %s" % (len(stocks), out_path))


if __name__ == "__main__":
    main()
