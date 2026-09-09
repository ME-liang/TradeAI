"""AkShare 数据源实现（真实 A 股数据，免费、无需 token）。

通过 AkShare 拉取：
- 财务分析指标（毛利率/净利率/ROE/资产负债率/周转率等，按报告期）
- 三大财务报表（利润表 / 资产负债表 / 现金流量表）
- 公司画像（行业 / 总市值）
- 行业可比公司指标（用于行业对标）

所有外部调用均做防御式处理：任一接口失败不影响整体，返回空值，
由上游引擎与渲染层回退到占位数据，保证报告始终可生成。
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from .cache import ttl_cache, throttle
from .client import DataClient
from .cninfo_guard import cninfo_guard


@contextlib.contextmanager
def _Silence():
    """抑制第三方库（baostock / akshare / tqdm）向 stdout、stderr 的噪声输出。

    这些库会打印 login success、分页进度条等内容，在 Web 服务与异步任务场景下
    会直接污染服务端日志并干扰进度排查。取数属于后台行为，不应产生终端输出。
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        yield


# OV-2（概况性能专项）：stock_gdfx_free_holding_detail_em 是全市场大表（单次 5-10s+），
# 按「报告期 → 全市场 DataFrame」当日缓存：候选期循环 / 同进程重复生成均只拉一次。
_GDFX_FREE_CACHE: dict = {}
_GDFX_FREE_LOCK = threading.Lock()

# 申万一级行业映射缓存（股票代码 -> {'l1': 申万一级, 'l3': 申万三级}）
_CACHE_DIR = Path(__file__).resolve().parent / "_cache"
_SW_L1_CACHE = _CACHE_DIR / "sw_l1_map.json"
_SW_CACHE_MAX_AGE = 7 * 24 * 3600  # 7 天

# 慢接口总开关：关闭时不调用实测耗时过长或无效的 AkShare 接口，
# 对应字段直接置空（上层展示为「未获取」），绝不让单个字段拖垮整条 pipeline。
# 逐个开关见各方法 docstring 中的「实测警告」。
ENABLE_SLOW_ENDPOINTS = False

# 网络硬超时（秒）：AkShare 多数接口未显式传 timeout，一旦上游（典型如被封禁的东财
# push2 端点）连接建立后不返回，socket 读会**无限期阻塞**，把整条报告 pipeline 挂死
# —— 实测曾卡在「拉取财报数据」阶段 20+ 分钟无进展，任务永远不 SUCCESS 也不 FAILED。
# 这里在客户端构造时兜底设置全局默认超时，把「挂死」降级为「超时异常」：各取数方法
# 已有 try/except，会退回空数据，报告照常生成、仅该字段缺失，远优于整份报告不可用。
# 说明：作用于单次 socket 操作，不是整次调用的总时长上限，故不影响多请求分页接口。
DEFAULT_SOCKET_TIMEOUT = 30.0

# 申万一级行业指数代码（2021 版，稳定不变）。`sw_index_first_info` 上游偶发解析失败，
# 故硬编码代码表；行业名称优先取自 sw_index_third_cons 的「申万1级」列，缺省用本表。
_SW_L1_CODES = {
    "801010": "农林牧渔", "801030": "采掘", "801040": "化工", "801050": "钢铁",
    "801080": "电子", "801110": "家用电器", "801120": "食品饮料", "801130": "纺织服装",
    "801140": "轻工制造", "801150": "医药生物", "801160": "公用事业", "801170": "交通运输",
    "801180": "房地产", "801200": "商业贸易", "801210": "休闲服务", "801230": "综合",
    "801710": "建筑材料", "801720": "建筑装饰", "801730": "电气设备", "801740": "国防军工",
    "801750": "计算机", "801760": "传媒", "801770": "通信", "801780": "银行",
    "801790": "非银金融", "801880": "汽车", "801890": "机械设备", "801950": "煤炭",
    "801960": "石油石化", "801970": "环保", "801980": "美容护理",
}

# 证监会 2012 行业分类（大类 2 位代码）-> 申万一级行业。
# 用于在 SW 精确映射缓存缺失时，基于 baostock 稳定返回的证监会代码即时给出申万一级。
# 注：个别制造业大类（如 C39 横跨 电子/通信/计算机）为近似映射，绝大多数股票准确。
_CSRC_TO_SW = {
    "A01": "农林牧渔", "A02": "农林牧渔", "A03": "农林牧渔", "A04": "农林牧渔", "A05": "农林牧渔",
    "B06": "煤炭", "B07": "石油石化", "B08": "钢铁", "B09": "有色金属", "B11": "采掘",
    "C13": "食品饮料", "C14": "食品饮料", "C15": "食品饮料", "C17": "纺织服装", "C18": "纺织服装",
    "C19": "纺织服装", "C20": "轻工制造", "C21": "轻工制造", "C22": "轻工制造", "C23": "轻工制造",
    "C24": "轻工制造", "C25": "石油石化", "C26": "化工", "C27": "医药生物", "C28": "化工",
    "C29": "化工", "C30": "建筑材料", "C31": "钢铁", "C32": "有色金属", "C33": "机械设备",
    "C34": "机械设备", "C35": "机械设备", "C36": "汽车", "C37": "国防军工", "C38": "电气设备",
    "C39": "电子", "C40": "机械设备", "C41": "轻工制造", "C42": "环保",
    "D44": "公用事业", "D45": "公用事业", "D46": "公用事业",
    "E47": "建筑装饰", "E48": "建筑装饰", "E50": "建筑装饰",
    "F51": "商业贸易", "F52": "商业贸易",
    "G53": "交通运输", "G54": "交通运输", "G55": "交通运输", "G56": "交通运输", "G57": "交通运输",
    "G58": "交通运输", "G59": "交通运输", "G60": "交通运输",
    "H61": "休闲服务", "H62": "休闲服务",
    "I63": "通信", "I64": "计算机", "I65": "计算机",
    "J66": "银行", "J67": "非银金融", "J68": "非银金融", "J69": "非银金融",
    "K70": "房地产",
    "L71": "商业贸易", "L72": "商业贸易",
    "M73": "综合", "M74": "综合", "M75": "综合",
    "N76": "环保", "N77": "环保", "N78": "环保",
    "O79": "综合", "O80": "综合", "O81": "综合",
    "P82": "综合",
    "Q83": "医药生物", "Q84": "综合",
    "R85": "传媒", "R86": "传媒", "R87": "传媒", "R88": "休闲服务", "R89": "传媒",
    "S90": "综合",
}


