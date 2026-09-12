#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录验证与会话管理（方案阶段 1 第 3 行: 注册/登录/登出 + 会话管理中的登录部分）。

对应方案: `src/fundamental_agent/auth/service.py`。本文件实现:
  - login(account, password)  登录验证(账号=用户名或手机号), 成功创建会话
  - get_user_by_session       会话校验(过期即失效), 供 /api/auth/me 与后续登录态中间层复用
  - logout                    注销会话
  - gen_captcha / verify_captcha  图形验证码(注册用, 内存态, 5 分钟有效一次性)
  - register_user             注册: 手机号即用户名, 密码 scrypt 哈希入库
短信绑定 / 频控属阶段 1 后续步骤(需 sms.py), 表结构已预留, 暂不实现。

Cookie 口径(与方案一致): HttpOnly + SameSite=Lax; 上 HTTPS 后由部署层补 Secure。
"""
from __future__ import annotations

import re
import secrets
import sqlite3
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fundamental_agent.auth.db import (
    REMEMBER_TTL_DAYS,
    SESSION_TTL_DAYS,
    get_conn,
    hash_password,
)

# 会话 Cookie 名(前端与部署层共用)
SESSION_COOKIE = "ta_session"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def login(account: str, password: str, remember: bool = False) -> tuple[Optional[dict], Optional[str]]:
    """登录验证: 成功返回 (用户信息dict, 会话token); 失败返回 (None, None)。

    account 支持用户名或已绑定手机号; 密码用 scrypt 常数时间比较校验。
    remember=True 时会话有效期延长至 REMEMBER_TTL_DAYS 天(「记住我」),
    Cookie 端是否持久化由 api 层决定(勾选→带 Max-Age, 否则→浏览器会话 Cookie)。
    """
    if not account or not password:
        return None, None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? OR (phone IS NOT NULL AND phone = ?)",
            (account, account),
        ).fetchone()
        if row is None or row["status"] != "active":
            return None, None
        if not _verify(row["password_hash"], password):
            return None, None
        token = _create_session(conn, row["id"], remember=remember)
        return _user_dict(row), token
    finally:
        conn.close()


# 供 db.verify_password 转发(避免循环导入: service -> db 单向)
def _verify(stored: str, password: str) -> bool:
    from fundamental_agent.auth.db import verify_password
    return verify_password(password, stored)


def _create_session(conn: sqlite3.Connection, user_id: int, remember: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    days = REMEMBER_TTL_DAYS if remember else SESSION_TTL_DAYS
    now, exp = _now(), _now() + timedelta(days=days)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, _fmt(now), _fmt(exp)),
    )
    # 顺手清理过期会话, 防止 sessions 无限增长
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_fmt(now),))
    conn.commit()
    return token


def get_user_by_session(token: Optional[str]) -> Optional[dict]:
    """按会话 token 取当前用户; token 无效/过期返回 None。"""
    if not token:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at >= ?",
            (token, _fmt(_now())),
        ).fetchone()
        return _user_dict(row) if row else None
    finally:
        conn.close()


def logout(token: Optional[str]) -> bool:
    """注销会话: 删除 sessions 行。返回是否确有会话被删。"""
    if not token:
        return False
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _user_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "phone": row["phone"],
        "phone_bound": bool(row["phone_bound"]),
        "status": row["status"],
    }


# ===== 注册 + 图形验证码 =====

_PHONE_RE = re.compile(r"^1\d{10}$")

# 图形验证码: 去掉易混淆字符(0/O, 1/I/L, 8/B 等)
_CAPTCHA_ALPHABET = "23456789ACDEFGHJKMNPQRTUVWXYZ"
CAPTCHA_TTL_SECONDS = 300
_CAPTCHA_LENGTH = 4

# 内存态验证码仓: {captcha_id: {"code": str, "expires": float}}
# 单进程 uvicorn 足够; 多进程部署时改存 sessions 表或 Redis(阶段 2)
_captcha_store: dict = {}


def gen_captcha() -> tuple[str, str]:
    """生成图形验证码: 返回 (captcha_id, svg)。code 一次性使用, 5 分钟过期。"""
    now = time.time()
    # 顺手清理过期项, 防止内存无限增长
    for k in [k for k, v in _captcha_store.items() if v["expires"] < now]:
        _captcha_store.pop(k, None)

    code = "".join(secrets.choice(_CAPTCHA_ALPHABET) for _ in range(_CAPTCHA_LENGTH))
    captcha_id = secrets.token_urlsafe(16)

    # 纯标准库 SVG 绘制: 逐字符随机旋转/位移/配色 + 噪点噪线
    chars, noise = [], []
    x = 12
    palette = ("#0071E3", "#005BB8", "#1D1D1F", "#6E6E73")
    for ch in code:
        y = 31 + secrets.randbelow(9) - 4
        rot = secrets.randbelow(37) - 18
        color = palette[secrets.randbelow(len(palette))]
        chars.append(
            '<text x="%d" y="%d" font-size="27" font-family="Courier New,monospace" '
            'font-weight="bold" fill="%s" transform="rotate(%d %d %d)">%s</text>'
            % (x, y, color, rot, x + 10, y, ch)
        )
        x += 25
    for _ in range(3):
        noise.append(
            '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#D8D8DE" stroke-width="1"/>'
            % (secrets.randbelow(125), secrets.randbelow(46), secrets.randbelow(125), secrets.randbelow(46))
        )
    for _ in range(20):
        noise.append(
            '<circle cx="%d" cy="%d" r="1" fill="#E8E8ED"/>'
            % (secrets.randbelow(125), secrets.randbelow(46))
        )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="125" height="46" viewBox="0 0 125 46" '
        'role="img" aria-label="图形验证码">%s%s</svg>' % ("".join(noise), "".join(chars))
    )
    _captcha_store[captcha_id] = {"code": code, "expires": now + CAPTCHA_TTL_SECONDS}
    return captcha_id, svg


def verify_captcha(captcha_id: Optional[str], text: Optional[str]) -> bool:
    """校验图形验证码: 一次性(取出即销毁), 过期/不存在/不匹配均失败。忽略大小写。"""
    if not captcha_id or captcha_id not in _captcha_store:
        return False
    entry = _captcha_store.pop(captcha_id)
    if time.time() > entry["expires"]:
        return False
    return (text or "").strip().upper() == entry["code"]


def register_user(phone: str, password: str) -> dict:
    """注册: 手机号即用户名且视为已绑定。字段非法/手机号重复抛 ValueError。"""
    if not _PHONE_RE.fullmatch(phone or ""):
        raise ValueError("手机号格式不正确")
    conn = get_conn()
    try:
        dup = conn.execute(
            "SELECT id FROM users WHERE username = ? OR phone = ?", (phone, phone)
        ).fetchone()
        if dup is not None:
            raise ValueError("该手机号已注册，请直接登录")
        conn.execute(
            "INSERT INTO users (username, password_hash, phone, phone_bound, status, created_at) "
            "VALUES (?, ?, ?, 1, 'active', ?)",
            (phone, hash_password(password), phone, _fmt(_now())),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (phone,)).fetchone()
        return _user_dict(row)
    finally:
        conn.close()


def reset_password(phone: str, password: str) -> Optional[dict]:
    """找回密码: 按手机号查账号并重置密码。

    - 账号不存在 → 返回 None(由 api 层转「账号不存在」提示)
    - 成功 → 更新密码哈希并作废该用户全部旧会话(改密后需重新登录), 返回用户信息
    """
    if not _PHONE_RE.fullmatch(phone or ""):
        raise ValueError("手机号格式不正确")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE phone = ? OR username = ?", (phone, phone)
        ).fetchone()
        if row is None or row["status"] != "active":
            return None
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (hash_password(password), _fmt(_now()), row["id"]),
        )
        # 改密后旧会话全部失效: 防止他人持旧会话继续操作
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        conn.commit()
        fresh = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
        return _user_dict(fresh)
    finally:
        conn.close()
