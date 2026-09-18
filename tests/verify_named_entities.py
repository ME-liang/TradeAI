# -*- coding: utf-8 -*-
"""离线验证：上游/下游/深度合作网络「具体公司列举」与公开信息补位（零 LLM 消耗）。

覆盖四层：
  A. 泛称判定与名单拆分（is_generic_entity / split_named_entities）
  B. 抽取层：动词前置句式具名抽取 + 泛称 named=False 标记（真实半年报 PDF）
  C. 公开信息补位护栏 _public_entity_check（自创领域 / 精确数字 / 泛称名 必须被拒）
  D. 装配+渲染：补位注入（monkeypatch LLM 通道）→ 上游「代表企业」列、
     下游「公开信息·待核实」行、合作网络具名行与标题家数重算
"""
from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import fitz  # noqa: E402

import fundamental_agent.report_types.overview as OV  # noqa: E402
from fundamental_agent.data.annual_report import (  # noqa: E402
    _extract_partner_sents,
    _find_cached_report,
    is_generic_entity,
    split_named_entities,
)
from fundamental_agent.report_types.overview import (  # noqa: E402
    _public_entity_check,
    build_overview_data,
    render_overview,
)

TPL_DIR = ROOT / "src" / "fundamental_agent" / "report"
OUT_DIR = ROOT / "output" / "_verify_named"
OUT_DIR.mkdir(exist_ok=True)


def load_chain(code: str) -> dict:
    hits = list((ROOT / "data" / "cache" / "annual_extract").glob(f"chain_{code}_*.json"))
    assert hits, f"no chain cache for {code}"
    return json.loads(hits[0].read_text(encoding="utf-8"))


def build(code: str, name: str, chain: dict | None, llm_json: dict | None = None):
    profile = {
        "code": code,
        "name": name,
        "layers": {
            "identity": {"short_name": name},
            "business": {},
            "governance": {},
            "market": {},
        },
    }
    if chain is not None:
        profile["layers"]["business"]["business_chain"] = chain
    result = types.SimpleNamespace(code=code, name=name)
    return build_overview_data(result, profile, llm_json=llm_json, use_llm=False)


def render(code: str, data: dict) -> str:
    out = OUT_DIR / f"verify_{code}.html"
    render_overview(data, str(TPL_DIR), str(out))
    return out.read_text(encoding="utf-8")


