"""首页卡片落点与 F-06 入口的随仓回归用例。

由来（N-10）：F-02 的 28/28、12/12、8/8 全部来自**临时 Node DOM 桩与一次性 Chrome CDP 脚本**，
没落进 `tests/` —— 而 F-01 紧接着要改同一个 `index.html`，任何误伤都不会被发现。
本文件把两批断言一起固化下来：**零依赖（只用标准库 html.parser）、零网络**。

对照断言编号来自 `docs/F-01-首页卡片导航打通-设计方案.md` §8.1
与 `docs/F-06-我的报告库-设计方案.md` §8.4 / §8.6。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"

# 首页四张卡允许的落点白名单（决策：F-06 已把卡片 3 的落点定为 reports.html）
CARD_HREFS = {
    "stock": "stock.html",
    "sector": "sector.html",
    "report": "reports.html",
    "financials": "stock.html?intent=financials",
}


class _Collector(HTMLParser):
    """收集「带 class 的元素」与「带 href 的链接」。零依赖，够用即可。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []   # (tag, attrs)
        self.hrefs: list[str] = []
        self._stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = {k: (v or "") for k, v in attrs}
        self.tags.append((tag, d))
        if d.get("href"):
            self.hrefs.append(d["href"])
        # 记录开标签，便于判断“某类名元素是否为 <a>”
        self._stack.append(tag)

    handle_startendtag = handle_starttag


def _parse(name: str) -> _Collector:
    p = FRONTEND / name
    assert p.is_file(), "页面不存在：%s" % p
    c = _Collector()
    c.feed(p.read_text(encoding="utf-8"))
    return c


def _cards(c: _Collector) -> list[tuple[str, dict]]:
    """返回 [(tag, attrs)]，按文档顺序，仅 feature-card。"""
    out = []
    for tag, attrs in c.tags:
        if "feature-card" in (attrs.get("class") or "").split():
            out.append((tag, attrs))
    return out


# ======================= F-01：卡片语义与落点 =======================

def test_A1_四张卡均为链接且_href_非空非井号():
    """A1：四张卡在 DOM 中均为 <a> 且 href 非空、非 '#'。"""
    cards = _cards(_parse("index.html"))
    assert len(cards) == 4, "首页应有 4 张功能卡，实际 %d" % len(cards)
    for tag, attrs in cards:
        assert tag == "a", "卡片应为语义化 <a>（键盘可达/可新开），实际 <%s>" % tag
        href = attrs.get("href") or ""
        assert href and href != "#", "卡片 href 不得为空或 '#'：%r" % href


def test_A5_四张卡落点符合映射表():
    """A5：落点与决策一致（卡片 3 → reports.html，卡片 4 → stock.html?intent=financials）。"""
    got = {}
    for tag, attrs in _cards(_parse("index.html")):
        cid = attrs.get("id")
        assert cid, "卡片必须有 id（用于定位与回归）：%r" % attrs
        got[cid] = attrs.get("href")
    assert got == CARD_HREFS, "卡片落点与映射表不符：%r" % got


def test_A6_卡片落点绝不含裸_stock_detail():
    """A6（关键护栏）：禁止出现裸 `stock_detail.html`。

    `stock_detail.html:338` 是 `(_SP.get('code') || '600519')` —— 任何不带 code 的导航
    都会**静默打开贵州茅台**。这条断言长期守住“卡片落点不会指向一个会静默打开别的公司的页面”。
    """
    for tag, attrs in _cards(_parse("index.html")):
        href = attrs.get("href") or ""
        assert "stock_detail.html" not in href, "卡片落点出现了裸 stock_detail.html：%r" % href


def test_A10_卡片_id_唯一且第四张不叫_report():
    """A10：卡片 4 的 id 必须存在且不与卡片 3 的 `report` 冲突。"""
    ids = [a.get("id") for _, a in _cards(_parse("index.html"))]
    assert len(ids) == len(set(ids)), "卡片 id 重复：%r" % ids
    assert "report" in ids, "卡片 3 的 id 必须仍是 report（导航 #report 锚点依赖它）"
    assert ids.count("report") == 1, "report 只能出现一次"
    assert "financials" in ids, "卡片 4 必须补上 id（原为无 id，点了绝对无事发生）"


