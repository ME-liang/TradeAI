"""六层报告的数据契约与溯源元信息。

- SourceRef：单个数字的溯源三要素（来源 / 时点 / 一手或二手）。
- Ref：带溯源的单个字段，六层数据的最小单元；missing 表示「未获取」。
- 六层 dataclass：Identity / Business / Governance / Industry / Financials / Dynamics，
  组合 Ref 与集合，构成 CompanyProfile 总结构。均可 json.dumps 序列化。

设计约束（对应方案金融红线）：
- 数据缺失时 Ref.value=None 是合法状态，绝不允许上层用「看起来合理」的数字填空。
- confidence 区分一手（年报/交易所）与非一手（检索/新闻），渲染时按需标注。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class SourceRef:
    """数字溯源三要素。"""

    source: str = ""                      # "2025年报" / "深交所" / "AkShare" / "公开检索"
    as_of: str = ""                        # "2025-12-31" / "2026-08-28"
    confidence: Literal["一手", "非一手"] = "一手"


@dataclass
class Ref:
    """带溯源的单个字段。六层数据的最小单元。"""

    label: str = ""
    value: Any = None
    unit: str = ""
    source: str = ""                      # 冗余 SourceRef.source，便于扁平读取
    as_of: str = ""
    confidence: Literal["一手", "非一手"] = "一手"
    note: str = ""

    @property
    def missing(self) -> bool:
        return self.value is None

    @property
    def display(self) -> str:
        if self.value is None:
            return "未获取"
        return f"{self.value}{self.unit}"


@dataclass
class IdentityLayer:
    name: Ref = field(default_factory=Ref)
    industry_sw: Ref = field(default_factory=Ref)      # 申万一级行业
    controller: Ref = field(default_factory=Ref)       # 实控人（口径见 note）
    board: Ref = field(default_factory=Ref)            # 上市板块
    list_date: Ref = field(default_factory=Ref)        # 上市日期
    total_mv: Ref = field(default_factory=Ref)         # 总市值
    float_mv: Ref = field(default_factory=Ref)        # 流通市值
    employees: Ref = field(default_factory=Ref)       # 员工人数


@dataclass
class Segment:
    """业务分部：收入占比 × 毛利率 × 利润占比，用于计算利润贡献效率。"""

    name: str = ""
    revenue: float | None = None
    revenue_share: float | None = None     # 收入占比 %
    gross_margin: float | None = None      # 毛利率 %
    profit_share: float | None = None      # 利润占比 %
    source: str = ""
    as_of: str = ""


@dataclass
class BusinessLayer:
    segments: list[Segment] = field(default_factory=list)
    upstream: list[str] = field(default_factory=list)     # 上游合作原厂
    downstream: list[str] = field(default_factory=list)   # 下游客户
    growth_space: str = ""                                # 增长空间描述


@dataclass
class GovernanceLayer:
    top_holders: list[dict] = field(default_factory=list)   # 十大股东
    pledge_ratio: Ref = field(default_factory=Ref)          # 质押比例
    dividend_yield: Ref = field(default_factory=Ref)        # 股息率
    audit_opinion: Ref = field(default_factory=Ref)         # 审计意见
    repurchase: str = ""                                    # 回购注销


@dataclass
class Peer:
    name: str = ""
    code: str = ""
    revenue: float | None = None
    gross_margin: float | None = None
    roe: float | None = None
    mv: float | None = None


@dataclass
class IndustryLayer:
    scale: Ref = field(default_factory=Ref)          # 行业规模
    cagr: Ref = field(default_factory=Ref)           # 行业增速
    cr5: Ref = field(default_factory=Ref)            # 集中度
    peers: list[Peer] = field(default_factory=list)  # 可比公司


@dataclass
class FinancialsLayer:
    revenue_cagr: Ref = field(default_factory=Ref)
    profit_cagr: Ref = field(default_factory=Ref)
    gross_margin: Ref = field(default_factory=Ref)
    roe: Ref = field(default_factory=Ref)
    ocf_netprofit: Ref = field(default_factory=Ref)  # 经营现金流/净利润
    pe: Ref = field(default_factory=Ref)
    pe_percentile: Ref = field(default_factory=Ref)  # PE 历史分位


@dataclass
class DynamicsLayer:
    events: list[str] = field(default_factory=list)   # 近一年重大事项
    core_thesis: str = ""                            # 核心逻辑与分歧
    risk_points: list[str] = field(default_factory=list)


@dataclass
class CompanyProfile:
    """六层报告总结构，对应二期 JSON 落盘契约。"""

    code: str = ""
    name: str = ""
    period: str = ""
    identity: IdentityLayer = field(default_factory=IdentityLayer)
    business: BusinessLayer = field(default_factory=BusinessLayer)
    governance: GovernanceLayer = field(default_factory=GovernanceLayer)
    industry: IndustryLayer = field(default_factory=IndustryLayer)
    financials: FinancialsLayer = field(default_factory=FinancialsLayer)
    dynamics: DynamicsLayer = field(default_factory=DynamicsLayer)
