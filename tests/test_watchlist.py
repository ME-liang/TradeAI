# -*- coding: utf-8 -*-
"""自选股（F-03）测试：仓储幂等性 / 上限 / 用户隔离 / 级联删除 / API 鉴权与流程。

全部离线、不依赖外网。通过 monkeypatch `auth.db.DB_PATH` 指向 tmp_path，
每个用例都从空库开始（get_conn 在**调用时**读取模块级 DB_PATH，故可被替换）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fundamental_agent.api import server as api_server
from fundamental_agent.auth import db as auth_db
from fundamental_agent.auth import watchlist as wl


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """把 auth 库指向临时文件并完成建表，返回 (DB_PATH, user_id)。"""
    db_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "DB_PATH", db_path)
    auth_db.init_db(seed_admin=False)
    user_id = _make_user("13900000001")
    return db_path, user_id


def _make_user(username: str) -> int:
    conn = auth_db.get_conn()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, status, created_at) "
            "VALUES (?, ?, 'active', ?)",
            (username, auth_db.hash_password("pw123456"), "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        return conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
    finally:
        conn.close()


# ===== 纯函数 =====

def test_normalize_code_strips_prefix_and_noise():
    assert wl.normalize_code("SH600519") == "600519"
    assert wl.normalize_code(" 600519 ") == "600519"
    assert wl.normalize_code("sz.000001") == "000001"
    assert wl.normalize_code(None) == ""


@pytest.mark.parametrize("code,ok", [
    ("600519", True), ("000001", True), ("300750", True),
    ("12", False), ("", False), ("6005190", False), ("abcdef", False),
])
def test_is_valid_code(code, ok):
    assert wl.is_valid_code(code) is ok


# ===== 仓储：幂等 =====

def test_add_is_idempotent(db):
    _, uid = db
    first = wl.add(uid, "600519", "贵州茅台")
    assert first == {"added": True, "count": 1}
    second = wl.add(uid, "600519", "贵州茅台")
    assert second == {"added": False, "count": 1}
    assert len(wl.list_items(uid)) == 1


def test_add_does_not_degrade_existing_name_snapshot(db):
    """已存在项的名称快照只在为空时填充，不被后到的劣质值覆盖。"""
    _, uid = db
    wl.add(uid, "600519", "贵州茅台")
    wl.add(uid, "600519", "x")
    assert wl.list_items(uid)[0]["name"] == "贵州茅台"


def test_add_fills_name_when_snapshot_empty(db):
    _, uid = db
    wl.add(uid, "600519", "")
    wl.add(uid, "600519", "贵州茅台")
    assert wl.list_items(uid)[0]["name"] == "贵州茅台"


def test_remove_is_idempotent(db):
    _, uid = db
    wl.add(uid, "600519", "贵州茅台")
    assert wl.remove(uid, "600519") == {"removed": True, "count": 0}
    assert wl.remove(uid, "600519") == {"removed": False, "count": 0}


def test_add_rejects_invalid_code(db):
    _, uid = db
    with pytest.raises(ValueError):
        wl.add(uid, "12", "x")
    with pytest.raises(ValueError):
        wl.remove(uid, "")
    assert wl.list_items(uid) == []


def test_has_reports_watched_state(db):
    _, uid = db
    assert wl.has(uid, "600519") is False
    wl.add(uid, "600519")
    assert wl.has(uid, "600519") is True
    assert wl.has(uid, "000001") is False
    assert wl.has(uid, "不是代码") is False


def test_list_keeps_insertion_order(db):
    _, uid = db
    for c in ("600519", "000001", "300750"):
        wl.add(uid, c)
    assert [i["code"] for i in wl.list_items(uid)] == ["600519", "000001", "300750"]


# ===== 上限 =====

def test_limit_enforced_and_insert_rolled_back(db, monkeypatch):
    _, uid = db
    monkeypatch.setattr(wl, "MAX_WATCHLIST", 2)
    wl.add(uid, "600519")
    wl.add(uid, "000001")
    with pytest.raises(ValueError) as ei:
        wl.add(uid, "300750")
    assert "上限" in str(ei.value)
    # 超出上限的那次插入必须被回滚，数量保持不变
    assert wl.count(uid) == 2
    assert [i["code"] for i in wl.list_items(uid)] == ["600519", "000001"]


def test_limit_does_not_block_re_adding_existing(db, monkeypatch):
    """已达上限时，重复添加「已存在」的标的属于幂等操作，不应被上限拦截。"""
    _, uid = db
    monkeypatch.setattr(wl, "MAX_WATCHLIST", 2)
    wl.add(uid, "600519")
    wl.add(uid, "000001")
    assert wl.add(uid, "600519")["added"] is False


# ===== 隔离与级联 =====

def test_users_are_isolated(db):
    _, uid = db
    other = _make_user("13900000002")
    wl.add(uid, "600519")
    assert wl.list_items(other) == []
    assert wl.has(other, "600519") is False
    assert wl.remove(other, "600519")["removed"] is False
    assert wl.count(uid) == 1     # 他人删除不影响本用户


def test_delete_user_cascades(db):
    db_path, uid = db
    wl.add(uid, "600519")
    conn = auth_db.get_conn()
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    conn.commit()
    left = conn.execute("SELECT COUNT(*) AS c FROM watchlist").fetchone()["c"]
    conn.close()
    assert left == 0


def test_reorder_applies_and_ignores_foreign_codes(db):
    _, uid = db
    other = _make_user("13900000003")
    wl.add(uid, "600519")
    wl.add(uid, "000001")
    wl.add(other, "300750")
    n = wl.reorder(uid, ["000001", "600519", "300750"])   # 最后一个是他人代码
    assert n == 2
    assert [i["code"] for i in wl.list_items(uid)] == ["000001", "600519"]
    assert [i["code"] for i in wl.list_items(other)] == ["300750"]


# ===== API 层（含 _auth_gate 中间件与 _current_user 取值） =====

@pytest.fixture()
def client(db):
    return TestClient(api_server.app)


def _login(client, username="13900000001", password="pw123456"):
    r = client.post("/api/auth/login", json={"account": username, "password": password})
    assert r.status_code == 200, r.text
    return r


def test_api_requires_login(client):
    assert client.get("/api/watchlist").status_code == 401
    assert client.post("/api/watchlist/add", json={"code": "600519"}).status_code == 401
    assert client.post("/api/watchlist/remove", json={"code": "600519"}).status_code == 401
    assert client.get("/api/watchlist/check?code=600519").status_code == 401


def test_api_add_list_check_remove_flow(client):
    _login(client)

    r = client.post("/api/watchlist/add", json={"code": "600519", "name": "贵州茅台"})
    assert r.status_code == 200 and r.json() == {"ok": True, "added": True, "count": 1,
                                                 "limit": wl.MAX_WATCHLIST}

    r = client.post("/api/watchlist/add", json={"code": "600519", "name": "贵州茅台"})
    assert r.json()["added"] is False and r.json()["count"] == 1

    client.post("/api/watchlist/add", json={"code": "000001", "name": "平安银行"})
    r = client.get("/api/watchlist")
    body = r.json()
    assert body["count"] == 2 and body["limit"] == wl.MAX_WATCHLIST
    assert [i["code"] for i in body["items"]] == ["600519", "000001"]

    assert client.get("/api/watchlist/check?code=600519").json()["watched"] is True
    assert client.get("/api/watchlist/check?code=300750").json()["watched"] is False

    r = client.post("/api/watchlist/remove", json={"code": "600519"})
    assert r.json() == {"ok": True, "removed": True, "count": 1, "limit": wl.MAX_WATCHLIST}
    assert client.post("/api/watchlist/remove", json={"code": "600519"}).json()["removed"] is False


def test_api_rejects_invalid_code_with_400(client):
    _login(client)
    for bad in ("", "12", "abcd", "6005190"):
        r = client.post("/api/watchlist/add", json={"code": bad})
        assert r.status_code == 400, bad
        assert "detail" in r.json()


def test_api_limit_returns_400(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(wl, "MAX_WATCHLIST", 1)
    assert client.post("/api/watchlist/add", json={"code": "600519"}).status_code == 200
    r = client.post("/api/watchlist/add", json={"code": "000001"})
    assert r.status_code == 400
    assert client.get("/api/watchlist").json()["count"] == 1


def test_api_order_endpoint(client):
    _login(client)
    client.post("/api/watchlist/add", json={"code": "600519"})
    client.post("/api/watchlist/add", json={"code": "000001"})
    r = client.post("/api/watchlist/order", json={"codes": ["000001", "600519"]})
    assert r.json() == {"ok": True, "updated": 2}
    assert [i["code"] for i in client.get("/api/watchlist").json()["items"]] == ["000001", "600519"]


def test_api_watchlist_is_user_scoped(client, db):
    _login(client)
    client.post("/api/watchlist/add", json={"code": "600519"})
    _make_user("13900000009")
    client2 = TestClient(api_server.app)
    _login(client2, "13900000009")
    assert client2.get("/api/watchlist").json()["count"] == 0
    assert client2.get("/api/watchlist/check?code=600519").json()["watched"] is False