def main() -> int:
    failures: list[str] = []

    # ================= A. 泛称判定与名单拆分 =================
    print("== A. 泛称判定 / 名单拆分 ==")
    for name, expect in (
        ("全球主要客户", True), ("核心客户", True), ("全球主要云服务商", True),
        ("英伟达", False), ("思灵机器人", False), ("村田", False),
        ("MURATA", False), ("华为技术有限公司", False), ("比亚迪电子", False),
    ):
        got = is_generic_entity(name)
        flag = "OK " if got == expect else "BAD"
        print(f"  [{flag}] is_generic_entity({name!r}) = {got}")
        if got != expect:
            failures.append(f"is_generic_entity({name!r}) 期望 {expect} 实得 {got}")

    named, generic = split_named_entities("英伟达、思灵机器人")
    if named != ["英伟达", "思灵机器人"] or generic:
        failures.append(f"split_named_entities 具名拆分异常：{named} / {generic}")
    named2, generic2 = split_named_entities("全球主要客户、核心客户")
    if named2 or len(generic2) != 2:
        failures.append(f"split_named_entities 泛称拆分异常：{named2} / {generic2}")

    # 名单级去重 + 具名优先排序（跨路径重复：契约表整串 vs AI 增量拆分）
    _ded = OV._dedupe_partner_rows([
        {"partner": "英伟达、思灵机器人", "named": True},
        {"partner": "全球多家头部客户", "named": False},
        {"partner": "英伟达", "named": True},
        {"partner": "思灵机器人", "named": True},
        {"partner": "苹果、华为", "named": True, "src": "public"},
    ])
    _order = [r["partner"] for r in _ded]
    print("  去重+排序后:", _order)
    if _order != ["英伟达、思灵机器人", "苹果、华为", "全球多家头部客户"]:
        failures.append(f"合作方名单级去重/排序异常：{_order}")

    # 上游类别名清洗（LLM 常把关系词混进类别）
    for raw, exp in (
        ("上游供应链 · 紧密合作", "供应链"),
        ("关键品类/技术", "关键品类/技术"),
        ("供应商集中度", "供应商集中度"),
        ("", "供给合作"),
    ):
        got = OV._clean_up_category(raw)
        flag = "OK " if got == exp else "BAD"
        print(f"  [{flag}] clean_up_category({raw!r}) = {got!r}")
        if got != exp:
            failures.append(f"_clean_up_category({raw!r}) 期望 {exp!r} 实得 {got!r}")

    # ================= B. 抽取层（真实半年报）=================
    print("\n== B. 抽取层：具名抽取与泛称标记 ==")
    for code in ("601138", "002384"):
        pdf = _find_cached_report(code=code, kind="semi")
        if not pdf:
            print(f"  [SKIP] {code} 无半年报缓存")
            continue
        txt = "\n".join(pg.get_text() for pg in fitz.open(str(pdf)))
        rows = _extract_partner_sents(txt)
        print(f"  [{code}] {len(rows)} 条")
        for r in rows:
            print(f"     [{'具名' if r['named'] else '泛称'}] {r['partner']}")
        # 碎片（如「全球」「头部」「核心」）必须已被贪婪+截断修掉
        for r in rows:
            if len(re.sub(r"\s+", "", r["partner"])) < 4:
                failures.append(f"{code} 合作方名出现碎片：{r['partner']!r}")
        if code == "601138":
            if not any(r["named"] and "英伟达" in r["partner"] for r in rows):
                failures.append("601138 未抽到具名合作方「英伟达」（动词前置句式漏抽未修好）")
            if not any(not r["named"] for r in rows):
                failures.append("601138 泛称条目未标记 named=False")

    # ================= C. 公开信息补位护栏 =================
    print("\n== C. 公开信息补位护栏 ==")
    up_cats = {"关键品类/技术"}
    down_fields = {"AI算力"}
    good = _public_entity_check(
        {
            "upstream": [
                {"category": "自创类别", "names": "台积电、日月光", "basis": "x"},   # 类别自创 → 拒
                {"category": "关键品类/技术", "names": "台积电、日月光", "basis": "公开产业链报道"},
            ],
            "downstream": [
                {"field": "自创领域", "customers": "英伟达", "basis": "x"},          # 领域自创 → 拒
                {"field": "AI算力", "customers": "英伟达、微软", "basis": "公开产业链报道"},
                {"field": "AI算力", "customers": "英伟达（占比 35.2%）", "basis": "x"},  # 精确数字 → 拒
                {"field": "AI算力", "customers": "多家头部企业", "basis": "x"},        # 泛称 → 拒
            ],
            "partners": [
                {"partner": "苹果、华为", "relation": "下游核心客户 · 公开信息",
                 "content": "公开产业信息显示的典型客户", "basis": "公开产业链报道"},
                {"partner": "相关厂商", "relation": "x", "content": "y", "basis": "z"},  # 泛称 → 拒
            ],
        },
        up_cats,
        down_fields,
    )
    print("  upstream 保留:", good["upstream"])
    print("  downstream 保留:", good["downstream"])
    print("  partners 保留:", good["partners"])
    if len(good["upstream"]) != 1 or good["upstream"][0]["category"] != "关键品类/技术":
        failures.append("护栏未正确拦截自创上游类别")
    if len(good["downstream"]) != 1 or good["downstream"][0]["customers"] != "英伟达、微软":
        failures.append("护栏未正确拦截自创领域 / 精确数字 / 泛称客户名")
    if len(good["partners"]) != 1:
        failures.append("护栏未正确拦截泛称合作方名")

    # ================= D. 装配 + 渲染（monkeypatch 补位通道）=================
    print("\n== D. 装配 + 渲染（模拟公开信息补位）==")

    def _fake_public(bc, seed, use_llm=True):
        """模拟公开信息补位：类别/领域严格复用 seed（与真实护栏口径一致）。"""
        out = {"upstream": [], "downstream": [], "partners": []}
        for u in (seed.get("upstream_struct") or []):
            if u.get("category"):
                out["upstream"].append({
                    "category": u["category"], "names": "台积电、日月光",
                    "basis": "公开产业链报道", "src": "public",
                })
        for d in (seed.get("downstream") or []):
            if d.get("field"):
                out["downstream"].append({
                    "field": d["field"], "customers": "英伟达、微软",
                    "basis": "公开产业链报道", "src": "public",
                })
        out["partners"].append({
            "partner": "苹果、华为", "relation": "下游核心客户 · 公开信息",
            "content": "公开产业信息显示的典型客户与合作伙伴", "basis": "公开产业链报道",
            "src": "public",
        })
        return out

    _orig = OV._build_public_entities
    _orig_fb = OV._build_customer_table_fallback
    OV._build_public_entities = _fake_public
    # 离线环境无 LLM 契约：用固定下游行（泛称口径）替代数据层兜底，
    # 复现「一手资料只有泛称、需公开信息补位」的真实场景
    OV._build_customer_table_fallback = lambda bc, period="": [
        {"field": "AI算力", "customers": "全球多家头部客户", "perf": "—",
         "hl": True, "cls": "", "period": period or "2026H1"},
        {"field": "数据中心", "customers": "全球主要云服务商", "perf": "收入同比增长",
         "hl": False, "cls": "up", "period": period or "2026H1"},
    ]
    try:
        d = build("601138", "工业富联", load_chain("601138"))
    finally:
        OV._build_public_entities = _orig
        OV._build_customer_table_fallback = _orig_fb

    print("[601138] upstream_struct_has_suppliers =", d.get("upstream_struct_has_suppliers"))
    for u in d.get("upstream_struct") or []:
        print(f"    {u.get('category')} | suppliers={u.get('suppliers')}")
    if not d.get("upstream_struct_has_suppliers"):
        failures.append("上游「代表企业」列开关未打开")
    if not any(str(u.get("suppliers") or "").strip() for u in d.get("upstream_struct") or []):
        failures.append("上游行未注入代表企业")

    _dw_pub = [x for x in (d.get("downstream") or []) if x.get("public_customers")]
    print("[601138] 下游补位行数 =", len(_dw_pub))
    for x in _dw_pub[:3]:
        print(f"    {x.get('field')}: 一手={x.get('customers')} | 公开={x.get('public_customers')}")
    if not _dw_pub:
        failures.append("下游未注入公开信息补充客户")

    _p_pub = [x for x in (d.get("partners") or []) if x.get("src") == "public"]
    print("[601138] partners_title =", d.get("partners_title"))
    print("[601138] partners_intro =", (d.get("partners_intro") or "")[:150])
    print("[601138] 合作网络补位行数 =", len(_p_pub))
    if not _p_pub:
        failures.append("合作网络未注入公开信息具名行")
    if "苹果" not in (d.get("partners_title") or ""):
        failures.append("合作网络标题未按最终行（含补位行）重算")
    if "公开信息" not in (d.get("partners_intro") or ""):
        failures.append("合作网络导语未说明公开信息补位")

    h = render("601138", d)
    print("[601138] 渲染产物：")
    for needle, must in (
        ("公开信息·待核实", True),
        ("原文未具名", True),
        ("代表企业", True),
        ("台积电、日月光", True),
        ("英伟达、微软", True),
    ):
        hit = needle in h
        print(f"    {needle}: {'命中' if hit else '缺失'}")
        if must and not hit:
            failures.append(f"渲染产物缺少：{needle}")

    # 回归：分销商（力源信息）不受影响——上游原厂仍为具名，且不出现补位列
    print("\n== 回归：300184 力源信息（分销商）==")
    d3 = build("300184", "力源信息", load_chain("300184"))
    print("[300184] upstream_title =", d3.get("upstream_title"))
    print("[300184] upstream_struct_has_suppliers =", d3.get("upstream_struct_has_suppliers"))
    _p3 = d3.get("partners") or []
    if _p3:
        print("[300184] partners 前 3：", [x.get("partner") for x in _p3[:3]])
    if not d3.get("upstream_title"):
        failures.append("300184 回归：upstream_title 丢失")
    if not any(not x.get("named", True) for x in _p3):
        print("    （注：力源的授权原厂条目全为具名，符合预期）")
    h3 = render("300184", d3)
    if "上游：代理了谁的产品线" in h3:
        failures.append("300184 回退到写死话术")
    if "原文未具名" in h3:
        failures.append("300184 出现「原文未具名」标记（授权原厂本应全部具名）")

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    print("outputs:", OUT_DIR / "verify_601138.html", "|", OUT_DIR / "verify_300184.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
