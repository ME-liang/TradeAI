"""检索代理（WorkBuddy 复刻 · M2：agentic_search 的本地等价物，选项 A）。

职责：为概况报告补充**定性层**信息（合作关系、行业格局、近期动态），
输出带来源与日期的综合摘要，供 LLM 在叙述中引用——**不向报告贡献任何
精确财务数字**（数字一律来自六层结构化快照，见 SKILL.md 前置约束）。

设计（选项 A，单调用形态，对应 agentic_search 的「输入意图 → 输出摘要」）：
- 后端 1（默认，免密钥）：东方财富个股新闻 `ak.stock_news_em`——与项目既有
  akshare 数据生态同源，国内网络可达；
- 后端 2（可选）：web 检索 API（tavily 兼容形态），仅当 `SEARCH_API_KEY`
  存在时启用，作为定性补充；
- 综合：经多厂商 LLM 路由（复用 get_router）把原始条目压缩为分节摘要，
  每条结论强制带来源与日期。

失败语义（与项目「降级不中断」文化一致）：任何后端失败只记 warning；
全部后端无结果时返回 None，pipeline 原样继续（等价于现状，绝不比现在更差）。

配置：config.yaml 的 `overview_writer.search` 段（enabled / news_limit /
web_provider / web_api_key_env / request_timeout / synthesis_max_tokens）。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import yaml

from ..llm import get_router, load_prompt
from ..llm.base import LLMRequest

logger = logging.getLogger(__name__)

# 项目根：agents/search_agent.py -> agents -> fundamental_agent -> src -> 根
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_ENV_PATH = _PROJECT_ROOT / ".env"

_DEFAULTS = {
    "enabled": True,
    "news_limit": 20,
    "web_provider": "tavily",
    "web_api_key_env": "SEARCH_API_KEY",
    "request_timeout": 30,
    "synthesis_max_tokens": 1600,
    "max_items_for_synthesis": 24,
    "item_content_chars": 160,
}


def _load_search_config() -> dict:
    """读取 config.yaml 的 overview_writer.search 段（缺省全默认）。"""
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        sec = (cfg.get("overview_writer") or {}).get("search") or {}
    except Exception:  # noqa: BLE001 — 配置读取失败按全默认处理
        sec = {}
    try:
        from dotenv import load_dotenv

        load_dotenv(_ENV_PATH)
    except Exception:  # noqa: BLE001
        pass
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in sec.items() if v is not None})
    return out


# ---------------------------------------------------------------------------
# 后端 1：东方财富个股新闻（免密钥，国内可达）
# ---------------------------------------------------------------------------
def _fetch_news_items(code: str, limit: int, timeout: int) -> list[dict]:
    try:
        import akshare as ak

        df = ak.stock_news_em(symbol=code)
        if df is None or df.empty:
            return []
    except Exception as e:  # noqa: BLE001 — 新闻后端失败只降级
        logger.warning("[search_agent] stock_news_em 失败：%s", e)
        return []

    items: list[dict] = []
    for _, row in df.head(limit).iterrows():
        items.append(
            {
                "kind": "news",
                "title": str(row.get("新闻标题") or "").strip(),
                "content": str(row.get("新闻内容") or "").strip()[:300],
                "time": str(row.get("发布时间") or "").strip(),
                "source": str(row.get("文章来源") or "").strip(),
                "url": str(row.get("网页链接") or "").strip(),
            }
        )
    return items


# ---------------------------------------------------------------------------
# 后端 2：web 检索 API（可选，tavily 兼容形态；需 SEARCH_API_KEY）
# ---------------------------------------------------------------------------
_WEB_QUERIES = ("{name} 主要客户 供应商 合作关系", "{name} 行业 竞争格局 市场份额", "{name} 最新公告 战略动态")


def _web_api_key(cfg: dict) -> str:
    return os.getenv(str(cfg.get("web_api_key_env") or "SEARCH_API_KEY"), "").strip()


def _fetch_web_items(name: str, cfg: dict) -> list[dict]:
    key = _web_api_key(cfg)
    if not key or str(cfg.get("web_provider")) == "none":
        return []
    import requests

    items: list[dict] = []
    timeout = int(cfg.get("request_timeout") or 30)
    for q in _WEB_QUERIES:
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": key,
                    "query": q.format(name=name),
                    "search_depth": "basic",
                    "max_results": 5,
                    "include_answer": False,
                },
                timeout=timeout,
            )
            if resp.status_code != 200:
                logger.warning("[search_agent] web 检索 %s -> HTTP %s", q, resp.status_code)
                continue
            for r in (resp.json() or {}).get("results") or []:
                items.append(
                    {
                        "kind": "web",
                        "title": str(r.get("title") or "").strip(),
                        "content": str(r.get("content") or "").strip()[:300],
                        "time": str(r.get("published_date") or "").strip(),
                        "source": str((r.get("url") or "")).strip(),
                        "url": str(r.get("url") or "").strip(),
                    }
                )
        except Exception as e:  # noqa: BLE001 — 单条查询失败不影响其他
            logger.warning("[search_agent] web 检索失败（%s）：%s", q, e)
    return items


# ---------------------------------------------------------------------------
# 综合：原始条目 -> 分节摘要（强制带来源与日期）
# ---------------------------------------------------------------------------
def _compact_items(items: list[dict], cfg: dict) -> str:
    """把原始条目压缩为给 LLM 阅读的编号清单（控长度，防撑爆 prompt）。"""
    max_items = int(cfg.get("max_items_for_synthesis") or 24)
    chars = int(cfg.get("item_content_chars") or 160)
    lines: list[str] = []
    for i, it in enumerate(items[:max_items], 1):
        content = (it.get("content") or "")[:chars]
        lines.append(
            f"[{i}] {it.get('time') or '日期未知'} | 来源：{it.get('source') or '未知'}\n"
            f"标题：{it.get('title')}\n内容：{content}"
        )
    return "\n".join(lines)


def _synthesize(name: str, code: str, items: list[dict], cfg: dict) -> str:
    """LLM 综合为三节摘要；失败返回空串（条目仍随结果返回，不丢弃）。"""
    try:
        system = load_prompt("search_synthesis")
    except Exception:
        system = (
            "你是专业的 A 股公司研究分析师。请把给定的检索条目综合为带来源的定性摘要，"
            "严禁编造数字，每条结论须标注来源与日期。"
        )
    user = (
        f"公司：{name}（{code}）\n\n检索条目（可能含噪音，请甄别与本公司直接相关者）：\n"
        f"{_compact_items(items, cfg)}\n\n"
        "请按系统提示的三节格式输出定性摘要。"
    )
    try:
        resp = get_router().complete(
            LLMRequest(system=system, user=user, max_tokens=int(cfg.get("synthesis_max_tokens") or 1600))
        ).text
    except Exception as e:  # noqa: BLE001
        logger.warning("[search_agent] 综合调用失败：%s", e)
        return ""
    text = (resp or "").strip()
    # 降级文本护栏：router 全部厂商不可用时会返回 fallback 提示文案，不能当摘要用
    if "所有模型提供商均不可用" in text or "AI 结论生成失败" in text:
        return ""
    return text


def run_search_agent(name: str, code: str) -> dict | None:
    """检索代理主入口：取条目 -> LLM 综合摘要。

    返回 dict：{"synthesis", "sources_count", "items", "backends", "generated_at"}；
    全部后端无条目或配置关闭时返回 None（调用方按「无检索」处理，pipeline 不中断）。
    """
    cfg = _load_search_config()
    if not cfg.get("enabled", True):
        logger.info("[search_agent] 配置关闭，跳过检索")
        return None

    used: list[str] = []
    items = _fetch_news_items(code, int(cfg.get("news_limit") or 20), int(cfg.get("request_timeout") or 30))
    if items:
        used.append("stock_news_em")
    web = _fetch_web_items(name, cfg)
    if web:
        used.append(f"web:{cfg.get('web_provider')}")
    items = (items + web)[: int(cfg.get("news_limit") or 20) + 15]

    if not items:
        logger.info("[search_agent] 全部后端无条目，按「无检索」降级")
        return None

    synthesis = _synthesize(name, code, items, cfg)
    result = {
        "synthesis": synthesis,
        "synthesis_ok": bool(synthesis),
        "sources_count": len(items),
        "items": [
            {k: it.get(k, "") for k in ("kind", "title", "time", "source", "url")}
            for it in items
        ],
        "backends": used,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "note": "检索摘要仅用于定性叙述；引用须带来源与日期；严禁从中取用精确财务数字",
    }
    logger.info(
        "[search_agent] 完成：后端=%s，条目=%d，综合=%s",
        "+".join(used), len(items), "成功" if synthesis else "失败（条目仍注入）",
    )
    return result
