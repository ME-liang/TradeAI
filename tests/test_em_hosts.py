# -*- coding: utf-8 -*-
"""东财主机池与 F10 取数参数（N-18 / N-19）。

**N-18｜板块报告「板块成员获取失败（bk=BK0890），无法生成报告」**
`board.py` 的 push2 主机列表硬编码为 `["push2delay", "push2"]`，**缺 `push2test`**。
2026-09-25 这两台被限流、三台中只有 `push2test` 可达 → `fetch_members_full()`
返回空 → `build_board_report_data()` 抛 RuntimeError，板块报告完全无法生成。
同型硬编码在 `industry.py` 另有 3 处（板块成员 / 单股 stock-get / 全市场快照）。

**N-19｜F10 接口恒返回空，且被误判成「接口失效」**
`datacenter*/securities/api/data/get` 的 `sty` 与 `filter` **都是必填**，原实现只传
`type` + `secucode`：
- 缺 `sty` → 9501「返回字段参数不能为空」→ `board._fetch_profile_http()` **从落地起
  就一直返回空**（重点公司「主营业务」整段为空），且 `RPT_F10_FINANCE_MAINFINADATA`
  （ROEJQ）因此被记成「2026-09 实测已失效」而换源绕开；
- 缺 `filter` → `secucode` 被忽略、返回**全表**（实测 24780 行）。若只补 `sty`
  不补 `filter`，`data[0]` 拿到的是**另一家公司**的数据 —— 比空值更危险。

本文件守住四件事：
1. 主机域名只允许定义在 `data/em.py`（源码级护栏，防止第 4 次复发）；
2. 主机池必须含 `push2test` 且在末位；冷却按「主机 + 路径」而非整台主机；
3. 只剩 `push2test` 可达时，板块成员与板块行情仍必须能取到；
4. F10 请求必须同时带 `sty` 与 `filter`，且不得再出现 `secucode=` 这种无效过滤。

全部离线：网络调用一律 monkeypatch，不触网。
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from fundamental_agent.data import em
from fundamental_agent.report_types import board as brd
from fundamental_agent.report_types import industry as ind


@pytest.fixture(autouse=True)
def _clean_health():
    """每个用例前后都清空主机冷却表，避免跨用例串味。"""
    em.reset_health()
    yield
    em.reset_health()


# ===== 1. 主机池单一来源（源码级护栏）=====
_FAMILY_LITERALS = (
    "push2.eastmoney.com",
    "push2delay.eastmoney.com",
    "push2test.eastmoney.com",
    "push2his.eastmoney.com",
    "datacenter.eastmoney.com",
    "datacenter-web.eastmoney.com",
)

# 已知遗留、本轮**有意未改**：`get_quote()` 只写死 push2delay，且返回体里
# `delayed=True` / `source="push2delay（延时行情）"` 是硬编码口径。要给它加备用
# 主机，必须同时如实标注「实际是哪个主机应答」，属产品可见的口径决策（见 N-20）。
# 注意它只影响「公司概况报告」的行情块，与本次板块报告故障链路不同。
_KNOWN_LEGACY = {"data/market.py"}


def test_no_eastmoney_host_literals_outside_em_module():
    """主机域名只允许出现在 data/em.py（+ 上面那条明确的遗留清单里）。"""
    root = Path(em.__file__).resolve().parents[1]        # src/fundamental_agent
    offenders = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        if rel == "data/em.py" or rel in _KNOWN_LEGACY:
            continue
        txt = p.read_text(encoding="utf-8")
        offenders += ["%s : %s" % (rel, lit) for lit in _FAMILY_LITERALS if lit in txt]
    assert not offenders, (
        "东财主机域名被硬编码到了 data/em.py 之外：%s\n"
        "（N-18 的根因就是这个：某处池子少了一台主机，一封就无路可走）" % offenders)


def test_report_types_use_shared_pool_not_own_lists():
    """board / industry 必须显式引用共享池，不能再各自维护一份。"""
    root = ind._PROJECT_ROOT / "src" / "fundamental_agent" / "report_types"
    for name in ("industry.py", "board.py"):
        txt = (root / name).read_text(encoding="utf-8")
        assert "_em.PUSH2_HOSTS" in txt, "%s 未使用共享的东财主机池" % name


def test_pool_shape():
    assert em.PUSH2_HOSTS[-1] == "https://push2test.eastmoney.com", \
        "push2test 必须留在末位兜底：主站被限流时它是唯一可达的一台"
    assert em.PUSH2_HOSTS[:2] == ("https://push2delay.eastmoney.com",
                                  "https://push2.eastmoney.com")
    assert set(em.DATACENTER_HOSTS) == {"https://datacenter.eastmoney.com",
                                        "https://datacenter-web.eastmoney.com"}


# ===== 2. 主机健康度（按「主机 + 路径」）=====
def test_ordered_hosts_demotes_cooling_host_but_keeps_it():
    path = "/api/qt/clist/get"
    assert em.ordered_hosts(path=path) == list(em.PUSH2_HOSTS)

    em.mark_down("https://push2delay.eastmoney.com", path)
    out = em.ordered_hosts(path=path)
    assert out[-1] == "https://push2delay.eastmoney.com"
    assert sorted(out) == sorted(em.PUSH2_HOSTS), \
        "冷却只能改变顺序，不能把主机剔除（全池冷却时仍需逐一尝试）"


def test_cooling_is_path_scoped_not_host_scoped():
    """封锁按「主机 + 路径」：clist 被封不应影响同一主机 stock/get 的优先级。"""
    em.mark_down("https://push2delay.eastmoney.com", "/api/qt/clist/get")
    assert em.ordered_hosts(path="/api/qt/clist/get")[-1] == \
        "https://push2delay.eastmoney.com"
    assert em.ordered_hosts(path="/api/qt/stock/get")[0] == \
        "https://push2delay.eastmoney.com"


def test_mark_up_restores_priority():
    em.mark_down("https://push2delay.eastmoney.com", "/api/qt/clist/get")
    em.mark_up("https://push2delay.eastmoney.com", "/api/qt/clist/get")
    assert em.ordered_hosts(path="/api/qt/clist/get")[0] == \
        "https://push2delay.eastmoney.com"


def test_get_json_falls_back_then_skips_cooled_host(monkeypatch):
    calls = []

    def _fake(url, timeout=10):
        calls.append(url)
        return None if "push2delay" in url else {"ok": 1}

    monkeypatch.setattr(em, "curl_json", _fake)
    assert em.get_json("/api/qt/clist/get") == {"ok": 1}
    assert len(calls) == 2, "首台失败应立即降级到下一台"

    em.get_json("/api/qt/clist/get")
    assert len(calls) == 3, "冷却后不应再为同一路径重试那台失效主机"


# ===== 3. F10 参数（N-19）=====
def _f10_ok(**kw):
    row = {"SECUCODE": "600519.SH",
           "MAIN_BUSINESS": "茅台酒及系列酒的生产与销售",
           "BUSINESS_SCOPE": "白酒生产与销售",
           "ORG_INTRODUCTION": "国内白酒龙头",
           "INDUSTRY": "白酒",
           "ROEJQ": "16.75"}
    row.update(kw)
    return {"result": {"data": [row]}}


def test_f10_get_must_send_sty_and_filter(monkeypatch):
    seen = {}

    def _fake(path, timeout=10, hosts=None):
        seen["path"], seen["hosts"] = path, hosts
        return _f10_ok()

    monkeypatch.setattr(em, "get_json", _fake)
    assert em.f10_get("RPT_F10_BASIC_ORGINFO", "600519")[0]["MAIN_BUSINESS"]

    q = parse_qs(urlparse(seen["path"]).query)
    assert q.get("sty") == ["ALL"], \
        "缺 sty 会被服务端判 9501「返回字段参数不能为空」→ 恒返回空（N-19 根因）"
    assert q.get("filter") == ['(SECUCODE="600519.SH")'], \
        "缺 filter 时 secucode 被忽略 → 返回全表 → data[0] 是另一家公司"
    assert "secucode" not in q, "secucode 不是有效的过滤参数，不要再传它"
    assert seen["hosts"] is em.DATACENTER_HOSTS


def test_f10_secucode_rule():
    assert em.secucode("600519") == "600519.SH"
    assert em.secucode("300184") == "300184.SZ"
    assert em.secucode("000636") == "000636.SZ"
    assert em.secucode("830799") == "830799.BJ"
    assert em.secucode("430047") == "430047.BJ"


def test_f10_profile_normalizes_and_degrades_silently(monkeypatch):
    monkeypatch.setattr(em, "get_json", lambda path, timeout=10, hosts=None: _f10_ok())
    p = em.f10_profile("600519")
    assert p["main_business"] == "茅台酒及系列酒的生产与销售"
    assert set(p) == {"main_business", "scope", "intro", "industry"}

    monkeypatch.setattr(em, "get_json", lambda path, timeout=10, hosts=None: None)
    assert em.f10_profile("600519") == {"main_business": "", "scope": "",
                                        "intro": "", "industry": ""}


def test_f10_roejq_parses_latest_period(monkeypatch):
    monkeypatch.setattr(em, "get_json", lambda path, timeout=10, hosts=None: _f10_ok())
    assert em.f10_roejq("600519") == pytest.approx(16.75)

    monkeypatch.setattr(em, "get_json",
                        lambda path, timeout=10, hosts=None: {"result": {"data": []}})
    assert em.f10_roejq("600519") is None

    monkeypatch.setattr(em, "get_json",
                        lambda path, timeout=10, hosts=None: _f10_ok(ROEJQ="-"))
    assert em.f10_roejq("600519") is None, "东财用 '-' 表示无数据"


def test_f10_report_type_is_forwarded(monkeypatch):
    seen = {}

    def _fake(path, timeout=10, hosts=None):
        seen["path"] = path
        return {"result": {"data": []}}

    monkeypatch.setattr(em, "get_json", _fake)
    em.f10_roejq("600519")
    assert parse_qs(urlparse(seen["path"]).query)["type"] == \
        ["RPT_F10_FINANCE_MAINFINADATA"]


def test_board_profile_http_delegates_to_em(monkeypatch):
    """事故点：原实现恒返回空 dict（请求缺 sty/filter）。"""
    called = {}

    def _fake(code):
        called["code"] = code
        return {"main_business": "研制、生产、销售电子元器件、电子材料等。",
                "scope": "", "intro": "", "industry": ""}

    monkeypatch.setattr(em, "f10_profile", _fake)
    out = brd._fetch_profile_http("000636")
    assert called["code"] == "000636"
    assert out["main_business"].startswith("研制")


# ===== 4. 板块成员/行情：只剩 push2test 也必须取到（N-18 行为回归）=====
_MEMBER_PAYLOAD = {"data": {"total": 2, "diff": [
    {"f12": "000636", "f14": "风华高科", "f2": 11.0, "f3": 1.0, "f100": "电子元件",
     "f20": 6.4e10, "f21": 6.0e10, "f115": 110.7, "f23": 2.3},
    {"f12": "300408", "f14": "三环集团", "f2": 20.0, "f3": -1.0, "f100": "电子元件",
     "f20": 2.4e11, "f21": 2.3e11, "f115": 30.0, "f23": 3.1},
]}}


def test_board_members_survive_when_only_push2test_alive(monkeypatch):
    alive = "https://push2test.eastmoney.com"
    seen_hosts = []

    def _fake(host, path, timeout=10):
        seen_hosts.append(host)
        return _MEMBER_PAYLOAD if host == alive else None

    monkeypatch.setattr(em, "fetch_host", _fake)
    members = brd.fetch_members_full("BK0890")

    assert alive in seen_hosts, "必须尝试过 push2test —— 它就是原实现缺的那一台"
    assert [m["code"] for m in members] == ["000636", "300408"]
    assert members[0]["name"] == "风华高科"
    assert members[0]["mcap"] == pytest.approx(6.4e10)
    assert members[0]["pe"] == pytest.approx(110.7)


def test_board_quote_uses_shared_pool(monkeypatch):
    seen = {}

    def _fake(path, timeout=10, hosts=None):
        seen["path"], seen["hosts"] = path, hosts
        return {"data": {"diff": [{"f12": "BK0890", "f14": "MLCC", "f2": 5092.65,
                                   "f3": -2.64, "f104": 5, "f105": 34,
                                   "f128": "裕兴股份", "f136": 5.92,
                                   "f20": 1.0878e12}]}}

    monkeypatch.setattr(em, "get_json", _fake)
    q = brd.fetch_board_quote("BK0890")
    assert q["name"] == "MLCC"
    assert q["leader"] == "裕兴股份"
    assert q["chg"] == pytest.approx(-2.64)
    assert "clist/get" in seen["path"]
    assert seen["hosts"] is None, "应使用 em 的默认池（PUSH2_HOSTS）"


def test_board_report_raises_readable_error_when_all_hosts_dead(monkeypatch):
    """全池不可用时行为不变：成员为空 + 报告层抛既有可读异常。"""
    class _FakeTime:
        @staticmethod
        def sleep(*_a):
            return None

        @staticmethod
        def time():
            return 0.0

    monkeypatch.setattr(brd, "time", _FakeTime, raising=False)
    monkeypatch.setattr(em, "fetch_host", lambda host, path, timeout=10: None)
    monkeypatch.setattr(brd, "fetch_board_quote", lambda bk: {})

    assert brd.fetch_members_full("BK0890") == []
    with pytest.raises(RuntimeError) as ei:
        brd.build_board_report_data("BK0890", use_llm=False)
    assert "板块成员获取失败（bk=BK0890）" in str(ei.value)


# ===== 5. industry 侧的薄包装与池接入 =====
def test_industry_wrappers_delegate_to_em(monkeypatch):
    """保留同名包装是刻意的（board 与既有测试按名引用），实现必须在 em。"""
    monkeypatch.setattr(em, "get_json",
                        lambda path, timeout=10, hosts=None: {"delegated": path})
    assert ind._curl_json_hosts(["https://h"], "/p", timeout=3) == {"delegated": "/p"}

    monkeypatch.setattr(em, "curl_json", lambda url, timeout=10: {"raw": url})
    assert ind._curl_json("https://h/p") == {"raw": "https://h/p"}
