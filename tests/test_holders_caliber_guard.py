"""股东表口径护栏：表头必须由口径派生，流通口径绝不能写成「前十大股东」。

背景（2026-09-16 翻车）：报告表头写「前十大股东」，数据却来自东财前十大**流通**股东
大表（限售股持有人只显示无限售部分），脚注还被标成「东方财富前十大股东」——
且因该股两个口径恰好重合，从数字上根本发现不了。

本测试锁死三件事：
  1. 口径判定优先级（落库标签 > 名册字段 > 行内 `_口径` 标记 > 东财十大股东）；
  2. 表头由口径派生：流通口径 → 「前十大流通股东」；
  3. 端到端渲染后表头/脚注自洽（用真实 profile 离线跑 build_overview_data）。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fundamental_agent.report_types.overview import (  # noqa: E402
    HOLDER_CALIBER_FREE,
    HOLDER_CALIBER_ROSTER,
    HOLDER_CALIBER_TOP10,
    build_overview_data,
    holders_caliber_of,
    holders_title,
)

# 已有名册口径的真实 profile（离线、零网络、零 LLM）
_REAL_PROFILE = ROOT / "output" / "reports" / "300308" / "overview" / "20260916" / "profile.json"


# --------------------------------------------------------------------------
# 1) 口径判定优先级
# --------------------------------------------------------------------------
def test_caliber_empty():
    assert holders_caliber_of({}) == ""
    assert holders_caliber_of({"holders": []}) == ""


def test_caliber_explicit_field_wins():
    gov = {"holders_caliber": HOLDER_CALIBER_FREE, "holders": [{"股东名称": "A"}]}
    assert holders_caliber_of(gov) == HOLDER_CALIBER_FREE


def test_caliber_roster_detected_by_report_holders():
    assert holders_caliber_of({"report_holders": {"holders": [{"name": "A"}]}}) == HOLDER_CALIBER_ROSTER


def test_caliber_free_detected_by_row_marker():
    """兼容历史 profile：未落库 holders_caliber 时仍须认出流通口径。"""
    gov = {"holders": [{"股东名称": "A", "_口径": "流通股东"}]}
    assert holders_caliber_of(gov) == HOLDER_CALIBER_FREE


def test_caliber_defaults_to_top10():
    assert holders_caliber_of({"holders": [{"股东名称": "A"}]}) == HOLDER_CALIBER_TOP10


# --------------------------------------------------------------------------
# 2) 表头派生
# --------------------------------------------------------------------------
def test_title_follows_caliber():
    assert holders_title(HOLDER_CALIBER_FREE) == "前十大流通股东"
    assert holders_title(HOLDER_CALIBER_ROSTER) == "前十大股东"
    assert holders_title(HOLDER_CALIBER_TOP10) == "前十大股东"
    assert holders_title("") == "前十大股东"


# --------------------------------------------------------------------------
# 3) 端到端：真实 profile 离线渲染，断言表头与脚注自洽
# --------------------------------------------------------------------------
pytestmark_e2e = pytest.mark.skipif(not _REAL_PROFILE.exists(), reason="缺 300308 profile 基线")


def _render(gov_patch: dict) -> dict:
    prof = json.loads(_REAL_PROFILE.read_text(encoding="utf-8"))
    prof = copy.deepcopy(prof)
    gov = prof["layers"]["governance"]
    gov.update(gov_patch)
    import types

    res = types.SimpleNamespace(code="300308", name=prof.get("name") or "300308")
    return build_overview_data(res, prof, None, use_llm=False)


def _as_top10_rows(gov: dict, *, free: bool) -> list:
    """把名册镜像行改造成东财口径行（去掉名册独有字段，补「占总股本持股比例」）。"""
    cap = float(gov.get("share_capital") or 0)
    out = []
    for i, h in enumerate(gov.get("holders") or []):
        shares = float(h.get("持股数") or 0)
        row = {
            "名次": i + 1,
            "股东名称": h.get("股东名称") or "",
            "报告期": "2026-06-30",
            "持股数": shares,
            "占总股本持股比例": (shares / cap * 100.0) if cap else 0.0,
            "增减": "—",
            "变动比率": "—",
        }
        if free:
            row["_口径"] = "流通股东"
        out.append(row)
    return out


@pytestmark_e2e
def test_e2e_roster_title_and_note():
    data = _render({})
    assert data["shareholders"], "名册口径应渲染出股东表"
    assert data["shareholders_title"] == "前十大股东"
    assert "名册" in data["gov_note"]


@pytestmark_e2e
def test_e2e_top10_title_and_note():
    prof_gov = json.loads(_REAL_PROFILE.read_text(encoding="utf-8"))["layers"]["governance"]
    rows = _as_top10_rows(prof_gov, free=False)
    data = _render({"report_holders": None, "holders": rows, "holders_caliber": HOLDER_CALIBER_TOP10})
    assert data["shareholders"], "东财十大股东口径应渲染出股东表"
    assert data["shareholders_title"] == "前十大股东"
    assert "东方财富前十大股东" in data["gov_note"]


@pytestmark_e2e
def test_e2e_free_caliber_never_claims_top10():
    """核心回归：流通兜底口径的表头必须显式写「流通」，脚注必须说明不含限售股。"""
    prof_gov = json.loads(_REAL_PROFILE.read_text(encoding="utf-8"))["layers"]["governance"]
    rows = _as_top10_rows(prof_gov, free=True)
    data = _render({"report_holders": None, "holders": rows, "holders_caliber": HOLDER_CALIBER_FREE})
    assert data["shareholders"], "流通口径应渲染出股东表"
    assert "流通" in data["shareholders_title"], "流通口径不得以「前十大股东」表头呈现"
    assert "不含限售" in data["gov_note"] or "流通口径" in data["gov_note"]


@pytestmark_e2e
def test_e2e_free_caliber_detected_without_explicit_field():
    """未落库 holders_caliber 时，靠行内 `_口径` 标记也要能拦下。"""
    prof_gov = json.loads(_REAL_PROFILE.read_text(encoding="utf-8"))["layers"]["governance"]
    rows = _as_top10_rows(prof_gov, free=True)
    data = _render({"report_holders": None, "holders": rows})
    assert "流通" in data["shareholders_title"]
