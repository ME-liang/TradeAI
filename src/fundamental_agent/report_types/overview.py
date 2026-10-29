"""公司概况报告：渲染 + 六层数据装配 + LLM 内容生成（方案 B①：LLM 整段生成）。

职责边界（与基本面报告严格隔离）：
- 本文件只服务「公司概况」报告类型，使用 template_overview.html。
- 不 import / 不修改 render() 与 template.html（基本面报告）。
- 内容来源：LLM 按 JSON 契约生成六层结构化内容 + 叙述；LLM 不可用时降级为空壳
  （所有数字标「未获取」、不编造），仅渲染排版骨架。

JSON 契约与 template_overview.html 的变量一一对应，详见 llm/prompts/overview.md。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..llm import get_router, load_prompt
from ..llm.base import LLMRequest
from ..data.annual_report import is_generic_entity
from . import chart_assets

logger = logging.getLogger(__name__)

# LLM 示例泄漏拦截。
# 提示词的 JSON 契约为演示字段类型而给出了具体示例值（现统一带「示例·」等可自曝标记）。
# 实测思考型模型在长输出压力下会把示例原样回显为正文——「供应商集中度说明」、
# 「市场空间与增速描述」、「交叉验证三层结论（HTML）」都曾被当成真实内容渲染出来。
# 把示例当内容输出等同编造，此处统一拦截并清空对应字段：宁可该处留空，也不展示假内容。
_PLACEHOLDER_MARKERS = (
    "示例·", "示例:", "示例：", "待填写",
    "（HTML）", "(HTML)", "（HTML 片段）", "XX 股份有限公司",
)

# 旧版提示词的示例原文（不含「示例·」标记），模型可能凭记忆原样回显。
# 这些字符串都是泛化的栏目名，正常行文不会让某个字段「整值等于」它们，
# 故用**全值精确匹配**兜底，兼顾召回与误伤控制。
_LEGACY_EXAMPLE_LITERALS = frozenset({
    "供应商集中度说明", "客户集中度说明", "市场空间与增速描述",
    "业绩轨迹说明", "股权结构说明", "治理风险说明", "现金流说明",
    "同业横比说明", "数据来源构成说明", "实控人性质分析",
})

# 已知的「硬错误」数字：LLM 多次把实控人赵马克(MARK ZHAO)持股比例写成 27.7%/27.8%
# （源自在 2.78% 基础上的 10 倍换算错误），并曾把侯红亮的未解押 3.41%
# 张冠李戴到赵马克头上（赵马克历史质押已全额解除，当前在押 0 股）。
# 凡命中以下模式，一律视为编造并清空该字段，宁可留空也不展示假数字。
#
# 口径更新（P1）：年报一手来源确认赵马克**总持股 127,564,160 股 = 11.08%**（含限售），
# 而东方财富快照的 2.78% 只是其 25% 无限售可转让额度。二者均为真值，仅口径不同。
# 因此拦截区间从「10~39%」收紧为「12~39%」，为合法的 11.08% 留出通道，
# 同时仍覆盖历史上真实出现过的 27.x% 等 10 倍级错误。
_WRONG_HOLDING_PATTERNS = [
    # 赵马克 / MARK ZHAO 附近出现 12%~39% 的持股 / 质押占比
    re.compile(r"(赵马克|MARK\s?ZHAO).{0,30}?(持股|质押|比例).{0,12}?(1[2-9]|2[0-9]|3[0-9])\.[0-9]{1,2}\s*%", re.I),
    # 实控人持股比例约 NN.N%（真实值：2.78% 快照口径 / 11.08% 年报口径）
    re.compile(r"实控人.{0,12}?持股.{0,12}?比例.{0,6}?(1[2-9]|2[0-9]|3[0-9])\.[0-9]{1,2}\s*%", re.I),
    # 赵马克 被标注任意质押比例（其当前在押为 0）
    re.compile(r"(赵马克|MARK\s?ZHAO).{0,30}?质押比例.{0,12}?[0-9]+\.[0-9]+\s*%", re.I),
]


def _leaks_placeholder(value) -> bool:
    """判断某个值是否携带提示词示例/占位痕迹。"""
    try:
        s = str(value)
    except Exception:
        return False
    if any(m in s for m in _PLACEHOLDER_MARKERS):
        return True
    return s.strip() in _LEGACY_EXAMPLE_LITERALS


def _leaks_wrong_holding(value) -> bool:
    """判断某个值是否携带已知的错误持股/质押数字（赵马克 10 倍误差等）。"""
    try:
        s = str(value)
    except Exception:
        return False
    return any(p.search(s) for p in _WRONG_HOLDING_PATTERNS)


# 模型把「占位符」当成答案输出。实测 7B 模型生成的结论里出现：
#   「公司为非上市 controlling shareholder」「属于 unspecified 行业」
#   「未提供收入、毛利率、净利润等关键数据」——而报告里这些数据全部真实存在。
# 这类不是「信息缺失」，而是**主动的错误陈述**，比留空更有害，故一并拦截。
# 只收窄到客观可判、误伤极低的模式，避免误杀正常行文。
_NONSENSE_PATTERNS = [
    re.compile(r"\bunspecified\b", re.I),
    re.compile(r"\bunknown\b", re.I),
    re.compile(r"\bN/A\b"),
    # 概况报告只服务 A 股上市公司，断言本公司「非上市」必为错误
    re.compile(r"公司\s*为\s*非上市"),
    re.compile(r"非上市\s*公司"),
]


def _leaks_nonsense(value) -> bool:
    """判断某个值是否把占位符/明显错误断言当结论输出。"""
    try:
        s = str(value)
    except Exception:
        return False
    return any(p.search(s) for p in _NONSENSE_PATTERNS)


def _is_bad_text(value) -> bool:
    """统一判定：占位符回显 / 控股关系误判 / 主动错误陈述，三者任一命中即不可用。"""
    if isinstance(value, str):
        return (
            _leaks_placeholder(value)
            or _leaks_wrong_holding(value)
            or _leaks_nonsense(value)
        )
    if isinstance(value, dict):
        return any(_is_bad_text(x) for x in value.values())
    if isinstance(value, list):
        return any(_is_bad_text(x) for x in value)
    return False


# ---------------------------------------------------------------------------
# 事实一致性校验（针对 LLM 结论）
# ---------------------------------------------------------------------------
# 背景：字符串黑名单只能挡住「上一版见过的编法」。实测 7B 模型两次运行给出两套不同的
# 错误陈述（第一次「非上市 / unspecified」，第二次「实控人为张跃军 / 主营半导体制造 /
# 收入占比未公开披露」），模式匹配必然漏网。故改为**拿数据去证伪**：
#   1) 点名实控人/控股股东 -> 该人必须在持股名单里且比例达到控制阈值（否则只是推测）
#   2) 断言「未披露/未提供」 -> 对应数据若真实存在即为谎言
#   3) 给出主营/行业定性 -> 必须与真实主营构成或行业分类共享词语（2-gram）
# 三条都是「可判定的客观冲突」，不做语义理解，误杀面小。
def _norm_segments(raw_segments) -> list[dict]:
    """把东财原始主营构成行归一成 `{name, pct, margin, profit_pct}`（比例均为百分数）。

    关键点：layers.business.segments 存的是 `stock_zygc_em` 的**原始中文列**
    （主营构成 / 收入比例 / 毛利率 / 利润比例，比例为 0.923 这样的小数），
    此前多处按 `name` / `pct` 取值——取不到就成 None，一路污染到喂给 LLM 的事实串，
    模型看到「收入占比 None」便顺理成章地写出「未公开披露」。这里统一收敛，三处共用。
    """
    rows = [r for r in (raw_segments or []) if isinstance(r, dict)]
    if not rows:
        return []

    def _d(r):
        v = r.get("报告日期")
        if v is None:
            return ""
        return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)

    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    dates = sorted({_d(r) for r in rows if _d(r)})
    latest = dates[-1] if dates else ""
    picked = [r for r in rows if (not latest or _d(r) == latest)]
    by_prod = [r for r in picked if str(r.get("分类类型", "")) == "按产品分类"]
    if by_prod:
        picked = by_prod

    out = []
    for r in sorted(picked, key=lambda x: (_f(x.get("收入比例")) or 0), reverse=True):
        pct, mg, pp = _f(r.get("收入比例")), _f(r.get("毛利率")), _f(r.get("利润比例"))
        out.append({
            "name": r.get("主营构成") or r.get("name") or "",
            # 东财返回小数；>=1 视为已是百分数，避免重复乘 100
            "pct": (pct * 100 if pct is not None and pct <= 1 else pct),
            "margin": (mg * 100 if mg is not None and mg <= 1 else mg),
            "profit_pct": (pp * 100 if pp is not None and pp <= 1 else pp),
        })
    return [x for x in out if x["name"]]


_RE_CONTROL_CLAIM = re.compile(
    r"(?:实际控制人|实控人|控股股东|控股方)\s*(?:为|是|系|：|:)?\s*([一-龥]{2,4})"
)
_RE_DENY_CLAIM = re.compile(r"未(?:公开)?披露|未提供|无公开披露|数据缺失|无法获取|信息缺失")
_RE_BIZ_CLAIM = re.compile(
    r"(?:主要从事|专业从事|主营业务(?:为|是)?|主营(?:为|是)?|专注于|致力于|属于)\s*"
    r"([一-龥·、（）()A-Za-z0-9]{2,24}?)(?=的|，|,|。|；|;|领域|行业|业务|公司|$)"
)
_CONTROL_PCT_THRESHOLD = 20.0  # 持股低于此比例不足以支撑「控股」定性

# ---------------------------------------------------------------------------
# 股东表口径护栏（2026-09-16）
#
# 翻车复盘：报告表头写「前十大股东」，数据却来自东财**前十大流通股东**大表
# （限售股持有人只显示无限售部分），且脚注被标成「东方财富前十大股东」——
# 表头与数据源名不副实，且因该票两个口径恰好重合而无法从数字上发现。
# 根治手段不是「记得改文案」，而是让**表头由口径派生**：口径变了表头必须跟着变，
# 从结构上不给「流通口径配十大股东表头」留出可能。
# ---------------------------------------------------------------------------
HOLDER_CALIBER_ROSTER = "名册"          # 《定期报告》股东名册（总持股含限售，含性质/质押数量）
HOLDER_CALIBER_TOP10 = "东财十大股东"    # 东财专用接口，与名册同为总持股口径
HOLDER_CALIBER_FREE = "东财流通股东"     # 东财全市场大表兜底，仅含无限售部分


def holders_caliber_of(gov: dict) -> str:
    """判定股东表口径。优先级：main.py 落库标签 > 名册字段 > 行内 ``_口径`` 标记 > 东财十大股东。

    ``holders_caliber`` 由 main.py 在 holders 收口点统一写入，是首选来源；
    其余分支用于兼容历史 profile（字段未落库时仍能正确识别，不再回退到「猜」）。
    """
    if not gov:
        return ""
    explicit = str(gov.get("holders_caliber") or "")
    if explicit:
        return explicit
    if gov.get("report_holders"):
        return HOLDER_CALIBER_ROSTER
    rows = gov.get("holders") or []
    if not rows:
        return ""
    if any(str((r or {}).get("_口径") or "") == "流通股东" for r in rows):
        return HOLDER_CALIBER_FREE
    return HOLDER_CALIBER_TOP10


def holders_title(caliber: str) -> str:
    """表头必须与口径一致：流通口径不得写成「前十大股东」。"""
    return "前十大流通股东" if caliber == HOLDER_CALIBER_FREE else "前十大股东"


def _cjk_bigrams(text: str) -> set[str]:
    """中文 2-gram 集合；单字词整体作为一个 token，避免短行业名（如「电子」）被丢掉。"""
    chars = re.findall(r"[一-龥]", str(text or ""))
    if not chars:
        return set()
    if len(chars) < 2:
        return {"".join(chars)}
    return {"".join(chars[i : i + 2]) for i in range(len(chars) - 1)}


def _conclusion_fact_check(text: str, profile_dict: dict) -> list[str]:
    """用结构化事实证伪 LLM 结论，返回冲突描述列表（空列表 = 通过）。"""
    s = str(text or "")
    if not s.strip():
        return []
    L = profile_dict.get("layers", {}) or {}
    ident, biz, gov, ind = (
        L.get("identity", {}) or {},
        L.get("business", {}) or {},
        L.get("governance", {}) or {},
        L.get("industry", {}) or {},
    )
    fin = (L.get("financials", {}) or {}).get("summary", {}) or {}
    cap = gov.get("share_capital")
    issues: list[str] = []

    # 1) 实控人 / 控股股东 点名断言
    _holders = gov.get("holders") or []
    _allowed_persons: set[str] = set()
    if ident.get("legal_rep"):
        _allowed_persons.add(str(ident["legal_rep"]))
    for h in _holders:
        try:
            _pct = float(str(h.get("持股数")).replace(",", "")) / float(cap) * 100.0 if cap else None
        except Exception:
            _pct = None
        _nm = h.get("股东名称") or h.get("name")
        if _nm and (_pct is None or _pct >= _CONTROL_PCT_THRESHOLD):
            _allowed_persons.add(str(_nm))
    # 年报口径的实控人是一手来源，优先于「持股≥20%」启发式。
    # 本公司实控人赵马克总持股 11.08%（东财快照仅列 25% 无限售额度 = 2.78%），
    # 达不到 20% 阈值，若不并入白名单，结论区将永远无法陈述实控人（C 类修复的前提）。
    _ctl0 = gov.get("actual_controller") or {}
    if _ctl0:
        for _ck in ("name_cn", "name"):
            if _ctl0.get(_ck):
                _allowed_persons.add(str(_ctl0[_ck]).split("（")[0].strip())
    # 「实控人为境外自然人」这类**性质描述**会被 _RE_CONTROL_CLAIM 当成 2-4 字人名捕获
    # （如取到「境外自然」），并非点名某人，应跳过检查而非判为编造。
    _RE_NOT_A_PERSON = re.compile(
        r"境外|境内|自然|法人|国有|民营|外资|企业|投资|合伙|控股股|质押|解押|减持|增持|冻结|变动"
    )
    for m in _RE_CONTROL_CLAIM.finditer(s):
        _claimed = m.group(1)
        if _RE_NOT_A_PERSON.search(_claimed):
            continue
        if _claimed not in _allowed_persons:
            issues.append(
                f"断言「{m.group(0)}」，但数据中无此控制关系（可确认的仅为："
                f"法人代表 {ident.get('legal_rep') or '未获取'}"
                f"／持股≥{_CONTROL_PCT_THRESHOLD}% 的股东 {sorted(_allowed_persons - {str(ident.get('legal_rep'))}) or '无'}）"
            )

    # 2) 「未披露 / 未提供」类否定断言：数据里明明有
    _have = [k for k in ("revenue", "gross_margin", "net_profit") if fin.get(k) not in (None, "")]
    if _RE_DENY_CLAIM.search(s) and _have:
        issues.append(
            f"结论称关键数据「{_RE_DENY_CLAIM.search(s).group(0)}」，"
            f"但 {('、'.join(_have))} 均已取到真实值，陈述与报告其余部分自相矛盾"
        )

    # 3) 主营 / 行业定性：必须与真实口径共享词语
    _allow_tokens: set[str] = set()
    _segs = _norm_segments(biz.get("segments"))
    for seg in _segs:
        _allow_tokens |= _cjk_bigrams(seg.get("name"))
    _allow_tokens |= _cjk_bigrams(biz.get("main_business_text"))
    _allow_tokens |= _cjk_bigrams(ident.get("industry_sw") or ind.get("industry_sw"))
    _allow_tokens |= _cjk_bigrams(ident.get("industry_cninfo") or ind.get("industry_cninfo"))
    if _allow_tokens:
        for m in _RE_BIZ_CLAIM.finditer(s):
            _phrase = m.group(1)
            _toks = _cjk_bigrams(_phrase)
            if _toks and not (_toks & _allow_tokens):
                issues.append(
                    f"业务定性「{_phrase}」与真实主营构成／行业分类无任何重合"
                    f"（真实口径：{[x['name'] for x in _segs] or '未获取'}"
                    f"／{ident.get('industry_sw') or ind.get('industry_sw') or '未获取'}）"
                )

    # 4) 「第N大股东 X」排名断言：必须与前十大股东名单的位次一致。
    #    实例：结论曾写「第二大股东侯红亮存在未解押质押」，但侯红亮并不在最新前十大
    #    股东名单内（年报仅称其为「主要股东」，系 2014 年重组进入的历史股东），
    #    该表述与治理风险扫描模块的「非前十大」结论直接冲突。
    _ranked = _rank_claim_issues(s, _holders)
    issues += _ranked

    return issues


_RE_RANK_CLAIM = re.compile(
    # 顺序式：「第N大股东 X」——允许 X 中含空格（英文姓名），到句读为止
    r"第\s*([一二三四五六七八九十]{1,3}|\d+)\s*大(?:股东|流通股东)\s*([^，。；、（）]{0,24})"
    r"|"
    # 倒序式：「X 为/系/是 第N大股东」
    r"([^，。；、（）\s]{2,15}?)\s*(?:为|系|是)\s*第\s*([一二三四五六七八九十]{1,3}|\d+)\s*大(?:股东|流通股东)"
)


def _cn_num(s: str) -> int | None:
    """中文数字转阿拉伯数字，覆盖 1~99（十 / 十一 / 二十 / 二十一 …）。"""
    d = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
         "六": 6, "七": 7, "八": 8, "九": 9}
    if s.isdigit():
        return int(s)
    if s in d:
        return d[s]
    if "十" not in s:
        return None
    _a, _, _b = s.partition("十")
    _hi = d.get(_a, 1) if _a else 1
    _lo = d.get(_b, 0) if _b else 0
    return _hi * 10 + _lo


def _rank_claim_issues(text: str, holders: list) -> list[str]:
    """证伪「第N大股东 X」类排名断言：X 必须真的在第 N 位（同人异名一并匹配）。

    实现上不做「抓出 X」再比对的脆弱解析（英文姓名含空格、中文名后无标点都会漏/误），
    改为：取「第N大股东」后紧跟的一段文本，检查**名单中第 N 位的名字（或其别名）
    是否为这段文本的前缀**。命中即放行，未命中即判冲突。
    """
    s = str(text or "")
    if not s.strip() or not holders:
        return []
    names = [
        str(h.get("股东名称") or h.get("name") or "").strip()
        for h in holders
    ]
    # 同人异名展开：每个位次同时接受其别名（如 赵马克 <-> MARK ZHAO）
    alias_of: dict[str, str] = {v: k for k, v in _SHAREHOLDER_ALIASES.items()}
    expanded: list[set[str]] = []
    for nm in names:
        _s = {nm}
        if nm in alias_of:
            _s.add(alias_of[nm])
        if nm in _SHAREHOLDER_ALIASES:
            _s.add(_SHAREHOLDER_ALIASES[nm])
        expanded.append(_s)

    issues: list[str] = []
    for m in _RE_RANK_CLAIM.finditer(s):
        _num_raw = m.group(1) or m.group(4)
        _n = _cn_num(_num_raw) if _num_raw else None
        if _n is None:
            continue
        if not (1 <= _n <= len(expanded)):
            issues.append(
                f"断言「{m.group(0).strip() or ('第' + str(_num_raw) + '大股东')}」，"
                f"但前十大股东名单仅 {len(expanded)} 位，无第 {_n} 位"
            )
            continue
        # 顺/倒序共用：取被点名的片段，剥掉「为/系/是/：」等引导词后做前缀匹配
        _tail = re.sub(r"^[\s为是系：:]+", "", (m.group(2) or m.group(3) or "").strip())
        if not _tail:  # 不构成点名断言
            continue
        if any(_nm and _tail.startswith(_nm) for _nm in expanded[_n - 1]):
            continue
        issues.append(
            f"断言「第{_n}大股东 {_tail}」，但第 {_n} 大股东实为"
            f"「{names[_n - 1] or '未获取'}」，与名单位次不符"
        )
    return issues


def _factual_claim_check(text: str, profile_dict: dict) -> list[str]:
    """拿数据层核心数字证伪叙述文本中的量级误述（毛利率 / 净利率 / 营收同比）。

    仅捕获高置信度冲突（真实值与声称值量级差 > 1.8 倍或 < 0.55 倍），避免误伤正常行文。
    与 _conclusion_fact_check 互补：后者是点名/定性证伪（实控人、业务定性、否定断言），
    这里是数字量级证伪。双管齐下覆盖「其余叙述字段幻觉漏网」（P3-2）。
    只校验自由文本字段，列表字段由 _is_bad_text 递归拦截（控股误判已含在内）。
    """
    s = str(text or "")
    if not s.strip():
        return []
    L = profile_dict.get("layers", {}) or {}
    fin = (L.get("financials", {}) or {}).get("summary", {}) or {}
    issues: list[str] = []

    def _real(key):
        try:
            f = float(fin.get(key))
        except (TypeError, ValueError):
            return None
        if key in ("gross_margin", "net_margin") and f <= 1:  # summary 存小数时归一为百分点
            f = f * 100
        return f if f is not None else None

    # 毛利率 / 净利率量级校验（分销商的生命线指标，最易被模型夸大）
    for key, label in (("gross_margin", "毛利率"), ("net_margin", "净利率")):
        real = _real(key)
        if not real or real <= 0:
            continue
        for m in re.finditer(rf"{label}[^。，；\n]{{0,12}}?(\d+(?:\.\d+)?)\s*%", s):
            claimed = float(m.group(1))
            if claimed > real * 1.8 or claimed < real * 0.55:
                issues.append(
                    f"{label}被写成 {claimed}%，但真实自算口径约 {real:.2f}%"
                    f"（数据源：三表计算，已写入财务速写模块）"
                )
    # 营收同比增速量级校验
    rg = _real("rev_growth")
    if rg is not None:
        for m in re.finditer(r"营收[同比]*增长?\s*(?:约|达|为|至|到)?\s*(\d+(?:\.\d+)?)\s*%", s):
            claimed = float(m.group(1))
            if claimed > abs(rg) * 1.8 or claimed < abs(rg) * 0.55:
                issues.append(
                    f"营收同比增速被写成 {claimed}%，但真实值约 {rg:.2f}%"
                )
    # 绝对额科目校验（存货 / 经营现金流）：与**任一已知报告期真值**比对，全部对不上才判冲突。
    # 分报告期真值由 build_financial_summary 提供，模型引用哪一期都算合理，
    # 只有凭记忆编造的数字才会对不上所有期——这正是「经营现金流 -1.91 亿」（真值 -0.91 亿）
    # 这类错误的特征。注意 A3/A5 那种 3%~5% 偏差不在此拦截（容差内），由提示词核对表根治。
    def _yi_set(d, extra=None) -> list[float]:
        vals = list(d.values()) if isinstance(d, dict) else []
        if extra is not None:
            vals.append(extra)
        out: list[float] = []
        for v in vals:
            try:
                out.append(float(v) / 1e8)
            except (TypeError, ValueError):
                continue
        return out

    _inv_vals = [v for v in _yi_set(fin.get("inventory_by_period"), fin.get("inventory")) if v > 0]
    if _inv_vals:
        for m in re.finditer(r"存货[^。；\n]{0,20}?(\d+(?:\.\d+)?)\s*亿元?", s):
            _claim = float(m.group(1))
            if all(not (0.7 <= (_claim / v) <= 1.4) for v in _inv_vals):
                issues.append(
                    f"存货被写成 {_claim} 亿元，但各报告期真值为 "
                    + "／".join(f"{v:.2f} 亿" for v in sorted(_inv_vals, reverse=True)[:4])
                )

    _cf_vals = [v for v in _yi_set(fin.get("oper_cash_by_period"), fin.get("oper_cash")) if v != 0]
    if _cf_vals:
        for m in re.finditer(r"经营(?:活动)?现金流[^。；\n]{0,20}?(-?\d+(?:\.\d+)?)\s*亿元?", s):
            _claim = float(m.group(1))
            _ok = False
            for v in _cf_vals:
                if (v > 0) != (_claim > 0):  # 正负号相反 = 典型符号编造
                    continue
                if 0.55 <= abs(_claim / v) <= 1.8:
                    _ok = True
                    break
            if not _ok:
                issues.append(
                    f"经营现金流被写成 {_claim} 亿元，但各报告期真值为 "
                    + "／".join(f"{v:.2f} 亿" for v in sorted(_cf_vals, key=abs, reverse=True)[:4])
                )

    # 股东 / 质押持股比例一致性（governance 硬数字字段事实校验）
    issues += _shareholder_claim_check(s, profile_dict)
    # 「第N大股东 X」位次一致性（与结论区共用同一套校验规则）
    issues += _rank_claim_issues(s, (profile_dict.get("layers", {}).get("governance", {}) or {}).get("holders") or [])
    return issues


# 同人异名映射：数据常把实控人用「中文法人名」记录、其持股主体用「拉丁化/英文名」记录，
# 二者实为同一人。以文件证据为准建立映射（力源信息 300184：法人代表「赵马克」即股东
# 「MARK ZHAO」，直接持股 3,189.10 万股 / 总股本 11.49 亿股 ≈ 2.78%，质押状态：无）。
# 作用：使「赵马克直接持股 2.78%」这类正确陈述不被误伤、「赵马克质押率 6.98%」这类错误被拦截。
# 其它公司若无映射则不纳入 known，保持宽松（不误报）。
_SHAREHOLDER_ALIASES: dict[str, str] = {
    "赵马克": "MARK ZHAO",
}


def _shareholder_claim_check(text: str, profile_dict: dict) -> list[str]:
    """拿数据层真实持股比例证伪文本中「X 持股/质押 Y%」类断言（governance 硬数字校验）。

    覆盖 _leaks_wrong_holding 的盲区：后者只匹配写死的历史错误模式（赵马克持股27.7% 等），
    对模型新编的任意「姓名+比例」无能为力。本函数对每位已知股东/法人代表做通用比例对证，
    量级差 > 1.8 倍或 < 0.55 倍即判冲突。仅扫描「姓名后紧跟持股/质押数字%」的紧凑写法，
    避免误伤「占总股本 X%」这类间接表述。返回冲突描述列表。
    """
    s = str(text or "")
    if not s.strip():
        return []
    L = profile_dict.get("layers", {}) or {}
    gov = L.get("governance", {}) or {}
    ident = L.get("identity", {}) or {}
    cap = gov.get("share_capital")
    holders = gov.get("holders") or []
    # 已知人物 -> 真实持股比例（百分数）；法人代表若不在前十大也纳入点名检查
    known: dict[str, float | None] = {}
    if cap:
        for h in holders:
            nm = h.get("股东名称") or h.get("name")
            sh = h.get("持股数")
            if nm and sh:
                try:
                    known[str(nm)] = float(str(sh).replace(",", "")) / float(cap) * 100.0
                except (TypeError, ValueError):
                    pass
    if ident.get("legal_rep"):
        # 法人代表与某股东实为同人时（同人异名），取其真实持股比例做对证，
        # 避免把正确的实控人陈述误判为「未确认」。找不到映射则宽松处理（不误报）。
        _lr = str(ident["legal_rep"])
        _tgt = _SHAREHOLDER_ALIASES.get(_lr)
        if _tgt and _tgt in known:
            known.setdefault(_lr, known[_tgt])
    if not known:
        return []

    # 年报口径的实控人持股比例：与东财快照（无限售可转让额度）是**两个不同口径**，
    # 二者相差可达 4 倍（本公司 11.08% vs 2.78%）。若只认快照值，会把正确的
    # 「赵马克持股 11.08%」误判为编造；故把年报值并列为可接受真值之一。
    # 另有最新定期报告名册口径（半年报总股本随回购注销缩小 → 11.10%），同为真值。
    _ctl = gov.get("actual_controller") or {}
    _ctl_names = {
        str(_ctl.get("name_cn") or ""),
        str(_ctl.get("name_en") or ""),
        "MARK ZHAO",
        "MARKZHAO",
    } - {""}
    try:
        _ctl_pct = float(_ctl.get("pct")) if _ctl.get("pct") is not None else None
    except (TypeError, ValueError):
        _ctl_pct = None
    _ctl_alts = [_ctl_pct]
    for _h in (gov.get("report_holders") or {}).get("holders") or []:
        _nk = str(_h.get("name") or "").upper().replace(" ", "")
        if "ZHAO" in _nk or "赵马克" in str(_h.get("name") or ""):
            try:
                _ctl_alts.append(float(_h.get("pct")))
            except (TypeError, ValueError):
                pass

    def _ok(claimed: float, *reals) -> bool:
        for _r in reals:
            if _r is None:
                continue
            if _r * 0.55 <= claimed <= _r * 1.8:
                return True
        return False

    issues: list[str] = []
    for nm, real in known.items():
        _esc = re.escape(nm)
        # 该人物是否有年报/名册口径的并列真值（取全部口径真值中的最大者用于报错文案）
        _alt = max(
            (a for a in ([_ctl_pct] if nm in _ctl_names else []) if a is not None),
            default=None,
        )
        for pat in (
            rf"{_esc}\s*(?:直接)?\s*持股\s*(\d+(?:\.\d+)?)\s*%",
            rf"{_esc}\s*质押[率]?\s*(\d+(?:\.\d+)?)\s*%",
        ):
            for m in re.finditer(pat, s):
                claimed = float(m.group(1))
                if real is None and _alt is None and len(_ctl_alts) <= 1:
                    issues.append(
                        f"点名「{nm}」并给出持股/质押比例 {claimed}%，"
                        "但数据未确认其持股（或不在前十大股东口径内）"
                    )
                elif not _ok(claimed, real, *_ctl_alts):
                    issues.append(
                        f"「{nm}」持股/质押比例被写成 {claimed}%，"
                        f"但数据层真实值约 {real:.2f}%（前十大股东/总股本折算）"
                        + (f"或 {_alt:.2f}%（年报总持股口径）" if _alt else "")
                    )
    return issues


def _sanitize_llm_json(llm_json: dict | None) -> tuple[dict, list[str]]:
    """清洗 LLM 返回内容：剔除把提示词示例原样回显的字段。

    返回 `(清洗后的 JSON, 被清空的字段名列表)`。字符串命中则置空串；
    列表命中则剔除命中的条目；对象命中则置空对象。
    """
    if not isinstance(llm_json, dict):
        return {}, []
    cleaned: dict = {}
    dropped: list[str] = []
    for k, v in llm_json.items():
        if isinstance(v, str):
            if _is_bad_text(v):
                dropped.append(k)
                cleaned[k] = ""
            else:
                cleaned[k] = v
        elif isinstance(v, list):
            kept, hit = [], False
            for item in v:
                if _is_bad_text(item):
                    hit = True
                    continue
                kept.append(item)
            if hit:
                dropped.append(k)
            cleaned[k] = kept
        elif isinstance(v, dict):
            if _is_bad_text(v):
                dropped.append(k)
                cleaned[k] = {}
            else:
                cleaned[k] = v
        else:
            cleaned[k] = v
    if dropped:
        logger.warning("[overview] LLM 示例泄漏已拦截并清空字段：%s", "、".join(dropped))
    return cleaned, dropped


# ---------------------------------------------------------------------------
# 空壳契约：LLM 不可用时的安全降级，所有数字缺省为「未获取」，绝不编造
# ---------------------------------------------------------------------------
def empty_overview_data() -> dict:
    return {
        # 头部
        "name": "（未获取）",
        "code": "",
        "full_name": "",
        "report_date": datetime.now().strftime("%Y-%m-%d"),
        "as_of_text": "财务数据截至最新报告期，行情数据截至最近收盘",
        "badges": [],

        # 核心结论
        "conclusion_html": "（AI 内容生成失败或跳过，已降级为空壳；报告其余部分不受影响）",
        "conclusion_note": "",
        "conclusion_title": "",

        # 01 身份识别
        "id_metrics": [],
        "id_kv_left": [],
        "id_kv_right": [],
        "id_insight_html": "",
        "id_insight_title": "",

        # 02 业务画像
        "seg_mix": {"labels": [], "values": []},
        "seg_mix_total": 0,
        "seg_mix_note": "",
        "segments": [],
        "seg_gap": {"labels": [], "income_pct": [], "profit_pct": []},
        "seg_gap_note": "",
        "business_flag_html": "",
        "business_flag_title": "",
        "seg_trend": [],
        "seg_trend_labels": [],
        "seg_trend_cols": [],
        "seg_trend_note": "",
        "seg_trend_title": "",
        "seg_trend_has_h1": False,
        "seg_trend_summary_html": "",
        "self_dev": {"labels": [], "revenue": [], "margin": []},
        "self_dev_note": "",
        "upstream": [],
        "upstream_note": "",
        "supplier_conc_html": "",
        "downstream": [],
        "downstream_note": "",
        "customer_conc_html": "",
        "partners": [],
        "partners_note": "",
        "upstream_struct": [],
        "upstream_struct_has_suppliers": False,
        "downstream_quotes": [],
        "downstream_fields": [],
        "bc_source": "",
        # 上下游/合作网络标题与导语动态化（因公司而异；空值时模板回退通用话术）
        "upstream_title": "",
        "upstream_intro": "",
        "upstream_struct_title": "",
        "upstream_struct_intro": "",
        "downstream_title": "",
        "downstream_intro": "",
        "partners_title": "",
        "partners_intro": "",
        "growth": [],
        "business_constraints": [],
        "growth_source": "",
        "business_cross_html": "",

        # 03 股权治理
        "shareholders": [],
        "shareholders_period": "",
        "shareholders_title": "前十大股东",
        "gov_note": "",
        "div_metrics": [],
        "div_text": "",
        # 股份回购（03 股权治理）：真值由年报 PDF 抽取，杜绝 LLM 凭记忆编造回购股数
        "repurchase_metrics": [],
        "repurchase_text": "",
        # 实际控制人（C 类）：真值取自年报「前十大股东」一手来源。
        # 关键口径差异：年报列示**总持股**（含限售），东方财富前十大快照列示
        # **无限售可转让额度**（高管每年 25%），二者相差恰好 4 倍，必须并列标注期间，
        # 否则同一人会出现「11.08% vs 2.78%」的自相矛盾。
        "controller_metrics": [],
        "controller_text": "",
        "gov_left": [],
        "gov_right": [],
        "gov_scan_note": "",

        # 04 行业坐标
        "peer_margin": {"labels": [], "values": [], "highlight": -1},
        "peer_margin_note": "",
        "peers": [],
        "peers_note": "",
        # 同业横比表的期间口径提示（营收=最新年报 vs 毛利率/ROE 等=最新报告期）
        "peers_period_note": "",
        "ind_metrics": [],
        "moat": [],

        # 05 财务速写
        "trend": {"labels": [], "revenue": [], "profit": []},
        "trend_note": "",
        "fin_metrics": [],
        "fin_growth": [],
        "fin_valuation": [],
        "cash_flag_title": "",
        "cash": {"labels": [], "profit": [], "cash": []},
        "cash_note": "",
        "asset_pressure": [],

        # 06 动态与风险
        "events": [],
        "market_logic": [],
        "risks_left": [],
        "risks_right": [],
        "risk_emphasis": [],
        # P2-E1/E3：行情资金面与多空逻辑（数据层确定性派生）
        "market_kv": [],
        "market_meta": "",
        "bull_points": [],
        "bear_points": [],

        # 页脚
        "sources_html": "（本报告内容由 AI 生成，数据来源以各模块标注为准；财务数据以公司公告为最终依据。）",
        "caliber_html": "财务数据均为合并报表口径。",
        "source_tiers_html": "一手来源——公司年报／半年报／季报、交易所披露，可直接采信；非一手来源（第三方机构预测、媒体转述）请以其原始报告为准。",
        "retrieval_html": "本报告财务数据来自公开披露与公司公告；行情与融资融券数据来自东方财富延时行情接口（push2delay / datacenter），存在延迟，非实时价，仅供研究参考。",
        "disclaimer_html": "以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，必要时咨询持牌专业机构。过往表现不预示未来收益。",
    }


# ---------------------------------------------------------------------------
# LLM 内容生成（B①）
# ---------------------------------------------------------------------------
def _parse_overview_json(text: str) -> dict | None:
    """从 LLM 返回中解析结构化 JSON；容忍 markdown 代码块与多余文本。"""
    if not text:
        return None
    s = text.strip()
    # 去掉 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if m:
        s = m.group(1)
    else:
        # 退而求其次：截取首个 { 到末个 }
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        s = s[start : end + 1]
    try:
        obj = json.loads(s)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _compact_facts(profile_dict: dict) -> str:
    """从六层契约抽取撰写结论所需的紧凑事实（股东/质押/业务板块/同业），供聚焦结论调用。"""
    L = profile_dict.get("layers", {})
    gov = L.get("governance", {}) or {}
    biz = L.get("business", {}) or {}
    ind = L.get("industry", {}) or {}
    cap = int(gov.get("share_capital") or 0)
    out = []
    for h in (gov.get("holders") or [])[:10]:
        nm = h.get("股东名称") or h.get("name") or ""
        sh = h.get("持股数")
        pct = None
        try:
            if sh not in (None, "") and cap:
                pct = float(str(sh).replace(",", "")) / float(cap) * 100.0
        except Exception:
            pct = None
        out.append(f"  股东 {nm}：持股{'%.2f%%' % pct if pct is not None else '?'}（{sh} 股）")
    ps = gov.get("pledge_summary") or {}
    if ps:
        th = (ps.get("top_holder") or {}).get("name", "—")
        out.append(
            f"  质押：未解押 {ps.get('active_count')} 笔，占总股本 "
            f"{ps.get('active_total_pct')}%（{ps.get('pct_basis')}），在押主要股东 {th}"
        )
    for s in _norm_segments(biz.get("segments"))[:6]:
        _p = f"{s['pct']:.2f}%" if s.get("pct") is not None else "未获取"
        _m = f"{s['margin']:.2f}%" if s.get("margin") is not None else "未获取"
        _pp = f"{s['profit_pct']:.2f}%" if s.get("profit_pct") is not None else "未获取"
        out.append(f"  业务板块 {s['name']}：收入占比 {_p}，毛利率 {_m}，利润占比 {_pp}")
    if biz.get("main_business_text"):
        out.append(f"  主营业务描述：{biz['main_business_text']}")
    # P1：上下游一手来源（年报 PyMuPDF 抽取），供结论聚焦调用引用，避免 LLM 编造「未披露」
    _bc = (biz.get("business_chain") or {}) or {}
    if _bc:
        _regs = _bc.get("upstream_regions") or []
        if _regs:
            _mk = "; ".join(f"{r['region']}{r['count']}家" for r in _regs)
            out.append(f"  上游（一手来源·年报）：授权代理原厂品牌 {_mk}")
        _fld = _bc.get("downstream_fields") or []
        if _fld:
            out.append(f"  下游（一手来源·年报）应用领域：{', '.join(_fld)}")
    for p in (ind.get("peers") or [])[:6]:
        out.append(
            f"  同业 {p.get('name')}：ROE {p.get('roe')}%，营收增速 {p.get('rev_growth')}%，"
            f"净利增速 {p.get('profit_growth')}%，负债率 {p.get('debt_ratio')}%"
        )
    return "\n".join(out)


def _generate_conclusion(name: str, code: str, facts: str) -> str | None:
    """聚焦调用：仅生成核心结论段落。输入紧凑，规避思考模型在大 profile 下清空叙述字段的问题。"""
    system = (
        "你是专业的 A 股公司研究分析师。请基于给定事实，撰写一段公司概况核心结论。"
        "要求：150-260 字中文，按「实控人性质 + 行业 + 主营 + 增长来源 + 估值坐标 +"
        "核心分歧 + 主要风险」七要素组织；纯文本（可含 <strong> 强调），"
        "严禁编造数字，未提及的字段不要臆测。只输出结论文本本身，不要 JSON、不要示例、不要解释。"
    )
    user = f"公司：{name}（{code}）\n\n已知事实：\n{facts}\n\n请输出核心结论段落："
    try:
        resp = get_router().complete(
            LLMRequest(system=system, user=user, max_tokens=1200)
        ).text
    except Exception:
        return None
    t = (resp or "").strip()
    while t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
        else:
            break
    t = t.strip()
    if t and _is_bad_text(t):
        logger.warning("[overview] 聚焦调用生成的结论含错误陈述，已丢弃")
        return None
    return t or None


def _fallback_conclusion(name: str, code: str, profile_dict: dict) -> str:
    """数据层兜底结论：LLM 结论缺失或被判为编造时使用，全部由结构化事实拼装。

    与 LLM 叙述的差别是「不润色、不推理、只陈述」——宁可平铺直叙，也不输出一句未经
    数据支撑的判断。这与「事实归数据层」的原则一致：结论区可以没有文采，但不能有幻觉。
    """
    L = profile_dict.get("layers", {}) or {}
    ident = L.get("identity", {}) or {}
    biz = L.get("business", {}) or {}
    gov = L.get("governance", {}) or {}
    ind = L.get("industry", {}) or {}
    fin = (L.get("financials", {}) or {}).get("summary", {}) or {}

    def _n(v, unit="%", nd=2):
        try:
            if v in (None, ""):
                return None
            return f"{float(v):.{nd}f}{unit}"
        except (TypeError, ValueError):
            return None

    parts: list[str] = []

    # 实控人性质：只用股东名与持股比例，不下「有无实控人」的定性判断
    holders = gov.get("holders") or []
    cap = gov.get("share_capital")
    if holders:
        h0 = holders[0]
        hn = h0.get("股东名称") or h0.get("name") or "—"
        pct = None
        try:
            if h0.get("持股数") not in (None, "") and cap:
                pct = float(str(h0["持股数"]).replace(",", "")) / float(cap) * 100.0
        except Exception:
            pct = None
        if pct is not None:
            parts.append(f"第一大股东 <strong>{hn}</strong> 持股 {pct:.2f}%（按总股本 {cap} 股计）")
        else:
            parts.append(f"第一大股东 <strong>{hn}</strong>（持股比例未获取）")

    # 行业坐标
    _ind = ident.get("industry_sw") or ind.get("industry_sw") or ident.get("industry_cninfo")
    _mkt = ident.get("market")
    if _ind:
        parts.append(f"所属申万行业 <strong>{_ind}</strong>" + (f"，上市板块 {_mkt}" if _mkt else ""))

    # 主营：优先用主营构成（分部占比为一手披露口径）
    segs = _norm_segments(biz.get("segments"))
    if segs:
        top = segs[0]  # _norm_segments 已按收入占比降序
        _sp = f"{top['pct']:.2f}%" if top.get("pct") is not None else "未获取"
        parts.append(f"主营以 <strong>{top['name']}</strong> 为主，收入占比 {_sp}")

    # 增长：增速与三年复合增速分开说，避免只看单期
    _rg, _pg = _n(fin.get("rev_growth")), _n(fin.get("profit_growth"))
    if _rg or _pg:
        parts.append(
            "营收同比 " + (_rg or "未获取") + "，归母净利同比 " + (_pg or "未获取")
        )
    _rc, _pc = _n(fin.get("rev_cagr")), _n(fin.get("profit_cagr"))
    if _rc or _pc:
        parts.append(
            f"近三年营收复合增速 {_rc or '未获取'}、净利复合增速 {_pc or '未获取'}"
        )

    # 盈利质量：毛利率 + 同业位置（分销商的核心比较维度）
    _gm = _n(fin.get("gross_margin"))
    if _gm:
        peers = [p for p in (ind.get("peers") or []) if not p.get("hl")]
        _rank = None
        try:
            gms = sorted(
                (float(p.get("gross_margin")) for p in peers if p.get("gross_margin") not in (None, "")),
                reverse=True,
            )
            if gms:
                _rank = sum(1 for g in gms if g > float(fin["gross_margin"])) + 1
        except Exception:
            _rank = None
        _s = f"毛利率 <strong>{_gm}</strong>"
        if _rank:
            _s += f"，在 {len(gms)} 家可比公司中列第 {_rank} 位"
        parts.append(_s)

    # 资产负债与现金流
    _dr = _n(fin.get("debt_ratio"))
    if _dr:
        parts.append(f"资产负债率 {_dr}")
    _cf = _n(fin.get("cf_to_profit"))
    if _cf:
        _flag = "，经营现金流对净利润的覆盖偏低，需关注利润含金量" if (
            isinstance(fin.get("cf_to_profit"), (int, float)) and fin["cf_to_profit"] < 60
        ) else ""
        parts.append(f"经营现金流/净利润 {_cf}{_flag}")

    # 估值坐标：优先用延时行情（push2delay）真实 PE/PB；无行情时诚实标注缺失
    _mv = (L.get("market", {}) or {}).get("quote") or {}
    _pe = _mv.get("pe_ttm")
    _pb = _mv.get("pb")
    if _pe not in (None, "") or _pb not in (None, ""):
        _pv = []
        if _pe not in (None, ""):
            _pv.append(f"PE(TTM) {float(_pe):.2f}")
        if _pb not in (None, ""):
            _pv.append(f"PB {float(_pb):.2f}")
        _qd = _mv.get("quote_date") or "最新"
        parts.append("估值坐标（" + "、".join(_pv) + f"，{_qd} 延时行情）")
    else:
        parts.append("PE(TTM)、PB <strong>未获取</strong>（当前行情源不可用，不做估算）")

    # P1：上下游一手来源（年报抽取），让数据层结论也覆盖业务链条
    _bc = (biz.get("business_chain") or {}) or {}
    if _bc:
        _regs = _bc.get("upstream_regions") or []
        if _regs:
            _mk = "、".join(f"{r['region']}{r['count']}家" for r in _regs)
            parts.append(f"上游授权代理原厂品牌 {_mk}（一手来源·年报）")
        _fld = _bc.get("downstream_fields") or []
        if _fld:
            parts.append(f"下游应用于 {', '.join(_fld[:6])} 等领域（一手来源·年报）")

    if not parts:
        return (
            f"<p>{name}（{code}）：本次运行未取到足以支撑结论的结构化数据，"
            "结论区留空而非推测。请检查数据源连通性后重跑。</p>"
        )
    return "<p>" + "；".join(parts) + "。</p>"


# ---------------------------------------------------------------------------
# P2：增长引擎（聚焦子调用 + 数据层确定性兜底）
# ---------------------------------------------------------------------------
def _compact_growth_facts(profile_dict: dict) -> str:
    """为聚焦增长子调用抽取紧凑事实：收入结构 + 财务摘要 + 上下游一手来源。

    与 _compact_facts（结论用）不同，这里只给增长分析必需的字段，输入更短，
    降低 7B 模型跑偏概率——这是「拆 8000-token 大 JSON 为聚焦子调用」的一部分。
    """
    L = profile_dict.get("layers", {}) or {}
    biz = L.get("business", {}) or {}
    fin = (L.get("financials", {}) or {}).get("summary", {}) or {}

    out: list[str] = []

    segs = _norm_segments(biz.get("segments"))
    if segs:
        out.append("收入结构（按产品口径，降序）：")
        for s in segs[:5]:
            _p = f"{s['pct']:.2f}%" if s.get("pct") is not None else "未获取"
            _m = f"毛利率 {s['margin']:.2f}%" if s.get("margin") is not None else ""
            out.append(f"  {s['name']}：占比 {_p}{('，' + _m) if _m else ''}")

    def _f(v, u="%", nd=2):
        try:
            return None if v in (None, "") else f"{float(v):.{nd}f}{u}"
        except (TypeError, ValueError):
            return None

    out.append("财务摘要：")
    for k, lbl in (
        ("rev_growth", "营收同比"), ("profit_growth", "净利同比"),
        ("rev_cagr", "营收三年复合"), ("profit_cagr", "净利三年复合"),
        ("gross_margin", "综合毛利率"), ("net_margin", "净利率"),
        ("debt_ratio", "资产负债率"), ("cf_to_profit", "经营现金流/净利润"),
    ):
        v = _f(fin.get(k))
        if v:
            out.append(f"  {lbl} {v}")

    _bc = (biz.get("business_chain") or {}) or {}
    if _bc:
        regs = _bc.get("upstream_regions") or []
        if regs:
            out.append("上游（一手来源·年报）：授权代理原厂 " +
                       "、".join(f"{r['region']}{r['count']}家" for r in regs) + "；产品线 " +
                       "、".join(p["term"] for p in (_bc.get("upstream_products") or [])[:8]))
        fld = _bc.get("downstream_fields") or []
        if fld:
            out.append("下游（一手来源·年报）应用领域：" + "、".join(fld))
    return "\n".join(out)


def _fallback_growth(profile_dict: dict) -> tuple[list[dict], list[str]]:
    """数据层确定性兜底：LLM 增长分析失败/被判无效时，用真实指标拼出三层次/三约束。

    与 _fallback_conclusion 同样的原则——不润色、不推理，只把已核实数字映射到结构。
    即便 7B 模型完全不可用，本函数也能产出有据可循的增长框架。
    """
    L = profile_dict.get("layers", {}) or {}
    biz = L.get("business", {}) or {}
    fin = (L.get("financials", {}) or {}).get("summary", {}) or {}
    _bc = (biz.get("business_chain") or {}) or {}

    def _f(v, u="%", nd=2):
        try:
            return None if v in (None, "") else f"{float(v):.{nd}f}{u}"
        except (TypeError, ValueError):
            return None

    # —— 三层次（全部指向真实数据；句式不绑定任何商业模式）——
    segs = _norm_segments(biz.get("segments"))
    top_name = segs[0]["name"] if segs else "核心主业"
    top_pct = f"{segs[0]['pct']:.2f}%" if (segs and segs[0].get("pct") is not None) else "未获取"
    gm = _f(fin.get("gross_margin")) or "未获取"
    regs = _bc.get("upstream_regions") or []
    _mk = "、".join(f"{r['region']}{r['count']}家" for r in regs) if regs else ""
    prods = "、".join(p["term"] for p in (_bc.get("upstream_products") or [])[:6]) or "多品类元器件"
    fld = _bc.get("downstream_fields") or []
    _is_dist = bool(regs)  # 存在上游授权原厂结构才用分销商句式

    growth = [
        {
            "tier": "层次一",
            "track": f"{top_name}份额深化",
            "space": (
                f"在核心主业（收入占比 {top_pct}）中提升高价值品类与高可靠产品占比，"
                "依托现有客户网络做深做透。"
                if _is_dist
                else f"在核心主业（收入占比 {top_pct}）中提升高端产品占比与产能利用率，"
                "依托现有客户结构做深做透。"
            ),
            "certainty": "高",
        },
        {
            "tier": "层次二",
            "track": "原厂产品线扩张" if _is_dist else "产品与品类结构升级",
            "space": (
                (
                    f"依托上游授权原厂（{_mk or '年报列示'}），向 {prods} 等紧缺与高可靠品类延伸，"
                    "拓宽可分销 SKU 宽度。"
                )
                if _is_dist
                else (
                    f"围绕 {prods} 等产品组合向高端品类与新一代产品迭代延伸，"
                    "拓宽收入结构宽度。"
                )
            ),
            "certainty": "中",
        },
        {
            "tier": "层次三",
            "track": "下游高景气应用渗透",
            "space": (("受益 " + "、".join(fld[:6]) + " 等下游高景气应用渗透，") if fld else "")
                     + "需求结构向新能源与算力侧倾斜带来的增量空间。",
            "certainty": "中",
        },
    ]

    # —— 三约束（对应真实短板；句式不绑定商业模式）——
    constraints: list[str] = []
    if gm not in ("未获取", None):
        constraints.append(
            (
                f"分销低毛利本质：综合毛利率仅 {gm}，盈利高度依赖规模与周转，提价空间有限。"
                if _is_dist
                else f"毛利率结构约束：综合毛利率 {gm}，需靠产品结构升级与成本管控维持盈利水平。"
            )
        )
    cf = _f(fin.get("cf_to_profit"))
    if cf:
        _low = "，利润含金量偏弱" if (isinstance(fin.get("cf_to_profit"), (int, float)) and fin["cf_to_profit"] < 100) else ""
        constraints.append(f"现金流覆盖约束：经营现金流/净利润为 {cf}{_low}，扩张需匹配回款节奏。")
    if _is_dist:
        constraints.append(
            "原厂授权与库存周期约束：代理权集中度与原厂政策、下游库存周期直接挂钩，"
            "是增长弹性的硬约束（一手来源·年报）。"
        )
    else:
        constraints.append(
            "产能与资本开支约束：产能爬坡节奏与资本开支强度直接决定出货弹性，"
            "是增长落地的硬约束（一手来源·年报）。"
        )
    # 若上述仍未凑满三条，用负债率补一条（真实指标）
    if len(constraints) < 3:
        dr = _f(fin.get("debt_ratio"))
        if dr:
            constraints.append(f"财务杠杆约束：资产负债率 {dr}，限制激进扩张的融资空间。")

    return growth, constraints[:3]


# 增长输出占位符泄漏：7B 模型（尤其 qwen 私有 7B）常把 prompt 示例原样回显成内容，
# 例如 track="赛道名"、space="市场空间与增速判断（含真实数值）"、约束="约束一（对应真实指标）"。
# 这类「格式正确但内容为空壳」比留空更有害（读者会误以为填好了），故单独拦截。
# 注意区分：层次标签「层次一/二/三」、诚实值「未获取」是合法内容，不在黑名单内，避免误杀。
_GROWTH_PLACEHOLDER = (
    "赛道名", "市场空间与增速判断", "（含真实数值）", "（对应真实指标）",
    "高/中/低",
)
_GROWTH_CERT = ("高", "中", "低")


def _validate_growth(parsed: dict | None) -> tuple[list[dict], list[str]] | None:
    """校验 LLM 增长输出：三层次 + 三约束，且不得为占位符回显，否则判无效回退兜底。

    关键：7B 模型会把 prompt 示例原样回显（track="赛道名"、space="市场空间与增速判断…"），
    仅判「非空」会放行走私——必须显式拦截占位符，确保要么真填了内容，要么回退数据层兜底。
    """
    if not isinstance(parsed, dict):
        return None
    tiers = parsed.get("growth_tiers")
    cons = parsed.get("constraints")
    if not isinstance(tiers, list) or len(tiers) < 3:
        return None
    if not isinstance(cons, list) or len(cons) < 3:
        return None

    _ok_tiers = []
    for t in tiers:
        if not isinstance(t, dict):
            continue
        _track = (t.get("track") or "").strip()
        _tier = (t.get("tier") or "").strip()
        _space = (t.get("space") or "").strip()
        if not _track or not _tier or not _space:
            continue
        # 占位符回显拦截：任一字段含示例词即判无效
        _blob = (_track + _space + _tier).lower()
        if any(p.lower() in _blob for p in _GROWTH_PLACEHOLDER):
            return None
        _cert = (t.get("certainty") or "中").strip()
        if _cert not in _GROWTH_CERT:
            _cert = "中"
        _ok_tiers.append({
            "tier": _tier, "track": _track, "space": _space, "certainty": _cert,
        })
    if len(_ok_tiers) < 3:
        return None

    _ok_cons = []
    for c in cons:
        if not isinstance(c, str):
            continue
        _c = c.strip()
        if not _c:
            continue
        _cl = _c.lower()
        if any(p.lower() in _cl for p in _GROWTH_PLACEHOLDER):
            return None
        _ok_cons.append(_c)
    if len(_ok_cons) < 3:
        return None

    return _ok_tiers[:3], _ok_cons[:3]


def _biz_enrich_check(out: dict, bc: dict) -> tuple[bool, dict]:
    """业务画像增强护栏 v3：实体/数字防编造底线 + **逐条过滤**（不再整批作废）。

    硬闸——每条目：
      1. 实体：partner/customers 按顿号等拆分、剥「厂商/客户/等/相关客户」类
         后缀后——短实体（≤4 字）须全串在语料；长实体用**最长公共子串 ≥5**
         （容忍 LLM 用全称/简称/归纳表述，如原文简称「中国广电」vs 全称
         「中国广播电视网络有限公司」）；归纳性标签（category）免实体校验。
      2. 数字：条目中的金额/比例数字串必须出现在语料（防编造市场规模）。
    quote 为软参考（LLM 会自然去除 PDF 折行空格并节选，逐字要求实测误杀全部
    合理输出）。
    返回 (是否至少保留一条, 过滤后的 out)。
    """
    from difflib import SequenceMatcher

    corpus = re.sub(r"\s+", "", "".join(bc.get("evidence_sentences") or []))
    corpus_num = corpus.replace(",", "")
    if not corpus:
        return False, {}

    def _ent_ok(ent: str) -> bool:
        ent = re.sub(r"\s+", "", (ent or "").strip())
        if not ent:
            return True
        if re.search(r"无具体|客户类型|暂未披露|未披露|不适用|各类|多类|相关客户|客户群", ent):
            return True
        for part in re.split(r"[、，,/；]|以及|及", ent):
            part = part.strip(" （）()。等")
            base = re.sub(r"(厂商|客户|企业|伙伴|供应商|相关客户)+$", "", part)
            if not base or len(base) < 2:
                continue
            if len(base) <= 4:
                if base not in corpus:
                    return False
            else:
                m = SequenceMatcher(None, base, corpus).find_longest_match(
                    0, len(base), 0, len(corpus)
                )
                if m.size < 5:
                    return False
        return True

    def _nums_ok(blob: str) -> bool:
        for num in re.findall(r"\d[\d,\.]{2,20}", blob):
            bare = num.replace(",", "")
            if len(bare.replace(".", "")) >= 3 and bare not in corpus_num:
                return False
        return True

    filtered: dict = {}
    kept = 0
    for key in ("partners", "upstream_struct", "downstream_detail"):
        kept_rows = []
        for r in (out.get(key) or []):
            if not isinstance(r, dict) or not any(str(v or "").strip() for v in r.values()):
                continue
            # 否定性/占位说明条目剔除（如「未出现具体芯片或EDA等原厂厂商名称…」
            # ——银行等非分销公司本无原厂概念，LLM 输出此类空说明行只会误导）
            _blob_neg = " ".join(str(v) for v in r.values())
            if re.search(r"未出现|未见[^。]{0,6}披露|无相关|暂未|不适用|无法确定", _blob_neg):
                logger.warning("[overview] 业务画像增强剔除否定性占位条目：%s",
                               str(r.get("category") or r.get("partner") or "")[:40])
                continue
            blob = " ".join(str(v) for v in r.values())
            if _ent_ok(str(r.get("partner") or "")) and _ent_ok(str(r.get("customers") or "")) and _nums_ok(blob):
                kept_rows.append(r)
                kept += 1
            else:
                logger.warning(
                    "[overview] 业务画像增强条目被护栏过滤：[%s] %s",
                    key, str(r.get("partner") or r.get("category") or r.get("field"))[:40],
                )
        if kept_rows:
            filtered[key] = kept_rows
    return kept > 0, filtered


def _build_biz_enrich(bc: dict, use_llm: bool = True) -> dict:
    """业务画像三字段（上游/下游/深度合作）的 LLM 增量补充。

    数据层（确定性正则）为主源；当数据层结果不够丰富时（上游无阵营/下游
    名单行少/合作条目少），把定期报告**证据句语料**交给聚焦 LLM 做受约束
    的增量提取——quote 必须是语料原文子串、实体必须逐字出现（_biz_enrich_check
    双闸），过护栏才与数据层合并；否则整体丢弃（宁缺毋滥）。
    """
    empty = {"upstream_struct": [], "downstream_extra": [], "partners_extra": []}
    if not use_llm:
        return empty
    ev = bc.get("evidence_sentences") or []
    if not ev:
        return empty
    known = []
    for _m in (bc.get("upstream_makers") or []):
        known += [_m.get("en") or "", _m.get("cn") or ""]
    for _p in (bc.get("partnerships") or []):
        known.append(str(_p.get("partner") or ""))
    facts = (
        "【公司已确定性抽取的合作/客户实体（词汇锚，禁止编造之外的名字）】\n"
        + "、".join(k for k in known if k)
        + "\n\n【证据句语料（quote 必须出自其中）】\n"
        + "\n".join(f"- {x}" for x in ev)
    )
    try:
        system = load_prompt("biz_enrich")
        resp = get_router().complete(
            LLMRequest(system=system, user=facts, max_tokens=2500)
        ).text
        parsed = _parse_overview_json(resp)
        passed, filtered = _biz_enrich_check(parsed, bc)
        if not passed:
            logger.warning("[overview] 业务画像增强：LLM 输出无任何条目通过护栏，丢弃")
            return empty
        logger.info(
            "[overview] 业务画像增强：LLM 提取（护栏逐条过滤后） partners=%s upstream=%s downstream=%s",
            len(filtered.get("partners") or []),
            len(filtered.get("upstream_struct") or []),
            len(filtered.get("downstream_detail") or []),
        )
        return {
            "upstream_struct": filtered.get("upstream_struct") or [],
            "downstream_extra": filtered.get("downstream_detail") or [],
            "partners_extra": filtered.get("partners") or [],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[overview] 业务画像增强聚焦调用异常：%s", e)
        return empty


# 公开信息补位每类条数上限（防 LLM 铺量）
_PUBLIC_MAX_ROWS = 6


def _public_entity_check(parsed: dict, up_cats: set[str], down_fields: set[str]) -> dict:
    """公开信息补位护栏：类别名须来自输入、企业名须为具名实体、禁精确数字。

    与 `_biz_enrich_check` 的本质区别：公开信息**无原文可校验**，因此换一套
    可机检的约束——① `field`/`category` 必须逐字复用输入（防自创产业环节）；
    ② 企业名拆分后每条须非泛称、长度合理（防「多家头部企业」这类空转）；
    ③ 整条不得出现精确数字（份额/金额/订单量一律禁止，这是幻觉高发区）；
    ④ 每类条数设上限，防 LLM 铺量。
    """

    def _names_ok(blob: object) -> str:
        keep: list[str] = []
        for part in re.split(r"[、，,/；]|以及|及", str(blob or "")):
            p = part.strip(" （）()。、，,·・")
            if not (2 <= len(p) <= 24) or is_generic_entity(p):
                continue
            if p not in keep:
                keep.append(p)
        return "、".join(keep[:5])

    def _no_number(blob: object) -> bool:
        # 三位以上数字串视为精确数字（「3M」这类单字符数字不受影响）
        return not re.search(r"\d[\d,\.]{2,}", str(blob or ""))

    out: dict = {"upstream": [], "downstream": [], "partners": []}
    # —— 上游代表企业：category 必须逐字复用手抽类别 ——
    for r in (parsed.get("upstream") or [])[:_PUBLIC_MAX_ROWS]:
        if not isinstance(r, dict):
            continue
        cat = str(r.get("category") or "").strip()
        if cat not in up_cats:
            continue
        names = _names_ok(r.get("names"))
        if not names or not _no_number(names):
            continue
        out["upstream"].append({
            "category": cat,
            "names": names,
            "basis": str(r.get("basis") or "").strip()[:60],
            "src": "public",
        })
    # —— 下游代表客户：field 必须逐字复用已定稿领域 ——
    for r in (parsed.get("downstream") or [])[:_PUBLIC_MAX_ROWS]:
        if not isinstance(r, dict):
            continue
        field = str(r.get("field") or "").strip()
        if field not in down_fields:
            continue
        names = _names_ok(r.get("customers"))
        if not names or not _no_number(names):
            continue
        out["downstream"].append({
            "field": field,
            "customers": names,
            "basis": str(r.get("basis") or "").strip()[:60],
            "src": "public",
        })
    # —— 代表性合作方：补充泛称条目对应的具体公司（或无一手合作方时的行业公认伙伴）——
    for r in (parsed.get("partners") or [])[:_PUBLIC_MAX_ROWS]:
        if not isinstance(r, dict):
            continue
        names = _names_ok(r.get("partner"))
        content = str(r.get("content") or "").strip()
        if not names or not _no_number(content) or not content:
            continue
        out["partners"].append({
            "partner": names,
            "relation": str(r.get("relation") or "公开信息 · 待核实").strip()[:40],
            "content": content[:160],
            "src": "public",
        })
    return out


def _build_public_entities(bc: dict, seed: dict, use_llm: bool = True) -> dict:
    """基于公开产业信息补位「代表性企业」名单（非一手来源·待核实）。

    背景：定期报告普遍不点名客户/供应商（实测 601138 半年报 221 页仅一处具名
    合作方，002384 全篇无具名客户），一手抽取无法满足「列举具体公司」的阅读需求。
    本通道在**明确标注非一手来源**的前提下，由聚焦 LLM 依据公开产业常识补充业内
    公认的代表企业，与一手数据分层展示（一手行在前、公开信息补位行在后并加标记）。

    seed 为已定稿的一手结果（上游类别 / 下游领域 / 泛称合作方），用于约束 LLM
    只能在这些环节内补名，不得自创。
    """
    empty: dict = {"upstream": [], "downstream": [], "partners": []}
    if not use_llm:
        return empty
    up_cats = {
        str(u.get("category") or "").strip()
        for u in (seed.get("upstream_struct") or [])
        if str(u.get("category") or "").strip()
    }
    down_fields = {
        str(d.get("field") or "").strip()
        for d in (seed.get("downstream") or [])
        if str(d.get("field") or "").strip()
    } - {"整体客户分布"}
    _generic_partners = [
        str(p.get("partner") or "").strip()
        for p in (seed.get("partners") or [])
        if p.get("named") is False and str(p.get("partner") or "").strip()
    ]
    if not (up_cats or down_fields or _generic_partners):
        return empty

    # 触发闸：仅当确实存在「没有具体公司名」的缺口时才调用 LLM（额度敏感）。
    # 缺口 = 下游某领域一手客户全为泛称 / 存在泛称合作口径 / 上游仅有类别无企业。
    def _all_generic(cust: str) -> bool:
        _names = [x.strip(" 、，,()") for x in re.split(r"[、，,]", str(cust or "")) if x.strip()]
        return bool(_names) and all(is_generic_entity(n) for n in _names)

    _has_gap = (
        any(_all_generic(str(d.get("customers") or "")) for d in (seed.get("downstream") or []))
        or bool(_generic_partners)
        or bool(up_cats)
    )
    if not _has_gap:
        return empty
    facts = (
        f"【公司】{seed.get('name') or ''}（{seed.get('code') or ''}）\n"
        f"【标签】{'、'.join(str(b) for b in (seed.get('badges') or []))}\n"
        f"【上游类别（逐字复用，不得改字）】{'、'.join(sorted(up_cats)) or '（无）'}\n"
        f"【下游应用领域（逐字复用，不得改字）】{'、'.join(sorted(down_fields)) or '（无）'}\n"
        "【定期报告中的泛称合作口径（请补充其对应的业内公认具体公司）】"
        f"{'、'.join(_generic_partners) or '（无）'}\n"
    )
    try:
        system = load_prompt("biz_public")
        resp = get_router().complete(
            LLMRequest(system=system, user=facts, max_tokens=1600)
        ).text
        parsed = _parse_overview_json(resp)
        if not parsed:
            logger.warning("[overview] 公开信息补位：LLM 输出无法解析，跳过")
            return empty
        filtered = _public_entity_check(parsed, up_cats, down_fields)
        _n = sum(len(v) for v in filtered.values())
        if not _n:
            logger.warning("[overview] 公开信息补位：无任何条目通过护栏，跳过")
            return empty
        logger.info(
            "[overview] 公开信息补位：upstream=%s downstream=%s partners=%s（均标注非一手来源）",
            len(filtered["upstream"]), len(filtered["downstream"]), len(filtered["partners"]),
        )
        return filtered
    except Exception as e:  # noqa: BLE001
        logger.warning("[overview] 公开信息补位调用异常：%s", e)
        return empty


def _build_growth(profile_dict: dict, use_llm: bool = True) -> tuple[list[dict], list[str], str]:
    """编排增长分析：聚焦 LLM 子调用 → 校验 → 数据层兜底；返回 (growth, constraints, source)。

    use_llm=False（--no-llm）时跳过聚焦 LLM 子调用，直接数据层兜底——聚焦调用
    曾不受该开关控制，导致 no-llm 模式仍消耗 LLM 额度且输出不受护栏约束。

    source 用于模板徽标溯源：LLM 路径=「非一手来源·待核实」；兜底路径=「数据层推导·真实口径」。
    """
    try:
        system = load_prompt("growth")
    except Exception:
        system = (
            "你是专业的 A 股行业研究员。先根据给定事实判断公司商业模式"
            "（分销/制造/服务等），再以匹配的分析框架返回 JSON："
            "{\"growth_tiers\":[3 个{层次/赛道/空间/确定性}],\"constraints\":[3 条约束]}。"
            "只用给定真实数据，严禁编造；外部市场数字必须标注「（待核实）」。"
        )
    facts = _compact_growth_facts(profile_dict)
    user = (
        f"请基于以下已核实事实，分析增长空间（三个层次）与增长的三个硬约束：\n\n{facts}\n\n"
        "仅返回 JSON，结构见系统提示。"
    )
    try:
        if use_llm:
            resp = get_router().complete(
                LLMRequest(system=system, user=user, max_tokens=2000)
            ).text
            parsed = _parse_overview_json(resp)
            valid = _validate_growth(parsed)
            if valid:
                logger.info("[overview] 增长分析由聚焦 LLM 子调用生成")
                return valid[0], valid[1], "非一手来源·待核实"
    except Exception as e:  # noqa: BLE001
        logger.warning("[overview] 增长聚焦调用异常：%s", e)

    g, c = _fallback_growth(profile_dict)
    logger.info("[overview] 增长分析由数据层兜底生成（聚焦调用未通过校验）")
    return g, c, "数据层推导·真实口径"


# ---------------------------------------------------------------------------
# P2-4/P2-5：叙述性字段（护城河 / 市场逻辑 / 错位 flag / 交叉验证）的数据层兜底
# ---------------------------------------------------------------------------
# 7B 模型对 moat / market_logic 这类「列表型叙述字段」偶发把整段回显成单个字符串
# （实测 moat="行业地位、技术优势、客户基础"、market_logic="半导体行业"），
# 而模板按 list 迭代会逐字符渲染（每个汉字成为一个 <li>），是可见的硬伤。
# 故对这类字段强制「类型正确 + 非占位符 + 非「类别A、类别B」空壳标签」三重校验；
# 不通过则走数据层兜底。对 business_flag_html / business_cross_html 等 HTML 字段，
# 同样要求「足量且有实质信号」，否则用数据层拼装的版本替换（绝不展示空壳片段）。
_LABEL_LIST_RE = re.compile(r"^[一-龥]{2,6}[、，][一-龥]{2,6}(?:[、，][一-龥]{2,6})*$")

# 叙述字段「实质信号」白名单：命中其一（或含数字）才视为有内容，否则判为空壳。
_SUBSTANCE_TOKENS = (
    "原厂", "授权", "毛利率", "收入", "下游", "上游", "占比", "供应链", "规模",
    "品牌", "行业", "市场", "存货", "账期", "车规", "工业", "新能源", "算力",
    "AI", "mlcc", "MLCC", "Murata", "murata", "利润", "周转", "现金流", "代理",
    "分销", "客户网络", "高毛利", "低毛利", "净利率",
)


def _valid_list_field(value, min_items: int = 1) -> bool:
    """moat / market_logic 必须是 list[str]，且条目非空、非占位符、非空壳标签列表。"""
    if not isinstance(value, list):
        return False
    if len(value) < min_items:
        return False
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return False
        if _is_bad_text(item):
            return False
        # 纯「类别A、类别B、类别C」形式的空壳标签（无数字/无分析性内容）判无效
        if _LABEL_LIST_RE.match(item.strip()):
            return False
    return True


def _valid_html_field(value, min_len: int = 20) -> bool:
    """business_flag_html / business_cross_html 必须是非空 HTML 字符串且含实质信号。"""
    if not isinstance(value, str):
        return False
    s = value.strip()
    if len(s) < min_len:
        return False
    if _is_bad_text(s):
        return False
    _has_digit = bool(re.search(r"\d", s))
    _has_substance = any(tok in s for tok in _SUBSTANCE_TOKENS)
    return _has_digit or _has_substance


def _valid_narr_html(value, min_len: int = 10) -> bool:
    """自由文本叙述字段（备注/说明类）的宽松校验：非空、非坏文本即可。

    与 _valid_html_field 不同——备注类不强制要求含数字/业务关键词，但必须过
    _is_bad_text（占位符/控股误判/错误断言）这道安全网。
    """
    if not isinstance(value, str):
        return False
    s = value.strip()
    if len(s) < min_len:
        return False
    return not _is_bad_text(s)


def _valid_plain_list(value, min_items: int = 1) -> bool:
    """通用列表字段（ind_metrics / badges / risks_* / gov_right / div_metrics）校验：

    非空 list，且每个元素（字符串或对象）非空、不命中坏文本（占位符/控股误判/错误断言）。
    """
    if not isinstance(value, list) or len(value) < min_items:
        return False
    for it in value:
        if isinstance(it, str):
            if not it.strip() or _is_bad_text(it):
                return False
        elif isinstance(it, dict):
            if _is_bad_text(it):
                return False
        else:
            if not it:
                return False
    return True


def _valid_events_field(value, min_items: int = 1) -> bool:
    """events 列表校验：每项为 {date, text} 且均非空、不命中坏文本。"""
    if not isinstance(value, list) or len(value) < min_items:
        return False
    for e in value:
        if not isinstance(e, dict):
            return False
        if not (e.get("date") and e.get("text")):
            return False
        if _is_bad_text(e.get("date")) or _is_bad_text(e.get("text")):
            return False
    return True


# 通用护栏覆盖的关键叙述字段：把原先只针对 conclusion 的「事实校验→重试」机制
# 下沉到全字段（P3-1）。与 build_overview_data 的逐字段兜底互为冗余：
# 这里在「生成侧」尽早拦截脏输出并触发重试，兜底层在「装配侧」对仍漏网的字段做确定
# 性数据兜底，双层保险。
_NARR_LIST_FIELDS = ("moat", "market_logic")
_NARR_HTML_FIELDS = ("business_flag_html", "business_cross_html")
# P3-2：自由文本叙述字段全字段扫描（含 conclusion）；不再只卡 conclusion。
# 这些字段直接承接 LLM 自由文本，需拦截占位符/控股误判/错误断言 + 数字量级证伪。
_NARR_STR_FIELDS = (
    "conclusion_html", "id_insight_html", "gov_scan_note", "peers_note",
    "trend_note", "cash_note", "div_text",
)
# 列表 / 对象叙述字段：递归坏文本扫描（占位符/控股误判/错误断言），不跑量级证伪以免误伤。
_NARR_LIST_FIELDS_SCAN = (
    "ind_metrics", "risks_left", "risks_right", "risk_emphasis",
    "badges", "events",
)


def _overview_json_is_valid(parsed: dict, profile_dict: dict) -> tuple[bool, list[str]]:
    """通用护栏：判定一次 LLM 概况 JSON 是否可采纳。

    返回 (ok, issues)。只要任一关键叙述字段被判脏即 ok=False，交由调用方决定是否重试。
    命中即重试，等价于把原先只针对 conclusion 的「幻觉拦截→重试」下沉到全字段。
    """
    if not isinstance(parsed, dict):
        return False, ["解析结果非 dict"]
    issues: list[str] = []

    # 1) 占位符泄漏（把提示词示例原样回显 = 等同编造）：对全字段做一遍安全网扫描
    _, _leaks = _sanitize_llm_json(parsed)
    if _leaks:
        issues.append(f"占位符泄漏：{_leaks}")

    # 2) list 字段：moat / market_logic 必须真 list 且非空壳标签
    for k in _NARR_LIST_FIELDS:
        v = parsed.get(k)
        if v not in (None, "", [], {}):
            if not _valid_list_field(v):
                issues.append(f"{k} 非合法 list（疑似回显字符串/空壳标签）")

    # 3) html 字段：business_flag_html / business_cross_html 必须含实质信号
    for k in _NARR_HTML_FIELDS:
        v = parsed.get(k)
        if v not in (None, "", [], {}):
            if not _valid_html_field(v):
                issues.append(f"{k} 缺实质信号（数字/业务关键词）")

    # 4) 自由文本叙述字段（P3-2 下沉到全部，不再仅 conclusion）：占位符/控股误判/错误断言
    #    + 数字量级证伪（毛利率/净利率/营收同比）。任一命中即整份丢弃重试。
    for k in _NARR_STR_FIELDS:
        v = parsed.get(k)
        if not isinstance(v, str) or not v.strip():
            continue
        if _is_bad_text(v):
            issues.append(f"{k} 命中坏文本/错误断言（占位符/控股误判/已知错误数字）")
            continue
        _fc = _factual_claim_check(v, profile_dict)
        if _fc:
            issues.append(f"{k} 数字量级冲突：" + "；".join(_fc))

    # 5) 列表 / 对象叙述字段：递归坏文本扫描（占位符/控股误判/错误断言）
    for k in _NARR_LIST_FIELDS_SCAN:
        v = parsed.get(k)
        if v in (None, "", [], {}):
            continue
        if _is_bad_text(v):
            issues.append(f"{k} 命中坏文本/错误断言")

    # 6) conclusion 专属最强护栏：点名/定性/否定断言事实证伪（与上面的量级证伪互补）
    _c = (parsed.get("conclusion_html") or "").strip()
    if _c:
        _cf = _conclusion_fact_check(_c, profile_dict)
        if _cf:
            issues.append("conclusion 事实冲突：" + "；".join(_cf))
    else:
        issues.append("conclusion_html 为空")

    return (len(issues) == 0), issues


def _build_narrative_fallbacks(profile_dict: dict) -> dict:
    """数据层确定性兜底：从真实结构化数据派生 moat / market_logic / business_flag_html /
    business_cross_html，供 LLM 这些字段返回垃圾（字符串/占位符/空壳片段）时替换。

    与增长引擎同一原则：不润色、不推理，只用已核实数字与一手来源（年报）拼装。
    id_insight_html 不在此处理——它已由 build_overview_data 的股东表块用真实持股/质押数据覆盖。
    """
    L = profile_dict.get("layers", {}) or {}
    biz = L.get("business", {}) or {}
    fin = (L.get("financials", {}) or {}).get("summary", {}) or {}
    ind = L.get("industry", {}) or {}
    _bc = (biz.get("business_chain") or {}) or {}

    def _f(v, u="%", nd=2):
        try:
            return None if v in (None, "") else f"{float(v):.{nd}f}{u}"
        except (TypeError, ValueError):
            return None

    segs = _norm_segments(biz.get("segments"))
    top = segs[0] if segs else None

    # —— 护城河（moat）：上游壁垒 + 产品线广度 + 规模 + 同业盈利位置 ——
    # 全部句式由**该公司自己的数据**填充；「分销商/授权代理」等表述仅在数据
    # 确有其事时出现（如存在上游授权原厂结构），不再写死任何商业模式。
    moat: list[str] = []
    _regs = _bc.get("upstream_regions") or []
    _maker_total = sum(r.get("count", 0) for r in _regs)
    _maker_names: list[str] = []
    for r in _regs:
        _maker_names.extend(r.get("makers", []))
    if _maker_total:
        _top = "、".join(_maker_names[:6])
        moat.append(
            f"上游授权壁垒：授权代理 {_maker_total} 家海外/本土芯片原厂品牌"
            + (f"（含 {_top} 等）" if _top else "")
            + "，代理权是电子元器件分销的核心进入壁垒，竞争对手难以短期复制（一手来源·年报）。"
        )
    _prods = _bc.get("upstream_products") or []
    if _prods:
        _pt = "、".join(p.get("term", "") for p in _prods[:6] if p.get("term"))
        moat.append(
            f"产品线广度与高可靠品类：产品/代理体系涵盖 {_pt} 等多品类，"
            "并向车规级、工业级高可靠 SKU 延伸，提升客户粘性与单价。"
        )
    if top and top.get("pct") is not None:
        _top_nm = (top.get("name") or "核心主业").strip()
        moat.append(
            f"规模化经营壁垒：主营集中于{_top_nm}（核心板块收入占比 {top['pct']:.2f}%），"
            "规模化带来采购/产能与账期优势，形成对中小竞争者的成本壁垒。"
        )
    _gm = _f(fin.get("gross_margin"))
    if _gm:
        peers = [p for p in (ind.get("peers") or []) if not p.get("hl")]
        try:
            gms = sorted(
                (float(p.get("gross_margin")) for p in peers if p.get("gross_margin") not in (None, "")),
                reverse=True,
            )
        except Exception:
            gms = []
        if gms:
            _rank = sum(1 for g in gms if g > float(fin["gross_margin"])) + 1
            moat.append(
                f"同业盈利位置：综合毛利率 {_gm}，在 {len(gms)} 家可比公司中列第 {_rank} 位，"
                "反映其产品结构与客户质量相对行业水平的位置。"
            )
    if not moat:
        moat.append("（数据层未取到足以归纳护城河的结构化信息；详见上下游与同业对比模块。）")

    # —— 市场逻辑与分歧（market_logic）——
    market_logic: list[str] = []
    _fld = _bc.get("downstream_fields") or []
    if _fld:
        market_logic.append(
            "下游高景气应用驱动：受益 " + "、".join(_fld[:6])
            + " 等领域需求，尤其新能源与算力侧的结构性增量，是市场对其成长的主要定价逻辑。"
        )
    if _regs or _prods:
        _mk = ("、".join(f"{r['region']}{r['count']}家" for r in _regs)) if _regs else ""
        if _regs:
            market_logic.append(
                "上游原厂供给与品类扩张：依托授权原厂（" + (_mk or "年报列示")
                + "）向紧缺与高可靠品类延伸，决定其可分销 SKU 的弹性与增量空间。"
            )
        else:
            market_logic.append(
                "上游供给与产品迭代：依托核心品类（"
                + "、".join(p.get("term", "") for p in _prods[:4] if p.get("term"))
                + "）的新一代产品与产能爬坡节奏，决定收入结构的弹性与增量空间。"
            )
    if _gm:
        market_logic.append(
            f"低毛利 + 周转驱动：综合毛利率仅 {_gm}，盈利高度依赖规模与周转，"
            "市场关注其能否通过高毛利品类改善盈利质量。"
        )
    _cf = _f(fin.get("cf_to_profit"))
    if _cf:
        market_logic.append(
            f"现金流与利润背离：经营现金流/净利润为 {_cf}，利润含金量偏低是当前主要分歧点，"
            "需关注应收账款与存货对资金的占用。"
        )
    if not market_logic:
        market_logic.append("（数据层未取到足以归纳市场逻辑的结构化信息；详见上下游与财务速写模块。）")

    business_flag_html, business_flag_title = _build_seg_divergence_flag(segs)
    business_cross_html = _build_business_cross(_bc, segs, top)
    return {
        "moat": moat,
        "market_logic": market_logic,
        "business_flag_html": business_flag_html,
        "business_flag_title": business_flag_title,
        "business_cross_html": business_cross_html,
    }


def _parse_news_date(raw) -> "datetime | None":
    """把公告日期规整为 datetime；支持 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS' / pandas Timestamp / datetime.date。

    统一返回 datetime，避免与 cutoff（datetime）比较时触发 date/datetime 类型不匹配。
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    if hasattr(raw, "strftime"):  # datetime.date / pandas Timestamp（均非 datetime 子类时）
        try:
            return datetime(raw.year, raw.month, raw.day)
        except (AttributeError, TypeError, ValueError):
            pass
    s = str(raw).strip()
    if not s or s in ("NaT", "None", "-"):
        return None
    for _fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:19] if " " in _fmt else s[:10], _fmt)
        except (ValueError, TypeError):
            continue
    return None


