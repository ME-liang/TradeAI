# -*- coding: utf-8 -*-
"""离线验证：分红与股东回报区块展示近期股份回购（零 LLM 消耗）。

覆盖三层：
  1) 抽取层：extract_repurchase / extract_repurchase_semi 对缓存 PDF 的结果
     （002384 表格版式 → 4,202,200 股·员工持股计划/股权激励；300184 半年报注销轮）
  2) 装配层：gov.repurchase → repurchase_metrics/repurchase_text
  3) 渲染层：分红与股东回报区块内出现回购卡与回购说明，独立「股份回购」h3 不再存在
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fundamental_agent.data.annual_report import (  # noqa: E402
    extract_repurchase,
    extract_repurchase_semi,
    get_corporate_actions,
)
from fundamental_agent.report_types.overview import (  # noqa: E402
    build_overview_data,
    render_overview,
)

TPL_DIR = ROOT / "src" / "fundamental_agent" / "report"
REPORTS = ROOT / "data" / "cache" / "reports"
OUT_DIR = ROOT / "output" / "_verify_repurchase"
OUT_DIR.mkdir(exist_ok=True)


def main() -> int:
    failures: list[str] = []

    # ---- 1) 抽取层：002384 半年报（进展信号：过户员工持股 + 现金回购支出）----
    rp_384_semi = extract_repurchase_semi(str(REPORTS / "002384_2026年半年度报告.pdf"))
    print("[002384 半年报] repurchase =", json.dumps(rp_384_semi, ensure_ascii=False)[:260])
    if not rp_384_semi:
        failures.append("002384 半年报 extract_repurchase_semi 返回 None")
    else:
        _et = rp_384_semi.get("esop_transfer") or {}
        if _et.get("shares") != 3048701 or _et.get("date") != "2026-03-09":
            failures.append(f"002384 半年报过户进展抽取错误：{_et}")
        if abs((rp_384_semi.get("h1_cash_spent_yi") or 0) - 1.0) > 0.01:
            failures.append(f"002384 半年报回购支出抽取错误：{rp_384_semi.get('h1_cash_spent_yi')}")

    # ---- 1) 抽取层：002384 表格版式（年报）----
    rp_ann = extract_repurchase(str(REPORTS / "002384_2025年年度报告.pdf"))
    print("[002384 年报] repurchase =", json.dumps(rp_ann, ensure_ascii=False)[:220])
    if not rp_ann:
        failures.append("002384 年报 extract_repurchase 返回 None")
    else:
        if rp_ann.get("executed_shares") != 4202200:
            failures.append(f"002384 已回购数量 {rp_ann.get('executed_shares')} != 4,202,200")
        if rp_ann.get("purpose") != "员工持股计划/股权激励":
            failures.append(f"002384 用途识别错误：{rp_ann.get('purpose')}")
        if not rp_ann.get("disclosure_date"):
            failures.append("002384 披露日期未抽出")

    # ---- 1) 抽取层：300184 半年报注销轮（回归验证，确保没改坏）----
    rp_semi = extract_repurchase_semi(str(REPORTS / "300184_2026年半年度报告.pdf"))
    print("[300184 半年报] repurchase =", json.dumps(rp_semi, ensure_ascii=False)[:220])
    if not rp_semi or rp_semi.get("executed_shares") != 1973093:
        failures.append("300184 半年报回购抽取回归失败（期望 1,973,093 股）")

    # ---- 1) 抽取层：300184 年报（模式 A 正文句式回归）----
    rp_184_ann = extract_repurchase(str(REPORTS / "300184_2025年年度报告.pdf"))
    print("[300184 年报] repurchase =", json.dumps(rp_184_ann, ensure_ascii=False)[:220])
    if not rp_184_ann or rp_184_ann.get("executed_shares") != 2979800:
        failures.append("300184 年报正文句式回归失败（期望 2,979,800 股）")

    # ---- 2)+3) 装配 + 渲染：002384 与 300184 ----
    actions = {}
    for code, name in (("002384", "东山精密"), ("300184", "力源信息")):
        ca = get_corporate_actions(code)
        actions[code] = ca
        rp = (ca or {}).get("repurchase")
        print(f"[{code}] get_corporate_actions.repurchase =",
              json.dumps(rp, ensure_ascii=False)[:260] if rp else None)
        if not rp:
            failures.append(f"{code} get_corporate_actions 未产出 repurchase")
            continue
        if code == "002384":
            # 合并断言：年度轮（4,202,200 股）+ 半年报最新进展（过户 + 支出）
            if rp.get("executed_shares") != 4202200:
                failures.append(f"002384 合并后缺年度轮：{rp.get('executed_shares')}")
            if (rp.get("esop_transfer") or {}).get("shares") != 3048701:
                failures.append(f"002384 合并后缺过户进展：{rp.get('esop_transfer')}")
            if "最新进展" not in str(rp.get("text")) or "累计回购" not in str(rp.get("text")):
                failures.append(f"002384 合并叙述不完整：{rp.get('text')}")
            if abs((rp.get("h1_cash_spent_yi") or 0) - 1.0) > 0.01:
                failures.append(f"002384 合并后缺回购支出：{rp.get('h1_cash_spent_yi')}")
        profile = {
            "code": code,
            "name": name,
            "layers": {
                "identity": {"short_name": name},
                "business": {},
                "governance": {"repurchase": rp},
                "market": {},
            },
        }
        data = build_overview_data(
            types.SimpleNamespace(code=code, name=name),
            profile, llm_json=None, use_llm=False,
        )
        if not data.get("repurchase_metrics"):
            failures.append(f"{code} 装配层 repurchase_metrics 为空")
        out = OUT_DIR / f"verify_{code}.html"
        render_overview(data, str(TPL_DIR), str(out))
        h = out.read_text(encoding="utf-8")
        if "分红与股东回报" not in h:
            failures.append(f"{code} 渲染产物缺「分红与股东回报」标题")
        if "回购·已回购股数" not in h:
            failures.append(f"{code} 渲染产物缺回购指标卡（回购·已回购股数）")
        if '<h3 data-page-node-id="Rp8hKq2mZ4nT7vXbN1cQdE">股份回购</h3>' in h:
            failures.append(f"{code} 独立「股份回购」h3 仍在渲染")
        if "回购·已回购股数" in h and "分红与股东回报" in h:
            i_div = h.find("分红与股东回报")
            i_rp = h.find("回购·已回购股数")
            if not (0 < i_div < i_rp):
                failures.append(f"{code} 回购卡未位于分红与股东回报区块内")

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    print("outputs:", list(str(p) for p in OUT_DIR.glob("verify_*.html")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
