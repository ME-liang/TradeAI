# -*- coding: utf-8 -*-
"""报告库（F-06）测试：report_id 编解码 / 扫描归一化 / 用户隔离 / 路径护栏 / API。

全部离线、不依赖外网，也不触碰真实的 `output/reports`：
扫描根通过 monkeypatch `report_index._DEFAULT_BASE` 与 `storage._DEFAULT_BASE`
指向 `tmp_path`，用代码直接造出报告目录树。

覆盖方案 §12.1 的 12 条断言，另加 D1（用户隔离落盘）与 D8（行业报告多基准不覆盖）两条。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from fundamental_agent import report_index as ri
from fundamental_agent import storage
from fundamental_agent.api import server as api_server
from fundamental_agent.auth import db as auth_db

# ===== 造数据 =====


def make_report(root, user_id, code, report_type, date_seg, meta, *,
                with_meta=True, report_type_seg=None):
    """在 root 下造一份报告产物，返回其目录。

    report_type 为 "" 表示走 2 段路径（基本面，type 段缺省）。
    report_type_seg 可显式指定目录段名（用于测试非法段名）。
    """
    parts = ["u%d" % user_id] if user_id else []
    parts.append(code)
    seg = report_type if report_type_seg is None else report_type_seg
    if seg:
        parts.append(seg)
    parts.append(date_seg)
    d = root.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.html").write_text("<html><body>%s</body></html>" % code, encoding="utf-8")
    if with_meta:
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return d


def stock_meta(code, name, report_type, generated_at="2026-09-22 14:28", user_id=1,
               warnings=0, bk="", bk_name=""):
    return {
        "code": code, "name": name, "report_type": report_type,
        "bk": bk, "bk_name": bk_name,
        "writer_mode": "contract", "search_used": False,
        "generated_at": generated_at, "data_source": "akshare",
        "validation": {"warnings": ["w"] * warnings},
        "user_id": user_id,
    }


@pytest.fixture()
def reports_root(tmp_path, monkeypatch):
    """扫描根指向 tmp_path，并让 storage 的落盘基准也指向它。"""
    root = tmp_path / "reports"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ri, "_DEFAULT_BASE", root)
    monkeypatch.setattr(storage, "_DEFAULT_BASE", root)
    return root


# ===== 1. 段数归一化 =====

def test_two_and_three_segment_layouts_normalize(reports_root):
    make_report(reports_root, 1, "002594", "", "20260922",
                stock_meta("002594", "比亚迪", "fundamental"))
    make_report(reports_root, 1, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview"))
    make_report(reports_root, 1, "BK1036", "board", "20260922",
                {"bk": "BK1036", "name": "半导体", "report_type": "board",
                 "generated_at": "2026-09-22 16:00", "data_source": "eastmoney",
                 "llm_ok": True, "user_id": 1})

    got = {it["report_id"] for it in ri.list_reports(1)["items"]}
    # 2 段（基本面，type 缺省）必须在 report_id 里补成 fundamental
    assert got == {"002594.fundamental.20260922",
                   "002594.overview.20260922",
                   "BK1036.board.20260922"}


# ===== 2. 非规范日期目录 =====

def test_non_standard_date_dir_is_listed(reports_root):
    make_report(reports_root, 1, "002594", "overview", "20260922_morning_noLLM",
                stock_meta("002594", "比亚迪", "overview"))
    items = ri.list_reports(1)["items"]
    assert [it["report_id"] for it in items] == ["002594.overview.20260922_morning_noLLM"]


# ===== 3. 未知 report_type 不丢弃 =====

def test_unknown_report_type_is_kept_and_sorted_last(reports_root):
    make_report(reports_root, 1, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview"))
    make_report(reports_root, 1, "002594", "zzz", "20260922",
                stock_meta("002594", "比亚迪", "zzz"))

    res = ri.list_reports(1)
    keys = [t["key"] for t in res["types"]]
    assert "zzz" in keys, "未知类型被丢弃了 —— 将来新增报告类型会凭空消失"
    assert keys[-1] == "zzz", "未知类型必须排在已注册类型之后"
    lbl = {t["key"]: t["label"] for t in res["types"]}
    assert lbl["zzz"] == "zzz" and lbl["overview"] == "公司概况"


# ===== 4. 非报告目录跳过 =====

def test_non_report_dirs_are_skipped(reports_root):
    (reports_root / "u1" / "junk").mkdir(parents=True)          # 1 段
    make_report(reports_root, 1, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview"))
    # 4 段（多一层）——既不是 2 段也不是 3 段，跳过不猜
    deep = reports_root / "u1" / "002594" / "overview" / "20260922" / "extra"
    deep.mkdir(parents=True)
    (deep / "report.html").write_text("x", encoding="utf-8")
    (deep / "meta.json").write_text(json.dumps({"report_type": "overview"}), encoding="utf-8")

    res = ri.list_reports(1)
    assert [it["report_id"] for it in res["items"]] == ["002594.overview.20260922"]
    assert res["warnings"]["skipped"] >= 1


# ===== 5. 用户隔离（列表） =====

def test_list_is_isolated_per_user(reports_root):
    make_report(reports_root, 1, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview", user_id=1))
    assert ri.list_reports(1)["total"] == 1
    res2 = ri.list_reports(2)
    assert res2["total"] == 0, "用户 2 看到了用户 1 的报告"
    assert res2["root_available"] is True, "用户根不存在应判为「空态」而非「不可用」"


# ===== 6. 越权 view =====

def test_view_is_isolated_per_user(reports_root):
    make_report(reports_root, 1, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview", user_id=1))
    assert ri.resolve_report_html(1, "002594.overview.20260922") is not None
    # 同一 report_id 是「用户相对」的：用户 2 解析不到任何东西
    assert ri.resolve_report_html(2, "002594.overview.20260922") is None


# ===== 7. 非法 report_id =====

@pytest.mark.parametrize("bad", [
    "../../../etc/passwd",
    "600519.fundamental.99999999",   # 格式合法但文件不存在
    "600519.xxx.2026/0922",          # 日期段含 /
    "..\\..\\windows\\win.ini",
    "a.b", "a.b.c.d",                # 段数不对
    "600519.fundamental.2026.0922",  # 4 段
    "",
    "600519.-bad.20260922",          # 类型段以非字母开头
    "600519.overview.",              # 空日期段
])
def test_invalid_report_id_never_resolves(reports_root, bad):
    assert ri.resolve_report_html(1, bad) is None


def test_report_id_roundtrip(reports_root):
    make_report(reports_root, 1, "002594", "overview", "20260922_morning_noLLM",
                stock_meta("002594", "比亚迪", "overview"))
    rid = ri.list_reports(1)["items"][0]["report_id"]
    assert ri.parse_report_id(rid) == ("002594", "overview", "20260922_morning_noLLM")
    # 列表里出现的每一条都必须能被 view 反解（否则就是「点开就 404」的死条目）
    assert ri.resolve_report_html(1, rid) is not None


# ===== N-21：基本面报告的两种目录形态都必须能打开 =====
#
# 排障记录（2026-09-26）：用户从报告库点「力源信息 基本面分析报告」→
# `{"detail":"报告不存在或不属于当前用户"}`。列表按目录名反解出
# `300184.fundamental.20260925`，而 view 只去找 **2 段**目录
# `u1/300184/20260925/`，实际落盘是 **3 段** `u1/300184/fundamental/20260925/`
# → 列表广告了 view 结构性拒绝的条目。旧用例只抽查 overview（永远 3 段），
# 于是「列表里每一条都必须能打开」这条不变量虽写了断言却漏掉了基本面。

def test_three_segment_fundamental_is_openable(reports_root):
    """回归：唯一写路径（`_worker` 显式传 report_type="fundamental"）落 3 段，读侧必须认。"""
    make_report(reports_root, 1, "300184", "fundamental", "20260925",
                stock_meta("300184", "力源信息", "fundamental"))
    items = ri.list_reports(1)["items"]
    assert [it["report_id"] for it in items] == ["300184.fundamental.20260925"]
    assert ri.resolve_report_html(1, items[0]["report_id"]) is not None, \
        "3 段基本面目录解析不到 → 报告库里点开必 404"


def test_two_segment_fundamental_still_resolves(reports_root):
    """向后兼容：type 段缺省的 2 段形态（旧产物 / 外部工具放置）依然可打开。"""
    make_report(reports_root, 1, "300184", "", "20260924",
                stock_meta("300184", "力源信息", "fundamental"))
    assert ri.resolve_report_html(1, "300184.fundamental.20260924") is not None


def test_fundamental_prefers_explicit_type_segment_when_both_exist(reports_root):
    """两种形态同时存在时取更具体的 3 段那份（避免误开手工放置的旧产物）。"""
    three = make_report(reports_root, 1, "300184", "fundamental", "20260925",
                        stock_meta("300184", "力源信息", "fundamental"))
    make_report(reports_root, 1, "300184", "", "20260925",
                stock_meta("300184", "力源信息", "fundamental"))
    got = ri.resolve_report_html(1, "300184.fundamental.20260925")
    assert got == (three / "report.html").resolve()


def test_every_listed_item_is_openable_across_all_layouts(reports_root):
    """**列表契约**：每一种落盘形态的条目都必须能被 view 打开。

    这是「列表广告了 view 拒绝的条目」这类缺陷的**类级**护栏 —— 不要只抽查一种形态
    （旧用例只查 overview，正是基本面恒 404 长期未被发现的原因）。新增报告类型 /
    新增路径形态时，把它加进下面即可。
    """
    make_report(reports_root, 1, "300184", "", "20260924",                       # 2 段基本面
                stock_meta("300184", "力源信息", "fundamental"))
    make_report(reports_root, 1, "300184", "fundamental", "20260925",            # 3 段基本面
                stock_meta("300184", "力源信息", "fundamental"))
    make_report(reports_root, 1, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview"))
    make_report(reports_root, 1, "002594", "industry", "20260922__BK0890",       # D8 后缀
                stock_meta("002594", "比亚迪", "industry", bk="BK0890"))
    make_report(reports_root, 1, "BK1036", "board", "20260922",                  # 板块
                {"bk": "BK1036", "name": "半导体", "report_type": "board",
                 "generated_at": "2026-09-22 16:00", "data_source": "eastmoney",
                 "llm_ok": True, "user_id": 1})

    res = ri.list_reports(1)
    assert res["total"] == 5
    dead = [it["report_id"] for it in res["items"]
            if ri.resolve_report_html(1, it["report_id"]) is None]
    assert dead == [], "列表里出现了点开即 404 的死条目：%s" % dead


# ===== 8. 过滤：items 变、types 不变 =====

def test_filters_do_not_change_type_counts(reports_root):
    make_report(reports_root, 1, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview"))
    make_report(reports_root, 1, "002594", "overview", "20260923",
                stock_meta("002594", "比亚迪", "overview"))
    make_report(reports_root, 1, "BK1036", "board", "20260922",
                {"bk": "BK1036", "name": "半导体", "report_type": "board",
                 "generated_at": "2026-09-22 16:00", "user_id": 1})

    full = ri.list_reports(1)
    assert full["total"] == 3

    only_board = ri.list_reports(1, report_type="board")
    assert [it["report_id"] for it in only_board["items"]] == ["BK1036.board.20260922"]
    assert only_board["types"] == full["types"], "types 必须是过滤前的全量镜像，否则切不回去"

    by_code = ri.list_reports(1, code="002594")
    assert by_code["total"] == 2
    assert by_code["types"] == full["types"]


# ===== 11. meta 缺失 / 损坏 =====

def test_damaged_or_missing_meta_is_skipped(reports_root):
    make_report(reports_root, 1, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview"))
    # 损坏：JSON 解析失败
    bad = reports_root / "u1" / "600519" / "industry" / "20260923"
    bad.mkdir(parents=True)
    (bad / "report.html").write_text("x", encoding="utf-8")
    (bad / "meta.json").write_text("{not json", encoding="utf-8")
    # 缺失：目录里有 report.html 但没有 meta.json
    lose = reports_root / "u1" / "600036" / "overview" / "20260922"
    lose.mkdir(parents=True)
    (lose / "report.html").write_text("x", encoding="utf-8")

    res = ri.list_reports(1)
    assert [it["report_id"] for it in res["items"]] == ["002594.overview.20260922"]
    assert res["warnings"]["damaged"] == 2, "损坏/缺失的条目必须留痕，不能静默消失"


# ===== 12. generated_at 缺失不抛 TypeError =====

def test_missing_generated_at_sorts_without_typeerror(reports_root):
    make_report(reports_root, 1, "002594", "overview", "20260922", {"name": "无时间"})
    make_report(reports_root, 1, "000001", "overview", "20260923",
                stock_meta("000001", "平安银行", "overview", generated_at="2026-09-23 10:00"))
    res = ri.list_reports(1)                     # 不能抛 TypeError
    assert res["total"] == 2
    assert res["items"][0]["report_id"] == "000001.overview.20260923"


# ===== 排序 / 分页 =====

def test_sort_and_pagination(reports_root):
    for i in range(5):
        make_report(reports_root, 1, "00259%d" % i, "overview", "2026092%d" % i,
                    stock_meta("00259%d" % i, "N%d" % i, "overview",
                               generated_at="2026-09-2%d 10:00" % i))
    res = ri.list_reports(1, page=1, page_size=2)
    assert res["total"] == 5 and res["count"] == 2
    assert res["items"][0]["report_id"].endswith("20260924")
    assert res["page"] == 1 and res["page_size"] == 2
    assert ri.list_reports(1, page=3, page_size=2)["count"] == 1


def test_page_size_is_clamped(reports_root):
    assert ri.list_reports(1, page_size=9999)["page_size"] == 100
    assert ri.list_reports(1, page_size=0)["page_size"] == 1


# ===== 主体链接护栏 =====

def test_subject_url_only_when_subject_is_valid(reports_root):
    make_report(reports_root, 1, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview"))
    make_report(reports_root, 1, "BK1036", "board", "20260922",
                {"bk": "BK1036", "name": "半导体", "report_type": "board",
                 "generated_at": "2026-09-22 16:00", "user_id": 1})
    by_id = {it["report_id"]: it for it in ri.list_reports(1)["items"]}
    assert by_id["002594.overview.20260922"]["subject_url"] == "/stock_detail.html?code=002594"
    # 板块报告没有个股页 → 只能回板块页
    assert by_id["BK1036.board.20260922"]["subject_url"] == "/sector_detail.html?code=BK1036"
    # 标的主体非法时**不下发链接**（堵住 stock_detail.html 的 || '600519' 默认值陷阱）
    assert ri._subject_url("overview", "BK1036") == ""
    assert ri._subject_url("board", "002594") == ""


# ===== D1：用户隔离落盘 =====

def test_storage_paths_are_isolated_per_user(reports_root):
    from datetime import date

    today = date.today().strftime("%Y%m%d")
    assert storage.report_output_path("600519", "overview", user_id=1).parent == \
        reports_root / "u1" / "600519" / "overview" / today
    assert storage.report_output_path("600519", "overview", user_id=2).parent == \
        reports_root / "u2" / "600519" / "overview" / today
    # user_id 为空 → 旧路径（向后兼容：CLI / 迁移脚本 / 既有测试的路径构造不变）
    assert storage.report_output_path("600519", "overview").parent == \
        reports_root / "600519" / "overview" / today
    # 基本面 2 段
    assert storage.report_output_path("600519").parent == reports_root / "600519" / today


def test_save_report_keeps_html_and_meta_in_same_dir(reports_root):
    """save_report 会**重算目录**，两处 user_id 必须一致，否则 HTML 与 meta 分居两处。"""
    src = reports_root / "_tmp_src.html"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("<html>ok</html>", encoding="utf-8")
    paths = storage.save_report("600519", src, meta={"code": "600519"}, report_type="overview",
                               user_id=7)
    from pathlib import Path

    assert Path(paths["html"]).parent == Path(paths["meta"]).parent
    assert "u7" in Path(paths["html"]).parts


# ===== D8：行业报告同日多基准不互相覆盖 =====

def test_industry_reports_with_different_benchmarks_do_not_overwrite(reports_root):
    p1 = storage.report_output_path("002594", "industry", user_id=1,
                                    date_suffix=storage.industry_date_suffix("industry", "BK0890"))
    p2 = storage.report_output_path("002594", "industry", user_id=1,
                                    date_suffix=storage.industry_date_suffix("industry", "BK0481"))
    assert p1.parent != p2.parent, "同日不同对比基准落到了同一目录 → 后写会覆盖先写"
    # 段数不变（扫描器无需改动）
    assert len(p1.parent.relative_to(reports_root).parts) == 4
    # 非 industry 类型不带后缀
    assert storage.industry_date_suffix("overview", "BK0890") == ""
    assert storage.industry_date_suffix("industry", "") == ""


def test_industry_report_with_bk_suffix_is_scannable(reports_root):
    from datetime import date

    seg = "%s__BK0890" % date.today().strftime("%Y%m%d")
    make_report(reports_root, 1, "002594", "industry", seg,
                stock_meta("002594", "比亚迪", "industry", bk="BK0890", bk_name="半导体"))
    res = ri.list_reports(1)
    assert res["total"] == 1
    it = res["items"][0]
    assert it["report_id"] == "002594.industry.%s" % seg
    assert it["bk_name"] == "半导体"
    assert ri.resolve_report_html(1, it["report_id"]) is not None


# ===== _reuse_today 的 user_id 隔离（签名变化 + 行为） =====

def _patch_overview_mode(monkeypatch, mode="contract"):
    monkeypatch.setattr(api_server.yaml, "safe_load",
                        lambda *_a, **_k: {"overview_writer": {"mode": mode}})


def test_reuse_today_is_isolated_per_user(reports_root, monkeypatch):
    _patch_overview_mode(monkeypatch)
    make_report(reports_root, 1, "999999", "overview",
                __import__("datetime").date.today().strftime("%Y%m%d"),
                {"code": "999999", "report_type": "overview", "writer_mode": "contract",
                 "user_id": 1})

    assert api_server._reuse_today("999999", "overview", user_id=1) is not None
    # 用户 2 不能复用用户 1 的产物（旧行为会复用 → 跨用户泄漏）
    assert api_server._reuse_today("999999", "overview", user_id=2) is None
    # user_id 为空（legacy 根）也不该命中带用户段的产物
    assert api_server._reuse_today("999999", "overview") is None


def test_reuse_today_rejects_owner_mismatch_in_meta(reports_root, monkeypatch):
    """路径隔离为主，meta.user_id 二次对账兜底（防手工搬动/迁移脚本误放）。"""
    _patch_overview_mode(monkeypatch)
    make_report(reports_root, 1, "999998", "overview",
                __import__("datetime").date.today().strftime("%Y%m%d"),
                {"code": "999998", "report_type": "overview", "writer_mode": "contract",
                 "user_id": 42})  # 放在 u1 目录里，meta 却说是 42
    assert api_server._reuse_today("999998", "overview", user_id=1) is None


# ===== API 端点 =====

@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "DB_PATH", db_path)
    auth_db.init_db(seed_admin=False)
    return db_path, _make_user("13900000001")


def _make_user(username: str) -> int:
    conn = auth_db.get_conn()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, status, created_at) "
            "VALUES (?, ?, 'active', ?)",
            (username, auth_db.hash_password("pw123456"), "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        return conn.execute("SELECT id FROM users WHERE username = ?",
                            (username,)).fetchone()["id"]
    finally:
        conn.close()


@pytest.fixture()
def client(db):
    return TestClient(api_server.app)


def _login(client, username="13900000001", password="pw123456"):
    r = client.post("/api/auth/login", json={"account": username, "password": password})
    assert r.status_code == 200, r.text


# 10. 未登录 → 401（沿用 _auth_gate）
def test_endpoints_require_login(client):
    assert client.get("/api/report/list").status_code == 401
    assert client.get("/api/report/view/002594.overview.20260922").status_code == 401


# 9. /api/report/list 不被 {task_id} 吃掉（顺序回归锁定）
def test_list_endpoint_is_not_swallowed_by_task_id_route(client, db):
    _login(client)
    r = client.get("/api/report/list")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and "items" in body and "types" in body
    # 若被 /api/report/{task_id} 捕获，症状是 404 {"detail":"任务不存在"}
    assert "任务不存在" not in r.text


def test_list_endpoint_returns_only_own_reports(client, db, reports_root):
    _login(client)
    uid = db[1]
    make_report(reports_root, uid, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview", user_id=uid))
    make_report(reports_root, uid + 1, "600519", "overview", "20260922",
                stock_meta("600519", "贵州茅台", "overview", user_id=uid + 1))

    body = client.get("/api/report/list").json()
    assert body["total"] == 1
    assert body["items"][0]["code"] == "002594"


def test_view_endpoint_serves_html_and_blocks_cross_user(client, db, reports_root):
    _login(client)
    uid = db[1]
    make_report(reports_root, uid, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview", user_id=uid))

    r = client.get("/api/report/view/002594.overview.20260922")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "002594" in r.text

    # 非法 id → 404，且不回显路径
    r2 = client.get("/api/report/view/..%2F..%2Fetc%2Fpasswd")
    assert r2.status_code == 404
    assert "etc" not in r2.text and "reports" not in r2.text

    r3 = client.get("/api/report/view/600519.overview.20260922")  # 不属于本人
    assert r3.status_code == 404


def test_endpoints_accept_type_and_code_filters(client, db, reports_root):
    _login(client)
    uid = db[1]
    make_report(reports_root, uid, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview", user_id=uid))
    make_report(reports_root, uid, "BK1036", "board", "20260922",
                {"bk": "BK1036", "name": "半导体", "report_type": "board",
                 "generated_at": "2026-09-22 16:00", "user_id": uid})

    body = client.get("/api/report/list?type=board").json()
    assert [it["report_id"] for it in body["items"]] == ["BK1036.board.20260922"]
    assert len(body["types"]) == 2  # 全量镜像

    body2 = client.get("/api/report/list?code=002594").json()
    assert body2["total"] == 1


def test_list_include_legacy_is_off_by_default(client, db, reports_root):
    """D7 定稿：无用户段的历史产物默认不进库（不擅自判定归属）。"""
    _login(client)
    uid = db[1]
    # legacy 形态：直接放在扫描根下，无 u{N} 段
    legacy = reports_root / "002594" / "overview" / "20260922"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "report.html").write_text("<html>legacy</html>", encoding="utf-8")
    (legacy / "meta.json").write_text(
        json.dumps(stock_meta("002594", "比亚迪", "overview"), ensure_ascii=False),
        encoding="utf-8")

    assert client.get("/api/report/list").json()["total"] == 0
    body = client.get("/api/report/list?include_legacy=true").json()
    assert body["total"] == 1
    assert body["include_legacy"] is True

    # 且 legacy 条目不能被越权打开成「不属于任何人的公共文件」
    r = client.get("/api/report/view/002594.overview.20260922")
    assert r.status_code == 404, "legacy 产物不属于当前用户，view 必须拒绝"
    # 个人根里放一份同名产物后即可正常打开（证明上一条失败是「根」而非「格式」）
    make_report(reports_root, uid, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview", user_id=uid))
    assert client.get("/api/report/view/002594.overview.20260922").status_code == 200


def test_relative_root_does_not_leak_other_users(client, db, reports_root):
    """另一个用户登录后，只能看到自己的库（D1 的验收核心）。"""
    _login(client)
    uid = db[1]
    conn = auth_db.get_conn()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, status, created_at) "
            "VALUES (?, ?, 'active', ?)",
            ("13900000009", auth_db.hash_password("pw123456"), "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        uid2 = conn.execute("SELECT id FROM users WHERE username = ?",
                            ("13900000009",)).fetchone()["id"]
    finally:
        conn.close()

    make_report(reports_root, uid, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview", user_id=uid))
    make_report(reports_root, uid2, "002594", "overview", "20260922",
                stock_meta("002594", "比亚迪", "overview", user_id=uid2))

    client2 = TestClient(api_server.app)
    _login(client2, "13900000009")
    body = client2.get("/api/report/list").json()
    assert body["total"] == 1
    assert body["items"][0]["user_id"] == uid2
