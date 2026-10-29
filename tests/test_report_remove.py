# -*- coding: utf-8 -*-
"""报告库删除（F-06 扩展）测试：路径护栏 / 物理删除语义 / 活跃任务守卫 / 跨模块死链。

全部离线、不触网、不跑 pipeline：
- 报告产物直接造在 `tmp_path`（`report_index._DEFAULT_BASE` 与 `storage._DEFAULT_BASE`
  一起替换），**不触碰真实 `output/reports`**；
- auth 库指向 `tmp_path`，不触碰真实 `data/auth.db`；
- `TaskManager` 用独立实例（`monkeypatch` 掉 `api_server.tm`）。

本文件锁的是**不可逆操作**的边界，因此断言比普通功能测试更偏执。三类重点：

1. **删不到不该删的东西**：跨用户（`not_found`）、legacy 共享根（结构上不可能）、
   同 code 下的兄弟报告、用户根以上的目录。删错一次就是数据损失，不是显示错误。
2. **失败必须可见**：批量删除允许部分成功，但每一条没删掉的原因都要出现在响应里
   （`not_found` / `skipped_busy` / `failed`）—— 静默少删是这类功能最难排查的故障。
3. **删报告必须收回任务中心的入口**：SUCCESS 任务下发的是 `/api/report/view/{report_id}`，
   报告没了而记录还在 = 一个点开就 404 的链接（N-21 的形状）。
"""
from __future__ import annotations

import errno
import json
import re
import shutil
import subprocess
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fundamental_agent import report_index as ri
from fundamental_agent import storage
from fundamental_agent.api import server as api_server
from fundamental_agent.api.tasks import TaskManager
from fundamental_agent.auth import db as auth_db
from fundamental_agent.auth import service as auth_service

ROOT = Path(__file__).resolve().parents[1]


# ===== 夹具（与 test_tasks_api.py 同形，便于两处对照阅读）=====


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
                 name: str = "", legacy: bool = False) -> Path:
    """造一份真实报告产物（report.html + meta.json），返回 report.html 路径。

    `legacy=True` 时造在**共享根** `root/` 下（不带 `u{uid}` 段）。
    """
    base = root if legacy else root / ("u%d" % user_id)
    d = base / code / report_type / date_seg
    d.mkdir(parents=True, exist_ok=True)
    html = d / "report.html"
    html.write_text("<html><body>%s %s</body></html>" % (code, name), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({
        "code": code, "name": name, "report_type": report_type,
        "generated_at": "2026-09-26 10:00", "user_id": user_id,
    }, ensure_ascii=False), encoding="utf-8")
    return html


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _node() -> str | None:
    """定位 node（PATH → managed → 系统），与 test_tasks_api.py 同口径。"""
    node = shutil.which("node")
    if not node:
        for cand in (r"C:\Users\lwn\.workbuddy\binaries\node\versions\22.22.2-3\node.exe",
                     r"D:\developer_tools\nvm\nodejs\node.exe"):
            if Path(cand).is_file():
                node = cand
                break
    return node


# ===== A. 路径解析：view 与 delete 必须给出同一个答案 =====


def test_resolve_dir_finds_three_segment_dir(env):
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    d = ri.resolve_report_dir(1, "600519.overview.20260926")
    assert d is not None and d.name == "20260926"
    assert (d / "report.html").is_file()


def test_resolve_dir_accepts_two_segment_fundamental(env):
    """基本面存在两种合法目录形态（N-21）：2 段与 3 段都必须能解析。"""
    d2 = env["root"] / "u1" / "600519" / "20260926"
    d2.mkdir(parents=True)
    (d2 / "report.html").write_text("x", encoding="utf-8")
    assert ri.resolve_report_dir(1, "600519.fundamental.20260926") == d2


def test_resolve_dir_accepts_industry_bk_suffix(env):
    """行业对比报告的日期段带 `__{bk}` 后缀（D8）；解析必须原样接受。"""
    _make_report(env["root"], 1, "300184", "industry", "20260926__BK0890", "力源信息")
    d = ri.resolve_report_dir(1, "300184.industry.20260926__BK0890")
    assert d is not None and d.name == "20260926__BK0890"


