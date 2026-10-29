"""API 请求 / 响应数据契约（pydantic）。

前后端字段以此为准；FastAPI 自动据此生成 OpenAPI 文档。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ReportRequest(BaseModel):
    """创建报告生成的请求体。"""

    code: str = ""           # 6 位股票代码，如 600519
    name: str = ""           # 可选：公司名称（前端有名称无代码时传，后端解析为代码）
    use_llm: bool = True     # 是否调用 LLM 生成 AI 结论；额度不足/离线校验时可置 False
    report_type: str = "fundamental"  # 报告类型：fundamental(基本面,默认) / overview(概况) / industry(行业对比)；默认不影响既有报告
    bk: str = ""             # 可选：行业对比报告的对比基准板块代码（如 BK0890），仅 report_type=industry 时生效；为空按东财行业
    bk_name: str = ""        # 可选：基准板块名称（前端下拉框选中项文本），用于报告标题展示
    force: bool = False      # True=强制重新生成（跳过「当日已有 SUCCESS 产物直接复用」），供前端「重新生成报告」按钮使用


class TaskStatus(BaseModel):
    """任务状态轮询响应（同时是 F-04 报告任务中心的列表项）。"""

    task_id: str
    status: str                          # PENDING / RUNNING / SUCCESS / FAILED
    progress: int = 0                    # 0-100
    stage: str = ""                      # 当前阶段描述文本
    error: Optional[str] = None          # FAILED 时的原因（截断到 300 字）
    html_path: Optional[str] = None      # SUCCESS 时报告的本地路径（**磁盘绝对路径，
                                         # 仅供服务端内部使用，不要直接给浏览器做链接**）
    code: str = ""                       # 绑定的股票代码，用于去重复用
    bk: str = ""                         # 绑定的对比基准板块（仅 industry 报告），用于去重复用
    report_type: str = ""                # 报告类型（AUTH/CR-2026-09-11）：纳入去重键，
                                         # 否则同股不同类型报告会复用同一任务、状态互相覆盖
    user_id: Optional[int] = None        # 归属用户（F-06 D1）：同样纳入去重键，
                                         # 否则两个用户同日同标的会命中同一 RUNNING 任务

    # ===== F-04 报告任务中心（2026-09-26）=====
    # 任务中心要在一个列表里同时表达「跑中」与「已完成」，故需要时间戳与展示名。
    # 三者均带默认值 → 加字段不破坏既有响应（`POST /api/report`、`GET /api/report/{id}`）。
    name: str = ""                       # 标的中文名（公司名 / 板块名），进度列表展示用
    bk_name: str = ""                    # 行业对比的对比基准板块名（进行中就已知，来自前端下拉框）
    created_at: str = ""                 # ISO8601：创建时刻（排序 + 耗时展示）
    updated_at: str = ""                 # ISO8601：最近一次状态变更（前端据此算「已耗时」）
    report_id: str = ""                  # SUCCESS 后的**持久**报告标识（F-06 的 report_id）。
                                         # 这是 F-04 与 F-06 的接缝：任务中心必须用它将用户
                                         # 引向磁盘产物，**不得下发内存 task_id 的 /html 链接**
                                         # ——那样 8002 一重启链接即失效（F-06 交付时修掉的
                                         # 正是这个缺陷，不能在任务中心里重新引入）。


class ReportResponse(BaseModel):
    """创建任务后的立即响应。"""

    task_id: str
    status: str


# ===== F-04 报告任务中心：删除任务记录（2026-09-26）=====

class TaskRemoveRequest(BaseModel):
    """删除报告任务记录的请求体（`POST /api/tasks/remove`）。

    单个删除与大类清理共用这一种形状（传列表），避免再长出一个端点。
    `task_ids` 只收**自己**的任务，且服务端只处理终态（见 `TaskManager.remove()`）——
    前端不在请求里表达"我的"，归属一律取自会话。
    """

    task_ids: list[str] = []


# ===== 报告库：删除已落盘报告（2026-09-26）=====

class ReportRemoveRequest(BaseModel):
    """删除报告库中若干份**已落盘**报告的请求体（`POST /api/report/remove`）。

    与 `TaskRemoveRequest` 是**两件事**，不要合并：
      - 任务记录活在内存（`TaskManager._tasks`），删它只是清理列表，8002 重启即全没；
      - 报告活在磁盘（`report.html` + `meta.json` + `profile.json`），删它**不可恢复**。

    与「删任务」一样只收 id 列表、单个与批量共用一种形状；`report_id` 是**用户相对**
    标识（不含 user_id），归属一律取自会话 —— 前端无法、也不应该表达「这是谁的报告」。
    """

    report_ids: list[str] = []


# ===== 自选股（F-03） =====

class WatchAddRequest(BaseModel):
    """添加自选请求。code 为 6 位代码（服务端会清洗并二次校验）。"""

    code: str = ""
    name: str = ""            # 可选：名称快照，行情缺失时兜底显示


class WatchRemoveRequest(BaseModel):
    """移除自选请求。"""

    code: str = ""


class WatchOrderRequest(BaseModel):
    """自选重排请求（v1.5 预留）。codes 为期望顺序。"""

    codes: list[str] = []


class WatchItem(BaseModel):
    """自选条目（仅存储层字段，行情字段由 8000 侧拼装）。"""

    code: str
    name: str = ""
    note: str = ""
    created_at: str = ""


class WatchListResponse(BaseModel):
    """自选列表响应。"""

    ok: bool = True
    items: list[WatchItem] = []
    count: int = 0
    limit: int = 0            # 单用户上限，供前端提示
