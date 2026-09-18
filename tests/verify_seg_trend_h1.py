# -*- coding: utf-8 -*-
"""离线验证：分部趋势在「数据源无 H1 口径」时隐藏近期收入/H1 同比两列（不消耗 LLM）。

背景：工业富联（601138）半年报未披露分产品构成（东财主营构成无 06-30 期次），
旧逻辑把最新年报值填进「近期收入」列（与 2025 列重复），且口径注谎称「含最新半年报」。
修复后：无 H1 期次 → 隐藏两列 + 口径注如实说明；有 H1 期次（002384）→ 两列照常。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fundamental_agent.report_types.overview import (  # noqa: E402
    build_overview_data,
    render_overview,
)

TPL_DIR = ROOT / "src" / "fundamental_agent" / "report"
OUT_DIR = ROOT / "output" / "_verify_seg_h1"
OUT_DIR.mkdir(exist_ok=True)

CASES = {
    "601138": ROOT / "output/reports/601138/overview/20260915/profile.json",
    "002384": ROOT / "output/reports/002384/overview/20260913/profile.json",
}


def build(code: str):
    profile = json.loads(CASES[code].read_text(encoding="utf-8"))
    result = types.SimpleNamespace(code=code, name=profile.get("name") or code)
    return build_overview_data(result, profile, llm_json=None, use_llm=False)


def render(code: str, data: dict) -> str:
    out = OUT_DIR / f"verify_{code}.html"
    render_overview(data, str(TPL_DIR), str(out))
    return out.read_text(encoding="utf-8")


def main() -> int:
    failures: list[str] = []

    # ---- 601138：无 H1 口径 → 两列隐藏 ----
    d = build("601138")
    print("[601138] seg_trend_labels =", d.get("seg_trend_labels"))
    print("[601138] seg_trend_has_h1 =", d.get("seg_trend_has_h1"))
    print("[601138] note =", (d.get("seg_trend_note") or "")[:150])
    if d.get("seg_trend_has_h1") is not False:
        failures.append("601138 应为无 H1 口径（seg_trend_has_h1 应为 False）")
    if d.get("seg_trend_labels") != ["2023", "2024", "2025"]:
        failures.append(f"601138 期次异常：{d.get('seg_trend_labels')}")
    if "最新半年报" in (d.get("seg_trend_note") or "") and "未披露" not in (d.get("seg_trend_note") or ""):
        failures.append("601138 口径注仍称「含最新半年报」")
    h = render("601138", d)
    # 只检查表头单元格（口径注里合法地提到「H1 同比」字样，不算泄漏）
    for needle in (">近期收入</th>", ">H1 同比</th>"):
        if needle in h:
            failures.append(f"601138 渲染产物仍出现列头：{needle}")

    # ---- 002384：有 H1 口径 → 两列照常（回归）----
    d2 = build("002384")
    print("\n[002384] seg_trend_labels =", d2.get("seg_trend_labels"))
    print("[002384] seg_trend_has_h1 =", d2.get("seg_trend_has_h1"))
    if d2.get("seg_trend_has_h1") is not True:
        failures.append("002384 应含最新半年度列（seg_trend_has_h1 应为 True）")
    if not any(str(x).endswith("H1") for x in (d2.get("seg_trend_labels") or [])):
        failures.append(f"002384 缺半年度列：{d2.get('seg_trend_labels')}")
    h2 = render("002384", d2)
    for needle in (">近期收入</th>", ">H1 同比</th>"):
        if needle not in h2:
            failures.append(f"002384 渲染产物丢失列头（回归）：{needle}")

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    print("outputs:", OUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
