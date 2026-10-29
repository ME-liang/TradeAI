"""报告图表的**本地库内联**（F-06 决策 D4）。

为什么要有这个模块
------------------
报告产物 `output/reports/**/report.html` 的设计承诺是「人类可读，**可离线打印**」
（`storage.py` 模块 docstring）。但概况报告模板引用 cdnjs 的 Chart.js、板块报告模板
引用 jsdelivr 的 ECharts —— 离线 / 内网 / 邮件转发后打开时**图表全部空白**，
这句承诺在实际使用场景下失效。

两种修法，选后者：

- 改外链为同源路径 `/js/echarts.min.js`：只在应用内有效（8002 侧没有静态服务），
  报告一旦被下载/转发/转 PDF 图表立刻空 —— 且把「报告能不能看」变成对运行时环境的
  隐式依赖（本项目已两次栽在隐式依赖上：行情源、缓存）。
- **渲染阶段把库文件内容内联进 `<script>`（本模块）**：报告自包含，代价是体积变大。

体积取舍
--------
报告是**交付物**（低频、一次性、需要可分发），不是页面资源（高频、需首屏速度），
因此体积可接受：概况报告约 70 KB → 约 270 KB；板块报告约 1.2 MB。
ECharts 复用仓库里已有的 `frontend/js/echarts.min.js`（约 1 MB），**不新增副本**。

⚠️ 这是**刻意的跨模块引用**（报告模块读前端静态目录）
------------------------------------------------------
目的是复用前端已有的那份 ECharts，避免仓库里躺两份 1 MB 的同一文件。
若将来前端目录结构调整，需同步改 `_FRONTEND_JS`。请在改动时保留本段说明，
否则下一个人会以为这是路径写错。

降级
----
文件缺失 / 读取异常 → 返回 `None`，模板回退 CDN 外链（即改造前行为）。
**绝不因静态资源缺失让报告生成失败** —— 与 `storage.py`「落盘失败绝不中断主流程」
同一哲学。
"""

from __future__ import annotations

from pathlib import Path

# 项目根：src/fundamental_agent/report_types/chart_assets.py → 上溯 3 级 = 仓库根
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND_JS = _PROJECT_ROOT / "frontend" / "js"

# 模板里变量的名字，避免各处写字符串字面量对不上
TEMPLATE_VAR = "chart_lib_inline"
OFFLINE_FLAG = "chart_offline"

# 进程内缓存：报告批量生成时不必反复读 1 MB 文件
_CACHE: dict[str, str | None] = {}


def _read_lib(filename: str) -> str | None:
    """读库文件并做内联安全处理；失败返回 None（调用方回退 CDN）。"""
    if filename in _CACHE:
        return _CACHE[filename]
    content: str | None = None
    try:
        p = _FRONTEND_JS / filename
        if p.is_file():
            content = p.read_text(encoding="utf-8")
            # 去掉 sourceMappingURL 注释：内联后 .map 并不存在，会在控制台留一条无谓报错
            content = _strip_source_map(content)
            # 防御性：`</script` 会提前闭合 <script> 标签（当前两个库实测均不含，
            # 但库升级后不能假定）。
            content = content.replace("</script", "<\\/script")
            if not content.strip():
                content = None
    except Exception:  # noqa: BLE001 — 任何读取异常都按「不可用」，绝不阻断报告生成
        content = None
    _CACHE[filename] = content
    return content


def _strip_source_map(src: str) -> str:
    """删除行尾的 `//# sourceMappingURL=...` 注释。"""
    idx = src.rfind("//# sourceMappingURL=")
    if idx < 0:
        return src
    end = src.find("\n", idx)
    return src[:idx] + (src[end:] if end >= 0 else "")


def inline_chartjs() -> str | None:
    """Chart.js 4.4.1（概况报告用）。"""
    return _read_lib("chart.umd.min.js")


def inline_echarts() -> str | None:
    """ECharts 5.5.0（板块报告用）——复用前端已有副本。"""
    return _read_lib("echarts.min.js")


def apply_chart_lib(data: dict, lib: str | None) -> dict:
    """把内联库与降级标志写进模板上下文（供 `**data` 渲染）。

    返回同一 dict（原地修改），便于渲染函数直接 `tpl.render(**data)`。
    """
    data[TEMPLATE_VAR] = lib or ""
    data[OFFLINE_FLAG] = bool(lib)
    return data
