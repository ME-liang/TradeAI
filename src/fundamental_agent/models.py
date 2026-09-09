"""共享数据结构。

定义贯穿整个 pipeline 的数据契约：
- Metric：单个指标（名称 / 数值 / 单位 / 是否适用 / 行业分位 / 备注）
- ModuleResult：单个分析模块的输出（指标集合 + 图表 + 结论）
- AnalysisResult：一次完整分析的总结果（公司 + 各模块）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .schema.report_schema import SourceRef


@dataclass
class Metric:
    key: str
    label: str
    value: Any = None
    unit: str = ""
    applicable: bool = True            # 当分母为 0 / 数据缺失时置 False
    industry_percentile: Optional[float] = None  # 0-100，行业分位
    note: str = ""
    source_ref: Optional[SourceRef] = None  # 溯源（来源 / 时点 / 一手或二手），缺省无


@dataclass
class ModuleResult:
    module: str
    title: str
    metrics: list[Metric] = field(default_factory=list)
    charts: list[str] = field(default_factory=list)  # base64 编码的 svg/png
    conclusion: str = ""
    extra: dict = field(default_factory=dict)  # 渲染层直接消费的结构化展示数据


@dataclass
class AnalysisResult:
    name: str = ""
    code: str = ""
    period: str = ""
    modules: list[ModuleResult] = field(default_factory=list)
    llm_summary: str = ""            # AI 生成的综合结论
    llm_insights: dict = field(default_factory=dict)  # AI 润色的各模块解读 prof/growth/solv/eff/bench/fin
    generated_at: str = ""
    profile: dict = field(default_factory=dict)  # 公司画像（行业 / 总市值等）
