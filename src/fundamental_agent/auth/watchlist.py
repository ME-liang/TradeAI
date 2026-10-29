#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自选股仓储（F-03）。

用户私有的关注标的集合，与 users / sessions 同库（默认 data/auth.db）。
设计要点（对应 `docs/F-03_自选股功能实现方案.md`）：

- **单一写入方**：本模块只由 FastAPI(8002) 侧调用。8000 静态服务不直接开库，
  避免 SQLite 多进程写竞争。
- **幂等**：借助唯一索引 `ux_watchlist_user_code` + `INSERT OR IGNORE`，
  重复添加不报错、不产生重复行；重复删除返回 removed=False。
- **name 为冗余快照**：行情源不可用时仍能显示名称，避免出现空白行。
  读取时若行情返回了更新的名称，应由展示层覆盖，本字段仅作兜底。
- **上限**：单用户 MAX_WATCHLIST 条，防滥用（对齐后续 F-28 的额度思路）。
- 零新增依赖，仅标准库 sqlite3。

代码格式校验与 `api/server.py::_code_ok` 保持同口径（6 位纯数字），
但在此独立实现，避免 auth 层反向依赖 api 层。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from fundamental_agent.auth.db import get_conn

# 单用户自选上限
MAX_WATCHLIST = 200

_CODE_RE = re.compile(r"^\d{6}$")


def _now() -> str:
    """UTC ISO8601（秒精度），与 db.py::_now 同口径，供 SQLite 文本比较。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_code(code: Optional[str]) -> str:
    """清洗代码：仅保留数字（与前端 `_SP.get('code').replace(/[^0-9]/g,'')` 同口径）。"""
    return re.sub(r"[^0-9]", "", (code or "").strip())


def is_valid_code(code: str) -> bool:
    """6 位纯数字。与 api/server.py::_code_ok 同口径。"""
    return bool(_CODE_RE.match(code or ""))


def _row_to_item(row) -> dict:
    return {
        "code": row["code"],
        "name": row["name"] or "",
        "note": row["note"] or "",
        "created_at": row["created_at"],
    }


def count(user_id: int) -> int:
    """当前用户自选数量。"""
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM watchlist WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]
    finally:
        conn.close()


def list_items(user_id: int) -> list[dict]:
    """列出当前用户自选（按手工排序 + 添加时间正序）。

    排序口径与索引 idx_watchlist_user(user_id, sort_order, created_at) 一致。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT code, name, note, created_at FROM watchlist "
            "WHERE user_id = ? ORDER BY sort_order ASC, created_at ASC, id ASC",
            (user_id,),
        ).fetchall()
        return [_row_to_item(r) for r in rows]
    finally:
        conn.close()


def has(user_id: int, code: str) -> bool:
    """是否已自选（供前端回显按钮状态）。"""
    c = normalize_code(code)
    if not is_valid_code(c):
        return False
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT 1 FROM watchlist WHERE user_id = ? AND code = ?", (user_id, c)
        ).fetchone() is not None
    finally:
        conn.close()


def add(user_id: int, code: str, name: str = "") -> dict:
    """添加自选。**幂等**。

    返回 {"added": bool, "count": int}：
      - 新增成功            → added=True
      - 已存在（重复添加）  → added=False，同时尽力刷新 name 快照，不报错

    异常：
      - code 非 6 位纯数字  → ValueError("股票代码格式不正确")
      - 已达上限            → ValueError("自选已达上限 N 只，请先移除部分标的")

    并发说明：唯一索引保证不会出现重复行；上限校验在并发下最多允许超出 1~2 条
    （两个请求同时通过计数检查），属可接受偏差，不做串行化以保持实现简单。
    """
    c = normalize_code(code)
    if not is_valid_code(c):
        raise ValueError("股票代码格式不正确")
    nm = (name or "").strip()[:32]
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO watchlist (user_id, code, name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, c, nm, _now()),
        )
        added = cur.rowcount > 0
        if not added:
            # 已存在：容忍重复提交。名称快照**仅在为空时填充**，不覆盖已有值——
            # 该字段只是行情缺失时的兜底，用可能不完整的新值覆盖旧值得不偿失；
            # 展示层本就会优先使用行情返回的最新名称。
            if nm:
                conn.execute(
                    "UPDATE watchlist SET name = ?, updated_at = ? "
                    "WHERE user_id = ? AND code = ? AND (name IS NULL OR name = '')",
                    (nm, _now(), user_id, c),
                )
            conn.commit()
            return {"added": False, "count": _count_locked(conn, user_id)}
        # 仅在真正新增时才校验上限：已存在的情况不应被上限拦截
        n = _count_locked(conn, user_id)
        if n > MAX_WATCHLIST:
            # 超出上限：回滚本次插入
            conn.execute("DELETE FROM watchlist WHERE user_id = ? AND code = ?",
                         (user_id, c))
            conn.commit()
            raise ValueError("自选已达上限 %d 只，请先移除部分标的" % MAX_WATCHLIST)
        conn.commit()
        return {"added": True, "count": n}
    finally:
        conn.close()


def _count_locked(conn, user_id: int) -> int:
    """在已持有连接的上下文内计数（避免嵌套开连接）。"""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM watchlist WHERE user_id = ?", (user_id,)
    ).fetchone()["c"]


def remove(user_id: int, code: str) -> dict:
    """移除自选。**幂等**。返回 {"removed": bool, "count": int}。

    不存在的自选返回 removed=False 而非报错，便于前端做乐观更新。
    """
    c = normalize_code(code)
    if not is_valid_code(c):
        raise ValueError("股票代码格式不正确")
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND code = ?", (user_id, c)
        )
        conn.commit()
        return {"removed": cur.rowcount > 0, "count": _count_locked(conn, user_id)}
    finally:
        conn.close()


def reorder(user_id: int, codes: list[str]) -> int:
    """按 codes 顺序重排自选（v1.5 预留）。仅影响该用户自己的行，返回更新条数。

    未出现在 codes 中的自选保持原 sort_order，排在指定顺序之后。
    """
    seq: list[str] = []
    for raw in codes or []:
        c = normalize_code(raw)
        if is_valid_code(c) and c not in seq:
            seq.append(c)
    if not seq:
        return 0
    conn = get_conn()
    try:
        n = 0
        now = _now()
        for idx, c in enumerate(seq):
            cur = conn.execute(
                "UPDATE watchlist SET sort_order = ?, updated_at = ? "
                "WHERE user_id = ? AND code = ?",
                (idx, now, user_id, c),
            )
            n += cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()