@pytest.mark.parametrize("bad", [
    "", "600519.overview", "600519.overview.20260926.x", "..%2Fetc.overview.20260926",
    "600519...20260926", "12345.overview.20260926", "600519.over..view.20260926",
])
def test_resolve_dir_rejects_illegal_id(env, bad):
    """任何不合法的 report_id 一律 None（调用方转 404，不回显路径）。"""
    assert ri.resolve_report_dir(1, bad) is None


def test_resolve_dir_requires_report_html(env):
    """「目录在」不等于「这里有一份报告」：没有 report.html 就不算可删对象。"""
    d = env["root"] / "u1" / "600519" / "overview" / "20260926"
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{}", encoding="utf-8")
    assert ri.resolve_report_dir(1, "600519.overview.20260926") is None


def test_resolve_html_and_dir_never_disagree(env):
    """同一个 id：能解析出目录 ⟺ 能解析出 HTML。

    这是「列表里点得开的」与「删得掉的」永远同集合的实现基础。
    """
    _make_report(env["root"], 1, "600519", "overview", "20260926")
    for rid in ("600519.overview.20260926", "600519.fundamental.20260926"):
        d = ri.resolve_report_dir(1, rid)
        h = ri.resolve_report_html(1, rid)
        assert (d is None) == (h is None), rid
        if d is not None:
            assert h == d / "report.html"


# ===== B. delete_reports：删得准、删不歪、删不掉要说清 =====


def test_delete_removes_target_dir_and_files(env):
    html = _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    res = ri.delete_reports(1, ["600519.overview.20260926"])
    assert res["deleted"] == 1
    assert res["not_found"] == 0 and res["failed"] == []
    assert res["removed_ids"] == ["600519.overview.20260926"]
    assert not html.exists()
    assert not html.parent.exists()          # 目录整体消失，不留半个产物


def test_delete_rejects_empty_user_id(env):
    """`user_id` 为空 → legacy 共享根，必须拒绝而不是「删到共享目录里去」。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926", legacy=True)
    with pytest.raises(ValueError):
        ri.delete_reports(None, ["600519.overview.20260926"])


def test_delete_legacy_entry_is_not_found(env):
    """legacy 条目**看得见、删不掉**（结构上不可能）——与 N-22 不放宽 view 边界同取向。"""
    html = _make_report(env["root"], 1, "000001", "overview", "20260926", legacy=True)
    res = ri.delete_reports(1, ["000001.overview.20260926"])
    assert res["deleted"] == 0 and res["not_found"] == 1
    assert html.is_file(), "共享根里的产物被删掉了 —— 这是跨用户数据损失"


def test_delete_cross_user_is_not_found_and_file_survives(env):
    """拿自己的 user_id 删别人的 report_id：返回 not_found（不泄露存在性），文件原样。"""
    html = _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    res = ri.delete_reports(env["user_ids"][1], ["600519.overview.20260926"])
    assert res["deleted"] == 0 and res["not_found"] == 1
    assert html.is_file()


def test_delete_touches_only_target_sibling_reports(env):
    """同一只票的其它类型 / 其它日期必须完好无损。"""
    keep_type = _make_report(env["root"], 1, "600519", "overview", "20260926")
    keep_day = _make_report(env["root"], 1, "600519", "fundamental", "20260925")
    gone = _make_report(env["root"], 1, "600519", "fundamental", "20260926")

    res = ri.delete_reports(1, ["600519.fundamental.20260926"])
    assert res["deleted"] == 1
    assert not gone.exists()
    assert keep_type.is_file() and keep_day.is_file()


def test_delete_prunes_empty_parents_up_to_user_root(env):
    """删掉最后一份后向上清理空目录，但**用户根本身保留**（库的容器）。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926")
    ri.delete_reports(1, ["600519.overview.20260926"])
    assert not (env["root"] / "u1" / "600519").exists()
    assert (env["root"] / "u1").is_dir(), "用户根不该被删（它是报告库的容器）"


