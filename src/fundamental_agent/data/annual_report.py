"""年报一手来源抽取（PyMuPDF）。

P1 核心模块：下载公司最新年度报告 PDF（巨潮 sourced，本地缓存），用 PyMuPDF 做
确定性解析，抽取「上游代理的原厂产品线」与「下游应用领域」的**一手来源**内容，
并逐段标注页码以便报告内归因。

设计原则：
- 全部为确定性解析（正则 + 页码归因），不依赖 LLM，避免编造。
- 失败时优雅降级：fetch_annual_report 返回 None，调用方据此隐藏对应小节。
- 缓存命中则不重复下载，控制链路耗时。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path

import fitz  # pymupdf
import requests

logger = logging.getLogger(__name__)

# data/cache/reports —— 相对本文件（src/fundamental_agent/data/annual_report.py）
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = _PROJECT_ROOT / "data" / "cache" / "reports"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# A2（速度优化 2026-09-10）：年报 PDF 抽取结果磁盘缓存——同一份 PDF 的确定性解析
# （名册/公司行动/上下游）每次生成都重复执行 PyMuPDF 全文解析，按 PDF 身份指纹缓存。
EXTRACT_CACHE_DIR = _PROJECT_ROOT / "data" / "cache" / "annual_extract"
EXTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _pdf_sig_of(path) -> str:
    st = Path(path).stat()
    return hashlib.sha256(f"{Path(path).stem}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:12]


def _code_pdf_sig(code: str) -> str:
    """某公司全部已缓存 PDF 的合并指纹：任一 PDF 增删/变化即失效。"""
    h = hashlib.sha256()
    for p in sorted(CACHE_DIR.glob(f"{code}_*.pdf")):
        st = p.stat()
        h.update(f"{p.name}|{st.st_mtime_ns}|{st.st_size};".encode())
    return h.hexdigest()[:12]


def _extract_cache_get(kind: str, code: str):
    try:
        f = EXTRACT_CACHE_DIR / f"{kind}_{code}_{_code_pdf_sig(code)}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _extract_cache_put(kind: str, code: str, data):
    try:
        f = EXTRACT_CACHE_DIR / f"{kind}_{code}_{_code_pdf_sig(code)}.json"
        f.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass

_CNINFO_QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
_STATIC = "http://static.cninfo.com.cn/"
# 东财公告兜底（cninfo 只返回「摘要」时用它找定期报告正文并下载）
_EM_ANN_QUERY = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_EM_PDF = "https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf"
_EM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}


def _fetch_report_full_from_em(code: str, kind: str) -> Path | None:
    """东财公告接口兜底取「定期报告正文」PDF（纯正文，非摘要/英文版/更正稿）。

    背景：cninfo 公告查询对上交所标的有时只返回《半年度报告摘要》，缓存里于是
    只有摘要——摘要不含「前十名股东」「前五名客户/供应商」等章节，会让股东名册、
    上下游一手来源整体退回上一年度年报（实测 601138：上游标注成《2025 年年度报告》）。
    东财公告接口与静态 PDF 域名均可匿名访问，作为 fallback 补全正文。
    成功时返回已下载的本地 PDF 路径，失败返回 None（调用方按原逻辑继续）。
    """
    kw = "半年度报告" if kind == "semi" else "年度报告"
    try:
        r = requests.get(
            _EM_ANN_QUERY,
            params={
                "sr": "-1", "page_size": "50", "page_index": "1",
                "ann_type": "A", "client_source": "web", "stock_list": code,
                "f_node": "0", "s_node": "0",
            },
            headers=_EM_HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        anns = ((r.json() or {}).get("data") or {}).get("list") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("[annual_report] 东财公告查询失败：%s", e)
        return None

    cands = []
    for a in anns:
        title = a.get("title") or ""
        if kw not in title:
            continue
        # 排除摘要/英文版/更正或取消类公告，只留正文本体
        if any(x in title for x in ("摘要", "英文", "取消", "更正", "补充")):
            continue
        if kind == "annual" and "半年度" in title:
            continue
        cands.append(a)
    if not cands:
        return None
    cands.sort(key=lambda a: a.get("notice_date") or "", reverse=True)
    art = cands[0].get("art_code") or ""
    raw_title = cands[0].get("title") or f"{code}_{kw}"
    # 东财标题形如「工业富联:富士康工业互联网股份有限公司2026年半年度报告」，
    # 去掉「简称:」前缀，与 cninfo 命名对齐——否则报告「一手来源」徽章会带上
    # 简称前缀（实测出现《工业富联_富士康工业互联网股份有限公司2026年半年度报告》）。
    title = re.split(r"[:：]", raw_title, maxsplit=1)[-1].strip() or raw_title
    if not art:
        return None

    safe = re.sub(r"[^\w一-鿿.-]", "_", title)
    pdf_path = CACHE_DIR / f"{code}_{safe}.pdf"
    if pdf_path.exists() and pdf_path.stat().st_size > 100_000:
        return pdf_path
    try:
        pr = requests.get(
            _EM_PDF.format(art_code=art), headers=_EM_HEADERS,
            timeout=180, stream=True,
        )
        pr.raise_for_status()
        if pr.content[:4] != b"%PDF" and not pr.headers.get(
            "Content-Type", ""
        ).startswith("application/pdf"):
            logger.warning("[annual_report] 东财返回非 PDF（title=%s）", title)
            return None
        tmp = pdf_path.with_suffix(".part")
        with open(tmp, "wb") as f:
            for ch in pr.iter_content(8192):
                f.write(ch)
        tmp.replace(pdf_path)
        logger.info("[annual_report] 东财兜底下载正文成功：%s", pdf_path.name)
        return pdf_path
    except Exception as e:  # noqa: BLE001
        logger.warning("[annual_report] 东财正文下载失败：%s", e)
        return None

# 原厂品牌定义行：MURATA（村田） 指 日本芯片原厂品牌
_RE_MAKER = re.compile(
    r"([A-Z][A-Za-z0-9()./+-]*)\s*[（(]([^）)]*?)[）)]\s*指\s*(.{0,12}?)芯片原厂品牌"
)
# 产品线定义行：MLCC 指 多层片式陶瓷电容器……
_RE_PRODUCT = re.compile(r"([A-Z][A-Za-z0-9-]*)\s*指\s*(.{4,60}?)(?=[；;。.\n]|$)")

# 上下游锚点
# 说明：原 _DOWN_ANCHORS 只收复合词（终端客户/主要客户/客户结构…），漏掉年报里最常见的
# 裸「客户」表述（如 p11「公司客户主要分布在…拥有舜宇、小米、比亚迪…等知名客户」），
# 导致客户名单整段从未进入候选。故补裸「客户」及同义表述。
_UP_ANCHORS = [
    "上游", "原厂", "授权代理", "主要代理", "代理产品", "代理品牌", "产品线",
    "供应商", "合作原厂", "分销业务",
    # 补：核心竞争力章节常用表述（p16「拥有上百家国内外知名上游芯片原厂产品线代理权」）
    "代理权", "核心竞争力", "授权分销", "代理及分销", "分销商",
    # 补：供给侧产能/资产（p16「2000 多平方米全性能测试实验室 + 10000 多平方米
    # 防静电车间」属制造与检测能力，是年报里的实体壁垒表述）
    "平方米", "仓储", "仓库", "物流", "实验室", "车间", "生产基地", "产能",
]

# 护城河信号：命中即排到最前。年报「核心竞争力分析」章节是护城河节的一手来源，
# 但其顿号密度低（列举少），若只按密度排序会被产品线/客户名单页挤掉。
_MOAT_SIGNALS = ["核心竞争力", "竞争优势", "护城河", "竞争壁垒", "行业地位", "进入壁垒"]
_DOWN_ANCHORS = [
    "下游", "终端客户", "下游客户", "应用领域", "主要客户", "应用于", "终端应用",
    "客户结构", "客户行业",
    # 补：裸「客户」及客户名单句式
    "客户", "知名客户", "客户群体", "客户资源", "客户覆盖", "销售网络", "销售客户",
]

# PDF 页眉噪声：「武汉力源信息技术股份有限公司2025 年年度报告全文 11」
_RE_PAGE_HEADER = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9（）()]{2,40}?\d{4}\s*年年度报告(?:全文|摘要)?\s*\d{1,4}"
)

# 下游应用领域词典（用于在原文中再抽取具体赛道标签，仅作展示用 chip）
_DOWN_FIELDS = [
    "新能源汽车", "汽车电子", "储能", "光伏", "低空经济", "AI", "消费电子",
    "工业控制", "通信设备", "医疗", "家电", "数据中心", "机器人", "物联网", "可穿戴",
    "安防", "工控", "电源", "军工",
]

# 国产线原厂**通用抽取**（替代旧版公司词典——词典只适配单一公司，违反
# 「不同公司报告内容不得写死」原则）：在含「原厂」的顿号列举句中收集纯中文
# 品牌项。中文顿号列举是 A 股年报披露品牌/客户/供应商名单的通用句式，
# 适用于任何公司；黑名单过滤叙述性通用名词，宁缺毋滥。
_RE_MAKER_LIST_SENT = re.compile(r"([^。；！？]{30,400}?原厂[^。；！？]{0,400}?)[。；！？]")
_GENERIC_NOUNS = (
    "公司", "有限", "股份", "集团", "控股", "科技股份", "上游", "下游", "客户",
    "供应商", "合作", "授权", "代理", "分销", "产品", "业务", "市场", "领域",
    "行业", "技术", "方案", "系统", "设备", "材料", "制造", "设计", "应用",
    "其他", "补充", "知名", "国内外", "芯片原厂", "原厂品牌", "等",
    # 叙述性动词/短语（列举句窗口偶尔吞入前后叙述）
    "研制", "推广", "布局", "经验", "情况", "积极", "深耕", "积累", "缺货",
    "丰富", "发展", "主要", "包括", "以下", "以及", "相关",
)


def _extract_domestic_makers(text: str) -> list[str]:
    """从定期报告原文抽国产线/中文名原厂（通用列举句式，零公司硬编码）。

    识别规则：含「原厂」且顿号 ≥4 的列举句中，纯中文（2~10 字）、不含通用
    名词的项视为品牌候选。为排除品类清单句（「电容、电感、磁珠…」）与叙述句，
    要求同句共现 ≥3 个「EN（中文名）」格式的海外品牌——只有品牌大列举句满足；
    按原文出现顺序去重，宁缺毋滥。
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in _RE_MAKER_LIST_SENT.finditer(text):
        sent = m.group(1)
        if sent.count("、") < 4:
            continue
        # 品牌大列举句确证：同句含 ≥3 个「EN（中文）」海外品牌
        if len(re.findall(r"[A-Za-z][A-Za-z0-9./+-]*\s*[（(][^）)]{1,12}[）)]", sent)) < 3:
            continue
        # 品牌列举总是位于「原厂」词**之前**（「…A、B、C 等上百家…原厂，代理分销
        # 的产品主要包括品类清单…」）；只取「原厂」前的子串，天然排除后半的品类清单
        part = sent[: m.group(0).find("原厂") if "原厂" in m.group(0) else len(sent)]
        for item in re.split(r"[、，,]", part):
            item = item.strip().strip("（）()")
            if not re.fullmatch(r"[\u4e00-\u9fa5]{2,10}", item):
                continue
            if any(k in item for k in _GENERIC_NOUNS):
                continue
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


# 供应商集中度：年报「主要供应商情况」（ flatten 后为连续文本 ）
# 单位变体通用：A 股年报存在「（元）」「（千元）」（宁德时代）与「销售额 xxx 万元」
# （贵州茅台）三种形态；金额统一折算成元（_UNIT_FACTOR）。
_UNIT_FACTOR = {"元": 1.0, "千元": 1e3, "万元": 1e4}

