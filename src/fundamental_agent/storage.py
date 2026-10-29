"""报告落盘：JSON 与 HTML 分离存储。

目录结构：output/reports/[u{user_id}/]{code}/[{report_type}/]{yyyymmdd}/[{report.html, profile.json, meta.json}]

- report.html：渲染产物（人类可读，可离线打印）
- profile.json：结构化六层数据契约（机器可读）；改样式/配色只凭它重渲染，
  不重复消耗 LLM 推理
- meta.json：任务元信息（code/name/report_type/生成时间/校验结果/数据源/归属用户）

两处与早期注释不同、但已按实际代码定稿的约定（见 F-06 方案 §4）：

1. **`report_type` 段"可缺省"，但实际没有调用方缺省它** —— 参数默认值是空串（旧路径
   `{code}/{date}/`，2 段），而**现有两条调用方都显式传 `report_type="fundamental"`**
   （`api/server.py::_worker` 透传 `schemas.ReportRequest.report_type`，其默认值即
   `"fundamental"`；CLI `main.py --type` 亦然，且 CLI 走 `--out` 不落报告库）。
   因此**磁盘上的基本面报告恒为 3 段** `{code}/fundamental/{date}/`。
   读取侧（`report_index.py`）为此必须**同时接受 2 段与 3 段** —— 只认 2 段会让
   报告库里所有基本面报告点开即 404（N-21，2026-09-26 修复）。
   教训：别照着**签名默认值**推断落盘形态，看调用方实际传了什么。
2. **`u{user_id}` 段仅在 user_id 非空时插入** —— 用户维度是 F-06 新增的隔离键。
   为空时（CLI、迁移脚本、既有测试的路径构造）行为与改造前完全一致，保持向后兼容。

`date_suffix` 用于把「同一天内、同一 report_type 下还有一个实质区分维度」的场景
并入日期段（如行业对比报告的对比基准板块 → `20260922__BK0890`）。这样段数不变，
读取侧无需改动，而不同基准不再互相覆盖（F-06 §13.2）。

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


def _date_segment(date_suffix: str = "") -> str:
    """当日日期目录名；带后缀时为 `{yyyymmdd}__{suffix}`（段数不变）。"""
    d = date.today().strftime("%Y%m%d")
    s = (date_suffix or "").strip()
    return "%s__%s" % (d, s) if s else d


def _target_dir(
    code: str,
    report_type: str = "",
    base: str | Path | None = None,
    user_id: int | None = None,
    date_suffix: str = "",
) -> Path:
    """拼装并创建报告目录。

    **本函数是落盘路径的唯一拼装点**：`report_output_path` 与 `save_report`
    都必须经由它，否则会让 HTML 与 meta/profile 分居两个目录
    （`save_report` 会重算目录，不接收 html_path 的所在目录）。
    """
    root = Path(base) if base else _DEFAULT_BASE
    parts: list[str] = []
    if user_id:  # 0 / None 均不插用户段（保持旧路径与既有测试的路径构造不变）
        parts.append("u%d" % int(user_id))
    parts.append(code)
    if report_type:
        parts.append(report_type)
    parts.append(_date_segment(date_suffix))
    d = root.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def industry_date_suffix(report_type: str, bk: str = "") -> str:
    """计算日期段后缀：仅行业对比报告按「对比基准板块」再分一层。

    行业对比报告的路径原本是 `{code}/industry/{date}/`，**不含 bk**。而 bk 是它的
    实质维度（同一只票可以对比不同板块），于是同日切换基准会互相覆盖 ——
    与「键漏一个维度」的 2026-09-11 事故同型（F-06 §13.2，决策 D8）。
    把 bk 并入日期段 `{date}__{BK0890}` 后段数不变，读取侧无需改动。

    其余报告类型返回空串（路径与改造前完全一致）。
    """
    if report_type != "industry":
        return ""
    return (bk or "").strip()


def report_output_path(
    code: str,
    report_type: str = "",
    base: str | Path | None = None,
    user_id: int | None = None,
    date_suffix: str = "",
) -> Path:
    """返回某标的的当日报告 HTML 路径（目录自动创建）。

    - `report_type` 为空（默认）→ `output/reports/{code}/{date}/report.html`（基本面报告原路径）。
    - 非空（如 "overview"）→ `output/reports/{code}/{report_type}/{date}/report.html`，
      保证不同报告类型物理隔离、互不复写。
    - `user_id` 非空 → 路径前缀 `u{user_id}/`，保证不同用户同日同标的互不复写
      （只写 meta 字段做不到隔离：路径重合时后写会覆盖先写）。
    - `date_suffix` 非空 → 日期段变 `{date}__{suffix}`（如行业对比报告按对比基准板块隔离）。
    """
    return _target_dir(code, report_type, base, user_id, date_suffix) / "report.html"


def save_report(
    code: str,
    html_path: str | Path,
    *,
    profile: Any = None,
    meta: Any = None,
    base: str | Path | None = None,
    report_type: str = "",
    user_id: int | None = None,
    date_suffix: str = "",
) -> dict:
    """把 HTML + 结构化 JSON + 元信息写入报告目录（路径规则同 `report_output_path`）。

    返回各文件实际路径；任何写失败向上抛出，由调用方决定降级。

    注意：本函数**自己重算目录**（不沿用 `html_path` 的所在目录），因此
    `user_id` / `date_suffix` 必须与调用 `report_output_path` 时**完全一致**，
    否则 HTML 与 meta/profile 会落到两个不同目录。
    """
    d = _target_dir(code, report_type, base, user_id, date_suffix)

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
