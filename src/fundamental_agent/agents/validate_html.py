"""HTML 产物 JS 语法校验器（WorkBuddy 复刻 M4 之 JS 闭环部分）。

对渲染出的报告 HTML 提取内联 <script> 块，逐块用 `node --check` 做语法校验。
设计约定（与 Step 2 search_agent 同风格，零耦合、可降级）：

- Node 不可用时返回 status="skipped"，**不视为失败、不阻断产物**，仅留 warning 日志；
- 校验失败返回 status="failed" 并携带每块的报错原文（未来 writer 模式据此回传重试）；
- 校验通过返回 status="passed"。

用法::

    from fundamental_agent.agents.validate_html import check_html_js
    result = check_html_js(html_text)          # -> {"status", "blocks", "errors", "node"}
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# 提取无 src 属性的内联 script 块（外链 CDN 的 Chart.js 等不在校验范围）
_INLINE_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", re.S | re.I
)

# node --check 对超大块可能有性能压力，报告内联块一般 <100KB，无需上限；防御性兜底 2MB
_MAX_BLOCK_CHARS = 2_000_000


def _find_node(configured: str | None = None) -> str | None:
    """定位 node 可执行文件：显式配置优先且不回退（配置缺失即视为无 node，行为确定可测）。"""
    if configured:
        if Path(configured).is_file() or shutil.which(configured):
            return configured
        return None
    return shutil.which("node")


def extract_inline_scripts(html: str) -> list[str]:
    """提取全部内联 <script> 块（按出现顺序）。"""
    return [m.group(1) for m in _INLINE_SCRIPT_RE.finditer(html) if m.group(1).strip()]


def check_html_js(
    html: str, node_path: str | None = None, timeout: int = 30
) -> dict:
    """对 HTML 的内联 JS 做语法校验。

    返回 dict：
    - status: "passed" | "failed" | "skipped"
    - blocks: 内联 script 块数量
    - errors: [{"block": 序号, "message": 报错原文}]（仅 failed 时非空）
    - node:   使用的 node 路径（skipped 时为 None）
    """
    blocks = extract_inline_scripts(html)

    node = _find_node(node_path)
    if not node:
        return {
            "status": "skipped",
            "blocks": len(blocks),
            "errors": [],
            "node": None,
            "reason": "node not found in PATH",
        }

    errors: list[dict] = []
    for i, code in enumerate(blocks):
        if len(code) > _MAX_BLOCK_CHARS:  # 防御性：超长块跳过并记录
            errors.append({"block": i, "message": f"block too large ({len(code)} chars), skipped"})
            continue
        # node --check 要求文件后缀能识别模块类型；.js 默认按 CommonJS/脚本解析，
        # 报告内联脚本均为普通脚本（Chart.js 初始化），满足需要。
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp = f.name
        try:
            r = subprocess.run(
                [node, "--check", tmp],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if r.returncode != 0:
                msg = (r.stderr or r.stdout or "").strip()
                errors.append({"block": i, "message": msg[:2000]})
        except subprocess.TimeoutExpired:
            errors.append({"block": i, "message": f"node --check timeout after {timeout}s"})
        finally:
            try:
                Path(tmp).unlink()
            except OSError:
                pass

    return {
        "status": "failed" if errors else "passed",
        "blocks": len(blocks),
        "errors": errors,
        "node": node,
    }
