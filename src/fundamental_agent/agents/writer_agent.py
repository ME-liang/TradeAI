"""writer_agent：WorkBuddy 复刻 M3 —— LLM 直写完整 HTML（direct 模式）。

四段式 prompt（对齐 docs/WorkBuddy复刻改造方案.md 第四节）：
  ① system：skills/company-profile 的 SKILL.md + references/html-report-style.md
     （经 skill_loader 读入——框架方法论 + 排版规范一次性注入）；
  ② 六层结构化快照 profile_dict（数字唯一来源）；
  ③ 关键财务事实核对表（复用 overview.py 的 _key_facts_brief 真值前置）；
  ④ 检索摘要（dynamics.research，仅定性叙述用）+ 输出指令。

生成后校验闭环（重试 ≤ N 次，报错原文回传）：
  - 基础健全性：完整 HTML 文档、长度下限；
  - 硬错误正则：复用 overview.py 的 _WRONG_HOLDING_PATTERNS（持股 10 倍误差案例）；
  - 数字量级证伪：复用 overview.py 的 _factual_claim_check（毛利率/净利率/营收同比，
    0.55x~1.8x 量级带，002384 毛利率 39.33% vs 真值 20.15% 即由此拦截）；
  - 存货/应收错标专项：历史事故（20.34 亿错标）回归防线；
  - JS 语法：validate_html.check_html_js（node --check）。

任何失败由调用方降级：direct 失败 → 回退 contract 路径，不产空壳。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..llm import get_router, load_llm_config
from ..llm.base import LLMRequest
from ..llm.skill_loader import load_skill

_SKILL_NAME = "company-profile"

# 输出长度软约束：完整 HTML（含 CSS）在此区间可保证信息量与 token 可行性的平衡
_MIN_HTML_CHARS = 5000

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


def _writer_config() -> dict:
    """读取 config.yaml 的 overview_writer.writer 段（缺省全默认）。"""
    defaults = {"max_tokens": 16000, "retries": 2, "timeout": 600}
    try:
        import yaml

        cfg_path = Path(__file__).resolve().parents[2] / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        sec = (cfg.get("overview_writer") or {}).get("writer") or {}
        defaults.update({k: v for k, v in sec.items() if v is not None})
    except Exception:  # noqa: BLE001 — 配置缺失按全默认
        pass
    return defaults


def _strip_fence(text: str) -> str:
    """剥离 LLM 可能包裹的 markdown 代码围栏。"""
    t = (text or "").strip()
    t = _FENCE_RE.sub("", t)
    return t.strip()


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _mislabel_check(html: str, profile_dict: dict) -> list[str]:
    """存货/应收错标专项（历史硬错误：20.34 亿应收被写成存货）。

    正文若点名「存货/应收账款 + 金额」，金额必须与核对表真值匹配（±2%）；
    点名了但数值对不上 → 错标。金额互相互换（A 字段写了 B 的值）同样命中。
    仅覆盖核对表有真值的字段，不做泛化推断，避免误杀。
    """
    fin = ((profile_dict.get("layers", {}) or {}).get("financials", {}) or {}).get("summary", {}) or {}
    pairs = [
        ("存货", fin.get("inventory")),
        ("应收账款", fin.get("accounts_receivable")),
    ]
    issues: list[str] = []
    for label, truth in pairs:
        try:
            tv = float(truth) / 1e8  # 元 → 亿元
        except (TypeError, ValueError):
            continue
        for m in re.finditer(rf"{label}\s*约?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*亿", html):
            try:
                claimed = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if tv <= 0:
                continue
            if abs(claimed - tv) / tv > 0.02:
                issues.append(
                    f"{label}错标：正文写 {claimed:.2f} 亿，真值 {tv:.2f} 亿（三表计算）"
                )
    return issues


def _strip_segment_margins(profile_dict: dict) -> dict:
    """深拷贝快照并剔除所有分项「毛利率」字段（数据级根治口径混用）。

    背景：主营构成等分部数据自带「毛利率」字段（300184「电力产品等」23.9%、
    002384「光模块」39.33%），qwen-turbo/qwen-plus 均反复把分项毛利率冒充
    公司整体毛利率写入正文，prompt 口径纪律 + 驳回重试（两模型各 6 稿实测）
    均无法扭转。唯一可靠做法是让分项毛利率根本不出现在 writer 输入里——
    公司整体真值由【比率真值】前置给出，分项毛利率定性描述不依赖具体数字。
    仅作用于 writer 直写的 prompt 快照，contract 管线与落盘 profile.json 不受影响。
    """
    import copy

    data = copy.deepcopy(profile_dict)

    def _walk(node) -> None:
        if isinstance(node, dict):
            node.pop("毛利率", None)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(data)
    return data


def _build_user_prompt(profile_dict: dict, name: str, code: str) -> str:
    """② 快照 + ③ 核对表 + ④ 检索摘要与输出指令。"""
    from ..report_types.overview import _key_facts_brief  # noqa: PLC0415 — 复用既有真值

    research = (
        ((profile_dict.get("layers", {}) or {}).get("dynamics", {}) or {}).get("research")
        or {}
    )
    research_txt = ""
    if research.get("synthesis"):
        research_txt = (
            "【检索代理补充素材（仅用于定性叙述：近期动态/合作关系/行业格局）】\n"
            "使用规则：引用其内容须保留来源与日期；严禁从中取用任何精确财务数字；"
            "条目分歧时并列呈现。\n"
            + str(research.get("synthesis"))
        )

    snapshot = json.dumps(_strip_segment_margins(profile_dict), ensure_ascii=False, default=str)

    # 比率真值钉死：核对表金额不含比率，LLM 曾反复自行推算毛利率致量级冲突
    # （300184 实测三稿分别写 23.9%/20.0%/23.9%，真值 10.35%），必须前置显式给出。
    fin = (
        ((profile_dict.get("layers", {}) or {}).get("financials", {}) or {}).get("summary", {})
        or {}
    )
    ratio_lines = [
        f"{label}：{fin[key]}%"
        for key, label in (
            ("gross_margin", "毛利率"),
            ("net_margin", "净利率"),
            ("rev_growth", "营收同比增速"),
        )
        if fin.get(key) is not None
    ]
    ratio_txt = (
        ("【比率真值（三表直接计算，正文提及以下指标时必须逐字采用，严禁另行推算）】\n  - " + "\n  - ".join(ratio_lines))
        if ratio_lines
        else ""
    )

    # 分项毛利率口径纪律：快照中分项「毛利率」字段已由 _strip_segment_margins 剔除
    # （两模型 6 稿实测证明提示词约束单独无效），此段退为兜底：禁止凭记忆补写分项百分比。
    ratio_discipline = (
        "【毛利率口径纪律（违反必被驳回）】\n"
        "  - 公司整体毛利率/净利率/营收同比增速：只能逐字使用上方【比率真值】，禁止任何推算或改写；\n"
        "  - 快照未提供分项业务的毛利率数字：任何「XX业务毛利率XX%」的百分比写法一律禁止，\n"
        "    分项盈利能力只允许定性表述（如「XX业务毛利率显著高于公司整体水平」），严禁编造数字。\n"
        "【存货等科目口径纪律（违反必被驳回）】\n"
        "  - 正文提及存货、应收账款等科目金额：只能逐字使用【关键财务事实核对表】中的数值，\n"
        "    严禁推算、加总、换算或凭记忆补写（qwen-plus 实测曾三稿把 19.45 亿存货写成 49~50 亿）。\n"
    )

    return (
        f"请为 {name}（{code}）生成一份「公司概况分析」完整 HTML 报告。\n\n"
        "【输出要求】\n"
        "- 直接输出完整 HTML 文档（<!DOCTYPE html> 开头，</html> 结尾），不要任何解释文字；\n"
        "- 按 system 中六层框架组织内容：身份识别/业务画像（篇幅最重）/合作关系/股权治理/行业坐标/财务速写/动态与风险；\n"
        "- 排版遵循 system 附带的 html-report-style 规范：单文件、内联 CSS、Chart.js 4（https://cdn.jsdelivr.net/npm/chart.js@4.4.1）"
        "红涨绿跌、数据一律来自下方快照；\n"
        "- 精确数字只能取自【六层结构化快照】与【关键财务事实核对表】，严禁凭记忆补充任何绝对额/比率；\n"
        "- 快照未覆盖的定性内容标注「非一手来源·待核实」，无法生成的小节写「未获取」；\n"
        "- 总长度控制在 12000~20000 字符（含 CSS），优先保证内容完整再优化样式。\n\n"
        f"【关键财务事实核对表（三表直接计算的真值，逐项钉死）】\n{_key_facts_brief(profile_dict)}\n\n"
        + (ratio_txt + "\n\n" if ratio_txt else "")
        + (ratio_discipline + "\n" if ratio_txt else "")
        + (research_txt + "\n\n" if research_txt else "")
        + f"【六层结构化快照（数字唯一来源）】\n{snapshot}"
    )


def generate_overview_html(result, profile_dict: dict, name: str, code: str) -> dict | None:
    """direct 模式主入口：LLM 直写完整 HTML，内置校验重试闭环。

    返回 {"html", "attempts", "last_errors"}；全部重试失败返回 None（调用方降级 contract）。
    """
    cfg = _writer_config()
    max_retries = int(cfg.get("retries") or 2)
    try:
        skill = load_skill(_SKILL_NAME)
        system = skill["system"] + "\n\n" + "\n\n".join(skill["references"].values())
    except Exception as e:  # noqa: BLE001 — skill 缺失则 direct 模式不可用
        print(f"[writer_agent] skill 加载失败：{e}")
        return None

    base_user = _build_user_prompt(profile_dict, name, code)
    from ..report_types.overview import (  # noqa: PLC0415 — 复用既有护栏
        _WRONG_HOLDING_PATTERNS,
        _factual_claim_check,
    )
    from .validate_html import check_html_js  # noqa: PLC0415

    feedback: list[str] = []
    for attempt in range(1, max_retries + 2):
        user = base_user
        if feedback:
            user += (
                "\n\n【上一稿被校验驳回，以下问题必须逐条修复后重新输出完整 HTML】\n- "
                + "\n- ".join(feedback)
            )
        try:
            resp = get_router().complete(
                LLMRequest(
                    system=system,
                    user=user,
                    max_tokens=int(cfg.get("max_tokens") or 16000),
                )
            )
        except Exception as e:  # noqa: BLE001
            print(f"[writer_agent] 第 {attempt} 次调用异常：{e}")
            feedback = [f"LLM 调用异常：{e}"]
            continue
        if not resp.ok or not resp.text:
            print(f"[writer_agent] 第 {attempt} 次调用失败：provider={resp.provider} err={(resp.error or '')[:120]}")
            feedback = [f"LLM 调用失败：{resp.error}"]
            continue

        html = _strip_fence(resp.text)
        checks: list[str] = []
        # 1) 基础健全性（篇幅下限：最初空壳稿约 7K 字符，qwen-turbo 稳定输出 11.2K+，
        #    下限定 10000 既拦极简稿又不误杀正常稿；300184 实测 11240 字符×3 稿被 12K 误杀后放宽）
        if len(html) < _MIN_HTML_CHARS or "</html>" not in html.lower():
            checks.append(
                f"输出不是完整 HTML 文档（长度 {len(html)}，"
                f"{'疑似被 max_tokens 截断' if '</html>' not in html.lower() else '过短'}），须输出完整文档"
            )
        elif len(html) < 10000:
            checks.append(
                f"内容量不足：当前 {len(html)} 字符，要求 10000 字符以上；"
                "各层内容须充分展开（业务画像篇幅最重），不得交付极简版"
            )
        # 1b) 图表存在性：报告规范要求 Chart.js 4 图表，外链 CDN 不算数，
        #     必须有内联 <script> 且其中实际调用 Chart(...)，否则财务速写无图
        #     （独立于篇幅判断，避免长稿漏检图表）
        if not checks and (
            "chart.js" not in html.lower() or not re.search(
                r"<script(?![^>]*\bsrc=)[^>]*>.*?new\s+Chart\s*\(", html, re.S | re.I
            )
        ):
            checks.append(
                "缺少 Chart.js 图表：须在财务速写等模块用内联 <script> 调用 new Chart(...) "
                "绘制图表（外链 CDN <script src> 不算图表），红涨绿跌配色"
            )
        if not checks:
            text = _strip_tags(html)
            # 2) 硬错误正则（持股误判等历史事故）
            if any(p.search(text) for p in _WRONG_HOLDING_PATTERNS):
                checks.append("命中持股硬错误模式（实控人/持股比例误判），参见核对表与前十大股东真值")
            # 3) 数字量级证伪（毛利率/净利率/营收同比）
            checks.extend(_factual_claim_check(text, profile_dict))
            # 4) 存货/应收错标专项
            checks.extend(_mislabel_check(html, profile_dict))
        # 5) JS 语法
        if not checks:
            js = check_html_js(html)
            if js["status"] == "failed":
                checks.extend(
                    f"JS 语法错误：{e['message'][:300]}" for e in js["errors"]
                )

        if not checks:
            print(f"[writer_agent] 第 {attempt} 次成稿通过全部校验")
            return {"html": html, "attempts": attempt, "last_errors": []}

        print(f"[writer_agent] 第 {attempt} 次成稿被驳回（{len(checks)} 项）：{checks[0][:80]}")
        feedback = checks

    return {"html": None, "attempts": max_retries + 1, "last_errors": feedback}