def test_delete_keeps_parent_when_sibling_remains(env):
    """同级还有别的报告时父目录必须保留 —— 这是「绝不递归删父目录」的行为证据。"""
    keep = _make_report(env["root"], 1, "600519", "overview", "20260926")
    _make_report(env["root"], 1, "600519", "fundamental", "20260926")
    ri.delete_reports(1, ["600519.fundamental.20260926"])
    assert keep.is_file()
    assert (env["root"] / "u1" / "600519" / "overview").is_dir()


def test_delete_unknown_id_counts_not_found(env):
    res = ri.delete_reports(1, ["600519.overview.20260926"])
    assert res == {"deleted": 0, "not_found": 1, "failed": [], "skipped_busy": [],
                   "removed_ids": [], "pruned": 0}


def test_delete_dedupes_repeated_ids(env):
    """重复 id 只处理一次：否则第二次会以 not_found 计入，前端报数就对不上。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926")
    rid = "600519.overview.20260926"
    res = ri.delete_reports(1, [rid, rid, rid])
    assert res["deleted"] == 1 and res["not_found"] == 0
    assert res["removed_ids"] == [rid]


def test_delete_clamps_batch_size(env, monkeypatch):
    """批量上限给请求体一个硬上界（防超大 payload），超出部分被截断而非整批拒绝。"""
    monkeypatch.setattr(ri, "MAX_DELETE_BATCH", 1)
    rid1 = "600519.overview.20260926"
    rid2 = "000001.overview.20260926"
    _make_report(env["root"], 1, "600519", "overview", "20260926")
    b = _make_report(env["root"], 1, "000001", "overview", "20260926")
    res = ri.delete_reports(1, [rid1, rid2])
    assert res["deleted"] == 1
    assert b.is_file(), "被截断的那份不该被删"


def test_delete_skips_report_being_regenerated(env):
    """活跃任务守卫：同一 (主体, 类型) 正在生成中 → 跳过，目录原样。

    删掉正在写的目录会让 worker 的 save_report() 中途失败，或写回一个用户以为
    已经删掉的目录 —— 这是「活跃任务绝不能被删除」的第三个入口。
    """
    html = _make_report(env["root"], 1, "600519", "overview", "20260926")
    res = ri.delete_reports(1, ["600519.overview.20260926"],
                            busy_keys={("600519", "overview")})
    assert res["skipped_busy"] == ["600519.overview.20260926"]
    assert res["deleted"] == 0
    assert html.is_file()


def test_delete_partial_failure_is_reported(env, monkeypatch):
    """部分失败必须逐条回报（report_id + 原因）—— 绝不静默少删。"""
    a = _make_report(env["root"], 1, "600519", "overview", "20260926")
    b = _make_report(env["root"], 1, "000001", "overview", "20260926")

    monkeypatch.setattr(ri, "shutil", types.SimpleNamespace(
        rmtree=lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EACCES, "文件被占用"))))

    res = ri.delete_reports(1, ["600519.overview.20260926", "000001.overview.20260926"])
    assert res["deleted"] == 0
    assert [f["report_id"] for f in res["failed"]] == [
        "600519.overview.20260926", "000001.overview.20260926"]
    assert "文件被占用" in res["failed"][0]["error"]
    assert a.is_file() and b.is_file()


def test_delete_continues_after_one_failure(env, monkeypatch):
    """一条失败不该中断整批（批量清理场景下部分成功比整批拒绝更有用）。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926")
    ok = _make_report(env["root"], 1, "000001", "overview", "20260926")

    real = ri.shutil.rmtree

    def flaky(path, *a, **k):
        if "600519" in str(path):
            raise OSError(errno.EACCES, "文件被占用")
        return real(path, *a, **k)

    monkeypatch.setattr(ri, "shutil", types.SimpleNamespace(rmtree=flaky))
    res = ri.delete_reports(1, ["600519.overview.20260926", "000001.overview.20260926"])
    assert res["deleted"] == 1 and len(res["failed"]) == 1
    assert not ok.exists()


# ===== C. TaskManager：活跃守卫与死链清理 =====