_RE_SUP_TOP5 = re.compile(
    r"前五名供应商(?:合计采购金额|采购额)\s*(?:[（(](?:人民币)?(元|千元|万元)[）)])?\s*"
    r"([\d,]+(?:\.\d+)?)\s*(元|千元|万元)?[^%]{0,40}?占年度采购总额(?:比例)?\s*(?:为|是)?\s*([\d.]+)\s*%"
)
_RE_SUP_NO1 = re.compile(r"1\s*第一名\s*([\d,]+(?:\.\d+)?)\s*([\d.]+)%")

# 客户集中度：年报「主要客户情况」。表结构与供应商表同构：
# 「前五名客户合计销售金额（元） 1,730,232,198.86 前五名客户合计销售金额占年度销售总额比例 19.51%」
# 随后「公司前5 大客户资料 序号 客户名称 销售额（元） 占年度销售总额比例 1 第一名 798,299,285.07 9.00% …」
_RE_CUST_TOP5 = re.compile(
    r"前五名客户(?:合计销售金额|销售额)\s*(?:[（(](?:人民币)?(元|千元|万元)[）)])?\s*"
    r"([\d,]+(?:\.\d+)?)\s*(元|千元|万元)?[^%]{0,40}?占年度销售总额(?:比例)?\s*(?:为|是)?\s*([\d.]+)\s*%"
)
_RE_CUST_ROW = re.compile(r"([1-5])\s*第([一二三四五])名\s*([\d,]+(?:\.\d+)?)\s*([\d.]+)%")

# 深度合作关系（通用句式，零公司硬编码）：
#   A. 授权代理（分销商）：释义章节「X（Y）指 …芯片原厂品牌」——合作方=原厂，
#      关系=上游授权代理（由 upstream_makers 提供，此处不重复抽）。
#   B. 联合/战略合作句：锚词（联合实验室/联合研发/战略合作/共同设立/合资设立/
#      深度合作 等）+ 句内「与/同/和 + 对方名 + 动词」定位合作方，整句作合作内容。
_RE_PARTNER_ANCHOR = re.compile(
    r"(?:联合实验室|联合研发|联合创新|联合开发|战略合作|战略协同|合资设立|共同设立|深度合作|长期合作)"
)
_RE_PARTNER_YU = re.compile(
    r"(?:与|同|和)\s*([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,24}?)\s*"
    r"(?:共同|联合|正式)?(?:设立|成立|共建|签署|签订|达成|开展|建设|打造|深化)"
)

# 列举式深度合作（制造/直销企业常见句式——合作方名单在句中、锚词在句尾）：
#   「公司与 A、B、C 等行业核心客户开展了深度合作」
#   「公司与 EDA 厂商、材料厂商、设备及零部件厂商等供应商建立了稳固的合作关系」
# 类别词可选：宁德时代式「与一汽解放、北汽福田、东风商用车等深化战略合作」
# （列举直接接锚词、无类别词）也能命中，relation 兜底「战略合作」；
# 排除「合作社」（茅台高粱采购模式噪音）。
_RE_PARTNER_ENUM = re.compile(
    r"(?:公司|本公司)(?:与|同|和)\s*"
    r"([^。；，,]{2,80})\s*(?:等\s*[^。；，,]{0,12}?)?\s*((?:客户|厂商|供应商|伙伴|企业)?)"
    r"[^。；]{0,24}?(?:开展|建立|深化|保持|推进|形成|签署|签订)[^。；]{0,8}?(?:了)?"
    r"(?:深度合作|战略合作|紧密合作|稳固的合作关系|长期合作|良好的合作关系|战略合作协议)"
)

# 动词前置变体（主语可省略）：「（同时，）深化与英伟达、思灵机器人等企业的合作，共同探索…」
# 实测 601138 半年报 221 页中唯一的具名合作句即此语序，旧正则要求
# 「公司与 X … 动词 … 锚词」顺序而整体漏抽，导致具名合作方全部丢失。
_RE_PARTNER_ENUM_PRE = re.compile(
    r"(?:深化|加强|拓展|扩大|推进|开展|建立)\s*(?:与|同|和)\s*"
    r"([^。；，,]{2,80})\s*"
    r"(?:等\s*[^。；，,]{0,12}?)?\s*"
    r"((?:客户|厂商|供应商|伙伴|企业|公司)?)"
    r"[^。；]{0,16}?(?:的)?(?:合作|协作|协同)"
)

# 泛称词素/词条：这些「名字」是原文真实措辞，但不是企业实体，不能当作
# 「具体公司」列示（实测 601138/002384 的合作方列曾被「全球多家头部客户」
# 「核心客户」占用，读者无法据此获取任何具体公司信息）。
_GENERIC_ENTITY_MARKERS = (
    "主要客户", "头部客户", "领先客户", "顶尖客户", "核心客户", "大客户", "客户群",
    "众多客户", "重要客户", "战略客户", "优质客户", "国内外客户", "全球客户",
    "主要供应商", "头部供应商", "核心供应商", "主要厂商", "合作伙伴", "合作方",
    "主要企业", "知名企业", "头部企业", "主要品牌",
)
_GENERIC_ENTITY_EXACT = {
    "客户", "供应商", "厂商", "伙伴", "企业", "公司", "客户群", "渠道商",
    "经销商", "品牌方", "服务商", "合作方", "上下游", "相关方", "原厂",
    "云服务商", "云厂商", "终端客户", "终端厂商", "下游客户", "上游供应商",
}


def is_generic_entity(name: str) -> bool:
    """判定名称是否为泛称（非具体公司/机构实体）。

    用于把「全球主要客户」这类表述与真正的具名公司（「英伟达」「村田」）
    区分开：前者可保留其原文信息量，但不能冒充「具体公司」展示。
    """
    n = re.sub(r"[\s·・（）()\[\]【】、，,]+", "", str(name or ""))
    if not n:
        return True
    if n in _GENERIC_ENTITY_EXACT:
        return True
    if any(w in n for w in _GENERIC_ENTITY_MARKERS):
        return True
    # 短名称且以泛称词素收尾（「全球主要云服务商」「行业核心客户」），
    # 但排除带企业后缀的正式名（「华为技术有限公司」不以泛称词收尾）
    if len(n) <= 12 and re.search(
        r"(客户|供应商|厂商|伙伴|合作方|服务商|企业|公司)$", n
    ):
        if not re.search(r"(有限公司|有限责任公司|股份公司|集团)$", n):
            return True
    # 关系描述而非企业实体（「产业链上下游」「产业生态伙伴」）
    if re.search(r"(上下游|产业链|供应链|生态圈|生态伙伴|相关方)$", n):
        return True
    return False


def split_named_entities(raw: str) -> tuple[list[str], list[str]]:
    """把「A、B、C 等 XX」式的名单拆成（具名实体, 泛称）两部分。"""
    names = [x.strip() for x in re.split(r"[、，,]", str(raw or "")) if x.strip()]
    named = [x for x in names if not is_generic_entity(x)]
    generic = [x for x in names if is_generic_entity(x)]
    return named, generic


def _extract_partner_sents(text: str) -> list[dict]:
    """从定期报告原文抽深度合作关系（通用句式，零公司硬编码）。

    模式 1（锚词式）：「与 X 共同设立…联合实验室」——锚词在前、合作方紧邻；
    模式 2（列举式）：「与 A、B、C 等行业核心客户开展了深度合作」——合作方
    名单在句中、锚词在句尾；
    模式 3（动词前置式）：「深化与 A、B 等企业的合作」——语序同上两式逆置，
    实测为具名合作方最常出现却最易漏抽的一种。

    名单统一按「具名实体 / 泛称」拆分：具名实体（英伟达、村田）作为合作方
    展示；纯泛称（「全球主要客户」）虽是原文真实措辞但不能充当「具体公司」，
    以 `named=False` 标记后由展示层弱化呈现。整句作合作内容（可溯源）。
    """
    out: list[dict] = []
    seen: set[str] = set()

    def _sent_of(pos_start: int, pos_end: int) -> str:
        s0 = text.rfind("。", 0, max(0, pos_start - 1))
        s1 = text.find("。", pos_end)
        return text[(s0 + 1 if s0 >= 0 else max(0, pos_start - 120)):(s1 + 1 if s1 >= 0 else pos_end + 40)].strip()

    def _relation_of(obj: str) -> str:
        if "客户" in obj:
            return "下游核心客户 · 深度合作"
        if "供应商" in obj or "厂商" in obj:
            return "上游供应链 · 紧密合作"
        return "战略合作"

    def _emit(obj_raw: str, relation: str, sent: str) -> None:
        """名单拆分 → 具名优先 → 去重入表。"""
        obj = re.sub(r"等[^，。；]{0,10}$", "", re.sub(r"\s+", "", obj_raw or "")).strip("、，, ")
        # 贪婪捕获会把定语一并吃进（「头部客户的联合研发模式」→ 合作方实为「头部客户」），
        # 「与 X 的 Y」句式中 X 才是合作方，按「的」截断
        obj = re.split(r"的|之|以及", obj)[0].strip("、，, ")
        # 句首即泛称词素且明显是定语从句（「客户在技术创新及新品研发方面」）时，
        # 截断回泛称词本身；「头部客户」这类短泛称保持原样（交由泛称判定处理）
        _gm = re.search(r"(客户|供应商|厂商|伙伴|企业|公司|服务商|上下游)", obj)
        if _gm and _gm.start() == 0 and len(obj) > 6:
            obj = _gm.group(1)
        if len(obj) < 2 or not sent:
            return
        # PDF 折行会把句子切碎（「技术迭代\n与产能优化」），去折行但保留词内空格
        sent = re.sub(r"\s*\n\s*", "", sent)
        sent = re.sub(r"[ \t]{2,}", " ", sent).strip()
        named, generic = split_named_entities(obj)
        partner = "、".join(named)
        if not partner:
            # 该句未点名具体公司：保留泛称原文（信息量仍在「合作内容」），
            # 标记 named=False，展示层据此弱化，不与具名公司混淆
            partner = "、".join(generic[:3])
        if len(partner) < 2:
            return
        key = partner[:40]
        if key in seen:
            return
        seen.add(key)
        out.append({
            "partner": partner,
            "relation": relation,
            "content": sent[:220] + ("……" if len(sent) > 220 else ""),
            "named": bool(named),
        })

    # 模式 1：锚词式（联合实验室等具体项目）
    for m in _RE_PARTNER_ANCHOR.finditer(text):
        head = text[max(0, m.start() - 80): m.start()]
        my = None
        for my in _RE_PARTNER_YU.finditer(head):
            pass  # 取锚词前最后一个匹配
        if not my:
            continue
        _emit(
            my.group(1).strip(),
            f"联合/战略合作（{m.group(0)}）",
            _sent_of(m.start(), m.end()),
        )

    # 模式 2：列举式（客户/供应商名单 + 合作锚词在句尾）
    for m in _RE_PARTNER_ENUM.finditer(text):
        _emit(
            m.group(1),
            _relation_of(m.group(2)) if m.group(2) else "战略合作",
            _sent_of(m.start(), m.end()),
        )

    # 模式 3：动词前置式（「深化与 A、B 等企业的合作」）
    for m in _RE_PARTNER_ENUM_PRE.finditer(text):
        _emit(
            m.group(1),
            _relation_of(m.group(2)) if m.group(2) else "战略合作",
            _sent_of(m.start(), m.end()),
        )
    return out

# 前十大股东名册（定期报告「持股5%以上的股东或前10名股东持股情况」表）：
# 每行 = 股东名称 | 股东性质 | 持股比例% | 期末持股数 | 期内增减 | 限售数 | 无限售数 | 质押状态 | 质押数量
_ANCHOR_HOLDER = re.compile(
    # ① 深市句式：「持股5%以上的股东或前10名股东持股情况」
    r"持股\s*5\s*%\s*以上的股东或前\s*10\s*名股东持股情况"
    # ② 通用句式：允许「前十名股东1 持股情况」这类脚注上标（实测 600941 中报）
    r"|前\s*(?:10|十)\s*名股东\s*[0-9]?\s*持股情况"
)
def _spaced(word: str) -> str:
    """把词逐字用 ``\\s*`` 连接，容忍 PDF 折行把词拆开。

    实测（300308 半年报）：股东性质列被排版拆成「境内 非国 有法 人」，
    而旧枚举写死 `国有` 连续 → 该行整条不匹配。逐字分隔后可覆盖任意
    断字位置，且因只放行空白、不会跨行吞掉正文。
    """
    return r"\s*".join(re.escape(ch) for ch in word)