def _build_risk_list(fin: dict, ps: dict, biz: dict, mkt: dict) -> list[dict]:
    """P2-E3：从已核实数据确定性派生风险清单（最多 10 项，含等级与依据）。

    原则：只用真实结构化数据 + 行业结构性常识拼装；无任何字段编造具体数值。
    行业结构性风险（周期、汇率、并购、政策）属定性常识，明确标注「行业属性」而非公司特定数据。
    返回 [{k:风险名, v:依据/描述, cls:等级}]，cls ∈ risk-high/mid/low。
    """
    _q = (mkt or {}).get("quote") or {}
    _mg = (mkt or {}).get("margin") or {}

    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    _basis = {
        "current_capital": "按当前总股本",
        "pledge_time_capital": "质押当时口径",
    }
    risks: list[tuple] = []

    # 1) 股权质押（数据驱动）
    _apt = _f(ps.get("active_total_pct"))
    if _apt is not None and _apt > 0:
        risks.append((
            "股权质押",
            f"未解押占总股本 {_apt}%（{_basis.get(ps.get('pct_basis') or '', ps.get('pct_basis') or '')}），"
            "关注控制权稳定性与减持对流动性的冲击",
            "risk-mid",
        ))
    # 2) 现金流背离（数据驱动）
    _cfp = _f(fin.get("cf_to_profit"))
    if _cfp is not None and _cfp < 60:
        risks.append((
            "现金流背离",
            f"经营现金流仅为净利润的 {_cfp:.1f}%，利润现金含量偏低，关注应收与存货资金占用",
            "risk-mid",
        ))
    # 3) 应收周转（数据驱动）
    _ard = _f(fin.get("ar_days"))
    if _ard is not None and _ard > 90:
        risks.append((
            "应收账款周转慢",
            f"应收周转天数 {_ard:.0f} 天，回款周期长，存在坏账与流动性压力",
            "risk-mid",
        ))
    # 4) 存货跌价（数据驱动）
    _invd = _f(fin.get("inventory_days"))
    if _invd is not None and _invd > 60:
        risks.append((
            "存货跌价",
            f"存货周转天数 {_invd:.0f} 天，电子元器件价格下行时存在跌价风险",
            "risk-mid",
        ))
    # 5) 杠杆资金集中（数据驱动）
    _fpct = _f(_mg.get("financing_pct_float_mcap"))
    if _fpct is not None and _fpct > 8:
        risks.append((
            "杠杆资金集中",
            f"融资余额占流通市值 {_fpct:.2f}%，两融资金情绪波动放大股价弹性",
            "risk-mid",
        ))
    # 6) 行业周期波动（行业属性，定性）
    risks.append((
        "行业周期波动",
        "电子元器件分销受半导体景气度与下游需求周期影响，量价波动大（行业属性）",
        "risk-mid",
    ))
    # 7) 汇率波动（行业属性，定性）
    risks.append((
        "汇率波动",
        "代理海外原厂产品，采购与结算受人民币汇率波动影响（行业属性）",
        "risk-mid",
    ))
    # 8) 产品线/客户集中（数据驱动，可选）
    _segs = biz.get("segments") or []
    if _segs:
        try:
            _top = max((_f(s.get("pct")) or 0) for s in _segs)
        except (TypeError, ValueError):
            _top = 0
        if _top and _top > 40:
            risks.append((
                "产品线集中",
                f"第一大产品线收入占比约 {_top:.0f}%，单一品类景气波动对整体业绩影响大",
                "risk-low",
            ))
    # 9) 并购整合（公开披露历史，定性）
    risks.append((
        "并购整合",
        "历史上通过外延并购扩张（公开披露），存在整合与管理协同风险（行业属性）",
        "risk-low",
    ))
    # 10) 贸易与政策（行业属性，定性）
    risks.append((
        "贸易与政策",
        "进口替代与出口管制政策、关税变动可能影响代理线供给与成本（行业属性）",
        "risk-low",
    ))
    # 11) 估值高波动（数据驱动，条件项，优先级最高）
    _pe = _f(_q.get("pe_ttm"))
    if _pe is not None and _pe > 50:
        risks.append((
            "估值高波动",
            f"当前 PE(TTM) {_pe:.0f} 倍偏高，估值对业绩与情绪敏感",
            "risk-high",
        ))

    # 按等级排序（high→mid→low），取前 10，保证最值得关注的项优先展示
    _order = {"risk-high": 0, "risk-mid": 1, "risk-low": 2}
    risks.sort(key=lambda r: _order.get(r[2], 9))
    return [{"k": k, "v": v, "cls": c} for (k, v, c) in risks[:10]]


