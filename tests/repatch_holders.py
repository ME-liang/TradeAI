"""外科式刷新报告里的「前十大股东」子区块（零 LLM、零网络）。

背景：名册口径与东财口径修正后，需要把存量报告更新到新口径，但**不能重跑 LLM**
（额度紧张），也不能动已生成的 AI 叙述段。做法：用既有 profile 零 LLM 重渲染，
只摘出「前十大股东」子区块（<h4>前十大股东</h4> → 下一个 <h4>）替换进目标报告，
其余内容（h3 标题、分红与股东回报、治理风险扫描、各 AI 叙述段）一字不动。

用法：python tests/repatch_holders.py output/reports/300308/overview/20260916
"""
from __future__ import annotations

import json
import re
import shutil
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
_OPEN = "<h4"

# 页脚「数据来源」整段为纯文本（`</strong>：` 与 `<br` 之间，不含标签）。
_RE_SOURCES = re.compile(r"(<strong[^>]*>数据来源</strong>：)([^<]*)(<br)")

# 「前十大股东」区块标题：名册/东财十大股东渲染为「前十大股东（期次）」，
# 流通兜底渲染为「前十大流通股东（期次）」，两种都要能定位。
_HOLDER_TITLE_PAT = r"前十大(?:流通)?股东"


def _patch_sources(old: str, new_html: str) -> str:
    """同步刷新页脚「数据来源」文本。

    股东口径从东财切到定期报告名册后，页脚若仍写「东方财富…前十大股东…
    按当前总股本折算」就与区块脚注自相矛盾，故一并替换。
    """
    m = _RE_SOURCES.search(new_html)
    if not m:
        return old
    return _RE_SOURCES.sub(
        lambda mm: mm.group(1) + m.group(2) + mm.group(3), old, count=1
    )


def _block(html: str, title_pat: str) -> tuple[int, int]:
    """返回标题匹配正则 `title_pat` 的 <h3> 子区块 [start, end)。

    必须精确匹配**标题节点**——`title` 也会出现在数据来源脚注里
    （「数据来源：东方财富前十大股东…」），用裸文本查找会命中脚注。
    """
    # 标题可能带期次后缀（新渲染为「前十大股东（2026 年中报）」），
    # 或口径前缀（流通兜底渲染为「前十大流通股东」），故用正则匹配而非全等。
    m = re.search(r"<h3[^>]*>\s*" + title_pat + r"[^<]*</h3>", html)
    if not m:
        raise SystemExit(f"未找到 <h3> 标题节点：{title_pat}")
    start = m.start()
    end = -1
    for tag in ("<h3", "<h2"):
        nxt = html.find(tag, start + len(_OPEN))
        if nxt > 0 and (end < 0 or nxt < end):
            end = nxt
    if end < 0:
        raise SystemExit(f"无法确定区块终点：{title}")
    return start, end


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit(__doc__)
    for raw in argv:
        d = Path(raw)
        if not d.is_absolute():
            d = ROOT / d
        tgt, prof = d / "report.html", d / "profile.json"
        if not (tgt.exists() and prof.exists()):
            print(f"跳过 {d}（缺 report.html / profile.json）")
            continue
        code = d.parts[-3]
        data = json.loads(prof.read_text(encoding="utf-8"))
        res = types.SimpleNamespace(code=code, name=data.get("name") or code)
        dview = build_overview_data(res, data, None, use_llm=False)
        fresh = d / "_fresh.html"
        render_overview(dview, str(TPL_DIR), str(fresh))
        new_html = fresh.read_text(encoding="utf-8")

        old = tgt.read_text(encoding="utf-8")
        b0, b1 = _block(new_html, _HOLDER_TITLE_PAT)
        block_new = new_html[b0:b1]
        try:
            a0, a1 = _block(old, _HOLDER_TITLE_PAT)
        except SystemExit:
            # 原报告**根本没有渲染股东表**（取数失败时模板 `{% if shareholders %}` 整块不输出，
            # 实测 688825/20260906）。此时按模板位置插入：紧随 03 卡片引言、
            # 位于「分红与股东回报」之前。插入会新增 1 个 <h3> 与 1 个 <table>，自检相应放宽。
            m = re.search(r"<h3[^>]*>\s*分红与股东回报", old)
            if not m:
                raise SystemExit(f"{d}：既无股东区块、也无「分红与股东回报」锚点，无法插入")
            patched = old[: m.start()] + block_new + old[m.start():]
            assert patched.count("<h3") == old.count("<h3") + 1, "h3 应恰好 +1"
            assert patched.count("<h2") == old.count("<h2"), "h2 数量变化"
            assert patched.count("<table") == old.count("<table") + 1, "table 应恰好 +1"
            note = f"插入新区块（原报告未渲染股东表）"
        else:
            block_old = old[a0:a1]
            patched = old[:a0] + block_new + old[a1:]
            # 自检：其余结构不得变化
            for tag in ("<h3", "<h2"):
                assert patched.count(tag) == old.count(tag), f"{tag} 数量变化"
            assert patched.count("<table") == old.count("<table"), "table 数量变化"
            note = f"区块 {len(block_old)} → {len(block_new)} 字符"
        # 页脚来源脚注同步（股东口径已变，页脚不得自相矛盾）
        patched = _patch_sources(patched, new_html)
        backup = d / "report_before_holders_fix.html"
        if not backup.exists():
            shutil.copy2(tgt, backup)
        tgt.write_text(patched, encoding="utf-8")
        fresh.unlink()
        print(f"{d}")
        print(f"   {note} | 整体 {len(old)} → {len(patched)}")
        print(f"   备份 {backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
