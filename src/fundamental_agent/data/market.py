"""市场行情与融资融券取数（公司概况报告「估值坐标 / 资金面」专用）。

**为什么不用 AkShare 封装**：本项目的 `AkShareClient` 明确规避东财 push2 系列端点——
实测其在当前环境下连接建立后不返回，socket 读会无限期阻塞，曾把整条报告 pipeline
挂死 20+ 分钟（见 `data/akshare.py` 的 `DEFAULT_SOCKET_TIMEOUT` 注释）。
但 `push2delay.eastmoney.com`（**延时行情**端点）实测稳定可用，且已在
`frontend/fetch_stocks.py` 中长期用于全市场列表拉取。

本模块因此绕开 AkShare，直接用 urllib 请求 push2delay，并强制：
- 显式 timeout（单次 15s），任何异常一律返回空 dict，**绝不让行情拖垮主流程**；
- 返回结构中标明 `delayed=True` 与数据日期，避免延迟行情被当成实时价使用。
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_TIMEOUT = 15.0

# f43=最新价 f44=最高 f45=最低 f46=今开 f60=昨收 f47=成交量(手) f48=成交额
# f169=涨跌额 f170=涨跌幅 f168=换手率 f116=总市值 f117=流通市值
# f162=PE(TTM) f167=PB f86=行情时间戳
_QUOTE_FIELDS = (
    "f43,f44,f45,f46,f47,f48,f57,f58,f60,f86,f116,f117,f162,f167,f168,f169,f170"
)


def _norm_code(code: str) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())[-6:].zfill(6)


def _secid(code: str) -> str:
    """东财市场前缀：1=沪市（60/68/9 开头），0=深市（00/30/8/4 开头）。"""
    c = _norm_code(code)
    return f"1.{c}" if c[:2] in ("60", "68") or c[0] == "9" else f"0.{c}"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _f(v):
    """东财用 '-' 表示无数据，统一转 None。"""
    if v is None or v == "-" or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_quote(code: str) -> dict:
    """个股延时行情快照。返回 dict；失败返回 {}。

    关键口径：这是**延时行情**（push2delay），非实时 tick。报告渲染时必须标注
    数据日期与「延时」字样，否则读者会当作当前价使用。
    """
    c = _norm_code(code)
    if not c:
        return {}
    url = (
        "https://push2delay.eastmoney.com/api/qt/stock/get"
        f"?secid={_secid(c)}&invt=2&fltt=2&fields={_QUOTE_FIELDS}"
    )
    try:
        d = _get_json(url).get("data") or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("[market] 行情拉取失败 %s：%s", c, e)
        return {}
    if not d:
        return {}

    ts = _f(d.get("f86"))
    qdate = ""
    if ts and ts > 1e9:
        try:
            qdate = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            qdate = ""

    return {
        "code": c,
        "name": d.get("f58") or "",
        "price": _f(d.get("f43")),
        "change": _f(d.get("f169")),
        "change_pct": _f(d.get("f170")),
        "open": _f(d.get("f46")),
        "high": _f(d.get("f44")),
        "low": _f(d.get("f45")),
        "prev_close": _f(d.get("f60")),
        "volume_hand": _f(d.get("f47")),
        "amount": _f(d.get("f48")),
        "turnover_rate": _f(d.get("f168")),
        "total_mcap": _f(d.get("f116")),
        "float_mcap": _f(d.get("f117")),
        "pe_ttm": _f(d.get("f162")),
        "pb": _f(d.get("f167")),
        "quote_date": qdate,
        "delayed": True,
        "source": "东方财富 push2delay（延时行情）",
    }


def get_margin(code: str, days: int = 5) -> dict:
    """个股融资融券余额（东财 datacenter 明细）。返回 dict；失败返回 {}。

    RZYE=融资余额 / RQYE=融券余额 / RZRQYE=融资融券余额 / RZYEZB=融资余额占流通市值比。
    """
    c = _norm_code(code)
    if not c:
        return {}
    flt = urllib.parse.quote(f'(SCODE="{c}")')
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPTA_WEB_RZRQ_GGMX&columns=ALL"
        f"&pageSize={int(days)}&sortColumns=DATE&sortTypes=-1&filter={flt}"
    )
    try:
        d = _get_json(url)
        rows = ((d or {}).get("result") or {}).get("data") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("[market] 融资融券拉取失败 %s：%s", c, e)
        return {}
    if not rows:
        return {}
    r = rows[0]
    date = str(r.get("DATE") or "")[:10]

    def _n(k):
        return _f(r.get(k))

    return {
        "code": c,
        "date": date,
        "financing_balance": _n("RZYE"),          # 融资余额（元）
        "securities_balance": _n("RQYE"),         # 融券余额（元）
        "margin_balance": _n("RZRQYE"),           # 融资融券余额（元）
        "financing_pct_float_mcap": _n("RZYEZB"),  # 融资余额 / 流通市值（%）
        "financing_buy": _n("RZMRE"),             # 融资买入额（元）
        "financing_repay": _n("RZCHE"),           # 融资偿还额（元）
        "source": "东方财富 datacenter 融资融券明细",
    }


def get_market_snapshot(code: str) -> dict:
    """一站式：行情 + 融资融券。任一失败不影响另一项。"""
    return {
        "quote": get_quote(code),
        "margin": get_margin(code),
    }