# 长词在前，避免「境内法人」抢先匹配掉「境内非国有法人」的合法前缀。
# 「境内一般法人」实测于 300750 宁德时代中报（部分公司的用词，等价于境内非国有法人）。
_HOLDER_NATURES = (
    "境内非国有法人", "境内一般法人", "境外自然人", "境内自然人",
    "国有法人", "境外法人", "境内法人", "其他",
)

# 尾字缺失变体：PDF 偶把性质末字排到下一页首（实测 300308 半年报「章建平 境内 自然
# 〔数值列+页眉〕人 中信证券…」），导致该行整条匹配不上、股东凭空少一位。
# 为每个 ≥4 字的性质再注册一个「砍掉末字」的变体，命中后由映射补回全称。
_HOLDER_NATURE_FORMS: list[tuple[str, str]] = [(w, w) for w in _HOLDER_NATURES]
_HOLDER_NATURE_FORMS += [(w[:-1], w) for w in _HOLDER_NATURES if len(w) >= 4]
_HOLDER_NATURE_MAP = dict(_HOLDER_NATURE_FORMS)

_RE_HOLDER_ROW = re.compile(
    r"((?:[\u4e00-\u9fa5A-Za-z0-9（）()·－—.\-]|\s){1,96}?)\s+"
    r"(" + "|".join(_spaced(v) for v, _ in _HOLDER_NATURE_FORMS) + r")\s+"
    r"([\d.]+)%\s+([\d,]+)\s+"
    # 行结构变体（002384 半年报实测）：增减栏可整段缺省或为 不变/新进 等文字；
    # 纯无限售股东的限售栏缺省、质押状态为孤立「不适用」（无数量）；
    # group(8) 质押数量在非质押分支为 None，消费端须兜底 0。
    r"(?:(-?[\d,]+|不变|新进|未变)\s+)?"
    r"(?:([\d,]+|不适用)\s+)?"
    r"([\d,]+)\s+"
    # 末列质押状态整体可缺省：PDF 会合并空列，无质押的股东行可能根本没有该列
    # （实测 300750 宁德时代：无限售股东行止于持股数，无「质押/不适用」尾）。
    # 有质押数量则取数；「无质押」的写法因公司而异（不适用/冻结/无/未知/-，
    # 000001 平安银行用孤立 "-"）。
    r"(?:质押\s+([\d,]+)|(?:不适用|冻结|未知|无|-))?"
)

# 版式 B（沪市/红筹系/大盘股：601138 工业富联、600941 中国移动、600519 贵州茅台、
# 688981 中芯国际、688825 长鑫科技 等）：
# 股东性质排在**行末**，比例在持股数量之后，列序为
#   名称 |〔期内增减〕| 期末持股数量 | 比例% |〔限售数量〕|〔转融通限售数量〕|
#   质押状态 |〔质押数量〕| 股东性质
# 与版式 A（性质紧随名称）完全不同，须单列一条；抽取时按命中行数择优。
#
# 2026-09-16 增强（修复「增减列有值时整行失配」）：
#   ① 增减列原先被完全忽略，但实测 600941/600519/688981 的该列**大多有值**
#      （中国移动只抽到 5/10 行、贵州茅台 4/10 行）；现容忍「缺省 / 未知 / 数字 /
#      折行数字」（601138 的增减被排成「- 81,80 2,944」，导致第 7 名整行丢失）。
#      安全性：持股数与比例在 PDF 文字流里必以空白分隔，故 `([\d,]+)\s+([\d.]+)`
#      无法把「20.5」拆成「20」与「.5」，增减组不会误吞持股数。
#   ② 限售列可为 0~2 个（688825 同时披露「限售数量」与「包含转融通借出股份的限售
#      股份数量」两列）。
#   ③ 质押状态补「质押」（688981 有在押股东）；股东性质补「未知」（港股股东未披露）。
#   ④ 名称字符集补半角逗号：英文名「Robot Holding Co., Ltd.」含逗号，缺之则名称
#      捕获组只剩末词「Ltd.」。
_HOLDER_NATURES_B = _HOLDER_NATURES + ("未知",)
_RE_HOLDER_STATUS_B = "|".join(_spaced(w) for w in ("不适用", "未知", "质押", "无"))

_RE_HOLDER_ROW_B = re.compile(
    r"((?:[\u4e00-\u9fa5A-Za-z0-9（）()·－—.,\-]|\s){1,96}?)\s+"
    r"(?:(?:[-－—+]?\s*[\d,]+(?:\s+[\d,]+)*|未知)\s+)?"   # 报告期内增减（可缺省）
    r"([\d,]+)\s+"                                        # 期末持股数量
    r"([\d.]+)\s+"                                        # 比例(%)
    r"(?:([\d,]+|-)\s+)?"                                 # 限售数量
    r"(?:(?:[\d,]+|-)\s+)?"                               # 转融通限售数量（688825）
    r"(" + _RE_HOLDER_STATUS_B + r")\s+"                  # 质押状态
    r"(?:([\d,]+|-)\s+)?"                                 # 质押数量
    r"(" + "|".join(_spaced(w) for w in _HOLDER_NATURES_B) + r")"  # 股东性质
)


# 页眉噪声：定期报告分页会把「XX股份有限公司20XX年年度报告全文」+ 页码插进表格
# 文字流，恰落在上一行尾部与下一行名字之间（实测 300308 半年报/年报）。
_RE_REPORT_HEADER = re.compile(
    r"[\u4e00-\u9fa5]{2,}?(?:股份)?有限公司?\s*\d{4}\s*年\s*(?:半)?年度报告全文"
)
_RE_PAGE_NO = re.compile(r"-\s*\d{1,3}\s*-")
# 上一行股东性质的尾字残迹（「…境内 自然 人 中信证券…」）
_RE_NATURE_DANGLE = re.compile(
    r"^(?:" + _spaced("境内") + r"|" + _spaced("境外") + r")?"
    r"(?:" + _spaced("非国有") + r")?"
    r"(?:" + _spaced("自然人") + r"|" + _spaced("法人") + r"|人)\s+"
)
# 质押状态词（容忍字间空格——002384 年报把「不适用」排成「不适 用」）
_RE_LEAD_STATUS = re.compile(
    r"^(?:(?:"
    + "|".join(_spaced(w) for w in ("不适用", "冻结", "质押", "未知", "无"))
    + r")[\d,.\s\u3000\-－—]*)+"
)
# 清洗后仍超过此长度的名称几乎必然是跨页断裂混入多行残渣（真实最长名约 37 字，
# 如「中国工商银行股份有限公司－华泰柏瑞沪深300交易型开放式指数证券投资基金」）
_HOLDER_NAME_MAX = 40
# 只剩组织后缀 → 名字主体已被跨页拆走，属残渣而非股东名。
# 英文后缀同理：PDF 把「Robot Holding Co., Ltd.」逐词分页时，若名称捕获组只吃到
# 末词就会产出「Ltd.」这种脏名（实测 601138 工业富联半年报第 10 名）。
_RE_ORG_ONLY = re.compile(
    r"^(?:股份)?有限(?:责任)?公司$|^集团$|^中心$|^公司$|^有限合伙$|^合伙$"
    r"|^(?:co\.?,?\s*)?ltd\.?$|^limited$|^inc\.?$|^corp\.?$|^corporation$"
    r"|^company$|^holdings?$|^group$|^l\.?p\.?$|^llc$",
    re.IGNORECASE,
)
# 括号型组织后缀（容忍折行空格：「（有限 合伙）」实测于 300308 年报）。
# 其后仍有正文说明它属于**上一行**的名字，须取其后片段。不能对「有限公司」同样
# 处理——基金名本身就含「股份有限公司－XX基金」。
_RE_ORG_MARK = re.compile(r"[（(]\s*(?:有限|普通)\s*合伙\s*[）)]")
# 表头列名：name 捕获组会连带上游表头，按**最后一个**列名截断（先细后粗）。
# 必须逐字容忍空格——688825 表头被排成「质押、标 记或冻结 情况 股东 性质 股份 状态
# 数 量」，连续字面匹配全部失效，表头整段残留进首个股东名。
_HOLDER_HEAD_TAILS = (
    "持有无限售条件的股份数量", "持有无限售条件", "股份状态", "股东名称",
    "期末持股数量", "持股数量", "数量",
)
_HOLDER_HEAD_TAIL_RES = tuple(_spaced(t) for t in _HOLDER_HEAD_TAILS)


def _clean_holder_name(raw: str) -> tuple[str, str]:
    """把名册行的名称捕获组清洗成干净股东名，返回 ``(name, orphan)``。

    捕获组为容错取「性质枚举前的最长片段」，起点可能落在表头/页眉/上一行尾部，
    实测噪声：① 表头（「…股份状态 数量 山东中际投 资控股有限 公司」）；
    ② 页眉+页码（「…不适用 0 中际旭创股份有限公司2026 年半年度报告全文 - 47 - 人 中信证券股 份有限公司」）；
    ③ 前行残迹（「444 不适用 0 中国工商银 行股份有限 公司－…」）。
    一律按「取最后一段」剥离；剥离后仍带报告字样/页码 → 返回空名（宁缺勿编）。

    orphan 用于修复**跨页断裂**：页眉把「苏州云昌锦企业管理」与「中心（有限合伙）」
    拆到两页，后半跑到下一页首被本行捕获。当组织后缀前的词干极短（≤4 字，如「中心」）
    且后缀后还有正文时，判定该后缀归属上一行，摘出交由调用方回填。
    """
    s = str(raw or "")
    # 表头列名截断：取**所有**列名（逐字容忍空格）在本串中出现的最大结束位置，截取其后
    # 片段。不能用连续字面 rsplit——688825 表头折行成「股份 状态 数 量」，字面匹配全失效。
    _cut = 0
    for _p in _HOLDER_HEAD_TAIL_RES:
        for _m in re.finditer(_p, s):
            if _m.end() > _cut:
                _cut = _m.end()
    if _cut:
        s = s[_cut:]
    s = _RE_REPORT_HEADER.sub(" ", s)
    s = _RE_PAGE_NO.sub(" ", s)
    s = s.lstrip("0123456789. \u3000-－—").strip()
    # 上一行质押状态残迹：正则的「不适用/冻结」分支不消费其后的数量列，列被 PDF 合并后
    # 还可能连续堆叠（实测「未知未知贵州茅台…」「不适用香港中央结算有限公司」）。
    s = _RE_LEAD_STATUS.sub("", s)
    _mm = None
    for _m in _RE_ORG_MARK.finditer(s):
        _mm = _m
    if _mm is not None and len(s) - _mm.end() > 3:
        _stem = re.sub(r"\s+", "", s[:_mm.start()])
        _rest = s[_mm.end():].lstrip("0123456789 \u3000-－—")
        if 0 < len(_stem) <= 4:
            # 词干太短，只可能是上一行名字的末段（「…企业管理」被切断后的「中心」）
            _orphan = _stem + re.sub(r"\s+", "", _mm.group(0))
            _rest = re.sub(r"^[\u4e00-\u9fa5]\s+", "", _rest)
        else:
            _orphan = ""
        s = _rest
        s = _RE_NATURE_DANGLE.sub("", s)
        s = s.lstrip("0123456789 \u3000-－—").strip()
    else:
        _orphan = ""
    s = _RE_NATURE_DANGLE.sub("", s)
    s = s.lstrip("0123456789 \u3000-－—").strip()
    # 版式 B 的名称捕获会顺带吃进「报告期内增减」列（为数字或 "-"，折行还会把它拆成
    # 「- 81,80 2,944」）。真实股东名不会以「数字/连字符」结尾 → 剥离尾部该片段。
    s = re.sub(r"(?:[\s\u3000]*[-－—\d,]+)+[\s\u3000]*$", "", s).strip()
    # 名称归一：以**首字符**判定名称语种。
    #   中文名（首字符为汉字）→ 合并**全部**折行空格。「中国人寿保险股份有限公司
    #   －传统－普通保险产品－005L－CT001 沪」虽含拉丁字母与数字，本质仍是中文名，
    #   其空格全是 PDF 折行产物；旧规则因「含拉丁字母即保留空格」把这类名字留成
    #   「…有限公 司－…005L－CT001 沪」脏名（实测 002384/600519）。
    #   英文名（首字符为拉丁字母）→ 保留词间空格（MARK ZHAO / Lumiza Limited），
    #   仅归一连续空白（英文名内部折词无字典无法还原，属已知保守限制）。
    if re.match(r"[\u4e00-\u9fa5]", s):
        s = re.sub(r"\s+", "", s)
    else:
        s = re.sub(r"\s+", " ", s).strip()
    if "年度报告" in s or "半年度报告" in s or _RE_PAGE_NO.search(s):
        return "", ""
    if len(s) > _HOLDER_NAME_MAX:
        return "", ""
    # 只剩一个组织后缀说明整段名字都被跨页拆走了（实测「…63 有限公司」）
    if _RE_ORG_ONLY.match(s):
        return "", ""
    return s, _orphan


