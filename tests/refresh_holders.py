"""离线批量刷新存量报告的「前十大股东」区块 + 页脚口径（零 LLM）。

背景：2026-09-16 之前的报告，股东表走的是东财「前十大**流通**股东」快照口径——
限售股被整个抹掉，名次与比例均失真（力源信息实控人总持股被少算 4 倍、东山精密
第一大股东整行缺席、宁德时代比例按过期总股本折算）。本脚本把存量报告刷新到正确
口径，且**不重跑 LLM**（只重渲染并外科式替换目标区块）。

口径装配（与 main.py 在 holders 唯一收口点的判定完全一致）：
  1. 名册      —— 定期报告《前十大股东》表，PyMuPDF 确定性抽取，要求**恰好 10 行**。
                 不足 10 行说明有股东整行丢失（实测 601138 年报因页眉劈开名称漏掉
                 第一、二大股东）——残缺名册比换口径更危险，故宁可不采用。
                 含「股东性质 / 质押数量」两列。
  2. 东财十大股东 —— 名册不足时回退（本地无 PDF 或抽取不全）。该接口与名册**逐条
                 同值同口径**（宁德时代 22.04%/19.33%/9.09% 三方对照一致），
                 缺「性质 / 质押数量」两列。
  3. 东财流通股东 —— 仅当专用接口整体不可用时（不应发生），表头会被强制改成
                 「前十大流通股东」，绝不冒称十大股东。

写回 profile（layers.governance）：report_holders / holders / holders_caliber，
然后复用 tests/repatch_holders.py 替换 <h3>前十大股东 子区块与页脚「数据来源」。
两者的原文件都会自动留备份：
    report_before_holders_fix.html / profile_before_holders_fix.json

用法：
    python tests/refresh_holders.py --all                 # 刷新全部非名册口径报告
    python tests/refresh_holders.py --all --dry-run       # 只看会改哪些，不落盘
    python tests/refresh_holders.py output/reports/300184/overview/20260905
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import audit_holders  # noqa: E402  复用「报告侧口径」判定，避免两处口径逻辑漂移
import repatch_holders  # noqa: E402  复用区块替换 + 页脚同步

PDF_DIR = ROOT / "data" / "cache" / "reports"
HOLDER_TITLE = "前十大股东"


def _local_pdf(code: str) -> Path | None:
    """挑本地定期报告**正文** PDF：优先半年度报告（与 main.py 的 kind="semi" 一致）。

    「摘要」不含前十大股东表，必须排除；文件名带公司全称（如 601138）也要能命中。
    """
    cands = [p for p in sorted(PDF_DIR.glob(f"{code}_*.pdf")) if "摘要" not in p.name]
    if not cands:
        cands = sorted(PDF_DIR.glob(f"{code}_*.pdf"))
    if not cands:
        return None
    semi = [p for p in cands if "半年度报告" in p.name]
    ann = [p for p in cands if "年度报告" in p.name and "半年度" not in p.name]
    for group in (semi, ann):
        if group:
            return group[0]
    return cands[0]


def _period_of(title: str) -> str:
    import re

    m = re.search(r"(\d{4})\s*年\s*(半)?年度报告", title)
    if not m:
        return title
    return f"{m.group(1)} 年中报" if m.group(2) else f"{m.group(1)} 年年报"


def build_holder_layers(code: str):
    """按 main.py 同口径装配，返回 ``(report_holders, holders, caliber, desc)``。"""
    from fundamental_agent.data.annual_report import extract_top_holders

    pdf = _local_pdf(code)
    if pdf is not None:
        rows = extract_top_holders(str(pdf))
        if len(rows) >= 10:
            title = pdf.stem.split("_", 1)[-1] if "_" in pdf.stem else pdf.stem
            period = _period_of(title)
            rh = {"holders": rows, "title": title, "period": period}
            holders = [
                {
                    "名次": i + 1,
                    "股东名称": h.get("name") or "",
                    "报告期": period,
                    "持股数": h.get("shares"),
                    "增减": "—",
                    "变动比率": "—",
                    "性质": h.get("nature") or "",
                    "质押数量": h.get("pledge_num"),
                }
                for i, h in enumerate(rows)
            ]
            return rh, holders, "名册", f"名册 {len(rows)} 行（{pdf.name}）"
        print(f"   · 名册抽取不全（{len(rows)}/10 行）→ 回退东财十大股东", file=sys.stderr)

    from fundamental_agent.data.akshare import AkShareClient

    try:
        df = AkShareClient().get_holders(code)
    except Exception as e:  # noqa: BLE001
        return None, None, "", f"东财取数异常：{e}"
    if df is None or getattr(df, "empty", True):
        return None, None, "", "名册与东财均无数据"
    recs = df.to_dict("records")
    is_free = any(str(r.get("_口径") or "") == "流通股东" for r in recs)
    caliber = "东财流通股东" if is_free else "东财十大股东"
    return None, recs, caliber, f"{caliber} {len(recs)} 行"


def refresh(d: Path, dry_run: bool = False) -> tuple[bool, str]:
    tgt, prof = d / "report.html", d / "profile.json"
    if not (tgt.exists() and prof.exists()):
        return False, "缺 report.html / profile.json"
    code = d.parts[-3]
    data = json.loads(prof.read_text(encoding="utf-8"))
    gov = data.setdefault("layers", {}).setdefault("governance", {})
    old_cal = (
        gov.get("holders_caliber")
        or ("名册" if gov.get("report_holders") else ("东财十大股东" if gov.get("holders") else "—"))
    )
    rh, holders, caliber, desc = build_holder_layers(code)
    if not caliber:
        return False, desc
    if dry_run:
        return True, f"{old_cal} → {caliber} | {desc}"

    pbackup = d / "profile_before_holders_fix.json"
    if not pbackup.exists():
        shutil.copy2(prof, pbackup)
    if rh:
        gov["report_holders"] = rh
    else:
        gov.pop("report_holders", None)
    gov["holders"] = holders
    gov["holders_caliber"] = caliber
    prof.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 复用 repatch：重渲染 → 只替换 <h3>前十大股东 子区块 + 页脚来源
    repatch_holders.main([str(d)])

    # 落盘后自检（把任何静默异常显性化）：区块存在，且表头与口径一致
    import re

    m = re.search(r"<h3[^>]*>\s*(前十大(?:流通)?股东[^<]*)</h3>", tgt.read_text(encoding="utf-8"))
    if not m:
        return False, f"{caliber} | 落盘后报告中未找到股东区块"
    expect = "前十大流通股东" if caliber == "东财流通股东" else HOLDER_TITLE
    if not m.group(1).startswith(expect):
        return False, f"{caliber} | 表头「{m.group(1)}」与口径不符"
    return True, f"{old_cal} → {caliber} | {desc} | 表头「{m.group(1)}」"


def _pending() -> list[Path]:
    """待刷新 = 报告侧脚注口径不是名册，**或** profile 侧数据源是流通口径/无区块。

    只按报告侧脚注判定会漏检：002384/20260911 的脚注文案里含「股东名册」字样，
    但 profile 侧数据源实为东财流通股东（旧·误标），故必须双侧一起看——与
    audit_holders.py 的判定口径保持一致。
    """
    out = []
    for rp in sorted(ROOT.glob("output/reports/*/overview/*/report.html")):
        title, note = audit_holders._report_side(rp.read_text(encoding="utf-8"))
        src = audit_holders._profile_side(rp.parent)
        if (
            note != "名册"
            or title == "无区块"
            or audit_holders._is_free(src)
            or src.startswith("东财流通股东")
        ):
            out.append(rp.parent)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="离线刷新报告的股东表区块与页脚口径")
    ap.add_argument("dirs", nargs="*", help="报告目录（可多个）")
    ap.add_argument("--all", action="store_true", help="刷新全部非名册口径报告")
    ap.add_argument("--dry-run", action="store_true", help="只打印将发生的口径变化，不落盘")
    args = ap.parse_args(argv)

    targets: list[Path] = []
    for raw in args.dirs:
        p = Path(raw)
        targets.append(p if p.is_absolute() else ROOT / p)
    if args.all:
        targets = _pending() + targets
    # 去重且保持稳定顺序
    seen, uniq = set(), []
    for t in targets:
        k = str(t.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    if not uniq:
        print("没有待刷新的报告。")
        return 0

    ok = fail = 0
    print(f"{'报告目录':46s} 口径变化 | 数据来源")
    print("-" * 108)
    for d in uniq:
        rel = str(d.relative_to(ROOT)) if d.is_relative_to(ROOT) else str(d)
        try:
            good, msg = refresh(d, dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001
            good, msg = False, f"异常：{type(e).__name__}: {e}"
        print(f"{rel:46s} {msg}")
        ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
    print("-" * 108)
    print(f"{'[dry-run] ' if args.dry_run else ''}成功 {ok} 份，跳过/失败 {fail} 份")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
