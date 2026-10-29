"""板块深度报告：板块行情 + 成员全景 + 重点公司定量/画像 + LLM 深度内容 + 渲染。

职责边界（与基本面/概况/行业对比报告严格隔离）：
- 本文件只服务 report_type="board"，使用 template_board.html。
- 输入是板块代码（BKxxxx），不含目标个股；完全不触碰 run_pipeline 的个股财报链路。
- 复用 industry.py 的通用取数工具（curl 封装/单股定量/公司画像），只读不改。
- **东财主机池与取数封装一律用 `data/em.py`**（本文件不再出现域名字面量）；
  定量数据走真实接口（push2 主机降级 + F10 ROEJQ + akshare 兜底）；
  定性维度由 LLM 生成；LLM 不可用时降级为"未生成"标注，绝不编造。
- 质量基准：对齐 mlcc-yanjiu 样例报告的框架——核心结论(先读版) → 产业链全景分层 →
  分组公司卡片 → 财务横向对比图表 → 结构化对比结论 → 发展趋势 → 风险提示与免责。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..data import em as _em
from ..llm import get_router, load_prompt
from ..llm.base import LLMRequest
from . import chart_assets

logger = logging.getLogger(__name__)

# 板块报告重点公司数量（按总市值取前 N；与样例报告的信息密度对齐，同时控制 LLM/取数预算）
TOP_N = 12
QUANT_BUDGET_S = 50       # 重点公司定量+画像取数总时长预算（秒）

# LLM 示例/占位泄漏拦截（与 industry 同思路）
_PLACEHOLDER_MARKERS = ("示例·", "示例:", "示例：", "待填写", "XX 股份有限公司", "TODO")


# ---------------------------------------------------------------------------
# 板块行情与成员取数（与 frontend/server.py 同源的 curl 封装 + 分页拉全范式）
#
# **主机域名一律从 data/em.py 取，本文件不再出现字面量**（N-18 事故根因）：
# 原实现把池写成 ["push2delay", "push2"]，恰好这两台被限流时就无路可走；
# 三台中唯一可达的 push2test 不在池里 → 板块成员/行情全空 → 报告直接失败。
# ---------------------------------------------------------------------------
def _num(x):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return None


def _fmt_mcap_yi(v):
    """市值(元) -> 展示串：万亿/亿。"""
    if not isinstance(v, (int, float)) or v <= 0:
        return "—"
    if v >= 1e12:
        return "%.2f万亿" % (v / 1e12)
    return "%.2f亿" % (v / 1e8)


def fetch_board_quote(bk: str) -> dict:
    """取单板块行情快照（在行业+概念板块清单中检索该板块，分页直到命中）。

    注意：clist 会把 pz 钳制在 100，pz=1000 只返回前 100 条（按 f12 排序时
    BK1036 这类大号排在后面根本拉不到），必须标准分页翻找。失败返回空 dict。
    """
    base = {"fs": "m:90+t:2,m:90+t:3",
            "fields": "f12,f14,f2,f3,f104,f105,f128,f136,f20",
            "po": "1", "np": "1", "fltt": "2", "invt": "2", "fid": "f12"}
    hit = None
    for pn in range(1, 13):  # 全清单约 1000 条, 100/页最多 10 页
        q = dict(base)
        q["pn"] = str(pn)
        q["pz"] = "100"
        d = _em.get_json("/api/qt/clist/get?" + urllib.parse.urlencode(q), timeout=12)
        diff = ((d or {}).get("data") or {}).get("diff") or []
        if not diff:
            break
        for v in diff:
            if str(v.get("f12") or "").upper() == bk:
                hit = v
                break
        if hit:
            break
    if not hit:
        logger.warning("[board] 板块 %s 未在清单中检索到, 行情快照缺失", bk)
        return {}
    return {
        "bk": bk,
        "name": str(hit.get("f14") or ""),
        "price": _num(hit.get("f2")),
        "chg": _num(hit.get("f3")),
        "up_n": _num(hit.get("f104")),
        "down_n": _num(hit.get("f105")),
        "leader": str(hit.get("f128") or ""),
        "leader_chg": _num(hit.get("f136")),
        "mcap": _num(hit.get("f20")),
    }


def fetch_members_full(bk: str) -> list[dict]:
    """分页拉全板块成员（clist fs=b:{bk}），带单页重试与完整性校验。失败返回空列表。"""
    fields = "f12,f14,f2,f3,f100,f20,f21,f115,f23"
    base = {"fs": "b:" + bk, "fields": fields,
            "po": "1", "np": "1", "fltt": "2", "invt": "2", "fid": "f20"}
    rows: list[dict] = []
    total, complete = None, False
    for _attempt in range(3):
        rows, pn = [], 1
        total = None
        while pn <= 100:
            q = dict(base)
            q["pn"] = str(pn)
            q["pz"] = "100"
            path = "/api/qt/clist/get?" + urllib.parse.urlencode(q)
            d = None
            for _round in range(2):
                # 逐主机验 payload（要确认 diff 完整，不能只看「有 JSON 返回」），
                # 故不用 get_json，而是自己遍历健康度排序后的主机池。
                for host in _em.ordered_hosts(_em.PUSH2_HOSTS, path):
                    d = _em.fetch_host(host, path, timeout=12)
                    if not d:
                        continue
                    data = (d or {}).get("data") or {}
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
            data = (d or {}).get("data") or {}
            diff = data.get("diff") or []
            rows.extend(v for v in diff if isinstance(v, dict))
            if total is None:
                total = int(data.get("total") or 0)
            if (total and len(rows) >= total) or not diff:
                break
            pn += 1
        complete = bool(rows) or (total == 0)
        if complete:
            break
        time.sleep(1.5)

    def _f(k):
        return lambda v: _num(v.get(k))

    members = []
    for v in rows:
        code = str(v.get("f12") or "")
        name = str(v.get("f14") or "").replace(" ", "")
        if not code or not name:
            continue
        members.append({
            "code": code, "name": name,
            "price": _num(v.get("f2")), "chg": _num(v.get("f3")),
            "ind": str(v.get("f100") or ""),
            "mcap": _num(v.get("f20")), "fcap": _num(v.get("f21")),
            "pe": _num(v.get("f115")), "pb": _num(v.get("f23")),
        })
    # total 已知时必须取满（total=0 的空板块同样合法）
    if total is not None and len(members) < total and total > 0:
        logger.warning("[board] 板块 %s 成员拉取不完整(%d/%d)", bk, len(members), total)
    return members


# ---------------------------------------------------------------------------
# LLM 内容生成 + 校验闸门（契约见 llm/prompts/board_report.md）
# ---------------------------------------------------------------------------
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


def _sanitize_board_json(parsed: dict, companies: list[dict]) -> dict | None:
    """LLM 输出清洗：去代码后缀、映射回给定名单、丢弃名单外条目、补漏并填充真实数据。

    不同模型风格不一，常见偏差：公司名带 "(688981)" 后缀、省略 -U 上市标识
    （"摩尔线程" vs 名单名"摩尔线程-U"）、自作主张补充名单外公司。逐条映射回
    给定名单，映射不上的一律丢弃（宁缺勿编）；给定公司有遗漏或卡片内容空白时，
    用已抓取的真实定量数据组卡填充（绝不留"只露名字"的空卡）。
    """
    code_by_name = {c["name"]: c for c in companies}
    # 别名表: LLM 常省略 -U/-W/-N/-X/-C 等上市标识后缀（"摩尔线程" vs "摩尔线程-U"）、
    # 或带 ST 前缀变体; 名单名去掉这些修饰后做二次匹配
    alias_by_bare: dict[str, str] = {}
    for nm in code_by_name:
        bare = re.sub(r"-[UWNXC]$", "", re.sub(r"^\*?ST", "", nm))
        alias_by_bare.setdefault(bare, nm)
    groups_raw = parsed.get("groups")
    if not isinstance(groups_raw, list):
        return None
    seen: set[str] = set()

    def _resolve(nm: str) -> str | None:
        """LLM 写法 -> 名单名。先精确, 再去代码后缀, 最后去上市标识/ST 修饰模糊匹配。"""
        if nm in code_by_name:
            return nm
        m = re.match(r"^(.*?)[（(](\d{6})[)）]$", nm)
        if m:
            nm_bare, code_part = m.group(1), m.group(2)
            if nm_bare in code_by_name:
                return nm_bare
            if code_part in code_by_name:
                return code_part
            nm = nm_bare
        return alias_by_bare.get(re.sub(r"^\*?ST", "", nm))

    clean_groups: list[dict] = []
    for g in groups_raw:
        if not isinstance(g, dict):
            continue
        comps = []
        for c in (g.get("companies") or []):
            if not isinstance(c, dict):
                continue
            nm = _resolve(str(c.get("name") or "").replace(" ", ""))
            if not nm or nm in seen:
                continue  # 名单外或重复: 丢弃
            seen.add(nm)
            pills = []
            for p in (c.get("pills") or []):
                if isinstance(p, dict):
                    t = str(p.get("text") or "").strip()[:6]
                    ty = str(p.get("type") or "flat")
                    if t and ty in ("up", "down", "flat", "warn"):
                        pills.append({"text": t, "type": ty})
            comps.append({
                "name": nm, "code": code_by_name[nm]["code"],
                "role": str(c.get("role") or "").strip()[:16],
                "desc": str(c.get("desc") or "").strip(),
                "kv": str(c.get("kv") or "").strip(),
                "pills": pills,
            })
        if comps:
            clean_groups.append({
                "title": str(g.get("title") or "").strip(),
                "badge": str(g.get("badge") or "").strip(),
                "intro": str(g.get("intro") or "").strip(),
                "companies": comps,
            })
    if not clean_groups:
        return None

    # 卡片填充: 用真实定量数据组卡（全部来自已抓取指标, 不编造）。
    # 适用于(a)补漏公司(b)LLM 列了名字但 desc/kv 空白的卡片, 避免"只露名字"的空卡。
    def _fill_card(entry: dict, src: dict) -> None:
        if not entry.get("role"):
            entry["role"] = str(src.get("ind") or "").strip()[:16]
        if not entry.get("kv"):
            entry["kv"] = ("市值 %s | PE %s | 毛利率 %s"
                           % (src["mcap_disp"], src["pe_disp"], src["gross_disp"]))
        if not entry.get("desc"):
            parts = ["总市值 %s（板块内第 %d 位）" % (src["mcap_disp"], src["rank"])]
            for label, key in (("PE", "pe_disp"), ("PB", "pb_disp"),
                               ("ROE", "roe_disp"), ("毛利率", "gross_disp"), ("营收同比", "rev_disp")):
                v = str(src.get(key) or "").strip()
                if v and v != "—":
                    parts.append("%s %s" % (label, v))
            mb = str(src.get("main_business") or "").strip()
            if mb and mb != "未获取":
                # 主营文本可能自带百分比, 换成全角避免被可溯源校验误伤
                parts.append("主营：" + mb.replace("%", "％")[:120])
            entry["desc"] = "，".join(parts) + "。"
        if not entry.get("pills"):
            chg = src.get("chg_num")
            if isinstance(chg, (int, float)):
                entry["pills"] = [{"text": src["chg_disp"].replace("+", "").strip(),
                                   "type": "up" if chg > 0 else ("down" if chg < 0 else "flat")}]

    # 补漏: 未覆盖的给定公司并入最后一组
    for c in companies:
        if c["name"] not in seen:
            entry = {"name": c["name"], "code": c["code"], "role": "", "desc": "", "kv": "", "pills": []}
            _fill_card(entry, c)
            clean_groups[-1]["companies"].append(entry)
            seen.add(c["name"])
    # LLM 已列出但内容为空的卡片同样填充
    for g in clean_groups:
        for c in (g.get("companies") or []):
            src = code_by_name.get(c.get("name"))
            if src and (not c.get("desc") or not c.get("kv")):
                _fill_card(c, src)
    parsed["groups"] = clean_groups
    for k in ("key_points", "trends", "risk_items"):
        v = parsed.get(k)
        parsed[k] = ([str(x).strip() for x in v if _bad_text(x) is False]
                     if isinstance(v, list) else [])
    parsed["summary_html"] = str(parsed.get("summary_html") or "").strip()
    return parsed


def _board_json_is_valid(parsed: dict, names: list[str], facts: dict[str, dict]) -> tuple[bool, list[str]]:
    """校验 LLM 输出：章节齐全、公司名可追溯、无占位残留、百分比可溯源。"""
    issues: list[str] = []
    name_set = set(names)
    # 1) 核心结论：≥3 条且非占位
    kp = parsed.get("key_points")
    if not isinstance(kp, list) or len(kp) < 3 or any(_bad_text(x) for x in kp):
        issues.append("key_points 缺失/不足3条/含占位")
    # 2) 产业链分层：≥1 组（清洗后名单外公司已丢弃），组内公司必须来自给定名单
    groups = parsed.get("groups")
    if not isinstance(groups, list) or len(groups) < 1:
        issues.append("groups 缺失")
    else:
        for g in groups:
            if not isinstance(g, dict) or _bad_text(g.get("title")):
                issues.append("groups 存在缺标题的分组")
                continue
            for c in (g.get("companies") or []):
                if not isinstance(c, dict):
                    continue
                nm = str(c.get("name") or "").replace(" ", "")
                if nm not in name_set:
                    issues.append(f"groups 出现名单外公司 {nm}")
                    break
                # 补漏占位公司的 desc 允许为空(模板留白), 但有内容时不得为占位残留
                if c.get("desc") and _bad_text(c.get("desc")):
                    issues.append(f"groups 公司 {nm} 描述含占位残留")
                    break
    # 3) 趋势与风险：各 ≥3 条
    for k, mn in (("trends", 3), ("risk_items", 3)):
        v = parsed.get(k)
        if not isinstance(v, list) or len(v) < mn or any(_bad_text(x) for x in v):
            issues.append(f"{k} 缺失/不足{mn}条/含占位")
    # 4) 百分比可溯源：正文出现的 % 必须与真实指标量级吻合
    allowed: list[float] = []
    for _c, q in facts.items():
        for k in ("roe", "gross", "debt_ratio", "rev_growth", "chg", "pe", "pb"):
            v = q.get(k)
            if isinstance(v, (int, float)):
                allowed.append(abs(float(v)))
    def _known(x: float) -> bool:
        return any(a * 0.5 <= x <= a * 2.0 for a in allowed)
    if allowed:
        blobs = list(parsed.get("key_points") or []) + list(parsed.get("trends") or []) \
            + list(parsed.get("risk_items") or [])
        for g in (groups or []):
            if isinstance(g, dict):
                blobs.append(str(g.get("intro") or ""))
                for c in (g.get("companies") or []):
                    if isinstance(c, dict):
                        blobs += [str(c.get("desc") or ""), str(c.get("kv") or "")]
        for b in blobs:
            for x in _pct_claims(b):
                if not _known(x):
                    issues.append(f"出现不可追溯的百分比 {x}%（真实数据中无此量级）")
                    break
    return (not issues), issues


def generate_board_content(bk: str, bk_name: str, quote: dict, members: list[dict],
                           companies: list[dict], facts: dict[str, dict],
                           max_retries: int = 2) -> dict | None:
    """调用 LLM 生成板块深度内容 JSON；失败（含额度/网络/校验不过）返回 None。"""
    try:
        system = load_prompt("board_report")
    except Exception:
        system = (
            "你是资深的 A 股行业研究分析师。基于给定真实数据生成板块深度报告的结构化 JSON，"
            "严禁编造任何未提供的数字，未知字段标'未获取'。"
        )

    def _line(c):
        q = facts.get(c["code"]) or {}
        f = lambda k: ("%.2f" % q[k]) if isinstance(q.get(k), (int, float)) else "未获取"
        mb = str(c.get("main_business") or "").strip()
        if len(mb) > 200:
            mb = mb[:200] + "…"
        return (f"- {c['name']}({c['code']}) 市值:{c['mcap_disp']} | PE:{c['pe_disp']} | PB:{c['pb_disp']}"
                f" | ROE:{f('roe')}% | 毛利率:{f('gross')}% | 营收同比:{f('rev_growth')}%"
                f" | 当日涨跌:{f('chg')}% | 细分行业:{c['ind']}\n  主营: {mb or '未获取'}")

    facts_text = "\n".join(_line(c) for c in companies)
    qs = quote
    board_text = (
        f"板块: {bk_name or qs.get('name') or bk}（{bk}）；"
        f"指数点位: {('%.2f' % qs['price']) if qs.get('price') is not None else '未获取'}；"
        f"涨跌幅: {('%.2f%%' % qs['chg']) if qs.get('chg') is not None else '未获取'}；"
        f"上涨/下跌家数: {qs.get('up_n') if qs.get('up_n') is not None else '未获取'}"
        f"/{qs.get('down_n') if qs.get('down_n') is not None else '未获取'}；"
        f"板块总市值: {_fmt_mcap_yi(qs.get('mcap'))}；"
        f"领涨股: {qs.get('leader') or '未获取'}"
    )
    user = (
        f"【板块快照】{board_text}\n"
        f"【板块规模】成员共 {len(members)} 家，以下为按总市值选取的 {len(companies)} 家重点公司。\n\n"
        f"【重点公司真实数据】（引用任何数字必须与此一致，严禁编造或改写）:\n{facts_text}\n\n"
        "请严格按契约返回板块深度报告内容：纯 JSON（不要 markdown 代码块）。"
        "groups 按产业链上/中/下游（或该板块合理的业务分层）划分，每家公司归入最贴合的一组，"
        "每个 groups[].companies[].name 必须与给定公司名逐字一致（禁止附带代码后缀、禁止补充名单外的任何公司）；"
        "desc/kv 只能引用给定真实数据；分析须体现公司之间的横向比较与传导逻辑，"
        "禁止写成互不相关的单公司介绍；key_points 按'景气主线/竞争格局/核心环节/风险提示'式要点组织。"
    )
    # max_tokens 阶梯: 按量计费厂商按 max_tokens 预留额度/校验上限（qwen 实测上限 5000，
    # 超限直接 400；deepseek 超额预留 402），故从 5000 起逐级降档重试
    token_ladder = (5000, 4000, 3000)
    for attempt in range(max_retries):
        mt = token_ladder[min(attempt, len(token_ladder) - 1)]
        try:
            resp = get_router().complete(LLMRequest(system=system, user=user, max_tokens=mt))
        except Exception as e:
            logger.warning("[board] LLM 调用异常: %s", e)
            return None
        if not resp.ok:
            logger.warning("[board] LLM 请求失败(max_tokens=%s): %s", mt, str(resp.error)[:200])
            continue
        parsed = _parse_json_loose(resp.text)
        if not parsed:
            logger.warning("[board] 第 %d 次生成 JSON 解析失败(长度 %d)", attempt + 1, len(resp.text))
            continue
        parsed = _sanitize_board_json(parsed, companies)
        if not parsed:
            logger.warning("[board] 第 %d 次生成清洗后无有效分组", attempt + 1)
            continue
        ok, issues = _board_json_is_valid(parsed, [c["name"] for c in companies], facts)
        if ok:
            return parsed
        for i in issues:
            logger.warning("[board] 第 %d 次生成未通过校验: %s", attempt + 1, i)
    return None


def _fetch_profile_http(code: str) -> dict:
    """东财 F10 公司画像（纯 HTTP）。

    刻意不走 industry.fetch_company_profile 的 cninfo/akshare 路径：
    akshare 的巨潮接口依赖 py_mini_racer（V8 原生库），实测在后台线程中偶发
    原生层 access violation 直接杀死整个服务进程（无法被 try/except 捕获），
    板块报告并发抓取 12 家画像会显著放大触发概率。F10 兜底为纯 curl，稳定。

    取数走 `em.f10_profile()`。**原实现的请求缺 `sty` 与 `filter`**，服务端回
    `{"success": false, "message": "返回字段参数不能为空", "code": 9501}`，
    于是本函数**从落地起就一直返回空 dict**（重点公司的「主营业务」整段为空），
    且因为它是「有数据就填、没有就跳过」的形状，静默失败无人察觉（N-19）。
    """
    try:
        return _em.f10_profile(code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[board] 东财 F10 画像获取失败(code=%s): %s", code, e)
        return {}


# 注：原先这里还有一份 `_fund_secucode()`（与 industry.py 的同名副本重复），
# 已统一到 `data/em.py::secucode()` —— F10 的 SECUCODE 规则只应有一处定义。


# ---------------------------------------------------------------------------
# 单股定量指标（纯 HTTP，进程安全）
# ---------------------------------------------------------------------------
_QUANT_HTTP_TTL_OK_S = 600    # 成功缓存 10 分钟（与 industry.fetch_stock_quant 口径一致）
_QUANT_HTTP_TTL_FAIL_S = 120  # 全失败短缓存 2 分钟
_QUANT_HTTP_CACHE: dict[str, tuple[float, dict]] = {}
_QUANT_HTTP_LOCK = __import__("threading").Lock()


def _fetch_quant_http(code: str, snap: dict | None = None) -> dict:
    """单股定量指标（纯 HTTP: push2 财务指标 + F10 ROEJQ），带 TTL 缓存。

    刻意不走 industry.fetch_stock_quant：其 akshare ROE 兜底
    （stock_financial_analysis_indicator）与 akshare 生态共享 py_mini_racer 等
    原生依赖，后台线程中偶发原生崩溃直接杀死整个服务进程——2026-09-06 MLCC
    板块报告实测复现：报告已 SUCCESS 返回后进程静默死亡，日志无任何 traceback。
    本函数只用 requests/curl 级 HTTP 调用，彻底隔离该崩溃源（industry.py 不动）。
    """
    now = time.time()
    with _QUANT_HTTP_LOCK:
        hit = _QUANT_HTTP_CACHE.get(code)
        if hit:
            ts, val = hit
            ok = any(val.get(k) is not None for k in ("roe", "gross", "debt_ratio", "rev_growth"))
            if now - ts < (_QUANT_HTTP_TTL_OK_S if ok else _QUANT_HTTP_TTL_FAIL_S):
                return dict(val)

    from .industry import _secid

    res = {"roe": None, "gross": None, "debt_ratio": None, "rev_growth": None,
           "pe": (snap or {}).get("pe"), "pb": (snap or {}).get("pb"),
           "mcap": (snap or {}).get("mcap")}
    # 1) push2 单股接口: 毛利率 f186 / 资产负债率 f188 / 营收同比 f184 / PE/PB/市值
    q = urllib.parse.urlencode({"secid": _secid(code),
                                "fields": "f186,f188,f184,f115,f162,f23,f116",
                                "invt": "2", "fltt": "2"})
    d = _em.get_json("/api/qt/stock/get?" + q, timeout=8)
    row = (d or {}).get("data") or {}
    res["gross"] = _num(row.get("f186"))
    res["debt_ratio"] = _num(row.get("f188"))
    res["rev_growth"] = _num(row.get("f184"))
    pe_ttm = _num(row.get("f115"))
    res["pe"] = pe_ttm if pe_ttm is not None else _num(row.get("f162"))
    pb = _num(row.get("f23"))
    if pb is not None:
        res["pb"] = pb
    mcap = _num(row.get("f116"))
    if mcap is not None:
        res["mcap"] = mcap
    # 2) ROE 主源: 东财业绩报表 RPT_LICO_FN_CPD.WEIGHTAVG_ROE（加权 ROE，最新报告期）
    #    注：F10 ROEJQ 曾被记为「2026-09 实测已失效」，实为请求缺 sty 参数（N-19），
    #    现已由 em.f10_roejq 修好并作为备用源并存；两条腿互为备份。
    try:
        q2 = urllib.parse.urlencode({"reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
                                     "filter": '(SECURITY_CODE="%s")' % code,
                                     "pageSize": "1", "pageNumber": "1",
                                     "sortColumns": "REPORTDATE", "sortTypes": "-1"})
        d2 = _em.get_json("/api/data/v1/get?" + q2, timeout=8, hosts=_em.DATACENTER_HOSTS)
        rows2 = ((d2 or {}).get("result") or {}).get("data") or []
        if rows2:
            res["roe"] = _num(rows2[0].get("WEIGHTAVG_ROE"))
    except Exception as e:
        logger.warning("[board] 业绩报表 ROE 获取失败(code=%s): %s", code, e)
    # 3) ROE 备用: F10 主要财务指标 ROEJQ
    if res["roe"] is None:
        try:
            v = _em.f10_roejq(code)
            if v is not None:
                res["roe"] = v
        except Exception as e:
            logger.warning("[board] F10 ROE 获取失败(code=%s): %s", code, e)

    with _QUANT_HTTP_LOCK:
        _QUANT_HTTP_CACHE[code] = (now, dict(res))
    return res


# ---------------------------------------------------------------------------
# 数据装配 + 渲染
# ---------------------------------------------------------------------------
def _disp(v, fmt="%s"):
    if v is None:
        return "—"
    try:
        return fmt % v
    except Exception:
        return "—"


def build_board_report_data(bk: str, bk_name: str = "", use_llm: bool = True,
                            progress=None) -> dict:
    """板块行情 → 成员全景 → 重点公司定量/画像 → LLM 深度内容 → 装配模板数据。"""

    def _emit(stage, pct=0):
        if progress:
            try:
                progress(stage, pct)
            except Exception:
                pass

    _emit("获取板块行情快照", 8)
    quote = fetch_board_quote(bk)

    _emit("拉取板块成员全景", 18)
    members = fetch_members_full(bk)
    if not members:
        raise RuntimeError("板块成员获取失败（bk=%s），无法生成报告" % bk)

    # 成员按市值取前 N 家做重点公司；全成员用于规模统计与图表
    members.sort(key=lambda m: (m.get("mcap") or 0), reverse=True)
    top = members[:TOP_N]
    total_mcap = sum(m["mcap"] for m in members if m.get("mcap"))
    up_count = sum(1 for m in members if (m.get("chg") or 0) > 0)
    down_count = sum(1 for m in members if (m.get("chg") or 0) < 0)

    # 并发抓取重点公司定量指标与画像
    # 定量走 _fetch_quant_http（push2 + F10 ROEJQ 纯 HTTP），画像走 _fetch_profile_http：
    # 两者均刻意绕开 akshare/py_mini_racer 生态——该生态在后台线程中偶发原生崩溃
    # 直接杀死服务进程（无法被 try/except 捕获），板块报告 12 路并发会显著放大触发概率。
    _emit("抓取重点公司定量指标与画像", 30)
    facts: dict[str, dict] = {}
    profiles: dict[str, dict] = {}
    ex = ThreadPoolExecutor(max_workers=8)
    try:
        deadline = time.time() + QUANT_BUDGET_S
        futs = {ex.submit(_fetch_quant_http, c["code"], c): ("q", c["code"]) for c in top}
        futs.update({ex.submit(_fetch_profile_http, c["code"]): ("p", c["code"]) for c in top})
        for fut in as_completed(futs):
            kind, code = futs[fut]
            try:
                r = fut.result()
                if isinstance(r, dict):
                    if kind == "q":
                        facts.setdefault(code, {}).update(r)
                    else:
                        profiles[code] = r
            except Exception:
                pass
            if time.time() > deadline:
                break
    except Exception as e:
        logger.warning("[board] 定量/画像并发取数异常: %s", e)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    for c in top:
        q = facts.get(c["code"]) or {}
        # 实时接口缺失的 pe/pb/mcap 用成员快照值补齐
        for k in ("pe", "pb", "mcap"):
            if q.get(k) is None and c.get(k) is not None:
                q[k] = c.get(k)
        facts[c["code"]] = q

    companies = []
    for i, c in enumerate(top):
        q = facts.get(c["code"]) or {}
        p = profiles.get(c["code"]) or {}
        mb = str(p.get("main_business") or p.get("scope") or p.get("intro") or "").strip()
        if len(mb) > 160:
            mb = mb[:160] + "…"
        companies.append({
            "name": c["name"], "code": c["code"], "ind": c.get("ind") or "—",
            "rank": i + 1,
            "price_disp": _disp(c.get("price"), "¥%.2f"),
            "chg_num": c.get("chg"),
            "chg_disp": _disp(c.get("chg"), "%+.2f%%"),
            "mcap_disp": _fmt_mcap_yi(c.get("mcap")),
            "pe_disp": _disp(q.get("pe"), "%.2f"),
            "pb_disp": _disp(q.get("pb"), "%.2f"),
            "roe_disp": _disp(q.get("roe"), "%.2f%%"),
            "gross_disp": _disp(q.get("gross"), "%.2f%%"),
            "rev_disp": _disp(q.get("rev_growth"), "%+.2f%%"),
            "main_business": mb or "未获取",
        })

    # LLM 深度内容（失败降级，不编造）
    llm_json = None
    if use_llm:
        _emit("生成 AI 板块深度内容", 60)
        try:
            llm_json = generate_board_content(bk, bk_name, quote, members, companies, facts)
        except Exception as e:
            logger.warning("[board] LLM 内容生成异常: %s", e)
            llm_json = None

    _emit("装配报告数据", 85)
    name_disp = bk_name or quote.get("name") or bk
    qs_chg = quote.get("chg")
    data = {
        "bk": bk,
        "bk_name": name_disp,
        "report_date": datetime.now().strftime("%Y-%m-%d"),
        "member_count": len(members),
        "quote": {
            "price_disp": _disp(quote.get("price"), "%.2f"),
            "chg_disp": _disp(qs_chg, "%+.2f%%"),
            "chg_dir": ("up" if (qs_chg or 0) > 0 else ("down" if (qs_chg or 0) < 0 else "flat")) if qs_chg is not None else "flat",
            "up_n": int(quote["up_n"]) if quote.get("up_n") is not None else up_count,
            "down_n": int(quote["down_n"]) if quote.get("down_n") is not None else down_count,
            "mcap_disp": _fmt_mcap_yi(quote.get("mcap")) if quote.get("mcap") else _fmt_mcap_yi(total_mcap),
            "leader": quote.get("leader") or "—",
            "leader_chg": _disp(quote.get("leader_chg"), "%+.2f%%"),
        },
        "stats": {
            "total_mcap_disp": _fmt_mcap_yi(total_mcap),
            "up_count": up_count,
            "down_count": down_count,
        },
        "companies": companies,
        "charts": {
            "names": [c["name"] for c in companies],
            "mcaps": [round((facts.get(c["code"], {}).get("mcap") or 0) / 1e8, 1) for c in companies],
            "chgs": [facts.get(c["code"], {}).get("chg") if facts.get(c["code"], {}).get("chg") is not None else c.get("chg_num") for c in companies],
            "grosses": [facts.get(c["code"], {}).get("gross") for c in companies],
            "roes": [facts.get(c["code"], {}).get("roe") for c in companies],
        },
        "llm_ok": bool(llm_json),
        "key_points": [str(x) for x in ((llm_json or {}).get("key_points") or [])] or [],
        "groups": (llm_json or {}).get("groups") or [],
        "trends": [str(x) for x in ((llm_json or {}).get("trends") or [])],
        "summary_html": (llm_json or {}).get("summary_html") or "",
        "risk_items": [str(x) for x in ((llm_json or {}).get("risk_items") or [])],
    }
    # LLM 缺位时的降级文案（绝不编造）
    if not data["key_points"]:
        data["key_points"] = ["核心结论未能生成（LLM 不可用或输出未通过校验，不编造内容）；下方板块行情、成员全景与定量对比均为真实接口数据。"]
    if not data["groups"]:
        data["groups"] = [{
            "title": "重点公司（按总市值）", "badge": "", "intro": "",
            "companies": [{"name": c["name"], "code": c["code"], "role": "", "desc": "",
                           "kv": "", "pills": []} for c in companies],
        }]
    if not data["trends"]:
        data["trends"] = ["发展趋势未能生成（LLM 不可用或输出未通过校验，不编造内容）。"]
    if not data["risk_items"]:
        data["risk_items"] = ["风险提示未能生成（LLM 不可用或输出未通过校验，不编造内容）。"]
    if not data["summary_html"]:
        data["summary_html"] = "<p>竞争格局总结未能生成（LLM 不可用或输出未通过校验）。定量对比见上方图表与成员概览表。</p>"
    return data


def _blockp(value):
    """段落：内容已含 <p> 时原样输出，否则补一层 <p>（与 industry/overview 渲染口径一致）。"""
    from markupsafe import Markup
    s = "" if value is None else str(value)
    s = s.strip()
    if not s:
        return Markup("")
    s = re.sub(r"\*\*([^*\n]{1,200})\*\*", r"<strong>\1</strong>", s)
    if re.search(r"<p[\s>]", s, re.I):
        return Markup(s)
    return Markup(f"<p>{s}</p>")


def render_board(data: dict, template_dir: str, out_path: str | None = None) -> str:
    """用 template_board.html 渲染板块深度报告；可选落盘（返回文件路径）。

    D4：把 ECharts 本地副本内联进 `<script>`，使报告可离线打开/打印；
    读不到本地库则模板自动回退 CDN（`chart_assets` 已把该分支的判断交给模板）。
    """
    # 注入内联库与降级标志（原地写回 data，供 meta 侧读取 chart_offline）
    chart_assets.apply_chart_lib(data, chart_assets.inline_echarts())
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["blockp"] = _blockp
    tpl = env.get_template("template_board.html")
    html = tpl.render(**data)
    if out_path:
        Path(out_path).write_text(html, encoding="utf-8")
        return out_path
    return html