def test_A2_卡片不再由_JS_接管跳转():
    """A2/A3：`main.js` 不得再绑定卡片点击跳转（否则 Ctrl+点击会双重导航）。

    只对「卡片相关」的写操作断言：`main.js` 里不应再出现 feature-card 的点击处理、
    也不应保留被注释掉的 `window.location.href` 占位。
    """
    js = (FRONTEND / "js" / "main.js").read_text(encoding="utf-8")
    assert "querySelectorAll('.feature-card')" not in js, "main.js 仍在遍历 feature-card 绑定事件"
    assert "location.href" not in js, "main.js 仍在做跳转（<a> 与 JS 跳转不能并存）"
    assert "console.log('进入" not in js, "main.js 仍留着 console.log 占位分支"


def test_A9_导航含报告库且智能报告锚点仍在():
    """A9 + F-06 §8.6-1：导航有 reports.html 入口；`#report` 锚点仍可滚到卡片。"""
    c = _parse("index.html")
    assert "reports.html" in c.hrefs, "主导航缺少「报告库」入口"
    assert "watchlist.html" in c.hrefs, "「我的自选」入口不应被替换掉"
    assert "#report" in c.hrefs, "「智能报告」的 #report 锚点不应被删除"
    # 锚点要能生效：id="report" 必须留在卡片根元素上（不是内层 h3）
    assert any(a.get("id") == "report" for _, a in _cards(c)), "id=report 必须留在卡片根元素"


# ======================= F-01：列表页 intent 引导条 =======================

def test_A7_列表页支持_intent_白名单且非白名单不渲染():
    """A7：`stock.html` 读 `?intent=`，仅 report / financials 生效。"""
    html = (FRONTEND / "stock.html").read_text(encoding="utf-8")
    assert "intentBar" in html, "缺少引导条 DOM"
    assert "intentText" in html, "缺少引导条文案节点"
    # 白名单：两个 key 都在
    assert "report:" in html and "financials:" in html, "intent 文案表缺少白名单键"
    # 非白名单必须静默忽略（不显示、不报错）
    assert re.search(r"if\s*\(\s*!msg\s*\)\s*return", html), "非白名单 intent 应静默忽略"
    # 引导条必须默认隐藏，避免无 intent 时露出空条
    assert re.search(r'id="intentBar"[^>]*hidden', html), "引导条应默认 hidden"


def test_A8_intent_与_q_可共存且_restore_优先级最高():
    """A8：`?intent=` 与 `?q=` 共存；`restore`（返回导航）仍最高优先。

    做法：断言「直达参数」块被 `if(!RESTORE)` 守卫 —— 这正是 N-06 修复的写法，
    若有人把它挪出守卫，返回导航时搜索框会被 URL 参数覆盖。
    """
    html = (FRONTEND / "stock.html").read_text(encoding="utf-8")
    # ?q= 直达参数必须在 !RESTORE 分支内
    m = re.search(r"URL 直达参数\(\?q=\)(.*?)\n\s*function snapshot", html, re.S)
    assert m, "找不到「URL 直达参数」区块"
    block = m.group(1)
    assert "if(!RESTORE){" in block, "直达参数必须被 if(!RESTORE) 守卫（否则会覆盖返回导航）"
    # intent 读取与 q 读取都在同一文件内、互不干扰
    assert "URLSearchParams(location.search).get('intent')" in html


# ======================= N-10：F-02 关键不变量回归 =======================

def test_N10_搜索框只依据_data_status_分流():
    """N-10①：`unavailable` 不得被当成「无结果」（本项目两次误报事故的根因）。"""
    js = (FRONTEND / "js" / "global_search.js").read_text(encoding="utf-8")
    assert "data_status" in js, "搜索框必须读 data_status"
    assert "failed: d.data_status === 'unavailable'" in js, \
        "unavailable 必须被标为 failed（HTTP 200 + 空 payload 会绕过兜底）"


def test_N10_搜索框文案区分不可用与无结果():
    """N-10②：不可用态必须明写「不代表没有」。"""
    js = (FRONTEND / "js" / "global_search.js").read_text(encoding="utf-8")
    assert "不代表没有这只股票" in js, "个股不可用态缺少「不代表没有」文案"
    assert "不代表没有这个板块" in js, "板块不可用态缺少「不代表没有」文案"


def test_N10_空分组不占位():
    """N-10③：空分组不得渲染孤立的组标题（F-02 验收时靠截图发现的问题）。"""
    js = (FRONTEND / "js" / "global_search.js").read_text(encoding="utf-8")
    assert "空分组不占位" in js, "缺少「空分组不占位」的实现或注释"


