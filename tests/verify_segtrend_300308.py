# -*- coding: utf-8 -*-
"""离线验证：分部趋势「三年」期次不再被残差科目（其他）压掉（不消耗 LLM）。

背景：中际旭创（300308）基准期 2025 年报列了「光通信收发模块 / 其他」两项，
而 2023/2024 年报没有「其他」这个轧差科目 → 匹配率分母被虚高到 2，历史期只命中
1 项 = 50% < 70% 阈值，两个年报期被整期剔除，标题「三年分部趋势」实际只剩
2025 + 2026H1 两列。
修复后：匹配率分母剔除残差科目 → 四期全部采纳（2023/2024/2025/2026H1）。
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
OUT_DIR = ROOT / "output" / "_verify_seg_300308"
OUT_DIR.mkdir(exist_ok=True)

PROFILE = ROOT / "output/reports/300308/overview/20260916/profile.json"


def main() -> int:
    failures: list[str] = []
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    result = types.SimpleNamespace(code="300308", name=profile.get("name") or "300308")

    d = build_overview_data(result, profile, llm_json=None, use_llm=False)
    labels = d.get("seg_trend_labels") or []
    print("[300308] seg_trend_labels =", labels)
    print("[300308] seg_trend_has_h1 =", d.get("seg_trend_has_h1"))
    print("[300308] seg_trend_note  =", (d.get("seg_trend_note") or "")[:200])
    print()
    for row in d.get("seg_trend") or []:
        print("  行:", row.get("name"), "->", row.get("cells"), "| 近期:", row.get("recent"), "| 同比:", row.get("yoy"))
    print()

    # 断言 1：四期齐备
    if labels != ["2023", "2024", "2025", "2026H1"]:
        failures.append(f"期次应为 ['2023','2024','2025','2026H1']，实际 {labels}")

    # 断言 2：主力板块每期都有数；逐段归并后 2023 应为 101.82 亿
    # （97.99 高速 + 3.83 中低速），而非只取最优单段的 97.99 亿。
    main_row = next((r for r in (d.get("seg_trend") or []) if "光通信" in str(r.get("name"))), None)
    if main_row is None:
        failures.append("趋势表缺少光通信板块行")
    else:
        cells = main_row.get("cells") or []
        for i, lab in enumerate(["2023", "2024", "2025", "2026H1"]):
            if i >= len(cells) or cells[i].startswith("—"):
                failures.append(f"光通信板块 {lab} 列无数据：{cells}")
        if cells and not cells[0].startswith("101.82"):
            failures.append(f"光通信板块 2023 应为「101.82 亿」（高速+中低速归并），实际 {cells[0]}")

    # 断言 3：合计行 == 当期真实主营收入（东财主营构成全量求和）
    tot_row = next((r for r in (d.get("seg_trend") or []) if str(r.get("name")) == "合计"), None)
    if tot_row is None:
        failures.append("趋势表缺少合计行")
    else:
        cells = tot_row.get("cells") or []
        expect = {"2023": 107.18, "2024": 238.62, "2025": 382.40, "2026H1": 419.11}
        for i, lab in enumerate(["2023", "2024", "2025", "2026H1"]):
            if i >= len(cells):
                failures.append(f"合计行缺 {lab} 列")
                continue
            try:
                got = float(cells[i].split("亿")[0].strip())
            except Exception:
                failures.append(f"合计行 {lab} 无法解析：{cells[i]}")
                continue
            if abs(got - expect[lab]) > 0.02:
                failures.append(f"合计行 {lab} 应为 {expect[lab]:.2f} 亿，实际 {got:.2f} 亿")

    # 断言 4：未匹配的历史独立板块应并入「其他」行（2023 汽车光电子+光组件=5.36 亿）
    other_row = next((r for r in (d.get("seg_trend") or []) if str(r.get("name")) == "其他"), None)
    if other_row is None:
        failures.append("趋势表缺少其他行")
    else:
        cells = other_row.get("cells") or []
        if cells and not cells[0].startswith("5.36"):
            failures.append(f"其他行 2023 应为「5.36 亿」（汽车光电子+光组件归并），实际 {cells[0]}")

    # 断言 5：渲染产物含四期列头
    # 模板里列头形如 <th class="num-c" ...>2023<br ...>收入 / 毛利率</th>，
    # 故用「>年份<br」而非「>年份</th>」匹配。
    out = OUT_DIR / "verify_300308.html"
    render_overview(d, str(TPL_DIR), str(out))
    h = out.read_text(encoding="utf-8")
    for lab in ("2023", "2024", "2025", "2026H1"):
        if f">{lab}<br" not in h:
            failures.append(f"渲染产物缺少列头：{lab}")

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    print("outputs:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
