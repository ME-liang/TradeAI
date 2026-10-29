# -*- coding: utf-8 -*-
"""报告任务中心（F-04）测试：任务列表 / 用户隔离 / 容量淘汰 / report_id 接缝 / API / 源码护栏。

全部离线、不触网、不跑 pipeline：
- `TaskManager` 用独立实例（`monkeypatch` 掉 `api_server.tm`），不污染真实服务；
- 报告产物用代码直接造在 `tmp_path`（`report_index._DEFAULT_BASE` 与 `storage._DEFAULT_BASE`
  一起替换），不触碰真实 `output/reports`；
- auth 库指向 `tmp_path`，不触碰真实 `data/auth.db`。

本文件的重点是两类断言：

1. **类级不变量**：`GET /api/tasks` 下发的 `view_url` **必须真的能打开**
   （`test_view_url_from_task_center_is_openable`）。这是 N-21 的直接教训 ——
   当时报告库「列表广告了详情结构性拒绝的条目」，抽查一种类型等于没查。
2. **源码级护栏**：8000 是否代理了 `/api/tasks`、7 个页面是否都引了 `js/task_center.js`、
   `_worker` 的每条 SUCCESS 路径是否都回填了 `report_id`。
   这三件事漏掉任何一件都是**静默**失效（404 / 功能不存在 / 链接打不开），
   靠人复读文档挡不住（N-15 / N-16 / N-18 是同一反模式在同一天里犯了三次）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fundamental_agent import report_index as ri
from fundamental_agent import storage
from fundamental_agent.api import server as api_server
from fundamental_agent.api.tasks import ACTIVE_STATUSES, TaskManager
from fundamental_agent.auth import db as auth_db
from fundamental_agent.auth import service as auth_service

ROOT = Path(__file__).resolve().parents[1]


# ===== 夹具 =====


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时 auth 库（两个用户 + 各一个会话）+ 临时报告根 + 独立的 TaskManager。"""
    db_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "DB_PATH", db_path)
    auth_db.init_db(seed_admin=False)

    conn = auth_db.get_conn()
    try:
        user_ids = []
        for phone in ("13900000001", "13900000002"):
            conn.execute(
                "INSERT INTO users (username, password_hash, status, created_at) "
                "VALUES (?, ?, 'active', ?)",
                (phone, auth_db.hash_password("pw123456"), "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
            user_ids.append(conn.execute(
                "SELECT id FROM users WHERE username = ?", (phone,)).fetchone()["id"])
        tokens = [auth_service._create_session(conn, uid) for uid in user_ids]
    finally:
        conn.close()

    root = tmp_path / "reports"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ri, "_DEFAULT_BASE", root)
    monkeypatch.setattr(storage, "_DEFAULT_BASE", root)

    tm = TaskManager()
    monkeypatch.setattr(api_server, "tm", tm)

    return {"root": root, "user_ids": user_ids, "tokens": tokens, "tm": tm}


@pytest.fixture()
def client():
    return TestClient(api_server.app)


def _auth(tok: str) -> dict:
    return {"Cookie": "%s=%s" % (auth_service.SESSION_COOKIE, tok)}


def _make_report(root: Path, user_id: int, code: str, report_type: str, date_seg: str,
                 name: str = "") -> Path:
    """造一份真实报告产物（report.html + meta.json），返回 report.html 路径。"""
    d = root / ("u%d" % user_id) / code / report_type / date_seg
    d.mkdir(parents=True, exist_ok=True)
    html = d / "report.html"
    html.write_text("<html><body>%s %s</body></html>" % (code, name), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({
        "code": code, "name": name, "report_type": report_type,
        "generated_at": "2026-09-26 10:00", "user_id": user_id,
    }, ensure_ascii=False), encoding="utf-8")
    return html


# ===== A. TaskManager.list：按用户过滤是唯一出口 =====


def test_list_returns_only_own_tasks():
    tm = TaskManager()
    a = tm.create("600519", report_type="overview", user_id=1)
    b = tm.create("600519", report_type="overview", user_id=2)
    anonymous = tm.create("600519", report_type="overview", user_id=None)

    assert [t.task_id for t in tm.list(1)] == [a.task_id]
    assert [t.task_id for t in tm.list(2)] == [b.task_id]
    # user_id=None **不是**「不过滤」：它只匹配同样无归属的任务
    assert [t.task_id for t in tm.list(None)] == [anonymous.task_id]
    assert tm.list(3) == []


def test_list_newest_first_and_stable():
    tm = TaskManager()
    ids = [tm.create("60000%d" % i, report_type="overview", user_id=1).task_id
           for i in range(5)]
    got = [t.task_id for t in tm.list(1)]
    assert got == list(reversed(ids)), "最新的任务必须排在最前（任务中心顶部是刚提交的）"
    assert got == [t.task_id for t in tm.list(1)], "同一批任务两次请求顺序必须一致"


def test_list_active_only_and_count():
    tm = TaskManager()
    running = tm.create("600519", report_type="overview", user_id=1)
    tm.update(running.task_id, status="RUNNING", progress=40, stage="取数")
    done = tm.create("600520", report_type="overview", user_id=1)
    tm.update(done.task_id, status="SUCCESS", progress=100)

    assert [t.task_id for t in tm.list(1, active_only=True)] == [running.task_id]
    assert tm.active_count(1) == 1
    assert tm.active_count(2) == 0, "计数也必须按用户隔离"


def test_list_limit_and_zero_means_all():
    tm = TaskManager()
    for i in range(8):
        tm.create("60000%d" % i, report_type="overview", user_id=1)
    assert len(tm.list(1, limit=3)) == 3
    assert len(tm.list(1, limit=0)) == 8
    # 超过上限的 limit 被钳到 MAX_LIST_LIMIT
    assert len(tm.list(1, limit=10 ** 6)) == 8


def test_create_dedups_so_list_has_single_item():
    tm = TaskManager()
    t1 = tm.create("600519", report_type="overview", user_id=1)
    t2 = tm.create("600519", report_type="overview", user_id=1)
    assert t1.task_id == t2.task_id
    assert len(tm.list(1)) == 1, "去重命中时列表不应出现两条同一任务"


def test_create_dedup_key_includes_user_id():
    """两个用户同时生成同一只票必须是两个任务（F-06 D1 的「键漏一个维度」）。"""
    tm = TaskManager()
    assert tm.create("600519", report_type="overview", user_id=1).task_id != \
        tm.create("600519", report_type="overview", user_id=2).task_id


# ===== B. 时间戳 =====


def test_timestamps_are_set_and_updated_at_refreshes():
    tm = TaskManager()
    t = tm.create("600519", report_type="overview", user_id=1)
    assert t.created_at and t.updated_at
    assert t.created_at == t.updated_at, "刚创建时两者应相同"
    first = t.updated_at
    tm.update(t.task_id, progress=30, stage="取数")
    assert tm.get(t.task_id).updated_at >= first
    assert tm.get(t.task_id).created_at == t.created_at, "created_at 不随后续更新改变"


def test_update_tolerates_unknown_fields_and_missing_task():
    tm = TaskManager()
    t = tm.create("600519", report_type="overview", user_id=1)
    tm.update(t.task_id, 不存在的字段="x")          # 不应抛异常
    assert tm.get(t.task_id) is not None
    tm.update("no-such-task", status="SUCCESS")     # 不存在则静默忽略
    assert tm.get("no-such-task") is None


# ===== C. 容量淘汰：只动终态 =====


def test_evict_drops_oldest_terminal_tasks():
    tm = TaskManager(max_tasks=3)
    ids = []
    for i in range(3):
        t = tm.create("60000%d" % i, report_type="overview", user_id=1)
        tm.update(t.task_id, status="SUCCESS")
        ids.append(t.task_id)
    t4 = tm.create("600004", report_type="overview", user_id=1)
    tm.update(t4.task_id, status="SUCCESS")
    t5 = tm.create("600005", report_type="overview", user_id=1)
    tm.update(t5.task_id, status="SUCCESS")

    assert tm.get(ids[0]) is None and tm.get(ids[1]) is None, "最旧的终态任务应被淘汰"
    assert tm.get(ids[2]) is not None and tm.get(t4.task_id) is not None
    assert tm.get(t5.task_id) is not None
    assert len(tm.list(1, limit=0)) == 3


def test_evict_never_drops_running_task():
    """淘汰运行中任务会让 worker 的 update() 静默失效 → 进度条永久卡死。"""
    tm = TaskManager(max_tasks=1)
    running = tm.create("600519", report_type="overview", user_id=1)
    tm.update(running.task_id, status="RUNNING", progress=47)
    for i in range(5):
        tm.create("6005%02d" % i, report_type="overview", user_id=1)   # 全是 PENDING

    assert tm.get(running.task_id) is not None
    assert tm.get(running.task_id).status == "RUNNING"
    tm.update(running.task_id, progress=80, stage="渲染")
    assert tm.get(running.task_id).progress == 80, "运行中任务的进度更新不能被淘汰吃掉"


def test_active_statuses_constant_covers_state_machine():
    assert set(ACTIVE_STATUSES) == {"PENDING", "RUNNING"}, \
        "create 去重 / list(active_only) / _evict 三处共用同一常量，改这里就要一起改"


# ===== D. report_id 反推（F-04 ↔ F-06 的接缝） =====


def test_report_id_derived_from_dirname_not_from_today():
    """必须从**真实目录名**反推：行业对比报告的日期段带 `__{bk}` 后缀（D8）。"""
    p = "/x/u1/002594/industry/20260926__BK0890/report.html"
    assert ri.report_id_for_path(p, "002594", "industry") == "002594.industry.20260926__BK0890"
    assert ri.report_id_for_path("/x/u1/300184/fundamental/20260925/report.html",
                                "300184", "fundamental") == "300184.fundamental.20260925"
    assert ri.report_id_for_path("/x/u1/BK0890/board/20260926/report.html",
                                 "BK0890", "board") == "BK0890.board.20260926"


def test_report_id_rejects_illegal_segment():
    """段名非法时宁可**不给链接**，也不给一个点开就 404 的 id。

    注意：非法段必须是**日期段本身**（`report.html` 的父目录名）。写成
    `.../600519/%2e%2e/20260926/report.html` 是无效用例 —— 那里 `parent.name` 是
    `20260926`，本来就应该通过。
    """
    # 日期段含 `%` / `.` / 长度越界 → 一律拒绝
    assert ri.report_id_for_path("/x/u1/600519/..%2F..%2Fetc/report.html",
                                "600519", "overview") == ""
    assert ri.report_id_for_path("/x/u1/600519/../report.html",
                                 "600519", "overview") == ""
    assert ri.report_id_for_path("/x/u1/600519/%s/report.html" % ("a" * 33),
                                 "600519", "overview") == ""
    # 合法段（含 D8 后缀）必须通过，且能被反解 —— 否则列表里会出现打不开的条目
    for seg, expect in [("20260926", "600519.overview.20260926"),
                        ("20260926__BK0890", "600519.overview.20260926__BK0890")]:
        rid = ri.report_id_for_path("/x/u1/600519/overview/%s/report.html" % seg,
                                    "600519", "overview")
        assert rid == expect
        assert ri.parse_report_id(rid) is not None


# ===== E. GET /api/tasks =====


def test_tasks_endpoint_requires_login(client):
    assert client.get("/api/tasks").status_code == 401


def test_tasks_endpoint_isolates_users(env, client):
    tm = env["tm"]
    t1 = tm.create("600519", report_type="overview", user_id=env["user_ids"][0])
    t2 = tm.create("600519", report_type="overview", user_id=env["user_ids"][1])

    d1 = client.get("/api/tasks", headers=_auth(env["tokens"][0])).json()
    d2 = client.get("/api/tasks", headers=_auth(env["tokens"][1])).json()
    assert [it["task_id"] for it in d1["items"]] == [t1.task_id]
    assert [it["task_id"] for it in d2["items"]] == [t2.task_id]
    assert d1["items"][0]["task_id"] != d2["items"][0]["task_id"]


def test_tasks_endpoint_contract(env, client):
    tm = env["tm"]
    t = tm.create("600519", report_type="overview", user_id=env["user_ids"][0], name="贵州茅台")
    tm.update(t.task_id, status="RUNNING", progress=42, stage="取财务数据")

    d = client.get("/api/tasks", headers=_auth(env["tokens"][0])).json()
    assert d["ok"] is True and d["active"] == 1 and d["count"] == 1
    it = d["items"][0]
    assert it["status"] == "RUNNING" and it["progress"] == 42 and it["stage"] == "取财务数据"
    assert it["report_type_label"] == "基本面分析" or it["report_type_label"] == "公司概况"
    assert it["name"] == "贵州茅台"
    assert it["created_at"] and it["updated_at"]
    # 跑中任务**不得**下发链接（还没有产物）
    assert it["view_url"] == "" and it["report_id"] == ""
    assert it["subject_url"] == "/stock_detail.html?code=600519"


def test_view_url_from_task_center_is_openable(env, client):
    """**类级不变量**：任务中心给出的每个 `view_url` 都必须真的能打开。

    这是 N-21 的教训 —— 当时报告库"列表广告了详情结构性拒绝的条目"，且旧用例
    只抽查了 `overview` 一种类型。这里遍历 4 种报告类型（含 board 与带 `__{bk}`
    后缀的 industry），逐条真发一次 HTTP。
    """
    root, uid, tok = env["root"], env["user_ids"][0], env["tokens"][0]
    tm = env["tm"]
    cases = [
        ("300184", "fundamental", "20260925"),
        ("002594", "overview", "20260926"),
        ("002594", "industry", "20260926__BK0890"),
        ("BK0890", "board", "20260926"),
    ]
    for code, rtype, seg in cases:
        html = _make_report(root, uid, code, rtype, seg, name=code)
        task = tm.create(code, bk=("BK0890" if rtype == "board" else ""),
                         report_type=rtype, user_id=uid)
        tm.update(task.task_id, status="SUCCESS", progress=100, stage="完成",
                  html_path=str(html),
                  report_id=ri.report_id_for_path(html, code, rtype))

    d = client.get("/api/tasks", headers=_auth(tok)).json()
    assert d["count"] == len(cases)
    for it in d["items"]:
        rid = it["report_id"]
        assert rid, "SUCCESS 任务必须带持久 report_id（否则任务中心只能用内存 task_id，重启即失效）"
        assert it["view_url"] == "/api/report/view/%s" % rid
        r = client.get(it["view_url"], headers=_auth(tok))
        assert r.status_code == 200, "任务中心下发的链接打不开：%s" % rid
        assert r.headers["content-type"].startswith("text/html")

    # board 的主体页是板块页（它没有个股页）
    board_it = [it for it in d["items"] if it["report_type"] == "board"][0]
    assert board_it["subject_url"] == "/sector_detail.html?code=BK0890"


def test_unknown_report_type_is_not_dropped(env, client):
    """未知类型（将来新增的）不得在任务中心消失，label 退回 key（与报告库同口径）。"""
    t = env["tm"].create("600519", report_type="brand_new_type",
                         user_id=env["user_ids"][0])
    d = client.get("/api/tasks", headers=_auth(env["tokens"][0])).json()
    assert [it["task_id"] for it in d["items"]] == [t.task_id]
    assert d["items"][0]["report_type_label"] == "brand_new_type"


def test_tasks_endpoint_not_shadowed_by_report_route(env, client):
    """/api/tasks 与 /api/report/{task_id} 是不同前缀，不应互相吞掉（怕将来有人「顺手」合并路由）。"""
    r = client.get("/api/tasks", headers=_auth(env["tokens"][0]))
    assert r.status_code == 200 and "items" in r.json()


# ===== F. 源码级护栏 =====


def test_frontend_proxies_api_tasks_with_session_guard():
    """8000 侧必须代理 /api/tasks **并**带会话校验。

    漏代理 → 任务中心静默 404（前端只能显示"加载失败"）；
    漏会话校验 → 未登录即可读到含 `html_path` 与失败原因的任务列表。
    两者都是"跑起来看着像好的"那类失效，故用源码断言挡住。
    """
    src = (ROOT / "frontend" / "server.py").read_text(encoding="utf-8")
    i = src.find('startswith("/api/tasks")')
    assert i >= 0, "frontend/server.py 未代理 /api/tasks → 任务中心必然 404"
    seg = src[i:i + 900]
    assert "_proxy_report" in seg, "命中 /api/tasks 后必须转发到 8002"
    assert "_session_ok" in seg, "任务项含 html_path/错误文本，代理必须要求有效会话"


def test_every_report_page_loads_task_center():
    """能在页面里发起报告生成的所有页面都必须引 js/task_center.js。

    少一个页面 = 该页面切走后任务进度不可见（F-04 要解决的核心症状），
    而页面本身看起来一切正常 —— 静默失效，只能靠遍历断言。
    """
    pages = ["index.html", "stock.html", "stock_detail.html", "sector.html",
             "sector_detail.html", "watchlist.html", "reports.html"]
    for p in pages:
        text = (ROOT / "frontend" / p).read_text(encoding="utf-8")
        assert 'src="js/task_center.js"' in text, "%s 未引入 js/task_center.js" % p


def test_reports_page_wires_task_center_and_auto_refresh():
    """报告库页必须：①有内联挂载点；②监听任务完成事件并重载列表（「自动进入报告库」）。"""
    text = (ROOT / "frontend" / "reports.html").read_text(encoding="utf-8")
    assert 'id="taskCenter"' in text, "报告库页缺少任务中心挂载点 #taskCenter"
    assert "tradeai:task-done" in text, \
        "报告库页未监听 tradeai:task-done → 任务完成后列表不会自动刷新（需用户手动 F5）"
    assert re.search(r"tradeai:task-done'[\s\S]{0,200}?load\(\)", text), \
        "监听 tradeai:task-done 后必须调用 load() 重载列表"


def test_task_center_js_is_syntactically_valid():
    """任务中心脚本必须能过 `node --check`。

    极小的投入换一类很贵的失效：`js/task_center.js` 语法坏了，页面**不会报错给用户**，
    只是任务中心整个不出现（F-04 静默退化）。用 `node --check` 只需语法，不跑代码。
    本机没有 node 时跳过（与 `.workbuddy` 的 managed node / 系统 node 二选一）。
    """
    node = shutil.which("node")
    if not node:
        for cand in (r"C:\Users\lwn\.workbuddy\binaries\node\versions\22.22.2-3\node.exe",
                     r"D:\developer_tools\nvm\nodejs\node.exe"):
            if Path(cand).is_file():
                node = cand
                break
    if not node:
        pytest.skip("本机无 node，跳过 JS 语法检查")

    js = ROOT / "frontend" / "js" / "task_center.js"
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "js/task_center.js 语法检查失败：\n%s" % (r.stderr or r.stdout)


def test_worker_fills_report_id_on_every_success_path():
    """`_worker` + 复用分支共有 3 条 SUCCESS 路径，每条都必须回填 report_id。

    数字对不上就说明有人新增了 SUCCESS 出口却没接上接缝 —— 那条任务的报告在
    任务中心里将永远没有「打开报告」按钮（或更糟：给个打不开的内存链接）。
    """
    src = (ROOT / "src" / "fundamental_agent" / "api" / "server.py").read_text(encoding="utf-8")
    success = len(re.findall(r'status="SUCCESS"', src))
    filled = len(re.findall(r"report_id=report_index\.report_id_for_path\(", src))
    assert success == 3, "SUCCESS 出口数量变了（当前应为 board / 常规 / 复用 三条），请复核本测试"
    assert filled == success, (
        "有 %d 条 SUCCESS 路径但只有 %d 处回填 report_id —— 漏掉的那条在任务中心里没有可用链接"
        % (success, filled)
    )


# ===== G. 删除任务记录（POST /api/tasks/remove，2026-09-26）=====
#
# 需求原文：「任务报告中心失败的应该要支持手动删除任务」。
# 删除是本模块里**唯一会减小 `_tasks` 的用户操作**（另一处是容量淘汰），所以它与
# `_evict_locked()` 共用同一条约束：**只动终态**。这组用例围绕三个"会静默出错"的点：
#   ① 活跃任务被删 → 该 worker 的 update() 静默失效 → 进度条永久卡死；
#   ② 跨用户删除 → 不只越权，还会把本端点变成他人 task_id 的**存在性探测器**；
#   ③ 「删任务」被误当成「删报告」→ 产物其实还在报告库（决定 UI 入口只给失败行）。


def _mk_failed(tm, user_id, *, bk="BK9999",
               err="板块成员获取失败（bk=BK9999），无法生成报告"):
    """造一条失败任务（board 类型，与截图里那两条「不存在的板块 BK9999」同形）。"""
    t = tm.create(bk=bk, report_type="board", user_id=user_id)
    tm.update(t.task_id, status="FAILED", stage="失败", error=err)
    return t


def _remove(client, tok, *task_ids):
    return client.post("/api/tasks/remove", headers=_auth(tok),
                       json={"task_ids": list(task_ids)})


def test_remove_deletes_own_terminal_task(env, client):
    tm, uid, tok = env["tm"], env["user_ids"][0], env["tokens"][0]
    a = _mk_failed(tm, uid)
    b = _mk_failed(tm, uid, bk="BK0000")

    d = _remove(client, tok, a.task_id).json()
    assert d["ok"] is True and d["deleted"] == 1 and d["count"] == 1

    left = client.get("/api/tasks", headers=_auth(tok)).json()
    assert [it["task_id"] for it in left["items"]] == [b.task_id], "被删的任务仍在列表里"

    # 幂等：再删同一个 id 不报错，也不再计入 deleted
    d2 = _remove(client, tok, a.task_id).json()
    assert d2["deleted"] == 0 and d2["not_found"] == 1 and d2["count"] == 1


def test_remove_refuses_active_task_and_keeps_progress_alive(env, client):
    """**活跃任务不可删** —— 删掉它会让 worker 的 update() 静默 no-op、进度条永久卡死。

    本组最关键的用例：不只断言"没删掉"，还断言**该任务随后的 update 仍然生效**。
    退化实现有两种可能 —— 直接删掉、或删掉后又塞回一个新对象（后者 update 打到幽灵对象上，
    列表里看着"任务还在"却永远不动），只断言"任务还在"会漏掉第二种。
    """
    tm, uid, tok = env["tm"], env["user_ids"][0], env["tokens"][0]
    t = tm.create("300184", report_type="fundamental", user_id=uid)
    tm.update(t.task_id, status="RUNNING", progress=47, stage="生成中")

    d = _remove(client, tok, t.task_id).json()
    assert d["deleted"] == 0 and d["skipped_active"] == [t.task_id], \
        "活跃任务必须被拒删并出现在 skipped_active 里（静默少删 = 删除键点了没用）"
    assert d["count"] == 1

    tm.update(t.task_id, progress=77)
    assert tm.get(t.task_id) is not None
    assert tm.get(t.task_id).progress == 77, \
        "活跃任务被删掉后 update 静默失效 —— 前端进度条会永久卡在 47%"
    assert client.get("/api/tasks", headers=_auth(tok)).json()["items"][0]["progress"] == 77


def test_remove_isolates_users_and_hides_existence(env, client):
    """跨用户删除：既不能删掉别人的任务，也不能**泄露该 task_id 是否存在**。

    「存在但属于别人」与「压根不存在」必须返回**完全一样的响应** —— 否则本端点就成了
    他人 task_id 的存在性探测器（据此可推断别人正在为哪只票生成什么报告）。
    这是 `remove()` 用 `not_found` 而不引入 `forbidden` 的原因，故用相等断言锁死。
    """
    tm, uids, toks = env["tm"], env["user_ids"], env["tokens"]
    victim = _mk_failed(tm, uids[0])
    ghost = "ffffffffffff"                      # 不存在的 id

    d_other = _remove(client, toks[1], victim.task_id).json()
    d_ghost = _remove(client, toks[1], ghost).json()
    assert d_other == d_ghost, \
        "「别人的任务」与「不存在的任务」响应不同 → 可被用来探测他人 task_id 是否存在"
    assert d_other["deleted"] == 0 and d_other["not_found"] == 1

    mine = client.get("/api/tasks", headers=_auth(toks[0])).json()
    assert [it["task_id"] for it in mine["items"]] == [victim.task_id], "别人的任务被我删掉了"


def test_remove_requires_login(client):
    assert client.post("/api/tasks/remove", json={"task_ids": ["x"]}).status_code == 401


def test_remove_batch_dedups_and_reports_missing(env, client):
    tm, uid, tok = env["tm"], env["user_ids"][0], env["tokens"][0]
    a = _mk_failed(tm, uid)
    b = _mk_failed(tm, uid, bk="BK0000")

    d = _remove(client, tok, a.task_id, a.task_id, b.task_id, "nope").json()
    assert d["deleted"] == 2, "重复传入同一 id 不应计两次"
    assert d["not_found"] == 1
    assert client.get("/api/tasks", headers=_auth(tok)).json()["items"] == []


def test_remove_empty_list_is_noop(env, client):
    d = _remove(client, env["tokens"][0]).json()
    assert set(["ok", "deleted", "not_found", "skipped_active", "count"]) <= set(d)
    assert d["deleted"] == 0 and d["not_found"] == 0 and d["skipped_active"] == []


def test_remove_task_does_not_touch_disk_report(env, client):
    """**删任务 ≠ 删报告**：删掉 SUCCESS 任务记录后，产物仍在报告库、链接仍能打开。

    这条边界决定了 UI 的入口位置（只给失败/异常行）。若哪天有人把删除按钮挪到
    「已完成」行上，用户会以为自己在删报告 —— 而实际什么产物也没删掉。
    """
    root, uid, tok = env["root"], env["user_ids"][0], env["tokens"][0]
    tm = env["tm"]
    html = _make_report(root, uid, "600519", "overview", "20260926", name="贵州茅台")
    task = tm.create("600519", report_type="overview", user_id=uid)
    rid = ri.report_id_for_path(html, "600519", "overview")
    assert rid, "报告没造对（report_id 反推失败），本用例失去意义"
    tm.update(task.task_id, status="SUCCESS", progress=100, stage="完成",
              html_path=str(html), report_id=rid)

    assert _remove(client, tok, task.task_id).json()["deleted"] == 1
    assert client.get("/api/tasks", headers=_auth(tok)).json()["count"] == 0

    # 产物与报告库均不受影响
    assert client.get("/api/report/view/%s" % rid, headers=_auth(tok)).status_code == 200
    listed = client.get("/api/report/list", headers=_auth(tok)).json()
    assert listed["total"] >= 1, "删除任务记录后报告库空了 —— 「删任务」不能动磁盘产物"


# ===== H. 删除链路的源码级护栏 =====


def test_frontend_post_proxies_api_tasks():
    """8000 侧必须**在 do_POST 里**代理 /api/tasks。

    8000 只有 do_GET/do_POST（无 do_DELETE），所以删除用 POST 表达是本项目既有约定
    （F-03 的 `POST /api/watchlist/remove` 就是这条路线）。漏配的后果：请求落到
    `do_POST` 末尾的 `self.send_error(405)`，页面表现只是"点了删除没反应" ——
    405 只在浏览器控制台可见。用 DELETE 更糟（501，本侧连方法都没有）。
    """
    src = (ROOT / "frontend" / "server.py").read_text(encoding="utf-8")
    i = src.find("def do_POST")
    assert i >= 0, "frontend/server.py 里找不到 do_POST"
    j = src.find('startswith("/api/tasks")', i)
    assert j >= 0, "do_POST 未代理 /api/tasks → 删除任务是 405，页面表现为「点了没反应」"
    seg = src[j:j + 800]
    assert '_proxy_report("POST")' in seg, "do_POST 命中 /api/tasks 后必须转发到 8002"
    assert "_session_ok" in seg, "删除会改服务端状态，代理必须要求有效会话"


def test_delete_button_only_on_terminal_rows():
    """删除按钮**只能**出现在终态行。

    活跃行的删除按钮 = "用户可点着把进度条删卡死"的口子，而 UI 层是唯一能挡住它的地方
    （服务端 `skipped_active` 是兜底，不是替代）。断言方式：切出 `rowHtml` 函数体，
    活跃分支里不得出现 `delBtn`，且全函数恰好 2 处调用（失败行 + 定位不到产物的成功行）。
    """
    js = (ROOT / "frontend" / "js" / "task_center.js").read_text(encoding="utf-8")
    i = js.find("function rowHtml")
    assert i >= 0, "js/task_center.js 里找不到 rowHtml"
    j = js.find("\n  }", i)
    body = js[i:j]
    k = body.find("if (isActive(it))")
    s = body.find("if (it.status === 'SUCCESS')")
    assert 0 <= k < s, "rowHtml 的分支结构变了（活跃 / SUCCESS / FAILED），请复核本用例"
    assert "delBtn" not in body[k:s], \
        "活跃（PENDING/RUNNING）任务行渲染了删除按钮 —— 删掉运行中任务会让进度条永久卡死"
    assert body.count("delBtn(") == 2, (
        "删除按钮的调用点应为 2 处（失败行 + 「成功但定位不到产物」行），当前 %d 处；"
        "少一处 = 某类失败记录清不掉，多一处就要问一句「多在哪一行上」"
        % body.count("delBtn(")
    )


def test_task_center_removal_uses_post_and_reads_receipt():
    """前端必须：①用 POST 调删除端点；②按服务端回执更新本地列表。

    ①用 DELETE 会撞 8000 侧没有 do_DELETE → 501（同 `test_frontend_post_proxies_api_tasks`
    的另一面）；②不看回执的写法（把点过的 id 全从本地抹掉）会制造**幽灵条目**：
    服务端拒删活跃任务、本地却认为删成功了，下一次轮询它又冒出来 —— 比直接报错更难排查。
    """
    js = (ROOT / "frontend" / "js" / "task_center.js").read_text(encoding="utf-8")
    assert "/api/tasks/remove" in js, "前端未指向删除端点 /api/tasks/remove"
    assert "method: 'POST'" in js, "删除必须用 POST（8000 侧无 do_DELETE，DELETE 会 501）"
    assert "skipped_active" in js, \
        "前端未读 skipped_active —— 被服务端拒删的任务会在本地消失、下次轮询又回来"
    assert "state.busy" in js, "删除进行中未置忙 → 连点会重复发请求"
