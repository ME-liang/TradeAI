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
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REF = "https://quote.eastmoney.com/"

# 复用 fetch_stocks 的取数逻辑(已抽取为 fetch_market_records)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_stocks  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)          # 项目根：运行期缓存放 <ROOT>/data/cache/

# 报告生成服务(FastAPI)地址; 由前端同源反向代理 /api/report*, 免 CORS。
# 启动时需另起: uvicorn src.fundamental_agent.api.server:app --host 127.0.0.1 --port 8002
# 端口口径: 8000=前端(本服务) / 8002=报告 API。
# 原默认值 8001 与线上实际部署不符且已确死（P3-4 实测直连 WinError 10061 连接被拒），
# 一旦漏设环境变量就会出现「一键生成」报「报告服务不可达」的假故障，故默认值改为 8002。
REPORT_API_BASE = os.environ.get("REPORT_API_BASE", "http://127.0.0.1:8002").rstrip("/")

# 代理时**必须原样透传**的额外响应头（除 Content-Type / Set-Cookie / Content-Length
# 这些已单独处理的外）。做成常量 + 源码级测试护栏，理由：漏一个头的症状是静默的
# —— 页面看起来完全正常，只有"保存下来的文件名不对"或"告警不见了"。
#
#   Content-Disposition  导出（F-07）用它下发带中文名的下载文件名。丢了它，用户经
#                        8000 下载会拿到随机名（`tradeai_export_xxxx.pdf`），
#                        "能发给别人"这件事直接折半。
#   X-Export-Format      实际采用的格式（前端据此提示"已导出 PDF"）
#   X-Export-Bytes       实际字节数（响应体是文件字节，这是唯一能承载元信息的地方）
#   X-Export-Warnings    图表未取到之类的**可见告警**（百分号编码的中文）
#   Accept-Ranges /      大报告下载时浏览器会发 Range 请求；不转发这两头，续传/分段
#   Content-Range        下载会退化，且响应头与响应体自相矛盾。
_PROXY_PASS_HEADERS = (
    "Content-Disposition",
    "X-Export-Format",
    "X-Export-Bytes",
    "X-Export-Warnings",
    "Accept-Ranges",
    "Content-Range",
)

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
_cache = {
    "date": None,       # 该批数据对应的「交易日」键
    "records": None,    # 全市场 record 列表（**保证非空**才算有效缓存）
    "updated": None,    # 写入时刻（展示用）
    "source": None,     # "eastmoney" | "tencent"，决定缓存命中的 data_status
}

# 排序字段映射: 前端传 mcap/fcap/chg -> 后端 record 字段
SORT_FIELDS = {
    "mcap": "总市值",
    "fcap": "流通市值",
    "chg": "涨跌幅",
}


# ===== 交易日键 =====
# 之前直接用 date.today()，语义与「交易日」无关：周末与节假日会把同一份收盘数据
# 当成两个不同的交易日，也会让人误以为休市日还会有新数据。
# 这里按「周末回退 + 可选节假日清单」取最近一个交易日。
# 节假日清单：data/holidays.txt，每行一个 YYYY-MM-DD，'#' 起为注释。
# 不内置日历是有意为之——A 股节假日每年由交易所公布，硬编码必然过期；
# 清单文件缺失时只做周末回退（宁可多刷一次，也不误判成休市而不刷）。
HOLIDAY_FILE = os.path.join(ROOT_DIR, "data", "holidays.txt")
_holiday_cache = {"mtime": None, "days": frozenset()}


