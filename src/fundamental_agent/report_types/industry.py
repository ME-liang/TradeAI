"""行业对比报告：竞品发现 + 数据组装 + LLM 对比内容生成 + 渲染。

职责边界（与基本面/公司概况报告严格隔离）：
- 本文件只服务 report_type="industry"，使用 template_industry.html。
- 不 import / 不修改 render() 与 template.html（基本面）、overview.py 与
  template_overview.html（概况）。
- 定量数据走真实接口（push2→push2delay 主机降级 + F10 ROEJQ + akshare 兜底，
  逻辑与前端 fetch_fundamentals 同源）；定性维度由 LLM 基于各公司画像生成并列
  对比内容；LLM 不可用时降级为空壳（标注"未生成"，绝不编造）。
- 竞品发现优先读 frontend/data/stocks.json（本地全市场快照，含东财申万行业与总市值），
  文件缺失时回退 push2delay clist 全市场分页查询。

JSON 契约与 template_industry.html 的变量一一对应，详见 llm/prompts/industry_report.md。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..data import em as _em
from ..llm import get_router, load_prompt
from ..llm.base import LLMRequest

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
"""仓库根。**必须是 parents[3]**：本文件在 src/fundamental_agent/report_types/ 下，
共 3 层包目录，取了 3 次 parent 只会到 src/。

