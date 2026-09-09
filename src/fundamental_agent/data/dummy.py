"""空数据客户端。

用于在缺少数据源 token 时也能跑通整条 pipeline（生成带空结果的 HTML 报告），
作为脚手架与冒烟测试的最小可用数据来源。后续接入真实数据源后无需改动引擎层。
"""

from __future__ import annotations

import pandas as pd

from .client import DataClient


class DummyDataClient(DataClient):
    def get_income_statement(self, code: str) -> pd.DataFrame:
        return pd.DataFrame()

    def get_balance_sheet(self, code: str) -> pd.DataFrame:
        return pd.DataFrame()

    def get_cash_flow(self, code: str) -> pd.DataFrame:
        return pd.DataFrame()

    def get_industry_peers(self, code: str) -> pd.DataFrame:
        return pd.DataFrame()
