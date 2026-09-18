"""股东名册解析的纯函数回归（合成数据，不依赖 output/ 与本地 PDF）。

固化 2026-09-16 修复的解析规则，防止后续再退化：
  ① 性质字段折行容忍  ② 跨页页眉/页码剥离  ③ 跨页名字孤儿后缀回填
  ④ 性质末字缺失变体  ⑤ 版式 B（性质列在行末）  ⑥ 脏名闸门
  ⑦ 中文名折词合并（按首字符判定语种）  ⑧ 英文碎片名拦截
  ⑨ 版式 B 增减列容忍（本轮增强，修复 600941/600519/688981 只抽到一半）
  ⑩ 版式 B 双限售列 / 未知性质 / 质押状态 / 含逗号英文名（本轮增强）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fundamental_agent.data.annual_report import (  # noqa: E402
    _clean_holder_name,
    _parse_holder_rows,
    _RE_HOLDER_ROW,
    _RE_HOLDER_ROW_B,
)


# ------------------------------------------------------------------ 名称清洗
def test_clean_strips_table_header():
    got, _ = _clean_holder_name("标记或冻结情况 股份状态 数量 山东中际投 资控股有限公司")
    assert got == "山东中际投资控股有限公司"


def test_clean_strips_report_header_and_page_no():
    raw = "789 不适用 0 中际旭创股份有限公司2026 年半年度报告全文 - 47 - 人 中信证券股 份有限公司"
    got, _ = _clean_holder_name(raw)
    assert got == "中信证券股份有限公司"


def test_clean_tolerates_spaced_status_word():
    """002384 年报把「不适用」排成「不适 用」。"""
    got, _ = _clean_holder_name("不适 用 香港中央结算有限公司")
    assert got == "香港中央结算有限公司"


def test_clean_strips_leading_decimal_and_double_status():
    got, _ = _clean_holder_name(".40未知未知贵州茅台酒厂（集团）技术开发有限公司")
    assert got == "贵州茅台酒厂（集团）技术开发有限公司"


def test_clean_strips_trailing_change_column():
    """版式 B 的名称捕获会吃进「报告期内增减」列。"""
    got, _ = _clean_holder_name("中国移动香港(BVI)有限公司 -")
    assert got == "中国移动香港(BVI)有限公司"


def test_clean_returns_orphan_for_cross_page_break():
    """跨页：名字后半「中心（有限合伙）」被下一行捕获 → 作为 orphan 回填。"""
    got, orphan = _clean_holder_name(
        "中际旭创股份有限公司2025 年年度报告全文 - 81 - 中心 （有限 合伙） 中国工 商银行"
        " 股份有 限公司 －华泰 柏瑞沪 深300 交易型 开放式 指数证 券投资 基金"
    )
    assert got == "中国工商银行股份有限公司－华泰柏瑞沪深300交易型开放式指数证券投资基金"
    assert orphan == "中心（有限合伙）"


def test_clean_drops_residue_names():
    """超长残渣与只剩组织后缀的名字必须丢弃（宁缺勿编）。"""
    long_residue = (
        "不适 苏州东山精密制造股份有限公司2025 年年度报告全文 63 有限公司－睿 远成长价值混"
        " 合型证券投资 基金 用 新华人寿保险 股份有限公司 －分红－个人 分红-018L- FH002 深"
    )
    assert _clean_holder_name(long_residue)[0] == ""
    assert _clean_holder_name("不适 用 苏州东山精密制造股份有限公司2025 年年度报告全文 63 有限公司")[0] == ""


def test_clean_keeps_english_name_spacing():
    """英文名保留词间空格（保守策略，见 verify_top_holders.py 注释）。"""
    got, _ = _clean_holder_name(" Lumiza Limited")
    assert got == "Lumiza Limited"
    assert _clean_holder_name("MARK ZHAO")[0] == "MARK ZHAO"


def test_clean_merges_cjk_folds_even_with_latin_digits():
    """含拉丁字母/数字的**中文名**仍须合并折词空格（判据是首字符，不是「是否含英文」）。

    旧规则「含拉丁字母即保留空格」会把 002384/600519 的保险产品股东名留成
    「…有限公 司－传统－普通保险产品 －005L－CT001 沪」这种脏名。
    """
    got, _ = _clean_holder_name(
        "中国人寿保险股份有限公 司－传统－普通保险产品 －005L－CT001 沪"
    )
    assert got == "中国人寿保险股份有限公司－传统－普通保险产品－005L－CT001沪"


def test_clean_strips_space_folded_table_header():
    """表头折行成「股份 状态 数 量」时连续字面匹配全失效，须逐字容忍空格。"""
    got, _ = _clean_holder_name(
        "质押、标 记或冻结 情况 股东 性质 股份 状态 数 量 "
        "合肥清辉集电企 业管理合伙企业 （有限合伙）"
    )
    assert got == "合肥清辉集电企业管理合伙企业（有限合伙）"


def test_clean_drops_english_suffix_fragments():
    """PDF 逐词分页时名称捕获组只吃到末词 → 产出「Ltd.」这类脏名，必须丢弃。"""
    for frag in ("Ltd.", "Ltd", "Limited", "Inc.", "Co., Ltd.", "Corporation"):
        assert _clean_holder_name(frag)[0] == "", frag
    # 但不能误伤真实英文名
    assert _clean_holder_name("UBS AG")[0] == "UBS AG"
    assert _clean_holder_name("Rich Pacific Holdings Limited")[0] == "Rich Pacific Holdings Limited"


# ------------------------------------------------------------------ 版式 A
_SEG_A = (
    "（不含通过转融通出借股份） 股东名称 股东 性质 持股 比例 报告期末持 股数量 "
    "报告期内 增减变动 情况 持有有限 售条件的 股份数量 持有无限售 条件的股份 数量 "
    "质押、标记或冻结情况 股份状态 数量 "
    "山东中际投 资控股有限 公司 境内 非国 有法 人 10.89% 121,440,135 -641,000 0 121,440,135 质押 4,600,000 "
    "香港中央结 算有限公司 境外 法人 6.57% 73,326,563 11,482,123 0 73,326,563 不适用 0 "
)


def test_layout_a_parses_folded_nature():
    rows = _parse_holder_rows(_SEG_A, _RE_HOLDER_ROW, layout="A")
    assert [r["name"] for r in rows] == ["山东中际投资控股有限公司", "香港中央结算有限公司"]
    assert rows[0]["nature"] == "境内非国有法人"
    assert rows[0]["pct"] == 10.89
    assert rows[0]["shares"] == 121440135
    assert rows[0]["pledge_num"] == 4600000
    assert rows[1]["pledge_num"] == 0


# ------------------------------------------------------------------ 版式 B
_SEG_B = (
    "（不含通过转融通出借股份） 股东名称 （全称） 报告 期内 增减 期末持股数量 比例(%) "
    "持有有限售 条件股份数 量 质押、标记或冻结 情况 股东性 质 股份状态 数量 "
    "China Galaxy Ent erprise Limited 7,293,115,611 36.7521 0 无 境外法 人 "
    "富泰华工 业（深 圳）有限 公司 4,364,680,127 21.9949 0 无 境内非 国有法 人 "
)


def test_layout_b_nature_at_line_end():
    """沪市红筹系：性质排在人名之后、比例在持股数之后。"""
    rows = _parse_holder_rows(_SEG_B, _RE_HOLDER_ROW_B, layout="B")
    # 英文折词不合并（保守策略），故与输入保持一致
    assert [r["name"] for r in rows] == ["China Galaxy Ent erprise Limited", "富泰华工业（深圳）有限公司"]
    assert rows[0]["pct"] == 36.7521
    assert rows[0]["shares"] == 7293115611
    assert rows[0]["nature"] == "境外法人"
    assert rows[1]["nature"] == "境内非国有法人"


def test_layout_a_misses_layout_b_text():
    """A 版正则在 B 版文本上打不中——这正是「多公司名册为 0 行」的根因。"""
    assert _parse_holder_rows(_SEG_B, _RE_HOLDER_ROW, layout="A") == []


# ------------------------------------------------- 版式 B 增强（2026-09-16 第二轮）
_SEG_B_CHANGE = (
    "（不含通过转融通出借股份） 股东名称 报告期内增减 期末持股数量 比例(%) "
    "持有有限售条件股份 数量 质押、标记或冻结情况 股东性质 股份状态 数量 "
    "中国移动香港(BVI)有限公司 - 14,890,116,842 68.67 - 无 - 国有法人 "
    "香港中央结算（代理人）有限公司2 20,403,151 5,638,307,698 26.00 - 未知 - 未知 "
    "港股股东A1 未知 74,209,500 0.34 - 未知 - 未知 "
)


def test_layout_b_tolerates_change_column():
    """「报告期内增减」列有值时不得整行失配——旧正则害得中国移动只抽到 5/10 行。"""
    rows = _parse_holder_rows(_SEG_B_CHANGE, _RE_HOLDER_ROW_B, layout="B")
    assert [r["name"] for r in rows] == [
        "中国移动香港(BVI)有限公司",
        "香港中央结算（代理人）有限公司",
        "港股股东A",  # 末位脚注上标「1」被剥离
    ]
    assert [r["pct"] for r in rows] == [68.67, 26.00, 0.34]
    assert [r["shares"] for r in rows] == [14890116842, 5638307698, 74209500]


def test_layout_b_allows_unknown_nature():
    """港股股东未披露性质，「未知」必须被接受（旧枚举无此项 → 整行丢失）。"""
    rows = _parse_holder_rows(_SEG_B_CHANGE, _RE_HOLDER_ROW_B, layout="B")
    assert rows[2]["nature"] == "未知"


_SEG_B_DOUBLE_RESTRICTED = (
    "（不含通过转融通出借股份） 股东名称 （全称） 报 告 期 内 增 减 期末持股数量 "
    "比例 (%) 持有有限售条 件股份数量 包含转融通借 出股份的限售 股份数量 "
    "质押、标 记或冻结 情况 股东 性质 股份 状态 数 量 "
    "合肥清辉集电企 业管理合伙企业 （有限合伙） 0 13,043,754,927 21.67 "
    "13,043,754,927 13,043,754,927 无 0 其他 "
)


def test_layout_b_tolerates_two_restricted_columns():
    """688825 同时披露「限售数量」与「包含转融通借出股份的限售股份数量」两列。"""
    rows = _parse_holder_rows(_SEG_B_DOUBLE_RESTRICTED, _RE_HOLDER_ROW_B, layout="B")
    assert len(rows) == 1
    assert rows[0]["name"] == "合肥清辉集电企业管理合伙企业（有限合伙）"
    assert rows[0]["pct"] == 21.67
    assert rows[0]["shares"] == 13043754927
    assert rows[0]["nature"] == "其他"


_SEG_B_PLEDGED = (
    "（不含通过转融通出借股份） 股东名称 （全称） 报告期内增减 期末持股数量 比例(%) "
    "持有有限 售条件股 份数量 质押、标记或冻结情况 股东 性质 股份 状态 数量 "
    "招商银行股份有限公司－华夏上证科创板50 成份交易型开 放式指数证券投资基金 "
    "-50,080,245 64,509,310 0.81 - 质押 1,625,000 其他 "
)


def test_layout_b_allows_pledged_status():
    """质押状态为「质押」（而非 无/未知）时也要能解析出质押数量。"""
    rows = _parse_holder_rows(_SEG_B_PLEDGED, _RE_HOLDER_ROW_B, layout="B")
    assert len(rows) == 1
    assert rows[0]["pct"] == 0.81
    assert rows[0]["shares"] == 64509310
    assert rows[0]["pledge_num"] == 1625000
    assert rows[0]["nature"] == "其他"


def test_layout_b_parses_comma_in_english_name():
    """英文名含半角逗号（Robot Holding Co., Ltd.）——名称字符集缺逗号会只剩「Ltd.」。"""
    seg = (
        "（不含通过转融通出借股份） 股东名称 报告期内增减 期末持股数量 比例(%) "
        "持有有限售条 件股份 数量 质押、标记或冻结情况 股东性 质 股份 状态 数 量 "
        "Robot Holding Co., Ltd. 0 103,333,500 0.5207 0 无 境外法 人 "
    )
    rows = _parse_holder_rows(seg, _RE_HOLDER_ROW_B, layout="B")
    assert [r["name"] for r in rows] == ["Robot Holding Co., Ltd."]
    assert rows[0]["pct"] == 0.5207
