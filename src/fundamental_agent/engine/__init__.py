"""分析引擎包。

每个模块继承 BaseEngine，接收标准化后的财报数据，返回 ModuleResult。
脚手架阶段各引擎返回空结果（标题已定），具体指标计算逻辑待填充。
"""

from .base import BaseEngine
from .financials import FinancialsEngine
from .profitability import ProfitabilityEngine
from .growth import GrowthEngine
from .solvency import SolvencyEngine
from .efficiency import EfficiencyEngine
from .benchmark import BenchmarkEngine

# 引擎注册表：编排层按顺序调用
ENGINE_REGISTRY = [
    FinancialsEngine,
    ProfitabilityEngine,
    GrowthEngine,
    SolvencyEngine,
    EfficiencyEngine,
    BenchmarkEngine,
]

__all__ = [
    "BaseEngine",
    "FinancialsEngine",
    "ProfitabilityEngine",
    "GrowthEngine",
    "SolvencyEngine",
    "EfficiencyEngine",
    "BenchmarkEngine",
    "ENGINE_REGISTRY",
]