def extract_top_holders(pdf_path: str) -> list[dict]:
    """(A2 缓存壳) PDF 身份未变时直接返回上次解析结果，跳过 PyMuPDF 全文解析。"""
    try:
        f = EXTRACT_CACHE_DIR / f"holders_{_pdf_sig_of(pdf_path)}.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return data
    except Exception:
        pass
    rows = _extract_top_holders_impl(pdf_path)
    try:
        if rows:
            (EXTRACT_CACHE_DIR / f"holders_{_pdf_sig_of(pdf_path)}.json").write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return rows


def _extract_top_holders_impl(pdf_path: str) -> list[dict]:
    """从定期报告 PDF 抽取前十大股东名册（一手披露，含性质/比例/质押数量）。

    东财接口的「前十大股东」实为流通/快照口径（本公司实控人只显示 2.78% 的
    无限售部分），与定期报告名册（11.10%，含限售总持股）差异巨大。名册是
    监管披露口径，须以其为准。全部确定性解析；失败返回 []。
    """
    try:
        doc = fitz.open(pdf_path)
        flat = re.sub(r"\s+", " ", "\n".join(p.get_text() for p in doc))
    except Exception as e:  # noqa: BLE001
        logger.warning("[annual_report] 股东名册打开失败：%s", e)
        return []
    _m = _ANCHOR_HOLDER.search(flat)
    seg = flat[_m.end(): _m.end() + 4000] if _m else flat
    # 截断到「无限售条件股东持股情况」表（其后是流通口径表，行结构相似，
    # 混入会污染名册——名册表头「持有无限售条件的股份数量」不含「股东」后缀，
    # 以「无限售条件股东持股」作为下一表起点标记是安全的）
    _stop = seg.find("无限售条件股东持股")
    if _stop > 0:
        seg = seg[:_stop]
    # 双版式择优：A（性质紧随名称，深市主流）行数通常≥B；仅当 B 命中更多时才采用
    # （沪市红筹系的 A 版正则完全打不中，返回 0 行）。
    _rows_a = _parse_holder_rows(seg, _RE_HOLDER_ROW, layout="A")
    _rows_b = _parse_holder_rows(seg, _RE_HOLDER_ROW_B, layout="B")
    if len(_rows_b) > len(_rows_a):
        logger.info("[annual_report] 股东名册采用版式 B（性质列在行末）：%d 行", len(_rows_b))
        return _rows_b
    return _rows_a


def _parse_holder_rows(seg: str, rex, *, layout: str) -> list[dict]:
    """按给定行正则解析名册，返回最多 10 条股东记录。

    layout="A"：名称|性质|比例|持股数|增减|限售|无限售|质押状态|质押数量
    layout="B"：名称|〔增减〕|持股数|比例|〔限售〕|质押状态|〔质押数量〕|性质
    """
    out: list[dict] = []
    for r in rex.finditer(seg):
        name, _orphan = _clean_holder_name(r.group(1))
        if len(name) < 2:
            continue
        # 跨页断裂回填：本行头部摘出的孤儿后缀（「中心（有限合伙）」）实为上一行
        # 名字被页眉切掉的尾巴（上一行名不以「）」/「公司」收尾即为截断态）。
        if _orphan and out and not re.search(r"[）)]$|公司$", out[-1]["name"]):
            out[-1]["name"] += _orphan
        if layout == "B":
            _nature, _pct, _shares, _chg, _pledge = (
                r.group(7), r.group(3), r.group(2), None, r.group(6),
            )
        else:
            _nature, _pct, _shares, _chg, _pledge = (
                r.group(2), r.group(3), r.group(4), r.group(5), r.group(8),
            )
        _pl = (_pledge or "").replace(",", "")
        out.append({
            "name": name,
            "nature": _HOLDER_NATURE_MAP.get(
                re.sub(r"\s+", "", _nature), re.sub(r"\s+", "", _nature)
            ),
            "pct": float(_pct),
            "shares": int(_shares.replace(",", "")),
            "period_change": _chg,
            "pledge_num": int(_pl) if _pl.isdigit() else 0,
        })
        if len(out) >= 10:
            break
    return out


