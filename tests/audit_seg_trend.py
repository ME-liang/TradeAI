# -*- coding: utf-8 -*-
"""「三年分部趋势」体检工具：对 output/ 下所有已生成 profile 做期次与合计体检。

用法：
    python tests/audit_seg_trend.py            # 体检全部
    python tests/audit_seg_trend.py 002384 300308

体检项（全部用当前代码 + 真实缓存，零 LLM / 零网络 / 只读）：
  1. 窗口期次是否齐备 —— 窗口 = 最近 3 期年报 + 最近 1 期半年报。
     任何一期有原始数据却被剔除 → 记为「缺期」，这是「三年趋势只剩两年」类
     问题（300308 / 002384 / 688981 都栽在这）的直接探针。
  2. 各期合计是否等于该期真实主营收入 —— 防「归并漏段」与「其中:子行重复计数」。
     基线口径：按产品分类收入求和，且**剔除「其中:XXX」子行**（与父行同值）。

退出码：0 = 全部正常；1 = 存在异常（可直接接进 CI）。
"""
from __future__ import annotations

import glob
import json
import pathlib
import re
import sys
import types
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fundamental_agent.report_types.overview import build_overview_data  # noqa: E402

# 与 overview.py._is_subrow 保持一致
_RE_SUBROW = re.compile(r"^其中[:：]")


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


_RE_RESIDUAL = re.compile(r"^(其他|其它|分部间抵销|内部抵销|抵销|平衡项目|合计|总计)")


def raw_profile(prof):
    """→ {日期: (收入合计, 行数, 产品名列表)}。

    收入合计剔除两类「非独立科目」，否则会重复计数：
      ① 「其中:XXX」子行 —— 与父行同值（600941 2023/2024）；
      ② 同值残差行 —— 同一期内两条残差行金额完全相同，是重分类/并列披露的
         复制，只应计一次（600941 2025：其他(补充) 与 其他主营业务 同为 1546.57 亿）。
    """
    segs = ((prof.get("layers", {}) or {}).get("business", {}) or {}).get("segments") or []
    per_date: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for r in segs:
        if str(r.get("分类类型", "")) != "按产品分类":
            continue
        d = str(r.get("报告日期", ""))[:10]
        if len(d) < 10:
            continue
        nm = str(r.get("主营构成", ""))
        if _RE_SUBROW.match("".join(nm.split())):
            continue  # ① 子行与父行同值
        per_date[d].append((_f(r.get("主营收入")) or 0.0, nm))

    agg, cnt, names = defaultdict(float), defaultdict(int), defaultdict(list)
    for d, rows in per_date.items():
        # ② 同值残差行只计一次
        seen_residual_vals: set[float] = set()
        for rev, nm in rows:
            if _RE_RESIDUAL.match("".join(nm.split())) and rev:
                if rev in seen_residual_vals:
                    continue
                seen_residual_vals.add(rev)
            agg[d] += rev
            cnt[d] += 1
            names[d].append(nm)
    return agg, cnt, names


def _kind(d):
    return "h1" if d.endswith("06-30") else ("annual" if d.endswith("12-31") else "")


def _label(d):
    return f"{d[:4]}H1" if d.endswith("06-30") else d[:4]


def audit_one(path: pathlib.Path, rel: str) -> list[str]:
    """返回该 profile 的问题列表（空 = 正常）。"""
    try:
        prof = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [f"{rel}: profile 解析失败 {e}"]

    agg, cnt, names = raw_profile(prof)
    if not agg:
        return []  # 无分部数据（如银行），跳过

    code = path.parts[-4]
    try:
        res = types.SimpleNamespace(code=code, name=prof.get("name") or code)
        d = build_overview_data(res, prof, None, use_llm=False)
    except Exception as e:  # noqa: BLE001
        return [f"{rel}: 装配异常 {type(e).__name__}: {e}"]

    annual = sorted([k for k in agg if _kind(k) == "annual"])[-3:]
    h1s = sorted([k for k in agg if _kind(k) == "h1"])
    target = list(annual) + ([h1s[-1]] if h1s and h1s[-1] not in annual else [])
    labels = [str(x) for x in (d.get("seg_trend_labels") or [])]
    got = {x[:4] + "-06-30" if x.endswith("H1") else x + "-12-31" for x in labels}

    issues: list[str] = []
    for t in target:
        if t not in got:
            issues.append(
                f"{rel}: 缺期 {t}（原始 {cnt[t]} 项 / {agg[t]/1e8:.2f} 亿；"
                f"产品名 {names[t]}）"
            )

    if len(target) >= 2 and len(labels) < 2:
        issues.append(f"{rel}: 采纳期次不足 2 期（{labels}），趋势块会被整块隐藏")

    total = next((r for r in (d.get("seg_trend") or []) if str(r.get("name")) == "合计"), None)
    if total:
        for i, lab in enumerate(labels):
            key = lab[:4] + ("-06-30" if lab.endswith("H1") else "-12-31")
            cells = total.get("cells") or []
            if key not in agg or i >= len(cells):
                continue
            m = re.match(r"([\d.]+)\s*亿", str(cells[i]))
            if not m:
                issues.append(f"{rel}: 合计 {lab} 单元格无法解析：{cells[i]}")
                continue
            got_v, exp_v = float(m.group(1)), agg[key] / 1e8
            if abs(got_v - exp_v) > max(0.5, exp_v * 0.02):
                issues.append(
                    f"{rel}: 合计不符 {lab} — 表内 {got_v:.2f} 亿 vs 原始 {exp_v:.2f} 亿"
                )
    return issues