def test_active_keys_uses_bk_for_board_and_code_otherwise():
    tm = TaskManager()
    tm.create("600519", report_type="overview", user_id=1)
    tm.create("", bk="BK0890", report_type="board", user_id=1)
    done = tm.create("000001", report_type="overview", user_id=1)
    tm.update(done.task_id, status="SUCCESS")
    tm.create("300184", report_type="fundamental", user_id=2)

    assert tm.active_keys(1) == {("600519", "overview"), ("BK0890", "board")}
    assert tm.active_keys(2) == {("300184", "fundamental")}
    assert tm.active_keys(3) == set()


def test_forget_report_ids_removes_only_matching_terminal_own_tasks():
    tm = TaskManager()
    mine = tm.create("600519", report_type="overview", user_id=1)
    tm.update(mine.task_id, status="SUCCESS", report_id="600519.overview.20260926")
    other_id = tm.create("000001", report_type="overview", user_id=1)
    tm.update(other_id.task_id, status="SUCCESS", report_id="000001.overview.20260926")
    other_user = tm.create("600519", report_type="overview", user_id=2)
    tm.update(other_user.task_id, status="SUCCESS", report_id="600519.overview.20260926")
    running = tm.create("300184", report_type="fundamental", user_id=1)

    n = tm.forget_report_ids(1, ["600519.overview.20260926"])
    assert n == 1
    left = [t.task_id for t in tm.list(1)]
    assert mine.task_id not in left
    assert other_id.task_id in left          # report_id 不同 → 保留
    assert running.task_id in left           # 活跃 → 保留
    assert [t.task_id for t in tm.list(2)] == [other_user.task_id]   # 别人 → 不动


def test_forget_report_ids_ignores_active_task_with_same_report_id():
    """活跃任务即便 report_id 相同也不清（只处理终态，与「活跃不可删」同源）。"""
    tm = TaskManager()
    t = tm.create("600519", report_type="overview", user_id=1)
    tm.update(t.task_id, status="RUNNING", report_id="600519.overview.20260926")
    assert tm.forget_report_ids(1, ["600519.overview.20260926"]) == 0
    assert [x.task_id for x in tm.list(1)] == [t.task_id]


def test_forget_report_ids_empty_input_is_noop():
    tm = TaskManager()
    t = tm.create("600519", report_type="overview", user_id=1)
    tm.update(t.task_id, status="SUCCESS", report_id="600519.overview.20260926")
    assert tm.forget_report_ids(1, []) == 0
    assert tm.forget_report_ids(1, ["", None]) == 0
    assert tm.list(1)


# ===== D. 端点 /api/report/remove =====


def test_remove_endpoint_requires_session(client):
    r = client.post("/api/report/remove", json={"report_ids": []})
    assert r.status_code == 401


def test_remove_endpoint_contract(env, client):
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    r = client.post("/api/report/remove",
                    json={"report_ids": ["600519.overview.20260926"]},
                    headers=_auth(env["tokens"][0]))
    assert r.status_code == 200
    d = r.json()
    for k in ("ok", "deleted", "not_found", "failed", "skipped_busy",
              "removed_ids", "pruned", "tasks_cleared"):
        assert k in d, "响应缺少字段 " + k
    assert d["ok"] is True and d["deleted"] == 1 and d["tasks_cleared"] == 0


def test_remove_endpoint_then_view_404_and_list_excludes(env, client):
    """删完必须**处处一致**：视野打不开、列表不含、磁盘没有。"""
    rid = "600519.overview.20260926"
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    h = _auth(env["tokens"][0])
    assert client.get("/api/report/view/" + rid, headers=h).status_code == 200

    assert client.post("/api/report/remove", json={"report_ids": [rid]},
                       headers=h).json()["deleted"] == 1

    assert client.get("/api/report/view/" + rid, headers=h).status_code == 404
    lst = client.get("/api/report/list", headers=h).json()
    assert lst["total"] == 0
    assert [it for it in lst["items"] if it["report_id"] == rid] == []
    assert not (env["root"] / "u1" / "600519").exists()


def test_remove_endpoint_cross_user_is_not_found(env, client):
    rid = "600519.overview.20260926"
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    r = client.post("/api/report/remove", json={"report_ids": [rid]},
                    headers=_auth(env["tokens"][1]))
    d = r.json()
    assert d["deleted"] == 0 and d["not_found"] == 1
    # 所有者仍然打得开
    assert client.get("/api/report/view/" + rid,
                      headers=_auth(env["tokens"][0])).status_code == 200


