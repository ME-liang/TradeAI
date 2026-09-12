"""skill_loader 单元测试（不依赖网络）。

验证（对应改造方案 Step 1 验收标准）：
1. load_skill 产出包含六层框架关键词（SKILL.md 主体）；
2. references 整体读入（html-report-style.md 全文可达，含 Chart.js 校准内容）；
3. skill 缺失抛 FileNotFoundError（沿用 prompts/loader 既有约定）；
4. references/ 缺失不报错（允许先写主体后补参考）；
5. list_skills 能发现已注册 skill。
"""

import shutil
from pathlib import Path

import pytest

from fundamental_agent.llm.skill_loader import (
    list_skills,
    load_skill,
    skills_dir,
)

_EXPECTED_LAYER_KEYWORDS = (
    "身份识别", "业务画像", "股权治理", "行业坐标", "财务速写", "动态与风险",
)


def test_company_profile_system_contains_six_layers():
    skill = load_skill("company-profile")
    assert skill["name"] == "company-profile"
    for kw in _EXPECTED_LAYER_KEYWORDS:
        assert kw in skill["system"], f"SKILL.md 缺少六层框架关键词：{kw}"


def test_company_profile_references_loaded_whole():
    skill = load_skill("company-profile")
    refs = skill["references"]
    assert "html-report-style.md" in refs, "排版规范参考文件应被整体读入"
    style = refs["html-report-style.md"]
    # 校准要点：实际图表库为项目使用的 Chart.js（script 引用与模板一致），含 JS 自检章节
    assert "Chart.js" in style
    assert "chart.umd.js" in style          # Chart.js 引用存在
    assert "echarts.min.js" not in style    # 不得残留 ECharts 引库
    assert "node --check" in style
    # 设计令牌与模板对齐（template_overview.html 的品牌色）
    assert "#185fa5" in style


def test_missing_skill_raises():
    with pytest.raises(FileNotFoundError):
        load_skill("no-such-skill")


def test_references_dir_optional(tmp_path: Path):
    """references/ 缺失时返回空 dict，不抛异常。"""
    fake_root = tmp_path / "skills"
    (fake_root / "mini").mkdir(parents=True)
    (fake_root / "mini" / "SKILL.md").write_text("# mini skill", encoding="utf-8")

    import fundamental_agent.llm.skill_loader as sl

    orig = sl._SKILLS_DIR
    sl._SKILLS_DIR = fake_root
    try:
        skill = load_skill("mini")
        assert skill["system"] == "# mini skill"
        assert skill["references"] == {}
        assert "mini" in list_skills()
        assert "company-profile" not in list_skills()
    finally:
        sl._SKILLS_DIR = orig
    assert skills_dir() == orig  # 不污染全局状态


def test_skill_dir_layout_matches_plan():
    """方案约定的目录物理存在（SKILL.md + references/html-report-style.md）。"""
    root = skills_dir() / "company-profile"
    assert (root / "SKILL.md").is_file()
    assert (root / "references" / "html-report-style.md").is_file()
