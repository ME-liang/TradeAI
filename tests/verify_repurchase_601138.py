# -*- coding: utf-8 -*-
"""离线验证：601138 工业富联回购数据不再陈旧（零 LLM 消耗）。

背景：报告「分红与股东回报」曾显示「931.99 万股 · 2024 年 · 股权激励」，是两处独立
缺陷叠加的结果：
  1) 半年报 extract_repurchase_semi 三个句柄全不匹配工业富联行文 → 返回 None → 上层回退年报
  2) 年报 extract_repurchase 在两轮方案表格上跨轮错配（日期取轮 A 的 2024-07-31、
     数量取轮 B 的 9,319,897），用途又被表头「已回购数量占股权激励计划所涉及的标的
     股票的比例」污染成"股权激励"

最新一手事实（2026 半年报）：2025-04-30 股东大会通过的回购方案截至 2026-03-31 实施
完毕，累计回购 14,103,497 股并全部注销（减少注册资本）。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fundamental_agent.data.annual_report import (  # noqa: E402
    _get_corporate_actions_impl,
    extract_repurchase,
    extract_repurchase_semi,
)
from fundamental_agent.report_types.overview import (  # noqa: E402
    build_overview_data,
    render_overview,
)

REPORTS = ROOT / "data" / "cache" / "reports"
ANN = REPORTS / "601138_富士康工业互联网股份有限公司2025年年度报告.pdf"
SEMI = REPORTS / "601138_富士康工业互联网股份有限公司2026年半年度报告.pdf"
TPL_DIR = ROOT / "src" / "fundamental_agent" / "report"
OUT_DIR = ROOT / "output" / "_verify_repurchase"
OUT_DIR.mkdir(exist_ok=True)


def main() -> int:
    failures: list[str] = []

    # ---- 1) 半年报：必须命中最新一轮（14,103,497 股 / 2026-03-31 完毕 / 注销减资）----
    semi = extract_repurchase_semi(str(SEMI))
    print("[601138 半年报] =", json.dumps(semi, ensure_ascii=False))
    if not semi:
        failures.append("半年报 extract_repurchase_semi 仍返回 None（①' 句柄未生效）")
    else:
        if semi.get("executed_shares") != 14103497:
            failures.append(f"半年报已回购股数 {semi.get('executed_shares')} != 14,103,497")
        if semi.get("completed_date") != "2026-03-31":
            failures.append(f"半年报完成日 {semi.get('completed_date')} != 2026-03-31")
        if semi.get("purpose") != "注销并减少注册资本":
            failures.append(f"半年报用途错误：{semi.get('purpose')}")
        if semi.get("source") != "半年报":
            failures.append(f"半年报 source 缺失：{semi.get('source')}")

    # ---- 2) 年报：取披露时间最新的轮次，且用途不得被表头污染 ----
    ann = extract_repurchase(str(ANN))
    print("[601138 年报]   =", json.dumps(ann, ensure_ascii=False))
    if not ann:
        failures.append("年报 extract_repurchase 返回 None")
    else:
        if ann.get("disclosure_date") != "2025-04-15":
            failures.append(
                f"年报披露日期 {ann.get('disclosure_date')} != 2025-04-15（未取最新轮）"
            )
        if ann.get("executed_shares") != 9319897:
            failures.append(f"年报已回购股数 {ann.get('executed_shares')} != 9,319,897")
        if ann.get("purpose") != "注销并减少注册资本":
            failures.append(f"年报用途错误（疑被表头污染）：{ann.get('purpose')}")
        if ann.get("disclosure_date") == "2024-07-31":
            failures.append("年报仍停在旧轮（2024-07-31）")
        if ann.get("source") != "年度报告":
            failures.append(f"年报 source 缺失：{ann.get('source')}")

    # ---- 3) 合并层：半年报覆盖年报，取到最新一轮 ----
    # 走 _get_corporate_actions_impl 绕开 actions_601138_*.json 存量缓存（零副作用，
    # 不改动缓存文件；PDF 缓存命中故仍零网络）。
    merged = (_get_corporate_actions_impl("601138") or {}).get("repurchase") or {}
    print("[601138 合并]   =", json.dumps(merged, ensure_ascii=False))
    if merged.get("executed_shares") != 14103497:
        failures.append(f"合并后未取到最新轮：{merged.get('executed_shares')} != 14,103,497")
    if merged.get("completed_date") != "2026-03-31":
        failures.append(f"合并后完成日错误：{merged.get('completed_date')}")
    if merged.get("source") != "半年报":
        failures.append(f"合并后 source 错误：{merged.get('source')}")

    # ---- 4) 装配 + 渲染：报告不再显示旧的 931.99 万股 / 股权激励 ----
    profile = {
        "code": "601138",
        "name": "工业富联",
        "layers": {
            "identity": {"short_name": "工业富联"},
            "business": {},
            "governance": {"repurchase": merged},
            "market": {},
        },
    }
    data = build_overview_data(
        types.SimpleNamespace(code="601138", name="工业富联"),
        profile,
        llm_json=None,
        use_llm=False,
    )
    out = OUT_DIR / "verify_601138.html"
    render_overview(data, str(TPL_DIR), str(out))
    h = out.read_text(encoding="utf-8")
    print("[渲染] 回购指标卡 =",
          json.dumps(data.get("repurchase_metrics"), ensure_ascii=False))
    print("[渲染] 回购说明   =", data.get("repurchase_text"))
    if "分红与股东回报" not in h:
        failures.append("渲染产物缺「分红与股东回报」标题")
    if "931.99 万股" in h:
        failures.append("渲染产物仍含旧数据「931.99 万股」")
    if "1,410.35 万股" not in h and "1410.35 万股" not in h:
        failures.append("渲染产物未出现新数据「1,410.35 万股」")
    if "半年报一手来源" not in h:
        failures.append("渲染产物来源标注未同步为「半年报一手来源」")
    if "注销并减少注册资本" not in h:
        failures.append("渲染产物回购用途未更新为「注销并减少注册资本」")

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