def audit_report_html(html_path: pathlib.Path, rel: str, profile_path: pathlib.Path) -> list[str]:
    """检查渲染产物里「三年分部趋势」表的列头是否与窗口期次一致。"""
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    if "三年分部趋势" not in html:
        return []
    try:
        prof = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    agg, _, _ = raw_profile(prof)
    if not agg:
        return []
    annual = sorted([k for k in agg if _kind(k) == "annual"])[-3:]
    h1s = sorted([k for k in agg if _kind(k) == "h1"])
    want = [x[:4] for x in annual] + ([h1s[-1][:4] + "H1"] if h1s and h1s[-1] not in annual else [])

    i = html.find("三年分部趋势")
    ends = [x for x in (html.find("<h3", i), html.find("<h2", i)) if x > 0]
    blk = html[i:min(ends)] if ends else html[i:]
    got: list[str] = []
    for t in re.findall(r"<th[^>]*>(.*?)</th>", blk, re.S):
        t = re.sub(r"<br[^>]*>", "\n", t)
        t = re.sub(r"<[^>]+>", "", t).strip()
        m = re.match(r"(\d{4}(?:H1)?)", t)
        if m:
            got.append(m.group(1))
    missing = [w for w in want if w not in got]
    if len(want) >= 2 and len(got) < 2:
        return [f"{rel}: 趋势表期次不足 2 列（{got}），区块本应隐藏而非残缺渲染"]
    if missing:
        return [f"{rel}: 趋势表缺列 {'/'.join(missing)}（实际 {got}，应为 {want}）"]
    return []


def main(argv: list[str]) -> int:
    codes = {a for a in argv if a.isdigit()}
    paths = sorted(glob.glob(str(ROOT / "output" / "reports" / "*" / "overview" / "*" / "profile.json")))
    if codes:
        paths = [p for p in paths if pathlib.Path(p).parts[-4] in codes]

    all_issues: list[str] = []
    checked = 0
    for p in paths:
        path = pathlib.Path(p)
        rel = str(path.relative_to(ROOT))
        prof_codes = path.parts
        agg, _, _ = raw_profile(json.loads(path.read_text(encoding="utf-8")))
        if not agg:
            print(f"  --  {rel:56s} 无分部数据(跳过)")
            continue
        checked += 1
        issues = audit_one(path, rel)
        if issues:
            print(f"  !!  {rel:56s} {len(issues)} 项异常")
            for it in issues:
                print(f"        - {it}")
        else:
            print(f"  OK  {rel:56s}")
        all_issues.extend(issues)

    # —— 渲染层：规范报告 report.html 的列头是否齐备 ——
    # 只把规范报告当硬性检查；report_v2/_before_fix/_direct 等历史版本仅提示，
    # 否则每次体检都会因陈年备份失败而永远红。
    print("-" * 78)
    r_issues: list[str] = []
    r_checked = 0
    for p in sorted(glob.glob(str(ROOT / "output" / "reports" / "*" / "overview" / "*" / "report*.html"))):
        hp = pathlib.Path(p)
        if codes and hp.parts[-4] not in codes:
            continue
        rel = str(hp.relative_to(ROOT))
        has_block = "三年分部趋势" in hp.read_text(encoding="utf-8", errors="ignore")
        is_canonical = hp.name == "report.html"
        if is_canonical and has_block:
            r_checked += 1
        issues = audit_report_html(hp, rel, hp.parent / "profile.json")
        if not issues:
            continue
        if is_canonical:
            print(f"  !!  {rel:56s} {len(issues)} 项异常")
            for it in issues:
                print(f"        - {it}")
            r_issues.extend(issues)
        else:
            print(f"  ..  {rel:56s} 历史版本，仅提示：{issues[0]}")

    print("-" * 78)
    if all_issues or r_issues:
        print(f"体检 {checked} 份 profile / {r_checked} 份规范报告，发现 "
              f"{len(all_issues) + len(r_issues)} 项异常：")
        for it in all_issues + r_issues:
            print("  -", it)
        return 1
    print(f"体检 {checked} 份 profile / {r_checked} 份规范报告，"
          f"全部正常（窗口期次齐备、合计与原始一致、报告列头完整）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
