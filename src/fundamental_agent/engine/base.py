"""引擎统一基类。

约定：
- 构造时注入 DataClient（依赖倒置，便于替换数据源）。
- run() 接收 code 与三表字典，返回 ModuleResult。
- 脚手架阶段默认返回空 ModuleResult，子类填充计算逻辑。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import ModuleResult

if TYPE_CHECKING:  # 避免循环导入，仅作类型提示
    from ..data.client import DataClient


class BaseEngine:
    module: str = ""
    title: str = ""

    # 占位标记（性能优化·措施5）：脚手架阶段的占位引擎置 True 后，编排层直接跳过，
    # 不再空转计入耗时。审计结论（2026-09-06）：六个注册引擎（financials/profitability/
    # growth/solvency/efficiency/benchmark）均已实装视图/卡片/metrics 计算，无占位实现，
    # 故当前无任何引擎被标记——该开关是为后续增量开发阶段保留的防回归机制。
    placeholder: bool = False

    def __init__(self, client: "DataClient"):
        self.client = client

    def run(self, code: str, statements: dict) -> ModuleResult:
        """执行分析。子类应覆写本方法填充 metrics / charts / conclusion。"""
        return ModuleResult(module=self.module, title=self.title)
