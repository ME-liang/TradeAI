"""东方财富端点：**主机池与统一取数封装的唯一定义处**。

为什么必须收敛到一处
--------------------
`frontend/server.py` 曾把 push2 域名硬编码在 10 个调用点上，一处被封则
列表 / 板块 / 成分 / 快照 / K 线 / 基本面指标同时失灵。8002 侧（报告生成）
有完全同型的问题：`board.py` 与 `industry.py` **各自**硬编码主机列表、
**各自**实现一遍 curl 封装。

2026-09-25 的事故就是这个后果：`board.py` 的池里只有 `push2delay` + `push2`，
**没有** `push2test`；当天这两台被限流、三台中只有 `push2test` 可达，于是
`fetch_members_full()` 返回空 → 板块报告直接失败
（「板块成员获取失败（bk=BK0890），无法生成报告」，见 N-18）。

可用性是「主机 × 路径 × 时效需求」三维的
-----------------------------------------
- **按「主机 + 路径」封锁，而非整站**：`push2delay` 的 `clist/get` 被重置时，
  同一个主机的 `stock/get` 可能仍然通。故冷却键取 `(host, path)`，不是 host。
- **时效需求**：`push2test` 只对**当前值**可信（clist 行情、stock/get 财务字段
  实测正确），其**历史 K 线是随机占位样本**（N-15）。K 线专用池在
  `frontend/server.py::KLINE_HOSTS`，**不含** push2test；本模块只服务
  当前的 clist / stock-get / F10 取数，故可把它放在末位兜底。

F10 接口的两个必填参数（第二个事故，N-19）
------------------------------------------
`datacenter*.eastmoney.com/securities/api/data/get` 的 `sty` 与 `filter`
**都是必填**，原实现只传 `type` + `secucode`：

- 缺 `sty` → `{"success": false, "message": "返回字段参数不能为空", "code": 9501}`，
  于是所有 F10 调用**恒返回空**，却被静默当成「这家公司没有该字段」继续跑。
  （写具体列名还会报 `XXX返回字段不存在`，故统一用 `ALL`。）
- 缺 `filter` → `secucode` 被忽略，返回**全表**（实测 24780 行）。这一条更危险：
  若只补 `sty` 不补 `filter`，`data[0]` 会拿到**第一家公司的数据**（实测是某银行），
  比返回空值错得更隐蔽。

历史误判：`board.py` 曾把 `RPT_F10_FINANCE_MAINFINADATA`（ROEJQ）的 0 行记成
「接口 2026-09 实测已失效」并换用别的报表绕开；实际原因就是缺 `sty`。
补上后 600519 返回 103 期数据、最新期 ROEJQ=16.75（2026-06-30）正确。
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.parse

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_REF = "https://quote.eastmoney.com/"

# push2 行情族（顺序即优先级；push2test 末位兜底，理由见模块 docstring）
PUSH2_HOSTS: tuple[str, ...] = (
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
    "https://push2test.eastmoney.com",
)

# F10 / 数据中心族（路径与字段体系与 push2 不同，单独一池；两个域名互为镜像）
DATACENTER_HOSTS: tuple[str, ...] = (
    "https://datacenter.eastmoney.com",
    "https://datacenter-web.eastmoney.com",
)

HOST_COOLDOWN_S = 90        # 与 frontend/server.py::MARKET_HOST_COOLDOWN 保持一致
CURL_TIMEOUT_PAD_S = 5      # subprocess 自身的兜底超时 = curl -m 值 + 该余量

# 冷却表：键为 (host, 端点路径)。路径维度不能省——封锁是按主机+路径的。
_down_until: dict[tuple[str, str], float] = {}
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 主机健康度
# ---------------------------------------------------------------------------
def path_key(path: str) -> str:
    """冷却键的路径维度：去掉查询串，只保留端点路径。"""
    return path.split("?", 1)[0]


def mark_down(host: str, path: str, reason: str = "") -> None:
    with _LOCK:
        _down_until[(host, path_key(path))] = time.time() + HOST_COOLDOWN_S
    logger.warning("[em] 主机降级 %s%s（%.0fs 内排到末位）：%s",
                   host, path_key(path), HOST_COOLDOWN_S, reason or "无响应")


def mark_up(host: str, path: str) -> None:
    with _LOCK:
        if _down_until.pop((host, path_key(path)), None) is not None:
            logger.info("[em] 主机恢复 %s%s", host, path_key(path))


def is_cooling(host: str, path: str = "") -> bool:
    with _LOCK:
        return _down_until.get((host, path_key(path)), 0.0) > time.time()


def ordered_hosts(hosts=None, path: str = "") -> list[str]:
    """按健康度排序：冷却中的主机排到**末尾**（不剔除——全冷却时仍需尝试）。

    这点很重要：若直接剔除冷却主机，全池冷却时会变成「无主机可用」而放弃；
    排在末尾则在仍有健康主机时完全跳过它们，省掉每页约 3.6s 的无效等待。
    `sorted` 稳定，同档主机保持输入顺序（即优先级顺序）。
    """
    hs = list(hosts or PUSH2_HOSTS)
    now = time.time()
    key = path_key(path)
    with _LOCK:
        rank = {h: 1 if _down_until.get((h, key), 0.0) > now else 0 for h in hs}
    return sorted(hs, key=lambda h: rank[h])


def cooling_report(hosts=None) -> dict[str, list[str]]:
    """当前冷却状态（运维/自检用）：{host: [仍在冷却的路径, ...]}。"""
    now = time.time()
    out: dict[str, list[str]] = {}
    with _LOCK:
        for (host, p), until in _down_until.items():
            if until > now and (hosts is None or host in hosts):
                out.setdefault(host, []).append(p)
    return out


def reset_health() -> None:
    """清空冷却表（测试与运维用）。"""
    with _LOCK:
        _down_until.clear()


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def curl_json(url: str, timeout: int = 10):
    """单 URL GET → JSON。任何失败（网络 / 超时 / 非 JSON）一律返回 None。

    用系统 curl 而非 urllib 是本项目既有约定：urllib 在本环境下对 push2 系列
    偶发「连接建立后不返回」（见 data/market.py 的说明）。
    """
    try:
        out = subprocess.check_output(
            ["curl", "-s", "-m", str(timeout), "-A", _UA, "-e", _REF, url],
            timeout=timeout + CURL_TIMEOUT_PAD_S,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[em] curl 失败 %s：%s", url, e)
        return None
    if not out.strip():
        return None
    try:
        return json.loads(out.decode("utf-8"))
    except Exception:
        return None


def fetch_host(host: str, path: str, timeout: int = 10):
    """向**指定**主机取数，并记录该主机在该路径上的健康度。

    与 `curl_json` 的区别是这里承担记账：调用方需要按主机逐一验payload
    （如「这一台返回的 diff 是否完整」）时用它，其余场景直接用 `get_json`。
    """
    d = curl_json(host.rstrip("/") + path, timeout=timeout)
    if d is None:
        mark_down(host, path)
    else:
        mark_up(host, path)
    return d


def get_json(path: str, timeout: int = 10, hosts=None):
    """逐主机降级取数：返回首个成功结果；全失败返回 None。"""
    for host in ordered_hosts(hosts, path):
        d = fetch_host(host, path, timeout=timeout)
        if d is not None:
            return d
    return None


# ---------------------------------------------------------------------------
# F10 / 数据中心报表
# ---------------------------------------------------------------------------
def secucode(code: str) -> str:
    """6 位代码 → 东财 F10 的 SECUCODE（600519 → 600519.SH）。"""
    c = "".join(ch for ch in str(code or "") if ch.isdigit())[-6:].zfill(6)
    return c + "." + ("SH" if c[:1] in ("6", "9")
                      else ("BJ" if c[:1] in ("4", "8") else "SZ"))


F10_TIMEOUT_S = 8


def f10_get(report_type: str, code: str, timeout: int = F10_TIMEOUT_S) -> list[dict]:
    """东财 F10 报表取数，返回 `result.data` 列表（失败返回空列表）。

    `sty` 与 `filter` 都是必填，理由见模块 docstring —— 不要「简化」掉任何一个。
    """
    q = urllib.parse.urlencode({
        "type": report_type,
        "sty": "ALL",
        "filter": '(SECUCODE="%s")' % secucode(code),
        "client": "PC",
        "source": "HSF10",
    })
    d = get_json("/securities/api/data/get?" + q, timeout=timeout,
                 hosts=DATACENTER_HOSTS)
    return ((d or {}).get("result") or {}).get("data") or []


def f10_profile(code: str) -> dict:
    """F10 公司概况 → `{main_business, scope, intro, industry}`（失败全为空串）。"""
    out = {"main_business": "", "scope": "", "intro": "", "industry": ""}
    try:
        data = f10_get("RPT_F10_BASIC_ORGINFO", code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[em] F10 画像获取失败(code=%s): %s", code, e)
        return out
    if not data:
        # 空结果必须留痕：静默为空正是上一轮「主路径从未生效却无人察觉」的成因
        logger.warning("[em] F10 画像返回空(code=%s)", code)
        return out
    r = data[0]
    out["main_business"] = str(r.get("MAIN_BUSINESS") or "")
    out["scope"] = str(r.get("BUSINESS_SCOPE") or "")[:400]
    out["intro"] = str(r.get("ORG_INTRODUCTION") or "")[:400]
    out["industry"] = str(r.get("INDUSTRY") or "")
    return out


def f10_roejq(code: str):
    """F10 主要财务指标加权 ROE（ROEJQ，最新报告期，单位 %）。失败返回 None。"""
    try:
        data = f10_get("RPT_F10_FINANCE_MAINFINADATA", code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[em] F10 ROEJQ 获取失败(code=%s): %s", code, e)
        return None
    if not data:
        return None
    try:
        v = data[0].get("ROEJQ")
        return None if v in (None, "-", "") else float(v)
    except (TypeError, ValueError):
        return None