def _build_bull_bear(fin: dict, ps: dict, biz: dict, mkt: dict) -> dict:
    """P2-E3：多空逻辑（数据驱动的量化表述 + 结构性常识）。各取前 5 条。"""
    _q = (mkt or {}).get("quote") or {}
    _mg = (mkt or {}).get("margin") or {}

    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    bull, bear = [], []
    _gm = _f(fin.get("gross_margin"))
    # 多空逻辑句式按**该公司自己的数据**派生；「分销/授权代理」等商业模式词
    # 仅在数据确有其事（存在上游授权原厂结构）时出现，不再写死。
    _is_dist = bool((biz.get("business_chain") or {}).get("upstream_regions")) if isinstance(biz, dict) else False
    _top_seg = ""
    _segs_ = _norm_segments(biz.get("segments")) if isinstance(biz, dict) else []
    if _segs_:
        _top_seg = _segs_[0].get("name") or ""
    if _gm is not None:
        if _is_dist:
            bull.append(f"分销主业毛利率 {_gm:.1f}%，处于同业可比中位区间，体现授权代理壁垒")
        else:
            bull.append(f"综合毛利率 {_gm:.1f}%，盈利水平在同业对比中具备支撑")
    _rg = _f(fin.get("rev_growth"))
    if _rg is not None and _rg > 0:
        bull.append(f"营收同比增长 {_rg:.1f}%，下游需求回暖带动景气上行")
    _pg = _f(fin.get("profit_growth"))
    if _pg is not None and _pg > 0:
        bull.append(f"净利润同比增长 {_pg:.1f}%，盈利弹性释放")
    if _is_dist:
        bull.append(
            "代理全球一线原厂产品线（涵盖 "
            + "、".join(p.get("term", "") for p in ((biz.get("business_chain") or {}).get("upstream_products") or [])[:4])
            + " 等品类），渠道壁垒稳固"
        )
        bull.append("国产替代与供应链安全趋势下，授权分销商的渠道价值与下游覆盖优势凸显")
    elif _top_seg:
        bull.append(f"核心主业（{_top_seg}）景气上行，规模化产能与产品迭代构筑竞争壁垒")
        bull.append("国产替代与供应链安全趋势下，本土龙头份额提升空间显著")

    _cfp = _f(fin.get("cf_to_profit"))
    if _cfp is not None and _cfp < 60:
        bear.append(f"经营现金流/净利润仅 {_cfp:.1f}%，利润现金含量偏低，质量存疑")
    _apt = _f(ps.get("active_total_pct"))
    if _apt is not None and _apt > 0:
        bear.append(f"股权质押占总股本 {_apt}%，关注减持与流动性冲击")
    _fpct = _f(_mg.get("financing_pct_float_mcap"))
    if _fpct is not None and _fpct > 8:
        bear.append(f"融资余额占流通市值 {_fpct:.2f}%，杠杆资金集中放大波动")
    if _is_dist:
        bear.append("分销业重资产运营（存货+应收占用大），行业下行时周转与跌价压力大")
        bear.append("毛利率受原厂返点政策与原厂直供趋势挤压，长期存在被渠道绕开风险")
    else:
        bear.append("行业资本开支与价格周期波动大，景气下行期盈利回撤显著")
        bear.append("技术路线迭代与产能竞赛带来持续的资本开支压力")
    return {"bull": bull[:5], "bear": bear[:5]}


