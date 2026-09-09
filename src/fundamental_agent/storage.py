"""报告落盘：JSON 与 HTML 分离存储。

目录结构：output/reports/{code}/{yyyymmdd}/{report.html, profile.json, meta.json}

- report.html：渲染产物（人类可读，可离线打印）
- profile.json：结构化六层数据契约（机器可读）；改样式/配色只凭它重渲染，
  不重复消耗 LLM 推理
- meta.json：任务元信息（code/name/period/生成时间/校验结果/数据源）

设计原则：落盘失败绝不中断主流程（调用方已 try/except）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Optional

# 项目根：src/fundamental_agent/storage.py -> parent*2 = fundamental_agent/ -> parent*3 = 仓库根
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_BASE = _PROJECT_ROOT / "output" / "reports"


def report_output_path(code: str, report_type: str = "", base: str | Path | None = None) -> Path:
    """返回某公司的当日报告 HTML 路径（目录自动创建）。

    report_type 为空（默认）→ output/reports/{code}/{date}/report.html（基本面报告原路径）。
    非空（如 "overview"）→ output/reports/{code}/{report_type}/{date}/report.html，
    保证不同报告类型物理隔离、互不复写。
    """
    root = Path(base) if base else _DEFAULT_BASE
    parts = [code]
    if report_type:
        parts.append(report_type)
    parts.append(date.today().strftime("%Y%m%d"))
    d = root.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d / "report.html"


def save_report(
    code: str,
    html_path: str | Path,
    *,
    profile: Any = None,
    meta: Any = None,
    base: str | Path | None = None,
    report_type: str = "",
) -> dict:
    """把 HTML + 结构化 JSON + 元信息写入 output/reports/{code}/[{type}/]{date}/。

    返回各文件实际路径；任何写失败向上抛出，由调用方决定降级。
    """
    root = Path(base) if base else _DEFAULT_BASE
    parts = [code]
    if report_type:
        parts.append(report_type)
    parts.append(date.today().strftime("%Y%m%d"))
    d = root.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)

    html_path = Path(html_path)
    target_html = d / "report.html"
    target_html.write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")

    paths = {"html": str(target_html)}
    if profile is not None:
        p = d / "profile.json"
        p.write_text(json.dumps(profile, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        paths["profile"] = str(p)
    if meta is not None:
        m = d / "meta.json"
        m.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        paths["meta"] = str(m)
    return paths
