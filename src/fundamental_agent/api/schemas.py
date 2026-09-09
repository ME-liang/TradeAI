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
    """任务状态轮询响应。"""

    task_id: str
    status: str                          # PENDING / RUNNING / SUCCESS / FAILED
    progress: int = 0                    # 0-100
    stage: str = ""                      # 当前阶段描述文本
    error: Optional[str] = None          # FAILED 时的原因（截断到 300 字）
    html_path: Optional[str] = None      # SUCCESS 时报告的本地路径
    code: str = ""                       # 绑定的股票代码，用于去重复用
    bk: str = ""                         # 绑定的对比基准板块（仅 industry 报告），用于去重复用


class ReportResponse(BaseModel):
    """创建任务后的立即响应。"""

    task_id: str
    status: str
