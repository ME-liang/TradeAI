"""_reuse_today 按成稿模式（writer_mode）区分复用的单元测试（Step 4）。

用假 code（999999）在真实 output 目录构造 meta 产物，通过 monkeypatch
yaml.safe_load 控制 config 读取，验证：contract 模式不命中 direct 产物，
direct 模式命中；切换模式不误用旧报告。
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import fundamental_agent.api.server as server  # noqa: E402

CODE = "999999"  # 测试专用假代码


def _make_product(writer_mode):
    from datetime import date

    d = (
        ROOT
        / "output"
        / "reports"
        / CODE
        / "overview"
        / date.today().strftime("%Y%m%d")  # 必须与 report_output_path 同日
    )
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.html").write_text("<html><body>test</body></html>", encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps(
            {"code": CODE, "report_type": "overview", "writer_mode": writer_mode}
        ),
        encoding="utf-8",
    )
    return d


@pytest.fixture
def product(tmp_path):
    d = _make_product("direct")
    yield d
    import shutil

    shutil.rmtree(ROOT / "output" / "reports" / CODE, ignore_errors=True)


def _patch_mode(monkeypatch, mode):
    monkeypatch.setattr(
        server.yaml, "safe_load", lambda *_a, **_k: {"overview_writer": {"mode": mode}}
    )


def test_reuse_miss_when_mode_differs(monkeypatch, product):
    _patch_mode(monkeypatch, "contract")
    assert server._reuse_today(CODE, "overview") is None


def test_reuse_hit_when_mode_matches(monkeypatch, product):
    _patch_mode(monkeypatch, "direct")
    hit = server._reuse_today(CODE, "overview")
    assert hit and hit.endswith("report.html")


def test_reuse_hit_for_contract_product(monkeypatch):
    d = _make_product("contract")
    try:
        _patch_mode(monkeypatch, "contract")
        assert server._reuse_today(CODE, "overview")
    finally:
        import shutil

        shutil.rmtree(ROOT / "output" / "reports" / CODE, ignore_errors=True)
