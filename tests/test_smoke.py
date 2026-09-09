"""冒烟测试：空壳 pipeline 也能跑通并生成 HTML。

运行：pytest tests/test_smoke.py
（pyproject.toml 已配置 pythonpath=["src"]，pytest 会自动识别包；
 直接用 `python tests/test_smoke.py` 运行时，下方 sys.path 兜底依然有效。）
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
    assert "（指标待实现）" in html
