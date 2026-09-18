"""前十大股东名册抽取离线验证（零 LLM、零网络，仅用本地已缓存 PDF）。

覆盖 2026-09-16 修复的三个缺陷：
  ① 股东性质字段被 PDF 折行拆开（「境内 非国 有法 人」）→ 用逐字 \\s* 容忍；
  ② 跨页页眉把名字切成两半（「苏州云昌锦企业管理」＋「中心（有限合伙）」）→ 孤儿回填；
  ③ 性质末字被排到下一页首（「境内 自然」…「人」）→ 缺尾字变体补全；
另含版式 B（沪市红筹系：性质列在行末）与东财接口口径纠错的端到端断言。

用法：python tests/verify_top_holders.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fundamental_agent.data.annual_report import (  # noqa: E402
    _clean_holder_name,
    _extract_top_holders_impl,
)

REPORTS = ROOT / "data" / "cache" / "reports"
SEMI = REPORTS / "300308_2026年半年度报告.pdf"
ANN = REPORTS / "300308_2025年年度报告.pdf"

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


# ---------------------------------------------------------------- 1) 清洗单元
_CLEAN_CASES = [
    # (原始捕获, 期望名称)  —— 噪声形态取自真实半年报/年报
    ("标记或冻结情况 股份状态 数量 山东中际投 资控股有限公司", "山东中际投资控股有限公司"),
    (" 香港中央结 算有限公司", "香港中央结算有限公司"),
    ("789 不适用 0 中际旭创股份有限公司2026 年半年度报告全文 - 47 - 人 中信证券股 份有限公司",
     "中信证券股份有限公司"),
    ("不适 用 香港中央结算有限公司", "香港中央结算有限公司"),
    (".40未知未知贵州茅台酒厂（集团）技术开发有限公司", "贵州茅台酒厂（集团）技术开发有限公司"),
    ("中际旭创股份有限公司2025 年年度报告全文 - 81 - 中心 （有限 合伙） 中国工 商银行 股份有 限公司 －华泰 柏瑞沪 深300 交易型 开放式 指数证 券投资 基金",
     "中国工商银行股份有限公司－华泰柏瑞沪深300交易型开放式指数证券投资基金"),
    ("中国移动香港(BVI)有限公司 -", "中国移动香港(BVI)有限公司"),
    # 已知限制：英文名的 PDF 折词不合并（中文名会合并）。若强行合并会破坏
    # "Lumiza Limited" / "MARK ZHAO" 这类正常词间空格，故按保守策略保留原样。
    ("Ambit Microsy stems (Cayman) Ltd.", "Ambit Microsy stems (Cayman) Ltd."),
]
for raw, want in _CLEAN_CASES:
    got, _ = _clean_holder_name(raw)
    check(got == want, f"清洗 {raw[:28]!r} → {got!r}，期望 {want!r}")

# 跨页断裂混入多行残渣（超长 / 含报告页眉痕迹）必须被丢弃，不得进入报告
_BAD_RAW = (
    "不适 苏州东山精密制造股份有限公司2025 年年度报告全文 63 有限公司－睿 远成长价值混"
    " 合型证券投资 基金 用 新华人寿保险 股份有限公司 －分红－个人 分红-018L- FH002 深"
)
for _bad in (_BAD_RAW, "不适 用 苏州东山精密制造股份有限公司2025 年年度报告全文 63 有限公司"):
    got, _ = _clean_holder_name(_bad)
    check(got == "", f"脏名未被丢弃：{_bad[:30]!r} → {got!r}")

# ---------------------------------------------------------- 2) 300308 名册抽取
_EXPECT_300308_SEMI = [
    ("山东中际投资控股有限公司", "境内非国有法人", 10.89, 121440135, 4600000),
    ("香港中央结算有限公司", "境外法人", 6.57, 73326563, 0),
    ("王伟修", "境内自然人", 6.25, 69731451, 1350000),
    ("苏州益兴福企业管理中心（有限合伙）", "境内非国有法人", 4.18, 46627154, 8054000),
    ("苏州云昌锦企业管理中心（有限合伙）", "境内非国有法人", 2.14, 23858278, 3530000),
    ("Lumiza Limited", "境外法人", 1.38, 15415548, 0),
    ("苏州福睿晖企业管理中心（有限合伙）", "境内非国有法人", 0.73, 8156444, 0),
    ("中国工商银行股份有限公司－国泰中证全指通信设备交易型开放式指数证券投资基金",
     "其他", 0.61, 6789203, 0),
    ("章建平", "境内自然人", 0.53, 5934789, 0),
    ("中信证券股份有限公司", "国有法人", 0.49, 5474693, 0),
]

if not SEMI.exists():
    print(f"跳过 300308 名册断言（缺少本地 PDF：{SEMI.name}）")
else:
    rows = _extract_top_holders_impl(str(SEMI))
    check(len(rows) == 10, f"300308 半年报名册应 10 行，实得 {len(rows)} 行")
    for i, (nm, nat, pct, sh, pl) in enumerate(_EXPECT_300308_SEMI):
        if i >= len(rows):
            failures.append(f"300308 半年报缺第 {i + 1} 名：{nm}")
            continue
        r = rows[i]
        check(r["name"] == nm, f"第{i + 1}名 名称 {r['name']!r} ≠ {nm!r}")
        check(r["nature"] == nat, f"第{i + 1}名 性质 {r['nature']!r} ≠ {nat!r}")
        check(abs(r["pct"] - pct) < 0.005, f"第{i + 1}名 比例 {r['pct']} ≠ {pct}")
        check(r["shares"] == sh, f"第{i + 1}名 持股数 {r['shares']} ≠ {sh}")
        check(r["pledge_num"] == pl, f"第{i + 1}名 质押数 {r['pledge_num']} ≠ {pl}")
    # 名次必须单调、不重复
    check(len({r["name"] for r in rows}) == len(rows), "名册出现重复股东名")

if ANN.exists():
    rows_a = _extract_top_holders_impl(str(ANN))
    check(len(rows_a) == 10, f"300308 年报名册应 10 行，实得 {len(rows_a)}")
    check(rows_a[0]["name"] == "山东中际投资控股有限公司", "300308 年报首名不符")
    # 跨页断裂回填：苏州云昌锦的名字后缀来自下一页首，须拼接完整
    check(
        any(r["name"] == "苏州云昌锦企业管理中心（有限合伙）" for r in rows_a),
        "300308 年报跨页名字未回填（苏州云昌锦…）",
    )

# --------------------------------- 2b) 多公司名册抽取（2026-09-16 第二轮增强）
# 版式 B 的「报告期内增减」列原先被忽略，导致该列有值的公司**半数股东整行丢失**：
#   600941 中国移动 5/10、600519 贵州茅台 4/10、688981 中芯国际 0/10、688825 0/10。
# 这里逐家钉死「必须满 10 行 + 第 1 名名称/比例/持股数」，任何回退都会被立刻捕获。
# (PDF 文件名, 期望首名, 期望性质, 期望比例%, 期望持股数)
_MULTI_COMPANY = [
    ("600941_中国移动_2026年半年度报告.pdf", "中国移动香港(BVI)有限公司", "国有法人", 68.67, 14890116842),
    ("600519_贵州茅台2026年半年度报告.pdf", "中国贵州茅台酒厂（集团）有限责任公司", "国有法人", 54.5, 681282935),
    ("688981_中芯国际2026年半年度报告.pdf", "HKSCC NOMINEES LIMITED", "未知", 54.82, 4692939101),
    ("688825_2026年半年度报告.pdf", "合肥清辉集电企业管理合伙企业（有限合伙）", "其他", 21.67, 13043754927),
    ("601138_富士康工业互联网股份有限公司2026年半年度报告.pdf", "China Galaxy Ent erprise Limited", "境外法人", 36.7521, 7293115611),
    ("002384_2026年半年度报告.pdf", "袁永刚", "境内自然人", 16.53, 302781254),
    ("300184_2026年半年度报告.pdf", "MARK ZHAO", "境外自然人", 11.1, 127564160),
    ("300750_2026年半年度报告.pdf", "厦门瑞庭投资有限公司", "境内一般法人", 22.04, 1019704949),
]
for fname, nm, nat, pct, sh in _MULTI_COMPANY:
    p = REPORTS / fname
    if not p.exists():
        print(f"跳过 {fname}（无本地 PDF）")
        continue
    rows = _extract_top_holders_impl(str(p))
    check(len(rows) == 10, f"{fname} 名册应 10 行，实得 {len(rows)} 行（增减列容忍回退？）")
    if not rows:
        continue
    check(rows[0]["name"] == nm, f"{fname} 首名 {rows[0]['name']!r} ≠ {nm!r}")
    check(rows[0]["nature"] == nat, f"{fname} 首名性质 {rows[0]['nature']!r} ≠ {nat!r}")
    check(abs(rows[0]["pct"] - pct) < 0.005, f"{fname} 首名比例 {rows[0]['pct']} ≠ {pct}")
    check(rows[0]["shares"] == sh, f"{fname} 首名持股数 {rows[0]['shares']} ≠ {sh}")
    check(len({r["name"] for r in rows}) == len(rows), f"{fname} 名册出现重复股东名")
    check(
        all(rows[i]["pct"] >= rows[i + 1]["pct"] for i in range(len(rows) - 1)),
        f"{fname} 名册比例非降序（可能抽入了非本表行）",
    )
    check(all(len(r["name"]) >= 2 for r in rows), f"{fname} 名册存在空/碎片股东名")

# 601138 第 10 名原被 PDF 分页切成「Ltd.」碎片——含逗号英文名必须完整
_p601138 = REPORTS / "601138_富士康工业互联网股份有限公司2026年半年度报告.pdf"
if _p601138.exists():
    _r = _extract_top_holders_impl(str(_p601138))
    check(
        any(r["name"] == "Robot Holding Co., Ltd." for r in _r),
        "601138 半年报第 10 名未还原为「Robot Holding Co., Ltd.」",
    )
    check(not any(r["name"].strip().lower() in {"ltd.", "ltd", "limited"} for r in _r),
          "601138 名册残留英文碎片名")

# ------------------------------------------------- 3) 东财接口口径（可选联网）
try:
    from fundamental_agent.data.akshare import AkShareClient  # noqa: E402

    df = AkShareClient().get_holders("300308")
    if df is not None and not df.empty:
        cols = set(df.columns)
        check("占总股本持股比例" in cols,
              "get_holders 未返回「占总股本持股比例」→ 仍在用流通股东大表兜底")
        if "股东名称" in cols:
            check("山东中际投资控股有限公司" in set(df["股东名称"]),
                  "get_holders 首名不是控股股东，口径可疑")
    else:
        print("跳过东财接口断言（返回空，可能离线）")
except Exception as e:  # noqa: BLE001
    print(f"跳过东财接口断言（{type(e).__name__}: {e}）")

# ---------------------------------------------------------------------- 汇总
print()
if failures:
    print(f"FAILED（{len(failures)} 项）：")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
