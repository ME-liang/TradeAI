# -*- coding: utf-8 -*-
"""离线验证：契约 upstream 阵营表硬护栏（不消耗 LLM 额度）。

复现 002384 线上问题：LLM 契约把供应商依赖关系（光芯片—索尔思光电）填进
upstream 阵营表，导致非分销商误走分销商版式 + 写死话术泄漏。修复后：
  1) build_overview_data 必须丢弃 llm_json["upstream"]（日志有 WARNING）
  2) 002384 走 struct 分支（供应商集中度兜底），分销商话术零泄漏
  3) 供应商集中度尾句用中性表述（非「代理权集中是分销商的护城河」）
  4) 300184 分销商回归：chain 抽取的阵营表不受影响
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
OUT_DIR = ROOT / "output" / "_verify_upstream_guard"
OUT_DIR.mkdir(exist_ok=True)


def load_chain(code: str) -> dict:
    hits = list((ROOT / "data" / "cache" / "annual_extract").glob(f"chain_{code}_*.json"))
    assert hits, f"no chain cache for {code}"
    return json.loads(hits[0].read_text(encoding="utf-8"))


def make_profile(code: str, name: str, chain: dict | None) -> dict:
    profile = {
        "code": code,
        "name": name,
        "layers": {
            "identity": {"short_name": name},
            "business": {},
            "governance": {},
            "market": {},
        },
    }
    if chain is not None:
        profile["layers"]["business"]["business_chain"] = chain
    return profile


def render(code: str, data: dict) -> str:
    out = OUT_DIR / f"verify_{code}.html"
    render_overview(data, str(TPL_DIR), str(out))
    return out.read_text(encoding="utf-8")


def main() -> int:
    failures: list[str] = []

    # ---- 002384：模拟 qwen 契约注入 upstream 阵营行（线上故障复现）----
    fake_llm = {
        "name": "东山精密",
        "upstream": [
            {
                "camp": "光芯片（核心）",
                "makers": "索尔思光电",
                "category": "EML光芯片",
                "hl": True,
            }
        ],
    }
    result = types.SimpleNamespace(code="002384", name="东山精密")
    d = build_overview_data(
        result,
        make_profile("002384", "东山精密", load_chain("002384")),
        llm_json=fake_llm,
        use_llm=False,
    )
    if d.get("upstream"):
        failures.append(
            f"002384 契约 upstream 未被丢弃，仍触发分销商分支：{d['upstream']}"
        )
    else:
        print("[002384] llm_json['upstream'] 已被护栏丢弃 ✓")
    if d.get("name") != "东山精密":
        failures.append("002384 契约 name 合并被护栏误伤")
    print("[002384] upstream_struct rows =", len(d.get("upstream_struct") or []))
    print("[002384] upstream_struct_title =", d.get("upstream_struct_title"))
    print("[002384] supplier_conc_html =",
          (d.get("supplier_conc_html") or "")[:150])

    h = render("002384", d)
    for needle in (
        "上游：代理了谁的产品线",
        "代理权是分销商的生命线",
        "代理权集中是分销商的护城河",
        "上百条国内外原厂线",
    ):
        if needle in h:
            failures.append(f"002384 渲染产物泄漏分销商话术：{needle}")
    if "索尔思光电" in h:
        failures.append("002384 渲染产物仍含被丢弃的索尔思阵营行")

    # ---- 300184 回归：chain 一手阵营表不受护栏影响 ----
    result2 = types.SimpleNamespace(code="300184", name="力源信息")
    d2 = build_overview_data(
        result2,
        make_profile("300184", "力源信息", load_chain("300184")),
        llm_json=None,
        use_llm=False,
    )
    if not d2.get("upstream"):
        failures.append("300184 chain 抽取的阵营表丢失（护栏误伤一手来源）")
    else:
        print(f"[300184] chain 阵营表保留 {len(d2['upstream'])} 行，"
              f"title={d2.get('upstream_title')}")

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
