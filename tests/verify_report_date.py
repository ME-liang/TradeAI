# -*- coding: utf-8 -*-
"""离线验证：页眉「报告日期」必须为生成当日（不消耗 LLM 额度）。

复现 601138 线上问题：契约把输入数据里主营构成的「报告日期 2025-12-31」
回填成报告日期，覆盖了系统生成的当日日期。修复后：
  1) build_overview_data 丢弃 llm_json 的 report_date / as_of_text（日志 WARNING）
  2) 最终 report_date == 本地今日日期（仅到日），与 LLM 输入无关
  3) 渲染产物页眉含当日日期、不含被回填的错误日期
  4) 回归：upstream 护栏仍生效（上一轮修复不被破坏）
"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fundamental_agent.report_types.overview import (  # noqa: E402
    build_overview_data,
    render_overview,
)

TPL_DIR = ROOT / "src" / "fundamental_agent" / "report"
OUT_DIR = ROOT / "output" / "_verify_report_date"
OUT_DIR.mkdir(exist_ok=True)


def load_chain(code: str) -> dict:
    hits = list((ROOT / "data" / "cache" / "annual_extract").glob(f"chain_{code}_*.json"))
    assert hits, f"no chain cache for {code}"
    return json.loads(hits[0].read_text(encoding="utf-8"))


def main() -> int:
    failures: list[str] = []
    today = datetime.now().strftime("%Y-%m-%d")

    # 模拟 601138 故障：契约回填报告日期 + 数据时点说明
    fake_llm = {
        "name": "工业富联",
        "report_date": "2025-12-31",
        "as_of_text": "财务数据截至 2025-12-31",
    }
    profile = {
        "code": "601138",
        "name": "工业富联",
        "layers": {
            "identity": {"short_name": "工业富联"},
            "business": {"business_chain": load_chain("601138")},
            "governance": {},
            "market": {},
        },
    }
    result = types.SimpleNamespace(code="601138", name="工业富联")
    d = build_overview_data(result, profile, llm_json=fake_llm, use_llm=False)

    print("[601138] report_date =", d.get("report_date"))
    print("[601138] as_of_text  =", d.get("as_of_text"))
    if d.get("report_date") != today:
        failures.append(
            f"report_date={d.get('report_date')!r} 不是生成当日 {today}"
        )
    if "2025-12-31" == d.get("report_date"):
        failures.append("report_date 仍被契约回填为 2025-12-31")
    if (d.get("as_of_text") or "").strip() == "财务数据截至 2025-12-31":
        failures.append("as_of_text 仍被契约回填")
    if d.get("name") != "工业富联":
        failures.append("name 合并被系统字段护栏误伤")

    # upstream 护栏回归（上一轮修复）
    fake_llm2 = {"upstream": [{"camp": "光芯片（核心）", "makers": "X"}]}
    d2 = build_overview_data(
        types.SimpleNamespace(code="002384", name="东山精密"),
        {
            "code": "002384",
            "name": "东山精密",
            "layers": {
                "identity": {"short_name": "东山精密"},
                "business": {"business_chain": load_chain("002384")},
                "governance": {},
                "market": {},
            },
        },
        llm_json=fake_llm2,
        use_llm=False,
    )
    if d2.get("upstream"):
        failures.append("upstream 护栏回归失败：契约阵营表未被丢弃")

    # 渲染层：页眉日期
    out = OUT_DIR / "verify_601138.html"
    render_overview(d, str(TPL_DIR), str(out))
    html = out.read_text(encoding="utf-8")
    import re

    m = re.search(r"报告日期：([^<\s&]+)", html)
    print("[601138] 渲染页眉 =", m.group(1) if m else "未找到")
    if not m or m.group(1) != today:
        failures.append(f"渲染页眉报告日期 {m.group(1) if m else None!r} != {today}")

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    print("output:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
