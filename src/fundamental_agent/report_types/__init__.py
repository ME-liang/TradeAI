"""报告类型包：每种报告（基本面 / 概况）各自拥有模板与装配逻辑，互不复用。

- 基本面：沿用项目既有的 render() + template.html（不在本包内改动）。
- 概况：本包 overview.py + template_overview.html。

取数 / LLM 路由 / 校验 / 存储 等底层能力为共享，渲染与模板按类型隔离，
保证"公司概况"与"公司基本面"是两套独立功能，互不污染。
"""

from .overview import (
    build_overview_data,
    empty_overview_data,
    generate_overview_content,
    render_overview,
)

__all__ = [
    "build_overview_data",
    "empty_overview_data",
    "generate_overview_content",
    "render_overview",
]
