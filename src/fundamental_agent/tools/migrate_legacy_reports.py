"""把「改造前」的报告产物迁移到按用户隔离的目录下（F-06 决策 D7，一次性工具）。

背景
----
F-06 之前，报告落盘路径是 `output/reports/{code}/[{report_type}/]{yyyymmdd}/`，
**不含用户维度**——因为当时只有一个 admin 账号，且报告也没有归属概念。
F-06 引入 `u{user_id}/` 段后，读取侧（`report_index.user_root`）只在**当前用户的根**内
扫描，于是这些历史产物既不在任何人的库里，也不该被误当成「共享资料」给所有人看
（多租户下那是数据泄漏）。

本工具把它们**搬进指定用户的根**，并补写 `meta.user_id`。

用法
----
    # 先看会做什么（默认 dry-run，不动任何文件）
    python -m fundamental_agent.tools.migrate_legacy_reports

    # 指定归属用户（默认取 auth.db 里的 admin）
    python -m fundamental_agent.tools.migrate_legacy_reports --user-id 1

    # 真正执行（先自动备份到 output/reports_migrate_backup/）
    python -m fundamental_agent.tools.migrate_legacy_reports --apply

安全约定
--------
- **默认 dry-run**：不加 `--apply` 只打印计划。
- `--apply` 前先整目录复制一份备份，打印备份位置；备份失败即中止。
- **同一目标已存在时跳过**（不覆盖），并把该条列为冲突，由人工判断。
- 只处理「看起来像报告目录」的条目（2 段或 3 段、且含 report.html），
  不认识的目录原样不动。
- 幂等：迁移后再跑一次，计划为空。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fundamental_agent.storage import _DEFAULT_BASE, _PROJECT_ROOT  # noqa: E402


def _backup_root(base: Path) -> Path:
    """备份放在报告根的**同级**目录。

    不用固定的 `output/reports_migrate_backup`：`--base` 可指向任意目录，
    备份必须跟着数据走（否则临时根的备份会跑到仓库里，反之亦然）。
    """
    return base.parent / (base.name + "_migrate_backup")


def _is_user_dir(name: str) -> bool:
    """`u{N}` 形式（已迁移过的目录，必须跳过，否则会搬进自己里面）。"""
    if not name.startswith("u"):
        return False
    return name[1:].isdigit() and len(name) > 1


def _find_candidates(base: Path) -> list[tuple[Path, str]]:
    """产出 (目录, 相对段路径) 的候选清单。

    段数只认 2 段 `{code}/{date}` 与 3 段 `{code}/{type}/{date}`；
    顶层 `u{N}` 目录整体跳过；必须有 report.html 才算候选。
    """
    out: list[tuple[Path, str]] = []
    if not base.is_dir():
        return out
    for child in sorted(base.iterdir()):
        if not child.is_dir() or _is_user_dir(child.name):
            continue
        for d in sorted(child.rglob("*")):
            if not d.is_dir() or not (d / "report.html").is_file():
                continue
            rel = d.relative_to(base)
            if len(rel.parts) not in (2, 3):
                continue      # 非报告目录：不认识的层级结构，保持原样
            out.append((d, rel.as_posix()))
    return out


def _resolve_admin_id(db_path: Path) -> int | None:
    """从 auth.db 取 admin 的 user id（取不到返回 None）。"""
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id FROM users WHERE username = 'admin' ORDER BY id LIMIT 1"
            ).fetchone()
            return int(row["id"]) if row else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _dest(base: Path, user_id: int, rel: str) -> Path:
    return base / ("u%d" % user_id) / Path(rel)


def _stamp_meta_user(dir_path: Path, user_id: int) -> None:
    """给 meta.json 补/改 user_id（保留其余字段与缩进风格）。"""
    p = dir_path / "meta.json"
    if not p.is_file():
        return
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 单条损坏不该让整个迁移失败
        return
    if not isinstance(obj, dict):
        return
    if obj.get("user_id") == user_id:
        return
    obj["user_id"] = user_id
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="迁移历史报告产物到按用户隔离的目录（F-06 D7）")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只打印计划）")
    ap.add_argument("--user-id", type=int, default=None,
                    help="归属用户 id；默认取 auth.db 里的 admin")
    ap.add_argument("--base", default=None, help="报告根目录，默认 output/reports")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（不建议）")
    args = ap.parse_args(argv)

    base = Path(args.base) if args.base else _DEFAULT_BASE
    if not base.is_dir():
        print("报告根不存在，无需迁移：%s" % base)
        return 0

    user_id = args.user_id
    if user_id is None:
        user_id = _resolve_admin_id(_PROJECT_ROOT / "data" / "auth.db")
        if user_id is None:
            print("无法从 data/auth.db 解析 admin 的 id —— 请显式传 --user-id")
            return 2
        print("归属用户：admin (id=%d)（可用 --user-id 覆盖）" % user_id)

    candidates = _find_candidates(base)
    if not candidates:
        print("没有待迁移的历史产物（幂等：可能已迁移过）")
        return 0

    print("\n计划迁移 %d 份报告到 u%d/：" % (len(candidates), user_id))
    plan, conflicts = [], []
    for d, rel in candidates:
        target = _dest(base, user_id, rel)
        if target.exists():
            conflicts.append((d, rel, target))
            continue
        plan.append((d, rel, target))

    for d, rel, target in plan:
        print("    %-52s → %s" % (rel, target.relative_to(base).as_posix()))
    for d, rel, target in conflicts:
        print("    [跳过] %-45s 目标已存在，未覆盖" % rel)

    if not args.apply:
        print("\n（dry-run）未改动任何文件。确认无误后加 --apply 执行。")
        return 0

    if plan and not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = _backup_root(base) / stamp
        try:
            shutil.copytree(base, backup)
        except Exception as e:  # noqa: BLE001 — 备份失败必须中止，不能裸搬
            print("\n备份失败，已中止迁移：%s" % e)
            return 3
        print("\n已备份：%s" % backup)
    elif plan:
        print("\n⚠️ 已跳过备份（--no-backup）")

    moved = 0
    for d, rel, target in plan:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(target))
        _stamp_meta_user(target, user_id)
        moved += 1
    print("\n已迁移 %d 份；跳过 %d 份。" % (moved, len(conflicts)))
    print("提示：只迁移了目录内容，源目录若已空可自行清理；"
          "stamp 后的 meta.user_id 用于审计，权限仍由目录位置决定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
