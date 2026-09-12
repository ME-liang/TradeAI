"""writer_agent 单元测试：direct 模式直写 HTML + 校验重试闭环（Step 4 / M3）。

不调用真实 LLM：mock router.complete 返回预置稿件，验证校验闭环的分诊逻辑——
好稿通过、坏稿（量级冲突/存货错标/JS 语法错/截断）被驳回且回传报错、重试后通过。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import fundamental_agent.agents.writer_agent as wa  # noqa: E402
from fundamental_agent.agents.writer_agent import (  # noqa: E402
    _mislabel_check,
    generate_overview_html,
)


GOOD_HTML = (
    "<!DOCTYPE html><html><head><style>body{color:#222}"
    + "/* padding */" * 500
    + "</style></head><body>"
    "<h1>测试公司（300999）</h1><p>分销业务稳健。</p>"
    "<script>var a=[1,2];console.log(a);</script>"
    "</body></html>"
)


def _profile(gross_margin=0.2015, inventory=5.0e8, ar=3.0e8):
    """最小六层快照：financials.summary 提供量级证伪与错标检查所需真值。"""
    return {
        "layers": {
            "financials": {
                "summary": {
                    "gross_margin": gross_margin,
                    "net_margin": 0.05,
                    "inventory": inventory,
                    "accounts_receivable": ar,
                    "revenue_by_period": {"2025": 5.0e9},
                }
            },
            "dynamics": {},
        }
    }


class _Resp:
    def __init__(self, text, ok=True, provider="mock", error=""):
        self.text = text
        self.ok = ok
        self.provider = provider
        self.error = error


@pytest.fixture
def no_skill(monkeypatch):
    """绕过真实 skill 加载（SKILL.md 在仓库中存在，但单测只关心闭环逻辑）。"""
    monkeypatch.setattr(
        wa, "load_skill", lambda name: {"system": "你是概况报告写手", "references": {}}
    )


class _FakeRouter:
    def __init__(self, texts):
        self.texts = texts
        self.calls = []
        self._i = 0

    def complete(self, req):
        self.calls.append(req)
        t = self.texts[min(self._i, len(self.texts) - 1)]
        self._i += 1
        return _Resp(t)


def _patch_router(monkeypatch, texts):
    """把 writer_agent 命名空间内的 get_router 整体替换为假路由（get_router 每次返回
    新实例，不能 patch 实例方法）。返回假路由供断言重试反馈是否回传。"""
    fake = _FakeRouter(texts)
    monkeypatch.setattr(wa, "get_router", lambda: fake)
    return fake


def test_good_html_passes_first_attempt(monkeypatch, no_skill):
    _patch_router(monkeypatch, [GOOD_HTML])
    out = generate_overview_html(None, _profile(), "测试公司", "300999")
    assert out and out["html"] and out["attempts"] == 1


def test_truncated_html_rejected_then_pass(monkeypatch, no_skill):
    truncated = GOOD_HTML[:60]  # 无 </html>
    fake = _patch_router(monkeypatch, [truncated, GOOD_HTML])
    out = generate_overview_html(None, _profile(), "测试公司", "300999")
    assert out and out["html"]
    assert out["attempts"] == 2
    assert "完整 HTML" in fake.calls[1].user  # 报错原文回传


def test_margin_hallucination_rejected(monkeypatch, no_skill):
    # 正文毛利率 39.33% vs 真值 20.15%：量级冲突（>1.8x），必须驳回
    bad = GOOD_HTML.replace("分销业务稳健", "公司毛利率高达 39.33%，行业领先")
    fake = _patch_router(monkeypatch, [bad, GOOD_HTML])
    out = generate_overview_html(None, _profile(), "测试公司", "300999")
    assert out and out["html"] and out["attempts"] == 2
    assert "毛利率" in fake.calls[1].user


def test_inventory_mislabel_rejected(monkeypatch, no_skill):
    # 历史事故回归：存货被写成 20.34 亿（真值 5 亿），第一稿驳回、第二稿通过
    bad = GOOD_HTML.replace("分销业务稳健", "期末存货 20.34 亿元，规模较大")
    _patch_router(monkeypatch, [bad, GOOD_HTML])
    out = generate_overview_html(None, _profile(), "测试公司", "300999")
    assert out and out["html"] and out["attempts"] == 2


def test_mislabel_check_unit():
    # 直接单测错标器：数值对不上命中；数值匹配放行
    prof = _profile(inventory=5.0e8, ar=3.0e8)
    bad = "期末存货 20.34 亿元"
    assert _mislabel_check(bad, prof)
    ok = "期末存货 5.02 亿元；应收账款 3.01 亿元"
    assert not _mislabel_check(ok, prof)


def test_all_retries_exhausted_returns_none_html(monkeypatch, no_skill):
    bad = GOOD_HTML.replace("分销业务稳健", "公司毛利率高达 39.33%，行业领先")
    _patch_router(monkeypatch, [bad])  # 每次都返回同一坏稿
    out = generate_overview_html(None, _profile(), "测试公司", "300999")
    assert out is not None and out["html"] is None  # 调用方据此降级 contract
    assert len(out["last_errors"]) > 0
