"""Prompt 模板加载器。

所有提示词外置为 Markdown 文件，改文案不动 Python，便于非工程同学维护。
模板内只写「系统提示」；用户侧内容由代码拼装（如数据包序列化文本）。
"""

from __future__ import annotations

from pathlib import Path

_PROMPT_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """读取 llm/prompts/{name}.md 并返回文本。找不到即报错，避免静默用空提示。"""
    path = _PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt 模板不存在：{path}")
    return path.read_text(encoding="utf-8").strip()