def test_remove_endpoint_clears_task_center_dead_link(env, client):
    """删报告必须顺带收回任务中心里那条成功记录，否则它就是一个 404 死链（N-21 形状）。"""
    rid = "300184.fundamental.20260926"
    h = _auth(env["tokens"][0])
    _make_report(env["root"], 1, "300184", "fundamental", "20260926", "力源信息")
    t = env["tm"].create("300184", report_type="fundamental", user_id=1, name="力源信息")
    env["tm"].update(t.task_id, status="SUCCESS", progress=100, stage="完成",
                     report_id=rid)

    # 删前：任务中心给出的链接真的能打开（F-04 的类级不变量）
    item = client.get("/api/tasks", headers=h).json()["items"][0]
    assert item["view_url"] == "/api/report/view/" + rid
    assert client.get(item["view_url"], headers=h).status_code == 200

    d = client.post("/api/report/remove", json={"report_ids": [rid]}, headers=h).json()
    assert d["deleted"] == 1 and d["tasks_cleared"] == 1

    # 删后：任务中心不再挂着这条记录 —— 任何残留的 view_url 都会是 404
    after = client.get("/api/tasks", headers=h).json()
    assert after["count"] == 0
    assert [x for x in after["items"] if x.get("view_url")] == []


def test_remove_endpoint_skipped_busy_is_surfaced(env, client):
    """正在生成中 → 端点必须**显式告知**跳过，而不是静默少删一条。"""
    rid = "600519.overview.20260926"
    _make_report(env["root"], 1, "600519", "overview", "20260926")
    env["tm"].create("600519", report_type="overview", user_id=1)

    d = client.post("/api/report/remove", json={"report_ids": [rid]},
                    headers=_auth(env["tokens"][0])).json()
    assert d["deleted"] == 0
    assert d["skipped_busy"] == [rid]
    assert client.get("/api/report/view/" + rid,
                      headers=_auth(env["tokens"][0])).status_code == 200


def test_list_marks_legacy_entries_not_deletable(env, client):
    """删除能力由服务端下发（`deletable`），前端不自建「这算不算我的」判断。"""
    _make_report(env["root"], 1, "600519", "overview", "20260926", "贵州茅台")
    _make_report(env["root"], 1, "000001", "overview", "20260926", "平安银行", legacy=True)

    d = client.get("/api/report/list?include_legacy=true",
                   headers=_auth(env["tokens"][0])).json()
    by = {it["code"]: it for it in d["items"]}
    assert by["600519"]["deletable"] is True
    assert by["000001"]["deletable"] is False
    # 个人根里的条目默认也可删（新增字段不破坏既有响应）
    d2 = client.get("/api/report/list", headers=_auth(env["tokens"][0])).json()
    assert all(it["deletable"] is True for it in d2["items"])


# ===== E. 源码级护栏（这三类漏配都是**静默**失效，靠复读文档挡不住）=====


def test_router_registers_remove_before_task_id():
    """`/api/report/remove` 必须注册在 `/api/report/{task_id}` 之前。

    FastAPI 按 (路径, 方法) 匹配，POST 不会被 GET 的 `{task_id}` 吞掉；这里锁顺序是
    **零成本的保险** —— 将来若给这个功能补 GET 变体，顺序错了的症状是极具误导性的
    「任务不存在」（与 `/api/report/list` 那条硬约束同型）。
    """
    paths = [r.path for r in api_server.app.routes]
    assert paths.index("/api/report/remove") < paths.index("/api/report/{task_id}")
    assert paths.index("/api/report/list") < paths.index("/api/report/{task_id}")


def test_delete_uses_shared_resolver_and_never_builds_its_own_path():
    """删除侧**不得自建路径规则**：必须复用 view 的 `resolve_report_dir`。

    自建一份路径规则 = N-15 / N-18 「同一件事有两个定义处」，症状是「列表里点得开的
    却删不掉」——或者更糟的方向，「看不到的反而删得掉」。
    """
    body = _read("src/fundamental_agent/report_index.py").split(
        "def delete_reports(")[1].split("# ===== 扫描 =====")[0]
    assert "resolve_report_dir(user_id, rid" in body
    assert "joinpath(" not in body, "delete_reports 自建了路径 —— 这会产生第二份路径规则"
    assert "user_root(user_id, base)" in body


