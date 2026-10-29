"""历史报告迁移工具（F-06 决策 D7）的随仓用例。

这个工具**会移动真实文件**，所以它的 dry-run / 备份 / 幂等 / 不覆盖 四条语义
必须被测试锁住 —— 否则一次误用就是不可逆的数据丢失。
"""

from __future__ import annotations

import json

import pytest

from fundamental_agent import report_index
from fundamental_agent.tools import migrate_legacy_reports as mig


def _mk(base, rel, user_meta=None, with_html=True, with_meta=True):
    d = base / rel
    d.mkdir(parents=True, exist_ok=True)
    if with_html:
        (d / "report.html").write_text("<html>x</html>", encoding="utf-8")
    if with_meta:
        meta = {"code": "002594", "name": "比亚迪", "report_type": "overview"}
        if user_meta is not None:
            meta["user_id"] = user_meta
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return d


@pytest.fixture()
def base(tmp_path):
    b = tmp_path / "reports"
    b.mkdir()
    return b


def test_dry_run_不改动任何文件(base, capsys):
    _mk(base, "002594/overview/20260922")
    mig.main(["--base", str(base), "--user-id", "1"])
    assert not (base / "u1").exists(), "dry-run 不应创建目标目录"
    assert (base / "002594/overview/20260922/report.html").is_file()
    assert "dry-run" in capsys.readouterr().out


def test_apply_搬到用户根并补写_meta_user_id(base):
    _mk(base, "002594/overview/20260922")
    _mk(base, "002594/20260922")                        # 2 段 = 基本面
    _mk(base, "002594/overview/20260922_morning_noLLM")  # 非规范日期目录
    _mk(base, "BK1036/board/20260922")

    assert mig.main(["--base", str(base), "--user-id", "1", "--apply"]) == 0

    for rel in ("u1/002594/overview/20260922/report.html",
                "u1/002594/20260922/report.html",
                "u1/002594/overview/20260922_morning_noLLM/report.html",
                "u1/BK1036/board/20260922/report.html"):
        assert (base / rel).is_file(), "缺少 %s" % rel

    meta = json.loads((base / "u1/002594/overview/20260922/meta.json").read_text(encoding="utf-8"))
    assert meta["user_id"] == 1
    assert meta["name"] == "比亚迪", "补 user_id 时不得丢失既有字段"


def test_不覆盖已存在的目标(base, capsys):
    _mk(base, "002594/overview/20260922")
    _mk(base, "u1/002594/overview/20260922")   # 目标已有一份
    marker = base / "u1/002594/overview/20260922/report.html"
    old = marker.read_text(encoding="utf-8")

    mig.main(["--base", str(base), "--user-id", "1", "--apply"])
    assert marker.read_text(encoding="utf-8") == old, "目标已存在时不得覆盖"
    assert "跳过" in capsys.readouterr().out


def test_跳过已有用户目录与非报告目录(base):
    _mk(base, "junk")                              # 1 段，非报告目录
    _mk(base, "u7/002594/overview/20260922")       # 已属用户 7
    mig.main(["--base", str(base), "--user-id", "1", "--apply"])
    assert (base / "junk/report.html").is_file(), "非报告目录应保持原样"
    assert (base / "u7/002594/overview/20260922/report.html").is_file(), "u{N} 目录不得被搬走"
    assert not (base / "u1/u7").exists()


def test_幂等_再跑一次无事可做(base):
    _mk(base, "002594/overview/20260922")
    mig.main(["--base", str(base), "--user-id", "1", "--apply"])
    assert mig.main(["--base", str(base), "--user-id", "1"]) == 0


def test_迁移后读取侧能列出(base):
    """工具的产出必须与 `report_index` 的读取口径对齐（否则搬了也看不到）。"""
    _mk(base, "002594/overview/20260922")
    _mk(base, "BK1036/board/20260922")
    mig.main(["--base", str(base), "--user-id", "1", "--apply"])
    res = report_index.list_reports(1, base=base)
    assert res["total"] == 2
    assert sorted(t["key"] for t in res["types"]) == ["board", "overview"]
    assert report_index.list_reports(7, base=base)["total"] == 0


def test_备份落在报告根同级(base):
    _mk(base, "002594/overview/20260922")
    mig.main(["--base", str(base), "--user-id", "1", "--apply"])
    assert (base.parent / (base.name + "_migrate_backup")).is_dir(), "备份应跟随数据根"
