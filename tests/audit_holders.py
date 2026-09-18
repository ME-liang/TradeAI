"""股权治理口径审计 + 门禁：报告里的「前十大股东」到底是哪个口径、表头有没有说谎？

产出四类信号：
  ① 报告**表头**口径（前十大股东 / 前十大流通股东）
  ② 报告**脚注**口径（定期报告名册 / 东财十大股东 / 东财流通 / 未标注）
  ③ profile 里的**数据源**口径（holders_caliber 落库值 > report_holders > 行内 `_口径`）
  ④ 本地是否有 PDF 可抽名册 → 有 PDF 却没用名册 = 可优化项

退出码（便于接 CI / pre-commit）：
    0  全部自洽
    1  存在「可优化项」（本地有 PDF 但报告仍走东财口径）
    2  存在**口径不一致**（表头说前十大股东、数据实为流通口径 等）——必须修

用法：
    python tests/audit_holders.py            # 全量
    python tests/audit_holders.py 300308     # 只看指定代码
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PDF_DIR = ROOT / "data" / "cache" / "reports"

_RE_TITLE = re.compile(r"<h3[^>]*>\s*(前十大(?:流通)?股东)[^<]*</h3>")
_RE_NEXT_H = re.compile(r"<h[23][\s>]")

CALIBER_NOTE = {
    "名册": "名册",
    "东财十大股东": "东财十大",
    "东财流通股东": "东财流通",
    "未标注": "未标注",
    "无区块": "无区块",
}


def _report_side(html: str) -> tuple[str, str]:
    """返回 (表头口径文案, 脚注口径归类)。"""
    m = _RE_TITLE.search(html)
    if not m:
        return "无区块", "无区块"
    title = m.group(1)
    nxt = _RE_NEXT_H.search(html, m.end())
    block = html[m.start(): nxt.start() if nxt else len(html)]
    text = re.sub(r"<[^>]+>", " ", block)
    if "股东名册" in text:
        note = "名册"
    elif "流通口径" in text or "不含限售" in text:
        note = "东财流通股东"
    elif "东方财富前十大股东" in text:
        note = "东财十大股东"
    else:
        note = "未标注"
    return title, note


def _profile_side(d: Path) -> str:
    """从 profile 读数据源口径（与渲染层 holders_caliber_of 同一优先级）。

    最后一级用**列特征**兜底判定历史口径：``占总股本持股比例`` 只由东财
    ``stock_gdfx_top_10_em``（十大股东专用接口）产出，而 2026-09-16 之前的代码
    只有全市场流通大表一条路（该表无此列）——故「无此列且无性质」即流通口径产物。
    """
    prof = d / "profile.json"
    if not prof.exists():
        return "-"
    try:
        j = json.loads(prof.read_text(encoding="utf-8"))
    except Exception:
        return "读取失败"
    gov = ((j.get("layers") or {}).get("governance") or {})
    if gov.get("holders_caliber"):
        return str(gov["holders_caliber"])
    if gov.get("report_holders"):
        return "名册"
    rows = gov.get("holders") or []
    if not rows:
        return "-"
    keys: set = set()
    for r in rows:
        keys |= set((r or {}).keys())
    if "性质" in keys or "质押数量" in keys:
        return "名册"
    if any(str((r or {}).get("_口径") or "") == "流通股东" for r in rows):
        return "东财流通股东"
    if "占总股本持股比例" in keys:
        return "东财十大股东"
    return "东财流通股东(旧·误标)"


def _is_free(src: str) -> bool:
    return src.startswith("东财流通股东")


_ROSTER_CACHE: dict[str, int] = {}


def _roster_rows(code: str) -> int:
    """本地定期报告可抽出的名册行数（0 = 抽不出，属不可修）。

    用于把「有 PDF」细分：有 PDF 但名册抽不足 10 行（如农业银行 601288 的
    「性质+股份类别」第三种列序）时，本报告**无法**改用名册，不应计入可优化项——
    否则门禁永远停在退出码 1，信号失效。
    """
    if code in _ROSTER_CACHE:
        return _ROSTER_CACHE[code]
    n = 0
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from fundamental_agent.data.annual_report import extract_top_holders

        cands = [p for p in sorted(PDF_DIR.glob(f"{code}_*.pdf")) if "摘要" not in p.name]
        semi = [p for p in cands if "半年度报告" in p.name]
        cands = semi or cands
        if cands:
            n = len(extract_top_holders(str(cands[0])))
    except Exception:
        n = 0
    _ROSTER_CACHE[code] = n
    return n


def main(argv: list[str]) -> int:
    codes = {a for a in argv if a.isdigit()}
    paths = sorted(ROOT.glob("output/reports/*/overview/*/report.html"))
    if codes:
        paths = [p for p in paths if p.parts[-3] in codes]
    if not paths:
        print("没有找到报告（output/reports/*/overview/*/report.html）")
        return 0

    inconsistent: list[str] = []
    legacy_free: list[str] = []
    fixable: list[str] = []
    unfixable: list[str] = []
    print(f"{'报告':50s} {'表头':10s} {'脚注':11s} {'数据源':16s} {'名册':5s}")
    print("-" * 106)
    for rp in paths:
        d = rp.parent
        code = d.parts[-3]
        title, note = _report_side(rp.read_text(encoding="utf-8"))
        src = _profile_side(d)
        rows = _roster_rows(code)
        pdf_n = len(sorted(PDF_DIR.glob(f"{code}_*.pdf")))
        roster_col = f"{rows}" if rows else ("抽不足" if pdf_n else "无PDF")
        rel = str(rp.relative_to(ROOT))

        # 硬错误 A：表头声称「前十大股东」，但数据/脚注是流通口径（名不副实 + 数据错误）
        mismatched = (title == "前十大股东" and (_is_free(src) or note == "东财流通股东")) or (
            title == "前十大流通股东" and note == "名册"
        )
        # 硬错误 B：有股东表却完全没有口径脚注（读者无从判断口径）
        unlabeled = title != "无区块" and note == "未标注"
        if mismatched or unlabeled:
            inconsistent.append(rel)
        # 历史流通口径产物：数据本身缺限售股（名次/比例都会错），与「表头说谎」分开统计
        if _is_free(src) and note != "名册":
            legacy_free.append(rel)
        if note != "名册":
            if rows >= 10:
                fixable.append(rel)
            else:
                unfixable.append(rel)

        mark = "  ✗" if (mismatched or unlabeled) else ""
        print(
            f"{rel:50s} {title:10s} {CALIBER_NOTE.get(note, note):11s} "
            f"{src:16s} {roster_col:5s}{mark}"
        )

    print("-" * 106)
    if legacy_free:
        print(f"✗ {len(legacy_free)} 份报告为**历史流通口径产物**（限售股被抹掉，名次/比例均失真，必须重生成）：")
        for f in legacy_free:
            print("  -", f)
    if inconsistent:
        print(f"✗ {len(inconsistent)} 份报告**表头与口径不一致或未标注口径**：")
        for f in inconsistent:
            print("  -", f)
    if fixable:
        print(f"· {len(fixable)} 份可改用定期报告名册（本地 PDF 名册可抽满 10 行，属优化项）：")
        for f in fixable:
            print("  -", f)
    if unfixable:
        print(f"· {len(unfixable)} 份仍走东财十大股东（本地无名册可抽：无 PDF 或列序未支持）——")
        print("  该口径与名册**逐条同值**（总持股含限售），仅缺「股东性质/质押数量」两列，属可接受降级：")
        for f in unfixable:
            print("  -", f)
    if inconsistent or legacy_free:
        print("修复：python tests/refresh_holders.py --all")
        return 2
    if fixable:
        print("修复：python tests/refresh_holders.py --all")
        return 1
    print("✓ 全部报告的股权治理口径、表头、脚注三者自洽。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
