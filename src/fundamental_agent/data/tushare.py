"""Tushare Pro 数据源实现（TODO）。

脚手架阶段仅继承 DataClient 并抛出 NotImplementedError，
后续填入 Tushare Pro 的接口调用与字段映射即可。
"""

from __future__ import annotations

import pandas as pd

from .client import DataClient


class TushareClient(DataClient):
    def __init__(self, token: str = ""):
        self.token = token
        # TODO: 初始化 pro 接口（ts.pro_api(token)）

    def get_income_statement(self, code: str) -> pd.DataFrame:
        # TODO: 调用 pro.income() 并整理为 行=科目 / 列=报告期
        raise NotImplementedError("TODO: 接入 Tushare Pro 利润表接口")

    def get_balance_sheet(self, code: str) -> pd.DataFrame:
        # TODO: 调用 pro.balancesheet()
        raise NotImplementedError("TODO: 接入 Tushare Pro 资产负债表接口")

    def get_cash_flow(self, code: str) -> pd.DataFrame:
        # TODO: 调用 pro.cashflow()
        raise NotImplementedError("TODO: 接入 Tushare Pro 现金流量表接口")

    def get_industry_peers(self, code: str) -> pd.DataFrame:
        # TODO: 依据行业映射筛选可比公司并拉取关键指标
        return pd.DataFrame()
