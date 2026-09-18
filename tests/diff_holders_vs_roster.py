"""报告 vs 名册 逐行对照：验证报告里的「前十大股东」是否真的等于定期报告股东名册。

用途：audit_holders.py 从**元数据**（脚注文案、profile 列特征）判断口径，
本脚本从**数据本身**判断——把报告表格与本地 PDF 名册逐行比对，直接暴露
「限售股被抹掉导致名次失真」这类只有对数据才能发现的问题。

结论口径：
  OK        名册前 3 大股东姓名都在报告前 3 行内，且第 1 大股东一致
  名次失真   名册第 1 大股东不在报告前 3 行（流通口径的典型症状：控股股东被限售股除名）
  行数不足   报告股东表行数 < 5
  无名册     本地无 PDF 或名册抽取不足 8 行，无法对照

用法：
    python tests/diff_holders_vs_roster.py             # 全量报告
    python tests/diff_holders_vs_roster.py 002384      # 只看指定代码
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PDF_DIR = ROOT / "data" / "cache" / "reports"


def _local_pdf(code: str) -> Path | None:
    """优先取本地缓存的半年报 PDF（排除「摘要」），全程离线、不碰 cninfo。

    注意：不要用 fetch_annual_report() —— 它会先去 cninfo 取 orgId，网络不可达时
    返回 0 行名册，看上去「全部一致」，属危险的假阳性。
    """
    cands = [p for p in sorted(PDF_DIR.glob(f"{code}_*.pdf")) if "摘要" not in p.name]
    semi = [p for p in cands if "半年" in p.name]
    picked = semi or cands
    return picked[0] if picked else None


def _report_holders(html: str) -> list[list[str]]:
    m = re.search(r"<h3[^>]*>\s*前十大(?:流通)?股东[^<]*</h3>(.*?)</table>", html, re.S)
    if not m:
        return []
    rows: list[list[str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S):
        tds = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", td, flags=re.S)).strip()
            for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        ]
        if tds:
            rows.append(tds)
    return rows


def _name_of(cells: list[str]) -> str:
    """取该行最像股东名称的单元格（跳过纯名次/纯数字）。"""
    for c in cells:
        if not c:
            continue
        if re.fullmatch(r"[\d,.]+%?", c):
            continue
        if re.fullmatch(r"\d{1,2}", c):
            continue
        return c
    return ""


def _norm(s: str) -> str:
    """名称归一：去空格/全角括号/连字符差异，便于跨来源比对。"""
    s = re.sub(r"[\s\u3000]+", "", s or "")
    return s.replace("（", "(").replace("）", ")").replace("－", "-").replace("—", "-")


def _nums(cells: list[str]) -> list[float]:
    """抽取单元格里的数字（去掉千分位/百分号/单位）。"""
    out: list[float] = []
    for c in cells:
        for tk in re.findall(r"\d[\d,]*(?:\.\d+)?", c or ""):
            try:
                out.append(float(tk.replace(",", "")))
            except ValueError:
                pass
    return out


def _shares_match(cells: list[str], shares: float) -> bool:
    """报告行里是否有某个数字与该股东的持股数对得上（股 或 万股，容差 0.5%）。

    持股数不在名册与流通两套口径间变化的股东（如香港中央结算），比例也可能因
    「按过期总股本折算」而失真——故必须**校验数值本身**，不能只看名次。
    """
    if not shares:
        return True
    for cand in (shares, shares / 1e4):
        for v in _nums(cells):
            if abs(v - cand) <= abs(cand) * 0.005:
                return True
    return False


def main(argv: list[str]) -> int:
    from fundamental_agent.data.annual_report import extract_top_holders

    codes = {a for a in argv if a.isdigit()}
    paths = sorted(ROOT.glob("output/reports/*/overview/*/report.html"))
    if codes:
        # 注意：股票代码在 report.html 的**祖父目录**（…/<code>/overview/<date>/report.html），
        # 用 rp.parts[-3] 会取到 "overview" 而不是代码。
        paths = [p for p in paths if p.parent.parts[-3] in codes]
    if not paths:
        print("没有找到报告")
        return 0

    bad: list[str] = []
    no_roster: list[str] = []
    print(f"{'报告':50s} {'报告行':6s} {'名册':6s} {'结论':12s} 报告第1 / 名册第1")
    print("-" * 120)
    for rp in paths:
        code = rp.parent.parts[-3]
        rel = str(rp.relative_to(ROOT))
        rows = _report_holders(rp.read_text(encoding="utf-8"))
        rep_names = [_name_of(r) for r in rows]
        pdf = _local_pdf(code)
        roster = extract_top_holders(str(pdf)) if pdf else []
        r_names = [str(h.get("name") or "") for h in roster]

        if pdf is None:
            verdict = "无本地PDF"
        elif len(roster) < 8:
            verdict = "名册抽不足"
        elif len(rows) < 5:
            verdict = "行数不足"
        elif rep_names and r_names and _norm(r_names[0]) != _norm(rep_names[0]):
            # 严格对第 1 名：股东表按名次排列，名册第一大股东必须就是表内第一行。
            # 流通口径的典型症状正是控股股东（持限售股）被挤出首位。
            verdict = "名次失真"
        else:
            # 名次对上了再核数值：同一位股东在报告与名册里的持股数必须一致，
            # 否则是口径不同（限售股被抹掉）或按过期总股本折算所致。
            by_name = {_norm(str(h.get("name") or "")): h for h in roster}
            mismatch = ""
            for cells in rows:
                nm = _norm(_name_of(cells))
                h = by_name.get(nm)
                if not h:
                    continue
                if not _shares_match(cells, float(h.get("shares") or 0)):
                    mismatch = nm
                    break
            verdict = "数值失真" if mismatch else "OK"

        head = f"{(rep_names[0] if rep_names else '—')[:24]} / {(r_names[0] if r_names else '—')[:24]}"
        print(f"{rel:50s} {len(rows):<6d} {len(roster):<6d} {verdict:12s} {head}")
        if verdict in ("名次失真", "数值失真", "行数不足"):
            bad.append(rel)
        elif verdict in ("无本地PDF", "名册抽不足"):
            no_roster.append(rel)

    print("-" * 120)
    if bad:
        print(f"✗ {len(bad)} 份报告的股东表与定期报告名册不符（数据错误，需按名册重生成）：")
        for f in bad:
            print("  -", f)
    if no_roster:
        print(f"· {len(no_roster)} 份无法对照（本地无 PDF 或名册抽取不足 8 行）：")
        for f in no_roster:
            print("  -", f)
    if not bad and len(no_roster) == len(paths):
        print("⚠ 全部报告均无法取得名册——本脚本结论**无效**（请检查本地 PDF 缓存）：")
        print("  PDF 目录：", PDF_DIR)
        return 3
    if bad:
        return 2
    print(f"✓ 可比对的报告（{len(paths) - len(no_roster)}/{len(paths)}）股东表与名册一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
