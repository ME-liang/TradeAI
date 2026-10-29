#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""K 线数据质量自检（N-15 的可观测部分）。

用途：按抽样规模拉取日/周/月 K，做与 `server.kline_quality_check` **同口径**的不变量校验，
输出「源分布 / 异常项 / 不可用项」，异常时退出码非 0 —— 便于放进定时任务或 CI，
把「上游悄悄返回错数据」从"靠用户截图发现"变成"自检告警"。

背景：2026-09-25 定位到全市场 K 线曾长期由 push2test（测试主机）供给，
它只对最近约 4 个交易日返回真实数据、更早的历史是随机占位样本。
见 docs/K线数据异常定位报告与修复方案.md。

用法：
    python frontend/check_kline.py                 # 抽样 40 只 × 日/周/月
    python frontend/check_kline.py --codes 600519,000001,300184
    python frontend/check_kline.py -n 120 --workers 3
    python frontend/check_kline.py --json          # 机器可读输出

退出码：0 = 全部通过；1 = 存在异常/不可用；2 = 抽样本身失败（无法判定）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402

PERIODS = ((101, "日K", 600), (102, "周K", 400), (103, "月K", 400))


def load_universe(limit, step):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stocks.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        snap = json.load(fh)
    codes = [it["code"] for it in snap.get("stocks", [])
             if isinstance(it, dict) and str(it.get("code", "")).isdigit()
             and len(str(it.get("code"))) == 6]
    if not codes:
        return []
    step = max(1, step or len(codes) // max(1, limit))
    return codes[::step][:limit]


def check_one(code, klt, n):
    """拉一只标的的一个周期，复算与护栏同口径的不变量。"""
    t0 = time.time()
    try:
        d = server.fetch_kline(code, klt, n)
    except Exception as exc:
        return {"code": code, "klt": klt, "status": "unavailable", "error": str(exc)[:200]}
    dates, candle, vols = d["dates"], d["candle"], d["vols"]
    q = d.get("quality") or {}
    bad = []
    if not (len(dates) == len(candle) == len(vols)):
        bad.append("长度不齐")
    for i, cd in enumerate(candle):
        o, c, lo, hi = cd
        if not (hi >= max(o, c) - 1e-9 and lo <= min(o, c) + 1e-9):
            bad.append("%s OHLC 乱序" % dates[i])
    for i in range(1, len(dates)):
        if dates[i] <= dates[i - 1]:
            bad.append("日期非递增 %s->%s" % (dates[i - 1], dates[i]))
    limit = server.KLINE_JUMP_LIMIT.get(klt, 21.5)
    s = max(0, len(dates) - server.KLINE_WINDOW)
    prev = None
    for i in range(s, len(dates)):
        o, c, lo, hi = candle[i]
        if min(o, c, lo, hi) <= 0:
            bad.append("%s 非正价" % dates[i])
        if prev:
            chg = abs((c - prev) / prev * 100)
            if chg > limit:
                bad.append("%s 单根涨跌 %.1f%%" % (dates[i], chg))
        prev = c
    return {"code": code, "klt": klt, "source": d.get("source"), "fq": q.get("fq"),
            "bars": len(dates), "xcheck": q.get("xcheck"),
            "warnings": q.get("warnings") or [],
            "status": "degraded" if q.get("warnings") else "ok",
            "anomalies": bad, "elapsed": round(time.time() - t0, 2)}


def main():
    ap = argparse.ArgumentParser(description="K 线数据质量自检")
    ap.add_argument("-n", "--limit", type=int, default=40, help="抽样个股数（默认 40）")
    ap.add_argument("--step", type=int, default=0, help="抽样步长（默认按 --limit 自动均分）")
    ap.add_argument("--codes", default="", help="指定代码，逗号分隔（优先于抽样）")
    ap.add_argument("--periods", default="101,102,103", help="周期，逗号分隔")
    ap.add_argument("--workers", type=int, default=3, help="并发（默认 3，过高易被上游限流）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or load_universe(args.limit, args.step)
    if not codes:
        print("无法确定抽样标的：--codes 未给且 frontend/data/stocks.json 不可用", file=sys.stderr)
        return 2

    want = {int(x) for x in args.periods.split(",") if x.strip()}
    jobs = [(c, klt, n) for c in codes for klt, label, n in PERIODS if klt in want]
    print("抽样 %d 只 / %d 项检查，并发 %d ..." % (len(codes), len(jobs), args.workers))

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for fut in as_completed({ex.submit(check_one, *j): j for j in jobs}):
            results.append(fut.result())
    results.sort(key=lambda r: (r["code"], r["klt"]))

    anomalies = [r for r in results if r.get("anomalies")]
    unusable = [r for r in results if r["status"] == "unavailable"]
    degraded = [r for r in results if r["status"] == "degraded"]
    sources = {}
    for r in results:
        sources[r.get("source") or "unavailable"] = sources.get(r.get("source") or "unavailable", 0) + 1

    if args.json:
        print(json.dumps({"total": len(results), "sources": sources,
                          "anomalies": anomalies, "unavailable": unusable,
                          "degraded": degraded}, ensure_ascii=False, indent=2))
    else:
        labels = {101: "日K", 102: "周K", 103: "月K"}
        for r in anomalies:
            print("[异常] %s %s 源=%s bars=%s" % (r["code"], labels.get(r["klt"], r["klt"]),
                                                 r.get("source"), r.get("bars")))
            for b in r["anomalies"][:6]:
                print("        -", b)
        for r in unusable:
            print("[不可用] %s %s %s" % (r["code"], labels.get(r["klt"], r["klt"]), r.get("error", "")))
        print()
        print("检查 %d 项，耗时 %.1fs" % (len(results), time.time() - t0))
        print("源分布: %s" % sources)
        print("异常 %d 项 / 不可用 %d 项 / 降级 %d 项" % (len(anomalies), len(unusable), len(degraded)))
        if degraded:
            sample = degraded[0]
            print("（降级示例）%s %s: %s" % (sample["code"], labels.get(sample["klt"], ""),
                                             (sample["warnings"] or [""])[0]))

    return 1 if (anomalies or unusable) else 0


if __name__ == "__main__":
    sys.exit(main())
