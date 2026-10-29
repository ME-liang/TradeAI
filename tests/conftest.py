# -*- coding: utf-8 -*-
"""pytest 全局配置：测试隔离 auth 数据库。

必要性：`auth/db.py` 在**导入时**解析 `DB_PATH`（db.py 顶部），所以必须在
任何 `fundamental_agent.auth.*` / `fundamental_agent.api.server` 被导入之前
设置 `AUTH_DB_PATH`；conftest.py 由 pytest 最先导入，正是为此。

这样测试永远不会碰到真实的 `data/auth.db`（其中含真实账号与会话）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

if not os.environ.get("AUTH_DB_PATH"):
    _tmp = Path(tempfile.gettempdir()) / "tradeai_pytest_auth.db"
    # 清掉上一轮遗留，保证每次测试从空库开始
    for _suffix in ("", "-wal", "-shm"):
        try:
            Path(str(_tmp) + _suffix).unlink()
        except OSError:
            pass
    os.environ["AUTH_DB_PATH"] = str(_tmp)