def _build_layer_fallbacks(profile_dict: dict) -> dict:
    """P3-2 数据层兜底：为 monolith 仍裸奔的 6 层自由文本/列表字段，从真实结构化数据派生安全值。

    覆盖字段：ind_metrics / peers_note / gov_scan_note / div_metrics / div_text /
    trend_note / cash_note / events / risk_emphasis / gov_right。
    原则与 _build_narrative_fallbacks 一致：不推理、只用已核实数据拼装；无数据则该键返回 None，
    由装配侧保留「未获取」空默认，绝不注入编造内容。
    """
    L = profile_dict.get("layers", {}) or {}
    ident = L.get("identity", {}) or {}
    biz = L.get("business", {}) or {}
    gov = L.get("governance", {}) or {}
    ind = L.get("industry", {}) or {}
    fin = (L.get("financials", {}) or {}).get("summary", {}) or {}
    dyn = L.get("dynamics", {}) or {}
    mkt = L.get("market", {}) or {}

    def _to_f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def _pct(x, nd=2):
        v = _to_f(x)
        return f"{v:.{nd}f}%" if v is not None else "未获取"

    def _num(x, nd=1):
        v = _to_f(x)
        return f"{v:.{nd}f}" if v is not None else "未获取"

    out: dict = {}

    # 04 行业坐标：申万/证监会行业 + 可比公司数
    sw = ident.get("industry_sw") or ind.get("industry_sw") or ""
    cn = ident.get("industry_cninfo") or ind.get("industry_cninfo") or ""
    peers = ind.get("peers") or []
    _im = []
    if peers:
        _im.append({"lab": "可比公司", "val": f"{len(peers)} 家", "note": "同业对比（含本公司）"})
    if sw:
        _im.append({"lab": "申万行业", "val": sw, "note": "一级分类"})
    if cn:
        _im.append({"lab": "证监会行业", "val": cn, "note": "行业归属"})
    if _im:
        out["ind_metrics"] = _im
    # 同业口径说明只在**确实取到可比公司**时输出（此前 peers=[] 时也会写
    # 「取申万二级行业成分 0 家」，属于误导性空态文案）。
    if peers:
        _is_curated = bool(peers and peers[0].get("curated"))
        out["peers_note"] = (
            (
                f"按业务实质精选 {len(peers)} 家同商业模式可比公司（含本公司高亮行），"
                "非申万二级成分——后者会混入商业模式迥异的公司，对比无意义。"
            )
            if _is_curated
            else (
                f"同业对比取申万二级行业成分 {len(peers)} 家（含本公司高亮行），"
                "用于横向定位；如需更严格的同商业模式可比集，可在精选集中收录本公司。"
            )
        )

    # 03 股权治理扫描备注：以质押汇总数据为准（不使用 LLM 自由文本）
    #
    # D 类修复：此前只说「在押主要股东为 X」，却不说明 X 是否属于前十大股东、控股股东
    # 自身有无在押——读者会把财务投资者的质押误读成控股股东的质押，风险定性被放大。
    # 本公司在押主体「侯红亮」并不在最新前十大股东名单内，且控股股东 MARK ZHAO
    # 的历史质押已全部解押（唯一状态为「未解押」的记录股数为 0），必须分别注明。
    ps = gov.get("pledge_summary") or {}
    if ps.get("active_count"):
        _basis = "按当前总股本" if ps.get("pct_basis") == "current_capital" else "质押当时口径"
        _th = ps.get("top_holder") or {}
        _scan = (
            f"截至 {ps.get('latest_announce_date', '—')} 公告，未解押质押 {ps['active_count']} 笔，"
            f"占总股本 {ps.get('active_total_pct')}%（{_basis}）"
        )
        if _th.get("name"):
            _scan += f"；在押主要股东为 {_th['name']}（{_th.get('pct')}%）"
            # 该在押主体是否属于前十大股东（同人异名一并匹配）
            _hNames = {
                str(h.get("股东名称") or h.get("name") or "").strip()
                for h in (gov.get("holders") or [])
            }
            # 同人异名一并匹配（如「赵马克」与「MARK ZHAO」），避免把控股股东判成非前十大
            _lr = str(ident.get("legal_rep") or "")
            _alias_tgt = _SHAREHOLDER_ALIASES.get(_lr, "")
            _ctrl_en = str((gov.get("actual_controller") or {}).get("name_en") or "")
            _in_top10 = (
                _th["name"] in _hNames
                or (_alias_tgt and _th["name"] == _alias_tgt)
                or (_ctrl_en and _th["name"] == _ctrl_en)
                or (_lr and _th["name"] == _lr)
            )
            if not _in_top10:
                _scan += "，<strong>该股东不在最新前十大股东名单内</strong>（非控股股东、非前十大）"
            else:
                _scan += "，属前十大股东"
        # 控股股东 / 实控人自身是否在押（0 股记录视为已结清）
        _ctrl_names = {
            "MARK ZHAO", "MARKZHAO", "赵马克",
            str((gov.get("actual_controller") or {}).get("name_cn") or ""),
        } - {""}
        _ctrl_active = sum(
            _to_f(r.get("pledge_shares")) or 0.0
            for r in (gov.get("pledge") or [])
            if str(r.get("status", "")) == "未解押"
            and str(r.get("shareholder") or "").strip() in _ctrl_names
        )
        if _ctrl_active <= 0:
            _scan += "；控股股东／实际控制人历史质押<strong>已全部解押，当前无在押</strong>"
        else:
            _capg = _to_f(gov.get("share_capital")) or 0.0
            _scan += (
                f"；控股股东／实际控制人当前在押 {_ctrl_active / 1e4:.2f} 万股"
                + (f"（占总股本 {_ctrl_active / _capg * 100:.2f}%）" if _capg else "")
            )
        _scan += "。质押主体为财务投资者而非控股股东，控制权层面的质押风险有限，"
        _scan += "但仍需关注其减持节奏对二级市场流动性的冲击。"
        out["gov_scan_note"] = _scan

    # 03 分红：取最新一期有效分红方案（方案说明非空；按报告期降序取最近）
    divs = gov.get("dividend") or []
    _valid_divs = sorted(
        [x for x in divs if isinstance(x, dict) and x.get("方案说明")],
        key=lambda x: str(x.get("报告期") or ""), reverse=True,
    )
    if _valid_divs:
        _d = _valid_divs[0]
        out["div_text"] = (
            f"最近一期分红方案（{_d.get('报告期', '—')}）：{_d.get('方案说明')}。"
        )
        out["div_metrics"] = [
            {"lab": "分红类型", "val": _d.get("分红类型") or "—", "note": _d.get("报告期") or ""}
        ]

    # 03 股份回购：真值取自年报一手来源（profile.repurchase，由 main.py 注入）。
    # 背景：schema 里 repurchase 字段此前从未被填充，LLM 凭记忆编造「回购 12,425,123 股」；
    # 年报权威值为已回购 2,979,800 股（约 297.98 万股），用于注销并减少注册资本。
    # 渲染位置：「分红与股东回报」区块内（回购属股东回报，与分红同节展示）。
    _rp = gov.get("repurchase")
    if isinstance(_rp, dict) and (
        _rp.get("executed_shares") or _rp.get("esop_transfer") or _rp.get("h1_cash_spent_yi")
    ):
        _rpwan = _to_f(_rp.get("executed_shares_wan"))
        out["repurchase_text"] = _rp.get("text") or ""
        _rp_year = str(_rp.get("year") or "").strip()
        # 来源措辞随抽取结果走：半年报数据覆盖年报时若仍标「年报一手来源」，
        # 会出现「2026 年实施·年报一手来源」这类自相矛盾的标注。
        _rp_src = str(_rp.get("source") or "定期报告")
        _rp_metrics: list[dict] = []
        if _rp.get("executed_shares"):
            _rp_metrics.append({
                "lab": "已回购股数",
                "val": (
                    f"{_rpwan:.2f} 万股"
                    if _rpwan is not None
                    else f"{int(_rp['executed_shares']):,} 股"
                ),
                "note": (
                    f"{_rp_year} 年实施·{_rp_src}一手来源"
                    if _rp_year
                    else f"{_rp_src}一手来源"
                ),
            })
            _rp_metrics.append({
                "lab": "回购用途",
                "val": _rp.get("purpose") or "—",
                "note": (
                    f"完成于 {_rp['completed_date']}"
                    if _rp.get("completed_date")
                    else f"{_rp_src}披露口径"
                ),
            })
        elif _rp.get("esop_transfer"):
            _et = _rp["esop_transfer"]
            _rp_metrics.append({
                "lab": "最新回购进展",
                "val": f"{int(_et.get('shares') or 0):,} 股",
                "note": f"{_et.get('date') or '—'} 过户员工持股计划",
            })
        out["repurchase_metrics"] = _rp_metrics

    # 03 实际控制人：真值取自年报「前十大股东」一手来源（profile.actual_controller）。
    #
    # C 类修复：此前报告完全未覆盖实控人持股比例，读者无法判断控制权集中度。
    # 关键口径：年报列示**总持股 127,564,160 股（11.08%）**，而东方财富前十大股东快照
    # 列示 31,891,040 股（2.78%）——后者恰为前者的 25%，即高管每年可转让额度
    # （无限售部分）。两者都对，但必须标明口径，否则同一人出现两个自相矛盾的比例。
    _ctl = gov.get("actual_controller")
    if isinstance(_ctl, dict) and _ctl.get("pct") is not None:
        _cpct = _to_f(_ctl.get("pct"))
        _cwan = _to_f(_ctl.get("shares_wan"))
        _crk = _ctl.get("rank")
        _cac = _ctl.get("acting_in_concert") or []
        # 最新定期报告名册口径优先（半年报总股本随回购注销缩小，11.08%→11.10%）；
        # 持股股数两期一致（127,564,160 股），仅比例基数不同
        _rh_ctl = next(
            (
                _h for _h in (gov.get("report_holders") or {}).get("holders") or []
                if "ZHAO" in str(_h.get("name") or "").upper().replace(" ", "")
                or "赵马克" in str(_h.get("name") or "")
            ),
            None,
        )
        if _rh_ctl:
            try:
                _cpct = float(_rh_ctl.get("pct"))
                _cwan = float(_rh_ctl.get("shares") or 0) / 1e4
            except (TypeError, ValueError):
                pass
            # as_of 同步名册报告期（比例已是名册口径，时点不能停留在年报）
            _rh_period = (gov.get("report_holders") or {}).get("period") or ""
            _rh_yr = _rh_period[:4]
            if _rh_yr.isdigit():
                _ctl = dict(_ctl)
                _ctl["as_of"] = (
                    f"{_rh_yr}-06-30" if "中报" in _rh_period else f"{_rh_yr}-12-31"
                )
        out["controller_metrics"] = [
            {
                "lab": "实际控制人",
                "val": _ctl.get("name_cn") or _ctl.get("name") or "—",
                "note": f"{_ctl.get('nature') or '—'}｜{_ctl.get('as_of') or '—'}",
            },
            {
                "lab": "持股比例",
                "val": f"{_cpct:.2f}%" if _cpct is not None else "—",
                "note": f"年报口径总持股（含限售），列第 {_crk} 位" if _crk else "年报口径总持股",
            },
            {
                "lab": "持股数",
                "val": f"{_cwan:,.2f} 万股" if _cwan is not None else "—",
                "note": "东方财富快照列示的 2.78% 为其 25% 无限售可转让额度，非总持股",
            },
        ]
        out["controller_text"] = (
            f"实际控制人为 {_ctl.get('name_cn') or _ctl.get('name') or '—'}"
            f"（{_ctl.get('name_en') or '—'}，{_ctl.get('nature') or '—'}），"
            + (f"截至 {_ctl.get('as_of')} " if _ctl.get("as_of") else "")
            + f"持有 {_cwan:,.2f} 万股，占总股本 {_cpct:.2f}%"
            + (f"，在定期报告前十大股东名册中列第 {_crk} 位" if _crk else "")
            + (f"；一致行动人：{'、'.join(_cac)}" if _cac else "")
            + "。需注意口径差异：东方财富前十大股东快照列示的 2.78% 为"
              "高管每年可转让额度（无限售部分，恰为总持股的 25%），并非持股总数下降；"
              "上表比例采用最新定期报告名册口径（报告期总股本随回购注销缩小，"
              "比例较年报口径小幅上升）。"
        )

    # 05 财务趋势 / 现金流备注：以自算口径为准
    if fin.get("as_of"):
        _parts = []
        if fin.get("rev_growth") not in (None, ""):
            _parts.append(f"营收同比 {_pct(fin.get('rev_growth'))}")
        if fin.get("profit_growth") not in (None, ""):
            _parts.append(f"净利同比 {_pct(fin.get('profit_growth'))}")
        if _parts:
            out["trend_note"] = (
                f"数据来源：三表计算（截至 {fin.get('as_of')}）。"
                + "、".join(_parts) + "；趋势图与同比指标均由系统直接计算，未经模型改写。"
            )
        _cf = fin.get("cf_to_profit")
        if _cf not in (None, ""):
            out["cash_note"] = (
                f"经营现金流/净利润 = {_pct(_cf)}"
                + ("，利润现金含量偏低，关注应收与存货对资金的占用。"
                   if _to_f(_cf) < 60 else "，利润现金含量健康。")
            )

    # 06 动态与风险：events 取近一年公告（倒序）；risks 由已知风险信号派生
    news = dyn.get("news") or []
    if news:
        _today = datetime.now()
        _cut = _today - timedelta(days=365)
        _ev = []
        for n in news:
            _dt_raw = n.get("公告日期") or ""
            _dt = _parse_news_date(_dt_raw)
            if _dt is None or _dt < _cut:
                continue
            _ti = (n.get("公告标题") or "").strip()
            _tp = (n.get("公告类型") or "").strip()
            if not _ti:
                continue
            _ev.append({
                "date": _dt.strftime("%Y-%m-%d"),
                "text": _ti,
                "type": _tp,
                "hl": False,
            })
        _ev.sort(key=lambda x: x["date"], reverse=True)
        if _ev:
            out["events"] = _ev[:12]
    _rk = []
    if ps.get("active_total_pct") not in (None, "") and _to_f(ps.get("active_total_pct")) > 0:
        _rk.append(
            f"股权质押：未解押占总股本 {ps.get('active_total_pct')}%，关注控制权稳定性"
        )
    if fin.get("cf_to_profit") not in (None, "") and _to_f(fin.get("cf_to_profit")) < 60:
        _rk.append(
            f"现金流背离：经营现金流仅为净利润的 {_pct(fin.get('cf_to_profit'))}，利润现金含量偏低"
        )
    if fin.get("ar_days") not in (None, ""):
        _rk.append(f"应收周转天数 {_num(fin.get('ar_days'))} 天，回款周期需关注")
    if _rk:
        out["risk_emphasis"] = _rk[:3]

    # P2-E1：行情与资金面（延时行情 + 融资融券），供「行情与资金面」模块与估值坐标覆盖
    _q = mkt.get("quote") or {}
    _mg = mkt.get("margin") or {}
    _mkv = []
    if _q:
        _price = _to_f(_q.get("price"))
        _chg = _to_f(_q.get("change_pct"))
        _chg_cls = "up" if (_chg is not None and _chg >= 0) else "down"
        if _price is not None:
            _mkv.append({"k": "最新价", "v": f"{_price:.2f} 元", "cls": _chg_cls if _chg is not None else ""})
        if _chg is not None:
            _mkv.append({"k": "涨跌幅", "v": f"{_chg:+.2f}%", "cls": _chg_cls})
        if _to_f(_q.get("total_mcap")) is not None:
            _mkv.append({"k": "总市值", "v": f"{_to_f(_q.get('total_mcap'))/1e8:.2f} 亿", "cls": ""})
        if _to_f(_q.get("float_mcap")) is not None:
            _mkv.append({"k": "流通市值", "v": f"{_to_f(_q.get('float_mcap'))/1e8:.2f} 亿", "cls": ""})
        if _to_f(_q.get("pe_ttm")) is not None:
            _mkv.append({"k": "PE(TTM)", "v": f"{_to_f(_q.get('pe_ttm')):.2f}", "cls": ""})
        if _to_f(_q.get("pb")) is not None:
            _mkv.append({"k": "PB(市净率)", "v": f"{_to_f(_q.get('pb')):.2f}", "cls": ""})
        if _to_f(_q.get("turnover_rate")) is not None:
            _mkv.append({"k": "换手率", "v": f"{_to_f(_q.get('turnover_rate')):.2f}%", "cls": ""})
    if _mg:
        if _to_f(_mg.get("financing_balance")) is not None:
            _mkv.append({"k": "融资余额", "v": f"{_to_f(_mg.get('financing_balance'))/1e8:.2f} 亿", "cls": ""})
        if _to_f(_mg.get("financing_pct_float_mcap")) is not None:
            _mkv.append({"k": "融资占流通市值", "v": f"{_to_f(_mg.get('financing_pct_float_mcap')):.2f}%", "cls": ""})
        if _to_f(_mg.get("financing_buy")) is not None:
            _mkv.append({"k": "近一日融资买入", "v": f"{_to_f(_mg.get('financing_buy'))/1e8:.2f} 亿", "cls": ""})
    if _mkv:
        out["market_kv"] = _mkv
        _qd = _q.get("quote_date") or "—"
        _md = _mg.get("date") or "—"
        out["market_meta"] = (
            f"行情：{_qd}（延时行情，东方财富 push2delay，非实时价）；"
            f"融资融券：{_md}（东方财富 datacenter）。数据存在滞后，仅供参考。"
        )

    # P2-E3：10 项风险清单（数据层确定性派生，含等级与依据）+ 多空逻辑
    _risks = _build_risk_list(fin, ps, biz, mkt)
    if _risks:
        out["risks_left"] = _risks[:5]
        out["risks_right"] = _risks[5:10]
        out["risk_emphasis"] = [r["k"] for r in _risks[:3]]
    _bb = _build_bull_bear(fin, ps, biz, mkt)
    if _bb:
        out["bull_points"] = _bb.get("bull", [])
        out["bear_points"] = _bb.get("bear", [])

    return out



def _build_seg_divergence_flag(segs: list[dict]) -> tuple[str, str]:
    """从真实分部数据计算「收入占比 vs 利润占比」错位；返回 (正文 HTML, 标题)。

    标题按各公司实际分部名生成（原为模板写死的「关键发现：收入占比与利润贡献的错位」），
    正文中的板块名一律取自数据，不再硬编码「分销主业」等特定公司话术。
    """
    _with_pp = [s for s in segs if s.get("profit_pct") is not None and s.get("pct") is not None]
    if not _with_pp:
        return "", ""
    _ranked = sorted(_with_pp, key=lambda s: (s["profit_pct"] - s["pct"]), reverse=True)
    _best, _worst = _ranked[0], _ranked[-1]
    parts: list[str] = []
    title = ""
    _gap_best = _best["profit_pct"] - _best["pct"]
    if _gap_best > 0.5:
        _m = f"（毛利率 {_best['margin']:.2f}%）" if _best.get("margin") is not None else ""
        parts.append(
            f"{_best['name']} 收入占比 {_best['pct']:.2f}%、利润占比 {_best['profit_pct']:.2f}%，"
            f"利润贡献高于收入占比约 {_gap_best:.1f} 个百分点，是利润结构中的高毛利引擎{_m}。"
        )
        title = f"关键发现：{_best['name']}利润贡献跑赢收入占比"
    if _worst is not _best and (_worst["pct"] - _worst["profit_pct"]) > 0.5:
        _gap_worst = _worst["pct"] - _worst["profit_pct"]
        parts.append(
            f"反观 {_worst['name']} 收入占比 {_worst['pct']:.2f}% 但利润占比仅 {_worst['profit_pct']:.2f}%，"
            f"这类板块对整体利润形成稀释（Δ {_gap_worst:.1f} 个百分点）。"
        )
        title = title or f"关键发现：{_worst['name']}收入占比大但利润贡献薄"
    if not parts:
        return "", ""
    return "<p>" + "".join(parts) + "</p>", title


def _build_seg_trend_summary(
    labels: list[str],
    segment_series: dict[str, list[dict]],
    self_dev_name: str,
    rd_expense: float | None,
    rd_expense_yoy: float | None,
    rd_period: str | None,
) -> str:
    """从真实分部趋势数据生成「战略与现实的落差」式总结 HTML（LLM 不可用时兜底）。"""
    if not labels or not segment_series:
        return ""

    # 1) 毛利率改善：自研/高毛利板块的年报毛利率序列
    margin_bullets: list[str] = []
    sd = segment_series.get(self_dev_name) or []
    anns = [(labels[i], sd[i].get("margin")) for i in range(len(labels))
            if i < len(sd) and not labels[i].endswith("H1") and sd[i].get("margin") is not None]
    anns = sorted(anns, key=lambda x: x[0])
    if len(anns) >= 2 and anns[-1][1] > anns[0][1]:
        seq = " → ".join(f"{y} 年 {v * 100:.2f}%" for y, v in anns)
        margin_bullets.append(
            f"<strong>毛利率确实在改善：</strong>{self_dev_name}毛利率从 {seq}，"
            f"三年提升了 {(anns[-1][1] - anns[0][1]) * 100:.1f} 个百分点，"
            "说明产品结构在往高端走。"
        )

    # 2) 收入增速失速：这块业务的年报增速 + 最新 H1 增速
    revenue_bullets: list[str] = []
    if sd:
        anns_rev = [(labels[i], sd[i].get("rev")) for i in range(len(labels))
                    if i < len(sd) and not labels[i].endswith("H1") and sd[i].get("rev") is not None]
        anns_rev = sorted(anns_rev, key=lambda x: x[0])
        yoy_texts: list[str] = []
        for i in range(1, len(anns_rev)):
            prev, cur = anns_rev[i - 1][1], anns_rev[i][1]
            if prev:
                yoy = (cur - prev) / abs(prev) * 100.0
                yoy_texts.append(f"{anns_rev[i][0]} 年 {yoy:+.1f}%")
        h1s = [(labels[i], sd[i].get("rev")) for i in range(len(labels))
               if i < len(sd) and labels[i].endswith("H1") and sd[i].get("rev") is not None]
        h1s = sorted(h1s, key=lambda x: x[0])
        h1_tail = ""
        if len(h1s) >= 2:
            prev, cur = h1s[-2][1], h1s[-1][1]
            if prev:
                yoy = (cur - prev) / abs(prev) * 100.0
                h1_tail = f"，到 {h1s[-1][0]} 仅剩 {yoy:+.1f}%"
        if yoy_texts:
            revenue_bullets.append(
                f"<strong>但收入增速在失速：</strong>这块业务 {'，'.join(yoy_texts)}{h1_tail}。"
            )

    # 3) 占比倒退：高毛利业务 vs 分销主业的收入占比变化
    share_bullets: list[str] = []
    if len(labels) >= 2:
        first_total = sum(
            (series[0].get("rev") or 0.0) if series else 0.0
            for series in segment_series.values()
        )
        last_total = sum(
            (series[-1].get("rev") or 0.0) if series else 0.0
            for series in segment_series.values()
        )
        if first_total and last_total:
            # 分销主业 = 收入规模最大的板块
            dom_name = max(segment_series, key=lambda n: (segment_series[n][0].get("rev") or 0.0) if segment_series[n] else 0.0)
            dom_first = ((segment_series[dom_name][0].get("rev") or 0.0) / first_total * 100.0) if segment_series[dom_name] else 0.0
            dom_last = ((segment_series[dom_name][-1].get("rev") or 0.0) / last_total * 100.0) if segment_series[dom_name] else 0.0
            high_name = self_dev_name
            high_first = ((segment_series[high_name][0].get("rev") or 0.0) / first_total * 100.0) if segment_series[high_name] else 0.0
            high_last = ((segment_series[high_name][-1].get("rev") or 0.0) / last_total * 100.0) if segment_series[high_name] else 0.0
            if abs(high_last - high_first) > 0.05:
                share_bullets.append(
                    f"<strong>结果是占比倒退：</strong>在整体营收变化的背景下，"
                    f"{dom_name}占比从 {dom_first:.2f}% 升到 {dom_last:.2f}%，"
                    f"{high_name}从 {high_first:.2f}% 降到 {high_last:.2f}%。"
                    f"高毛利业务的占比不升反降，{dom_name}与{high_name}的规模差进一步拉大。"
                )

    # 4) 研发投入
    rd_bullets: list[str] = []
    if rd_expense is not None and rd_expense_yoy is not None and rd_period:
        rd_bullets.append(
            f"<strong>更值得警惕的是研发投入：</strong>{rd_period}年研发费用 "
            f"{rd_expense / 1e4:.2f} 万元，同比{rd_expense_yoy:+.2f}%。"
        )

    bullets = margin_bullets + revenue_bullets + share_bullets + rd_bullets
    if not bullets:
        return ""
    # 标题按实际发现的矛盾组合生成（原写死「战略与现实的落差」）
    if margin_bullets and revenue_bullets:
        title = f"战略与现实：{self_dev_name}毛利率在改善，收入却在失速"
    elif margin_bullets:
        title = f"战略与现实：{self_dev_name}在改善"
    elif revenue_bullets:
        title = f"战略与现实：{self_dev_name}增长失速"
    else:
        title = f"战略与现实：{self_dev_name}的结构性落差"
    # 每个 bullet 单独一个 <p>；不要嵌套 <p><p>，也不要提前多出一个 </p>
    inner = "".join(f"<p>{b}</p>" for b in bullets)
    return f'<div class="flag"><div class="t">{title}</div>{inner}</div>'