2026-09-25 事故：此处原为 parent.parent.parent(= <repo>/src)，于是
_STOCKS_JSON 指向 <repo>/src/frontend/data/stocks.json —— **该路径不存在**，
_load_market_snapshot() 恒返回 None，竞品发现静默退化为「实时 clist 全市场分页」，
本地快照这条主路径从未生效。东财 push2delay/push2 被限流当天，实时兜底也失败，
用户侧表现为「竞品识别失败：未在市场快照中找到该股票」。
同目录的 chart_assets.py 用的就是 parents[3]，可作为该写法的参照。"""

_STOCKS_JSON = _PROJECT_ROOT / "frontend" / "data" / "stocks.json"

COMPETITOR_N = 5          # 竞品数量（不含目标公司）
QUANT_BUDGET_S = 40       # 六家定量取数总时长预算（秒）
PROFILE_BUDGET_S = 30     # 六家画像取数总时长预算（秒）

# LLM 示例/占位泄漏拦截（与 overview 同思路，精简为本报告所需的最小集合）
_PLACEHOLDER_MARKERS = ("示例·", "示例:", "示例：", "待填写", "XX 股份有限公司", "TODO")

# 单股定量缓存: {code: (时间戳, dict)}
_QUANT_CACHE: dict[str, tuple[float, dict]] = {}
_QUANT_LOCK = threading.Lock()
_QUANT_TTL_OK_S = 600
_QUANT_TTL_FAIL_S = 120

# 公司画像缓存（进程内一次即可；主营范围属低频变化数据）
_PROFILE_CACHE: dict[str, dict] = {}
_PROFILE_FAIL_CACHE: dict[str, float] = {}
_PROFILE_LOCK = threading.Lock()

# 远程全市场快照缓存。仅在「本地快照缺目标股」时才用到，而拉取代价很高
# （最多 60 页 × 100 条），故按 TTL 复用，避免同一进程内反复全量拉取。
_REMOTE_SNAP: dict = {"ts": 0.0, "stocks": []}
_REMOTE_SNAP_LOCK = threading.Lock()
_REMOTE_SNAP_TTL_S = 600


# ---------------------------------------------------------------------------
# 基础取数工具：**实现已统一到 data/em.py**（主机池只应有一处定义，见 N-18）。
# 这里保留同名薄包装，理由有二：
#   1) board.py 与 tests/test_industry_snapshot.py 都按名字引用过它们；
#   2) `_curl_json_hosts(hosts, path)` 的 hosts 由调用方传入，便于测试注入。
# 新增代码请直接用 `_em.get_json()`，不要再在本文件写主机字面量。
# ---------------------------------------------------------------------------
def _curl_json(url: str, timeout: int = 10):
    """单 URL GET → JSON（失败 None）。等价于 `em.curl_json`。"""
    return _em.curl_json(url, timeout=timeout)


def _curl_json_hosts(hosts: list[str], path: str, timeout: int = 10):
    """按主机健康度降级取数；返回首个成功结果。等价于 `em.get_json`。"""
    return _em.get_json(path, timeout=timeout, hosts=hosts)


def _secid(code: str) -> str:
    return ("1." if code[:1] in ("6", "9") else "0.") + code


# ---------------------------------------------------------------------------
# 竞品发现（本地全市场快照优先，clist 全市场分页兜底）
# ---------------------------------------------------------------------------
def _load_market_snapshot() -> list[dict] | None:
    try:
        d = json.loads(_STOCKS_JSON.read_text(encoding="utf-8"))
        stocks = d.get("stocks") or []
        return stocks or None
    except Exception:
        return None


def _fetch_market_snapshot_remote() -> list[dict]:
    """clist 全市场分页拉取（stocks.json 缺失时的兜底），返回精简记录。"""
    out: list[dict] = []
    pn = 1
    while pn <= 60:  # 60 页 x 100 条足够覆盖全市场
        q = urllib.parse.urlencode({
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:90",
            # f12代码 f14名称 f100行业 f20总市值 f9市盈率(动) f23市净率 —— pe/pb 必须带上，
            # 否则 stocks.json 读取失败走远程兜底时 PE/PB 全空
            "fields": "f12,f14,f100,f20,f9,f23",
            "pn": str(pn), "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
        })
        # 主机池取自 data/em.py（push2test 在末位兜底，理由见该模块 docstring）
        d = _curl_json_hosts(_em.PUSH2_HOSTS, "/api/qt/clist/get?" + q, timeout=10)
        diff = ((d or {}).get("data") or {}).get("diff") or []
        if not diff:
            break
        for v in diff:
            try:
                mcap = float(v.get("f20")) if v.get("f20") not in ("-", None) else None
            except (TypeError, ValueError):
                mcap = None

            def _f(k):
                try:
                    x = v.get(k)
                    return float(x) if x not in ("-", None) else None
                except (TypeError, ValueError):
                    return None
            out.append({"code": str(v.get("f12") or ""), "name": str(v.get("f14") or ""),
                        "ind": str(v.get("f100") or ""), "mcap": mcap,
                        "pe": _f("f9"), "pb": _f("f23")})
        pn += 1
    return out


def _remote_snapshot_cached() -> list[dict]:
    """带 TTL 缓存的远程全市场快照（失败返回上次结果 / 空列表，绝不抛）。"""
    now = time.time()
    with _REMOTE_SNAP_LOCK:
        cached = list(_REMOTE_SNAP.get("stocks") or [])
        if cached and now - float(_REMOTE_SNAP.get("ts") or 0.0) < _REMOTE_SNAP_TTL_S:
            return cached
    try:
        stocks = _fetch_market_snapshot_remote()
    except Exception as e:                      # 兜底路径不允许把异常抛给调用方
        logger.warning("[industry] 远程全市场快照拉取异常: %s", e)
        stocks = []
    if stocks:
        with _REMOTE_SNAP_LOCK:
            _REMOTE_SNAP["ts"] = now
            _REMOTE_SNAP["stocks"] = list(stocks)
        return stocks
    return cached


def _snapshot_find(code: str) -> tuple[list[dict], dict | None]:
    """在本地/远程市场快照中定位标的，返回 ``(stocks, target)``。

    本地快照优先；**只有「本地快照里没有这只股票」时**才用远程兜底再查一次。
    这一条是必须的：本地快照会陈旧（frontend/data/stocks.json 的 updated 字段
    可能落后数周，其间新上市的标的都不在其中），而原实现仅在「整个文件读不到」
    时才兜底 —— 于是「文件在、但缺该股」直接判失败。

    注意：**北交所标的两边都查不到**，因为 `_fetch_market_snapshot_remote()` 沿用了
    前端的 fs 过滤串，其中北交所段 `m:0+t:90` 实测无效（total=None / 0 行，
    且加不加它对总数毫无影响：均为 5561）。北交所正解是 `m:0+t:81+s:2048`（359 只）。
    这属独立的市场覆盖问题，未在本处擅自改动 fs（会连带影响列表页与覆盖率分段），
    见 `.workbuddy/memory/2026-09-25.md`。
    """
    stocks = _load_market_snapshot()
    target = next((r for r in (stocks or []) if r.get("code") == code), None)
    if target is None:
        logger.warning("[industry] 本地市场快照未含标的 %s, 改用远程全市场快照兜底", code)
        remote = _remote_snapshot_cached()
        rt = next((r for r in remote if r.get("code") == code), None)
        if rt is not None:
            return remote, rt
    return list(stocks or []), target


def _fetch_board_members(bk: str) -> list[dict]:
    """按板块代码拉取成员列表（clist fs=b:{bk}，按市值降序）。失败返回空列表。"""
    q = urllib.parse.urlencode({
        "fs": "b:" + bk,
        "fields": "f12,f14,f20,f115,f23",   # 代码/名称/总市值/市盈率TTM/市净率
        "pn": "1", "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
    })
    d = _curl_json_hosts(_em.PUSH2_HOSTS, "/api/qt/clist/get?" + q, timeout=15)
    diff = ((d or {}).get("data") or {}).get("diff") or []

    def _f(x):
        try:
            return float(x) if x not in ("-", None) else None
        except (TypeError, ValueError):
            return None

    return [{"code": str(v.get("f12") or ""), "name": str(v.get("f14") or "").replace(" ", ""),
             "mcap": _f(v.get("f20")), "pe": _f(v.get("f115")), "pb": _f(v.get("f23"))}
            for v in diff if str(v.get("f12") or "")]


def discover_competitors(code: str, n: int = COMPETITOR_N,
                         bk: str = "", bk_name: str = "") -> dict:
    """识别竞品（排除目标公司自身）。

    指定 bk（东财板块代码）时以该板块成员为对比样本（基准由前端下拉框实时选定）；
    未指定或板块拉取失败时，回退为市场快照按东财行业筛选市值前 N。
    市场快照本地优先（frontend/data/stocks.json），**本地缺该股时再用远程全市场兜底**。
    返回 {"industry": str, "target": {code,name,mcap,pe,pb},
          "competitors": [{code,name,mcap}, ...]}；识别失败返回空 peers。
    """
    bk = str(bk or "").strip()
    if bk:
        if not re.fullmatch(r"BK\d+", bk):
            logger.warning("[industry] 非法板块代码 %r, 忽略板块基准", bk)
        else:
            members = _fetch_board_members(bk)
            if members:
                target = next((r for r in members if r.get("code") == code), None)
                if target is None:
                    # 板块清单偶发未含目标股（数据同步延迟）: 从市场快照补目标信息
                    _snap, ts = _snapshot_find(code)
                    if ts:
                        target = {"code": code, "name": str(ts.get("name") or "").replace(" ", ""),
                                  "mcap": ts.get("mcap"), "pe": ts.get("pe"), "pb": ts.get("pb")}
                if target is not None:
                    peers = [r for r in members if r.get("code") != code and r.get("mcap")]
                    peers.sort(key=lambda r: (r.get("mcap") or 0), reverse=True)
                    competitors = [{"code": r["code"], "name": r["name"], "mcap": r.get("mcap"),
                                    "pe": r.get("pe"), "pb": r.get("pb")} for r in peers[:n]]
                    ind_disp = str(bk_name or "").strip() or f"板块{bk}"
                    return {"industry": ind_disp, "target": target, "competitors": competitors}
            logger.warning("[industry] 板块 %s 成员获取失败, 回退按东财行业识别竞品", bk)
    stocks, target = _snapshot_find(code)
    if not stocks or target is None:
        logger.warning("[industry] 市场快照未覆盖标的 %s, 竞品识别失败", code)
        return {"industry": "", "target": None, "competitors": []}
    ind = str(target.get("ind") or "")
    peers = [r for r in stocks if r.get("ind") == ind and r.get("code") != code]
    peers.sort(key=lambda r: (r.get("mcap") or 0), reverse=True)
    competitors = [{"code": r["code"], "name": str(r.get("name") or "").replace(" ", ""),
                    "mcap": r.get("mcap"), "pe": r.get("pe"), "pb": r.get("pb")} for r in peers[:n]]
    t = {"code": code, "name": str(target.get("name") or "").replace(" ", ""),
         "mcap": target.get("mcap"), "pe": target.get("pe"), "pb": target.get("pb")}
    # 行业名去掉申万二级的罗马数字后缀(与前端展示口径一致), 如 "白酒Ⅱ" -> "白酒"
    ind_disp = re.sub(r"[ⅠⅡⅢⅣⅤ]$", "", ind)
    return {"industry": ind_disp, "target": t, "competitors": competitors}


# ---------------------------------------------------------------------------
# 单股定量指标（push2 单股 + F10 ROEJQ + akshare 兜底，多源降级）
# ---------------------------------------------------------------------------
def _fetch_quant_impl(code: str, snap: dict | None) -> dict:
    res = {"roe": None, "gross": None, "debt_ratio": None, "rev_growth": None,
           "pe": (snap or {}).get("pe"), "pb": (snap or {}).get("pb"),
           "mcap": (snap or {}).get("mcap")}
    # 1) push2 单股接口: 毛利率 f186 / 资产负债率 f188 / 营收同比增长 f184，
    #    顺带取市盈率 f115(TTM, 部分时段返回'-')/f162(动态) / 市净率 f23 / 总市值 f116（快照值作兜底）
    q = urllib.parse.urlencode({"secid": _secid(code),
                                "fields": "f186,f188,f184,f115,f162,f23,f116",
                                "invt": "2", "fltt": "2"})
    d = _curl_json_hosts(_em.PUSH2_HOSTS, "/api/qt/stock/get?" + q, timeout=8)
    row = (d or {}).get("data") or {}

    def _num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    res["gross"] = _num(row.get("f186"))
    res["debt_ratio"] = _num(row.get("f188"))
    res["rev_growth"] = _num(row.get("f184"))  # 注意: f162 是市盈率(动)而非营收同比
    pe_ttm = _num(row.get("f115"))
    res["pe"] = pe_ttm if pe_ttm is not None else _num(row.get("f162"))
    pb = _num(row.get("f23"))
    if pb is not None:
        res["pb"] = pb
    mcap = _num(row.get("f116"))
    if mcap is not None:
        res["mcap"] = mcap
    # 2) ROE 主源: F10 主要财务指标 ROEJQ。走 em 封装是**必须**的：
    #    `sty` 与 `filter` 都是必填，原实现漏了二者 → 服务端回 9501
    #    「返回字段参数不能为空」→ 这里恒拿不到值，却被当成「该股没有 ROE」（N-19）。
    try:
        _v = _em.f10_roejq(code)
        if _v is not None:
            res["roe"] = _v
    except Exception as e:
        logger.warning("[industry] F10 ROE 获取失败(code=%s): %s", code, e)
    # 3) ROE 兜底: akshare 财务分析指标（非东财源）
    if res["roe"] is None:
        try:
            import akshare as ak
            df = ak.stock_financial_analysis_indicator(symbol=code,
                                                       start_year=str(datetime.now().year - 1))
            if df is not None and len(df):
                cols = [c for c in df.columns if ("加权" in c and "净资产收益率" in c) or c == "净资产收益率(%)"]
                if cols:
                    v = df.iloc[-1][cols[0]]
                    res["roe"] = _num(v)
        except Exception as e:
            logger.warning("[industry] akshare ROE 兜底失败(code=%s): %s", code, e)
    return res


def fetch_stock_quant(code: str, snap: dict | None = None) -> dict:
    """带 TTL 缓存的单股定量指标（成功 10 分钟 / 全失败 2 分钟）。"""
    now = time.time()
    with _QUANT_LOCK:
        hit = _QUANT_CACHE.get(code)
        if hit:
            ts, val = hit
            ok = any(val.get(k) is not None for k in ("roe", "gross", "debt_ratio", "rev_growth"))
            if now - ts < (_QUANT_TTL_OK_S if ok else _QUANT_TTL_FAIL_S):
                return dict(val)
    res = _fetch_quant_impl(code, snap)
    with _QUANT_LOCK:
        _QUANT_CACHE[code] = (now, dict(res))
    return res


# ---------------------------------------------------------------------------
# 公司画像（主营范围等定性素材，cninfo 优先、东财 F10 兜底）
# ---------------------------------------------------------------------------
_PROFILE_TTL_FAIL_S = 60  # 画像取数失败的短缓存时长（秒）


def fetch_company_profile(code: str) -> dict:
    now = time.time()
    with _PROFILE_LOCK:
        if code in _PROFILE_CACHE:
            return dict(_PROFILE_CACHE[code])
        fail_ts = _PROFILE_FAIL_CACHE.get(code)
        if fail_ts is not None and now - fail_ts < _PROFILE_TTL_FAIL_S:
            return {}  # 刚失败过，短缓存期内直接返回空，避免重复打接口
    info: dict = {}
    # 1) 巨潮 cninfo 公司简介（含主营业务/经营范围/机构简介）
    try:
        import akshare as ak
        # cninfo 系接口用 py_mini_racer 新建 V8 isolate, 多线程并发会 FATAL 崩进程
        # （2026-09-06 行业画像 6 路并发实测复现, 见 data/cninfo_guard.py）；
        # _gather_profiles_parallel 仍可 6 并发, 但 cninfo 段经全局锁串行执行。
        from fundamental_agent.data.cninfo_guard import cninfo_guard
        with cninfo_guard():
            df = ak.stock_profile_cninfo(symbol=code)
        if df is not None and len(df):
            row = df.iloc[0].to_dict()

            def _s(k):
                v = row.get(k)
                return str(v).strip() if v is not None and str(v).strip().lower() != "nan" else ""
            info = {"main_business": _s("主营业务"), "scope": _s("经营范围"),
                    "intro": _s("机构简介"), "industry": _s("所属行业")}
    except Exception as e:
        logger.warning("[industry] cninfo 画像获取失败(code=%s): %s", code, e)
    # 2) 兜底: 东财 F10 公司概况（同走 em 封装，参数理由见 N-19）
    if not info.get("main_business"):
        try:
            _fp = _em.f10_profile(code)
            for _k in ("main_business", "scope", "intro", "industry"):
                if _fp.get(_k):
                    info.setdefault(_k, _fp[_k])
        except Exception as e:
            logger.warning("[industry] 东财画像兜底失败(code=%s): %s", code, e)
    with _PROFILE_LOCK:
        if info.get("main_business") or info.get("scope") or info.get("intro"):
            _PROFILE_CACHE[code] = dict(info)  # 成功结果永久缓存
        else:
            _PROFILE_FAIL_CACHE[code] = now  # 空结果只短缓存，60 秒后允许重试
    return info


def _gather_quant_parallel(codes: list[str], snaps: dict[str, dict]) -> dict[str, dict]:
    """并发取六家定量指标，带总时长预算；超时用已完成部分。"""
    out: dict[str, dict] = {}
    ex = ThreadPoolExecutor(max_workers=6)
    try:
        deadline = time.time() + QUANT_BUDGET_S
        futs = {ex.submit(fetch_stock_quant, c, snaps.get(c)): c for c in codes}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                out[c] = fut.result()
            except Exception:
                out[c] = {}
            if time.time() > deadline:
                break
    except Exception as e:
        logger.warning("[industry] 定量并发取数异常: %s", e)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    for c in codes:
        out.setdefault(c, {})
    return out


def _gather_profiles_parallel(codes: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    ex = ThreadPoolExecutor(max_workers=6)
    try:
        deadline = time.time() + PROFILE_BUDGET_S
        futs = {ex.submit(fetch_company_profile, c): c for c in codes}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                out[c] = fut.result()
            except Exception:
                out[c] = {}
            if time.time() > deadline:
                break
    except Exception as e:
        logger.warning("[industry] 画像并发取数异常: %s", e)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    for c in codes:
        out.setdefault(c, {})
    return out


# ---------------------------------------------------------------------------
# LLM 生成 + 校验闸门
# ---------------------------------------------------------------------------
_DIMENSIONS = ("positioning", "differentiation", "customers", "market_position")


def _parse_json_loose(text: str) -> dict | None:
    s = str(text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _bad_text(s: str) -> bool:
    t = str(s or "")
    return (not t.strip()) or any(mk in t for mk in _PLACEHOLDER_MARKERS)


def _pct_claims(text: str) -> list[float]:
    return [float(m.group(1)) for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", str(text))]


def _norm_items(raw, names: list[str], keys: tuple[str, ...]) -> list[dict]:
    """把 LLM 返回的维度条目归一化为 [{name, *keys}]。

    兼容三种形态（不同模型风格不一）：
      1. [{"name": "...", "text": "..."}]           —— 标准对象列表
      2. ["公司名：text...", ...]                    —— 字符串列表（前缀公司名）
      3. {"公司名": "text..." 或 {...}}              —— 以公司名为键的字典
    无法归入给定公司的条目一律丢弃（宁缺勿编）。
    """
    out: list[dict] = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            nm = str(k).replace(" ", "")
            if nm not in names:
                continue
            entry = {"name": nm}
            if isinstance(v, dict):
                for kk in keys:
                    entry[kk] = v.get(kk)
            else:
                entry[keys[0]] = v
            out.append(entry)
        return out
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                nm = str(it.get("name") or "").replace(" ", "")
                if nm in names:
                    entry = {"name": nm}
                    for kk in keys:
                        entry[kk] = it.get(kk)
                    out.append(entry)
            elif isinstance(it, str):
                s = it.strip()
                for nm in names:
                    if s.startswith(nm):
                        rest = s[len(nm):].lstrip("：: ")
                        entry = {"name": nm}
                        entry[keys[0]] = rest
                        out.append(entry)
                        break
    return out


def _normalize_llm_json(parsed: dict, names: list[str]) -> dict:
    """将各维度归一化；swot 字符串需再拆优势/劣势两段。"""
    out = {}
    for dim in _DIMENSIONS:
        out[dim] = _norm_items(parsed.get(dim), names, ("text",))
    swot_raw = parsed.get("swot")
    swot = []
    for it in _norm_items(swot_raw, names, ("advantage", "disadvantage")):
        if not it.get("advantage") and isinstance(swot_raw, list):
            # 字符串形态: "公司名：优势…；劣势…" —— 按标记拆两段
            for raw_it in (swot_raw if isinstance(swot_raw, list) else []):
                if isinstance(raw_it, str) and raw_it.strip().startswith(it["name"]):
                    s = raw_it.strip()[len(it["name"]):].lstrip("：: ")
                    m_adv = re.search(r"优势[:：]?\s*(.*?)(?:；|;|$)", s)
                    m_dis = re.search(r"劣势[:：]?\s*(.*)$", s)
                    if m_adv:
                        it["advantage"] = m_adv.group(1).strip()
                    if m_dis:
                        it["disadvantage"] = m_dis.group(1).strip()
                    break
        swot.append(it)
    out["swot"] = swot
    out["summary_html"] = parsed.get("summary_html")
    return out


def _industry_json_is_valid(parsed: dict, names: list[str], facts: dict[str, dict]) -> tuple[bool, list[str]]:
    """校验 LLM 输出：维度齐全、公司全覆盖、无占位残留、百分比引用可追溯。"""
    issues: list[str] = []
    name_set = set(names)
    for dim in _DIMENSIONS:
        items = parsed.get(dim)
        if not isinstance(items, list) or not items:
            issues.append(f"{dim} 缺失或为空")
            continue
        got = {str(it.get("name") or "").replace(" ", "") for it in items if isinstance(it, dict)}
        if got != name_set:
            issues.append(f"{dim} 公司覆盖不全: 缺{sorted(name_set - got)} 多{sorted(got - name_set)}")
        for it in items:
            if not isinstance(it, dict) or _bad_text(it.get("text")):
                issues.append(f"{dim} 存在空文本或占位残留")
                break
    swot = parsed.get("swot")
    if not isinstance(swot, list) or not swot:
        issues.append("swot 缺失或为空")
    else:
        got = {str(it.get("name") or "").replace(" ", "") for it in swot if isinstance(it, dict)}
        if got != name_set:
            issues.append(f"swot 公司覆盖不全: 缺{sorted(name_set - got)} 多{sorted(got - name_set)}")
        for it in swot:
            if not isinstance(it, dict) or _bad_text(it.get("advantage")) or _bad_text(it.get("disadvantage")):
                issues.append("swot 存在空文本或占位残留")
                break
    if _bad_text(parsed.get("summary_html")):
        issues.append("summary_html 缺失或占位残留")
    # 百分比引用可追溯：正文里出现的 % 必须与某家公司的真实指标量级吻合
    allowed: list[float] = []
    for c, q in facts.items():
        for k in ("roe", "gross", "debt_ratio", "rev_growth"):
            v = q.get(k)
            if v is not None:
                allowed.append(abs(float(v)))
    def _known(x: float) -> bool:
        return any(a * 0.55 <= x <= a * 1.8 for a in allowed)
    if allowed:
        for dim in (*_DIMENSIONS, "swot"):
            for it in (parsed.get(dim) or []):
                if not isinstance(it, dict):
                    continue
                for key in ("text", "advantage", "disadvantage"):
                    for x in _pct_claims(it.get(key)):
                        if not _known(x):
                            issues.append(f"{dim} 出现不可追溯的百分比 {x}%（数据速览中无此量级）")
                            break
    return (not issues), issues


def _company_fact_lines(companies: list[dict], quants: dict[str, dict],
                        profiles: dict[str, dict], ranks: dict[str, int]) -> str:
    lines = []
    for c in companies:
        code = c["code"]
        q = quants.get(code) or {}
        p = profiles.get(code) or {}

        def f(k):
            v = q.get(k)
            return ("%.2f" % v) if isinstance(v, (int, float)) else "未获取"

        mcap = q.get("mcap")
        mcap_disp = ("%.0f 亿" % (mcap / 1e8)) if isinstance(mcap, (int, float)) and mcap > 0 else "未获取"
        mb = str(p.get("main_business") or p.get("scope") or p.get("intro") or "").strip()
        if len(mb) > 260:
            mb = mb[:260] + "…"
        lines.append(
            f"- {c['name']}（市值排名: 第{ranks.get(c['name'], '?')}名）"
            f" 总市值: {mcap_disp} | PE: {f('pe')} | PB: {f('pb')}"
            f" | ROE: {f('roe')}% | 毛利率: {f('gross')}% | 营收同比: {f('rev_growth')}%"
            f" | 资产负债率: {f('debt_ratio')}%\n"
            f"  主营业务: {mb or '未获取'}"
        )
    return "\n".join(lines)


def generate_industry_content(companies: list[dict], quants: dict[str, dict],
                              profiles: dict[str, dict], ranks: dict[str, int],
                              target_name: str, industry: str,
                              max_retries: int = 1) -> dict | None:
    """调用 LLM 生成六维度并列对比 JSON；失败（含额度/网络）返回 None。

    max_tokens 策略（性能优化·措施3）：qwen 等按量计费厂商按 max_tokens 预留额度，
    从 6000 起步必报 402（余额不足），每次都白等一轮完整失败再降档。故直接从
    4000 起步（qwen 实测上限 5000），单轮即可成功；重试从 2 次降为 1 次。
    resp.ok=False 时 router 返回降级文本而非异常，必须显式检查。
    """
    names = [c["name"] for c in companies]
    try:
        system = load_prompt("industry_report")
    except Exception:
        system = (
            "你是资深的 A 股行业研究分析师。基于给定真实数据生成行业对比报告的结构化 JSON，"
            "以目标公司为主线与竞争对手逐维度并列对比，严禁编造任何未提供的数字，未知字段标'未获取'。"
        )
    facts_text = _company_fact_lines(companies, quants, profiles, ranks)
    user = (
        f"行业: {industry or '未获取'}\n目标公司: {target_name}\n"
        f"对比样本共 {len(names)} 家（目标公司居首）: {'、'.join(names)}\n\n"
        f"【真实数据速览】（引用任何数字必须与此一致，严禁编造或改写）:\n{facts_text}\n\n"
        "请严格按契约返回行业对比报告内容：纯 JSON（不要 markdown 代码块）。"
        "注意：positioning/differentiation/customers/market_position/swot 每个都必须是"
        "**对象数组**，每个元素形如 {\"name\": \"<公司名>\", \"text\": \"...\"}"
        "（swot 为 {\"name\":..., \"advantage\":..., \"disadvantage\":...}），"
        "**禁止用纯字符串代替对象**；每个数组的元素数必须等于公司数且 name 与给定名称逐字一致。"
        "定位/差异化/客户群体等分析必须体现公司之间的横向比较（共同点与差异点），"
        "禁止写成互不相关的单公司介绍；竞争地位只能基于给定的市值排名与指标陈述。"
    )
    token_ladder = (4000, 2000)  # 措施3：从 4000 起步（6000 对 qwen 必 402 白等一轮）
    for attempt in range(max_retries):
        mt = token_ladder[min(attempt, len(token_ladder) - 1)]
        try:
            resp = get_router().complete(
                LLMRequest(system=system, user=user, max_tokens=mt)
            )
        except Exception as e:
            logger.warning("[industry] LLM 调用异常: %s", e)
            return None
        if not resp.ok:
            # router 失败时返回降级文本(不是异常), 记录真实错误并降档重试
            logger.warning("[industry] LLM 请求失败(max_tokens=%s): %s", mt, str(resp.error)[:200])
            continue
        parsed = _parse_json_loose(resp.text)
        if not parsed:
            logger.warning("[industry] 第 %d 次生成 JSON 解析失败(长度 %d)", attempt + 1, len(resp.text))
            continue
        parsed = _normalize_llm_json(parsed, names)
        ok, issues = _industry_json_is_valid(parsed, names, quants)
        if ok:
            return parsed
        for i in issues:
            logger.warning("[industry] 第 %d 次生成未通过校验: %s", attempt + 1, i)
    return None


# ---------------------------------------------------------------------------
# 数据装配 + 渲染
# ---------------------------------------------------------------------------
def _fmt_num(v, suffix="", digits=2):
    if isinstance(v, (int, float)):
        return ("%." + str(digits) + "f%s") % (v, suffix)
    return "—"


def _fmt_mcap(v):
    if isinstance(v, (int, float)) and v > 0:
        return "%.0f 亿" % (v / 1e8)
    return "—"


def _fmt_pct(v):
    if not isinstance(v, (int, float)):
        return "—"
    return ("+%.2f%%" % v) if v > 0 else ("%.2f%%" % v)


def build_industry_data(discovery: dict, quants: dict[str, dict], profiles: dict[str, dict],
                        llm_json: dict | None, use_llm: bool = True) -> dict:
    """装配模板数据；LLM 缺位时定性维度渲染降级说明（不编造）。"""
    target = discovery.get("target") or {}
    comps = [target] + list(discovery.get("competitors") or [])
    target_code = target.get("code") or ""
    companies = []
    names = []
    for c in comps:
        if not c or not c.get("code"):
            continue
        code = c["code"]
        q = quants.get(code) or {}
        companies.append({
            "code": code, "name": c.get("name") or code,
            "is_target": code == target_code,
            "mcap": _fmt_mcap(q.get("mcap", c.get("mcap"))),
            "pe": _fmt_num(q.get("pe")), "pb": _fmt_num(q.get("pb")),
            "roe": _fmt_num(q.get("roe"), suffix="%"),
            "gross": _fmt_num(q.get("gross"), suffix="%"),
            "rev": _fmt_pct(q.get("rev_growth")),
            "main_business": (profiles.get(code) or {}).get("main_business") or "未获取",
        })
        names.append(c.get("name") or code)

    ranks = {c["name"]: i + 1 for i, c in enumerate(
        sorted([c for c in comps if c], key=lambda x: (x.get("mcap") or 0), reverse=True))}

    def _items(dim, keys=("text",), optional=()):
        src = (llm_json or {}).get(dim) or []
        by_name = {str(it.get("name") or "").replace(" ", ""): it for it in src if isinstance(it, dict)}
        out = []
        for c in companies:
            it = by_name.get(c["name"]) or {}
            entry = {"name": c["name"], "is_target": c["is_target"]}
            for k in keys:
                v = str(it.get(k) or "").strip()
                if k in optional:
                    # 可选字段(如差异化卡的"细分占优"): 缺失就留空, 模板直接隐藏该块
                    entry[k] = v
                else:
                    entry[k] = v if v else "未生成（LLM 不可用或校验未通过，不编造内容）"
            out.append(entry)
        return out

    swot_src = (llm_json or {}).get("swot") or []
    swot_by = {str(it.get("name") or "").replace(" ", ""): it for it in swot_src if isinstance(it, dict)}
    swot = []
    for c in companies:
        it = swot_by.get(c["name"]) or {}
        swot.append({
            "name": c["name"], "is_target": c["is_target"],
            "advantage": str(it.get("advantage") or "").strip() or "未生成（LLM 不可用或校验未通过）",
            "disadvantage": str(it.get("disadvantage") or "").strip() or "未生成（LLM 不可用或校验未通过）",
        })
    summary = str((llm_json or {}).get("summary_html") or "").strip()
    if not summary:
        summary = ("<p>竞争格局总结未能生成（LLM 不可用或输出未通过校验）。"
                   "定量对比见上方速览表，定性维度待 AI 生成。</p>")
    return {
        "target_name": target.get("name") or "—",
        "target_code": target_code,
        "industry": discovery.get("industry") or "—",
        "report_date": datetime.now().strftime("%Y-%m-%d"),
        "companies": companies,
        "llm_ok": bool(llm_json),
        "positioning": _items("positioning"),
        "differentiation": _items("differentiation", keys=("text", "edge"), optional=("edge",)),
        "customers": _items("customers"),
        "market_position": _items("market_position"),
        "swot": swot,
        "summary_html": summary,
    }


def render_industry(data: dict, template_dir: str, out_path: str | None = None) -> str:
    """用 template_industry.html 渲染行业对比报告；可选落盘（返回文件路径）。"""
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["blockp"] = _blockp
    tpl = env.get_template("template_industry.html")
    html = tpl.render(**data)
    if out_path:
        Path(out_path).write_text(html, encoding="utf-8")
        return out_path
    return html


def _blockp(value):
    """段落：内容已含 <p> 时原样输出，否则补一层 <p>（与 overview 渲染口径一致）。"""
    from markupsafe import Markup
    s = "" if value is None else str(value)
    s = s.strip()
    if not s:
        return Markup("")
    s = re.sub(r"\*\*([^*\n]{1,200})\*\*", r"<strong>\1</strong>", s)
    if re.search(r"<p[\s>]", s, re.I):
        return Markup(s)
    return Markup(f"<p>{s}</p>")


# ---------------------------------------------------------------------------
# 编排入口（供 main.py 的 industry 分支调用）
# ---------------------------------------------------------------------------
def build_industry_report_data(code: str, name: str, use_llm: bool = True,
                               progress=None, bk: str = "", bk_name: str = "") -> dict:
    """竞品发现 → 定量/画像取数 → LLM 生成 → 装配模板数据。

    bk/bk_name 为前端下拉框实时选定的对比基准板块（概念或行业板块）；为空时按东财行业。
    """
    def _emit(stage, pct=0):
        if progress:
            try:
                progress(stage, pct)
            except Exception:
                pass

    _emit("识别板块内竞品" if bk else "识别同行业竞品", 60)
    discovery = discover_competitors(code, n=COMPETITOR_N, bk=bk, bk_name=bk_name)
    target = discovery.get("target") or {}
    comps = [target] + list(discovery.get("competitors") or [])
    comps = [c for c in comps if c and c.get("code")]
    if not comps:
        raise RuntimeError("竞品识别失败：未在市场快照中找到该股票")

    codes = [c["code"] for c in comps]
    snaps = {c["code"]: c for c in comps}
    _emit("抓取竞品定量指标与公司画像", 68)
    quants = _gather_quant_parallel(codes, snaps)
    # 回填兜底：实时接口缺失的 pe/pb/mcap 用竞品发现阶段的快照值补齐，
    # 避免快照/接口任一来源抖动导致报告出现"—"空值
    for c in codes:
        q = quants.get(c) or {}
        s = snaps.get(c) or {}
        for k in ("pe", "pb", "mcap"):
            if q.get(k) is None and s.get(k) is not None:
                q[k] = s.get(k)
        quants[c] = q
    profiles = _gather_profiles_parallel(codes)

    ranks = {}
    for i, c in enumerate(sorted(comps, key=lambda x: (x.get("mcap") or 0), reverse=True)):
        ranks[c.get("name") or ""] = i + 1

    llm_json = None
    if use_llm:
        _emit("生成 AI 竞争对比分析", 80)
        target_name = str(target.get("name") or name or "").replace(" ", "")
        names = [str(c.get("name") or "").replace(" ", "") for c in comps]
        try:
            llm_json = generate_industry_content(
                [{"name": n, "code": c} for n, c in zip(names, codes)],
                quants, profiles, ranks, target_name, discovery.get("industry") or "")
        except Exception as e:
            logger.warning("[industry] LLM 内容生成异常: %s", e)
            llm_json = None

    _emit("渲染行业对比报告", 92)
    return build_industry_data(discovery, quants, profiles, llm_json, use_llm=use_llm)
