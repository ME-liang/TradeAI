#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 四表与连接封装（方案阶段 1 第 1 行 + F-03 自选股）。

对应方案: `src/fundamental_agent/auth/db.py` —— users / sessions / sms_codes /
watchlist 四表 + 连接封装。零新增依赖, 仅用标准库 sqlite3。

表设计（与方案阶段 1 一致）:
  users      username 唯一; password_hash 为 scrypt 哈希(自带盐); phone/phone_bound
             记录手机号绑定状态(首次登录强制绑定, 属阶段 1 后续)
  sessions   token 为主键(透明随机会话令牌); 外键 user_id 级联删除; expires_at 过期
  sms_codes  验证码流水: phone + purpose + 有效期 + used 标记, 支撑频控与校验
  watchlist  自选股(F-03): 用户私有的关注标的集合; 外键 user_id 级联删除;
             唯一约束 (user_id, code) 保证幂等; name 为冗余名称快照(行情缺失时兜底显示)

建表与"迁移"方式:
    get_conn() 每次连接都执行 executescript(SCHEMA_SQL), 而所有 DDL 均为
    CREATE TABLE/INDEX IF NOT EXISTS —— 因此**新增表无需迁移脚本**,
    老库在任一次连接后自动补齐结构, 既有数据不受影响。

建表脚本用法:
    python -m fundamental_agent.auth.db          # 建表 + 预置管理员 admin(若 users 表为空)
    # 可用环境变量 AUTH_DB_PATH 指定库文件, 默认 <项目根>/data/auth.db
    # 可用环境变量 AUTH_ADMIN_PASSWORD 覆盖预置管理员口令, 默认按交付约定为 1289234985
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 库文件默认落在项目 data/ 目录(与行情缓存等本地产物同层, 已在 .gitignore 体系内)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = Path(os.environ.get("AUTH_DB_PATH", str(_PROJECT_ROOT / "data" / "auth.db")))

# 预置管理员(阶段 1 交付约定): 登录名 admin
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = os.environ.get("AUTH_ADMIN_PASSWORD", "1289234985")

# 会话有效期(天): sessions.expires_at 依据此值计算
SESSION_TTL_DAYS = 7
# 「记住我」会话有效期(天): 勾选后服务端会话与 Cookie Max-Age 均按此值延长
REMEMBER_TTL_DAYS = 30

# scrypt 参数(标准库 hashlib.scrypt, N/r/p 写入哈希串便于后续调参兼容旧哈希)
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    phone         TEXT,
    phone_bound   INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'active',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user  ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expire ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS sms_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phone      TEXT    NOT NULL,
    code       TEXT    NOT NULL,
    purpose    TEXT    NOT NULL DEFAULT 'bind',
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sms_phone_time ON sms_codes(phone, created_at);

-- 手机号唯一: 同一手机号不允许注册多个账号(空手机号不约束)。
-- 服务层 register_user 已先查重并返回友好提示, 此索引为数据库层兜底,
-- 防并发注册/绕过服务层写入造成重复。
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_phone
    ON users(phone) WHERE phone IS NOT NULL AND phone != '';

CREATE TABLE IF NOT EXISTS watchlist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code       TEXT    NOT NULL,
    name       TEXT    NOT NULL DEFAULT '',
    note       TEXT    NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL,
    updated_at TEXT
);
-- 幂等的根本保证: 同一用户同一代码只允许一行。并发双击/重复提交由数据库兜底,
-- 服务层用 INSERT OR IGNORE 依赖此约束把重复添加转成「无声成功」。
-- 注: ON DELETE CASCADE 生效前提是 PRAGMA foreign_keys = ON, 见 get_conn()。
CREATE UNIQUE INDEX IF NOT EXISTS ux_watchlist_user_code ON watchlist(user_id, code);
-- 列表查询索引: 按用户取, 按手工排序 + 添加时间
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id, sort_order, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    """获取库连接: 自动建目录、开外键与 WAL, 首次连接时确保表结构存在。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


# ===== 密码哈希(scrypt, 格式 scrypt$N$r$p$salt_hex$hash_hex) =====

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
                                      salt.hex(), digest.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=32)
        return secrets.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def _seed_admin_if_empty(conn: sqlite3.Connection) -> bool:
    """users 表为空时预置管理员账号(幂等, 已有用户则不动作)。"""
    row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    if row["c"] > 0:
        return False
    conn.execute(
        "INSERT INTO users (username, password_hash, status, created_at) VALUES (?, ?, 'active', ?)",
        (ADMIN_USERNAME, hash_password(ADMIN_PASSWORD), _now()),
    )
    conn.commit()
    return True


def init_db(seed_admin: bool = True) -> bool:
    """建表(幂等)并按需预置管理员; 返回是否执行了预置。"""
    conn = get_conn()
    try:
        return _seed_admin_if_empty(conn) if seed_admin else False
    finally:
        conn.close()


if __name__ == "__main__":
    seeded = init_db(seed_admin=True)
    print("数据库: %s" % DB_PATH)
    print("四表就绪: users / sessions / sms_codes / watchlist")
    if seeded:
        print("已预置管理员: %s" % ADMIN_USERNAME)
    else:
        print("users 表非空, 跳过管理员预置")
    conn = get_conn()
    for row in conn.execute("SELECT id, username, phone_bound, status, created_at FROM users"):
        print("  user: id=%s username=%s phone_bound=%s status=%s created_at=%s"
              % (row["id"], row["username"], row["phone_bound"], row["status"], row["created_at"]))
        wl = conn.execute("SELECT COUNT(*) AS c FROM watchlist WHERE user_id = ?",
                          (row["id"],)).fetchone()["c"]
        print("        自选 %d 只" % wl)
    conn.close()
    conn.close()
