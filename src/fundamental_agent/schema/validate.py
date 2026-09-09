"""校验闸门：让「缺失即标未获取、数字可溯源、LLM 不编造」成为硬约束。

三个检查：
1. mark_missing：value 为 None 的指标 → applicable=False 且 note="未获取"，绝不留空让渲染层填空。
2. cross_check：资产负债表恒等式（资产≈负债+权益，容差 0.5%）、PE×净利≈市值等
   内部一致性校验，返回告警列表（不抛异常，数据缺失则跳过该项）。
3. check_llm_traceable：扫描 llm_summary 中的数字，回溯不到结构化数据包的标为可疑，
   返回可疑数字列表，供人工复核或渲染时标红。

validate_report() 汇总三者，供 pipeline 在引擎跑完后调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import AnalysisResult

_MISSING_NOTE = "未获取"

# 资产/负债/权益 关键词（标签可能含其一）
_ASSET_KW = ("总资产",)
_LIAB_KW = ("总负债", "负债合计")
_EQUITY_KW = ("所有者权益", "净资产", "归属于母公司所有者权益")


@dataclass
class ValidateResult:
    missing_marked: int = 0
    warnings: list[str] = field(default_factory=list)
    suspicious_llm_numbers: list[str] = field(default_factory=list)


def _norm_num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _find_metric(result: AnalysisResult, *keywords: str):
    """在全部模块指标中按标签关键词查找第一个匹配且 value 非 None 的指标。"""
    for m in result.modules:
        for metric in m.metrics:
            if metric.value is None:
                continue
            label = metric.label or ""
            if any(kw in label for kw in keywords):
                return metric
    return None


def mark_missing(result: AnalysisResult) -> int:
    """把 value 为 None 的指标显式标为「未获取」。返回标记数量。"""
    n = 0
    for m in result.modules:
        for metric in m.metrics:
            if metric.value is None:
                if metric.applicable and not metric.note:
                    metric.applicable = False
                    metric.note = _MISSING_NOTE
                    n += 1
                elif not metric.note:
                    metric.note = _MISSING_NOTE
    return n


def cross_check(result: AnalysisResult) -> list[str]:
    """内部一致性校验，返回告警列表（数据缺失则跳过该项，不报错）。"""
    warnings: list[str] = []

    assets = _find_metric(result, *_ASSET_KW)
    liab = _find_metric(result, *_LIAB_KW)
    equity = _find_metric(result, *_EQUITY_KW)
    if assets and liab and equity:
        a, l, e = (_norm_num(assets.value), _norm_num(liab.value), _norm_num(equity.value))
        if None not in (a, l, e) and a:
            diff = abs(a - (l + e)) / abs(a)
            if diff > 0.005:
                    warnings.append(
                        f"资产负债表失衡：资产 {assets.value}{assets.unit} ≠ 负债+权益 {l+e:.2f}"
                        f"（偏差 {diff*100:.2f}%，阈值 0.5%）"
                    )

    pe = _find_metric(result, "市盈率", "PE")
    profit = _find_metric(result, "归母净利润", "净利润")
    mv = _find_metric(result, "总市值", "市值")
    if pe and profit and mv:
        pe_v, p_v, mv_v = (_norm_num(pe.value), _norm_num(profit.value), _norm_num(mv.value))
        if None not in (pe_v, p_v, mv_v) and pe_v and p_v:
            implied_mv = pe_v * p_v
            if mv_v and abs(implied_mv - mv_v) / abs(mv_v) > 0.10:
                warnings.append(
                    f"估值自洽异常：PE×净利≈{implied_mv:.2f} 亿元，与总市值 {mv.value}{mv.unit} 偏差超 10%"
                )
    return warnings


def _known_numbers(result: AnalysisResult) -> set[float]:
    """汇集数据包中所有数值（指标值 + 画像值），归一化为浮点集合。"""
    known: set[float] = set()
    for m in result.modules:
        for metric in m.metrics:
            v = _norm_num(metric.value)
            if v is not None:
                known.add(round(v, 2))
                known.add(round(v * 100, 2))
                known.add(round(v / 100, 2))
    for val in result.profile.values():
        v = _norm_num(val)
        if v is not None:
            known.add(round(v, 2))
    return known


def check_llm_traceable(summary: str, result: AnalysisResult) -> list[str]:
    """扫描 summary 中的数字，标出回溯不到数据包的可疑数字。"""
    if not summary:
        return []
    known = _known_numbers(result)
    # 提取数字（含小数；排除年份与日期 4 位独立数字）
    tokens = re.findall(r"(?<!\d)(\d{1,3}(?:\.\d+)?|\d{4,}(?:\.\d+)?)(?!\d)", summary)
    suspicious: list[str] = []
    for t in tokens:
        f = _norm_num(t)
        if f is None:
            continue
        if 1900 <= f <= 2100:  # 年份/日期，跳过
            continue
        backed = any(abs(f - k) < 0.01 or abs(f - k * 100) < 0.01 or abs(f - k / 100) < 0.01 for k in known)
        if not backed:
            suspicious.append(t)
    return suspicious


def validate_report(result: AnalysisResult) -> ValidateResult:
    """汇总三类校验。绝不抛异常，保证不阻塞渲染。"""
    missing = mark_missing(result)
    warnings = cross_check(result)
    suspicious = check_llm_traceable(result.llm_summary, result)
    return ValidateResult(
        missing_marked=missing, warnings=warnings, suspicious_llm_numbers=suspicious
    )