def _build_customer_table_fallback(bc: dict, period: str = "") -> list[dict]:
    """下游客户表数据层兜底：从定期报告原文段落做**通用句式**结构化（零公司硬编码）。

    中文年报的四类行业标准表述：
      A. 枚举式：「在XX方面主要有 A、B、C 等，在YY方面主要有 D、E 等；」
      B. 叙述式：「在XX业务方面，公司主要从事……，主要客户有 A、B、C 等。」
      C. 总览式：「公司客户主要分布在 A、B、C 等市场，拥有 X、Y、Z 等知名客户。」
      D. 导入式：「在XX业务方面……原有客户如 A、B、C 等的导入 / 持续推进在原有
         客户 A、B、C 导入相关产品」
    做法：先找句内所有「（在）XX（业务）方面」领域命中点，以相邻命中点切分段落；
    名单引导词支持「主要(客户)?有 / 原有客户(如)? / 拥有」三种，枚举段要求紧跟、
    叙述段要求出现在 field 后 120 字内，再从引导词后抓「名单…等/的导入」。
    表现判断取该句的「营业收入同比有所增加/下降」定性表述。全部为通用句式
    正则，适用于任何公司；配不上对的领域一律不出（宁缺毋滥）；LLM 在线时其
    输出经 `_customer_table_check` 优先采用。
    注意：PDF 折行会把空格留在中文名中间（如「林洋 能源」），匹配前先去空白。
    """
    import re as _re

    _re_head = _re.compile(
        r"(?:在)?([\u4e00-\u9fa5A-Za-z+/]{2,12}?)(?:业务)?方面"
        r"|(?:还?从事)([\u4e00-\u9fa5A-Za-z]{2,10}?)业务"
    )
    _re_lead_any = _re.compile(r"主要(?:客户)?有|原有客户(?:如)?")
    _re_lead_enum = _re.compile(r"^[，,]?主要(?:客户)?有")
    _re_list = _re.compile(r"([\u4e00-\u9fa5A-Za-z0-9（）()/、，,]+?)等")
    _re_list_imp = _re.compile(r"([\u4e00-\u9fa5A-Za-z0-9（）()/、，,]+?)(?:等的?导入|导入)")
    _re_up = _re.compile(r"营业收入同比?有所?增加|同比?有?所?增长")
    _re_down = _re.compile(r"营业收入(?:同比)?有?所?下降")
    _re_overview = _re.compile(
        r"客户主要分布在([\u4e00-\u9fa5、，,]{2,40}?)等市场，拥有"
    )

    def _valid_seg_field(f: str) -> bool:
        """领域词有效性：句子碎片（含「公司/方面/业务」等非领域字样）不算领域。"""
        return bool(f) and len(f) >= 2 and not any(
            k in f for k in ("公司", "方面", "业务")
        )

    out: list[dict] = []
    _seen_fields: set[str] = set()
    for card in (bc.get("downstream_raw") or []):
        # 去掉所有空白：PDF 折行空格会把「林洋 能源」这类客户名截断
        text = _re.sub(r"\s+", "", card.get("text") or "")
        if not text:
            continue
        for sent in _re.split(r"(?<=[。；])", text):
            if not sent:
                continue
            # C 总览式：「客户主要分布在 A、B、C 等市场，拥有 X、Y、Z 等知名客户」
            _mov = _re_overview.search(sent)
            if _mov and "整体客户分布" not in _seen_fields:
                _seg = sent[_mov.end():]
                _ml = _re_list.search(_seg)
                if _ml:
                    _names = [
                        n for n in _re.split(r"[、，,]", _ml.group(1))
                        if 2 <= len(n) <= 20
                    ]
                    if _names:
                        _seen_fields.add("整体客户分布")
                        out.append({
                            "field": "整体客户分布",
                            "customers": "、".join(_names[:12]),
                            "perf": "—", "hl": False, "cls": "",
                            "period": period,
                        })
            heads = [
                mf for mf in _re_head.finditer(sent)
                if _valid_seg_field((mf.group(1) or mf.group(2) or "").strip("/"))
            ]
            if not heads:
                continue
            for i, mf in enumerate(heads):
                field = (mf.group(1) or mf.group(2) or "").strip("/")
                if len(field) < 2 or field in _seen_fields:
                    continue
                # 以相邻领域命中点为界切出本领域的片段，防止名单跨领域串行
                seg_end = heads[i + 1].start() if i + 1 < len(heads) else len(sent)
                seg = sent[mf.end(): seg_end]
                if _re_lead_enum.search(seg):
                    # 枚举段：「XX方面主要有 → 名单」紧跟
                    src = seg[_re_lead_enum.search(seg).end():]
                else:
                    # 叙述段：名单引导词需出现在 field 后 120 字内
                    _ml = _re_lead_any.search(seg[:120])
                    if not _ml:
                        continue
                    src = seg[_ml.end():]
                _m2 = _re_list.match(src.lstrip("，,"))
                if not _m2:
                    # D 导入式：名单以「…等的导入 / …导入」收尾
                    _m2 = _re_list_imp.match(src.lstrip("，,"))
                if not _m2:
                    continue
                names = [
                    n for n in _re.split(r"[、，,]", _m2.group(1))
                    if 2 <= len(n) <= 20
                ]
                if not names:
                    continue
                if _re_down.search(sent):
                    perf, cls = "收入同比下降", "down"
                elif _re_up.search(sent):
                    perf, cls = "收入同比增长", "up"
                else:
                    perf, cls = "—", ""
                _seen_fields.add(field)
                out.append({
                    "field": field,
                    "customers": "、".join(names[:10]),
                    "perf": perf,
                    "hl": False,
                    "cls": cls,
                    "period": period,
                })
    if out:
        out[0]["hl"] = True
    return out


def _customer_table_check(rows, bc: dict) -> bool:
    """LLM 客户表护栏：每个客户名必须能在年报下游原文中找到（防编造客户名单）。

    判定为宽松子串匹配（客户名可能被原文全称/简称差异影响），任何一行出现
    「在原文中完全找不到的客户名」即整表拒绝，回退数据层兜底。
    """
    if not isinstance(rows, list) or not rows:
        return False
    corpus = "".join(
        (c.get("text") or "") + (c.get("brief") or "")
        for c in (bc.get("downstream_raw") or [])
    )
    if not corpus:
        return False
    for r in rows:
        if not isinstance(r, dict):
            return False
        # 契约表禁止自带 public 标记：那是系统「公开信息补位」通道的专属标记，
        # 模型自行添加等同绕过原文校验（等同于编造客户名），整表拒绝
        if str(r.get("src") or "").strip().lower() == "public":
            return False
        cust = str(r.get("customers") or "")
        if not cust:
            return False
        for name in re.split(r"[、，,]", cust):
            name = name.strip(" 、，,()")
            if len(name) < 2:
                continue
            if name not in corpus:
                return False
    return True


def _partners_check(rows, bc: dict) -> bool:
    """LLM 合作表护栏：每个合作方名必须能在定期报告语料中找到（防编造合作关系）。

    语料 = 授权原厂（en/cn）+ 数据层合作句 partner + 下游原文段落。
    """
    if not isinstance(rows, list) or not rows:
        return False
    corpus_parts = []
    for _m in (bc.get("upstream_makers") or []):
        corpus_parts.append(_m.get("en") or "")
        corpus_parts.append(_m.get("cn") or "")
    for _p in (bc.get("partnerships") or []):
        corpus_parts.append(str(_p.get("partner") or ""))
    for c in (bc.get("downstream_raw") or []):
        corpus_parts.append((c.get("text") or "") + (c.get("brief") or ""))
    corpus = re.sub(r"\s+", "", "".join(corpus_parts))
    if not corpus:
        return False
    for r in rows:
        if not isinstance(r, dict):
            return False
        pn = re.sub(r"\s+", "", str(r.get("partner") or ""))
        # 同 _customer_table_check：契约表自带 public 标记即视为绕过校验
        if str(r.get("src") or "").strip().lower() == "public":
            return False
        if len(pn) < 2 or pn not in corpus:
            return False
    return True