def test_N10_列表页直达参数不依赖_ListState():
    """N-10④：`?q=` / `?type=` 直达必须直读 URLSearchParams。

    `js/list_state.js` 的 `ListState.load()` 首行即 `isReturnNav()` 判定，非「返回导航」
    一律返回 null —— 照抄它会让直达参数被静默忽略（N-06 的成因）。
    """
    ls = (FRONTEND / "js" / "list_state.js").read_text(encoding="utf-8")
    assert "isReturnNav()" in ls, "list_state.js 的返回导航判定不应被删除"
    for page in ("stock.html", "sector.html"):
        html = (FRONTEND / page).read_text(encoding="utf-8")
        assert "new URLSearchParams(location.search)" in html, \
            "%s 必须直读 URLSearchParams，不能只依赖 ListState" % page


# ======================= F-06：入口与三态分离 =======================

def test_F06_报告库页面存在且三态分离():
    """F-06 §8.4：空态 / 不可用态 / 单条损坏 三者文案必须分离。"""
    html = (FRONTEND / "reports.html").read_text(encoding="utf-8")
    assert "你还没有生成过报告" in html, "缺少空态文案"
    assert "报告库暂不可用" in html, "缺少不可用态文案"
    assert "这不是「你没有报告」" in html, "不可用态必须与空态明确区分"
    assert "元信息损坏" in html, "单条 meta 损坏必须告知而非静默吞掉"
    assert "root_available" in html, "不可用态必须由 root_available 驱动"


def test_F06_报告库按类型分区且空分组不渲染():
    """F-06 §8.2：类型条计数取全量镜像，分区渲染，空类型不占位。"""
    html = (FRONTEND / "reports.html").read_text(encoding="utf-8")
    assert "aria-pressed" in html, "类型条应是有按下态的 chip"
    assert "/api/report/list" in html, "应调用 /api/report/list"
    # 计数来自服务端 types（过滤前的全量镜像），不得用 items.length 反推
    assert "state.types" in html and "t.count" in html, "类型计数必须取服务端 types"
    # 分区只由有数据的类型生成（空分组不渲染）
    m = re.search(r"arr\.forEach\(function\(it\)\{\s*if\(!groups\[it\.report_type\]\)", html)
    assert m, "分区应由实际存在的数据驱动（空分组不占位）"


def test_F06_打开方式为新标签且用持久_view_url():
    """F-06 §8.5（D3）：新标签直开持久链接（现状连刷新都会 404）。"""
    html = (FRONTEND / "reports.html").read_text(encoding="utf-8")
    assert 'target="_blank"' in html and 'rel="noopener"' in html, "打开报告应为新标签 + noopener"
    assert "it.view_url" in html, "应使用服务端下发的持久 view_url，而非内存 task_id"


def test_F06_报告库直达参数直读_URLSearchParams():
    """F-06 §8.6-2：reports.html 也必须直读 URLSearchParams（N-06 教训）。"""
    html = (FRONTEND / "reports.html").read_text(encoding="utf-8")
    assert "new URLSearchParams(location.search)" in html
    assert "SP.get('type')" in html and "SP.get('code')" in html


@pytest.mark.parametrize(
    "page,expected",
    [
        ("stock_detail.html", ["reports.html?type=overview",
                               "reports.html?type=fundamental",
                               "reports.html?type=industry",
                               "&code="]),
        ("sector_detail.html", ["reports.html?type=board", "&code="]),
    ],
)
def test_F06_生成现场有历史报告入口(page, expected):
    """F-06 §8.6-2/3：详情页报告面板旁有「查看历史报告」，且**带上当前标的**。"""
    html = (FRONTEND / page).read_text(encoding="utf-8")
    assert "查看历史报告" in html, "%s 缺少历史报告入口" % page
    for needle in expected:
        assert needle in html, "%s 缺少 %r（历史报告入口需带类型/标的）" % (page, needle)


def test_F06_板块报告入口不带个股链接():
    """F-06 §8.3：board 类型没有个股页，其列表项不得出现「查看个股详情」。

    由服务端 `subject_url` 只在标的主体合法时下发来保证，前端只按类型选文案。
    """
    html = (FRONTEND / "reports.html").read_text(encoding="utf-8")
    assert "前往板块页" in html, "board 类型应给「前往板块页」"
    assert re.search(r"report_type\s*===\s*'board'", html), "应存在按类型选择动作文案的分支"
    # 前端不得自行拼 stock_detail.html（必须用服务端 subject_url）
    assert "stock_detail.html" not in html, "前端不应硬编码个股页路径，应用 subject_url"
