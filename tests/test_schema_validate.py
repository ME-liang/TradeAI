"""校验闸门测试（不依赖网络）。

覆盖：mark_missing 标未获取、cross_check 资产负债恒等式通过/失败、
check_llm_traceable 标出无出处数字、validate_report 不抛异常。
"""

from fundamental_agent.models import AnalysisResult, ModuleResult, Metric
from fundamental_agent.schema.validate import (
    mark_missing,
    cross_check,
    check_llm_traceable,
    validate_report,
)


def _result_with(metrics):
    return AnalysisResult(
        name="测试", code="600519", modules=[ModuleResult(module="x", title="X", metrics=metrics)]
    )


def test_mark_missing_flags_none_as_unacquired():
    r = _result_with([Metric(key="k", label="某指标", value=None)])
    n = mark_missing(r)
    assert n == 1
    m = r.modules[0].metrics[0]
    assert m.note == "未获取"
    assert m.applicable is False


def test_mark_missing_keeps_present_value():
    r = _result_with([Metric(key="k", label="某指标", value=12.3, unit="亿")])
    n = mark_missing(r)
    assert n == 0
    assert r.modules[0].metrics[0].note == ""


def test_cross_check_passes_on_balanced_sheet():
    r = _result_with([
        Metric(key="a", label="总资产", value=100.0, unit="亿"),
        Metric(key="l", label="总负债", value=60.0, unit="亿"),
        Metric(key="e", label="所有者权益", value=40.0, unit="亿"),
    ])
    assert cross_check(r) == []


def test_cross_check_warns_on_imbalanced_sheet():
    r = _result_with([
        Metric(key="a", label="总资产", value=100.0, unit="亿"),
        Metric(key="l", label="总负债", value=70.0, unit="亿"),
        Metric(key="e", label="所有者权益", value=40.0, unit="亿"),
    ])
    warns = cross_check(r)
    assert warns  # 偏差 10% > 0.5%，应有告警
    assert "资产负债表失衡" in warns[0]


def test_cross_check_skips_when_data_missing():
    r = _result_with([Metric(key="a", label="总资产", value=100.0, unit="亿")])
    assert cross_check(r) == []  # 负债/权益缺失则跳过，不报错


def test_llm_traceable_flags_unbacked_number():
    r = _result_with([Metric(key="rev", label="营业收入", value=64.32, unit="亿")])
    suspicious = check_llm_traceable("本期营收 64.32 亿元，净利率高达 99.9%", r)
    assert "99.9" in suspicious           # 数据包装无此数 → 标出
    assert "64.32" not in suspicious       # 有出处 → 不标


def test_validate_report_does_not_raise():
    r = _result_with([
        Metric(key="rev", label="营业收入", value=64.32, unit="亿"),
        Metric(key="miss", label="员工人数", value=None),
    ])
    vr = validate_report(r)
    assert vr.missing_marked >= 1
    assert isinstance(vr.warnings, list)
    assert isinstance(vr.suspicious_llm_numbers, list)
