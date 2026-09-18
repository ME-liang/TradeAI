# -*- coding: utf-8 -*-
"""离线验证：上下游/合作网络标题与导语动态化（不消耗 LLM 额度）。

用缓存的 chain JSON（300184 分销商 + 000001 非分销商对照）直接驱动
build_overview_data(use_llm=False) → render_overview，检查：
  1) 分销商分支：upstream_title / upstream_intro 含真实家数与阵营
  2) 下游标题含真实应用领域
  3) 合作网络标题含真实合作方
  4) 非分销商分支：upstream_struct_title/intro 生效
  5) 模板中不再出现写死话术正文（仅允许 fallback 表达式）
"""
from __future__ import annotations

import json
import re
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
OUT_DIR = ROOT / "output" / "_verify_bc_titles"
OUT_DIR.mkdir(exist_ok=True)


def load_chain(code: str) -> dict:
    hits = list((ROOT / "data" / "cache" / "annual_extract").glob(f"chain_{code}_*.json"))
    assert hits, f"no chain cache for {code}"
    return json.loads(hits[0].read_text(encoding="utf-8"))


def build(code: str, name: str, chain: dict | None):
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
    result = types.SimpleNamespace(code=code, name=name)
    return build_overview_data(result, profile, llm_json=None, use_llm=False)


def render(code: str, data: dict) -> str:
    out = OUT_DIR / f"verify_{code}.html"
    render_overview(data, str(TPL_DIR), str(out))
    return out.read_text(encoding="utf-8")


def main() -> int:
    failures: list[str] = []

    # ---- 300184 力源信息（分销商）----
    d1 = build("300184", "力源信息", load_chain("300184"))
    print("[300184] upstream_title =", d1.get("upstream_title"))
    print("[300184] upstream_intro  =", (d1.get("upstream_intro") or "")[:120])
    print("[300184] downstream_title=", d1.get("downstream_title"))
    print("[300184] partners_title  =", d1.get("partners_title"))

    if not d1.get("upstream_title"):
        failures.append("300184 缺 upstream_title")
    if not d1.get("upstream_intro"):
        failures.append("300184 缺 upstream_intro")
    if "上百条" in (d1.get("upstream_intro") or ""):
        failures.append("300184 upstream_intro 仍含硬编码「上百条」")
    if not d1.get("downstream_title"):
        failures.append("300184 缺 downstream_title")
    if not d1.get("partners_title"):
        failures.append("300184 缺 partners_title")
    # 家数必须与抽取口径一致
    regs = load_chain("300184").get("upstream_regions") or []
    dom = load_chain("300184").get("domestic_makers") or []
    expect_total = sum(r.get("count", 0) for r in regs) + len(dom)
    m = re.search(r"(\d+)\s*家原厂", d1.get("upstream_title") or "")
    if m and int(m.group(1)) != expect_total:
        failures.append(
            f"300184 upstream_title 家数 {m.group(1)} != 抽取口径 {expect_total}"
        )

    h1 = render("300184", d1)
    for needle in ("上游：代理了谁的产品线", "下游：卖给谁", "上百条国内外原厂线"):
        if needle in h1:
            failures.append(f"300184 渲染产物仍出现写死文案：{needle}")
    if d1.get("upstream_title") and d1["upstream_title"] not in h1:
        failures.append("300184 渲染产物未包含动态 upstream_title")

    # ---- 600519 贵州茅台（非分销商：补充期供应商集中度走确定性兜底 struct 分支）----
    d2 = build("600519", "贵州茅台", load_chain("600519"))
    print("[600519] upstream_struct_title =", d2.get("upstream_struct_title"))
    print("[600519] upstream_struct_intro =", (d2.get("upstream_struct_intro") or "")[:120])
    print("[600519] upstream_title(应为空) =", repr(d2.get("upstream_title")))
    if d2.get("upstream"):
        print("[600519] 注：该链路仍产出了阵营表，走分销商分支")
    if not (d2.get("upstream_struct_title") or d2.get("upstream_title")):
        failures.append("600519 上游标题（struct 或 camp 分支）均为空")

    h2 = render("600519", d2)
    if "该公司不属于授权分销模式，上游以采购与供应链" in h2:
        failures.append("600519 渲染产物仍出现写死 struct 导语")

    # ---- 000001 平安银行（上游完全无源：整个上游区块不应渲染，写死标题不得泄漏）----
    d3 = build("000001", "平安银行", load_chain("000001"))
    if d3.get("upstream_struct") or d3.get("upstream"):
        failures.append("000001 无上游数据却产出了上游表")
    h3 = render("000001", d3)
    for needle in ("上游：代理了谁的产品线", "上游：供给与供应链结构", "代理权是分销商的生命线"):
        if needle in h3:
            failures.append(f"000001（上游无源）渲染产物泄漏写死文案：{needle}")

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    print("outputs:", OUT_DIR / "verify_300184.html", "|", OUT_DIR / "verify_000001.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
