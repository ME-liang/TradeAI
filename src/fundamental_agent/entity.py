"""实体解析：公司名称 / 股票代码 → 标准实体（TODO）。

脚手架阶段仅做透传（name/code 原样返回）。
后续实现：模糊匹配、交易所前缀补全、防重名、别名表。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Entity:
    code: str
    name: str


def resolve(name: str = "", code: str = "") -> Entity:
    """将用户输入解析为标准实体。TODO: 接入名称/代码映射与模糊匹配。"""
    return Entity(code=code, name=name or code)