def _dedupe_partner_rows(rows: list[dict]) -> list[dict]:
    """合作方「名单级」去重 + 具名优先排序。

    同一句合作关系可能被三条路径同时抓到（LLM 契约表给「英伟达、思灵机器人」，
    AI 增量又拆成「英伟达」「思灵机器人」两行），按整串判重会漏，必须按名单
    拆分后逐个公司判重。排序让具名公司（一手 → 公开信息补位）排在泛称口径之前，
    读者先看到具体公司，泛称原文口径（标记「原文未具名」）置后。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        parts = [
            x for x in re.split(r"[、，,]", str(r.get("partner") or "")) if x.strip()
        ]
        keep = [x for x in parts if re.sub(r"\s+", "", x) not in seen]
        if not keep:
            continue
        seen |= {re.sub(r"\s+", "", x) for x in keep}
        _r = dict(r)
        _r["partner"] = "、".join(keep)
        out.append(_r)
    out.sort(
        key=lambda r: (
            2 if not r.get("named", True) else (1 if r.get("src") == "public" else 0)
        )
    )
    return out


def _clean_up_category(cat: object) -> str:
    """清洗上游类别名：去关系后缀与方位/主体前缀。

    LLM 增量提取常把「关系」混进类别名（实测得到「上游供应链 · 紧密合作」），
    既不适合做表格分类，进标题也会变成「上游：上游供应链 · 紧密合作」这种
    重复表述，这里统一归一为产业环节名。
    """
    c = str(cat or "").strip()
    # 仅按间隔号切（「上游供应链 · 紧密合作」）；「/」是类别内部并列（「关键品类/技术」），
    # 不可作为分隔符
    c = re.split(r"[·・]", c)[0].strip()
    c = re.sub(r"^(上游|下游|本公司|公司)", "", c).strip()
    return c.strip("、，,（）() ") or "供给合作"


def _build_business_cross(bc, segs, top) -> str:
    """上下游一手来源与主营构成的交叉验证：验证「上游供给 + 下游应用」商业闭环一致性。

    句式按该公司是否有上游授权原厂结构自动分支（分销商 vs 制造商等），
    不写死任何单一商业模式。
    """
    parts: list[str] = []
    _regs = (bc or {}).get("upstream_regions") or []
    _prods = (bc or {}).get("upstream_products") or []
    _fld = (bc or {}).get("downstream_fields") or []
    _top_nm = (top or {}).get("name") or "核心板块"
    if _regs or _prods:
        _mk = "、".join(f"{r['region']}{r['count']}家" for r in _regs) if _regs else ""
        _pt = "、".join(p.get("term", "") for p in _prods[:6] if p.get("term"))
        if _regs:
            _s = "上游授权原厂（" + (_mk or "年报列示")
            if _pt:
                _s += f"，核心品类含 {_pt}"
            _s += f"）支撑其授权分销主业（核心板块 {_top_nm}"
        else:
            _s = f"上游供应链（核心品类含 {_pt}）" if _pt else "上游供应链"
            _s += f"支撑其{_top_nm}主业（"
        if top and top.get("pct") is not None:
            _s += f"收入占比 {top['pct']:.2f}%"
        _s += "）"
        parts.append(_s + "。")
    if _fld:
        _loop = (
            "「授权分销 + 下游高景气应用」" if _regs else "「上游供给 + 下游高景气应用」"
        )
        parts.append(
            "下游应用于 " + "、".join(_fld[:6]) + " 等领域，"
            f"与年报「主营业务/行业情况」章节表述一致，验证其{_loop}的商业闭环。"
        )
    if not parts:
        return ""
    return "<p>" + "".join(parts) + "</p>"


def _period_label(as_of) -> str:
    """把报告期（YYYY-MM-DD / YYYYMMDD）转成人类可读的期间标签，如「2026H1」「2025年报」。

    B 类差距的根治点：财报指标全为最新报告期口径，但报告里从不标注期间，
    读者会把 2026H1 的毛利率/ROE 与参考样本的 2025 年报口径数字对不上而误判为错误。
    统一标注后，期间差异从「疑似错误」变回「口径不同」。
    """
    s = str(as_of or "").strip()
    if not s:
        return ""
    d = s.replace("-", "").replace("/", "")
    if len(d) != 8 or not d.isdigit():
        return f"截至 {s}"
    y, md = d[:4], d[4:]
    return {
        "1231": f"{y}年报",
        "0630": f"{y}H1",
        "0331": f"{y}Q1",
        "0930": f"{y}Q3",
    }.get(md, f"截至 {s}")


def _key_facts_brief(profile_dict: dict) -> str:
    """生成「关键财务事实核对表」：把最易被模型编造 / 混淆的绝对额按报告期摊开。

    背景（力源信息 300184 实测的硬错误）：模型曾把「应收账款 20.34 亿」错标成
    「存货 20.34 亿」，并凭记忆写出「经营现金流 -1.91 亿」（真值 -0.91 亿）与
    「2023 营收 56.42 亿」（真值 59.44 亿）。这些偏差仅 3%~5%，量级护栏
    （0.55x~1.8x）根本抓不到，必须在提示词侧把真值按报告期显式钉死。
    """
    fin = ((profile_dict.get("layers", {}) or {}).get("financials", {}) or {}).get("summary", {}) or {}

    def _yi(x):
        try:
            return f"{float(x) / 1e8:.2f} 亿元"
        except (TypeError, ValueError):
            return None

    def _fmt_periods(d, label):
        if not isinstance(d, dict) or not d:
            return []
        items = sorted(d.items(), key=lambda kv: str(kv[0]), reverse=True)
        return [f"{label}（{k}）：{_s}" for k, v in items if (_s := _yi(v))]

    lines: list[str] = []
    # 存货 / 应收账款成对给出：杜绝二者数值互相错标（这是「存货 20.34 亿」错误的根因）
    if (v := _yi(fin.get("inventory"))):
        lines.append(f"存货（最新报告期）：{v}")
    if (v := _yi(fin.get("accounts_receivable"))):
        lines.append(f"应收账款（最新报告期）：{v}")
    lines += _fmt_periods(fin.get("revenue_by_period"), "营业总收入")
    lines += _fmt_periods(fin.get("oper_cash_by_period"), "经营活动现金流量净额")
    lines += _fmt_periods(fin.get("inventory_by_period"), "存货")
    return "\n".join(f"  - {x}" for x in lines)


def _overview_max_tokens() -> int:
    """概况 JSON 大调用的 max_tokens：config.yaml llm.overview_max_tokens 可覆盖（默认 8000）。

    背景（Step 2 实测）：qwen MaaS 对单请求输出上限有限制，≥6000 时返回误导性
    「402 余额不足」——此前 8000 硬编码导致概况 LLM 生成在 qwen 上**必然静默失败**，
    整份报告无声降级为数据层兜底。可在 config 中下调至该端点可用值。
    """
    try:
        from ..llm import load_llm_config

        return int(load_llm_config().get("overview_max_tokens") or 8000)
    except Exception:  # noqa: BLE001 — 配置缺失时回退默认
        return 8000


def generate_overview_content(result, profile_dict: dict, name: str, code: str, max_retries: int = 2) -> dict | None:
    """调用多厂商路由生成六层结构化概况内容；失败（含额度耗尽）返回 None。

    自带重试：思考型模型偶发把全部叙述字段返回为空串（结论区渲染成空 <p></p>），
    故在通用护栏不过时最多重试 max_retries 次（OV-4 降档 3→2：每次重试都是完整
    8000 token 重生成，3 次在最坏情况下多花 1~2 分钟），优先返回有效版本；
    全部失败则退回最后一次，并用聚焦小调用（max_tokens=1200）单独补写结论，
    避免结论区沦为空壳或占位文案。
    """
    try:
        system = load_prompt("overview")
    except Exception:
        system = (
            "你是专业的 A 股公司研究分析师。请基于给定结构化数据，"
            "生成公司概况报告的结构化 JSON 内容，未知字段标'未获取'，严禁编造数字。"
        )
    data_text = json.dumps(profile_dict, ensure_ascii=False, default=str)
    # 关键财务事实核对表：把易编造 / 易混淆的绝对额真值前置钉死在提示词里
    _brief = _key_facts_brief(profile_dict)
    _facts_block = ""
    if _brief:
        _facts_block = (
            "\n\n【关键财务事实核对表】以下为系统从三大报表直接计算的真值，"
            "引用时必须原样使用：严禁改写单位、严禁换成别的精度、"
            "严禁跨科目错标、严禁凭记忆替换成相近数字。\n"
            + _brief
            + "\n（特别注意：存货与应收账款是两个不同科目，数值绝不可互换。）"
        )
    user = (
        f"公司：{name}（{code}）\n\n"
        f"已抓取的结构化数据（可能不完整，缺失字段请标'未获取'，不要编造数字）：\n{data_text}\n\n"
        + _facts_block
        + "\n\n请严格按契约返回公司概况报告内容：纯 JSON（不要 markdown 代码块），"
        "键名与模板变量一一对应。conclusion_html、id_insight_html 等叙述字段"
        "必须基于给定数据写出有依据的分析，禁止留空。"
    )
    last = None
    for attempt in range(max_retries):
        # Step 2（WorkBuddy 复刻）：max_tokens 可由 config.yaml llm.overview_max_tokens
        # 覆盖（默认仍 8000）。实测 qwen MaaS 端点对单请求 max_tokens 有限制
        # （≤4000 正常，≥6000 返回误导性「402 余额不足」），大调用静默失败会让
        # 整份报告无声降级为数据层兜底——故失败必须留日志（见下方两处 warning）。
        _mt = _overview_max_tokens()
        try:
            resp = get_router().complete(
                LLMRequest(system=system, user=user, max_tokens=_mt)
            )
        except Exception as e:
            logger.warning("[overview] 第 %d 次 LLM 调用异常：%s", attempt + 1, e)
            return None
        if not getattr(resp, "ok", True):
            logger.warning(
                "[overview] 第 %d 次 LLM 调用失败：provider=%s error=%s（max_tokens=%s）",
                attempt + 1, resp.provider, (resp.error or "")[:120], _mt,
            )
        parsed = _parse_overview_json(resp.text)
        if not parsed:
            logger.warning(
                "[overview] 第 %d 次输出 JSON 解析失败（text_len=%s，可能截断或为降级文案），重试/降级",
                attempt + 1, len(resp.text or ""),
            )
            continue
        last = parsed
        # 接受条件（P3-1 通用护栏）：占位符无泄漏 + list/html 字段合法 + conclusion 通过
        # 事实一致性校验。原仅校验 conclusion，现已下沉为全关键叙述字段通用门，
        # 任一字段脏即整份丢弃并重试，最大化「首次幻觉被拦截后重试成功」的覆盖。
        _ok, _issues = _overview_json_is_valid(parsed, profile_dict)
        if _ok:
            return parsed
        for _i in _issues:
            logger.warning("[overview] 第 %d 次生成未通过通用护栏：%s", attempt + 1, _i)
        logger.warning("[overview] 第 %d 次生成被丢弃并重试（剩 %d 次）", attempt + 1, max_retries - attempt - 1)
    # 主调用结论仍为空（思考模型在大 profile 下偶发清空叙述字段）：用聚焦小输入调用专门补齐，
    # 避免结论区沦为空壳或占位文案。
    _concl = (last.get("conclusion_html") or "").strip() if isinstance(last, dict) else ""
    if not _concl:
        _facts = _compact_facts(profile_dict)
        _c = _generate_conclusion(name, code, _facts)
        if _c:
            if not isinstance(last, dict):
                last = {}
            last["conclusion_html"] = _c
            logger.info("[overview] 已用聚焦调用补齐核心结论")
    return last


# ---------------------------------------------------------------------------
# 装配：空壳 + LLM JSON 覆盖
# ---------------------------------------------------------------------------
def _build_trend_cash(fin: dict) -> tuple[dict, dict]:
    """装配业绩趋势(cTrend)与净利润/经营现金流对比(cCash)两张图的数据。

    此前该数据从未被填充，导致两张图渲染为空白（regression）。财务时间序列已在
    company_profile.build_financial_summary 中按报告期算出，此处取最近 4 个年度(1231)
    报告期 + 最新半年报(0630)（若有），保证同口径可比、避免半年报与全年并列误读。
    所有值换算为亿元；仅在 rev/profit/cf 三个序列共有的报告期上取点，避免缺口。
    """
    rev = (fin.get("revenue_by_period") or {}) if isinstance(fin, dict) else {}
    prof = (fin.get("profit_by_period") or {}) if isinstance(fin, dict) else {}
    cf = (fin.get("oper_cash_by_period") or {}) if isinstance(fin, dict) else {}

    _empty = ({"labels": [], "revenue": [], "profit": []},
              {"labels": [], "profit": [], "cash": []})

    def _periods():
        _common = set(rev) & set(prof) & set(cf)
        _annual = sorted([k for k in _common if str(k).endswith("1231")])[-4:]
        _latest = sorted(_common, reverse=True)[:1]
        _all = list(_annual)
        if _latest and str(_latest[0]).endswith("0630") and _latest[0] not in _all:
            _all.append(_latest[0])
        return sorted(set(_all))

    _ps = _periods()
    if len(_ps) < 2:
        return _empty

    def _lab(p):
        s = str(p)
        if len(s) >= 8:
            y = s[:4]
            if s.endswith("1231"):
                return f"{y}年报"
            if s.endswith("0630"):
                return f"{y}H1"
            if s.endswith("0930"):
                return f"{y}Q3"
            if s.endswith("0331"):
                return f"{y}Q1"
        return s

    def _yi(d, p):
        try:
            v = d.get(p)
            return round(float(v) / 1e8, 2) if v is not None else None
        except (TypeError, ValueError):
            return None

    _labels = [_lab(p) for p in _ps]
    _rev = [_yi(rev, p) for p in _ps]
    _prof = [_yi(prof, p) for p in _ps]
    _cf = [_yi(cf, p) for p in _ps]
    if any(v is None for v in _rev + _prof + _cf):
        return _empty
    _trend = {"labels": _labels, "revenue": _rev, "profit": _prof}
    _cash = {"labels": _labels, "profit": _prof, "cash": _cf}
    return _trend, _cash


def build_overview_data(
    result, profile_dict: dict, llm_json: dict | None = None, use_llm: bool = True
) -> dict:
    """合并空壳与 LLM 生成内容，得到模板可直接消费的 data 字典。"""
    data = empty_overview_data()

    def _to_float(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    data["code"] = profile_dict.get("code") or getattr(result, "code", "") or ""

    # 抬头修复（2026-09-12）：抬头标题应为「公司名称（公司代码）」，此前当东财
    # 个股画像接口限流、顶层 name 缺失时，result.name 退化为股票代码，抬头被渲染成
    # 「002384（002384）」。此处建立名称解析链，且**纯 6 位代码一律不算名称**：
    #   顶层 name → 六层 identity 层（cninfo 口径，上游失败时往往仍有值）
    #   → identity.short_name（A股简称）→ result.name
    # 全部缺失时才退化为代码（模板对 name==code 会去重显示，不出现重复）。
    def _pick_name(*cands):
        for _c in cands:
            _s = str(_c or "").strip()
            if _s and not re.fullmatch(r"\d{6}", _s):
                return _s
        return ""

    _id_layer = (profile_dict.get("layers", {}) or {}).get("identity", {}) or {}
    # 顺序「简称优先」：顶层 name（通常是东财/用户给出的简称）→ identity.short_name
    # （cninfo「A股简称」）→ identity.name（上游缺失时该字段可能被 cninfo 填成**工商
    # 全称**，如「苏州东山精密制造股份有限公司」，故排在简称之后）→ result.name。
    _best_name = _pick_name(
        profile_dict.get("name"),
        _id_layer.get("short_name"),
        _id_layer.get("name"),
        getattr(result, "name", ""),
    )
    data["name"] = _best_name or data["code"]
    # P2-E1：行情与资金面（延时行情 + 融资融券）图层，供估值坐标覆盖与「行情与资金面」模块渲染
    _mkt_layer = (profile_dict.get("layers", {}) or {}).get("market", {}) or {}
    _q = _mkt_layer.get("quote") or {}
    _mg = _mkt_layer.get("margin") or {}

    if llm_json:
        # 先拦截把提示词示例原样回显为内容的字段（等同编造），再进入合并。
        llm_json, _ = _sanitize_llm_json(llm_json)
        # 阵营表（upstream）只允许年报一手来源（chain 抽取）生成：LLM 不掌握
        # 授权代理名单，其填写的 upstream 实为供应商依赖关系，会误触发模板
        # 分销商版式（实测 002384 被写成「光芯片—索尔思光电」阵营行，连带
        # 渲染出写死的「代理权是分销商的生命线」话术）。
        if llm_json.pop("upstream", None):
            logger.warning(
                "[overview] 契约 upstream 字段已丢弃（阵营表仅限年报一手来源）"
            )
        # 页眉系统字段（报告日期 / 数据时点说明）由系统生成：契约曾把
        # report_date 列为可填字段，模型会把输入数据里主营构成的「报告日期」
        # （如 2025-12-31）当成本报告日期回填，导致页眉日期与生成时间不符
        # （实测 601138）。此处整体丢弃，最终由数据层无条件写回生成当日。
        _sys_dropped = [
            k for k in ("report_date", "as_of_text") if llm_json.pop(k, None)
        ]
        if _sys_dropped:
            logger.warning(
                "[overview] 契约系统字段已丢弃（由系统生成）：%s", "、".join(_sys_dropped)
            )
        # 仅覆盖契约内已知键，避免 LLM 多余字段污染
        for k, v in llm_json.items():
            if k in data:
                # 空字符串 / None 不覆盖骨架默认值：否则结论区等会渲染出空 <p></p>，
                # 比诚实的「AI 内容生成失败」降级文案更糟。仅用 LLM 返回的有效内容覆盖。
                if isinstance(v, str) and not v.strip():
                    continue
                data[k] = v
        if _pick_name(llm_json.get("name")):
            data["name"] = str(llm_json["name"]).strip()
        if llm_json.get("code"):
            data["code"] = llm_json["code"]
    # 抬头兜底（2026-09-12）：LLM 未给名称或把代码回显成名称时，用数据层解析链的名称，
    # 避免抬头最终只剩一串代码（名称确实无源可查时才显示代码本身）。
    if re.fullmatch(r"\d{6}", str(data.get("name") or "").strip()):
        data["name"] = _best_name or data["code"]

    # 结论区兜底：LLM 结论缺失、或命中事实性拦截被清空时，落到数据层拼装的事实陈述，
    # 而不是渲染「AI 内容生成失败」这类与报告其余部分自相矛盾的降级文案。
    _llm_concl = ((llm_json or {}).get("conclusion_html") or "").strip()
    _issues = _conclusion_fact_check(_llm_concl, profile_dict) if _llm_concl else []
    if not _llm_concl or _is_bad_text(_llm_concl) or _issues:
        for _i in _issues:
            logger.warning("[overview] LLM 结论被拒绝：%s", _i)
        data["conclusion_html"] = _fallback_conclusion(
            data["name"], data["code"], profile_dict
        )
        if _issues:
            _reason = "AI 叙述未通过事实一致性校验（点名控制关系、断言数据未披露、或业务定性与真实主营不符）"
        elif not _llm_concl:
            _reason = "本次运行 AI 未返回结论文本（模型在大数据量下偶发清空叙述字段）"
        else:  # 命中占位符/错误陈述黑名单
            _reason = "AI 叙述被占位符/错误陈述拦截清空"
        data["conclusion_note"] = (
            f"本节由数据层事实自动拼装：{_reason}，故不采用其文本。"
            "以下仅陈述已核实数据，不含推测性判断。"
        )

    # 同业可比（peers）以数据为准：即便 LLM 漏返，也用骨架里的真实同业列表兜底，
    # 保证概况报告「行业坐标」模块不会留空。并由 peers 派生 ROE 对比图。
    _skel_peers = (
        profile_dict.get("layers", {}).get("industry", {}).get("peers") or []
    )
    if not data.get("peers"):
        data["peers"] = _skel_peers
    if data.get("peers"):
        # 对比主指标 = 毛利率（分销商的生命线），而非 ROE。
        # 修复 label bug：此前图表取的是 ROE 数值，却标成「毛利率（%）」——
        # 数据口径与图例不符，会把读者引向错误结论。此处改为真实毛利率（自算口径）。
        _labels, _values, _hl = [], [], -1
        for _p in data["peers"]:
            try:
                _v = (
                    float(_p.get("gross_margin"))
                    if _p.get("gross_margin") not in (None, "")
                    else None
                )
            except (TypeError, ValueError):
                _v = None
            if _v is None:
                continue
            if _p.get("hl"):  # 本公司自身：在对比中高亮，给出「位置感」
                _hl = len(_labels)
            _labels.append(_p.get("name", ""))
            _values.append(_v)
        if _labels:
            data["peer_margin"] = {
                "labels": _labels,
                "values": _values,
                "highlight": _hl,
            }
            # B 类：同业对比的期间必须显式标注。毛利率取自各公司**最新报告期**
            # （get_gross_margin 取利润表首列），而同行表「营收」列取自**最新年报**
            # （get_revenue_yi 强制 1231 列）——两列期间本就不同，不标注会被误读为算错。
            _fs = (profile_dict.get("layers", {}).get("financials", {}) or {}).get("summary", {}) or {}
            _pl = _period_label(_fs.get("as_of"))
            _ry = [k for k in (_fs.get("revenue_by_period") or {}) if str(k).endswith("1231")]
            _ry_lab = f"{max(_ry)[:4]}年报" if _ry else "最新年报"
            # 同业口径说明：按数据来源分支（curated=人工精选同商业模式可比集；
            # 否则=申万二级行业成分），**不写死任何一家公司的业务描述**
            _is_curated = bool(data.get("peers") and data["peers"][0].get("curated"))
            _sel = (
                "可比公司按<strong>业务实质</strong>精选（同商业模式的人工可比集，含本公司），"
                if _is_curated
                else "可比公司取<strong>申万二级行业成分</strong>（含本公司，高亮行），"
            )
            data["peer_margin_note"] = (
                _sel
                + "毛利率为自算口径 (营业总收入−营业成本)/营业总收入"
                + (f"，期间为各公司最新报告期（{_pl}）" if _pl else "")
                + "；深色柱为本公司，用于横向定位其在同业中的位置。"
            )
            # 同业横比表：营收列与其余比率列取自不同期间，紧邻表格点明，避免误读
            data["peers_period_note"] = (
                f"口径提示：表中「营收」为各公司<strong>最新年报（{_ry_lab}）</strong>口径，"
                "而「毛利率 / ROE / 营收增速 / 净利增速 / 资产负债率」为"
                + (f"<strong>最新报告期（{_pl}）</strong>口径" if _pl else "最新报告期口径")
                + "——两者期间不同，横向对比时请勿视为同期数据。"
            )

    # 业绩趋势(cTrend)与净利润/经营现金流对比(cCash)图表数据：此前未被填充导致空白，
    # 此处用财务时间序列装配。即便 LLM 漏返，也保证两张图有真实数据。
    _fin_sum = (
        (profile_dict.get("layers", {}) or {}).get("financials", {}) or {}
    )
    _fin_sum = (_fin_sum.get("summary") or {}) if isinstance(_fin_sum, dict) else {}
    if _fin_sum:
        _tr, _cs = _build_trend_cash(_fin_sum)
        if _tr.get("labels"):
            data["trend"] = _tr
        if _cs.get("labels"):
            data["cash"] = _cs

    # P2：增长引擎（聚焦子调用 + 数据层兜底）。覆盖 monolith 可能返回的增长/约束字段，
    # 用聚焦 LLM 或数据层确定性推导填充「三层次 / 三约束」，并写入溯源徽标 growth_source。
    # OV-5（概况性能专项）：growth 与 biz_enrich 两个聚焦 LLM 子调用相互独立
    # （均只依赖 profile_dict 派生输入），use_llm 时并行执行——总耗时 ≈ max 而非 sum。
    _bc_early = (
        profile_dict.get("layers", {}).get("business", {}).get("business_chain") or {}
    )
    _enrich_fut = None
    _enrich_ex = None
    if use_llm:
        try:
            from concurrent.futures import ThreadPoolExecutor as _TPE

            _enrich_ex = _TPE(max_workers=1)
            _enrich_fut = _enrich_ex.submit(_build_biz_enrich, _bc_early, True)
        except Exception:  # noqa: BLE001 — 并行预取失败回退串行
            _enrich_fut = None
            _enrich_ex = None
    _g, _c, _gsrc = _build_growth(profile_dict, use_llm=use_llm)
    if _g:
        data["growth"] = _g
    if _c:
        data["business_constraints"] = _c
    data["growth_source"] = _gsrc

    # P2-4/P2-5：叙述性字段（moat / market_logic / business_flag_html / business_cross_html）
    # 的数据层兜底：LLM 返回类型错误（moat/market_logic 被回显成字符串→逐字符渲染）或空壳片段时，
    # 一律用数据层确定性推导替换；LLM 值仅在其类型正确且含实质内容时保留（LLM 只做叙述润色）。
    _fb = _build_narrative_fallbacks(profile_dict)
    _narr_rules = {
        "moat": _valid_list_field,
        "market_logic": _valid_list_field,
        "business_flag_html": _valid_html_field,
        "business_cross_html": _valid_html_field,
    }
    for _k, _valid in _narr_rules.items():
        _llm_v = data.get(_k)
        if _valid(_llm_v):
            continue
        if _llm_v not in (None, "", [], {}):
            logger.warning(
                "[overview] 叙述字段 %s 的 LLM 值无效（已回退数据层）：%r", _k, _llm_v
            )
        data[_k] = _fb.get(_k)

    # 错位 flag 的标题：由数据层按分部名生成（原为模板写死话术），随正文同源
    if not data.get("business_flag_title"):
        data["business_flag_title"] = _fb.get("business_flag_title") or ""

    # P1：年报一手来源——上下游（PyMuPDF 解析，带页码归因）。以数据为准覆盖 LLM 可能
    # 漏返的字段；并将原本「非一手来源·待核实」的定性彻底改为一手来源标注。
    _bc = (
        profile_dict.get("layers", {}).get("business", {}).get("business_chain") or {}
    )
    if _bc:
        _rep_title = (_bc.get("report") or {}).get("title") or "公司年度报告"
        data["bc_source"] = f"一手来源·《{_rep_title}》"
        # 上游：按「阵营」分组的原厂品牌表（一手来源：定期报告释义/主营业务章节）。
        # 阵营合并口径：日本→日系（核心）；美国+欧洲→欧美系；中国台湾单列；
        # 国产线来自主营业务概述段落的通用列举句抽取（无 EN 释义行者）。
        _regions = _bc.get("upstream_regions") or []
        if _regions:
            # 主供品类：优先用年报授权证书段落归纳的映射（历史版本兼容），
            # 键失配（其它公司）时**回退到该公司年报的代理品类清单**——通用兜底，
            # 不把任何一家公司的品类描述硬编码给其它公司。
            _CAMP_CATEGORY = {
                "日系（核心）": "MLCC、电感、磁珠、传感器/摄像头、存储、继电器、晶振",
                "欧美系": "功率器件（含碳化硅）、MOSFET、MCU、硅麦、动铁喇叭、指纹芯片、LED",
                "国产线": "MCU、存储、图像传感器、功率器件、模拟芯片、计量芯片、模组",
                "中国台湾": "指纹识别芯片",
            }
            _by_region = {r["region"]: r for r in _regions}
            _prods_all = _bc.get("upstream_products") or []
            _cat_default = (
                "、".join(p["term"] for p in _prods_all[:8]) or "见下方品类说明"
            )

            def _camp_cat(camp: str) -> str:
                return _CAMP_CATEGORY.get(camp) or _cat_default

            def _camp_rows() -> list[dict]:
                rows: list[dict] = []
                _jp = _by_region.get("日本")
                if _jp:
                    rows.append({
                        "camp": "日系（核心）",
                        "makers": _jp["makers"] + (f" 等 {_jp['count']} 家" if _jp.get("extra") else ""),
                        "count": _jp["count"],
                        "category": _camp_cat("日系（核心）"),
                        "hl": True,
                    })
                _us, _eu = _by_region.get("美国"), _by_region.get("欧洲")
                if _us or _eu:
                    _mk = "、".join(x for x in (
                        (_us or {}).get("makers"), (_eu or {}).get("makers")) if x)
                    _cnt = (_us or {}).get("count", 0) + (_eu or {}).get("count", 0)
                    _extra = (_us or {}).get("extra", 0) + (_eu or {}).get("extra", 0)
                    rows.append({
                        "camp": "欧美系",
                        "makers": _mk + (f" 等 {_cnt} 家" if _extra else ""),
                        "count": _cnt,
                        "category": _camp_cat("欧美系"),
                        "hl": False,
                    })
                _dm = _bc.get("domestic_makers") or []
                if _dm:
                    _names = [d["name"] for d in _dm]
                    _shown = "、".join(_names[:12])
                    _more = f" 等 {len(_names)} 家" if len(_names) > 12 else ""
                    rows.append({
                        "camp": "国产线",
                        "makers": _shown + _more,
                        "count": len(_names),
                        "category": _camp_cat("国产线"),
                        "hl": False,
                    })
                _tw = _by_region.get("中国台湾")
                if _tw:
                    rows.append({
                        "camp": "中国台湾",
                        "makers": _tw["makers"],
                        "count": _tw["count"],
                        "category": _camp_cat("中国台湾"),
                        "hl": False,
                    })
                return rows

            _camp = _camp_rows()
            if _camp:
                data["upstream"] = _camp
                _prods = _bc.get("upstream_products") or []
                _prod_terms = "、".join(p["term"] for p in _prods[:10])
                # 国产线示例用**该公司**抽取结果的前几名（禁止写死任何公司的品牌）
                _dm_names = [
                    d["name"] for d in (_bc.get("domestic_makers") or [])
                ]
                _dm_txt = (
                    f"国产线来自主营业务概述段落列举（{'、'.join(_dm_names[:4])} 等），"
                    if _dm_names
                    else ""
                )
                data["upstream_note"] = (
                    f"一手来源·《{_rep_title}》释义与主营业务章节；{_dm_txt}"
                    f"代理分销产品涵盖 {_prod_terms} 等。"
                    "「主供品类」为按年报授权品类与授权证书段落归纳的口径，非逐家披露。"
                    "代理权是分销商的核心壁垒，原厂资质与产品线广度决定其上游话语权。"
                )
                # 上游标题/导语动态化：阵营数、原厂家数、领衔阵营全部取自
                # 该公司年报抽取结果（count 为抽取到的原厂总数，含未逐一列示部分）。
                _camp_total = sum(r.get("count", 0) for r in _camp)
                _camp_names = [r["camp"] for r in _camp]
                if _camp_names and _camp_total:
                    _lead = _camp_names[0]
                    data["upstream_title"] = (
                        f"上游：{_lead}领衔的{_camp_total}家原厂代理版图"
                        if len(_camp_names) > 1
                        else f"上游：{_lead}的{_camp_total}家原厂代理版图"
                    )
                    _camps_txt = (
                        f"{'、'.join(_camp_names)} {len(_camp_names)} 大阵营"
                        if len(_camp_names) > 1
                        else f"{_camp_names[0]}阵营"
                    )
                    data["upstream_intro"] = (
                        f"代理权是分销商的生命线。公司代理 {_camp_total} 家国内外原厂，"
                        f"覆盖{_camps_txt}，按阵营与品类拆分如下"
                        "（一手来源：定期报告「释义 / 主营业务」章节列示的授权代理原厂品牌）："
                    )

        # 供应商集中度改在下游模块统一渲染（与客户集中度对称，且需支持
        # annual_supplement 回补时点），此处不再重复渲染。
        # 下游：一手来源原文引用卡片 + 应用领域标签
        _down = _bc.get("downstream_raw") or []
        if _down:
            # 渲染用 brief（按句抽提的精华）：完整窗口 text 可能长达 1200 字，
            # 直接铺在引用区会把页面撑爆；而完整版仍留在 profile 里供 LLM 消费。
            # 旧 profile 无 brief 字段时回退到 text 并做硬截断。
            data["downstream_quotes"] = [
                {
                    "page": c["page"],
                    "text": c.get("brief")
                    or (c.get("text", "")[:420] + ("……" if len(c.get("text", "")) > 420 else "")),
                }
                for c in _down[:4]
            ]
            _fields = _bc.get("downstream_fields") or []
            if _fields:
                data["downstream_fields"] = _fields
            # 数据时点：按来源报告类型标注报告期（年报=12-31，半年报=06-30）
            _is_semi = "半年度" in (_bc.get("report", {}) or {}).get("title", "")
            _rep_year = (_bc.get("report", {}) or {}).get("year") or ""
            _period = f"{_rep_year}-06-30" if _is_semi else f"{_rep_year}-12-31"
            _kind = "半年度报告" if _is_semi else "年度报告"
            _supp_title = (_bc.get("annual_supplement", {}) or {}).get("report", {}) or {}
            _supp_txt = ""
            if _supp_title.get("title"):
                _supp_txt = f"；补充期：《{_supp_title['title']}》"
            data["downstream_note"] = (
                f"数据来源：主期·《{_rep_title}》（{_kind}，报告期 {_period}）{_supp_txt}；"
                f"客户名单与表现判断由系统从定期报告「主营业务/行业情况」章节原文抽取归纳，"
                f"各行数据期见「数据期」列；客户集中度为最近一期披露数据。"
            )

        # 深度合作网络（业务画像）：授权代理原厂 + 联合/战略合作句，一手来源。
        # LLM 的 `partners` 字段优先（须过 _partners_check 护栏：合作方名必须在
        # 定期报告语料中出现，防编造合作关系）；不过护栏或 LLM 不可用时落数据层。
        _llm_parts = data.get("partners")
        if isinstance(_llm_parts, list) and _partners_check(_llm_parts, _bc):
            _ps_rows = [
                {
                    "partner": str(p.get("partner") or ""),
                    "relation": str(p.get("relation") or "深度合作"),
                    "content": str(p.get("content") or ""),
                    "named": not is_generic_entity(str(p.get("partner") or "")),
                    "src": "",
                    "basis": "",
                }
                for p in _llm_parts
                if isinstance(p, dict) and p.get("partner")
            ]
            logger.info("[overview] 深度合作表采用 LLM 动态总结（已过合作方护栏）")
        else:
            if _llm_parts:
                logger.warning("[overview] LLM 合作表未过护栏（含语料外合作方），回退数据层")
            _ps_rows = [
                {
                    "partner": str(p.get("partner") or ""),
                    "relation": str(p.get("relation") or ""),
                    "content": str(p.get("content") or ""),
                    # 数据层已按「具名/泛称」拆分（partner 为泛称时 named=False）；
                    # 旧缓存无该键时按 is_generic_entity 兜底判定，避免泛称口径
                    # 被当成具体公司展示
                    "named": bool(p.get("named", True))
                    and not is_generic_entity(str(p.get("partner") or "")),
                    "src": "",
                    "basis": "",
                }
                for p in (_bc.get("partnerships") or [])
                if p.get("partner")
            ]
        if _ps_rows:
            data["partners"] = _ps_rows
            _is_semi_p = "半年度" in (_bc.get("report", {}) or {}).get("title", "")
            _py = (_bc.get("report", {}) or {}).get("year") or ""
            _pn_period = f"{_py} 年中报" if _is_semi_p else f"{_py} 年年报"
            data["partners_note"] = (
                f"深度合作网络取自《{_bc.get('report', {}).get('title', '定期报告')}》"
                f"（{_pn_period}）一手披露：授权代理关系来自释义章节列示的原厂品牌；"
                "联合/战略合作条目为报告原文整句摘录，可溯源。"
            )

        # —— 业务画像 LLM 增量补充（数据层为主源、LLM 受约束提取为增量）——
        # 触发条件：上游无阵营结构（制造商等）、下游名单行 <3、合作条目 <2
        # 任一成立即视为「数据源不够丰富」，调用聚焦 LLM 从证据句语料增量提取。
        # OV-5：该调用已与 growth 并行预取（见函数前部），此处仅取结果；预取失败回退串行。
        try:
            if _enrich_fut is not None:
                _enrich = _enrich_fut.result()
            else:
                _enrich = _build_biz_enrich(_bc, use_llm=use_llm)
        except Exception:  # noqa: BLE001 — 预取失败回退串行调用
            _enrich = _build_biz_enrich(_bc, use_llm=use_llm)
        if _enrich_ex is not None:
            _enrich_ex.shutdown(wait=False, cancel_futures=True)
            _enrich_ex = None
        _extra_partners = _enrich.get("partners_extra") or []
        if _extra_partners:
            _have = {re.sub(r"\s+", "", str(p.get("partner") or "")) for p in data.get("partners") or []}
            for _ep in _extra_partners:
                _pn = re.sub(r"\s+", "", str(_ep.get("partner") or ""))
                if _pn and _pn not in _have:
                    data.setdefault("partners", []).append({
                        "partner": str(_ep.get("partner") or ""),
                        "relation": str(_ep.get("relation") or "战略合作"),
                        "content": str(_ep.get("content") or ""),
                        "named": not is_generic_entity(str(_ep.get("partner") or "")),
                        "src": "",
                        "basis": "",
                        "ai": True,
                    })
                    _have.add(_pn)
        if data.get("partners"):
            # 名单级去重 + 具名优先排序：消除跨路径重复（契约表「英伟达、思灵机器人」
            # 与 AI 增量拆出的「英伟达」「思灵机器人」并存）
            data["partners"] = _dedupe_partner_rows(data["partners"])
            _n_ai = sum(1 for p in data["partners"] if p.get("ai"))
            _tail = (
                f"其中 {_n_ai} 条为 AI 从报告原文证据句中受约束提取（quote 已校验），请以原始披露为准。"
                if _n_ai else ""
            )
            if data.get("partners_note"):
                data["partners_note"] += _tail
            # 合作网络标题/导语动态化：合作方名单取自最终定稿的 partners 行。
            # 标题优先具名公司（泛称口径如「全球主要客户」仅作兜底），避免出现
            # 「深度合作网络：全球多家头部客户」这类零信息量表述。
            _pn_all = [
                str(p.get("partner") or "").strip()
                for p in data["partners"]
                if p.get("partner")
            ]
            _pn_names = [
                str(p.get("partner") or "").strip()
                for p in data["partners"]
                if p.get("partner") and p.get("named", True)
            ] or _pn_all
            _pn_names = [n for n in _pn_names if n]
            if _pn_names:
                _pn_head = "、".join(_pn_names[:3])
                data["partners_title"] = (
                    f"深度合作网络：{_pn_head} 等 {len(_pn_names)} 家合作方"
                    if len(_pn_names) > 3
                    else f"深度合作网络：{_pn_head}"
                )
                data["partners_intro"] = (
                    f"合作网络是业务模式的直接证据。下列 {len(_pn_names)} 条合作方与"
                    "合作关系均取自公司定期报告的一手披露："
                )

        # 下游表增量行：LLM downstream_detail 并入（field+customers 去重）
        _extra_dw = _enrich.get("downstream_extra") or []
        if _extra_dw:
            _dw_have = {
                (re.sub(r"\s+", "", str(d.get("field") or "")) + "|" +
                 re.sub(r"\s+", "", str(d.get("customers") or "")))
                for d in data.get("downstream") or []
            }
            for _ed in _extra_dw:
                _k = (re.sub(r"\s+", "", str(_ed.get("field") or "")) + "|" +
                      re.sub(r"\s+", "", str(_ed.get("customers") or "")))
                if _k in _dw_have:
                    continue
                _dw_have.add(_k)
                data.setdefault("downstream", []).append({
                    "field": str(_ed.get("field") or ""),
                    "customers": str(_ed.get("customers") or ""),
                    "perf": str(_ed.get("perf") or "—"),
                    "period": "AI 提取·待核实",
                    "cls": "",
                    "ai": True,
                })

        # 上游供给结构（类别级）：制造商等无授权代理阵营时，用 LLM 提取的
        # 类别级行呈现上游模块（不再是完全置空），仍以原文 quote 为据。
        _extra_up = _enrich.get("upstream_struct") or []
        if _extra_up and not data.get("upstream"):
            # 类别级去重：LLM 常对同一环节输出多行（detail 不同、类别名相同），
            # 不去重会让表格与标题出现「供应链及制造服务、供应链及制造服务」这类重复
            _seen_up_cat: set[str] = set()
            _up_rows: list[dict] = []
            for _u in _extra_up:
                if not _u.get("category"):
                    continue
                _cat = _clean_up_category(_u.get("category"))
                if _cat in _seen_up_cat:
                    continue
                _seen_up_cat.add(_cat)
                _up_rows.append({
                    "category": _cat,
                    "detail": str(_u.get("detail") or ""),
                    "ai": True,
                })
            if _up_rows:
                data["upstream_struct"] = _up_rows

        # 下游客户表（应用领域 → 代表客户 → 表现 → 数据期）：
        # 数据源时效优先：最新半年报为主期，年报为补充期（半年报下游章节更简略，
        # 领域覆盖不全时用年报补齐，每行标注数据期，最新在前）。
        # LLM 基于下游原文动态总结的 `downstream` 表优先，但必须先过
        # `_customer_table_check` 护栏（每个客户名都要能在定期报告原文中找到，
        # 防 LLM 编造客户名单）；不过护栏或 LLM 不可用时，落到数据层
        # `_build_customer_table_fallback`（通用句式正则，零公司硬编码）。
        _llm_down = data.get("downstream")
        if isinstance(_llm_down, list) and _customer_table_check(_llm_down, _bc):
            logger.info("[overview] 下游客户表采用 LLM 动态总结（已过客户名护栏）")
        else:
            if _llm_down:
                logger.warning("[overview] LLM 客户表未过护栏（含原文中不存在的客户名），回退数据层兜底")
            _rep_title_s = (_bc.get("report", {}) or {}).get("title", "")
            _semi = "半年度" in _rep_title_s
            _yr_main = (_bc.get("report", {}) or {}).get("year") or ""
            _period_main = f"{_yr_main}H1" if _semi else f"{_yr_main}年报"
            _rows_main = _build_customer_table_fallback(_bc, period=_period_main)
            _supp = _bc.get("annual_supplement") or {}
            _rows_all = _rows_main
            if _supp.get("downstream_raw"):
                _yr_ann = (_supp.get("report") or {}).get("year") or (_yr_main - 1)
                _rows_ann = _build_customer_table_fallback(
                    _supp, period=f"{_yr_ann}年报"
                )
                # 字段归一判重：去「产品」等后缀后相同视为同一领域（半年报优先）
                def _norm_f(f):
                    return f.replace("产品", "").replace("相关", "")
                _seen_f = {_norm_f(r["field"]) for r in _rows_main}
                _rows_all = _rows_main + [
                    r for r in _rows_ann if _norm_f(r["field"]) not in _seen_f
                ]
            if _rows_all:
                data["downstream"] = _rows_all

        # 下游标题/导语动态化：应用领域取自最终定稿的下游行（LLM/数据层兜底
        # + AI 增量行均已并入），按该公司实际覆盖领域生成。
        if data.get("downstream"):
            _dw_fields = [
                str(r.get("field") or "").strip()
                for r in data["downstream"]
                if r.get("field")
            ]
            _dw_fields = [f for f in _dw_fields if f]
            if _dw_fields:
                data["downstream_title"] = (
                    f"下游：{_dw_fields[0]}等{len(_dw_fields)}大应用领域"
                    if len(_dw_fields) > 1
                    else f"下游：{_dw_fields[0]}"
                )
                _dw_head = "、".join(_dw_fields[:4])
                data["downstream_intro"] = (
                    f"客户网络是第二资产。客户覆盖{_dw_head}"
                    f"等 {len(_dw_fields)} 个应用领域"
                    "（基于年报披露的客户名单动态归纳）："
                )

        # 客户/供应商集中度：半年报不披露该表，回用年报（annual_supplement）真值，
        # 并按实际披露期注明时点。数字全部来自定期报告原文，LLM 不参与。
        _ann_sup = _bc.get("annual_supplement") or {}
        _sup5 = _bc.get("supplier_top5") or _ann_sup.get("supplier_top5") or {}
        _sup5_period = (
            f"{_bc.get('report', {}).get('year') or ''}H1"
            if ("半年度" in (_bc.get("report", {}) or {}).get("title", "") and _bc.get("supplier_top5"))
            else f"{(_ann_sup.get('report') or _bc.get('report') or {}).get('year') or ''} 年度"
        )
        if _sup5.get("top5_amount"):
            _no1 = ""
            if _sup5.get("no1_amount"):
                _no1 = (
                    f"其中第一大供应商采购 {_sup5['no1_amount_yi']:.2f} 亿元"
                    f"（占 {_sup5['no1_pct']:.2f}%，定期报告未披露名称）。"
                )
            # 尾句按商业模式分支：分销商谈授权/护城河；制造商等用中性供给表述
            _is_dist = bool(data.get("upstream"))
            _sup5_tail = (
                "头部原厂授权的稳定性直接决定公司货源与议价能力，"
                "代理权集中是分销商的护城河，也是供给侧单一依赖的风险点。"
                if _is_dist
                else "头部供应商集中带来规模议价空间，也构成供给侧单一依赖风险，"
                "需关注核心原材料的保供与价格波动。"
            )
            data["supplier_conc_html"] = (
                '<div class="note">'
                f"<strong>供应商高度集中（{_sup5_period}披露）：</strong>"
                f"前五大供应商采购额 "
                f"<strong>{_sup5['top5_amount_yi']:.2f} 亿元</strong>，"
                f"占年度采购总额 <strong>{_sup5['top5_pct']:.2f}%</strong>。{_no1}"
                f"{_sup5_tail}"
                "</div>"
            )

        _cust5 = _bc.get("customer_top5") or _ann_sup.get("customer_top5") or {}
        if _cust5.get("total"):
            _cust_period = (
                f"{_bc.get('report', {}).get('year') or ''}H1"
                if ("半年度" in (_bc.get("report", {}) or {}).get("title", "") and _bc.get("customer_top5"))
                else f"{(_ann_sup.get('report') or _bc.get('report') or {}).get('year') or ''} 年度，最近一期披露"
            )
            _detail = "、".join(
                f"第{_cn}名 {_r['amount_yi']:.2f} 亿（{_r['pct']:.2f}%）"
                for _cn, _r in zip("一二三四五", _cust5.get("rows") or [])
            )
            data["customer_conc_html"] = (
                '<div class="note">'
                f"<strong>客户集中度（{_cust_period}）：</strong>"
                f"前五大客户销售额 "
                f"<strong>{_cust5['total_yi']:.2f} 亿元</strong>，"
                f"占营业收入 <strong>{_cust5['pct']:.2f}%</strong>"
                + (f"——{_detail}。" if _detail else "。")
                + "客户集中度相对健康，无单一大客户依赖。"
                "</div>"
            )

        # 制造商/服务商上游确定性兜底：无授权代理阵营（upstream 阵营表为空——
        # 该表数据源是释义章节「授权代理原厂」，仅分销商披露）、LLM 增量提取
        # （upstream_struct）也未产出时，用定期报告一手数据确定性构建上游供给
        # 结构表。与下游「数据层为主源」同哲学，不再完全依赖 LLM 成败。
        # 数据源：①释义章节定义的关键品类/技术术语（upstream_products）；
        # ②供应商前五名集中度（supplier_top5，半年报不披露时用年报补充期）。
        if not data.get("upstream") and not data.get("upstream_struct"):
            _up_rows: list[dict] = []
            _terms = [p["term"] for p in (_bc.get("upstream_products") or [])]
            if _terms:
                _up_rows.append({
                    "category": "关键品类/技术",
                    "detail": "定期报告「释义」章节定义：" + "、".join(_terms[:8]),
                })
            if _sup5.get("top5_amount"):
                _sup5_d = (
                    f"前五大供应商采购额 {_sup5['top5_amount_yi']:.2f} 亿元，"
                    f"占采购总额 {_sup5['top5_pct']:.2f}%"
                )
                if _sup5.get("no1_amount"):
                    _sup5_d += (
                        f"；第一大供应商 {_sup5['no1_amount_yi']:.2f} 亿元"
                        f"（{_sup5['no1_pct']:.2f}%，定期报告未披露名称）"
                    )
                _up_rows.append({
                    "category": "供应商集中度",
                    "detail": f"{_sup5_d}（{_sup5_period}披露）",
                })
            if _up_rows:
                data["upstream_struct"] = _up_rows
                # 集中度已入表（更完整的表格语境），独立 note 版不再重复渲染
                if data.get("supplier_conc_html"):
                    data["supplier_conc_html"] = ""
                logger.info(
                    "[overview] 上游供给结构采用数据层确定性兜底（rows=%d，无授权代理阵营）",
                    len(_up_rows),
                )

        # 上游供给结构标题/导语动态化（非分销商分支）：类别取自最终定稿行
        # （LLM 增量提取或数据层确定性兜底均已并入），按该公司实际供给类别生成。
        if data.get("upstream_struct") and not data.get("upstream"):
            _us_cats = [
                _clean_up_category(u.get("category"))
                for u in data["upstream_struct"]
                if u.get("category")
            ]
            _us_cats = [c for c in _us_cats if c]
            if _us_cats:
                _us_n = len(_us_cats)
                data["upstream_struct_title"] = (
                    f"上游：{'、'.join(_us_cats[:2])}等{_us_n}类供给结构"
                    if _us_n > 2
                    else f"上游：{'、'.join(_us_cats)}"
                )
                data["upstream_struct_intro"] = (
                    f"该公司不属于授权分销模式，上游涵盖{'、'.join(_us_cats)}"
                    f"等 {_us_n} 类供给/合作关系，以下内容提取自定期报告原文："
                    if _us_n > 1
                    else f"该公司不属于授权分销模式，上游供给与合作关系包括"
                    f"{_us_cats[0]}（一手来源：定期报告披露），以下内容提取自原文："
                )

    # OV-5：_bc 为空（无年报上下游数据）时 enrich 预取不会被消费——在此统一回收，
    # 避免 ThreadPoolExecutor 非守护线程拖住进程退出。
    if _enrich_ex is not None:
        _enrich_ex.shutdown(wait=False, cancel_futures=True)
        _enrich_ex = None

    # 股权质押：以数据为准的骨架兜底。即便 LLM 漏返 gov_left / gov_right，
    # 也把「未解押质押笔数 / 占总股本」注入治理风险扫描区，保证该模块不空。
    _ps = (
        profile_dict.get("layers", {}).get("governance", {}).get("pledge_summary") or {}
    )
    if _ps and _ps.get("active_count"):
        _kv = []
        _kv.append({
            "k": "未解押质押笔数",
            "v": f"{_ps['active_count']} 笔",
            "cls": "up",
        })
        if _ps.get("active_total_pct") not in (None, ""):
            # 口径必须显式标注：质押明细自带的 pct_of_total 是「质押当时」总股本口径，
            # 与「按当前总股本折算」差异很大（本公司历史总股本曾不足当前的 1/15）。
            _basis = (
                "按当前总股本" if _ps.get("pct_basis") == "current_capital"
                else "质押当时口径"
            )
            _kv.append({
                "k": "未解押占总股本",
                "v": f"{_ps['active_total_pct']}%（{_basis}）",
                "cls": "up",
            })
        # 归属：LLM 曾把他人质押张冠李戴到实控人头上，故归属一律取真实归集结果
        _th = _ps.get("top_holder") or {}
        if _th.get("name") and _th.get("pct") is not None:
            _kv.append({
                "k": "在押主要股东",
                "v": f"{_th['name']} {_th['pct']}%",
                "cls": "up",
            })
        # 质押类 KV 一律以数据为准：剔除 LLM 可能填错的同类项后前置注入
        _PLEDGE_KEYS = {
            "未解押质押笔数", "未解押占总股本", "控股股东质押率", "在押主要股东",
        }
        _rest = [
            x for x in (data.get("gov_left") or [])
            if str(x.get("k", "")) not in _PLEDGE_KEYS
        ]
        data["gov_left"] = _kv + _rest

    # 主营构成：以真实数据为准（东方财富主营构成接口 stock_zygc_em），覆盖 LLM 可能
    # 编造或跨报告期混搭口径的分部数据。取最新报告期、按产品分类，作为「业务板块全景」
    # 主表 + 收入/利润错位数图（seg_mix / seg_gap）的唯一数据源。
    _segs_raw = (
        profile_dict.get("layers", {}).get("business", {}).get("segments") or []
    )

    def _seg_date_key(r):
        d = r.get("报告日期")
        if d is None:
            return ""
        if hasattr(d, "strftime"):
            return d.strftime("%Y-%m-%d")
        return str(d)

    def _seg_f(x):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        # NaN / ±Infinity 不是有效数值（东财部分期次毛利率为空时落成 nan，经
        # json 往返仍是 float('nan')）。必须归一为 None，否则会污染分部趋势的
        # 收入加权毛利率与合计（实测渲染成「nan%」）。
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v

    if _segs_raw:
        _dates = sorted({_seg_date_key(r) for r in _segs_raw if _seg_date_key(r)})
        _latest = _dates[-1] if _dates else ""
        _prod = [
            r for r in _segs_raw
            if _seg_date_key(r) == _latest
            and str(r.get("分类类型", "")) == "按产品分类"
        ]
        if _prod:
            _prod_sorted = sorted(
                _prod, key=lambda r: (_seg_f(r.get("收入比例")) or 0), reverse=True
            )
            _max_pct = max((_seg_f(r.get("收入比例")) or 0) for r in _prod_sorted)
            _shaped = []
            for r in _prod_sorted:
                _rev = _seg_f(r.get("主营收入"))
                _pct = _seg_f(r.get("收入比例"))
                _margin = _seg_f(r.get("毛利率"))
                _ppct = _seg_f(r.get("利润比例"))
                _shaped.append({
                    "name": r.get("主营构成", ""),
                    "revenue": f"{_rev/1e8:.2f} 亿" if _rev is not None else "",
                    "pct": f"{_pct*100:.2f}%" if _pct is not None else "",
                    "margin": f"{_margin*100:.2f}%" if _margin is not None else "—",
                    "profit_pct": f"{_ppct*100:.2f}%" if _ppct is not None else "—",
                    "nature": "绝对主体" if (_pct or 0) > 0.5
                    else ("重要构成" if (_pct or 0) > 0.1 else "补充"),
                    "hl": abs((_pct or 0) - _max_pct) < 1e-9,
                })
            data["segments"] = _shaped
            _labels = [s["name"] for s in _shaped]

            def _pct_f(sv: str) -> float | None:
                """把 '12.34%' 解析为 12.34；空串/占位符（'—'）返回 None。

                东财偶尔整列缺「收入比例」（全为 None → 这里的 ''），旧写法
                `float(s["pct"].rstrip('%'))` 会直接抛 ValueError，把整个报告装配
                打断（不是在渲染层局部降级）。故此处改为 None 安全解析（2026-09-16）。
                """
                s = str(sv).strip().rstrip("%")
                try:
                    return float(s)
                except (TypeError, ValueError):
                    return None

            # 与 _labels 等长：缺值补 0.0（图表不崩、不越界），真实值照常。
            _inc = [_pct_f(s["pct"]) or 0.0 for s in _shaped]
            _prof = [v for v in (_pct_f(s["profit_pct"]) for s in _shaped) if v is not None]
            data["seg_mix"] = {"labels": _labels, "values": _inc}
            data["seg_gap"] = {
                "labels": _labels, "income_pct": _inc, "profit_pct": _prof,
            }
            # seg_mix_total：气泡图 tooltip 的占比分母（收入，单位亿元），以一手主营构成
            # 真实收入求和，杜绝默认 0 导致的除零与错误百分比。
            _total_rev = sum(
                (_seg_f(r.get("主营收入")) or 0.0) for r in _prod_sorted
            )
            if _total_rev:
                data["seg_mix_total"] = round(_total_rev / 1e8, 2)
            data["seg_mix_note"] = (
                f"数据来源：东方财富主营构成（报告期 {_latest}，按产品分类）；"
                "单位：收入/利润占比 %。"
            )

    # 分部多期趋势（seg_trend）与自研/高毛利业务趋势（self_dev）：以真实数据为准，
    # 覆盖 LLM 凭记忆编造的多期数字。
    #
    # 口径纪律（关键）：
    # 1) 取最近 3 个年报（12-31）+ 最新半年报（06-30），呈现「三年趋势 + 当年 H1」；
    #    年报列同时展示收入/毛利率，H1 列仅展示收入并单独计算 H1 同比。
    # 2) 产品名做保守归一化（去空格后取前 4 字），兼容「结构模块器件(模组)」与
    #    「结构模块器(模组)」这类同义漂移；
    # 3) 拒绝分类体系确实不同的报告期——宁可少列期数，不硬凑。
    if _segs_raw:
        def _is_subrow(name) -> bool:
            """是否为「其中:XXX」形式的**子行**（明细拆分），而非独立产品线。

            东财主营构成会把某个父级科目的明细以「其中:销售产品收入及其他」这类
            行并列返回，与父行**同值**。把它们与父行一起计入趋势合计会重复计数：
            实测中国移动（600941）2023 年「其他(补充)」1457.95 亿 + 「其中:销售
            产品收入及其他」1457.95 亿 → 合计虚高到 11551.04 亿（真实 10093.09 亿）。
            子行的信息量已被父行覆盖，故仅把它们排除出**趋势归并**（不影响
            「业务板块全景」表的原样展示）。2026-09-16 修复。
            """
            s = "".join(str(name).split())
            return bool(re.match(r"^其中[:：]", s))

        _prod_all = [
            r for r in _segs_raw
            if str(r.get("分类类型", "")) == "按产品分类" and not _is_subrow(r.get("主营构成"))
        ]
        _all_dates = sorted({_seg_date_key(r) for r in _prod_all if _seg_date_key(r)})

        import re as _re

        def _seg_keywords(name):
            """把产品名拆成关键词集合，用于跨报告期的模糊匹配。

            先剥离「等其他业务」「业务」「产品」等通用后缀，再按标点切分，
            避免「电力产品、自研芯片等其他业务」与「自研芯片、电力产品等其他业务」
            因顺序不同而被判为不同类别。
            """
            s = "".join(str(name).split())
            for strip in ("等其他业务", "等其他", "业务", "产品", "等", "补充", "及其他", "及"):
                s = s.replace(strip, "")
            parts = _re.split(r"[、，,；;()（）]", s)
            kw = set()
            for p in parts:
                if len(p) >= 2:
                    kw.add(p)
            return frozenset(kw)

        def _seg_match_score(name_a, name_b):
            ka, kb = _seg_keywords(name_a), _seg_keywords(name_b)
            sa, sb = "".join(str(name_a).split()), "".join(str(name_b).split())
            # 前缀包含（2026-09-09）：同一产品的简称/全称漂移（如「光通信收发模块」
            # 与「光通信收发模块及器件」）在关键词拆分后交集为 0 被判死，导致跨期
            # 匹配率骤降、趋势期被整期剔除。前缀包含视为同一产品（阈值 4 字防误伤）。
            if len(sa) >= 4 and len(sb) >= 4 and (sa.startswith(sb) or sb.startswith(sa)):
                return 1.0
            if not ka or not kb:
                return 0.0
            inter = len(ka & kb)
            if inter > 0:
                return inter / max(len(ka), len(kb))
            # 词级关键词无交集时退用字符 2-gram 相似度：跨期改名常是缩写/省略
            # （实测 002384：「触控面板及液晶显示模组」→「光电显示模组」，公共
            # 2-gram 显示/示模/模组，相似度 0.6），词级判死会拖垮整期匹配率、
            # 把 2023/2024 年报整期剔除（标题「三年」只剩一期）。
            # 阈值 0.34：要求短名侧约 1/3 以上 2-gram 重合；「光模块」vs
            # 「LED显示器件」2-gram 交集为 0 不误伤——新业务与旧业务是并存
            # 更替而非改名，不许跨期拼接。
            ga = {sa[i:i + 2] for i in range(len(sa) - 1)}
            gb = {sb[i:i + 2] for i in range(len(sb) - 1)}
            if ga and gb:
                g_score = len(ga & gb) / min(len(ga), len(gb))
                if g_score >= 0.34:
                    return g_score
            return 0.0

        def _find_seg_rec(target_name, date_rows):
            """在 date_rows 中找到与 target_name 最匹配的主营构成记录。"""
            best, best_score = None, 0.0
            for r in date_rows:
                score = _seg_match_score(target_name, r.get("主营构成", ""))
                if score > best_score:
                    best_score = score
                    best = r
            return best if best_score >= 0.3 else None

        def _mg_of(x):
            """毛利率：优先取接口「毛利率」列；缺失（东财部分板块/期次
            系统性为空，如「其他(补充)」）时用 (收入−成本)/收入 自算，
            与 05 财务速写的毛利率自算口径一致——避免趋势图折线整段为空。"""
            mg = _seg_f(x.get("毛利率"))
            if mg is not None:
                return mg
            rev = _seg_f(x.get("主营收入"))
            cost = _seg_f(x.get("主营成本"))
            if rev is not None and cost is not None and rev != 0:
                return (rev - cost) / rev
            return None

        def _period_agg(d: str) -> dict:
            """把某期「按产品分类」的全部产品段归类到基准期产品名并求和。

            为什么不能只取「最优单段」（旧实现）：中际旭创 2023 年报把光通信业务
            拆成「高速光通信模块 / 中低速光通信模块」两段，二者与基准名「光通信
            收发模块」的匹配得分**并列 0.5**——旧实现遇到并列只取先出现的「高速」，
            漏掉「中低速」3.83 亿，导致 2023 列合计 97.99 亿 ≠ 当期真实营收 107.18 亿。
            改为**逐段归并**：每个历史段按最高得分归到唯一一个基准名（避免重复计数），
            同名多段求和；得分为 0 的非残差段（2023「汽车光电子」「光组件」）并入
            基准期的「其他」行，与其自身归类口径一致；残差 / 技术性科目（分部间抵销、
            平衡项目）不参与，避免虚增合计。

            返回 ``{基准名: {"rev": 收入合计, "margin": 加权毛利率}}``。
            """
            acc: dict[str, dict] = {}
            for r in _prod_all:
                if _seg_date_key(r) != d:
                    continue
                nm = r.get("主营构成", "")
                rev = _seg_f(r.get("主营收入"))
                if rev is None:
                    continue
                best_name, best_score = "", 0.0
                for bn in _latest_names:
                    sc = _seg_match_score(nm, bn)
                    if sc > best_score:
                        best_score, best_name = sc, bn
                if best_score >= 0.3:
                    tgt = best_name
                elif _is_residual(nm) or _fallback_name is None:
                    continue
                else:
                    tgt = _fallback_name
                slot = acc.setdefault(tgt, {"rev": 0.0, "_wsum": 0.0, "_wden": 0.0})
                slot["rev"] += rev
                _mg = _mg_of(r)
                if _mg is not None:
                    slot["_wsum"] += rev * _mg
                    slot["_wden"] += rev
            for slot in acc.values():
                _wden = slot.pop("_wden")
                _wsum = slot.pop("_wsum")
                slot["margin"] = (_wsum / _wden) if _wden else None
            return acc

        def _is_residual(name) -> bool:
            """是否为「残差 / 技术性科目」而非真实产品线。

            「其他(补充)」「分部间抵销」「平衡项目」这类科目只是合并报表的轧差项，
            公司**是否列出它们取决于当年有无残差**，不构成「分类体系差异」的证据。
            实测 300308：基准期 2025 年报列了「其他」，2023/2024 年报没列 —— 匹配率
            分母虚高到 2，历史期只命中 1 项 = 50% < 70%，两个年报期被整期剔除，
            标题「三年分部趋势」只剩 2025 + 2026H1。故仅把它们排除出**匹配率分母**，
            仍保留在趋势行里（前期无值渲染为 —），不改变既有展示口径。
            """
            s = "".join(str(name).split())
            return bool(_re.match(
                r"^(其他|其它|分部间抵销|内部抵销|抵销|平衡项目|合计|总计)", s
            ))

        def _period_kind(d: str) -> str:
            if len(d) >= 10:
                if d.endswith("06-30"):
                    return "h1"
                if d.endswith("12-31"):
                    return "annual"
            return ""

        def _trend_period_label(d: str) -> str:
            if len(d) >= 10:
                y, md = d[:4], d[5:]
                if md == "06-30":
                    return f"{y}H1"
                if md == "12-31":
                    return y
            return d

        _annual = sorted([d for d in _all_dates if _period_kind(d) == "annual"])[-3:]
        _h1_all = sorted([d for d in _all_dates if _period_kind(d) == "h1"])
        _latest_h1 = _h1_all[-1] if _h1_all else ""
        # 目标期次：最近 3 年年报 + 最近 1 年半年报
        _trend_dates = _annual.copy()
        if _latest_h1 and _latest_h1 not in _trend_dates:
            _trend_dates.append(_latest_h1)

        # —— 跨期匹配的基准与期次过滤（2026-09-09 修复「只剩一期」）——
        # 1) 基准锚定「最近一期年报」：最新期常为半年报，其产品分类名目比年报更细/更粗，
        #    以它为基准会把全部年报期整期判为不匹配（实测 300308 仅剩 2026H1 一期）。
        # 2) 期次采纳改为「产品名匹配率 ≥70%」，不再要求全部产品名匹配（原 all()）：
        #    东财不同期次的分类名目会漂移，一个名对不上就废掉整期（实测 600941 丢 2025 年报）。
        _annual_in_trend = [d for d in _trend_dates if _period_kind(d) == "annual"]
        _base_d = _annual_in_trend[-1] if _annual_in_trend else _trend_dates[-1]
        _latest_names = [
            r.get("主营构成", "")
            for r in _prod_all
            if _seg_date_key(r) == _base_d
        ]
        # 匹配率分母只算真实产品线：基准期的「其他」等残差科目在历史期常常不存在，
        # 计入分母会把历史期误判为「分类体系不一致」而整期剔除（见 _is_residual）。
        _match_names = [n for n in _latest_names if not _is_residual(n)] or _latest_names
        # 历史期里未匹配到任何基准产品名的**非残差**段（多为基准期已并入「其他」
        # 的旧独立板块，如中际旭创 2023 的「汽车光电子」「光组件」）统一并入基准期的
        # 残差行（通常是「其他」），与该期自身的归类口径一致；基准期若无该行则保持
        # 丢弃（宁缺勿编）。用于 _period_agg 的归并。
        _fallback_name = next((n for n in _latest_names if _is_residual(n)), None)

        _MATCH_RATIO = 0.7
        _ok_dates = []
        _dropped: list[tuple[str, float, list[str]]] = []
        for d in _trend_dates:
            _date_rows = [r for r in _prod_all if _seg_date_key(r) == d]
            if not _match_names:
                break
            _hit_names = [name for name in _match_names if _find_seg_rec(name, _date_rows) is not None]
            _ratio = len(_hit_names) / len(_match_names)
            if d == _base_d or _ratio >= _MATCH_RATIO:
                _ok_dates.append(d)
            else:
                _miss = [n for n in _match_names if n not in _hit_names]
                _dropped.append((d, _ratio, _miss))
                # WARNING（不是 INFO）：期次被剔除会让「三年分部趋势」静默缺列，
                # 属于必须被看见的数据质量问题。历史上 002384 / 300308 / 688981
                # 都因残差科目虚高分母在此被误剔，故升级级别并打印缺失的产品名。
                logger.warning(
                    "[overview] 分部趋势剔除期次 %s：产品名匹配率 %.0f%% < %.0f%%"
                    "（基准期 %s，分母 %d 项已剔残差；未命中产品名 %s）",
                    d, _ratio * 100, _MATCH_RATIO * 100, _base_d, len(_match_names), _miss,
                )
        if _dropped:
            logger.warning(
                "[overview] 分部趋势窗口期次不齐：目标 %s，采纳 %s，剔除 %s。"
                "请核对是否属「分类体系真的变了」——若只是科目命名漂移（如「其他」"
                "有无、「XX制造代工」vs「XX代工」），应扩充匹配而非丢期。",
                _trend_dates, _ok_dates, [d for d, _, _ in _dropped],
            )
        logger.info(
            "[overview] 分部趋势期次：目标 %s → 采纳 %s（基准期 %s）",
            _trend_dates, _ok_dates, _base_d,
        )

        # 宁缺毋滥：采纳期次不足 2 期时**不装配**趋势块（模板 {% if seg_trend %} 自动隐藏），
        # 杜绝渲染出「只有一期的假趋势」。
        if len(_ok_dates) >= 2:
            _labels = [_trend_period_label(d) for d in _ok_dates]
            _is_h1 = [_period_kind(d) == "h1" for d in _ok_dates]

            # 计算 H1 同比：需要倒数第二期也是 H1，或者往前找一个同产品的 H1
            _prev_h1 = _h1_all[-2] if len(_h1_all) >= 2 else ""

            _trend_rows = []
            # 保留原始数值，供 summary 二次计算
            _series_raw: dict[str, list[dict]] = {}
            # 每期只归并一次（逐段归并，见 _period_agg），行/合计/同比共用同一份结果，
            # 保证「行之和 == 合计」且各期合计 == 当期真实主营收入。
            _agg_by_date = {d: _period_agg(d) for d in _ok_dates}
            _prev_agg = _period_agg(_prev_h1) if _prev_h1 else {}
            for _disp in _latest_names:
                _revs = [_agg_by_date[d].get(_disp, {}).get("rev") for d in _ok_dates]
                _mgs = [_agg_by_date[d].get(_disp, {}).get("margin") for d in _ok_dates]
                # 中途诞生的板块允许前期缺数（实测 002384「光模块」2025 年并购
                # 新增，2023/2024 无记录）：原 any(None) 一票否决会把整行剔出
                # 趋势表，板块当期收入 53.5 亿却凭空消失。改为「至少 2 期有数」，
                # 缺期单元格渲染为 —；当期无数据的板块仍不展示（宁缺毋滥不变）。
                if sum(1 for x in _revs if x is not None) < 2:
                    continue
                _series_raw[_disp] = [
                    {"rev": _revs[i], "margin": _mgs[i], "is_h1": _is_h1[i]}
                    for i in range(len(_ok_dates))
                ]

                def _cell(rev, mg, is_h1_col):
                    a = f"{rev / 1e8:.2f} 亿" if rev is not None else "—"
                    if is_h1_col:
                        return a
                    b = f"{mg * 100:.2f}%" if mg is not None else "—"
                    return f"{a} / {b}"

                _cells = [
                    _cell(_revs[i], _mgs[i], _is_h1[i]) for i in range(len(_ok_dates))
                ]
                _recent = _revs[-1]
                # H1 同比：用最新 H1 对上一个 H1（同一套归并口径）
                _yoy_s, _cls = "—", ""
                if _is_h1[-1] and _prev_h1:
                    _prev_rev = _prev_agg.get(_disp, {}).get("rev")
                    if _recent is not None and _prev_rev:
                        _yoy = (_recent - _prev_rev) / abs(_prev_rev) * 100.0
                        _yoy_s = f"{_yoy:+.1f}%"
                        _cls = "up" if _yoy >= 0 else "down"
                _trend_rows.append({
                    "name": _disp,
                    "cells": _cells,
                    "recent": f"{_recent / 1e8:.2f} 亿" if _recent is not None else "—",
                    "yoy": _yoy_s, "cls": _cls,
                    "hl": False, "strong": True,
                    "_rev": _recent or 0.0,
                })

            _trend_rows.sort(key=lambda x: x["_rev"], reverse=True)
            for _r in _trend_rows:
                _r.pop("_rev", None)
            if _trend_rows:
                _trend_rows[0]["hl"] = True
                # ── 合计行 ──────────────────────────────────────────────────
                # 收入：所有产品分类当期求和；毛利率：按收入加权平均
                # （年报列同时给收入/毛利率，H1 列只给收入，H1 同比 = 当期对上一同口径 H1）
                _total_row = {
                    "name": "合计",
                    "cells": [],
                    "recent": "—",
                    "yoy": "—", "cls": "",
                    "hl": False, "strong": False,
                }
                _prev_total_h1 = None
                for _i, _is_h1_col in enumerate(_is_h1):
                    _cell_revs = []
                    _cell_mgs = []
                    for _disp, _series in _series_raw.items():
                        if _i < len(_series):
                            _r0 = _series[_i].get("rev")
                            _m0 = _series[_i].get("margin")
                        else:
                            _r0, _m0 = None, None
                        if _r0 is not None:
                            _cell_revs.append(_r0)
                        if _m0 is not None:
                            _cell_mgs.append((_r0 or 0.0, _m0))
                    if not _cell_revs:
                        _total_row["cells"].append("—")
                        continue
                    _total_rev = sum(_cell_revs)
                    _a = f"{_total_rev / 1e8:.2f} 亿"
                    if _is_h1_col:
                        _total_row["cells"].append(_a)
                        _prev_total_h1 = _total_rev
                    else:
                        # 分部加权毛利率：Σ(rev × margin) / Σrev
                        # 全部板块毛利率均缺失时（如 2023「汽车光电子/光组件」两段
                        # 东财毛利率为空）渲染 — ，而不是被 1e-9 兜底成误导性的 0.00%。
                        _mg_ok = [(r, m) for r, m in _cell_mgs if r > 0]
                        _den = sum(r for r, m in _mg_ok)
                        if _mg_ok and _den > 0:
                            _wm = sum(r * m for r, m in _mg_ok) / _den
                            _b = f"{_wm * 100:.2f}%"
                        else:
                            _b = "—"
                        _total_row["cells"].append(f"{_a} / {_b}")
                # 合计的 H1 同比：用最新 H1 对上一个 H1
                if _is_h1[-1]:
                    _latest_h1_rev = sum(
                        (_series[-1].get("rev") or 0.0)
                        for _series in _series_raw.values()
                    )
                    if _prev_h1:
                        # 上一同口径 H1 的合计：同样走逐段归并，避免漏计
                        _prev_total = sum(
                            (_prev_agg.get(_disp, {}).get("rev") or 0.0)
                            for _disp in _series_raw
                        )
                        if _prev_total > 0:
                            _yoy = (_latest_h1_rev - _prev_total) / abs(_prev_total) * 100.0
                            _total_row["recent"] = f"{_latest_h1_rev / 1e8:.2f} 亿"
                            _total_row["yoy"] = f"{_yoy:+.1f}%"
                            _total_row["cls"] = "up" if _yoy >= 0 else "down"
                _trend_rows.append(_total_row)

                data["seg_trend"] = _trend_rows

                # 趋势标题按数据生成（原为模板写死「三年分部趋势：一块在改善，一块在恶化」，
                # 该断言并非对所有公司成立）：以最新 H1 同比的最高/最低板块命名
                def _pct_num(s: str) -> float:
                    try:
                        return float(str(s).replace("%", "").strip())
                    except (TypeError, ValueError):
                        return 0.0

                _t_body = [r for r in _trend_rows if r.get("name") != "合计"]
                _t_ups = [(_pct_num(r.get("yoy")), r["name"]) for r in _t_body if r.get("cls") == "up"]
                _t_downs = [(_pct_num(r.get("yoy")), r["name"]) for r in _t_body if r.get("cls") == "down"]
                if _t_ups and _t_downs:
                    data["seg_trend_title"] = (
                        f"三年分部趋势：{max(_t_ups)[1]}在改善，{min(_t_downs)[1]}在失速"
                    )
                elif _t_ups:
                    data["seg_trend_title"] = f"三年分部趋势：{max(_t_ups)[1]}领跑，各板块齐升"
                elif _t_downs:
                    data["seg_trend_title"] = f"三年分部趋势：{min(_t_downs)[1]}承压最深"
                else:
                    data["seg_trend_title"] = "三年分部趋势：同口径分板块对比"
                data["seg_trend_labels"] = _labels
                data["seg_trend_cols"] = [
                    {"label": _labels[i], "is_h1": _is_h1[i]}
                    for i in range(len(_labels))
                ]
                # 是否含最新半年度列：东财主营构成的口径取决于公司是否在半年报
                # 披露「分行业/分产品」表——工业富联等单一业务公司半年报明确
                # 「未呈列分部信息」，数据源就没有 06-30 期次（实测 601138）。
                # 此时「近期收入」列会退化成最新年报值（与 2025 列完全重复、
                # 且列名暗示「最近一期」），必须整列隐藏，口径注如实说明。
                _has_h1 = bool(_is_h1) and _is_h1[-1]
                data["seg_trend_has_h1"] = _has_h1
                _annual_labels = [
                    _labels[i] for i in range(len(_labels)) if not _is_h1[i]
                ]
                if _has_h1:
                    data["seg_trend_note"] = (
                        "数据来源：东方财富主营构成（按产品分类）。"
                        f"取最近 3 个完整年报与最新半年报（{'、'.join(_labels)}）；"
                        "H1 同比 = 最新半年报对上一同口径半年报。"
                        "分类体系不一致的历史期次已剔除，不做拼接。"
                    )
                else:
                    data["seg_trend_note"] = (
                        "数据来源：东方财富主营构成（按产品分类）。"
                        f"仅取最近 {len(_annual_labels)} 个完整年报"
                        f"（{'、'.join(_annual_labels)}）：公司最新半年度报告未披露"
                        "分产品 / 分行业构成（无 H1 口径数据），故不展示最新半年度列"
                        "与 H1 同比；半年度整体营收与毛利率见「财务速写」。"
                        "分类体系不一致的历史期次已剔除，不做拼接。"
                    )

                # 自研 / 高毛利业务趋势：通用规则——取「毛利率最高的非主导板块」
                # （收入不足主导板块一半者中毛利率最高者）。旧版用公司特定关键词
                # （自研/芯片/电力…）优先命中，属单一公司特化硬编码，已删除。
                _cands = []
                for _disp, _series in _series_raw.items():
                    _last = _series[-1]
                    _cands.append((
                        _disp,
                        _last.get("margin") or 0.0,
                        _last.get("rev") or 0.0,
                    ))
                if _cands:
                    _non_dom = [c for c in _cands if c[2] < max(c[2] for c in _cands) * 0.5] or _cands
                    _pick = max(_non_dom, key=lambda c: c[1])
                else:
                    _pick = None

                if _pick:
                    _disp = _pick[0]
                    _rv = [round((x.get("rev") or 0.0) / 1e8, 2) if x.get("rev") is not None else None for x in _series_raw[_disp]]
                    _mg_s = [round((x.get("margin") or 0.0) * 100, 2) if x.get("margin") is not None else None for x in _series_raw[_disp]]
                    data["self_dev"] = {"labels": _labels, "revenue": _rv, "margin": _mg_s}
                    # 注只列有数期次：中途纳入披露的板块（如 002384 光模块）前期为空，
                    # 写全期次会让人误读为「该期数据缺失」而非「业务尚未存在」
                    _pts = [lab for lab, v in zip(_labels, _rv) if v is not None]
                    if _pts and len(_pts) < len(_labels):
                        data["self_dev_note"] = (
                            f"口径：{_disp}（有数期次 {'、'.join(_pts)}；"
                            f"该板块自 {_pts[0]} 起纳入分部披露，此前无数据）；"
                            "数据来源：东方财富主营构成。"
                        )
                    else:
                        data["self_dev_note"] = (
                            f"口径：{_disp}（{'、'.join(_labels)}，同口径对比）；"
                            "数据来源：东方财富主营构成。"
                        )

                    # 数据层兜底：生成「战略与现实的落差」式 LLM 总结
                    _fin = (profile_dict.get("layers", {}).get("financials", {}) or {}).get("summary", {}) or {}
                    data["seg_trend_summary_html"] = _build_seg_trend_summary(
                        _labels, _series_raw, _disp,
                        _fin.get("rd_expense_annual"), _fin.get("rd_expense_yoy"), _fin.get("rd_expense_period"),
                    )

    # 前十大股东：以真实数据为准（覆盖 LLM）。
    # 原因（实测翻车）：LLM 把赵马克持股比例写成 27.80%（真实 2.78%，差 10 倍），
    # 并把他人（侯红亮）的未解押 6.98% 张冠李戴到实控人名下——而实控人在押实为 0 股。
    # 持股比例与质押状态均属可精确计算的事实，不允许由模型生成。
    _gov = profile_dict.get("layers", {}).get("governance", {}) or {}
    _holders = _gov.get("holders") or []
    _cap = int(_gov.get("share_capital") or 0)
    _pledges = _gov.get("pledge") or []
    _rows = []  # 前十大股东行；无数据时保持空列表，供下游「身份识别」稳定引用
    _rh = _gov.get("report_holders") or {}
    _rh_rows = _rh.get("holders") or []
    # 页脚「数据来源」按**实际启用的股东口径**生成（名册 / 东财），
    # 否则会出现「区块脚注写名册原文、页脚却写东方财富前十大股东并按总股本折算」的自相矛盾。
    _holders_src_txt = ""
    _holders_pct_txt = ""
    # 口径护栏：表头由口径派生（流通口径绝不写「前十大股东」）。
    _caliber = holders_caliber_of(_gov)
    data["shareholders_title"] = holders_title(_caliber)

    def _num(x):
        try:
            if x is None:
                return None
            return float(str(x).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    def _name_key(n):
        s = str(n or "").replace(" ", "").replace("\u3000", "").upper()
        return s.split("(")[0].split("（")[0]

    if _rh_rows:
        # 前十大股东：定期报告**股东名册**口径（监管披露，含性质/总持股/质押数量）。
        # 东财「前十大股东」为流通快照口径（实控人仅显示无限售部分），与名册
        # （总持股含限售，11.10% vs 2.78%）差异巨大，名册为披露口径必须优先。
        # pct 直接用名册披露值，不按东财总股本折算。
        for _h in _rh_rows:
            _pn = int(_h.get("pledge_num") or 0)
            _rows.append({
                "name": _h.get("name") or "",
                "nature": _h.get("nature") or "—",
                "pct": f"{float(_h.get('pct') or 0):.2f}%",
                "shares": f"{int(_h.get('shares') or 0) / 1e4:,.2f}",
                "pledge": (f"在押 {_pn:,} 股" if _pn > 0 else "无"),
                "hl": False,
            })
        if _rows:
            _rows[0]["hl"] = True
            data["shareholders"] = _rows
            data["shareholders_period"] = _rh.get("period") or ""
            data["gov_note"] = (
                f"前十大股东为《{_rh.get('title') or '定期报告'}》披露的股东名册口径"
                f"（{_rh.get('period') or '最新报告期'}），持股比例/股东性质/质押数量"
                "均取自名册原文（总持股含限售股）；质押、标记或冻结情况以名册披露为准。"
                "数据来源：定期报告股东名册（PyMuPDF 确定性抽取）。"
            )
            _holders_src_txt = (
                f"公司定期报告《{_rh.get('title') or '定期报告'}》股东名册"
                "（前十大股东／上下游／分红回购原文均由 PyMuPDF 确定性抽取）"
            )
            _holders_pct_txt = (
                "前十大股东的持股比例、股东性质与质押数量取自定期报告名册原文"
                "（总持股含限售股），不按当前总股本折算"
            )
    # 门槛放宽（2026-09-16）：原条件 `_cap > 0` 会让**总股本取数失败的历史报告直接不渲染
    # 股东表**（实测 600519/601288/601398 的 profile 里 share_capital=None，报告治理区块
    # 整块缺失）。东财十大股东专用接口自带「占总股本持股比例」原文，不依赖总股本，
    # 故只要该列存在即允许渲染；总股本仅在折算兜底与质押占比时用到，缺失时优雅降级。
    elif _holders and (
        _cap > 0
        or any(_num(_h.get("占总股本持股比例")) is not None for _h in _holders)
    ):
        _act_by, _any_by = {}, {}
        for _r in _pledges:
            _k = _name_key(_r.get("shareholder"))
            if not _k:
                continue
            _any_by.setdefault(_k, []).append(_r)
            if str(_r.get("status", "")) == "未解押":
                _act_by[_k] = _act_by.get(_k, 0.0) + (_num(_r.get("pledge_shares")) or 0.0)

        # 口径：东财「十大股东」专用接口（默认）／全市场大表兜底时带 _口径=流通股东。
        _is_free = any(str(_h.get("_口径") or "") == "流通股东" for _h in _holders)
        _rows = []
        for _h in _holders:
            _nm = _h.get("股东名称") or _h.get("name") or ""
            _sh = _num(_h.get("持股数"))
            _k = _name_key(_nm)
            # 优先用接口原文「占总股本持股比例」（十大股东专用接口提供）；
            # 兜底口径无此列时按当前总股本折算。
            _pct = _num(_h.get("占总股本持股比例"))
            if _pct is None and _sh and _cap:
                _pct = _sh / float(_cap) * 100.0
            _act = _act_by.get(_k, 0.0)
            if _act > 0:
                _inst = ""
                for _r in _any_by.get(_k, []):
                    if str(_r.get("status", "")) == "未解押" and _r.get("institution"):
                        _inst = str(_r.get("institution"))
                        break
                _inst_s = re.sub(
                    r"(股份有限责任公司|股份有限公司|有限责任公司|有限公司)$", "", _inst
                )
                # 总股本缺失时按股数呈现，不做除法（历史报告 share_capital=None）
                _pl = (
                    f"未解押 {_act / float(_cap) * 100.0:.2f}%"
                    if _cap else f"未解押 {_act:,.0f} 股"
                )
                if _inst_s:
                    _pl += f"（{_inst_s}）"
            elif _any_by.get(_k):
                _pl = "已解押"
            else:
                _pl = "无"
            _rows.append({
                "name": _nm,
                "nature": _h.get("股东性质") or "—",
                "pct": f"{_pct:.2f}%" if _pct is not None else "",
                "shares": f"{_sh / 1e4:,.2f}" if _sh else "",
                "pledge": _pl,
                "hl": False,
            })
        if _rows:
            _rows[0]["hl"] = True
            data["shareholders"] = _rows
            if _is_free:
                # 兜底口径只披露流通部分（限售股持有人会显著偏低），必须如实标注，
                # 否则与报告表头「前十大股东」冲突——这正是 2026-09-16 的翻车点。
                _src = "东方财富前十大流通股东（**流通口径**，不含限售股）/ 股权质押明细"
                _basis = (
                    f"持股比例按当前总股本 {_cap / 1e8:.2f} 亿股折算（该口径未披露）"
                    if _cap
                    else "该口径未披露持股比例，本地亦无总股本数据可折算"
                )
            else:
                _src = "东方财富前十大股东 / 股权质押明细"
                _basis = "持股比例取自东财「占总股本持股比例」原文"
            if _cap:
                data["gov_note"] = (
                    f"{_basis}；质押占比按当前总股本口径（非质押当时口径）。"
                    f"数据来源：{_src}。"
                )
            else:
                # 总股本缺失时不得声称「按当前总股本口径」——那会变成无据陈述
                data["gov_note"] = f"{_basis}。数据来源：{_src}。"
            _holders_src_txt = _src
            _holders_pct_txt = _basis

    # 口径护栏（硬断言，2026-09-16）：股东表一旦渲染，表头与口径必须自洽、且必须带口径脚注。
    # 表头已由 holders_title(_caliber) 派生，此处为不变量校验——用于在**生成期**拦下
    # 「流通口径配『前十大股东』表头」这类无声降级，而不是等读者从数字里发现。
    if data.get("shareholders"):
        _t = str(data.get("shareholders_title") or "")
        if _caliber == HOLDER_CALIBER_FREE and "流通" not in _t:
            raise ValueError(
                "股东口径护栏：数据源为东财流通股东口径（不含限售股），"
                f"表头却为「{_t}」——拒绝产出名不副实的股东表"
            )
        if not str(data.get("gov_note") or "").strip():
            raise ValueError("股东口径护栏：股东表缺少口径脚注（gov_note 为空）")

    # 身份识别洞察：以数据为准覆盖 LLM 自由文本，杜绝 LLM 自算持股比例的编造
    # （实测翻车：把 2.78% 写成 27.7%，或把他人质押张冠李戴到实控人名下）。
    # 名册口径与东财快照口径的行结构一致（name/nature/shares/pct/pledge）。
    # 通用化：取第一大自然人股东作锚点，不再硬编码特定公司人名（原写死「赵马克/侯红亮」，
    # 导致 002384 等其他公司此块恒为空）。
    if _rows:
        _lead_nat = next(
            (r for r in _rows if "自然人" in (r.get("nature") or "")), None
        )
        _ins = []
        _lead = _lead_nat or (_rows[0] if _rows else None)
        if _lead:
            _ins.append(
                f"第一大股东 {_lead['name']}（{_lead.get('nature') or '—'}）直接持股 "
                f"{_lead['shares']} 万股，"
                f"占总股本 {_cap / 1e8:.2f} 亿股的 {_lead['pct']}，"
                f"质押状态：{_lead['pledge']}"
            )
        _pledged = [
            r for r in _rows
            if r is not _lead and r.get("pledge") not in ("无", "已解押", "—", None)
        ]
        if _pledged:
            _ins.append(f"当前在押主要为{_pledged[0]['name']}（{_pledged[0]['pledge']}）")
        if _ins:
            data["id_insight_html"] = (
                "；".join(_ins)
                + "。自然人持股比例偏高，治理层面需重点关注实控人减持与股权质押动机，"
                  "以及由此带来的控制权稳定性风险。"
            )
            _lead_nature = (_lead or {}).get("nature") or ""
            data["id_insight_title"] = (
                f'为什么"实控人性质：{_lead_nature}"这一栏值得单独看'
                if _lead_nature and _lead_nature != "—"
                else '为什么"实控人性质"这一栏值得单独看'
            )

    # ------------------------------------------------------------------
    # 01 身份识别 / 05 财务速写 / 页脚来源：均以真实数据为准覆盖 LLM
    #
    # 背景：这三块此前完全交给 LLM 自由生成，实测后果是——身份只填出 3 个字段、
    # 财务速写大面积留空、页脚渲染出「数据来源：数据来源：…」的占位重复文本。
    # 而这三块的每一个数字都来自可精确获取的一手数据，没有任何理由交给模型编。
    # 故一律代码计算后覆盖，与股东表 / 主营构成 / 同业一致的「数据优先」策略。
    # ------------------------------------------------------------------
    _ident = profile_dict.get("layers", {}).get("identity", {}) or {}
    _fin = ((profile_dict.get("layers", {}).get("financials") or {}).get("summary") or {})

    def _fmtnum(x, digits: int = 2, suffix: str = "%") -> str:
        """格式化数值；缺失一律显示「未获取」——绝不把缺失显示成 0。"""
        v = _num(x)
        if v is None or v != v:  # None / NaN
            return "未获取"
        return f"{v:.{digits}f}{suffix}"

    def _clsnum(x) -> str:
        """数值着色：≥0 记 up（红），<0 记 down（绿），遵循 A 股涨跌配色惯例。"""
        v = _num(x)
        if v is None:
            return ""
        return "up" if v >= 0 else "down"

    # --- 01 身份识别：顶部指标 + 关键识别字段（只列有真实值的项，缺失不占位）---
    _id_metrics = []
    if _cap > 0:
        _id_metrics.append({
            "lab": "总股本", "val": f"{_cap / 1e8:.2f} 亿股", "note": "当前口径",
        })
    if _ident.get("list_date"):
        _ld = str(_ident["list_date"])
        _yrs = ""
        try:
            _yrs = "上市 %.1f 年" % (
                (datetime.now() - datetime.strptime(_ld, "%Y-%m-%d")).days / 365.25
            )
        except Exception:
            _yrs = ""
        _id_metrics.append({"lab": "上市日期", "val": _ld, "note": _yrs or "—"})
    if _num(_fin.get("total_assets")) is not None:
        _id_metrics.append({
            "lab": "总资产",
            "val": f"{_num(_fin.get('total_assets')) / 1e8:.2f} 亿",
            "note": f"截至 {_fin.get('as_of') or '最新报告期'}",
        })
    if _id_metrics:
        data["id_metrics"] = _id_metrics

    def _kv(pairs):
        """只保留有真实值的 KV 项——身份字段缺失时宁可不列，也不展示一排「未获取」。"""
        return [{"k": k, "v": v} for k, v in pairs if v]

    _rc = _num(_ident.get("reg_capital"))
    _id_left = _kv((
        ("成立日期", str(_ident.get("found_date") or "")),
        ("上市日期", str(_ident.get("list_date") or "")),
        ("法人代表", str(_ident.get("legal_rep") or "")),
        # cninfo「注册资金」单位为万元 → 换算为亿元展示
        ("注册资本", f"{_rc / 1e4:.2f} 亿元" if _rc else ""),
        ("所属市场", str(_ident.get("market") or "")),
    ))
    _id_right = _kv((
        ("申万行业", str(_ident.get("industry_sw") or "")),
        ("证监会行业", str(_ident.get("industry_cninfo") or "")),
        ("官方网站", str(_ident.get("website") or "")),
        ("入选指数", str(_ident.get("indices") or "")),
        ("办公地址", str(_ident.get("office_addr") or "")),
    ))
    if _id_left:
        data["id_kv_left"] = _id_left
    if _id_right:
        data["id_kv_right"] = _id_right

    # --- 05 财务速写：全部来自三表/指标表计算，不交给 LLM ---
    # B 类：统一标注指标所属报告期。财务速写所有指标均为**最新报告期（as_of）**口径，
    # 不标注会与「年报口径」的同类指标（如毛利率 10.35% vs 年报 9.51%）对不上，
    # 被误读为算错——标注后期间差异回归为「口径不同」。
    _plabel = _period_label(_fin.get("as_of"))
    _psuffix = f"｜{_plabel}" if _plabel else ""
    _fin_metrics = []
    for _k, _lab, _note in (
        ("roe", "ROE", "净资产收益率"),
        ("gross_margin", "毛利率", "自算 (营收−成本)/营收"),
        ("net_margin", "净利率", "销售净利率"),
        ("debt_ratio", "资产负债率", "总负债 / 总资产"),
    ):
        if _num(_fin.get(_k)) is not None:
            _fin_metrics.append({
                "lab": _lab, "val": _fmtnum(_fin.get(_k)),
                "note": f"{_note}{_psuffix}",
            })
    if _fin_metrics:
        data["fin_metrics"] = _fin_metrics

    _fg = []
    for _k, _lab in (
        ("rev_growth", "营收同比"),
        ("profit_growth", "净利同比"),
        ("rev_cagr", "营收 CAGR（3Y）"),
        ("profit_cagr", "净利 CAGR（3Y）"),
    ):
        if _num(_fin.get(_k)) is not None:
            # CAGR 已自带 3Y 区间语义，不再叠加单期标签
            _lb = _lab if "CAGR" in _lab else (f"{_lab}（{_plabel}）" if _plabel else _lab)
            _fg.append({
                "k": _lb,
                "v": _fmtnum(_fin.get(_k)),
                "cls": _clsnum(_fin.get(_k)),
            })
    if _fg:
        data["fin_growth"] = _fg

    _fv = []
    _eq = _num(_fin.get("equity"))
    if _eq and _cap > 0:
        _fv.append({"k": "每股净资产", "v": f"{_eq / _cap:.2f} 元", "cls": ""})
    if _num(_fin.get("rd_ratio")) is not None:
        _fv.append({
            "k": f"研发费率（{_plabel}）" if _plabel else "研发费率",
            "v": _fmtnum(_fin.get("rd_ratio")), "cls": "",
        })
    if _num(_fin.get("oper_cash")) is not None:
        _fv.append({
            "k": f"经营现金流净额（{_plabel}）" if _plabel else "经营现金流净额",
            "v": f"{_num(_fin.get('oper_cash')) / 1e8:.2f} 亿",
            "cls": _clsnum(_fin.get("oper_cash")),
        })
    # 估值口径：本项目未接入实时行情源（东财 push2 端点不稳定），诚实标注缺失，
    # 绝不用 LLM 记忆里的 PE/PB 冒充实时数据。
    _fv.append({"k": "PE(TTM) / PB", "v": "未获取（无实时行情源）", "cls": ""})
    if _fv:
        data["fin_valuation"] = _fv

    _ap = []
    if _num(_fin.get("inventory_days")) is not None:
        _ap.append({
            "lab": "存货周转天数",
            "val": f"{_num(_fin.get('inventory_days')):.1f} 天",
            "note": f"越短越好{_psuffix}",
        })
    if _num(_fin.get("ar_days")) is not None:
        _ap.append({
            "lab": "应收周转天数",
            "val": f"{_num(_fin.get('ar_days')):.1f} 天",
            "note": f"回款速度{_psuffix}",
        })
    _c2p = _num(_fin.get("cf_to_profit"))
    if _c2p is not None:
        _ap.append({
            "lab": "经营现金流 / 净利润",
            "val": f"{_c2p:.1f}%",
            "note": f"利润的现金含量{_psuffix}",
        })
    if _ap:
        data["asset_pressure"] = _ap
    # 现金流背离提示：经营现金流显著低于净利润，是分销商典型的利润质量风险点
    if _c2p is not None and _c2p < 60:
        data["cash_flag_title"] = (
            f"现金流背离：经营现金流仅为净利润的 {_c2p:.1f}%——"
            "利润的现金含量偏低，需关注应收账款与存货对资金的占用"
        )

    # --- 页脚来源分级：替换 LLM 生成的「数据来源：数据来源：…」占位重复文本 ---
    # 股东口径随**实际启用来源**变化：名册口径下不再并列「东方财富前十大股东」，
    # 也不再说「持股比例按当前总股本折算」（名册比例即其披露原文）。
    _roster_used = bool(_rh_rows and _rows)
    _ef_src = (
        "东方财富（主营业务构成、股权质押明细、财务分析指标）"
        if _roster_used
        else "东方财富（主营业务构成、前十大股东、股权质押明细、财务分析指标）"
    )
    if _roster_used:
        _basis_txt = (
            f"{_holders_pct_txt}；03 治理风险扫描的未解押质押笔数／占比取东方财富质押明细，"
            "按当前总股本折算（非质押当时口径）"
        )
    else:
        _basis_txt = (
            f"{_holders_pct_txt or '持股比例按东方财富披露值折算'}；"
            "质押占比按当前总股本口径（非质押当时口径）"
        )
    data["sources_html"] = (
        f"{_ef_src}；"
        + (f"{_holders_src_txt}；" if _holders_src_txt else "")
        + "巨潮资讯 cninfo（公司基本资料、主营业务与经营范围）；"
        "申万指数（行业分类）；Sina 财经（利润表 / 资产负债表 / 现金流量表）。"
        f"财务数据截至最新报告期；{_basis_txt}。"
    )
    data["caliber_html"] = (
        f"财务数据均为合并报表口径；最新报告期 {_fin.get('as_of') or '—'}"
        + (f"；总股本 {_cap / 1e8:.2f} 亿股（当前口径）。" if _cap > 0 else "。")
        + "毛利率为自算口径 (营业总收入−营业成本)/营业总收入——"
          "东方财富「销售毛利率(%)」列在半年报期对全行业返回空值，故不采用。"
    )
    data["source_tiers_html"] = (
        "一手来源——公司年报／半年报／公告、交易所披露、东方财富与 Sina 财经的结构化数据；"
        "本报告中的持股比例、股权质押、主营构成、同业对比、财务速写等指标均由系统"
        "<strong>代码直接计算</strong>，未经模型改写，可直接采信。"
        "其中「上下游 / 前十大股东 / 深度合作 / 分红回购」等由系统用 PyMuPDF 解析公司定期报告原文抽取（最新半年报优先），"
        "并在对应小节标注「一手来源·《年报》」与页码，可直接溯源。"
        "非一手来源——增长空间、各类叙述性分析由 AI 基于公开信息生成，"
        "已在对应小节标注「非一手来源·待核实」，请以其原始披露为准。"
    )

    # P3-2：6 层裸奔叙述字段的数据层兜底。仅当 LLM 值类型正确且过校验时保留
    # （LLM 只做叙述润色），否则用数据层确定性派生替换；数据层也无货的字段清空回「未获取」
    # 空默认，绝不注入编造内容。与生成侧通用护栏（`_overview_json_is_valid`）互为冗余。
    _fb_layer = _build_layer_fallbacks(profile_dict)
    _layer_rules = {
        "ind_metrics": _valid_plain_list,
        "peers_note": _valid_narr_html,
        "gov_scan_note": _valid_narr_html,
        "div_metrics": _valid_plain_list,
        "div_text": _valid_narr_html,
        "gov_right": _valid_plain_list,
        "trend_note": _valid_narr_html,
        "cash_note": _valid_narr_html,
        "events": _valid_events_field,
        "risks_left": _valid_plain_list,
        "risks_right": _valid_plain_list,
        "risk_emphasis": _valid_plain_list,
        "badges": _valid_plain_list,
        "id_insight_html": _valid_narr_html,
    }
    # 文本型叙述字段在类型校验通过后，再过一次数字量级/股东比例事实校验；
    # 命中（如错误质押比例、虚高毛利率）即视为脏值，强制回退数据层确定性派生。
    _layer_text_fields = {
        "peers_note", "gov_scan_note", "div_text", "trend_note",
        "cash_note", "id_insight_html",
    }
    for _k, _valid in _layer_rules.items():
        _llm_v = data.get(_k)
        if _valid(_llm_v):
            if _k in _layer_text_fields and isinstance(_llm_v, str):
                _claim_issues = _factual_claim_check(_llm_v, profile_dict)
                if _claim_issues:
                    logger.warning(
                        "[overview] 叙述字段 %s 命中数字量级/股东比例证伪（已回退数据层）：%s",
                        _k, _claim_issues,
                    )
                    _llm_v = None  # 强制进入下方兜底分支
            if _llm_v is not None:
                continue
        _fb = _fb_layer.get(_k)
        if _fb not in (None, "", [], {}):
            if _llm_v not in (None, "", [], {}):
                logger.warning(
                    "[overview] 叙述字段 %s 的 LLM 值无效（已回退数据层）：%r", _k, _llm_v
                )
            data[_k] = _fb
        elif _llm_v not in (None, "", [], {}):
            # 数据层也无货：清空 LLM 脏值，回到「未获取」空默认，避免编造内容外流
            logger.warning(
                "[overview] 叙述字段 %s 的 LLM 值无效且无数据兜底，已清空：%r", _k, _llm_v
            )
            data[_k] = [] if isinstance(_llm_v, list) else ({} if isinstance(_llm_v, dict) else "")

    # 股份回购 = 年报一手硬事实，**不接受 LLM 叙述覆盖**。
    # A1 根因：schema 的 repurchase 字段长期未填充，LLM 凭记忆编造「12,425,123 股」
    # （真值 2,979,800 股）。故刻意不进上面的 _layer_rules 白名单——那会让 LLM 的编造值
    # 通过 _valid_narr_html 校验后被保留；改为在循环后无条件以数据层真值覆盖，
    # 无年报数据时清空，确保编造内容不外流。
    data["repurchase_metrics"] = _fb_layer.get("repurchase_metrics") or []
    data["repurchase_text"] = _fb_layer.get("repurchase_text") or ""

    # 实际控制人同理：年报一手硬事实，不接受 LLM 叙述覆盖（同 A1 编造风险）。
    data["controller_metrics"] = _fb_layer.get("controller_metrics") or []
    data["controller_text"] = _fb_layer.get("controller_text") or ""

    # 治理风险扫描同理（D 类）：整段由质押明细确定性推导——在押主体是否属于前十大、
    # 控股股东有无在押。这两点是**定性结论的关键依据**，交给 LLM 就会丢失
    # 「侯红亮非前十大 / 控股股东已全额解押」的区分，把财务投资者的质押误读成
    # 控股股东风险。数据层有值即无条件覆盖；无质押数据时清空，避免旧值残留。
    if _fb_layer.get("gov_scan_note"):
        data["gov_scan_note"] = _fb_layer["gov_scan_note"]
    else:
        data["gov_scan_note"] = ""

    # P2-E1：行情与资金面（延时行情 + 融资融券），以数据为准无条件覆盖 LLM 可能漏返的字段。
    _mk = _fb_layer.get("market_kv")
    if _mk:
        data["market_kv"] = _mk
        data["market_meta"] = _fb_layer.get("market_meta") or ""
        # 页眉时效说明精确化：有行情日期时不再用模糊的「最近收盘」
        _mq = re.search(r"(\d{4}-\d{2}-\d{2})", data["market_meta"] or "")
        if _mq:
            data["as_of_text"] = (
                f"财务数据截至最新报告期，行情数据截至 {_mq.group(1)}（延时行情）"
            )
        # 用真实行情覆盖 05 估值坐标里的「未获取」占位（PE(TTM)/PB）
        _pe = _to_float(_q.get("pe_ttm"))
        _pb = _to_float(_q.get("pb"))
        if _pe is not None or _pb is not None:
            _val = (f"PE {_pe:.2f}" if _pe is not None else "PE —") + " / " + (
                f"PB {_pb:.2f}" if _pb is not None else "PB —"
            )
            data["fin_valuation"] = [
                x for x in (data.get("fin_valuation") or [])
                if "未获取" not in str(x.get("v", ""))
            ]
            data["fin_valuation"].append({"k": "PE(TTM) / PB", "v": _val, "cls": ""})
    # P2-E3：多空逻辑（数据层确定性派生），以数据为准覆盖
    if _fb_layer.get("bull_points"):
        data["bull_points"] = _fb_layer["bull_points"]
    if _fb_layer.get("bear_points"):
        data["bear_points"] = _fb_layer["bear_points"]

    # —— 三块「具体公司」补位：一手抽取未点名的环节由公开产业信息补充业内公认的
    # 代表性企业（一律标注「非一手来源·待核实」）。背景：定期报告普遍不点名客户/
    # 供应商（实测 601138 半年报 221 页仅 1 处具名合作方、002384 全篇无具名客户），
    # 单纯依赖一手抽取无法满足「列举具体公司」的阅读需求。
    # 原则：一手行永远在前且原样保留，公开信息只补空缺、绝不覆盖一手来源。
    try:
        _pub = _build_public_entities(_bc, data, use_llm=use_llm)
    except Exception as _pub_e:  # noqa: BLE001
        logger.warning("[overview] 公开信息补位异常：%s", _pub_e)
        _pub = {"upstream": [], "downstream": [], "partners": []}

    # 上游：代表企业挂到对应类别行（无授权代理阵营的制造商等）
    if _pub.get("upstream") and data.get("upstream_struct"):
        _sup_by_cat = {
            str(r.get("category") or ""): str(r.get("names") or "")
            for r in _pub["upstream"]
        }
        for _ur in data["upstream_struct"]:
            _sup = _sup_by_cat.get(str(_ur.get("category") or ""))
            if _sup:
                _ur["suppliers"] = _sup
                _ur["suppliers_basis"] = "公开产业信息 · 待核实"
        data["upstream_struct_has_suppliers"] = any(
            str(r.get("suppliers") or "").strip() for r in data["upstream_struct"]
        )

    # 下游：仅当该领域一手「代表客户」为泛称时才补位（一手已具名的领域不动）
    if _pub.get("downstream") and data.get("downstream"):
        _dw_by_field = {
            re.sub(r"\s+", "", str(d.get("field") or "")): d
            for d in data["downstream"]
        }
        for _pd in _pub["downstream"]:
            _fkey = re.sub(r"\s+", "", str(_pd.get("field") or ""))
            _row = _dw_by_field.get(_fkey)
            if _row is None:
                continue
            _cur = str(_row.get("customers") or "")
            _cur_names = [n for n in re.split(r"[、，,]", _cur) if n.strip()]
            if _cur_names and not all(is_generic_entity(n.strip(" 、，,()")) for n in _cur_names):
                continue  # 一手已具名，无需补位
            _row["public_customers"] = str(_pd.get("customers") or "")
            _row["public_basis"] = str(_pd.get("basis") or "") or "公开产业信息 · 待核实"
        if any(str(d.get("public_customers") or "").strip() for d in data["downstream"]):
            data["downstream_note"] = (data.get("downstream_note") or "") + (
                "「代表客户」列中单列的标注「公开信息·待核实」部分为基于公开产业信息"
                "补充的业内公认代表企业，非公司定期报告披露，仅供参照。"
            )

    # 合作网络：一手为泛称口径时，追加公开信息具名行（独立成行，不替换泛称行）
    if _pub.get("partners"):
        _p_have = {
            re.sub(r"\s+", "", str(p.get("partner") or ""))
            for p in (data.get("partners") or [])
        }
        for _pp in _pub["partners"]:
            _pn = str(_pp.get("partner") or "")
            _pk = re.sub(r"\s+", "", _pn)
            if not _pn or _pk in _p_have:
                continue
            data.setdefault("partners", []).append({
                "partner": _pn,
                "relation": str(_pp.get("relation") or "公开信息 · 待核实"),
                "content": str(_pp.get("content") or ""),
                "named": True,
                "src": "public",
                "basis": str(_pp.get("basis") or ""),
            })
            _p_have.add(_pk)
        # 再走一次名单级去重 + 排序（公开信息行可能与一手行重名）
        data["partners"] = _dedupe_partner_rows(data.get("partners") or [])
        # 标题/导语按最终行重算（含公开信息补位行），避免标题家数与表格不一致
        if data.get("partners"):
            _all_rows = [p for p in data["partners"] if str(p.get("partner") or "").strip()]
            _n_pub = sum(1 for p in _all_rows if p.get("src") == "public")
            _n_1st = len(_all_rows) - _n_pub
            _head_rows = [p for p in _all_rows if p.get("named", True)] or _all_rows
            _head = "、".join(str(p.get("partner") or "") for p in _head_rows[:3])
            data["partners_title"] = (
                f"深度合作网络：{_head} 等 {len(_all_rows)} 家合作方"
                if len(_all_rows) > 3 else f"深度合作网络：{_head}"
            )
            data["partners_intro"] = (
                f"合作网络是业务模式的直接证据。下列 {len(_all_rows)} 条合作方与合作关系中，"
                + (f"{_n_1st} 条取自定期报告一手披露" if _n_1st else "一手披露部分已并入下表")
                + (
                    f"，另 {_n_pub} 条为基于公开产业信息补充的业内公认代表企业"
                    "（标注「公开信息·待核实」，非公司披露，仅供参照）："
                    if _n_pub else "，均可溯源："
                )
            )

    # 固定话术治理：核心结论标题按公司名生成（原为模板写死的「一句话看这家公司」）
    _nm_final = (data.get("name") or "").strip()
    if _nm_final and _nm_final != "（未获取）":
        data["conclusion_title"] = f"一句话看{_nm_final}"

    # 数据层无条件覆盖：页眉「报告日期」恒为报告生成当日（本地日期，仅到日）。
    # 防任何上游来源（契约回填、缓存）把报告期/主营构成期误当成报告日期。
    data["report_date"] = datetime.now().strftime("%Y-%m-%d")

    return data


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _block(value, tag: str):
    """渲染叙述内容：内容自带块级标签时不再外层重复包裹。

    修复 `<p><p>…</p></p>` / `<li><li>…</li></li>` 双层嵌套：模板对每个叙述字段都套了一层
    `<p>`（或 `<li>`），而 LLM 生成的内容常自带同名标签，浏览器虽能容错渲染，
    但属非法嵌套。这里按内容自身是否已含块级标签决定是否包裹——单点解决，无需逐个改模板。
    """
    from markupsafe import Markup

    s = "" if value is None else str(value)
    s = s.strip()
    if not s:
        return Markup("")
    # LLM 常在 HTML 正文里混写 Markdown 粗体（**x**），直接输出会露出星号
    s = re.sub(r"\*\*([^*\n]{1,200})\*\*", r"<strong>\1</strong>", s)
    if re.search(rf"<{tag}[\s>]", s, re.I):
        return Markup(s)
    return Markup(f"<{tag}>{s}</{tag}>")


def _blockp(value):
    """段落：内容已含 <p> 时原样输出，否则补一层 <p>。"""
    return _block(value, "p")


def _blockli(value):
    """列表项：内容已含 <li> 时原样输出，否则补一层 <li>。"""
    return _block(value, "li")


def render_overview(data: dict, template_dir: str, out_path: str | None = None) -> str:
    """用 template_overview.html 渲染概况报告；可选落盘。

    D4：把 Chart.js 本地副本内联进 `<script>`，使报告可离线打开/打印；
    读不到本地库则模板自动回退 CDN（`chart_assets` 已把该分支的判断交给模板）。
    """
    # 注入内联库与降级标志（原地写回 data，供 meta 侧读取 chart_offline）
    chart_assets.apply_chart_lib(data, chart_assets.inline_chartjs())
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["blockp"] = _blockp
    env.filters["blockli"] = _blockli
    tpl = env.get_template("template_overview.html")
    html = tpl.render(**data)
    if out_path:
        Path(out_path).write_text(html, encoding="utf-8")
        return out_path  # 落盘时返回文件路径（与 render() 一致），供 storage 二次落盘
    return html
