"""Skill 目录加载器（WorkBuddy 复刻 · M1：框架即上下文）。

把「报告生成方法论」外置为项目内 skill 目录，运行时读入拼进 writer 的
system prompt——对应 WorkBuddy 会话链路中的 Skill 加载 + Read references 环节，
但路径静态确定，无 Glob 式动态发现。

目录约定（项目根下）：
    skills/<name>/SKILL.md           # 技能主体（六层框架 + 输出约束）
    skills/<name>/references/*.md    # 参考文件（排版规范等），整体读入

与 llm/prompts/loader.py 的约定一致：SKILL.md 缺失即抛 FileNotFoundError，
避免静默用空提示。references/ 目录允许缺失（返回空 dict），便于先写主体后补参考。
"""

from __future__ import annotations

from pathlib import Path

# skill_loader.py -> llm -> fundamental_agent -> src -> 项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_DIR = _PROJECT_ROOT / "skills"


def skills_dir() -> Path:
    """返回 skills 根目录（测试与诊断用）。"""
    return _SKILLS_DIR


def list_skills() -> list[str]:
    """列出所有可用 skill 名（含 SKILL.md 的子目录），按名排序。"""
    if not _SKILLS_DIR.is_dir():
        return []
    return sorted(
        p.parent.name
        for p in _SKILLS_DIR.glob("*/SKILL.md")
        if p.is_file()
    )


def load_skill(name: str) -> dict:
    """读取 skill 目录，返回 {"name", "system", "references"}。

    - system：SKILL.md 全文（strip 后）；
    - references：{文件名: 全文}，按文件名排序读入 references/*.md
      （整体读入、不做动态筛选——未来往 references/ 加新参考文件时零代码改动）。

    SKILL.md 不存在抛 FileNotFoundError（沿用 prompts/loader 既有约定）；
    references/ 缺失或为空不报错。
    """
    skill_dir = _SKILLS_DIR / name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise FileNotFoundError(f"skill 不存在：{skill_md}")

    system = skill_md.read_text(encoding="utf-8").strip()

    references: dict[str, str] = {}
    refs_dir = skill_dir / "references"
    if refs_dir.is_dir():
        for ref in sorted(refs_dir.glob("*.md")):
            references[ref.name] = ref.read_text(encoding="utf-8").strip()

    return {"name": name, "system": system, "references": references}