def _load_holidays():
    try:
        mtime = os.path.getmtime(HOLIDAY_FILE)
    except OSError:
        mtime = None
    with _cache_lock:
        if _holiday_cache["mtime"] == mtime:
            return _holiday_cache["days"]
    days = set()
    if mtime is not None:
        try:
            with open(HOLIDAY_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if not line:
                        continue
                    try:
                        days.add(datetime.date.fromisoformat(line))
                    except ValueError:
                        print("[warn] 节假日清单忽略非法日期: %r" % line)
        except Exception as e:
            print("[warn] 节假日清单读取失败:", e)
    with _cache_lock:
        _holiday_cache.update({"mtime": mtime, "days": frozenset(days)})
    if days:
        print("[cache] 已载入节假日清单 %d 天" % len(days))
    return _holiday_cache["days"]


def _trading_day(now=None):
    """返回「最近一个交易日」的 ISO 日期，作为缓存键。

    交易日定义：非周末，且不在节假日清单内。休市日回退到上一个交易日，
    这样周末/节假日的请求会命中同一份收盘数据，而不是被当成新的「一天」。
    """
    today = now or datetime.date.today()
    holidays = _load_holidays()
    d = today
    for _ in range(40):        # 防御性上限：清单异常时也不会死循环
        if d.weekday() < 5 and d not in holidays:
            return d.isoformat()
        d -= datetime.timedelta(days=1)
    return today.isoformat()


# ===== 落盘缓存 =====
# 目的：把「一次全市场拉取」的成果跨进程/跨重启复用。
# 此前每次启动都要打 ~56 页全市场，既慢又正是被上游限流的主因；
# 同一交易日内已有落盘数据时直接复用，不再触网。
# 路径沿用项目既有缓存根 data/cache/（已在 .gitignore 中）。
CACHE_DIR = os.path.join(ROOT_DIR, "data", "cache")
MARKET_CACHE_PATH = os.path.join(CACHE_DIR, "market_quotes.json")
MARKET_CACHE_VERSION = 1
MARKET_CACHE_MAX_AGE_DAYS = 5      # 超过则视为不可用，避免"复活"远古数据

# 落盘缓存的内存镜像：避免每个请求都重读 ~5MB JSON
_disk_mirror = {"records": None, "source": None, "updated": None, "tried": False}


def _age_ok(fetched_at):
    try:
        ts = datetime.datetime.strptime(fetched_at, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return False
    age = datetime.datetime.now() - ts
    return datetime.timedelta(0) <= age <= datetime.timedelta(days=MARKET_CACHE_MAX_AGE_DAYS)


def _day_of(fetched_at):
    """从 'YYYY-MM-DD HH:MM' 取出日期部分。"""
    return (fetched_at or "")[:10]


def _save_disk_cache(records, source, fetched_at):
    """原子写入落盘缓存；任何失败只告警，绝不影响主流程。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        payload = {"version": MARKET_CACHE_VERSION, "fetched_at": fetched_at,
                   "source": source, "count": len(records), "records": records}
        tmp = MARKET_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, MARKET_CACHE_PATH)     # 原子替换，不留半截文件
        _disk_mirror.update({"records": records, "source": source,
                             "updated": fetched_at, "tried": True})
    except Exception as e:
        print("[warn] 落盘缓存写入失败(不影响本次使用):", e)


def _load_disk_cache():
    """读取落盘缓存（带进程内镜像）。返回 (records, source, fetched_at)。

    不可用（不存在/损坏/版本不符/过期）时返回 (None, None, None)。
    """
    if _disk_mirror["tried"]:
        return (_disk_mirror["records"], _disk_mirror["source"], _disk_mirror["updated"])
    _disk_mirror["tried"] = True
    try:
        with open(MARKET_CACHE_PATH, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return None, None, None
    except Exception as e:
        print("[warn] 落盘缓存读取失败:", e)
        return None, None, None
    if not isinstance(payload, dict) or payload.get("version") != MARKET_CACHE_VERSION:
        print("[info] 落盘缓存版本不符, 忽略")
        return None, None, None
    records, fetched_at = payload.get("records"), payload.get("fetched_at")
    # 空列表一律不认——与内存缓存同一条铁律
    if not isinstance(records, list) or not records:
        print("[info] 落盘缓存为空, 忽略")
        return None, None, None
    if not _age_ok(fetched_at):
        print("[info] 落盘缓存已超 %d 天或时间戳异常, 忽略" % MARKET_CACHE_MAX_AGE_DAYS)
        return None, None, None

    # 覆盖率护栏：**残缺的落盘缓存必须被拒绝**。
    # 2026-09-24 事故正是「1961 条（创业板、深市 002 整段缺失）的缓存被读入并标 ok」；
    # 又因为同一交易日优先复用落盘缓存，重启也无法自愈，一整天都搜不到那 65% 的股票。
    # 把校验放在读取处，坏缓存会自动失效、后续请求转而重新拉取实时数据
    # ——即便坏文件还躺在磁盘上（例如来不及手工删除）也不会再被使用。
    universe, _upd = fetch_stocks.load_snapshot()
    cov_ok, cov_why = fetch_stocks.coverage_report(records, universe or None)
    if not cov_ok:
        print("[warn] 落盘缓存覆盖率不足, 忽略该缓存并重新取数: %s" % cov_why)
        return None, None, None

    _disk_mirror.update({"records": records, "source": payload.get("source"),
                         "updated": fetched_at})
    return records, payload.get("source"), fetched_at


def _invalidate_disk_mirror():
    _disk_mirror.update({"records": None, "source": None, "updated": None, "tried": False})


def _now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# ===== 板块行情的落盘缓存 =====
# 与全市场行情同一个道理：板块清单每次冷构建要翻 5~6 页 clist，而**启动预取**
# 对行业+概念各拉一遍全量 + 各拉一个排序窗口块 + 分类清单，合计约 25 次 clist
# —— 这与此前把 push2 打成「爬虫」被限流高度相关（2026-09-23 实测 push2* 的
# clist 路径全线被封）。落盘后同一交易日重启零触网，既是提速也是降风险。
# 一个 kind 一个文件，避免多 kind 共写一份 JSON 需要读-改-写。
BOARD_CACHE_VERSION = 1
_board_disk_mirror = {}        # {kind: (records, fetched_at)} 内存镜像，避免每请求重读
_board_disk_lock = threading.Lock()


def _board_cache_path(kind):
    return os.path.join(CACHE_DIR, "board_%s_quotes.json" % kind)


def _save_board_disk_cache(kind, records, fetched_at=None):
    """原子写入板块落盘缓存；任何失败只告警，绝不影响主流程。"""
    if not records:
        return                      # 空列表永不落盘——与内存缓存同一条铁律
    fetched_at = fetched_at or _now_str()
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        payload = {"version": BOARD_CACHE_VERSION, "kind": kind,
                   "fetched_at": fetched_at, "count": len(records), "records": records}
        path = _board_cache_path(kind)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)       # 原子替换，不留半截文件
        with _board_disk_lock:
            _board_disk_mirror[kind] = (records, fetched_at)
    except Exception as e:
        print("[warn] 板块落盘缓存写入失败(不影响本次使用):", e)


def _load_board_disk_cache(kind):
    """读取板块落盘缓存，返回 (records, fetched_at)；不可用返回 (None, None)。"""
    with _board_disk_lock:
        if kind in _board_disk_mirror:
            recs, at = _board_disk_mirror[kind]
            return (recs, at) if recs else (None, None)
    recs, at = None, None
    try:
        with open(_board_cache_path(kind), encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict) and payload.get("version") == BOARD_CACHE_VERSION:
            r, a = payload.get("records"), payload.get("fetched_at")
            if isinstance(r, list) and r and _age_ok(a):
                recs, at = r, a
            elif r:
                print("[info] 板块落盘缓存已超 %d 天或时间戳异常, 忽略" % MARKET_CACHE_MAX_AGE_DAYS)
    except FileNotFoundError:
        pass
    except Exception as e:
        print("[warn] 板块落盘缓存读取失败:", e)
    with _board_disk_lock:
        _board_disk_mirror[kind] = (recs, at)
    return recs, at


def _status_for_source(source):
    """缓存命中时按数据来源还原 data_status（东财=ok，腾讯备用源=backup）。"""
    return "ok" if source in (None, "eastmoney") else "backup"


# 实时源失败后的冷却窗口：避免上游挂掉时「每个请求都等一次长超时」。
# 窗口内的请求直接走降级数据（秒回），窗口过后再试一次实时源。
FAILURE_COOLDOWN = 60          # 秒
_failure_lock = threading.Lock()
_last_failure = {"ts": 0.0, "reason": None}


def _mark_failure(reason):
    with _failure_lock:
        _last_failure["ts"] = time.time()
        _last_failure["reason"] = reason


def _in_cooldown():
    with _failure_lock:
        return (time.time() - _last_failure["ts"]) < FAILURE_COOLDOWN


def _fetch_live():
    """尝试实时源，返回 (records, source, err)。

    全部失败**或结果不完整**时 records 为 None。

    **每条实时通道都必须过覆盖率校验**（共用 fetch_stocks.coverage_report）：
    全市场行情「接近完整」才有意义，残缺集一旦被当 ok 落盘就会整日固化。
    2026-09-24 事故的根因正是东财这条通道缺了这道校验 —— 1961/5561 的残缺集
    被标 ok 落盘，创业板整段搜不到却显示数据正常，且重启无效。
    现在的取向：**宁可降级到旧快照（价旧但全量），也不接受残缺数据。**
    """
    # 代码宇宙：既用于校验东财结果，也作为腾讯备用源的询价清单
    universe, _upd = fetch_stocks.load_snapshot()

    try:
        stats = {}
        recs = fetch_stocks.fetch_market_records(stats=stats)
        ok, why = fetch_stocks.coverage_report(recs, universe or None)
        if ok:
            return recs, "eastmoney", None
        print("[warn] 东财实时结果不完整, 放弃该通道: %s（翻页: %s）"
              % (why, "; ".join(stats.get("failures") or []) or "无翻页错误"))
        live_err = "东财结果不完整(%s)" % why
    except Exception as e:
        live_err = e

    # ---- 二级实时源：腾讯（以「代码宇宙」批量询价）----
    # 腾讯不提供行业分类，故需要一份代码宇宙；用离线快照即可（行业沿用快照值）。
    if not universe:
        return None, None, live_err
    try:
        cand = fetch_stocks.fetch_market_records_tencent(universe)
        ok, why = fetch_stocks.coverage_report(cand, universe)
        if ok:
            return cand, "tencent", live_err
        print("[warn] 腾讯备用源不完整, 放弃该通道: %s" % why)
    except Exception as e:
        print("[warn] 腾讯备用源失败:", e)
    return None, None, live_err


def get_market_data():
    """返回 (records, data_status, meta)。

    `data_status` 让前端不必猜测空列表的含义：
      - "ok"          东方财富实时取数成功
      - "backup"      东财不可用，改用腾讯备用源实时取数（价格仍是实时的）
      - "stale"       两条实时通道都失败，回退内存旧缓存（可用于展示但已过期）
      - "snapshot"    回退到静态离线快照（frontend/data/stocks.json）
      - "unavailable" 全部失败，明确告知不可用

    降级顺序：
      1. 内存缓存（本进程、当前交易日）
      2. 落盘缓存（当前交易日）—— 同一交易日已抓过就直接复用，不再触网
      3. 东财实时
      4. 腾讯备用源实时（价格仍是实时的，行业沿用快照）
      5. 内存/落盘旧数据（取两者中较新的）-> stale
      6. 静态离线快照 -> snapshot
      7. unavailable

    实时源失败后进入 FAILURE_COOLDOWN 秒冷却，窗口内直接走降级数据（秒回），
    窗口过后再试实时源；一旦成功即写入内存 + 落盘缓存，恢复常态。

    关键约定：**空列表永不写入任何缓存**（内存与落盘同一铁律）。此前
    get_records() 只在「抛异常」时才降级，而 fetch_all 失败时是静默返回 []，
    于是空结果被当作当日有效数据缓存下来，前端见 total=0 便渲染「未找到匹配」，
    形成整日不可自愈、重启无效的故障。现在取数层改为抛异常
    （见 fetch_stocks.MarketSourceUnavailable），这里再补一道「非空才缓存」
    的防线，两层都可独立兜住。
    """
    day = _trading_day()
    with _cache_lock:
        if _cache["date"] == day and _cache["records"]:
            return (_cache["records"], _status_for_source(_cache["source"]),
                    {"updated": _cache["updated"], "fetched_at": _cache["updated"],
                     "source": _cache["source"], "layer": "memory"})
        mem_stale = ((_cache["updated"], _cache["records"], _cache["source"])
                     if _cache["records"] else None)

    # ---- 落盘缓存：同一交易日内直接可用，省掉一次 56 页全市场拉取 ----
    disk_records, disk_source, disk_at = _load_disk_cache()
    if disk_records and _day_of(disk_at) == day:
        with _cache_lock:
            _cache.update({"date": day, "records": disk_records, "updated": disk_at,
                           "source": disk_source})
        print("[cache] 命中落盘缓存(%d 条, 抓取于 %s, 来源=%s), 跳过全市场拉取"
              % (len(disk_records), disk_at, disk_source))
        return (disk_records, _status_for_source(disk_source),
                {"updated": disk_at, "fetched_at": disk_at,
                 "source": disk_source, "layer": "disk"})

    # ---- 实时取数（放锁外，避免阻塞其它请求）；冷却窗口内跳过以保响应速度 ----
    live_err = None
    if not _in_cooldown():
        recs, source, live_err = _fetch_live()
        if recs:
            now = _now_str()
            with _cache_lock:
                _cache.update({"date": day, "records": recs, "updated": now,
                               "source": source})
            _save_disk_cache(recs, source, now)
            if source == "eastmoney":
                return (recs, "ok", {"updated": now, "fetched_at": now,
                                     "source": source, "layer": "live"})
            print("[warn] 东财不可用, 已用腾讯备用源刷新 %d 只实时行情" % len(recs))
            return (recs, "backup", {"updated": now, "fetched_at": now,
                                     "source": source, "layer": "live",
                                     "reason": str(live_err)})
        _mark_failure(str(live_err) if live_err else "行情源返回空结果")
    else:
        print("[info] 实时源冷却中, 直接使用降级数据")

    reason = str(live_err) if live_err else "行情源不可用(冷却中)"

    # ---- 旧数据：内存与落盘各一份，取时间更近的 ----
    candidates = []
    if mem_stale and mem_stale[1]:
        candidates.append(mem_stale)
    if disk_records:
        candidates.append((disk_at, disk_records, disk_source))
    if candidates:
        at, recs_old, src_old = max(candidates, key=lambda c: c[0] or "")
        print("[warn] 实时取数失败, 回退旧数据(%d 条, %s): %s"
              % (len(recs_old), at, reason))
        return recs_old, "stale", {"updated": at, "reason": reason, "source": src_old}

    # ---- 再退一步：静态离线快照 ----
    # 注意**不写入任何缓存** —— 快照是静态的，若缓存下来，上游恢复后会被误当成
    # 当日实时数据，反而延长故障。
    snap, snap_updated = fetch_stocks.load_snapshot()
    if snap:
        print("[warn] 全市场行情源不可用, 使用离线快照(%d 条, %s): %s"
              % (len(snap), snap_updated, reason))
        return snap, "snapshot", {"updated": snap_updated, "reason": reason}

    print("[error] 全市场行情源不可用: %s" % reason)
    return [], "unavailable", {"reason": reason}


def get_records():
    """兼容入口：只取 record 列表（自选行情 / 板块等调用方沿用）。

    需要区分「不可用」与「真空」的调用方，请改用 get_market_data()。
    """
    return get_market_data()[0]


def _prefetch():
    """启动时后台预热缓存, 让首个前端请求不必等待全市场拉取。

    多数情况下这里会命中落盘缓存（同一交易日内）而完全不触网 —— 这正是
    「每次重启都打 56 页全市场」这个被上游限流主因的治本点。
    """
    try:
        recs, status, meta = get_market_data()
        layer = meta.get("layer") or "-"
        if status == "ok":
            print("[cache] 已预热全市场股票缓存: %d 条(层级=%s, 来源=%s)"
                  % (len(recs), layer, meta.get("source")))
        elif status == "backup":
            print("[cache] 已预热(东财不可用, 备用源腾讯): %d 条" % len(recs))
        else:
            # 不再无条件打印「已预热」——否则取数失败时日志看起来一切正常，
            # 会把「静默返回空」伪装成成功（此前的排查陷阱之一）。
            print("[cache] 预热未取到实时数据(status=%s): %s"
                  % (status, meta.get("reason", "")))
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

    recs, data_status, meta = get_market_data()
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
        "updated": _now_str(),
        "total": total,
        "page": page,
        "pageSize": pageSize,
        "sort": sort,
        "order": order,
        "keyword": q,
        "stocks": page_items,
        # 前端据此选择渲染分支：unavailable 绝不渲染「未找到匹配」
        "data_status": data_status,
        # 数据本身的时间（快照 / 旧缓存场景下与 updated 不同）
        "source_updated": meta.get("updated"),
    }


def _to_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ===== 单股快照 + K线 =====
# 2026-09-23 起 push2his 也被 clist 同批限流波及（curl exit 56），故共用同一主机池。
#
# ⚠️ 2026-09-25 修正：**K 线主机池不得包含 push2test**。
# 该主机是「测试」节点，实测（见 docs/K线数据异常定位报告与修复方案.md）：
#   · 只对最近约 4 个交易日返回真实数据，更早的历史是随机占位样本；
#   · 返回的 K 线窗口对所有个股固定为 2026-02-27~2026-09-24（与上市日期无关），
#     行数 10~104 根，大量行满足 o=c=h=l 且成交量个位数（与真实值差 4 个数量级）；
#   · 它之所以被选中，是因为 fetch_stocks.ordered_hosts 的「大页优先」把它排到了首位
#     ——那条优化只对 clist 分页成立，对 K 线毫无意义（故此处同时传 prefer_big_page=False）。
# push2his 才是历史 K 线正解（返回全历史、0 跳变）；push2delay/push2 不支持 kline（返回 0 行），
# 保留在池中仅作为「东财整体不可达」时的显式失败通道——它们的空响应会被
# kline_quality_check 拒绝，不会污染结果。
KLINE_HOSTS = [
    "https://push2his.eastmoney.com",
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
]


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
    d, _ = _push2_try(path, timeout=timeout, hosts=hosts)
    return d


# ===== 东方财富行情主机池（集中定义，勿在各调用点再硬编码）=====
# 2026-09-23 实测：东财对 push2* 集群的 **/api/qt/clist/get** 路径按 IP 定向限流——
# push2delay / push2 / 82.push2 / 1.push2 / push2his 全部 curl exit 56（连接重置），
# 而同一时刻 **push2test.eastmoney.com 的 clist 完全可用**（行业 496 / 概念 504 /
# 全市场 5919 条，返回的仍是 BK 代码与同一套 f 字段），且 push2delay 的
# /api/qt/slist/get 仍可用 —— 说明封锁是**按路径**而非整站，也说明「某台主机被封」
# 不等于「这条数据链路死了」。
# 此前 10 个调用点各自硬编码主机列表，导致 push2delay/push2 一被封，股票列表、板块
# 列表、板块成分、个股快照、K线、基本面四项指标同时失灵。现集中于此，改主机只改这里。
PUSH2_HOSTS = [
    "https://push2delay.eastmoney.com",
    "https://push2test.eastmoney.com",
    "https://push2.eastmoney.com",
]


def _push2_try(path, timeout=12, hosts=None, prefer_big_page=True):
    """按「主机健康度」依次尝试，返回 (json, host)；全部失败返回 (None, None)。

    与 fetch_stocks 共用同一套主机冷却表：刚失败过的主机在冷却期内排到末尾（但不剔除），
    避免每个请求都先白撞一遍已封禁的主机——虽然实测失败很快(~0.6s)，但会平白放大
    请求量，而这正是当初被判定为爬虫的诱因。成功即清除冷却，自动回归。

    ``prefer_big_page`` 透传给 ``fetch_stocks.ordered_hosts``：**取历史序列（K线）的
    调用点必须传 False**，否则 push2test 会因「大页」被排到首位，而它的历史 K 线是
    占位样本（2026-09-25 定位，见 docs/K线数据异常定位报告与修复方案.md）。
    """
    for host in fetch_stocks.ordered_hosts(hosts or PUSH2_HOSTS, prefer_big_page=prefer_big_page):
        d = _curl_json(host.rstrip("/") + path, timeout=timeout)
        if d is not None:
            fetch_stocks.mark_host_up(host)
            return d, host
        fetch_stocks.mark_host_down(host)
    return None, None


def _push2_each(hosts=None, prefer_big_page=True):
    """按健康度产出主机，供「需要对响应做额外校验（如截断判定）」的调用点自行遍历。

    约定：调用方判定该主机本次不可用时，应显式 fetch_stocks.mark_host_down(host)；
    可用则 mark_host_up(host)。这样主机冷却表才是**各处共用**的一份事实。
    """
    return fetch_stocks.ordered_hosts(hosts or PUSH2_HOSTS, prefer_big_page=prefer_big_page)


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
    #    主机降级走模块级 PUSH2_HOSTS（含实测可用的 push2test），不再在本函数内另立一份
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
    """东方财富: 历史最全(push2his 返回全历史), 但该主机常被按 IP 限流/封禁(exit 56)。

    注意 prefer_big_page=False: 历史序列不能套用 clist 的「大页主机优先」策略,
    否则 push2test 会排到首位并返回占位样本。
    """
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
    data, _ = _push2_try(path, timeout=20, hosts=KLINE_HOSTS, prefer_big_page=False)
    if not data:
        raise RuntimeError("东方财富K线: 无响应")
    klines = (data.get("data") or {}).get("klines") or []
    if not klines:
        raise RuntimeError("东方财富K线: 返回为空")
    klines = klines[-n:] if len(klines) > n else klines
    dates, candle, vols, amounts = [], [], [], []
    for k in klines:
        p = k.split(",")
        o, c, h, l, v = float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])
        dates.append(p[0][:10])
        candle.append([o, c, l, h])
        vols.append(int(float(v)))
        # f57 = 成交额(元)。东财是唯一提供逐根成交额的源；字段缺失记 None，
        # 由前端决定「用真值」还是「标注为估算」——不在此处编造数据。
        try:
            amounts.append(float(p[6]))
        except (IndexError, ValueError):
            amounts.append(None)
    out = {"dates": dates, "candle": candle, "vols": vols, "fq": "qfq"}
    if amounts and all(a is not None for a in amounts):
        out["amounts"] = amounts          # 真实逐根成交额(元)
    return out


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
    return {"dates": dates, "candle": candle, "vols": vols, "fq": "qfq"}


def _kline_sina(code, klt, n):
    """新浪财经: 最后兜底。

    字段为 {day, open, high, low, close, volume(股)} —— 顺序与东财不同, 且量为「股」需换算成「手」。

    ⚠️ **复权口径不同（2026-09-25 实证）**：本接口返回**不复权**价格，与东财 `fqt=1`
    和腾讯 `qfq` 不是同一口径。证据：42 只抽样中 5 只出现 -26%~-36.6% 的**向下**单日跳空，
    月份集中在 5/6 月分红季，301319 连续三年各一次（年度除权的指纹）；前复权序列不会有这种跳空。
    后果：**除权日会出现假暴跌，涨跌幅不可跨除权日比较**。故此处声明 fq="none"，
    由 _kline_pick 在使用它时给出告警（前端显示"暂不可用/口径存疑"提示条），
    绝不让它在无标注的情况下冒充前复权数据。待办：改造为前复权（需分红数据）或换前复权接口。
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
    return {"dates": dates, "candle": candle, "vols": vols, "fq": "none"}


# K 线数据源降级顺序: 东方财富历史最全但 push2his 常被限流封禁, 失败时依次降级
KLINE_SOURCES = [
    ("eastmoney", _kline_eastmoney),
    ("tencent", _kline_tencent),
    ("sina", _kline_sina),
]

# ===== K 线质量护栏（2026-09-25 新增，见 docs/K线数据异常定位报告与修复方案.md）=====
#
# 背景：K 线曾长期由 push2test 供给，而它只对最近约 4 个交易日返回真实数据、更早的历史
# 是随机占位样本 —— 单日涨跌 ±267%、成交量个位数、时间轴被抽样压扁。当时全链路只有
# 「非空即采纳」，没有任何值域校验，脏数据一路进缓存、进图表。
# 教训：**「能返回」≠「返回正确」**；条数达标 ≠ 内容正确。
#
# 设计取向（与 fetch_stocks.coverage_report 同思路：宁可判不可用，不可展示错的）：
#   · 结构类判据看**整份序列**（等长 / 日期序 / OHLC 自洽）；
#   · 值域类判据只看**前端可视窗口**（最近 KLINE_WINDOW 根）—— 因为高分红老股的
#     前复权长历史段会产出 ≤0 的价格（如 002048 宁波华翔 2006 年前复权价 -2.07），
#     那些根永远不会被显示，不该因此判死整份数据（属 R6，另行决策）。
# 阈值均由 2026-09-25 实测标定（push2his vs 腾讯 qfq，9 只 × 日/周/月）：
#   可视窗口内两源收盘价最大偏差 ≤0.55%（日 0.13 / 周 0.50 / 月 0.55），故 3% 阈值有 5 倍余量；
#   可视窗口内真实单根涨跌最大 日 12.7% / 周 52.1% / 月 66.4%，故各周期上限留足空间。
KLINE_WINDOW = 120                          # 值域校验窗口 = 前端可视窗口根数
KLINE_JUMP_LIMIT = {101: 21.5, 102: 80.0, 103: 150.0}   # 单根涨跌幅上限(%)
KLINE_JUMP_MAX_COUNT = 3                    # 窗口内超上限根数 ≥ 此值 -> 判"两档价位混排"
KLINE_GAP_LIMIT = {101: 15, 102: 25, 103: 70}           # 相邻根日期最大间隔(自然日)
KLINE_GAP_MAX_COUNT = 3                     # 窗口内超上限间隔 ≥ 此值 -> 判"抽样缺根"
KLINE_XCHECK_MAX_DEV = 1.5                  # 跨源单根收盘价偏差上限(%，实测最大 0.55%)
KLINE_XCHECK_MAX_RATIO = 0.10               # 跨源不一致根数占比上限
KLINE_XCHECK_MIN_BARS = 5                   # 可比根数下限, 低于此值不做跨源判定
KLINE_LEN_RATIO_MIN = 0.8                   # 相对对照源的行数比下限(同一标的同一周期)
KLINE_RACE_TIMEOUT = 12                     # 并行竞速总等待(秒)
KLINE_PEER_GRACE = 2.5                      # 首源返回后, 再等对照源的宽限(秒)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _kline_days_between(a, b):
    """两个 YYYY-MM-DD 之间的自然日差；格式非法时返回 0（交由日期格式判据拦）。"""
    try:
        da = datetime.date(int(a[0:4]), int(a[5:7]), int(a[8:10]))
        db = datetime.date(int(b[0:4]), int(b[5:7]), int(b[8:10]))
        return abs((db - da).days)
    except Exception:
        return 0


def kline_quality_check(data, klt, n, peer=None):
    """K 线响应完整性/值域校验。返回 (ok: bool, detail: dict)。

    detail 分三档，语义严格区分：
      reason   —— 不通过原因（整份丢弃，不采纳、不落缓存）
      warnings —— 通过但需提示（会让接口返回 data_status=degraded）
      notes    —— 仅供记录/排查，**不影响 data_status**（如可视窗口之外的历史伪影）

    ``peer`` 为对照源(另一数据源同一 code/klt 的结果)；给定且可比时做跨源交叉校验
    —— 这是唯一能抓住「内部自洽但整体错」那一类污染的手段（占位样本在某些个股上
    恰好跳变为 0，单看形态查不出来）。
    """
    detail = {"reason": "", "warnings": [], "notes": [], "checked": 0,
              "jumps": 0, "gaps": 0, "xcheck": "skipped", "neg_outside": 0}

    if not isinstance(data, dict):
        detail["reason"] = "响应结构异常"
        return False, detail
    dates = data.get("dates") or []
    candle = data.get("candle") or []
    vols = data.get("vols") or []
    if not dates:
        detail["reason"] = "返回为空"
        return False, detail
    if not (len(dates) == len(candle) == len(vols)):
        detail["reason"] = "长度不齐 dates=%d candle=%d vols=%d" % (len(dates), len(candle), len(vols))
        return False, detail

    # --- 结构类：整份序列 ---
    prev_date = None
    for i, dt in enumerate(dates):
        if not _DATE_RE.match(str(dt)):
            detail["reason"] = "日期格式非法: %r" % (dt,)
            return False, detail
        if prev_date is not None and str(dt) <= prev_date:
            detail["reason"] = "日期非严格递增: %s -> %s" % (prev_date, dt)
            return False, detail
        prev_date = str(dt)
        cd = candle[i]
        if not (isinstance(cd, (list, tuple)) and len(cd) == 4):
            detail["reason"] = "第 %d 根 OHLC 结构异常: %r" % (i + 1, cd)
            return False, detail
        try:
            o, c, lo, hi = (float(cd[0]), float(cd[1]), float(cd[2]), float(cd[3]))
        except (TypeError, ValueError):
            detail["reason"] = "第 %d 根 OHLC 含非数值: %r" % (i + 1, cd)
            return False, detail
        if hi < lo or hi < max(o, c) or lo > min(o, c):
            detail["reason"] = "%s OHLC 自相矛盾 o=%s c=%s lo=%s hi=%s" % (dt, o, c, lo, hi)
            return False, detail

    # --- 值域类：仅可视窗口 ---
    s = max(0, len(dates) - KLINE_WINDOW)
    win = list(zip(dates[s:], candle[s:]))
    detail["checked"] = len(win)

    for dt, cd in win:
        if min(float(cd[0]), float(cd[1]), float(cd[2]), float(cd[3])) <= 0:
            detail["reason"] = "%s 可视窗口内出现非正价格 %r" % (dt, cd)
            return False, detail
    if s > 0:
        detail["neg_outside"] = sum(1 for cd in candle[:s]
                                    if min(float(cd[0]), float(cd[1]), float(cd[2]), float(cd[3])) <= 0)
        if detail["neg_outside"]:
            detail["notes"].append(
                "可视窗口之外有 %d 根非正价格（前复权长历史伪影，未参与展示）" % detail["neg_outside"])

    jump_limit = KLINE_JUMP_LIMIT.get(_to_int(klt, 101), KLINE_JUMP_LIMIT[101])
    gap_limit = KLINE_GAP_LIMIT.get(_to_int(klt, 101), KLINE_GAP_LIMIT[101])
    prev_close = None
    prev_date = None
    for dt, cd in win:
        c = float(cd[1])
        if prev_close:
            if abs((c - prev_close) / prev_close * 100) > jump_limit:
                detail["jumps"] += 1
        if prev_date is not None and _kline_days_between(prev_date, dt) > gap_limit:
            detail["gaps"] += 1
        prev_close = c
        prev_date = dt
    if detail["jumps"] >= KLINE_JUMP_MAX_COUNT:
        detail["reason"] = ("可视窗口内 %d 根单根涨跌超 %.1f%%，判定为两档价位混排"
                            % (detail["jumps"], jump_limit))
        return False, detail
    # 「缺根」降级为提示而不否决：长期停牌股（如 002450）在周/月 K 上会有数处超长间隔，
    # 那是真实停牌而非数据缺陷，用否决会把它错判成「不可用」。
    if detail["gaps"] >= KLINE_GAP_MAX_COUNT:
        detail["warnings"].append(
            "可视窗口内 %d 处相邻根间隔超 %d 天（可能为长期停牌，也可能是数据抽样缺根）"
            % (detail["gaps"], gap_limit))

    # --- 跨源一致性（可选，需对照源）---
    if peer:
        ok, xd = kline_xcheck(data, peer, klt, n)
        detail.update({k: v for k, v in xd.items() if k != "warnings"})
        detail["warnings"].extend(xd.get("warnings") or [])
        if not ok:
            return False, detail

    return True, detail


def kline_xcheck(data, peer, klt, n):
    """跨源交叉校验：同一标的两源在**可视窗口**内的收盘价应高度一致。

    返回 (ok, {"xcheck": 描述, "warnings": [...], "notes": [...]})。

    只看可视窗口是关键（2026-09-25 实测踩到）：高分红老股的前复权长历史段价格趋于 0，
    同源两家的 0.02 vs 0.20 就能算出 900% 的「偏差」，若全序列比对会把正常数据判死。
    另含「行数相对对照源过少」判据：同一 code/klt 下两源截断后的行数**实测恒为 1:1**
    （2026-09-25 标定 11 只 × 3 周期全部 1.000），占位样本只给 10~104 根而真实源给数百根，
    差距过大即视为异常。该判据用全序列长度，不受窗口影响。
    """
    out = {"xcheck": "skipped", "warnings": [], "notes": []}
    if not peer or not peer.get("dates"):
        return True, out

    la, lb = len(data.get("dates") or []), len(peer.get("dates") or [])
    if max(la, lb) >= 20 and min(la, lb) < KLINE_LEN_RATIO_MIN * max(la, lb):
        out["xcheck"] = "行数异常"
        out["warnings"].append("行数 %d 相对对照源 %d 过少" % (la, lb))
        return False, out

    ta = list(zip(data.get("dates") or [], data.get("candle") or []))[-KLINE_WINDOW:]
    peer_close = {d: c[1] for d, c in zip(peer.get("dates") or [], peer.get("candle") or [])}
    common, mismatch = 0, 0
    for dt, cd in ta:
        c = float(cd[1])
        pc = peer_close.get(dt)
        if pc is None or c <= 0 or pc <= 0:
            continue
        common += 1
        if abs(c - pc) / pc * 100 > KLINE_XCHECK_MAX_DEV:
            mismatch += 1

    if common < KLINE_XCHECK_MIN_BARS:
        out["xcheck"] = "insufficient"
        out["notes"].append("跨源可比根数仅 %d 根，未做一致性判定" % common)
        return True, out
    ratio = mismatch / common
    out["xcheck"] = "%d/%d 根偏差>%.1f%%" % (mismatch, common, KLINE_XCHECK_MAX_DEV)
    if ratio > KLINE_XCHECK_MAX_RATIO:
        out["warnings"].append("与对照源收盘价偏差超阈值的根数占比 %.0f%%" % (ratio * 100))
        return False, out
    return True, out


# K线结果短缓存: 详情页每次进页/切周期都会请求, 而上游单次 1-2s 且常被限流。
# 历史 K 线当日不变, 只有最后一根随盘口变动 —— 60s TTL 下最后一根最多滞后 1 分钟,
# 对图表展示可接受(若需盘口实时, 走行情快照接口而非 K 线)。
# ⚠️ 只有**通过 kline_quality_check 的数据**才允许写入；读取命中时再复验一次
#    （双保险：阈值或判据将来收紧后，旧缓存不会继续流出）。
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


def _kline_cache_clear():
    """清空 K 线缓存（测试与运维用）。"""
    with _KLINE_LOCK:
        _KLINE_CACHE.clear()


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


_KLINE_PICK_ORDER = ("eastmoney", "tencent", "sina")   # 同档优先序: 历史最全者在前
KLINE_FQ_LABEL = {"qfq": "前复权", "none": "不复权", "unknown": "口径未验证"}


def _kline_tag_fq(d):
    """给选中结果补上复权口径，并在**口径与主链不一致**时给出告警。

    只做声明与提示，不改数值 —— 混用不同口径又不说，正是 N-15 那类"静默给出错数据"
    的同族问题（新浪为不复权，见 _kline_sina 注释的实证）。
    """
    q = d.setdefault("quality", {})
    q.setdefault("warnings", [])
    fq = d.get("fq") or "unknown"
    q["fq"] = fq
    if fq != "qfq":
        q["warnings"].append(
            "当前数据源为%s：除权日会出现价格跳空，涨跌幅不可跨除权日比较"
            % KLINE_FQ_LABEL.get(fq, fq))
    return d


def _kline_race(code, klt, n):
    """东财 + 腾讯 并行取数，返回 {name: data|None}。

    与旧实现的关键差别：**不再"首个非空者胜"**。脏源（如 push2test）响应体更小、
    返回更快，竞速下必然先到 —— 竞速本身放大了污染。这里改为「收齐两源后统一校验」：
    首源返回后再宽限 KLINE_PEER_GRACE 秒等对照源（跨源交叉校验需要它）；
    对照源仍未到则以单源结构校验为准（不因等待而无限拖长首屏）。
    """
    def _one(name, fn):
        try:
            d = fn(code, klt, n)
            return d if (d and d.get("dates")) else None
        except Exception:
            return None

    ex = ThreadPoolExecutor(max_workers=2)
    try:
        futs = {ex.submit(_one, name, fn): name
                for name, fn in (("eastmoney", _kline_eastmoney), ("tencent", _kline_tencent))}
        done, pending = wait(futs, timeout=KLINE_RACE_TIMEOUT, return_when=FIRST_COMPLETED)
        if pending:
            done2, _ = wait(pending, timeout=KLINE_PEER_GRACE)
            done |= done2
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    out = {}
    for fut in done:
        name = futs[fut]
        try:
            out[name] = fut.result()
        except Exception:
            out[name] = None
    return out


def _kline_pick(cands, klt, n):
    """从各源候选中挑出**通过校验**的一份；返回 (data|None, {name: detail})。

    流程：① 各自过结构/值域校验；② 结构通过者两两互为对照做跨源校验，
    只在「与至少一个其它源一致」时才被确认（完全互不一致则全部不确认 ——
    此时无法判断谁对，宁可判不可用）；③ 按 _KLINE_PICK_ORDER 取优先级最高的确认者。
    仅有一个源可用时跳过第 ② 步（结构校验仍生效），并在 quality 里标注。
    """
    detail = {}
    passed = {}
    for name, d in (cands or {}).items():
        if not d:
            continue
        ok, det = kline_quality_check(d, klt, n)
        detail[name] = det
        if ok:
            passed[name] = d

    # 方向性判据（2026-09-25 加）：结构判据为了兼容复牌股，对"单根不可能跳变"只按计数拒
    # （≥3）。但若同期存在一个**没有该跳变**的对照源，错值就有了归属 —— 直接剔除带跳变的那一方。
    # 真实的复牌跳空两家都会有，不会误伤；只有单边出现的异常才被剔除。
    if len(passed) >= 2:
        for name in list(passed):
            others = [detail[o]["jumps"] for o in passed if o != name]
            if detail[name]["jumps"] >= 1 and others and all(j == 0 for j in others):
                detail[name]["warnings"].append(
                    "可视窗口内存在对照源没有的异常跳变（%d 根）" % detail[name]["jumps"])
                passed.pop(name, None)

    if len(passed) >= 2:
        # 先做快照再判定：判定结果统一在最后剔除，避免「边遍历边删除」导致取到已删除的键。
        names = sorted(passed)
        snapshot = {k: passed[k] for k in names}
        unconfirmed = []
        for name in names:
            others = [snapshot[o] for o in names if o != name]
            confirmed = False
            for peer in others:
                ok, xd = kline_xcheck(snapshot[name], peer, klt, n)
                detail[name].update({k: v for k, v in xd.items() if k not in ("warnings", "notes")})
                detail[name]["warnings"].extend(xd.get("warnings") or [])
                detail[name]["notes"].extend(xd.get("notes") or [])
                if ok:
                    confirmed = True
                    break
            if not confirmed:
                unconfirmed.append(name)     # 与任何一个对照源都不一致 -> 剔除
        for name in unconfirmed:
            passed.pop(name, None)

    for name in _KLINE_PICK_ORDER:
        if name in passed:
            d = passed[name]
            d["source"] = name
            d["quality"] = detail[name]
            return _kline_tag_fq(d), detail
    return None, detail


def _kline_reason(det):
    """把校验 detail 压成一句可读原因（供不可用态的错误文案用）。

    注意区分三种"没被选中"：明确否决(reason) / 通过但一致性存疑(warnings) /
    压根没返回。早期实现只读 reason，导致跨源被剔除的源显示成"未返回"，误导排查。
    """
    det = det or {}
    if det.get("reason"):
        return det["reason"]
    if det.get("warnings"):
        return "；".join(det["warnings"])
    if det.get("notes"):
        return "；".join(det["notes"])
    return "未返回"


def fetch_kline(code, klt, n):
    """拉取真实 K 线: 返回 {dates, candle:[o,c,h], vols[, amounts], source, quality}。

    优先命中 60s 缓存（**缓存里只可能存校验通过的数据**）。
    未命中时：东财 + 腾讯并行取数 → 双源交叉校验 → 取优先级最高且通过校验者；
    仍无可用源再按 KLINE_SOURCES 串行兜底（含新浪，与已到源互为对照）。
    全部不可用或全部未通过校验则抛 MarketSourceUnavailable ——
    调用方据此返回 data_status=unavailable，**不做任何数据合成**。
    """
    key = (code, klt, n)
    hit = _kline_cache_get(key)
    if hit is not None:
        ok, _det = kline_quality_check(hit, klt, n)   # 复验：判据收紧后旧缓存不再流出
        if ok:
            return hit
        _kline_cache_clear()

    cands = _kline_race(code, klt, n)
    picked, detail = _kline_pick(cands, klt, n)
    if picked is None:
        # 串行兜底：新浪。与已到的东财/腾讯互为对照，走同一套挑选逻辑。
        try:
            d = _kline_sina(code, klt, n)
            if d and d.get("dates"):
                cands["sina"] = d
                picked, detail = _kline_pick(cands, klt, n)
        except Exception:
            pass
    if picked is None:
        reasons = "; ".join("%s: %s" % (k, _kline_reason(v))
                            for k, v in sorted(detail.items())) or "无任何数据源返回"
        raise fetch_stocks.MarketSourceUnavailable("K线数据源不可用或未通过校验 —— " + reasons)

    _kline_cache_put(key, picked)
    return picked


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
    today = _trading_day()          # 交易日键，与行情缓存口径一致
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
    today = _trading_day()          # 交易日键，周末/节假日复用同一份清单
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
            for host in _push2_each():
                d = _curl_json(host + "/api/qt/clist/get?" + q, timeout=12)
                if not d:
                    fetch_stocks.mark_host_down(host)
                    continue
                data = d.get("data") or {}
                diff = data.get("diff") or []
                t = data.get("total")
                # 截断页(条数明显少于 total 应有的一页)→ 换主机重试
                if diff and (not t or len(diff) >= min(100, int(t))):
                    fetch_stocks.mark_host_up(host)
                    break
                fetch_stocks.mark_host_down(host)   # 截断主机同样视为本次不可用
                d = None
            if not d:
                break  # 所有主机都失败, 用已拿到的部分(不缓存, 见外层)
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
    today = _trading_day()
    with _STOCK_BOARDS_LOCK:
        hit = _STOCK_BOARDS_CACHE.get(code)
        if hit and hit[0] == today:
            return (hit[1], 200)
    q = urllib.parse.urlencode({"secid": secid, "spt": "3", "fields": "f12,f14",
                                "pn": "1", "pz": "200", "np": "1", "fltt": "2", "invt": "2"})
    out = {"code": code, "industry": [], "concept": []}
    # slist 在被限流的主机上会返回半截数据(正常个股关联板块有 20~40 个):
    #   - 主机顺序统一走 _push2_each()（含 push2test 与主机冷却）;
    #   - 用响应自带 total 校验完整性, len(diff)<total 视为截断, 换主机/重试。
    for attempt in range(3):
        d = None
        for host in _push2_each():
            d = _curl_json(host + "/api/qt/slist/get?" + q, timeout=12)
            if d is not None:
                diff = ((d or {}).get("data") or {}).get("diff") or []
                total = ((d or {}).get("data") or {}).get("total") or 0
                if diff and len(diff) >= total:
                    fetch_stocks.mark_host_up(host)
                    break
                fetch_stocks.mark_host_down(host)   # 截断响应, 试下一个主机
                d = None
            else:
                fetch_stocks.mark_host_down(host)
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
    today = _trading_day()
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
    d, _ = _push2_try("/api/qt/clist/get?" + q, timeout=12)
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


def _fetch_board_records_live(kind, today):
    """拉取某类型板块的全量行情。返回 (records, complete)。

    complete=False 表示**取数失败或清单残缺**（不等于「板块为空」）——
    这是与「股票页空列表」同类的风险点：上游被限流时旧实现静默返回 []，
    上层便把它当成「今天没有板块」返回 HTTP 200 + total=0。
    """
    fs = _BOARD_QUOTE_FS.get(kind)
    if not fs:
        return [], False
    base = {"fs": fs, "fields": "f12,f14,f2,f3,f104,f105,f128,f136,f20",
            "po": "1", "np": "1", "fltt": "2", "invt": "2", "fid": "f3"}
    out, complete = [], False
    # 最多 3 轮拉取: 中途某页所有主机都失败会得到残缺清单, 必须拉全(total)才缓存,
    # 否则「半导体」等板块会整天缺失(与 _board_class_lists 同范式)。
    for attempt in range(3):
        # 全部主机都在冷却 → 整条链路刚被拒, 再重试只是白打请求(既慢又更像爬虫)
        if attempt and fetch_stocks.all_hosts_cooling():
            print("[info] 板块行情主机全部冷却中, 跳过本轮重试(kind=%s)" % kind)
            break
        out, pn, total = [], 1, None
        # 标准分页拉全: 部分主机把 pz 钳制在 100
        while pn <= 30:
            q = dict(base)
            q["pn"] = str(pn)
            q["pz"] = "100"
            d = None
            # 单页重试: 两轮 × 全部主机; 末页(累计达 total)同样放行, 避免末页被误判截断
            for _round in range(2):
                for host in _push2_each():
                    d = _curl_json(host + "/api/qt/clist/get?" + urllib.parse.urlencode(q), timeout=12)
                    if not d:
                        fetch_stocks.mark_host_down(host)
                        continue
                    data = d.get("data") or {}
                    diff = data.get("diff") or []
                    t = data.get("total")
                    if diff and (not t or len(diff) >= 100 or len(out) + len(diff) >= int(t)):
                        fetch_stocks.mark_host_up(host)
                        break
                    fetch_stocks.mark_host_down(host)   # 截断页, 换主机
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
        _save_board_disk_cache(kind, out, today)
    return out, complete


def get_board_data(kind):
    """返回 (records, data_status, meta)。与 get_market_data() 保持同一套语义。

    data_status: "ok" 实时取到 / "stale" 实时失败但回退到近期落盘缓存 /
                 "unavailable" 全失败（**绝不是「没有板块」**）
    meta["updated"] 是**数据自身的抓取时间**（stale 时前端必须据此标注），
    不是响应生成时间。

    降级顺序与全市场行情一致（见 get_market_data 的说明）：内存当日缓存 →
    落盘当日缓存 → 东财实时 → 落盘旧数据 → unavailable。
    「空列表永不写入任何缓存」这条铁律在板块上同样适用。
    """
    today = _trading_day()
    with _BOARD_QUOTE_LOCK:
        hit = _BOARD_QUOTE_CACHE.get(kind)
        if hit and hit[0] == today:
            return hit[1], "ok", {"updated": _now_str(), "layer": "memory"}

    # ---- 落盘缓存：同一交易日直接复用，启动时不再打十几次 clist ----
    disk, disk_at = _load_board_disk_cache(kind)
    if disk and _day_of(disk_at) == today:
        with _BOARD_QUOTE_LOCK:
            _BOARD_QUOTE_CACHE[kind] = (today, disk)
        print("[cache] 命中板块落盘缓存(kind=%s, %d 个, 抓取于 %s), 跳过实时拉取"
              % (kind, len(disk), disk_at))
        return disk, "ok", {"updated": disk_at, "layer": "disk"}

    if fetch_stocks.all_hosts_cooling():
        # 上游刚被整体拒掉：此时再去打一次实时源，用户要等十几秒才看到「不可用」。
        # 直接用降级数据即可 —— 冷却是 90s 的，过后自动恢复尝试。
        print("[info] 板块行情主机全部冷却中, 跳过实时拉取(kind=%s)" % kind)
    else:
        recs, complete = _fetch_board_records_live(kind, today)
        if complete and recs:
            return recs, "ok", {"updated": _now_str(), "layer": "live"}

    if disk:
        print("[warn] 板块实时取数失败, 回退落盘缓存(kind=%s, %d 个, %s)"
              % (kind, len(disk), disk_at))
        return disk, "stale", {"updated": disk_at, "layer": "disk-stale",
                               "reason": "实时行情源不可用，展示最近一次成功抓取的清单"}

    print("[error] 板块行情源不可用(kind=%s): 实时与缓存均无数据" % kind)
    return [], "unavailable", {"reason": "板块行情源不可用（实时与缓存均无数据）"}


def get_board_records(kind):
    """兼容入口：只取记录列表（板块名称反查等调用方沿用）。

    需要区分「不可用」与「真空」的调用方，请改用 get_board_data()。
    """
    return get_board_data(kind)[0]


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
    today = _trading_day()          # 交易日键（周末/节假日回退），与全市场行情口径一致
    hit = _BOARD_TOTAL_CACHE.get(kind)
    return hit[1] if (hit and hit[0] == today) else None


def _fetch_board_block(fs, fid, po, pn, tries=2):
    """拉单个 100 条块(按主机健康度轮换 + 重试), 返回 (items, total) 或 (None, None)。"""
    q = urllib.parse.urlencode({"fs": fs, "fields": "f12,f14,f2,f3,f104,f105,f128,f136,f20",
                                "pn": str(pn), "pz": "100", "po": po, "np": "1",
                                "fltt": "2", "invt": "2", "fid": fid})
    for t in range(tries):
        if t and fetch_stocks.all_hosts_cooling():
            break                    # 全部主机冷却中, 第二次尝试只是白打请求
        for host in _push2_each():
            d = _curl_json(host + "/api/qt/clist/get?" + q, timeout=10)
            if not d:
                fetch_stocks.mark_host_down(host)
                continue
            data = d.get("data") or {}
            diff = data.get("diff") or []
            if not diff:
                # 空 diff 是「这一块确实没有数据」，不是传输失败，不记主机故障
                continue
            items = [r for r in (_board_rec_from_row(v) for v in diff) if r]
            if not items:
                continue
            fetch_stocks.mark_host_up(host)
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
    # 交易日键：周末/节假日回退到最近交易日，避免休市日重复打上游
    # （与全市场行情的 _trading_day 口径一致，见 D 阶段说明）
    today = _trading_day()
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

    # data_status 的意义：让前端不必猜测 total=0 到底是「没有这个板块」还是「取数失败」。
    # 背景（2026-09-23）：东财对 push2* 的 clist 路径定向限流，get_board_records 静默返回 [],
    # 本函数便以 HTTP 200 + total=0 响应，前端渲染出「未找到匹配「」的板块」——
    # 与股票页那次事故是同一个错误类别，用户无法区分「故障」与「真的没有」。
    data_status = "ok"
    data_meta = {}
    if q:
        # 关键词搜索必须全量匹配(命中全量当日缓存时无外网开销)
        all_recs, data_status, data_meta = get_board_data(kind)
        recs = [r for r in all_recs if q in r["name"].lower()
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
            all_recs, data_status, data_meta = get_board_data(kind)
            recs = sorted(all_recs, key=sort_key, reverse=(order == "desc"))
            total = len(recs) or (total or 0)
            start = (page - 1) * pageSize
            page_items = recs[start:start + pageSize]
        elif not total:
            total = _board_total_cached(kind) or len(page_items)

    return {
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_status": data_status,
        # 数据自身的抓取时间：stale 时前端必须用**这个**标注，
        # 用 updated（响应生成时间）会把「上一交易日的清单」标成刚刚抓的。
        "data_updated": data_meta.get("updated"),
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


def get_board_members_data(bk):
    """返回 (records, data_status)。data_status: ok / unavailable。

    与板块列表同一个道理：上游 clist 被限流时旧实现静默返回 []，上层便把它当成
    「该板块暂无成分股」——用户会以为这个板块是空的。此处显式区分：
      - total=0 且响应正常  → ("ok", 空列表) 确实是空板块
      - 全部主机都取不到    → ("unavailable", 空列表) 明确是故障
    """
    today = _trading_day()
    with _BOARD_MEMBERS_LOCK:
        hit = _BOARD_MEMBERS_CACHE.get(bk)
        if hit and hit[0] == today:
            return hit[1], "ok"
    base = {"fs": "b:" + bk, "fields": _BOARD_MEMBERS_FIELDS,
            "po": "1", "np": "1", "fltt": "2", "invt": "2", "fid": "f20"}
    rows, complete, got_any = [], False, False
    # 与 get_board_records 同范式: 分页拉全才缓存; 单页两轮 × 按健康度轮换主机重试
    for attempt in range(3):
        if attempt and fetch_stocks.all_hosts_cooling():
            print("[info] 板块行情主机全部冷却中, 跳过本轮重试(bk=%s)" % bk)
            break
        rows, pn, total = [], 1, None
        while pn <= 100:
            q = dict(base)
            q["pn"] = str(pn)
            q["pz"] = "100"
            d = None
            for _round in range(2):
                for host in _push2_each():
                    d = _curl_json(host + "/api/qt/clist/get?" + urllib.parse.urlencode(q), timeout=12)
                    if not d:
                        fetch_stocks.mark_host_down(host)
                        continue
                    data = d.get("data") or {}
                    diff = data.get("diff") or []
                    t = data.get("total")
                    if diff and (not t or len(diff) >= 100 or len(rows) + len(diff) >= int(t)):
                        fetch_stocks.mark_host_up(host)
                        break
                    fetch_stocks.mark_host_down(host)   # 截断页, 换主机
                    d = None
                if d:
                    break
                time.sleep(0.8)
            if not d:
                break
            got_any = True     # 至少有一页真的取到了响应
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
        return records, "ok"
    if got_any or complete:
        return records, "ok"
    print("[error] 板块成分股取数失败(bk=%s): 所有主机均无响应" % bk)
    return [], "unavailable"


def get_board_members(bk):
    """兼容入口：只取记录列表。需要区分「不可用」与「确实是空板块」请用
    get_board_members_data()。"""
    return get_board_members_data(bk)[0]


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

    # data_status 让前端不必猜测「total=0」的含义：
    #   unavailable → 行情源故障（**不是**「这个板块没有成分股」），前端必须换文案
    recs, data_status = get_board_members_data(bk)
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
        "data_status": data_status,
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
    """K 线接口。返回体带 data_status（与行情链统一的词表），前端据此分流：
      ok          — 校验通过
      degraded    — 校验通过但带提示（如可视窗口之外存在前复权非正价、跨源可比根数不足）
      unavailable — 全部数据源不可用或未通过校验
    契约变更（2026-09-25）：三种情形**一律 200**，不可用态由 data_status 表达而非
    5xx —— 与 get_market_data/handle_api_stocks 的既有约定一致，避免前端把
    「数据不可用」误当作「请求失败」而走进兜底分支。**任何情形都不合成数据。**
    """
    code = (params.get("code") or [""])[0].strip()
    if not code:
        return ({"error": "缺少 code 参数", "data_status": "unavailable"}, 400)
    klt = _to_int(params.get("klt", ["101"])[0], 101)
    n = _to_int(params.get("n", ["120"])[0], 120)
    try:
        d = fetch_kline(code, klt, n)
    except fetch_stocks.MarketSourceUnavailable as e:
        return ({"error": "K线数据源不可用: %s" % e, "data_status": "unavailable",
                 "source": None, "quality": None,
                 "dates": [], "candle": [], "vols": []}, 200)
    except Exception as e:
        return ({"error": "K线获取失败: %s" % e, "data_status": "unavailable",
                 "source": None, "quality": None,
                 "dates": [], "candle": [], "vols": []}, 200)
    q = d.get("quality") or {}
    d["data_status"] = "degraded" if q.get("warnings") else "ok"
    d["data_updated"] = d["dates"][-1] if d.get("dates") else None
    return (d, 200)


# ===== 自选股行情拼装（F-03）=====
#
# 职责划分（方案 §2.1 / §4.3）：
#   自选的**存储与身份**在 8002（auth.db 与用户身份只在那里），
#   本文件只负责把「代码列表」拼成「行情列表」——因为全部行情取数能力都在这一侧。
#
# 主路径：复用 get_records() 的交易日全市场内存缓存，按代码过滤。
#   get_records() 返回的 record 字段与 stock.html::buildRow(d) 所需字段完全同构
#   （见 fetch_stocks.fetch_market_records），因此前端可直接复用同一套行渲染，
#   且**零额外外部请求**。
#
# 降级路径：get_records() 为空时（此前「股票页空列表」的同类故障），改为逐只调用
#   push2 单股快照接口（独立于 clist 全市场接口的另一条通道）拼最小行情，
#   而不是把「行情源异常」误报成「自选为空」。

# f57=代码 f58=名称 f43=最新价 f170=涨跌幅(%) f169=涨跌额 f20=总市值 f21=流通市值 f100=行业
WATCH_QUOTE_FIELDS = "f57,f58,f43,f170,f169,f20,f21,f100"

# 自选行情单次拼装的降级上限：超过该数量的部分不再逐只取价（避免拖垮请求）
WATCH_DEGRADE_MAX = 60


def _watchlist_from_report_api(cookie_header):
    """经 8002 /api/watchlist 取当前用户自选条目。

    返回 (items, error_status)：成功时 error_status 为 None；
    未登录 401 / 服务不可达 502 原样透传，避免把「没登录」表现成「自选为空」。
    """
    req = urllib.request.Request(REPORT_API_BASE + "/api/watchlist")
    if cookie_header:
        req.add_header("Cookie", cookie_header)
    try:
        # 与 _proxy_report 同口径：本机回环必须绕过 HTTP_PROXY，否则会被代理拦成 502
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return (payload.get("items") or []), None
    except urllib.error.HTTPError as e:
        return [], e.code
    except Exception:
        return [], 502


def _quote_only_record(code, name_fallback=""):
    """降级路径：push2 单股快照拼一条最小 record（字段名与全市场 record 保持一致）。

    取不到行情时返回 None（由调用方决定如何展示），绝不编造价格。
    """
    q = urllib.parse.urlencode({"secid": _secid(code), "fields": WATCH_QUOTE_FIELDS,
                                "invt": "2", "fltt": "2"})
    try:
        d, _host = _push2_try("/api/qt/stock/get?" + q, timeout=4)
    except Exception:
        return None
    row = (d or {}).get("data") or {}
    if not row:
        return None
    pv = _to_num(row.get("f43"))
    cv = _to_num(row.get("f170"))
    mcap, fcap = _to_num(row.get("f20")), _to_num(row.get("f21"))
    if cv is None:
        chg_str, cv0 = "--", 0.0
    else:
        chg_str, cv0 = (("+" if cv > 0 else "") + ("%.2f" % cv) + "%"), cv
    # f100/f20/f21 是 clist 全市场接口的字段，单股接口通常不返回 —— 缺了就留空，
    # 不伪造："—"/"--" 会被误读成「真实值就是空」，留空则由前端不渲染该列。
    ind_raw, name_raw = row.get("f100"), (row.get("f58") or name_fallback or "")
    return {
        "name": name_raw.strip(),
        "code": row.get("f57") or code,
        "ind": fetch_stocks.norm_industry(ind_raw) if ind_raw else "",
        "price": ("¥%.2f" % pv) if pv is not None else "--",
        "chg": chg_str,
        "chg_num": cv0,
        "dir": "up" if cv0 > 0 else ("down" if cv0 < 0 else "flat"),
        "mcap": mcap,
        "mcap_disp": fetch_stocks.fmt_mcap(mcap) if mcap is not None else "",
        "fcap": fcap,
        "fcap_disp": fetch_stocks.fmt_mcap(fcap) if fcap is not None else "",
    }


def handle_api_watchlist_quotes(qs, cookie_header):
    """GET /api/watchlist_quotes -> 自选标的行情（结构与 /api/stocks 对齐）。

    响应额外带 `data_status`，让前端无需猜测空列表的含义：
      - "ok"          正常（可能含个别取不到行情的标的，行内以 "--" 体现）
      - "empty"       自选确实为空
      - "unavailable" 自选非空但行情源不可用（**不是**"未找到"）

    另带 `market_status` / `market_updated`：本页行情来自哪个层级的市场数据
    （ok/backup/stale/snapshot）。它独立于 data_status —— 即使自选本身正常，
    行情也可能来自离线快照，必须让前端能标注，避免把旧价当实时价展示。
    """
    items, err = _watchlist_from_report_api(cookie_header)
    if err is not None:
        # 401 原样透传；502 表示报告/认证服务不可达
        return ({"error": "未登录或会话已过期" if err == 401 else "自选服务不可达",
                 "data_status": "unavailable"}, err)
    if not items:
        return ({"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "total": 0, "page": 1, "pageSize": 0, "stocks": [],
                 "data_status": "empty"}, 200)

    # 手工排序：排序字段在前端做（records 的 chg 为展示字符串，服务端排序口径不适合复用）
    want = [str(i.get("code") or "") for i in items]
    name_map = {str(i.get("code") or ""): (i.get("name") or "") for i in items}

    recs, mkt_status, mkt_meta = get_market_data()
    mkt_info = {"market_status": mkt_status, "market_updated": mkt_meta.get("updated")}
    by_code = {}
    for r in recs:
        c = str(r.get("code") or "")
        if c:
            by_code[c] = r

    rows, missing = [], []
    for c in want:
        r = by_code.get(c)
        if r is not None:
            row = dict(r)
            # 行情返回的名称优先（快照可能过期：改名 / 加 ST）；快照仅作兜底
            if not row.get("name"):
                row["name"] = name_map.get(c, "")
            rows.append(row)
        else:
            missing.append(c)

    if rows:
        resp = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "total": len(rows), "page": 1, "pageSize": len(rows),
                "stocks": rows, "data_status": "ok"}
        resp.update(mkt_info)
        return (resp, 200)

    # 一条都没拼出来 -> 行情源异常，走降级：逐只取单股快照
    for c in missing[:WATCH_DEGRADE_MAX]:
        rec = _quote_only_record(c, name_map.get(c, ""))
        if rec is not None:
            rows.append(rec)
    if rows:
        resp = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "total": len(rows), "page": 1, "pageSize": len(rows),
                "stocks": rows, "data_status": "degraded"}
        resp.update(mkt_info)
        return (resp, 200)

    # 降级也拿不到 -> 明确告知「行情不可用」，绝不复用「未找到匹配」文案
    return ({"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
             "total": 0, "page": 1, "pageSize": 0, "stocks": [],
             "data_status": "unavailable"}, 200)


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

        if parsed.path == "/api/watchlist_quotes":
            # 自选行情拼装：以本侧 get_records() 为主源，含降级与 data_status 标识。
            # 该路径非公开 -> 上面的登录态拦截已保证此处必然是有效会话。
            try:
                obj, status = handle_api_watchlist_quotes(
                    urllib.parse.parse_qs(parsed.query), self.headers.get("Cookie"))
                self._send_json(obj, status)
            except Exception as e:
                self._send_json({"error": str(e), "data_status": "unavailable"}, status=500)
            return

        if parsed.path.startswith("/api/watchlist"):
            # 自选存储（F-03）：同源代理到 8002（auth.db 与用户身份只在那里）。
            if not self._session_ok():
                self._send_json({"detail": "未登录或会话已过期"}, status=401)
                return
            self._proxy_report("GET")
            return

        if parsed.path.startswith("/api/report"):
            if not self._session_ok():
                self._send_json({"detail": "未登录或会话已过期"}, status=401)
                return
            self._proxy_report("GET")
            return

        if parsed.path.startswith("/api/tasks"):
            # 报告任务中心（F-04）：同源代理到 8002。
            # ⚠️ 与 /api/report* 一样**必须**带会话校验：任务项含 html_path（磁盘路径）
            # 与失败原因，不是公开数据。8002 侧另有按 user_id 的硬过滤（双重保险）。
            # 漏掉本分支的后果是静默 404 —— 前端任务中心会显示「加载失败」而非「未生成」。
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
        - 透传 `_PROXY_PASS_HEADERS`: 其中 `Content-Disposition` 与 `X-Export-*` 是
          /api/report/export（F-07）下发的**下载文件名与导出元信息** —— 响应体是文件
          字节，这些头丢了既没有替代载体、也不会报错，只会表现为"下载下来名字不对"。
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
        # 下载名 / 导出元信息 / Range 相关头：见 _PROXY_PASS_HEADERS 处的说明。
        # 用 get_all 逐个可能的多值头（Content-Range 等在某些链路上会重复），不合并成一行。
        for _h in _PROXY_PASS_HEADERS:
            for _v in resp.headers.get_all(_h) or []:
                self.send_header(_h, _v)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/watchlist"):
            # F-03 · 路线1：8000 侧只实现了 do_GET/do_POST（无 do_DELETE），
            # 故「删除自选」用 POST /api/watchlist/remove 表达，零改动 HTTP 方法层。
            if not self._session_ok():
                self._send_json({"detail": "未登录或会话已过期"}, status=401)
                return
            self._proxy_report("POST")
            return
        if parsed.path.startswith("/api/report"):
            if not self._session_ok():
                self._send_json({"detail": "未登录或会话已过期"}, status=401)
                return
            self._proxy_report("POST")
            return
        if parsed.path.startswith("/api/tasks"):
            # 报告任务中心（F-04）· 删除失败任务记录 → 同源代理到 8002。
            # ⚠️ 用 POST 而非 DELETE：本侧**只有 do_GET/do_POST**（无 do_DELETE），
            # 与 F-03「删除自选用 POST /api/watchlist/remove 表达」是同一条路线 ——
            # 零改动 HTTP 方法层。若这里漏配分支，前端拿到的是 **405**（落到末尾
            # self.send_error(405)），而 DELETE 更是连方法都没有 → **501**，两者都
            # 只在浏览器控制台可见，页面表现只是「点了删除没反应」。
            # 会话校验必须保留：本端点会改服务端状态（8002 侧另有按 user_id 的归属校验）。
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
    # 注意：**据实打印**——此前无论取数成功与否都会打印「预热完成」，
    # 正是这种假成功日志让上游故障潜伏整日（同样的坑见 _prefetch 的注释）。
    def _prewarm_board_quotes():
        try:
            time.sleep(8)
            # 1) 默认排序窗口: 覆盖列表页首屏与常规翻页
            win_ok = []
            for kind in ("industry", "concept"):
                items, _t = get_board_window(kind, "chg", "desc", 1, 6)
                if items:
                    win_ok.append(kind)
            print("[cache] 板块排序窗口预热: %s 可用%s"
                  % (",".join(win_ok) if win_ok else "无",
                     "" if len(win_ok) == 2 else "（另一个取数失败，首次访问时会重试）"))
            # 2) 全量清单: 供关键词搜索路径(必须全量匹配), 否则首次搜索要等 10-20s
            time.sleep(2)
            full_ok = []
            for kind in ("industry", "concept"):
                recs, st, _m = get_board_data(kind)
                if st == "ok" and recs:
                    full_ok.append("%s(%d)" % (kind, len(recs)))
            if full_ok:
                print("[cache] 板块全量清单预热完成: %s" % " ".join(full_ok))
            else:
                print("[warn] 板块全量清单预热失败: 行情源不可用, 板块页将提示「暂不可用」")
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