class AkShareClient(DataClient):
    def __init__(self, code: str = ""):
        self.code = code
        self._ak = None
        self._apply_socket_timeout()
        try:
            import akshare as ak  # 延迟导入：未安装时不影响 dummy 路径
            self._ak = ak
        except Exception:
            self._ak = None

    @staticmethod
    def _apply_socket_timeout() -> None:
        """杜绝取数无限期阻塞（见 DEFAULT_SOCKET_TIMEOUT）。

        两层防护：
        1) 进程级 socket 默认超时（兜底，覆盖裸 socket 用法）。
        2) 真正生效的关键层——**给 requests 注入默认 timeout**。
           AkShare 内部走 `requests` 且多数接口不显式传 timeout；`requests` 收到
           `timeout=None` 时会把底层 socket 设为阻塞模式，使 `socket.setdefaulttimeout`
           完全失效，这正是此前「拉取财报数据」卡死 20+ 分钟的根因。
           通过 monkey-patch `requests.Session.request`，对任意未显式带 timeout 的
           取数请求统一注入 `DEFAULT_SOCKET_TIMEOUT`，把「挂死」降级为「超时异常」——
           各取数方法已有 try/except，会退回空数据，报告照常生成、仅缺该字段，
           远优于整份报告不可用。
        幂等：重复构造客户端不重复 patch。
        """
        try:
            import socket

            current = socket.getdefaulttimeout()
            if current is None or current > DEFAULT_SOCKET_TIMEOUT:
                socket.setdefaulttimeout(DEFAULT_SOCKET_TIMEOUT)
        except Exception:
            pass

        try:
            import requests

            if getattr(requests.Session.request, "_wb_timeout_patched", False):
                return
            _orig = requests.Session.request

            def _patched(self, *args, **kwargs):
                kwargs.setdefault("timeout", DEFAULT_SOCKET_TIMEOUT)
                return _orig(self, *args, **kwargs)

            _patched._wb_timeout_patched = True
            requests.Session.request = _patched
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def available(self) -> bool:
        return self._ak is not None

    @staticmethod
    def _norm_code(code: str) -> str:
        """把用户输入规整为 6 位纯数字代码。"""
        return "".join(ch for ch in str(code) if ch.isdigit())[:6]

    def _exchange_prefix(self, code: str) -> str:
        """依据代码前缀返回交易所前缀（沪市 sh / 深市 sz）。"""
        code = self._norm_code(code)
        if not code:
            return "sh000000"
        if code[0] in ("6", "9"):
            return "sh" + code
        return "sz" + code

    @staticmethod
    def _to_float(value) -> Optional[float]:
        try:
            if value is None or pd.isna(value):
                return None
            return float(value)
        except (ValueError, TypeError):
            return None

    @throttle()
    def _info_em(self, code: str, tries: int = 3):
        """拉取个股基本信息（含行业/总市值），带重试以规避东财偶发限流。"""
        if self._ak is None:
            return None
        for _ in range(tries):
            try:
                info = self._ak.stock_individual_info_em(symbol=code)
                if info is not None and not info.empty:
                    return info
            except Exception:
                time.sleep(0.6)
        return None

    def _industry_baostock(self, code: str) -> str:
        """兜底：东财接口被封时，用 baostock 取证监会行业分类（独立上游）。"""
        try:
            import baostock as bs

            lg = bs.login()
            if lg.error_code != "0":
                bs.logout()
                return ""
            rs = bs.query_stock_industry(code=code[:6] and f"{'sh' if code[0] in '69' else 'sz'}.{code[:6]}")
            ind = ""
            if rs.error_code == "0":
                row = rs.get_row_data()
                if row:
                    d = dict(zip(rs.fields, row))
                    ind = (d.get("industry") or "").strip()
            bs.logout()
            # 形如 'C15酒、饮料和精制茶制造业'：去掉前缀行业代码
            return re.sub(r"^[A-Z]\d+", "", ind).strip()
        except Exception:
            return ""

    def _csrc_code_from_baostock(self, code: str) -> str:
        """用 baostock 取证监会行业大类代码（如 'C15'），失败返回空串。"""
        try:
            import baostock as bs

            lg = bs.login()
            if lg.error_code != "0":
                bs.logout()
                return ""
            rs = bs.query_stock_industry(code=code[:6] and f"{'sh' if code[0] in '69' else 'sz'}.{code[:6]}")
            ind = ""
            if rs.error_code == "0":
                row = rs.get_row_data()
                if row:
                    d = dict(zip(rs.fields, row))
                    ind = (d.get("industry") or "").strip()
            bs.logout()
            m = re.match(r"^([A-Z]\d{2})", ind)
            return m.group(1) if m else ""
        except Exception:
            return ""

    @staticmethod
    def _csrc_to_sw(csrc_code: str) -> str:
        """证监会大类代码 -> 申万一级行业（近似映射，绝大多数准确）。"""
        return _CSRC_TO_SW.get(csrc_code, "")

    # ------------------------------------------------------------------
    # 公司画像
    # ------------------------------------------------------------------
    def resolve_code(self, name: str) -> str:
        """根据公司名称模糊匹配返回 6 位代码（失败返回空串）。"""
        if not name or self._ak is None:
            return ""
        try:
            nm = self._ak.stock_info_a_code_name()
            if nm is not None and not nm.empty:
                hit = nm[nm["name"].astype(str).str.contains(str(name), na=False)]
                if not hit.empty:
                    return str(hit.iloc[0]["code"])
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # 申万一级行业（AkShare sw_index 系列，非东财源，本沙箱可用）
    # ------------------------------------------------------------------
    def _sw_cons_retry(self, symbol: str, tries: int = 3):
        """拉取申万某一级指数成分股，带重试以规避上游偶发失败。"""
        if self._ak is None:
            return None
        for _ in range(tries):
            try:
                df = self._ak.sw_index_third_cons(symbol=symbol)
                if df is not None:
                    return df
            except Exception:
                time.sleep(0.5)
        return None

    def _build_sw_l1_map(self) -> dict:
        """遍历 31 个申万一级指数成分股，构建 股票代码 -> {l1, l3} 映射。

        使用 AkShare `sw_index_third_cons`（申万官方指数，上游非东方财富），
        每行自带「申万1级」列，返回形如 {'600519': {'l1': '食品饮料', 'l3': ''}}。
        逐代码容错：单个行业失败不影响其余；请求间加延迟规避上游限流。
        """
        if self._ak is None:
            return {}
        mapping: dict = {}
        for l1_code, fallback_name in _SW_L1_CODES.items():
            try:
                cons = self._sw_cons_retry(f"{l1_code}.SI")
                if cons is None or cons.empty:
                    time.sleep(0.2)
                    continue
                # 行业名称优先取自成分股行的「申万1级」列，缺省用硬编码表
                l1_name = fallback_name
                if "申万1级" in cons.columns and cons["申万1级"].notna().any():
                    first = cons["申万1级"].dropna().iloc[0]
                    if str(first).strip():
                        l1_name = str(first).strip()
                for _, c in cons.iterrows():
                    sc = "".join(ch for ch in str(c.get("股票代码", "")) if ch.isdigit())[:6]
                    if not sc:
                        continue
                    mapping[sc] = {
                        "l1": l1_name,
                        "l3": str(c.get("申万3级", "") or "").strip(),
                    }
            except Exception:
                pass
            time.sleep(0.25)  # 请求间延迟，避免被上游限流强杀
        return mapping

    def _load_sw_l1_map(self) -> dict:
        """加载申万一级映射：优先用 7 天内的本地缓存，失效/缺失则重建并写回。

        完整性自检：A 股约 5000+ 只，缓存 <4000 条说明某次构建时部分行业失败
        （残缺映射会让大量公司 sw_map miss → 同业对比整模块为空，实测 2843 条
        时 600519 miss）。残缺缓存触发重建，重建结果与旧缓存取更大者。
        """
        cached: dict = {}
        try:
            if _SW_L1_CACHE.exists():
                age = time.time() - _SW_L1_CACHE.stat().st_mtime
                if age < _SW_CACHE_MAX_AGE:
                    cached = json.loads(_SW_L1_CACHE.read_text(encoding="utf-8"))
                    if isinstance(cached, dict) and len(cached) >= 4000:
                        return cached
        except Exception:
            pass
        # 重建限时 90s（守护线程）：申万上游偶发 socket 阻塞曾让报告生成挂 15 分钟+
        # （index_component_sw/sw_index_third_cons 无超时保护）。超时放弃重建，
        # 用旧缓存（残缺映射由独立工具 tools/rebuild_sw_map.py 在源可用时补齐）。
        mapping: dict = {}
        _t = threading.Thread(
            target=lambda: mapping.update(self._build_sw_l1_map()), daemon=True
        )
        _t.start()
        _t.join(90)
        best = mapping if len(mapping) > len(cached) else cached
        if best:
            try:
                _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                _SW_L1_CACHE.write_text(
                    json.dumps(best, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:
                pass
        return best

    def _industry_sw(self, code: str) -> str:
        """返回申万一级行业文本（优先带三级，如 '食品饮料（白酒）'）。

        仅读取本地缓存的 SW 精确映射；不在线构建（避免 31 次上游请求拖慢报告生成）。
        缓存缺失时返回空串，由调用方退化为证监会代码→申万一级静态映射。
        """
        code = self._norm_code(code)
        try:
            if _SW_L1_CACHE.exists():
                m = json.loads(_SW_L1_CACHE.read_text(encoding="utf-8")).get(code)
                if m:
                    l1 = m.get("l1", "")
                    l3 = m.get("l3", "")
                    if l1 and l3 and l3 != l1:
                        return f"{l1}（{l3}）"
                    return l1
        except Exception:
            pass
        return ""

    def get_company_profile(self, code: str) -> dict:
        code = self._norm_code(code)
        profile: dict = {"code": code}
        if self._ak is None:
            return profile
        # 1) 申万一级行业（优先：本地 SW 精确映射缓存，若存在）
        profile["industry"] = self._industry_sw(code)
        # 2) 退而用 证监会大类代码 -> 申万一级 静态映射（即时、零额外网络）
        if not profile.get("industry"):
            csrc = self._csrc_code_from_baostock(code)
            if csrc:
                profile["industry"] = self._csrc_to_sw(csrc)
        # 3) 仍无：东财个股画像行业（被封时为空）/ 证监会行业名称兜底
        if not profile.get("industry"):
            info = self._info_em(code)
            if info is not None and not info.empty:
                d = dict(zip(info["item"], info["value"]))
                profile["industry"] = str(d.get("行业", "")).strip()
        if not profile.get("industry"):
            profile["industry"] = self._industry_baostock(code)
        # 名称 / 市值等补充（东财，被封时为空不影响其余字段）
        info = self._info_em(code)
        if info is not None and not info.empty:
            d = dict(zip(info["item"], info["value"]))
            profile["name"] = str(d.get("股票简称", "")).strip()
            profile["total_mv"] = d.get("总市值")
            profile["float_mv"] = d.get("流通市值")
            profile["list_date"] = d.get("上市时间")
        # 东财不可用（实测当前环境返回空）时，用 baostock 补上市日期
        if not profile.get("list_date"):
            profile["list_date"] = self._list_date_from_baostock(code)
        # 兜底名称：用代码→名称映射表
        if not profile.get("name"):
            try:
                name_map = self._ak.stock_info_a_code_name()
                if name_map is not None and not name_map.empty:
                    row = name_map[name_map["code"] == code]
                    if not row.empty:
                        profile["name"] = str(row.iloc[0]["name"]).strip()
            except Exception:
                pass
        return profile

    # ------------------------------------------------------------------
    # 公司概况六层框架：新增取数口（数据来自 cninfo / 同花顺 / 东财可用端点）
    # 所有方法防御式返回空值（dict / DataFrame），绝不让单接口失败拖垮 pipeline。
    # ------------------------------------------------------------------
    @throttle()
    def _gdfx_free_holding(self, date: str) -> Optional[pd.DataFrame]:
        """全市场前十大流通股东明细（OV-2：按报告期当日缓存）。

        同报告期同进程只拉一次；结果（含空表，口径稳定）缓存，异常不缓存以便重试。
        """
        with _GDFX_FREE_LOCK:
            if date in _GDFX_FREE_CACHE:
                return _GDFX_FREE_CACHE[date]
        df = self._ak.stock_gdfx_free_holding_detail_em(date=date)
        with _GDFX_FREE_LOCK:
            _GDFX_FREE_CACHE[date] = df
        return df

    def get_holders(self, code: str) -> pd.DataFrame:
        """03 股权治理：前十大（流通）股东。

        实测：stock_gdfx_top_10_em 在当前环境抛 KeyError('sdgd')（push2 封禁），
        改用 stock_gdfx_free_holding_detail_em（同域名族、可用），按股票代码过滤取前十大。
        返回列对齐基类契约：名次/股东名称/持股数/占总股本持股比例/增减/变动比率。
        """
        code = self._norm_code(code)
        if self._ak is None:
            return pd.DataFrame()
        # 由近及远尝试最近的几个报告期，取首个有本股数据的。
        # 2026-09-09 收敛为 3 个候选：本接口每次拉取**全市场**大表（单次 5-10s），
        # 6 个候选期逐次拉取实测 39s；概况报告中 holders 仅为名册缺失时的兜底口径
        # （主源是定期报告股东名册），保留最近 3 期足以覆盖绝大多数公司。
        year = pd.Timestamp.now().year
        candidates = [f"{year}0630", f"{year}0331", f"{year-1}1231"]
        try:
            with _Silence():
                for dt in candidates:
                    try:
                        df = self._gdfx_free_holding(dt)
                    except Exception:
                        continue
                    if df is None or df.empty:
                        continue
                    sub = df[df.get("股票代码") == code]
                    if sub is None or sub.empty:
                        continue
                    sub = sub.copy()
                    sub["名次"] = range(1, len(sub) + 1)
                    sub = sub.rename(columns={
                        "股东名称": "股东名称",
                        "期末持股-数量": "持股数",
                        "期末持股-持股变动": "增减",
                        "期末持股-数量变化比例": "变动比率",
                        "报告期": "报告期",
                    })
                    keep = ["名次", "股东名称", "报告期", "持股数", "增减", "变动比率"]
                    return sub[[c for c in keep if c in sub.columns]].reset_index(drop=True)
        except Exception:
            pass
        return pd.DataFrame()

    @throttle()
    def get_dividend(self, code: str) -> pd.DataFrame:
        """03 股权治理：分红送配（cninfo 历史分红）。"""
        code = self._norm_code(code)
        if self._ak is None:
            return pd.DataFrame()
        try:
            # cninfo 接口内部用 py_mini_racer 新建 V8 isolate, 多线程并发会 FATAL 崩进程,
            # 必须全局串行（见 data/cninfo_guard.py 的复现证据）
            with _Silence(), cninfo_guard():
                df = self._ak.stock_dividend_cninfo(symbol=code)
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.copy()
            df = df.rename(columns={
                "报告时间": "报告期",
                "派息比例": "现金分红比例",
                "股权登记日": "股权登记日",
                "除权日": "除权除息日",
                "实施方案分红说明": "方案说明",
                "分红类型": "分红类型",
            })
            keep = ["报告期", "分红类型", "现金分红比例", "股权登记日", "除权除息日", "方案说明"]
            return df[[c for c in keep if c in df.columns]].reset_index(drop=True)
        except Exception:
            return pd.DataFrame()

    @throttle()
    def get_news(self, code: str) -> pd.DataFrame:
        """06 动态风险：近期公告（cninfo 个股公告列表）。

        用作概况「动态与风险」层的事件源；返回列：公告日期/公告标题/公告类型/网址。
        """
        code = self._norm_code(code)
        if self._ak is None:
            return pd.DataFrame()
        end = pd.Timestamp.now()
        begin = end - pd.DateOffset(years=1)
        try:
            with _Silence():
                df = self._ak.stock_individual_notice_report(
                    security=code, symbol="全部",
                    begin_date=begin.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                )
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.copy()
            df = df.rename(columns={
                "公告日期": "公告日期",
                "公告标题": "公告标题",
                "公告类型": "公告类型",
                "网址": "网址",
            })
            keep = ["公告日期", "公告标题", "公告类型", "网址"]
            return df[[c for c in keep if c in df.columns]].reset_index(drop=True)
        except Exception:
            return pd.DataFrame()

    @ttl_cache(3600)
    def get_main_business(self, code: str) -> pd.DataFrame:
        """02 业务画像：主营构成（按产品/行业/地区拆分）。

        真实可靠源：东方财富主营构成接口 `stock_zygc_em`（emweb.securities.eastmoney.com，
        非 push2 端点，稳定可用）。返回多报告期、多分类口径的分部收入/成本/利润/毛利率。
        概况「业务板块全景」主线取最新报告期、按产品分类的数据。
        """
        code = "".join(ch for ch in str(code) if ch.isdigit())[:6]
        if self._ak is None or not code:
            return pd.DataFrame()
        # 东财 zygc 接口要求带市场前缀（SZ/SH/BJ）
        mkt = "SH" if code[0] == "6" else ("BJ" if code[0] in ("8", "4") else "SZ")
        try:
            with _Silence():
                df = self._ak.stock_zygc_em(symbol=mkt + code)
            if df is None or df.empty:
                return pd.DataFrame()
            keep = ["股票代码", "报告日期", "分类类型", "主营构成", "主营收入",
                    "收入比例", "主营成本", "成本比例", "主营利润", "利润比例", "毛利率"]
            keep = [c for c in keep if c in df.columns]
            df = df[keep].copy()
            if "报告日期" in df.columns:
                # 转字符串，避免 date 对象在序列化/排序时出问题
                df["报告日期"] = df["报告日期"].astype(str)
            return df
        except Exception:
            return pd.DataFrame()

    @ttl_cache(3600)
    def get_pledge(self, code: str) -> pd.DataFrame:
        """03 股权治理：股权质押明细（逐股东、逐笔）。

        数据源：AkShare `stock_gpzy_individual_pledge_ratio_detail_em`（东方财富股权质押明细，
        非 push2 封禁端点，已实测可用）。入参为 6 位纯数字代码（带市场前缀会触发解析异常，故先归一）。

        返回归一化 DataFrame（列为 English key，便于上层序列化与 LLM 消费），失败时返回空
        DataFrame（上层标记「未获取」，绝不抛错）。列：
        shareholder / pledge_shares / pct_of_holding / pct_of_total / institution /
        start_date / end_date / status / announce_date
        status ∈ {已解押, 未解押}。
        """
        code = "".join(ch for ch in str(code) if ch.isdigit())[:6]
        if self._ak is None or not code:
            return pd.DataFrame()
        try:
            with _Silence():
                df = self._ak.stock_gpzy_individual_pledge_ratio_detail_em(symbol=code)
            if df is None or df.empty:
                return pd.DataFrame()
            rename = {
                "股东名称": "shareholder",
                "质押股份数量": "pledge_shares",
                "占所持股份比例": "pct_of_holding",
                "占总股本比例": "pct_of_total",
                "质押机构": "institution",
                "质押开始日期": "start_date",
                "质押结束日期": "end_date",
                "状态": "status",
                "公告日期": "announce_date",
            }
            keep = {k: v for k, v in rename.items() if k in df.columns}
            df = df.rename(columns=keep)
            out_cols = ["shareholder", "pledge_shares", "pct_of_holding", "pct_of_total",
                        "institution", "start_date", "end_date", "status", "announce_date"]
            out_cols = [c for c in out_cols if c in df.columns]
            return df[out_cols].copy()
        except Exception:
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 01 身份识别（公司概况六层框架）
    # ------------------------------------------------------------------
    @staticmethod
    def _infer_board(code: str) -> str:
        """依据代码前缀推断上市板块（纯本地推断，零网络开销）。"""
        code = "".join(ch for ch in str(code) if ch.isdigit())[:6]
        if not code:
            return ""
        if code.startswith("688"):
            return "科创板"
        if code.startswith(("300", "301")):
            return "创业板"
        if code.startswith(("8", "4")):
            return "北交所"
        if code.startswith("9"):
            return "B股"
        if code.startswith("6"):
            return "沪市主板"
        if code.startswith("0"):
            return "深市主板"
        return ""

    def _holders_count(self, code: str) -> Optional[int]:
        """股东户数（`stock_zh_a_gdhs` 最新一期），失败或已禁用时返回 None。

        实测警告：该接口单次调用约 90 秒（内部遍历 800+ 项分页），且在当前
        akshare 版本下返回空 DataFrame —— 又慢又拿不到数据。放在 pipeline 里
        会让单次报告凭空多等一分半，故默认关闭（见 ENABLE_SLOW_ENDPOINTS）。
        二期若找到更快的替代源（如东财股东户数直连）再启用。

        列名在不同 akshare 版本下可能带后缀（如 '股东户数-总户数'），
        故按包含匹配查找，避免硬编码列名导致版本升级即失效。
        """
        if self._ak is None or not ENABLE_SLOW_ENDPOINTS:
            return None
        try:
            df = self._ak.stock_zh_a_gdhs(symbol=code)
            if df is None or df.empty:
                return None
            hits = [c for c in df.columns if "股东户数" in str(c)]
            if not hits:
                return None
            series = pd.to_numeric(df[hits[0]], errors="coerce").dropna()
            if series.empty:
                return None
            return int(series.iloc[-1])
        except Exception:
            return None

    def _list_date_from_baostock(self, code: str) -> str:
        """备用源：用 baostock `query_stock_basic` 取上市日期（ipoDate）。

        东财 `stock_individual_info_em` 不可用时（实测当前环境返回空），
        这是唯一稳定的上市日期来源；baostock 在项目中已用于行业兜底，可用性已验证。
        失败返回空串。

        注：雪球 `stock_individual_basic_info_xq` 需 xq_a_token，匿名访问必失败，
        故不作为备用源。
        """
        try:
            import baostock as bs

            prefixed = f"{'sh' if code[0] in '69' else 'sz'}.{code}"
            with _Silence():
                lg = bs.login()
                if lg.error_code != "0":
                    bs.logout()
                    return ""
                rs = bs.query_stock_basic(code=prefixed)
                date = ""
                if rs.error_code == "0":
                    row = rs.get_row_data()
                    if row:
                        d = dict(zip(rs.fields, row))
                        date = str(d.get("ipoDate") or "").strip()
                bs.logout()
            return date
        except Exception:
            return ""

    @ttl_cache(3600)
    def get_identity(self, code: str) -> dict:
        """01 身份识别：公司基础画像。

        数据来源（防御式，单点失败不影响其余字段）：
        - get_company_profile：名称 / 行业(申万) / 上市时间 / 市值（baostock + 东财兜底）
        - stock_profile_cninfo：cninfo 个股资料，覆盖主营业务 / 法人 / 注册地 / 成立日期 / 官网 等
        - _infer_board：上市板块（本地推断）
        - _holders_count：股东户数（默认禁用，慢且空）
        """
        code = self._norm_code(code)
        identity: dict = {
            "code": code,
            "name": "",
            "industry": "",
            "industry_cninfo": "",
            "board": self._infer_board(code),
            "list_date": "",
            "total_mv": None,
            "float_mv": None,
            "holders_count": None,
            "employees": None,           # 无接口，二期补
            "controller": None,          # 无接口，用第一大股东近似
            "controller_basis": "未获取（AkShare 无实控人字段，展示时以第一大股东近似并标注口径）",
            # cninfo 丰富字段（主营业务 / 法人 / 注册地 等）
            "short_name": "", "en_name": "", "market": "", "legal_rep": "",
            "reg_capital": "", "found_date": "", "indices": "", "website": "",
            "reg_addr": "", "office_addr": "", "main_business": "",
            "biz_scope": "", "org_profile": "",
        }
        # 复用现有画像：名称 / 行业 / 上市时间 / 市值（baostock + 东财兜底）
        try:
            profile = self.get_company_profile(code) or {}
            for key in ("name", "industry", "list_date", "total_mv", "float_mv"):
                if profile.get(key):
                    identity[key] = profile[key]
        except Exception:
            pass

        # cninfo 个股资料（稳定可用，覆盖主营业务 / 法人 / 注册地等）
        if self._ak is not None:
            try:
                # cninfo 系接口依赖 py_mini_racer(MiniRacer/V8), 并发即可能 FATAL 崩进程
                # （data/cninfo_guard.py 有复现证据），故整段串行。
                with cninfo_guard():
                    with _Silence():
                        cdf = self._ak.stock_profile_cninfo(symbol=code)
                    if cdf is not None and not cdf.empty:
                        d = cdf.iloc[0].to_dict()
                        mk = lambda k: (str(d.get(k, "")).strip() if d.get(k) not in (None, "") else "")
                        identity["short_name"] = mk("A股简称")
                        identity["en_name"] = mk("英文名称")
                        identity["market"] = mk("所属市场")
                        identity["industry_cninfo"] = mk("所属行业")
                        identity["legal_rep"] = mk("法人代表")
                        identity["reg_capital"] = mk("注册资金")
                        identity["found_date"] = mk("成立日期")
                        identity["indices"] = mk("入选指数")
                        identity["website"] = mk("官方网站")
                        identity["reg_addr"] = mk("注册地址")
                        identity["office_addr"] = mk("办公地址")
                        identity["main_business"] = mk("主营业务")
                        identity["biz_scope"] = mk("经营范围")
                        identity["org_profile"] = mk("机构简介")
                        if not identity["name"]:
                            identity["name"] = mk("公司名称")
            except Exception:
                pass

        # 股东户数：接口存在但实测约 90s 且返回空，默认禁用（见 ENABLE_SLOW_ENDPOINTS）
        identity["holders_count"] = self._holders_count(code)
        return identity

    # ------------------------------------------------------------------
    # 财务分析指标（核心比率）
    # ------------------------------------------------------------------
    def get_financial_indicators(self, code: str) -> pd.DataFrame:
        code = self._norm_code(code)
        if self._ak is None:
            return pd.DataFrame()
        try:
            df = self._ak.stock_financial_analysis_indicator(symbol=code)
            if df is None or df.empty:
                return pd.DataFrame()
            if "日期" in df.columns:
                df = df.copy()
                df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
                df = df.dropna(subset=["日期"]).sort_values("日期").reset_index(drop=True)
            return df
        except Exception:
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 三大报表（行=科目，列=报告期）
    # ------------------------------------------------------------------
    def _statement_sina(self, code: str, report: str) -> pd.DataFrame:
        """拉取 Sina 报表并转置为 行=科目 / 列=报告期。"""
        if self._ak is None:
            return pd.DataFrame()
        try:
            df = self._ak.stock_financial_report_sina(
                stock=self._exchange_prefix(code), symbol=report
            )
            if df is None or df.empty:
                return pd.DataFrame()
            if "报告日" in df.columns:
                df = df.set_index("报告日")
            df = df.apply(pd.to_numeric, errors="coerce")
            df = df.loc[:, ~df.columns.duplicated()]  # 列去重（科目）
            out = df.T  # index=科目, columns=报告期
            out = out.loc[:, ~out.columns.duplicated()]  # 报告期去重（保留末次）
            out.index.name = "科目"
            return out
        except Exception:
            return pd.DataFrame()

    def get_income_statement(self, code: str) -> pd.DataFrame:
        return self._statement_sina(code, "利润表")

    def get_balance_sheet(self, code: str) -> pd.DataFrame:
        return self._statement_sina(code, "资产负债表")

    def get_cash_flow(self, code: str) -> pd.DataFrame:
        return self._statement_sina(code, "现金流量表")

    @ttl_cache(3600)
    def get_share_capital(self, code: str) -> int:
        """当前总股本（股）。

        来源：东财股东户数详情（stock_zh_a_gdhs_detail_em，含「总股本」列，非 push2 端点）。
        用途：把持股比例、质押占比统一折算到**当前**总股本口径。

        为何必须取当前总股本：质押明细里的「占总股本比例(pct_of_total)」是**质押当时**
        的总股本口径。本公司历史总股本从 2012 年的约 6,700 万股增长到当前的约 11.49 亿股
        （增发/重组所致），直接把各笔 pct_of_total 相加会显著高估在押比例。
        失败返回 0，由上层退回原口径。
        """
        code = self._norm_code(code)
        if self._ak is None or not code:
            return 0
        try:
            with _Silence():
                df = self._ak.stock_zh_a_gdhs_detail_em(symbol=code)
            if df is None or df.empty or "总股本" not in df.columns:
                return 0
            s = pd.to_numeric(df["总股本"], errors="coerce").dropna()
            if s.empty:
                return 0
            return int(s.iloc[-1])
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # 行业可比公司
    # ------------------------------------------------------------------
    def get_industry_peers(self, code: str) -> pd.DataFrame:
        """返回行业可比公司的关键指标中位数等（列=公司，行=指标）。

        列=可比公司，行=指标名；同时附带一行 '资产负债率(%)' 等。
        失败时返回空 DataFrame，由 benchmark 引擎回退。
        """
        code = self._norm_code(code)
        if self._ak is None:
            return pd.DataFrame()
        try:
            # 1) 找到公司所属行业板块
            boards = self._ak.stock_board_industry_name_em()
            if boards is None or boards.empty:
                return pd.DataFrame()
            # 用个股所属行业：从公司画像拿不到行业代码时，退化为全 A 采样
            profile = self.get_company_profile(code)
            industry = profile.get("industry", "")
            cons = None
            if industry:
                hit = boards[boards["板块名称"] == industry]
                if not hit.empty:
                    cons = self._ak.stock_board_industry_cons_em(symbol=hit.iloc[0]["板块名称"])
            if cons is None or cons.empty:
                # 退化：取同行业前若干只
                cons = boards.head(20)
                cons = self._ak.stock_board_industry_cons_em(symbol=cons.iloc[0]["板块名称"])
            if cons is None or cons.empty:
                return pd.DataFrame()

            # 2) 对每只成分股拉取最新财务分析指标，抽关键比率
            rows = {}
            for _, r in cons.iterrows():
                pcode = str(r.get("代码", ""))
                if not pcode or pcode == code:
                    continue
                idf = self.get_financial_indicators(pcode)
                if idf is None or idf.empty:
                    continue
                last = idf.iloc[-1]
                for col in [
                    "销售毛利率(%)", "销售净利率(%)", "净资产收益率(%)",
                    "主营业务收入增长率(%)", "资产负债率(%)", "存货周转率(次)",
                ]:
                    if col in idf.columns:
                        rows.setdefault(col, {})[pcode] = self._to_float(last.get(col))
                if len(rows.get("净资产收益率(%)", {})) >= 5:
                    break
            if not rows:
                return pd.DataFrame()
            return pd.DataFrame(rows)  # index=公司代码, columns=指标
        except Exception:
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 公司概况报告：同业可比（仅依赖稳定源：申万指数官网 index_component_sw + 财务分析指标）
    # ------------------------------------------------------------------
    # 申万二级代码回退表：同花顺行业接口（sw_index_second_info / sw_index_third_cons 等）
    # 偶发不可用时，用于按二级精确定位同业。仅收录已验证的少数一级行业；未收录则退化为同申万一级。
    _SW_L2_FALLBACK = {
        "801080": ["801081", "801082", "801083"],  # 电子：半导体 / 其他电子Ⅱ / 元件
    }

    def _component_sw_retry(self, symbol: str, tries: int = 3):
        """拉取申万指数成分（申万指数官网，稳定源），带重试。"""
        if self._ak is None:
            return None
        for _ in range(tries):
            try:
                df = self._ak.index_component_sw(symbol=symbol)
                if df is not None:
                    return df
            except Exception:
                time.sleep(0.5)
        return None

    def get_industry_peers_overview(self, code: str, top_n: int = 8) -> list:
        """公司概况报告用的「同业可比」列表（仅依赖稳定源，规避东财 push2 / 同花顺偶发封禁）。

        数据源：
        - 同业范围：AkShare `index_component_sw(<申万指数代码>)`（申万指数官网，稳定可用），
          先用本地缓存的 股票→申万一级 映射确定一级；优先按「申万二级」精确定位——
          通过 _SW_L2_FALLBACK 中的硬编码二级代码逐个 index_component_sw 校验成员归属；
          若无可判定二级（如该一级未收录或上游偶发异常），退化为同申万一级全部成分。
        - 同业财务指标：`get_financial_indicators`（东方财富财务分析，非 push2 封禁端点），
          取最新报告期：毛利率 / ROE / 营收增速 / 净利润增速 / 资产负债率。

        返回 list[dict]，键与概况报告 peers 表一一对应：
        {name, code, gross_margin, revenue, roe, rev_growth, profit_growth, debt_ratio, strong, hl}
        失败时返回空 list（上层展示为「未获取」，绝不抛错）。
        """
        code = self._norm_code(code)
        if self._ak is None:
            return []
        try:
            sw_map = self._load_sw_l1_map()
            info = sw_map.get(code) or {}
            l1_name = info.get("l1", "")
            if not l1_name:                       # 缓存缺失兜底：现场构建
                sw_map = self._build_sw_l1_map()
                l1_name = (sw_map.get(code) or {}).get("l1", "")
            if not l1_name:
                return []
            l1_code = next((c for c, n in _SW_L1_CODES.items() if n == l1_name), None)
            if not l1_code:
                return []

            # 1) 定位公司所在申万二级，得到同业池（稳定源 index_component_sw）
            sub = self._resolve_l2_constituents(code, l1_code)
            if sub is None or sub.empty:
                sub = self._component_sw_retry(l1_code)   # 退化：同申万一级全部成分
            if sub is None or sub.empty or "证券代码" not in sub.columns:
                return []
            sub = sub.copy()
            sub["_code"] = sub["证券代码"].astype(str).str.replace(r"\.\w+$", "", regex=True)
            # 注意：**不剔除自身**——同业对比必须包含本公司行（hl/strong 高亮），
            # 否则报告无法回答「我在同业中排第几」。

            # 2) 按权重降序取前 top_n（top_n 同时约束后续财务拉取次数，避免拖慢报告）
            if "最新权重" in sub.columns:
                sub["_w"] = pd.to_numeric(sub["最新权重"], errors="coerce")
                sub = sub.sort_values("_w", ascending=False, na_position="last")
            sub = sub.head(top_n)
            # 自身若不在权重前 top_n 内，强制插入（同业对比缺自己无意义）
            if code not in set(sub["_code"]):
                self_name = (sw_map.get(code) or {}).get("name") or code
                sub = pd.concat(
                    [pd.DataFrame([{"_code": code, "证券名称": self_name}]), sub],
                    ignore_index=True,
                )

            # 3) 逐家拉取财务指标（单家失败不影响其余）
            out = []
            for _, r in sub.iterrows():
                pcode = str(r.get("_code", "")).strip()
                rec = {
                    "name": str(r.get("证券名称", "")).strip(),
                    "code": pcode,
                    "gross_margin": None,
                    "revenue": None,
                    "roe": "",
                    "rev_growth": "",
                    "profit_growth": "",
                    "debt_ratio": "",
                    "strong": pcode == code,
                    "hl": pcode == code,
                    "curated": False,
                }
                try:
                    rec["gross_margin"] = self.get_gross_margin(pcode)
                except Exception:
                    rec["gross_margin"] = None
                try:
                    rec["revenue"] = self.get_revenue_yi(pcode)
                except Exception:
                    rec["revenue"] = None
                ind = self.get_financial_indicators(pcode)
                if ind is not None and not ind.empty and "日期" in ind.columns:
                    row = ind.iloc[-1]
                    rec["roe"] = self._fmt_metric(row.get("净资产收益率(%)"))
                    rec["rev_growth"] = self._fmt_metric(row.get("主营业务收入增长率(%)"))
                    rec["profit_growth"] = self._fmt_metric(row.get("净利润增长率(%)"))
                    rec["debt_ratio"] = self._fmt_metric(row.get("资产负债率(%)"))
                out.append(rec)
            # 本公司行置顶（其余保持权重降序），保证「自己 vs 同业」的位置感
            out.sort(key=lambda r: 0 if r["code"] == code else 1)
            return out
        except Exception:
            return []

    # 业务可比公司（人工精选）：同业可比的正确口径是「同商业模式」，不是「同申万二级」。
    # 力源信息是**电子元器件授权分销商**，申万二级归入「其他电子Ⅱ」，而该行业成分里混有
    # 旭光电子（真空器件）、沃尔核材（高分子材料）等商业模式完全不同的公司——用它们做
    # 对比得出的「行业位置」没有意义（此前报告正是因此混入了非分销商）。
    # 故按业务实质精选授权分销商可比集；未收录的公司退化为申万二级口径。
    _CURATED_PEERS = {
        "300184": [
            ("000062", "深圳华强"), ("300975", "商络电子"), ("301099", "雅创电子"),
            ("300493", "润欣科技"), ("300131", "英唐智控"), ("001298", "好上好"),
            ("300475", "香农芯创"), ("001287", "中电港"),
        ],
    }

    @ttl_cache(3600)
    def get_gross_margin(self, code: str):
        """最新报告期毛利率(%)——自算：(营业总收入 − 营业成本) / 营业总收入。

        为何不直接取「销售毛利率(%)」列：该列在半年报期（实测 2026-06-30）对**全行业
        系统性返回 NaN**（深圳华强 / 商络电子 / 力源信息 均 NaN），而同期的销售净利率、
        ROE 均有值。毛利率是分销商最重要的横向对比指标，缺不得，故用利润表自算——
        只需「营业总收入」「营业成本」两行，不依赖该列。

        失败 / 缺失返回 None（绝不返回 0，避免把缺失显示成 0% 毛利）。
        """
        code = self._norm_code(code)
        inc = self.get_income_statement(code)
        if inc is None or getattr(inc, "empty", True):
            return None
        cols = list(inc.columns)
        if not cols:
            return None
        col = cols[0]

        def _v(row):
            try:
                f = float(inc.loc[row, col])
                return None if f != f else f  # NaN -> None
            except Exception:
                return None

        rev = None
        for row in ("营业总收入", "营业收入"):
            if row in inc.index:
                rev = _v(row)
                if rev is not None:
                    break
        cost = _v("营业成本") if "营业成本" in inc.index else None
        if not rev or cost is None:
            return None
        gm = (rev - cost) / rev * 100.0
        if gm != gm:  # NaN 防护
            return None
        return round(gm, 2)

    @ttl_cache(3600)
    def get_revenue_yi(self, code: str):
        """最新报告期营业收入（亿元）——用于同业「行业梯队」对比。

        取利润表「营业总收入」（缺则「营业收入」）最新**年报（报告期末尾为 1231）**列，
        折算为亿元（÷1e8，四舍五入 2 位）。无年报列时退化为最新报告期（季/半年报）。
        失败 / 缺失 / 为 0 返回 None（绝不返回 0，避免把缺失显示成 0 亿营收）。
        """
        code = self._norm_code(code)
        inc = self.get_income_statement(code)
        if inc is None or getattr(inc, "empty", True):
            return None
        cols = [str(c) for c in inc.columns]
        if not cols:
            return None
        yearend = [c for c in cols if c.endswith("1231")]
        col = max(yearend) if yearend else cols[0]  # 优先最新年报，保证跨公司口径可比
        row = None
        for r in ("营业总收入", "营业收入"):
            if r in inc.index:
                row = r
                break
        if row is None:
            return None
        try:
            v = float(inc.loc[row, col])
        except Exception:
            return None
        if v != v or v == 0:  # NaN / 0 防护
            return None
        return round(v / 1e8, 2)

    def get_curated_peers_overview(self, code: str, self_name: str = "") -> list:
        """按业务实质精选的可比公司列表（含本公司自身，供横向定位）。

        与 `get_industry_peers_overview`（申万二级口径）的区别：
        - 可比集来自 `_CURATED_PEERS` 人工精选的**同商业模式**公司；
        - **包含本公司自身**（`hl`/`strong` 标记），使「在同业中的位置」有直接参照；
        - 额外给出自算的 `gross_margin`（分销商的核心对比指标）。

        返回 list[dict]：{name, code, gross_margin, revenue, roe, rev_growth, profit_growth,
        debt_ratio, strong, hl}，按毛利率降序（缺失排最后）。无精选集时返回 []。
        revenue 为最新年报营业收入（亿元，None 表示缺失），用于「行业梯队」对比。
        """
        code = self._norm_code(code)
        pool = self._CURATED_PEERS.get(code)
        if not pool:
            return []
        members = list(pool) + [(code, self_name or code)]
        out = []
        for pcode, pname in members:
            rec = {
                "name": pname,
                "code": pcode,
                "gross_margin": None,
                "revenue": None,
                "roe": "",
                "rev_growth": "",
                "profit_growth": "",
                "debt_ratio": "",
                "strong": pcode == code,
                "hl": pcode == code,
                "curated": True,
            }
            try:
                rec["gross_margin"] = self.get_gross_margin(pcode)
            except Exception:
                rec["gross_margin"] = None
            try:
                rec["revenue"] = self.get_revenue_yi(pcode)
            except Exception:
                rec["revenue"] = None
            try:
                ind = self.get_financial_indicators(pcode)
                if ind is not None and not ind.empty and "日期" in ind.columns:
                    row = ind.iloc[-1]
                    rec["roe"] = self._fmt_metric(row.get("净资产收益率(%)"))
                    rec["rev_growth"] = self._fmt_metric(row.get("主营业务收入增长率(%)"))
                    rec["profit_growth"] = self._fmt_metric(row.get("净利润增长率(%)"))
                    rec["debt_ratio"] = self._fmt_metric(row.get("资产负债率(%)"))
            except Exception:
                pass
            out.append(rec)
        out.sort(key=lambda r: (r["gross_margin"] is None, -(r["gross_margin"] or 0)))
        return out

    def _resolve_l2_constituents(self, code: str, l1_code: str):
        """返回公司所在申万二级的成分 DataFrame（index_component_sw，稳定源）；无则 None。"""
        for l2 in self._SW_L2_FALLBACK.get(l1_code, []):
            try:
                d = self._ak.index_component_sw(symbol=l2)
            except Exception:
                d = None
            if d is None or d.empty or "证券代码" not in d.columns:
                continue
            d = d.copy()
            d["_code"] = d["证券代码"].astype(str).str.replace(r"\.\w+$", "", regex=True)
            if code in d["_code"].tolist():
                return d
        return None

    @staticmethod
    def _fmt_metric(v):
        """把数值规整为两位小数字符串；None / NaN / 非数值返回空串。"""
        try:
            if v is None:
                return ""
            f = float(v)
            if pd.isna(f):
                return ""
            return f"{f:.2f}"
        except (ValueError, TypeError):
            return ""
