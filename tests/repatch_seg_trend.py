# -*- coding: utf-8 -*-
"""外科式刷新「三年分部趋势」区块：只替换数据驱动部分，保留 LLM 标题与叙述段。

用途：某份**已生成**的报告因当时代码缺陷导致趋势表缺列（如 300308 只剩 2026H1、
002384 只剩 2025+2026H1、688981 缺 2026H1），而 LLM 额度不可用时无法整份重跑。
本工具复用既有 profile.json 走修复后的数据层重渲染，然后**只**把下列片段换进去：

  1. 趋势表的 <table>…</table>
  2. 其后到下一个 ``<div class="flag"`` 之间的数据驱动内容
     （两处口径注 + 自研业务 canvas 的期次标签）
  3. cSelfDev 图表的 ``new Chart(...)`` JS 块

h3 标题、``<div class="flag">`` 叙述段、以及报告其余全部内容**一字不动**。

用法：
    python tests/repatch_seg_trend.py output/reports/002384/overview/20260911 [...]
    python tests/repatch_seg_trend.py --all-marked     # 修所有体检标记的规范报告

首次运行会自动在报告目录留一份 ``report_before_fix.html`` 备份（已存在则不覆盖）。
零 LLM、零网络。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fundamental_agent.report_types.overview import (  # noqa: E402
    build_overview_data,
    render_overview,
)

TPL_DIR = ROOT / "src" / "fundamental_agent" / "report"


def _block_span(html: str) -> tuple[int, int]:
    i = html.find("三年分部趋势")
    assert i >= 0, "报告中未找到「三年分部趋势」"
    ends = [x for x in (html.find("<h3", i), html.find("<h2", i)) if x > 0]
    return html.rfind("<h3", 0, i), (min(ends) if ends else len(html))


def _table_span(html: str, s: int, e: int) -> tuple[int, int]:
    ts = html.find("<table", s, e)
    assert ts >= 0, "区块内未找到 <table>"
    return ts, html.find("</table>", ts, e) + len("</table>")


def _mid_span(html: str, s: int, e: int) -> tuple[int, int]:
    te = html.find("</table>", s, e) + len("</table>")
    fe = html.find('<div class="flag"', te, e)
    return te, (fe if fe > 0 else e)


def _chart_span(html: str) -> tuple[int, int] | None:
    j = html.find("new Chart(document.getElementById('cSelfDev')")
    if j < 0:
        return None
    return j, html.find("});", j) + len("});")


def repatch(report_dir: pathlib.Path) -> bool:
    target = report_dir / "report.html"
    prof_p = report_dir / "profile.json"
    if not target.exists() or not prof_p.exists():
        print(f"  跳过 {report_dir}：缺 report.html 或 profile.json")
        return False

    orig = target.read_text(encoding="utf-8")
    if "三年分部趋势" not in orig:
        print(f"  跳过 {report_dir}：报告不含「三年分部趋势」区块")
        return False

    backup = report_dir / "report_before_fix.html"
    if not backup.exists():
        shutil.copy2(target, backup)
        print(f"  已备份 → {backup.name}")

    # 1) 用修复后代码从既有 profile 重渲染（零 LLM）
    profile = json.loads(prof_p.read_text(encoding="utf-8"))
    code = report_dir.parts[-3]
    res = types.SimpleNamespace(code=code, name=profile.get("name") or code)
    d = build_overview_data(res, profile, llm_json=None, use_llm=False)
    labels = d.get("seg_trend_labels") or []
    if len(labels) < 2:
        print(f"  跳过 {report_dir}：重渲染后仍不足 2 期（{labels}）")
        return False

    fresh_p = report_dir / "_fresh.html"
    render_overview(d, str(TPL_DIR), str(fresh_p))
    fresh = fresh_p.read_text(encoding="utf-8")
    fresh_p.unlink(missing_ok=True)

    # 2) 切片
    ot, ote = _table_span(orig, *_block_span(orig))
    nt, nte = _table_span(fresh, *_block_span(fresh))
    om, ome = _mid_span(orig, *_block_span(orig))
    nm, nme = _mid_span(fresh, *_block_span(fresh))

    # 3) 从后往前替换（chart → mid → table，保证偏移不失效）
    patched = orig
    oc, nc = _chart_span(orig), _chart_span(fresh)
    if oc and nc:
        patched = patched[:oc[0]] + fresh[nc[0]:nc[1]] + patched[oc[1]:]
    s, e = _block_span(patched)
    ts, te = _table_span(patched, s, e)
    patched = patched[:ts] + fresh[nt:nte] + patched[te:]
    s, e = _block_span(patched)
    te = patched.find("</table>", s, e) + len("</table>")
    fe = patched.find('<div class="flag"', te, e)
    patched = patched[:te] + fresh[nm:nme] + patched[fe if fe > 0 else e:]

    # 4) 自检
    checks = [
        (patched.count("<h3") == orig.count("<h3"), "h3 数量变化"),
        (patched.count("<table") == orig.count("<table"), "table 数量变化"),
        (len(patched) > len(orig) * 0.9, "体积异常缩小（可能误删内容）"),
    ]
    i = patched.find("三年分部趋势")
    blk = patched[i:_block_span(patched)[1]]
    for lab in labels:
        checks.append((f">{lab}<br" in blk, f"缺列头 {lab}"))
    bad = [msg for ok, msg in checks if not ok]
    if bad:
        print(f"  失败 {report_dir}：{bad}")
        return False

    target.write_text(patched, encoding="utf-8")
    print(f"  已修复 {target.relative_to(ROOT)}  期次={labels}  大小 {len(orig)}→{len(patched)}")
    return True


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    n = 0
    for a in argv:
        d = pathlib.Path(a)
        if not d.is_absolute():
            d = ROOT / d
        if repatch(d):
            n += 1
    print(f"\n共修复 {n} 份报告。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
