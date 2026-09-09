"""数据层抽象契约。

所有数据源（Tushare / AKShare / PDF 解析）都必须实现 DataClient 接口，
返回统一格式的 pandas.DataFrame。计算引擎只依赖本接口，与具体数据源解耦。

约定（脚手架阶段仅定义，待实现）：
- 利润表 / 资产负债表 / 现金流量表：行索引为科目名，列为报告期。
- 行业可比公司：列为可比公司，行为关键指标。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class DataClient(ABC):
    @abstractmethod
    def get_income_statement(self, code: str) -> pd.DataFrame:
        """利润表。行=科目，列=报告期。"""
        raise NotImplementedError

    @abstractmethod
    def get_balance_sheet(self, code: str) -> pd.DataFrame:
        """资产负债表。行=科目，列=报告期。"""
        raise NotImplementedError

    @abstractmethod
    def get_cash_flow(self, code: str) -> pd.DataFrame:
        """现金流量表。行=科目，列=报告期。"""
        raise NotImplementedError

    @abstractmethod
    def get_industry_peers(self, code: str) -> pd.DataFrame:
        """行业可比公司关键指标。列=公司，行=指标。"""
        raise NotImplementedError

    # ---- 以下为可选方法（非抽象），真实数据源可按需覆写；占位/未实现时返回空值 ----

    def get_financial_indicators(self, code: str) -> pd.DataFrame:
        """财务分析指标（按报告期的一揽子比率）。行=报告期，列为指标。"""
        return pd.DataFrame()

    def get_company_profile(self, code: str) -> dict:
        """公司画像：名称 / 行业 / 总市值 等。"""
        return {}

    def get_share_capital(self, code: str) -> int:
        """当前总股本（股）；未实现返回 0（上层退回质押当时口径）。"""
        return 0

    # ---- 「公司概况」六层框架新增的可选取数口（一期留空，二期逐个实装）----
    # 约定：全部返回空值而非抛异常；数据源未覆盖时由上层标记「未获取」。
    # 返回类型约定：
    #   - dict：单值字段集合（身份信息等）
    #   - pd.DataFrame：表格型数据（股东、分红、主营构成等）

    def get_identity(self, code: str) -> dict:
        """01 身份识别：上市板块 / 上市时间 / 实控人 / 员工数 / 股东户数 等。

        实控人字段口径说明：数据源通常只提供股东名单而无实控人标注，
        一期允许用「第一大股东」近似，但必须在展示侧显式标注口径，
        避免与工商登记的实控人混淆。
        """
        return {}

    def get_main_business(self, code: str) -> pd.DataFrame:
        """02 业务画像：主营构成。

        期望列（按 AkShare `stock_zygc_em` 口径）：
        报告期 / 分类类型（按产品、按地区）/ 主营构成 / 主营收入 /
        收入比例 / 主营成本 / 成本比例 / 主营利润 / 利润比例 / 毛利率
        """
        return pd.DataFrame()

    def get_holders(self, code: str) -> pd.DataFrame:
        """03 股权治理：股东结构。

        期望列：报告期 / 名次 / 股东名称 / 持股数 / 占总股本持股比例 / 增减 / 变动比率
        """
        return pd.DataFrame()

    def get_dividend(self, code: str) -> pd.DataFrame:
        """03 股权治理：分红送配。

        期望列：报告期 / 现金分红比例 / 股息率 / 方案进度 / 股权登记日 / 除权除息日
        """
        return pd.DataFrame()

    def get_pledge(self, code: str) -> pd.DataFrame:
        """03 股权治理：股权质押。期望列：股票代码 / 质押比例 / 质押股数 等。"""
        return pd.DataFrame()

    def get_news(self, code: str) -> pd.DataFrame:
        """06 动态风险：个股新闻资讯。期望列：发布时间 / 新闻标题 / 新闻内容 / 来源。"""
        return pd.DataFrame()
