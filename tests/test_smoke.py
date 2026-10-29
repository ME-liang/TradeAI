"""冒烟测试：空壳 pipeline 也能跑通并生成 HTML。

运行：pytest tests/test_smoke.py
（pyproject.toml 已配置 pythonpath=["src"]，pytest 会自动识别包；
 直接用 `python tests/test_smoke.py` 运行时，下方 sys.path 兜底依然有效。）

⚠️ 本用例**不是 hermetic 的**（2026-09-26 实测）：`analyze(use_dummy=True)` 只让
   六大分析引擎用假数据，**概况六层取数仍会 spawn 子进程走真实网络**（`main.py`
   的 `_overview_layers_worker`，原因为 akshare C 扩展的偶发进程级崩溃隔离）。
   子进程内实测耗时可达 **184s**（网络不顺时更久），故单独跑看似"卡住"。
   跑全仓时请预留时间，或用 `-k "not smoke"` 排除。
"""

import os
import sys


# 兜底：非 pytest 直接运行（python tests/test_smoke.py）时也能找到包
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from src.fundamental_agent.main import analyze  # noqa: E402
from src.fundamental_agent.report.render import render  # noqa: E402


def test_smoke_generates_html(tmp_path):
    result = analyze(name="测试", code="000001", use_dummy=True)
    assert result is not None
    assert len(result.modules) == 6  # 六大模块骨架齐全

    out = os.path.join(str(tmp_path), "report.html")
    path = render(result, template_dir=os.path.join(ROOT, "src", "fundamental_agent", "report"), out_path=out)
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        html = f.read()
    assert "基本面分析报告" in html
    # 断言真实产物契约（原断言 "（指标待实现）" 早已废弃：模板里已无该占位符，
    # 保留它等于长期挂一盏红灯，会掩盖真正的回归。2026-09-26 实测确认）
    assert "<!DOCTYPE" in html and "</html>" in html, "产物必须是完整文档而非片段"
    assert "<svg" in html, "报告必须带图表（F-07 导出依赖它，图表丢失=导出白页）"