def _get_org_id(code: str) -> str | None:
    try:
        r = requests.get("http://www.cninfo.com.cn/new/data/szse_stock.json", timeout=20)
        r.raise_for_status()
        m = {i["code"]: i["orgId"] for i in r.json().get("stockList", [])}
        return m.get(code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[annual_report] 取 orgId 失败：%s", e)
        return None


def _find_cached_report(
    year: int | None = None, kind: str = "annual", code: str | None = None
) -> Path | None:
    """在缓存目录中查找已下载的定期报告 PDF（纯本地，不打网络）。

    code 指定时只命中文件名以「{code}_」开头的 PDF——**跨公司隔离**：
    缓存文件名含公司前缀，防止 A 公司的定期报告被 B 公司的报告生成命中
    （实测教训：旧版无前缀，生成长鑫科技时命中了力源信息缓存的
    「2026年半年度报告.pdf」，上下游/股东/实控人整节变成力源数据）。
    kind="annual"：文件名含「年度报告」且不含「半年度」；
    kind="semi"：文件名含「半年度报告」。
    year 指定时只匹配该年度；否则返回年份最新的一份（同年份取 mtime 更新者）。
    """
    if not CACHE_DIR.exists():
        return None
    prefix = f"{code}_" if code else ""
    cands: list[tuple[int, Path]] = []
    for p in CACHE_DIR.glob("*.pdf"):
        if prefix and not p.name.startswith(prefix):
            continue
        if kind == "semi":
            if "半年度报告" not in p.name:
                continue
        else:
            if "年度报告" not in p.name or "半年度" in p.name:
                continue
        m = re.search(r"(\d{4})年", p.name)
        if not m:
            continue
        try:
            cands.append((int(m.group(1)), p))
        except ValueError:
            continue
    if not cands:
        return None
    if year is not None:
        for _y, _p in cands:
            if _y == year:
                return _p
        return None
    cands.sort(
        # 年份优先；同年份**正文优先于摘要**（摘要不含客户/供应商前五名表、
        # 上下游与股东名册章节，命中摘要会让多个模块退化到年报口径——实测
        # 601138 只缓存了 2026 半年度报告摘要，上游一手来源退回 2025 年报）；
        # 最后才按 mtime 取更新者。
        key=lambda t: (
            t[0],
            0 if "摘要" in t[1].name else 1,
            t[1].stat().st_mtime if t[1].exists() else 0.0,
        ),
        reverse=True,
    )
    return cands[0][1]


def fetch_annual_report(
    code: str, year: int | None = None, force_refresh: bool = False,
    kind: str = "annual",
) -> dict | None:
    """取最新定期报告 PDF 的元信息（含本地缓存路径）。失败返回 None。

    year 指定时只取该年度的报告；否则取最新一期。
    force_refresh=True 时忽略本地缓存，强制重新联网查询并下载。
    kind="annual"：年度报告（cninfo category_ndbg_szsh）；
    kind="semi"：半年度报告（category_bndbg_szsh）——「上下游/客户集中度」等
    时效敏感模块优先取半年报（更晚披露），取不到再回退年报。

    P3-0 缓存短路：原实现先联网取 orgId(20s) + 查公告(30s)，再判断 PDF 是否已
    缓存——即使文件早已存在，也会因 cninfo read timeout 返回 None，导致重跑时
    「上下游一手来源」整节丢失。改为缓存命中即直接返回，全程零网络调用。
    """
    if not force_refresh:
        cached = _find_cached_report(year, kind=kind, code=code)
        if cached:
            _m = re.search(r"(\d{4})年", cached.stem)
            logger.info("[annual_report] 命中本地缓存，跳过网络查询：%s", cached.name)
            return {
                # 剥掉缓存文件名的 {code}_ 前缀——title 会展示在报告来源标注中
                "title": re.sub(r"^\d{6}_", "", cached.stem),
                "year": int(_m.group(1)) if _m else None,
                "pdf_path": str(cached),
                # 缓存命中时无 URL；下游（get_business_chain）只消费 pdf_path/title/year
                "pdf_url": "",
                "from_cache": True,
            }

    org = _get_org_id(code)
    if not org:
        return None
    stock_item = f"{code},{org}"
    payload = {
        "pageNum": "1", "pageSize": "30", "column": "szse", "tabName": "fulltext",
        "plate": "", "stock": stock_item, "searchkey": "", "secid": "",
        "category": (
            "category_bndbg_szsh" if kind == "semi" else "category_ndbg_szsh"
        ),
        "trade": "", "seDate": "",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    try:
        r = requests.post(_CNINFO_QUERY, data=payload, timeout=30)
        r.raise_for_status()
        anns = (r.json() or {}).get("announcements") or []
    except Exception as e:  # noqa: BLE001
        # cninfo 不可用（403/超时）时不再直接放弃：东财公告接口可匿名访问，
        # 作为兜底仍能取到定期报告正文（实测 cninfo 403 期间东财正常）。
        logger.warning("[annual_report] 查询公告失败，转东财兜底：%s", e)
        _em_pdf = _fetch_report_full_from_em(code, kind)
        if not _em_pdf:
            return None
        _m2 = re.search(r"(\d{4})年", _em_pdf.stem)
        _y2 = int(_m2.group(1)) if _m2 else None
        if year and _y2 != year:
            return None
        return {
            "title": re.sub(r"^\d{6}_", "", _em_pdf.stem),
            "year": _y2,
            "pdf_path": str(_em_pdf),
            "pdf_url": "",
            "from_cache": True,
        }

    # 「半年度报告」包含「年度报告」子串：annual 分支必须显式排除，防止串类
    if kind == "semi":
        targets = [
            a for a in anns if "半年度报告" in (a.get("announcementTitle") or "")
        ]
    else:
        targets = [
            a for a in anns
            if "年度报告" in (a.get("announcementTitle") or "")
            and "半年度" not in (a.get("announcementTitle") or "")
        ]
    # cninfo 查询为空、或只返回《…报告摘要》时，用东财公告兜底取正文：
    # 摘要不含「前十名股东」「前五名客户/供应商」等章节，命中摘要会让股东名册、
    # 上下游一手来源整体退回上一年度年报（实测 601138 上游标注成 2025 年报）。
    if not targets or all(
        "摘要" in (a.get("announcementTitle") or "") for a in targets
    ):
        _em_pdf = _fetch_report_full_from_em(code, kind)
        if _em_pdf:
            _m2 = re.search(r"(\d{4})年", _em_pdf.stem)
            _y2 = int(_m2.group(1)) if _m2 else None
            if not year or _y2 == year:
                return {
                    "title": re.sub(r"^\d{6}_", "", _em_pdf.stem),
                    "year": _y2,
                    "pdf_path": str(_em_pdf),
                    "pdf_url": "",
                    "from_cache": True,
                }
    if not targets:
        return None
    targets.sort(key=lambda a: a.get("announcementTime", 0) or 0, reverse=True)
    # 「摘要」与全文常同日披露且时间戳排序不稳定：摘要不含客户/供应商前五名表、
    # 上下游章节极简（曾致农业银行 supplement 误取摘要 → supplier_top5=null），
    # 故优先全文；仅当全文不存在时才回退摘要。
    _full = [a for a in targets if "摘要" not in (a.get("announcementTitle") or "")]
    if _full:
        targets = _full
    # 英文版公告不含「前十名股东」「前五名客户」等中文小节，解析全部扑空
    # （002384 实测误取《2026年半年度报告_英文版_》→ 名册抽取 0 条 →
    # 十大股东退化为东财十大流通股东口径），故优先中文版；
    # 仅当只剩英文版时保留原选择（有总比没有好）。
    _zh = [a for a in targets if "英文" not in (a.get("announcementTitle") or "")]
    if _zh:
        targets = _zh
    if year:
        hit = next((a for a in targets if str(year) in (a.get("announcementTitle") or "")), None)
        if not hit:
            return None
        targets = [hit]
    a = targets[0]

    title = a.get("announcementTitle") or f"{code}_annual"
    adjunct = a.get("adjunctUrl") or ""
    pdf_url = _STATIC + adjunct
    safe = re.sub(r"[^\w一-鿿.-]", "_", title)
    # 缓存文件名强制带 code 前缀（跨公司隔离，防止 A 公司命中 B 公司的报告缓存）
    pdf_path = CACHE_DIR / f"{code}_{safe}.pdf"

    if force_refresh or not pdf_path.exists():
        try:
            logger.info("[annual_report] 下载 %s -> %s", title, pdf_path)
            pr = requests.get(pdf_url, timeout=180, stream=True)
            pr.raise_for_status()
            tmp = pdf_path.with_suffix(".part")
            with open(tmp, "wb") as f:
                for ch in pr.iter_content(8192):
                    f.write(ch)
            tmp.replace(pdf_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("[annual_report] 下载失败：%s", e)
            return None

    yr = None
    m = re.search(r"(\d{4})年", title)
    if m:
        yr = int(m.group(1))
    return {
        "title": title,
        "year": yr,
        "pdf_path": str(pdf_path),
        "pdf_url": pdf_url,
        "from_cache": False,
    }


def _extract_makers(text: str) -> list[dict]:
    out = []
    for m in _RE_MAKER.finditer(text):
        en, cn, region = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if en and region:
            out.append({"en": en, "cn": cn, "region": region})
    return out


def _extract_products(text: str) -> list[dict]:
    out = []
    seen = set()
    for m in _RE_PRODUCT.finditer(text):
        term, definition = m.group(1).strip(), m.group(2).strip()
        if term in seen:
            continue
        seen.add(term)
        out.append({"term": term, "definition": definition})
    return out


def _flatten(text: str) -> str:
    """归一 PDF 排版：剥离页眉、单换行折行合并为空格（PDF 无空行分段）。"""
    t = _RE_PAGE_HEADER.sub(" ", text)
    return re.sub(r"\s+", " ", t).strip()


def _snap_window(flat: str, start: int, end: int, max_extend: int = 400) -> str:
    """把窗口起止吸附到句子边界，避免从半句话开头、或把列举名单拦腰截断。

    start 向前吸附到最近句末标点之后（最多回退 80 字，保留必要上下文）；
    end 向后吸附到最近句末标点（最多再延伸 max_extend 字，让客户名单这类
    长列举句能收完整）；找不到句末标点则硬截断并以「……」示意。
    """
    for p in ("。", "；", ";", "！", "？"):
        k = flat.rfind(p, max(0, start - 80), start)
        if k >= 0:
            start = k + 1
            break
    j = -1
    lim = min(len(flat), end + max_extend)
    for p in ("。", "；", ";", "！", "？"):
        k = flat.find(p, end, lim)
        if k >= 0 and (j < 0 or k < j):
            j = k
    if j >= 0:
        return flat[start: j + 1].strip()
    seg = flat[start:end].strip()
    return seg + "……" if end < len(flat) else seg


def _block_around(
    text: str,
    anchors: list[str],
    max_len: int = 800,
    pre: int = 120,
    per_page: int = 10,
) -> list[str]:
    """按锚点切上下文窗口。

    原实现有两个致命缺陷（实测证据：188 页年报只产出 8 个片段，客户名单整段丢失）：
      1. 用 `\\n\\s*\\n` 空行切段——但 PDF 文本整页只有**单换行**排版，全页被当作
         一个 block，切段完全失效；
      2. 长段落只取**首个**锚点切一个 max_len=240 的窗口——客户名单位于年报 p11
         偏移 677~1259，而首个上游锚点「上游」在偏移 300，窗口 220~540 把名单
         整个落在窗外。

    现策略：
      1. 先 _flatten 归一 PDF 排版折行；
      2. 收集**所有锚点的每一处**命中位置（而非仅首个）；
      3. 以每个命中点为中心切窗口，重叠窗口合并，起止吸附到句子边界；
      4. 每页最多保留 per_page 个窗口，过短（<40 字）丢弃。
    """
    flat = _flatten(text)
    if not flat:
        return []
    spans: list[list[int]] = []
    for k in anchors:
        for m in re.finditer(re.escape(k), flat):
            s = max(0, m.start() - pre)
            spans.append([s, min(len(flat), s + max_len)])
    if not spans:
        return []
    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out: list[str] = []
    for s, e in merged[:per_page]:
        seg = _snap_window(flat, s, e)
        if len(seg) >= 40:
            out.append(seg)
    return out


def _quote_brief(text: str, keys: list[str], limit: int = 420) -> str:
    """从完整窗口中按句抽提精华，供页面渲染（完整窗口仍留在 profile 供 LLM 消费）。

    直接截断前 N 字会砍掉真正的高价值句——客户名单常位于段落中段。故按句评分：
    含锚点词 +3、含顿号列举（>=3 个）+2，按原文顺序取够 limit 为止。
    """
    sents = [s for s in re.split(r"(?<=[。；;！？])", text) if s.strip()]
    if not sents:
        return text[:limit]
    scored = []
    for idx, s in enumerate(sents):
        score = sum(3 for k in keys if k in s) + (2 if s.count("、") >= 3 else 0)
        if score > 0:
            scored.append((score, idx, s))
    if not scored:
        return text[:limit] + ("……" if len(text) > limit else "")
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked: list[tuple[int, str]] = []
    total = 0
    for _sc, idx, s in scored:
        if total and total + len(s) > limit:
            continue
        picked.append((idx, s))
        total += len(s)
        if total >= limit:
            break
    return "".join(s for _i, s in sorted(picked)).strip()


# 产品线：仅保留与分销业务强相关的术语
_PROD_KEEP = [
    "电容", "晶体管", "半导体", "模块", "芯片", "MCU", "MOSFET", "MLCC", "IGBT",
    "BMS", "射频", "电池", "存储器", "处理器", "电阻", "电感", "传感器", "二极管",
]


def extract_business_chain(pdf_path: str) -> dict:
    """从年报 PDF 抽取上下游一手来源。返回结构化 dict（全部确定性解析）。"""
    doc = fitz.open(pdf_path)
    n = doc.page_count

    makers: list[dict] = []
    products: list[dict] = []
    up_cand: list[tuple[int, str]] = []
    down_cand: list[tuple[int, str]] = []
    field_pages: dict[str, int] = {}
    domestic_pages: dict[str, int] = {}
    seen_maker = set()

    for i in range(n):
        t = doc[i].get_text()
        # 原厂 / 产品线定义集中在「释义」页，全本扫描以兜底
        for mk in _extract_makers(t):
            key = (mk["en"], mk["region"])
            if key not in seen_maker:
                seen_maker.add(key)
                makers.append(mk)
        for pd in _extract_products(t):
            products.append(pd)
        # 上下游原文段落候选（带页码）
        for b in _block_around(t, _UP_ANCHORS):
            up_cand.append((i + 1, b))
        for b in _block_around(t, _DOWN_ANCHORS):
            down_cand.append((i + 1, b))
        # 下游应用领域标签：全本扫描，记录首次出现页码（最可靠）
        for f in _DOWN_FIELDS:
            if f in t and f not in field_pages:
                field_pages[f] = i + 1
        # 国产线/中文名原厂：通用列举句抽取，记录首次出现页码
        for dm in _extract_domestic_makers(t):
            if dm not in domestic_pages:
                domestic_pages[dm] = i + 1

    # 产品线：去重（按 term），仅保留分销业务强相关术语
    prods = []
    seen_p = set()
    for p in products:
        if p["term"] in seen_p:
            continue
        if any(k in p["definition"] for k in _PROD_KEEP):
            seen_p.add(p["term"])
            prods.append(p)
    prods = prods[:12]

    # 上游按国别分组
    by_region: dict[str, list[dict]] = {}
    for mk in makers:
        by_region.setdefault(mk["region"], []).append(mk)
    upstream_regions = []
    for region, lst in sorted(by_region.items(), key=lambda kv: -len(kv[1])):
        upstream_regions.append({
            "region": region,
            "count": len(lst),
            "makers": "、".join(f"{m['en']}（{m['cn']}）" for m in lst[:14]),
            "extra": max(0, len(lst) - 14),
        })

    # 上下游卡片：去重，优先含强锚点（上游/下游）的段落。
    # 次级排序用「顿号列举密度」——客户名单/产品线名单这类高信息密度段落的顿号
    # 可达数十个，远超普通叙述句，据此把名单卡排到最前（避免被业务回顾页挤掉）。
    def _dedupe(cands, strong, anchors, limit=4, keep_list=2):
        # 去重（同文本只留一次）
        seen = set()
        uniq = []
        for pg, txt in cands:
            if txt in seen:
                continue
            seen.add(txt)
            uniq.append((pg, txt))

        def _key(c):
            pg, txt = c
            # 护城河信号优先于强锚点，强锚点优先于普通锚点
            if any(s in txt for s in _MOAT_SIGNALS):
                tier = 0
            elif any(s in txt for s in strong):
                tier = 1
            else:
                tier = 2
            return (tier, -txt.count("、"), pg)

        def _card(c):
            pg, txt = c
            # text 保留完整窗口（进 profile，供 LLM 消费）；brief 供页面渲染，
            # 避免 1200 字的长窗口把引用区撑爆。
            return {"page": pg, "text": txt, "brief": _quote_brief(txt, anchors)}

        out: list[tuple[int, str]] = []
        # 阶段 1：名单类保底。客户名单 / 产品线名单是「下游卖给谁」的唯一一手来源，
        # 但它们未必命中护城河信号，纯按 tier 排序会被业务回顾、风险提示页挤掉
        # （实测：P12 智能电网客户、P33 AI 客户名单曾因此整体丢失）。
        by_density = sorted(uniq, key=lambda c: (-c[1].count("、"), c[0]))
        for c in by_density[:keep_list]:
            if c[1].count("、") >= 3:  # 确为列举句才保底
                out.append(c)
        # 阶段 2：按「护城河 > 强锚点 > 普通」补足到 limit
        rest = [c for c in uniq if c not in out]
        rest.sort(key=_key)
        for c in rest:
            out.append(c)
            if len(out) >= limit:
                break
        return [_card(c) for c in out]

    # 上游卡 limit=6（原 4）：上游卡只进 profile 供 LLM 消费、不直接渲染页面，
    # 扩到 6 张可容纳「核心竞争力分析」「产能/实验室」等此前被挤掉的实体壁垒表述。
    up_cards = _dedupe(up_cand, ["上游", "原厂", "代理权"], _UP_ANCHORS, 6, keep_list=2)
    down_cards = _dedupe(
        down_cand, ["下游", "应用领域", "客户结构", "客户行业", "客户"], _DOWN_ANCHORS, 4,
        keep_list=3,
    )

    # 下游应用领域标签（全本扫描结果，按 _DOWN_FIELDS 顺序）
    fields = [f for f in _DOWN_FIELDS if f in field_pages]
    # 把「AI」规范展示为「AI 算力」
    if "AI" in fields:
        fields = [("AI 算力" if f == "AI" else f) for f in fields]

    # 国产线/中文名原厂（通用列举句抽取，按原文出现顺序，附首次出现页码）。
    # 品牌与产品术语可能同句列举（「…天勤等上百家…原厂，代理分销的产品主要
    # 包括电容、电感、磁珠…」），与产品线术语（prods）求交排除品类词。
    _prod_terms = {p["term"] for p in prods}
    _prod_alias = set()
    for _p in prods:
        _prod_alias.update(re.findall(r"[\u4e00-\u9fa5]{2,10}", _p.get("definition") or ""))
    domestic_makers = [
        {"name": dm, "page": domestic_pages[dm]}
        for dm in sorted(domestic_pages, key=lambda k: domestic_pages[k])
        if dm not in _prod_terms and dm not in _prod_alias
    ]

    # 深度合作网络（一手来源）：
    #   A. 授权代理原厂（upstream_makers 释义）——合作方=原厂，关系=上游授权代理，
    #      合作内容=公司代理品类总述（区分度由 upstream 模块承担，此处不重复展开）；
    #   B. 联合/战略合作句（联合实验室、战略合作等通用锚词）——整句作合作内容。
    partnerships: list[dict] = []
    _prod_terms_all = "、".join(p.get("term", "") for p in prods[:8]) or "多品类元器件"
    for _mk in makers:
        partnerships.append({
            "partner": f"{_mk['en']}（{_mk['cn']}）" if _mk.get("cn") else _mk["en"],
            "relation": "上游芯片原厂 · 授权代理",
            "content": f"公司代理分销其芯片产品线（公司代理品类涵盖 {_prod_terms_all} 等）。",
            "named": True,
        })
    try:
        _full_txt = _flatten("\n".join(doc[i].get_text() for i in range(n)))
        partnerships.extend(_extract_partner_sents(_full_txt))
    except Exception as e:  # noqa: BLE001
        logger.warning("[annual_report] 合作句抽取失败：%s", e)

    # 证据句语料（供 LLM 增量抽取与护栏校验）：全文中与「合作/客户/供应商/
    # 原厂/经销/渠道/伙伴」相关的句子集合，去重限 80 句。只做句子级收集，
    # 不做结论——结论由 LLM 在护栏内提取或数据层确定性派生。
    _EV_KEYS = ("合作", "战略", "伙伴", "客户", "供应商", "原厂", "经销", "渠道", "签署", "协议")
    evidence_sentences: list[str] = []
    _ev_seen: set[str] = set()
    try:
        _full_txt2 = _flatten("\n".join(doc[i].get_text() for i in range(n)))
        for _raw_sent in re.split(r"[。；]", _full_txt2):
            _sent = _raw_sent.strip()
            if not (20 <= len(_sent) <= 220):
                continue
            if not any(k in _sent for k in _EV_KEYS):
                continue
            # 噪音过滤：纯表格数字行 / 承诺函 / 审计表述
            if _sent.count("，") < 1 and _sent.count("、") < 1:
                continue
            if any(nz in _sent for nz in ("承诺", "函证", "审计", "截止测试")):
                continue
            _k = _sent[:40]
            if _k in _ev_seen:
                continue
            _ev_seen.add(_k)
            evidence_sentences.append(_sent)
            if len(evidence_sentences) >= 80:
                break
    except Exception as e:  # noqa: BLE001
        logger.warning("[annual_report] 证据句收集失败：%s", e)

    # 供应商集中度（年报「主要供应商情况」表）：前五名合计采购金额与占比 + 第一名
    supplier_top5 = None
    try:
        _full = _flatten("\n".join(doc[i].get_text() for i in range(n)))
        _m5 = _RE_SUP_TOP5.search(_full)
        if _m5:
            # 金额单位：括号内单位或数字后单位，缺省为元——统一折算成元再换亿
            _sup_unit = _m5.group(1) or _m5.group(3) or "元"
            _amt = float(_m5.group(2).replace(",", "")) * _UNIT_FACTOR.get(_sup_unit, 1.0)
            supplier_top5 = {
                "top5_amount": _amt,
                "top5_amount_yi": round(_amt / 1e8, 2),
                "top5_pct": float(_m5.group(4)),
            }
            _m1 = _RE_SUP_NO1.search(_full, _m5.end(), _m5.end() + 600)
            if _m1:
                _n1 = float(_m1.group(1).replace(",", "")) * _UNIT_FACTOR.get(_sup_unit, 1.0)
                supplier_top5["no1_amount"] = _n1
                supplier_top5["no1_amount_yi"] = round(_n1 / 1e8, 2)
                supplier_top5["no1_pct"] = float(_m1.group(2))
    except Exception as e:  # noqa: BLE001
        logger.warning("[annual_report] 供应商集中度抽取失败：%s", e)

    # 客户集中度（年报「主要客户情况」表）：结构与供应商表对称
    customer_top5 = None
    try:
        _full2 = _flatten("\n".join(doc[i].get_text() for i in range(n)))
        _c5 = _RE_CUST_TOP5.search(_full2)
        if _c5:
            # 金额单位：括号内单位或数字后单位（明细行单位与合计行一致），统一折算成元
            _cust_unit = _c5.group(1) or _c5.group(3) or "元"
            _camt = float(_c5.group(2).replace(",", "")) * _UNIT_FACTOR.get(_cust_unit, 1.0)
            customer_top5 = {
                "total": _camt,
                "total_yi": round(_camt / 1e8, 2),
                "pct": float(_c5.group(4)),
                "rows": [],
            }
            for _cr in _RE_CUST_ROW.finditer(
                _full2, _c5.end(), _c5.end() + 800
            ):
                if len(customer_top5["rows"]) >= 5:
                    break  # 窗口内第 6 行起是紧随其后的供应商表，严禁混入
                _row_amt = float(_cr.group(3).replace(",", "")) * _UNIT_FACTOR.get(_cust_unit, 1.0)
                customer_top5["rows"].append({
                    "rank": int(_cr.group(1)),
                    "amount": _row_amt,
                    "amount_yi": round(_row_amt / 1e8, 2),
                    "pct": float(_cr.group(4)),
                })
            # 茅台等公司只披露合计、无明细行——保留合计（total/pct），行空由渲染层处理
    except Exception as e:  # noqa: BLE001
        logger.warning("[annual_report] 客户集中度抽取失败：%s", e)

    return {
        "upstream_makers": makers,
        "partnerships": partnerships,
        "evidence_sentences": evidence_sentences,
        "upstream_regions": upstream_regions,
        "upstream_products": prods,
        "upstream_raw": up_cards,
        "downstream_raw": down_cards,
        "downstream_fields": fields,
        "downstream_field_pages": field_pages,
        "domestic_makers": domestic_makers,
        "supplier_top5": supplier_top5,
        "customer_top5": customer_top5,
    }


def get_business_chain(code: str) -> dict | None:
    """(A2 缓存壳) 公司已缓存 PDF 指纹未变时直接返回上次解析结果。"""
    cached = _extract_cache_get("chain", code)
    if cached is not None:
        return cached
    res = _get_business_chain_impl(code)
    if res:
        _extract_cache_put("chain", code, res)
    return res


def _get_business_chain_impl(code: str) -> dict | None:
    """一站式：取最新定期报告 + 解析。返回 {report, chain} 或 None（降级）。

    时效优先级：最新**半年度报告**（披露更晚、客户结构更新）→ 最新**年度报告**。
    半年报为主时，另解析年报并挂到 `annual_supplement`（半年报不披露前五名
    客户/供应商集中度表，该部分回用年报真值并注明时点；下游客户表两期合并、
    以最新期为主）。`report.title` 标注主来源，供页面注明数据时点。
    """
    def _parse(meta):
        try:
            chain = extract_business_chain(meta["pdf_path"])
        except Exception as e:  # noqa: BLE001
            logger.warning("[annual_report] 解析失败：%s", e)
            return None
        chain["report"] = {"title": meta["title"], "year": meta["year"]}
        return chain

    meta_semi = fetch_annual_report(code, kind="semi")
    if meta_semi:
        chain = _parse(meta_semi)
        if chain and (chain.get("downstream_raw") or chain.get("upstream_regions")):
            meta_ann = fetch_annual_report(code)
            if meta_ann and meta_ann["pdf_path"] != meta_semi["pdf_path"]:
                ann = _parse(meta_ann)
                if ann:
                    chain["annual_supplement"] = {
                        "report": {"title": ann.get("report", {}).get("title"), "year": ann.get("report", {}).get("year")},
                        "supplier_top5": ann.get("supplier_top5"),
                        "customer_top5": ann.get("customer_top5"),
                        "downstream_raw": ann.get("downstream_raw") or [],
                        "partnerships": ann.get("partnerships") or [],
                    }
                    # 深度合作年报补充：合作披露分布不均（如宁德仅在年报披露
                    # 「与一汽解放、北汽福田、东风商用车等深化战略合作」），
                    # 半年报无锚词句时用年报条目补位，按 partner 去重。
                    _ann_pt = ann.get("partnerships") or []
                    if _ann_pt:
                        _seen = {p.get("partner") for p in (chain.get("partnerships") or [])}
                        chain["partnerships"] = (chain.get("partnerships") or []) + [
                            p for p in _ann_pt if p.get("partner") not in _seen
                        ]
                    # 证据句语料同步合并年报独有句（LLM 增量抽取的护栏语料
                    # 必须覆盖年报——否则年报补充的合作条目会被实体校验误杀）
                    _ann_ev = ann.get("evidence_sentences") or []
                    if _ann_ev:
                        _have = set(chain.get("evidence_sentences") or [])
                        chain["evidence_sentences"] = (
                            list(chain.get("evidence_sentences") or [])
                            + [x for x in _ann_ev if x not in _have]
                        )[:100]
            logger.info(
                "[annual_report] 上下游/客户数据采用最新半年报：%s（年报作补充期）",
                meta_semi["title"],
            )
            return chain
    meta = fetch_annual_report(code)
    if not meta:
        return None
    chain = _parse(meta)
    return chain


def _normalize_spaced(text: str) -> str:
    """合并 PDF 抽取里被空格打散的数字（如 '1,333,333 股' -> '1,333,333股'），便于正则捕获。"""
    return re.sub(r"(?<=\d)\s+(?=\d)", "", text)


def extract_dividend_decision(
    pdf_path: str, period_label: str | None = None
) -> list[dict]:
    """从定期报告 PDF 抽取最新一期利润分配/转增决策。

    cninfo 分红接口缺 2025 条目（仅到 2018），但定期报告正文明确载有当年分配方案，
    属一手来源，须据此兜底，避免把历史方案（如 2014「10转增10股」）误当作最新渲染。
    period_label 由调用方按所用报告传入（如「2026半年报」「2025年报」）；未传时
    从文本推断（「截至 YYYY 年 12 月 31 日」→ 年报；「半年度」表述 → 半年报），
    最后才落默认值——绝不把半年报决策误挂到年报报告期。
    """
    try:
        doc = fitz.open(pdf_path)
        txt = _normalize_spaced("".join(p.get_text() for p in doc))
    except Exception as e:  # noqa: BLE001
        logger.warning("[annual_report] 分红抽取失败：%s", e)
        return []
    m = re.search(
        r"公司计划(?:半年度)?不派发现金红利[，,]\s*不送红股[，,]\s*不以公积金转增股本", txt
    )
    if not m:
        return []
    if period_label:
        label = period_label
    else:
        # 决策附近的「截至 YYYY 年 12 月 31 日」确定报告年度
        yr = None
        for mm in re.finditer(
            r"截至\s*(\d{4})\s*年\s*12\s*月\s*31\s*日",
            txt[max(0, m.start() - 600):m.start() + 80],
        ):
            yr = mm.group(1)
        label = f"{yr or '2025'}年报"
    div_type = "半年度分红" if "半年" in label else "年度分红"
    return [{
        "报告期": label,
        "分红类型": div_type,
        "现金分红比例": None,
        "股权登记日": "",
        "除权除息日": "",
        "方案说明": "不派发现金红利、不送红股、不以公积金转增股本"
        "（母公司未弥补亏损，以回购注销视同现金分红）",
    }]


# 年报回购表格的固定表头里含「股权激励」字样，但它描述的是「占股权激励计划所涉及
# 标的股票的比例」这一**列定义**，与本公司回购用途毫无关系。朴素关键词匹配会因此把
# 所有公司误判为股权激励（601138 实测：两轮方案实际用途均为"减少注册资本"，却因
# 表头命中被判成"股权激励"）。故判定用途前必须先剥离该表头噪声。
_REPURCHASE_HEADER_NOISE = re.compile(
    r"已\s*回购\s*数量\s*占[\s\S]{0,40}?比\s*例"
    r"|占\s*股权\s*激励\s*计划\s*所\s*涉及\s*的\s*标的\s*股票\s*的\s*比例"
)

# 逐轮切分锚点：兼容纵向标签版式「回购股份方案披露时间」（601138）与横向表头版式
# 「方案披露时间」（002384 / 300184，PyMuPDF 压缩后常夹空格）。
_REPURCHASE_ROUND_ANCHOR = re.compile(r"(?:回购股份)?方案披露\s*时\s*间")


def _to_int(s: str) -> int | None:
    """千分位数字串 → int；解析失败返回 None。"""
    try:
        return int(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _classify_repurchase_purpose(seg: str) -> str:
    """回购用途归类（泛化，不写死任何公司的措辞）。

    先剥离表格固定表头噪声，再按落地表述判定；「注销」在我国必然伴随注册资本减少，
    故单独出现时仍归为注销并减少注册资本。
    """
    seg = _REPURCHASE_HEADER_NOISE.sub(" ", seg)
    if "减少注册资本" in seg or ("注销" in seg and "注册资本" in seg):
        return "注销并减少注册资本"
    if "注销" in seg:
        return "注销并减少注册资本"
    if "员工持股" in seg and "股权激励" in seg:
        return "员工持股计划/股权激励"
    if "员工持股" in seg:
        return "员工持股计划"
    if "股权激励" in seg:
        return "股权激励"
    return ""


def _split_repurchase_rounds(block: str) -> list[str]:
    """按「（回购股份）方案披露时间」把回购章节切成逐轮段落。

    年报常并排列示多轮方案（601138 2025 年报同时含 2024-07-31 与 2025-04-15 两轮），
    若整段共用一次取值，会出现「日期取自轮 A、数量取自轮 B」的跨轮错配，拼出两个
    轮次都不成立的伪数据。切分后每轮的日期/数量/用途由该轮段落内部自洽解析。
    """
    idx = [m.start() for m in _REPURCHASE_ROUND_ANCHOR.finditer(block)]
    if not idx:
        return [block]
    return [
        block[st : (idx[k + 1] if k + 1 < len(idx) else len(block))]
        for k, st in enumerate(idx)
    ]


def extract_repurchase_semi(pdf_path: str) -> dict | None:
    """从半年度/年度报告抽取**最新一轮**回购实施结果 + 报告期内回购最新进展（一手来源）。

    ① 完整轮次：半年报会同时列示历史多轮回购（如 2025-12 那轮 2,979,800 股与
       2026-06 那轮 1,973,093 股），须取完成日期最新的一轮，否则报告会停在旧数据。
       行文模式：「回购方案已于 YYYY 年 M 月 D 日实施完毕，公司累计回购股份数量为
       X 股，于 YYYY 年 M 月 D 日……完成……注销」。
    ② 报告期内进展（无完整轮次句式时的兜底信号，实测 002384 2026H1）：
       - 库存股附注：「公司于 YYYY 年 M 月 D 日以非交易过户方式将回购证券账户中的
         X 股过户至……员工持股计划」；
       - 现金流量表：「股份回购支出」本期发生额（合并报表在前，取首次命中）。
    返回 dict（可同时含轮次字段与进展字段 esop_transfer / h1_cash_spent_yi），
    两者皆无时返回 None（宁缺勿编）。
    """
    try:
        doc = fitz.open(pdf_path)
        txt = _normalize_spaced("".join(p.get_text() for p in doc))
    except Exception as e:  # noqa: BLE001
        logger.warning("[annual_report] 半年报回购抽取失败：%s", e)
        return None
    best = None  # (completed_tuple, executed, done_tuple)
    # ① 年报式行文：「回购方案已于 YYYY 年 M 月 D 日实施完毕，公司累计回购股份数量为
    #    X 股，于 YYYY 年 M 月 D 日……完成注销」
    for m in re.finditer(
        r"回购方案已于\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日实施完毕[，,]"
        r"公司累计回购股份数量为\s*([\d,]+)\s*股[，,]?于?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        txt,
    ):
        done = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        comp = (int(m.group(5)), int(m.group(6)), int(m.group(7)))
        if best is None or comp > best[0]:
            best = (comp, int(m.group(4).replace(",", "")), done)

    # ①' 半年报式行文（601138 2026H1 实测）：「截至 YYYY 年 M 月 D 日，本次回购已实施
    #     完毕。……累计回购（公司股份数量）X 股」。原 ① 要求「回购方案已于…」前缀，
    #     该表述会整体漏配 → 函数返回 None → 上层回退年报，报告因此停在旧数据。
    #     注意「已实施完毕。」后跨句号，须用 [\s\S] 而非 [^。]。
    for m in re.finditer(
        r"截至\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[，,]?\s*本次回购已实施完毕"
        r"[\s\S]{0,150}?累计回购\s*(?:公司股份数量)?\s*([\d,]+)\s*股",
        txt,
    ):
        comp = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if best is None or comp > best[0]:
            best = (comp, int(m.group(4).replace(",", "")), comp)

    # —— 报告期内进展信号（与完整轮次独立；原文含换行/空格，数字与单位间须容 \s*）——
    _esop = re.search(
        r"公司于\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日以非交易过户方式"
        r"将回购证券账户中的\s*([\d,]+)\s*股过户至\s*([^。，]{0,40}?员工持股计划)",
        txt,
    )
    _cash = re.search(r"股份回购支出\s*([\d,]+(?:\.\d+)?)", txt)

    if not best and not _esop and not _cash:
        return None

    out: dict = {
        "year": "",
        "plan_shares": "",
        "executed_shares": None,
        "executed_shares_wan": None,
        "purpose": "",
        "disclosure_date": "",
        "completed_date": "",
        "text": "",
    }
    if best:
        comp, executed, _done = best
        completed = f"{comp[0]:04d}-{comp[1]:02d}-{comp[2]:02d}"
        wan = round(executed / 1e4, 2)
        # 成交总金额（就近窗口内）
        _amt = re.search(r"成交总金额为\s*([\d,\.]+)\s*元", txt)
        amt_txt = ""
        if _amt:
            try:
                amt_yi = float(_amt.group(1).replace(",", "")) / 1e8
                amt_txt = f"，成交总金额 {amt_yi:.2f} 亿元"
            except ValueError:
                pass
        # 用途：优先在「截至…累计回购」窗口内判定（半年报常无「回购用途」字段，但会写
        # 「注销本次所回购的股份」等落地表述），窗口抽不到再退全文；两者都先剥离表头噪声。
        # 旧实现固定写死「已全部注销并用于减少注册资本」，对股权激励类回购会失真。
        _pw = ""
        _mw = re.search(
            r"截至\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日[，,][\s\S]{0,200}?累计回购", txt
        )
        if _mw:
            _pw = txt[_mw.start() : _mw.start() + 400]
        purpose = _classify_repurchase_purpose(_pw) or _classify_repurchase_purpose(txt)
        out.update({
            "year": str(comp[0]),
            "executed_shares": executed,
            "executed_shares_wan": wan,
            "purpose": purpose,
            "completed_date": completed,
            "source": "半年报",
            "text": (
                f"{comp[0]} 年回购方案于 {completed} 实施完毕，累计回购 "
                f"{executed:,} 股（约 {wan:,.2f} 万股）{amt_txt}"
                + (f"，用于{purpose}。" if purpose else "。")
            ),
        })

    # —— 进展字段与叙述追加 ——
    _prog_txt = ""
    if _esop:
        _ey, _em, _ed = int(_esop.group(1)), int(_esop.group(2)), int(_esop.group(3))
        _esh = int(_esop.group(4).replace(",", ""))
        _eto = _esop.group(5).strip()
        out["esop_transfer"] = {
            "date": f"{_ey:04d}-{_em:02d}-{_ed:02d}",
            "shares": _esh,
            "to": _eto,
        }
        _prog_txt += (
            f"{_ey} 年 {_em} 月 {_ed} 日将回购证券账户中的 {_esh:,} 股"
            f"过户至{_eto}；"
        )
    if _cash:
        try:
            _amt_f = float(_cash.group(1).replace(",", ""))
        except ValueError:
            _amt_f = None
        if _amt_f:
            out["h1_cash_spent_yi"] = round(_amt_f / 1e8, 2)
            _prog_txt += (
                f"报告期内股份回购支出 {_amt_f / 1e8:.2f} 亿元（现金流量表口径）。"
            )
    if _prog_txt:
        _base = str(out.get("text") or "").rstrip()
        out["text"] = (
            _base + (" 最新进展：" + _prog_txt if _base else _prog_txt)
        ).strip()
    return out


def extract_repurchase(pdf_path: str) -> dict | None:
    """从年报 PDF 抽取报告期股份回购实施情况（一手来源），按轮次逐轮解析后取最新一轮。

    年报「股份回购在报告期的具体实施情况」常并排列示多轮方案（601138 2025 年报同时含
    2024-07-31 与 2025-04-15 两轮）。旧实现整段共用一次取值：日期取段内首个、数量取段内
    最后一个 ≥5 位数字，于是把轮 A 的日期配到轮 B 的数量上，拼出两个轮次都不成立的伪数据。
    现按「（回购股份）方案披露时间」切分逐轮，每轮的日期/数量/用途在段内自洽解析，
    最后取披露时间最新的一轮。

    单轮内数量按四级优先解析（见 _executed_of）：正文「公司回购股份 X 股」→ 结构化字段
    「已回购数量（股）X 股」→ 正文「累计回购（公司股份数量）X 股」→ 表格压缩版式兜底
    （剔除区间值后取表尾数字）。用途判定先剥离表格固定表头噪声——「已回购数量占股权激励
    计划所涉及标的股票的比例」含"股权激励"字样，会让所有公司被误判为股权激励用途。
    全部未命中返回 None（宁缺勿编）。
    """
    try:
        doc = fitz.open(pdf_path)
        txt = _normalize_spaced("".join(p.get_text() for p in doc))
    except Exception as e:  # noqa: BLE001
        logger.warning("[annual_report] 回购抽取失败：%s", e)
        return None
    i = txt.find("股份回购的实施进展情况")
    if i < 0:
        i = txt.find("股份回购在报告期的具体实施情况")
    if i < 0:
        return None
    block = txt[i:i + 1200]
    # 截断：到「减持回购股份」小节或下一章节标题为止，防止把相邻节数字误当已回购量
    for _cut in ("减持回购股份的实施进展", "五、优先股", "利润分配", "第七节"):
        _j = block.find(_cut, 30)
        if _j > 0:
            block = block[:_j]

    def _executed_of(seg: str) -> int | None:
        """单轮段落内解析「已回购数量」，四级优先、逐级兜底（宁缺勿编）。"""
        # ① 正文句式「公司回购股份 X 股」（300184 等）
        exe = re.search(r"公司回购股份\s*([\d,]+)\s*股", seg)
        if exe:
            return _to_int(exe.group(1))
        # ② 结构化字段「已回购数量（股）X 股」（601138 纵向标签-值版式）
        _f = re.search(r"已\s*回购\s*数量\s*[（(]?\s*股\s*[）)]?\s*([\d,]+)\s*股", seg)
        if _f:
            return _to_int(_f.group(1))
        # ③ 正文「累计回购（公司股份数量）X 股」
        _c = re.search(r"累计回购\s*(?:公司股份数量)?\s*([\d,]+)\s*股", seg)
        if _c:
            return _to_int(_c.group(1))
        # ④ 表格压缩版式兜底（002384 横向表头，标签与数据分行）：剔除区间值
        #    （拟回购数量/金额「X-Y」）后取最后一个 ≥5 位数字（已回购数量列位于表尾）
        if "已回购数量" in seg:
            _no_range = re.sub(r"\d[\d,]*\s*-\s*\d[\d,]*", " ", seg)
            _cands = re.findall(r"\d{1,3}(?:,\d{3})+|\d{5,}", _no_range)
            if _cands:
                return _to_int(_cands[-1])
        return None

    # —— 逐轮解析：同一轮的日期/数量/用途必须同源，否则会拼出跨轮伪数据 ——
    parsed: list[dict] = []
    for _order, seg in enumerate(_split_repurchase_rounds(block)):
        executed = _executed_of(seg)
        if not executed:
            continue
        _dm = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", seg)
        disclosure = (
            f"{_dm.group(1)}-{int(_dm.group(2)):02d}-{int(_dm.group(3)):02d}"
            if _dm
            else ""
        )
        # 该轮是否已实施完毕（如 601138 轮 A「截至2025年2月7日…实施完毕」）
        _cm = re.search(
            r"截至\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日[^。]{0,60}?回购[^。]{0,30}?实施完毕",
            seg,
        )
        completed = (
            f"{_cm.group(1)}-{int(_cm.group(2)):02d}-{int(_cm.group(3)):02d}"
            if _cm
            else ""
        )
        # 拟回购数量：仅认原文「…股-…股」句式。表格压缩版式中数量与比例数字会粘连
        # （如 2050000-41000000.12%），通用区间兜底会抽出错误数字，故不做兜底（宁缺勿编）。
        plan = re.search(
            r"拟回购股份数量[（(]股[）)]?\s*([\d,]+\s*股\s*-\s*[\d,]+股)", seg
        )
        parsed.append({
            "_order": _order,
            "year": disclosure[:4],
            "plan_shares": plan.group(1).replace(" ", "") if plan else "",
            "executed_shares": executed,
            "executed_shares_wan": round(executed / 1e4, 2),
            "purpose": _classify_repurchase_purpose(seg),
            "disclosure_date": disclosure,
            "completed_date": completed,
        })
    if not parsed:
        return None
    # 取披露时间最新的一轮；披露时间同为空时退化为出现顺序最后（表格通常旧轮在前）。
    best = max(parsed, key=lambda r: (r["disclosure_date"], r["_order"]))
    best.pop("_order", None)
    wan = best["executed_shares_wan"]
    _seg_txt = (
        f"公司于 {best['disclosure_date']} 披露回购方案，" if best["disclosure_date"] else ""
    )
    _plan_seg = f"，拟回购 {best['plan_shares']}" if best["plan_shares"] else ""
    _purp_seg = f"，用途为{best['purpose']}" if best["purpose"] else ""
    best["source"] = "年度报告"
    # 拟回购数量抽取不到时（年报表格版式差异）整句省略，避免出现「拟回购 ，」这类空洞
    best["text"] = (
        f"{_seg_txt}报告期内累计回购 {best['executed_shares']:,} 股"
        f"（约 {wan:,.2f} 万股）{_plan_seg}{_purp_seg}。"
        "（一手来源：年度报告「股份回购在报告期的具体实施情况」）"
    )
    return best


def extract_actual_controller(pdf_path: str) -> dict | None:
    """从年报抽取实际控制人（一手来源）：姓名、性质、持股比例/数量、名次、一致行动人。

    为何必须补：概况报告此前只呈现东财前十大股东的**最新快照**直接持股
    （MARK ZHAO 2.78%、列第 2），完全没有年报口径的实控人控制比例
    （MARK ZHAO 11.08%、127,564,160 股、列第 1），弱化了「自然人低比例控股」
    这一核心治理识别。两者并非矛盾，而是**报告期不同**（年报 2025-12-31 vs
    快照 2026H1），故年报口径以一手来源单列，并在渲染层注明口径差异。

    注：WorkBuddy 参考样本写的 11.10% 与年报原文 11.08% 差 0.02%，
    以年报一手来源为准（11.08%）。
    """
    try:
        doc = fitz.open(pdf_path)
        txt = _normalize_spaced("".join(p.get_text() for p in doc))
    except Exception as e:  # noqa: BLE001
        logger.warning("[annual_report] 实控人抽取失败：%s", e)
        return None

    # 1) 姓名与性质：「公司实际控制人及其一致行动人」节的通用「名（EN 名）」模式。
    # 旧版写死「赵马克/MARK ZHAO」，只适配单一公司；改为通用抽取——
    # 在实控人章节附近找任意「中文名（英文名）」对，并要求该名出现在
    # 「实际控制人」上下文中。抽不到则放弃（返回 None），绝不猜测。
    i = txt.find("公司实际控制人及其一致行动人")
    block = txt[i : i + 1600] if i >= 0 else ""
    _m_nm_pat = re.compile(r"([\u4e00-\u9fa5]{2,4})\s*[（(]\s*([A-Z][A-Za-z\s]{1,30})\s*[）)]")
    m_nm = None
    for cand in (_m_nm_pat.finditer(block) if block else []):
        m_nm = cand
        break
    if not m_nm:
        # 兜底：全文找实控人表述句中的「名（EN）」
        for m_c in re.finditer(
            r"实际控制人[^。；]{0,60}?([\u4e00-\u9fa5]{2,4})\s*[（(]\s*([A-Z][A-Za-z\s]{1,30})\s*[）)]",
            txt,
        ):
            m_nm = m_c
            break
    nature = ""
    m_nat = re.search(r"实际控制人性质[：:]\s*([一-龥]{2,8})", block)
    if m_nat:
        nature = m_nat.group(1)

    # 2) 持股：前十大股东表中按姓名匹配（口径 = 报告期末）。姓名取英文 key
    #（名册与十大表的英文名无空格差异用压缩比对）
    if not m_nm:
        return None
    _cn, _en = m_nm.group(1), re.sub(r"\s+", "", m_nm.group(2))
    m_hold = None
    m0_end = None
    # 实控人姓名在全文出现多次（释义页/十大表/关联方章节）；只有十大表中的
    # 出现位置其后紧跟「X% N股」。遍历所有出现位置，取第一个命中的。
    if _en:
        _pat = r"\s*".join(re.escape(ch) for ch in _en)
        for _m in re.finditer(_pat, txt):
            _mh = re.search(r"([\d]+\.\d+)\s*%\s*([\d,]+)", txt[_m.end(): _m.end() + 120])
            if _mh:
                m_hold, m0_end = _mh, _m.end()
                break
    if not m_hold and _cn:
        for _m in re.finditer(re.escape(_cn), txt):
            _mh = re.search(r"([\d]+\.\d+)\s*%\s*([\d,]+)", txt[_m.end(): _m.end() + 120])
            if _mh:
                m_hold, m0_end = _mh, _m.end()
                break
    if not m_hold:
        return None
    try:
        pct = float(m_hold.group(1))
        shares = int(m_hold.group(2).replace(",", ""))
    except ValueError:
        return None

    # 名次：表内按持股比例降序，统计该行之前已出现几个「X% N股」条目
    _head = txt[: m0_end]
    _tb = _head.rfind("股东名称")
    _tb = _tb if _tb >= 0 else 0
    rank = len(re.findall(r"[\d]+\.\d+\s*%\s*[\d,]+", txt[_tb : m0_end])) + 1

    # 3) 一致行动人：通用句式「（实控人名）一致行动人」前的人名
    acting = []
    for m_a in re.finditer(
        r"([一-龥]{2,4})\s*公司实际控制人[（(][一-龥A-Za-z\s]{2,20}[）)]一致行动人", txt
    ):
        if m_a.group(1) not in acting:
            acting.append(m_a.group(1))

    # 报告期：优先取本报告标题年份（年报=12-31）
    _ym = re.search(r"(\d{4})年年度报告", txt[:200] + pdf_path)
    as_of = f"{_ym.group(1)}-12-31" if _ym else ""

    return {
        "name": f"{_cn}（{_en}）",
        "name_cn": _cn,
        "name_en": _en,
        "nature": nature,
        "pct": pct,
        "shares": shares,
        "shares_wan": round(shares / 1e4, 2),
        "rank": rank,
        "acting_in_concert": acting,
        "as_of": as_of,
        "source": "定期报告 · 前十大股东",
    }


def get_corporate_actions(code: str) -> dict | None:
    """(A2 缓存壳) 公司已缓存 PDF 指纹未变时直接返回上次抽取结果。"""
    cached = _extract_cache_get("actions", code)
    if cached is not None:
        return cached
    res = _get_corporate_actions_impl(code)
    if res:
        _extract_cache_put("actions", code, res)
    return res


def _get_corporate_actions_impl(code: str) -> dict | None:
    """一站式：抽取分红决策、回购、实际控制人。返回 dict 或 None。

    时效优先：最新**半年度报告**（分红决策 + 最新一轮回购）→ 年报回退。
    实际控制人认定（含一致行动人）章节仅在年报，controller 仍取年报口径；
    其持股比例/股数由渲染层用最新名册口径同步（见 overview.py）。
    """
    meta_semi = fetch_annual_report(code, kind="semi")
    if meta_semi:
        try:
            div = extract_dividend_decision(
                meta_semi["pdf_path"], period_label=f"{meta_semi.get('year')}半年报"
            )
            rep = extract_repurchase_semi(meta_semi["pdf_path"])
        except Exception as e:  # noqa: BLE001
            logger.warning("[annual_report] 半年报公司行动抽取失败：%s", e)
            div, rep = [], None
        if div or rep:
            ann = fetch_annual_report(code)
            ctl = None
            rep_ann = None
            if ann:
                try:
                    ctl = extract_actual_controller(ann["pdf_path"])
                except Exception:  # noqa: BLE001
                    ctl = None
                try:
                    rep_ann = extract_repurchase(ann["pdf_path"])
                except Exception:  # noqa: BLE001
                    rep_ann = None
            # 回购合并：年度「最近一轮完整实施」为基底，半年报「报告期内进展」
            # 覆盖同名键（后者时点更新）。半年报仅有进展信号（无完整轮次句式，
            # 如 002384 2026H1 过户员工持股 + 1 亿元回购支出）时也必须保留，
            # 否则报告会停留在旧数据。
            if rep and rep_ann:
                _merged = {
                    **{k: v for k, v in rep_ann.items() if v},
                    **{k: v for k, v in rep.items() if v},
                }
                # 半年报叙述若只有进展（无「累计回购」字样），拼接年度轮叙述
                if (
                    rep.get("text")
                    and rep_ann.get("text")
                    and "累计回购" not in str(rep.get("text"))
                ):
                    _merged["text"] = (
                        str(rep_ann["text"]).rstrip()
                        + " 最新进展："
                        + str(rep["text"])
                    )
                rep = _merged
            elif not rep and rep_ann:
                rep = rep_ann
            return {
                "dividend": div,
                "repurchase": rep,
                "controller": ctl,
                "year": meta_semi.get("year"),
                "source": "semi",
            }
    meta = fetch_annual_report(code)
    if not meta:
        return None
    return {
        "dividend": extract_dividend_decision(meta["pdf_path"]),
        "repurchase": extract_repurchase(meta["pdf_path"]),
        "controller": extract_actual_controller(meta["pdf_path"]),
        "year": meta.get("year"),
    }