def test_prune_empty_parents_is_bounded_by_user_root():
    """向上清理空目录必须有边界：越界即停，绝不碰用户根以上的东西。"""
    body = _read("src/fundamental_agent/report_index.py").split(
        "def _prune_empty_parents(")[1].split("def delete_reports(")[0]
    assert "_is_within" in body
    assert "pr == root_r" in body


def test_report_index_holds_live_reference_to_default_base():
    """`user_root` 必须走模块级 `_DEFAULT_BASE`（夹具靠 monkeypatch 它隔离真实 output）。

    这条护栏防的是「有人把它改成从 storage 模块即时取」——那时夹具的隔离会静默失效，
    测试会开始**真删** `output/reports` 下的产物。
    """
    src = _read("src/fundamental_agent/report_index.py")
    assert "from fundamental_agent.storage import _DEFAULT_BASE" in src
    assert "_DEFAULT_BASE" in src.split("def user_root(")[1].split("def ")[0]


def test_server_guards_empty_user_id_with_403():
    """legacy / 共享视角（`user_id` 为空）必须 403 拒绝，而不是删到共享根。

    注意锚点用 `\\n@app.`（下一个路由装饰器）而不是 `@app.get("/api/report/{task_id}"`——
    后者的字面量也出现在本函数的 docstring 里，用它切会把函数体截在文档串中间。
    """
    body = _read("src/fundamental_agent/api/server.py").split(
        "def remove_reports(")[1].split("\n@app.")[0]
    assert "if not user_id" in body
    assert "403" in body


def test_frontend_proxy_reuses_report_prefix():
    """8000 侧**无需新增分支**：`/api/report/remove` 已被既有 `/api/report` 前缀覆盖。

    这正是 N-23（网关逐前缀硬编码）的正面样本：复用已有前缀 = 网关零改动。
    这里同时锁住「该分支必须带会话校验」，否则未登录就能删别人报告。
    """
    post = _read("frontend/server.py").split("def do_POST(self):")[1]
    assert 'parsed.path.startswith("/api/report")' in post
    seg = post.split('startswith("/api/report")')[1].split("if parsed.path.startswith")[0]
    assert "_session_ok()" in seg
    assert "_proxy_report" in seg


def test_reports_page_has_delete_ui():
    """报告库页的删除入口：按钮 / 确认弹层 / 取消出口 / 事件委托 / 跨模块事件。"""
    html = _read("frontend/reports.html")
    assert "/api/report/remove" in html and "API_REMOVE" in html
    assert "data-del=" in html, "行内删除按钮缺失"
    assert "delModal" in html and "data-del-cancel" in html, "确认弹层或取消出口缺失"
    assert "此操作会从磁盘永久删除报告文件" in html, "不可恢复的警告文案缺失"
    assert "tradeai:report-removed" in html
    assert "elList.addEventListener('click'" in html, "必须用事件委托（行会整体重建）"
    assert "it.deletable !== false" in html, "必须按服务端下发的 deletable 决定是否渲染按钮"


def test_task_center_listens_for_report_removed():
    js = _read("frontend/js/task_center.js")
    assert "tradeai:report-removed" in js


@pytest.mark.parametrize("rel", ["frontend/js/task_center.js"])
def test_js_syntax(rel):
    node = _node()
    if not node:
        pytest.skip("本机无 node，跳过 JS 语法检查")
    r = subprocess.run([node, "--check", str(ROOT / rel)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_reports_page_inline_js_syntax(tmp_path):
    """内联脚本是本次改动的主体，也过一遍语法检查（提取后交给 node --check）。"""
    node = _node()
    if not node:
        pytest.skip("本机无 node，跳过 JS 语法检查")
    html = _read("frontend/reports.html")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert blocks, "没提取到内联脚本"
    f = tmp_path / "inline.js"
    f.write_text("\n;\n".join(blocks), encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
