"""报告库读取侧：持久化报告标识 + 库视图（F-06）。

`storage.py` 负责「写」，本模块是它的**读取侧配对物**：扫描磁盘、归一化 meta、
把「一份报告」表达成可长期引用的 `report_id`，并按当前用户隔离。放在 `storage.py`
同级，正是为了体现这层配对关系。

设计要点（依据 `docs/F-06-我的报告库-设计方案.md`）：

- **文件即索引**：不建索引表。磁盘上的 `meta.json` 已是权威，再建一份索引等于制造
  两份真相（落盘成功但 INSERT 失败 → 列表缺条目；文件被删但表还在 → 死链）。
  扫描封装在本模块内部，将来量大换 SQLite 只改函数内部，接口与调用方不变。
- **`report_id` 是「用户相对」标识**：`{code}.{report_type}.{date}`，**不含 `user_id`**。
  解析始终以「当前用户根」为基准，因此**越权在结构上不可能发生** —— 不需要额外的
  权限判断，也就不存在「忘了判断权限」的风险。
- **未知 `report_type` 保留不丢弃**：label 退回 key、排在末尾。否则将来新增报告类型时，
  产物会「凭空消失」（与 N-07「失败不许伪装成不存在」同一取向）。
- **不假设日期目录是 8 位纯数字**：实测存在 `20260922_morning_noLLM`（外部产物）。
- **段数决定类型**：2 段 = 基本面（type 段缺省），3 段 = 其他；其他段数跳过不猜。
- **⚠️ 基本面目录有两种合法形态，解析必须同时接受**（N-21，2026-09-26 修复）：
  `{code}/{date}/`（2 段，type 段缺省）与 `{code}/fundamental/{date}/`（3 段）。
  本模块原先**只按 2 段反推**基本面路径，但**实际唯一的写路径**
  （`api/server.py::_worker` + `storage.save_report`）与 CLI 都显式传
  `report_type="fundamental"`（`schemas.ReportRequest.report_type` 默认值即
  `"fundamental"`，`main.py --type` 亦然）→ 落盘恒为 3 段。
  于是「列表按目录名反解出 `{code}.fundamental.{date}`、view 却去找 2 段目录」
  → **报告库里所有基本面报告点开必 404**（列表广告了 view 结构性拒绝的条目）。
  详见 `docs/产品功能扩展建议清单.md` 的 N-21。
- **空态 / 不可用态分离**：用户根不可读返回 `root_available=False`，**不是**「没有报告」。
- **删除侧与读取侧共用路径解析**（2026-09-26 新增 `delete_reports`）：`view` 与 `delete`
  都走 `resolve_report_dir`，因此「列表里点得开的」与「删得掉的」永远是同一集合。
  唯一的**有意不一致**是 legacy：读取允许（CLI / 巡检），删除**一律拒绝**（共享目录，
  删它等于跨用户删除）—— 列表项用服务端下发的 `deletable` 表达这条差异。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from fundamental_agent.storage import _DEFAULT_BASE

# ===== 类型注册表（单一事实源） =====
#
# 这四个中文名此前散落在 6 处以上硬编码（前端按钮、模板 <title>、schemas 注释各写各的）。
# 报告库要按类型组织列表，正好把它收敛到这一处：后端下发 `report_type_label`，前端不硬编码。
REPORT_TYPES: dict[str, dict[str, Any]] = {
    "fundamental": {"label": "基本面分析", "has_code": True},
    "overview": {"label": "公司概况", "has_code": True},
    "industry": {"label": "行业对比", "has_code": True},
    "board": {"label": "板块深度", "has_code": False},
}

# 分区与类型条的固定顺序（与用户在详情页看到的能力顺序一致）
REPORT_TYPE_ORDER: list[str] = ["fundamental", "overview", "industry", "board"]

# 列表项「查看标的主体」的落点页。board 没有个股页，只能回板块页。
SUBJECT_PAGE: dict[str, str] = {
    "fundamental": "/stock_detail.html",
    "overview": "/stock_detail.html",
    "industry": "/stock_detail.html",
    "board": "/sector_detail.html",
}

# 基本面报告在落盘时 report_type 段缺省（storage.report_output_path 的 2 段形态）
FUNDAMENTAL_KEY = "fundamental"

# ===== 三段白名单（逐段校验，缺一不可；见方案 §7.4） =====
_CODE_RE = re.compile(r"^\d{6}$")
_BK_RE = re.compile(r"^BK\d+$")
_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
# 日期段：只校验非空与字符集，**不做「必须 8 位数字」的假设**；
# 且明确排除 `.` `/` `\`（防路径穿越 / 与 report_id 分隔符冲突）
_DATE_RE = re.compile(r"^[A-Za-z0-9_\-]{4,32}$")
_USER_DIR_RE = re.compile(r"^u\d+$")

_ID_SEP = "."


def is_code(s: str) -> bool:
    """6 位纯数字个股代码（与 api/server.py 的 _code_ok 同口径）。"""
    return bool(s) and bool(_CODE_RE.match(s))


def is_bk(s: str) -> bool:
    """板块代码 BKxxxx（与 api/server.py 的 _bk_ok 同口径）。"""
    return bool(s) and bool(_BK_RE.match(s))


def is_subject(s: str) -> bool:
    """合法的报告标的主体：个股代码或板块代码。"""
    return is_code(s) or is_bk(s)


def type_label(key: str) -> str:
    """类型中文名。**未知类型退回 key 本身**（不丢弃、不报错）。"""
    meta = REPORT_TYPES.get(key)
    return str(meta["label"]) if meta else key


def has_code(key: str) -> bool:
    """该类型是否有「个股主体」可跳（board 为 False）。

    未知类型保守取 True：主体若是 `BKxxxx`，`_subject_url` 仍会因板块类型判定失败而拒发，
    而未知类型的 6 位个股代码能正常跳详情页。
    """
    meta = REPORT_TYPES.get(key)
    if meta is None:
        return True
    return bool(meta.get("has_code"))


def type_sort_key(key: str) -> tuple[int, str]:
    """已注册类型按 REPORT_TYPE_ORDER 排；未知类型一律排末尾。"""
    try:
        return (REPORT_TYPE_ORDER.index(key), key)
    except ValueError:
        return (len(REPORT_TYPE_ORDER), key)


# ===== 根目录与 report_id =====

def user_root(user_id: int | None, base: str | Path | None = None) -> Path:
    """当前用户的报告根：`output/reports/u{user_id}/`。

    user_id 为空（CLI / 迁移脚本）→ 返回 legacy 根 `output/reports/`。
    注意：**view 的越权检查必须以本函数的结果为界**，用 `_DEFAULT_BASE` 会落空。
    """
    root = Path(base) if base else _DEFAULT_BASE
    return root / ("u%d" % int(user_id)) if user_id else root


def encode_report_id(code: str, report_type: str, date_seg: str) -> str:
    """`{code}.{report_type}.{date}` —— 三段点分。

    不用原始相对路径：含 `/` 会与路由段冲突，且更易被路径穿越。
    """
    return _ID_SEP.join([code, report_type or FUNDAMENTAL_KEY, date_seg])


def parse_report_id(report_id: str) -> tuple[str, str, str] | None:
    """解析并校验 report_id；任何不合法一律返回 None（调用方转 404，不回显路径）。"""
    if not report_id:
        return None
    parts = report_id.split(_ID_SEP)
    if len(parts) != 3:
        return None
    code, report_type, date_seg = parts
    if not is_subject(code):
        return None
    if not _TYPE_RE.match(report_type):
        return None
    if not _DATE_RE.match(date_seg):
        return None
    return code, report_type, date_seg


def report_dir_candidates(user_id: int | None, code: str, report_type: str, date_seg: str,
                          base: str | Path | None = None) -> list[Path]:
    """该 report_id 可能对应的**全部**报告目录，按优先级排列。

    为什么会有多个候选：`report_type` 段在磁盘上**可缺省**，即基本面报告存在
    `{code}/{date}/`（2 段）与 `{code}/fundamental/{date}/`（3 段）两种合法形态
    （见模块 docstring 的 N-21 说明 —— 实际写路径恒为 3 段，2 段是为向后兼容保留）。

    优先级：**带类型段（3 段）在前**，因为当前两条写路径都显式传 `"fundamental"`；
    万一两种形态同时存在，取更具体的那份（避免误打开一份手工放置的旧产物）。
    """
    root = user_root(user_id, base)
    cands: list[Path] = []
    if report_type:
        cands.append(root.joinpath(code, report_type, date_seg))
    if not report_type or report_type == FUNDAMENTAL_KEY:
        cands.append(root.joinpath(code, date_seg))
    return cands


def report_dir(user_id: int | None, code: str, report_type: str, date_seg: str,
               base: str | Path | None = None) -> Path:
    """报告目录（**首选**候选）；解析请用 `report_dir_candidates` 逐个尝试。"""
    return report_dir_candidates(user_id, code, report_type, date_seg, base)[0]


def report_id_for_path(html_path: str | Path, code: str, report_type: str) -> str:
    """由**已落盘**的 `report.html` 路径反推 `report_id`；不可反解时返回 `""`。

    用途：F-04 报告任务中心在任务 SUCCESS 时下发一个**持久**链接（`TaskStatus.report_id`）。
    任务状态本身活在内存里，任务中心若用 `task_id` 拼链接，8002 一重启链接即失效 ——
    这正是 F-06 交付时修掉的缺陷，不能在任务中心里重新引入。

    为什么必须**从目录名反推**、而不是按 `(code, report_type, 今天)` 现算：日期段可能带
    `__{bk}` 后缀（D8，同日不同对比基准隔离），BC 端现算会造出一个指向不存在目录的 id。
    落盘与展示必须来自同一个事实（真实目录名），否则「任务中心给了个点开就 404 的链接」
    会以最坏的方式重现 N-21（列表广告了详情拒绝的条目）。

    末位做**回环校验**（`parse_report_id`）：段名非法时返回 `""`，宁可不给链接。
    """
    seg = Path(str(html_path)).parent.name
    rid = encode_report_id(code, report_type, seg)
    return rid if parse_report_id(rid) is not None else ""


def resolve_report_dir(user_id: int | None, report_id: str,
                       base: str | Path | None = None) -> Path | None:
    """把 report_id 解析为**当前用户根内**已存在的报告目录；不满足一律 None。

    这是 **view（打开）与 delete（删除）共用的唯一路径解析入口** —— 两侧必须走同一套
    护栏，否则会出现「列表里点得开的却删不掉」或更糟的「看不到的反而删得掉」。

    护栏（三条缺一不可）：
      1. 三段白名单校验（`parse_report_id`）——`..` / `/` / `\\` 在字符集层面已被排除；
      2. `resolve()` 后断言仍在**该用户根**之内（防御性，防符号链接与手工放置的目录）；
      3. `report.html` 必须真实存在 —— 「目录在」不等于「这里有一份报告」，少了这条，
         手工放在用户根下的同名目录会被当成可删报告。

    基本面报告有两种合法目录形态（3 段 / 2 段），故按 `report_dir_candidates`
    **逐个候选**做上述三条检查，命中即返回。**只查 2 段**会让列表里每一条基本面
    报告都点不开（N-21）。

    `user_id` 为空时基准是 legacy 根 `output/reports/`：读取侧允许（CLI / 巡检用），
    但**删除侧一律拒绝**（见 `delete_reports` 的第 1 条约束）。
    """
    parsed = parse_report_id(report_id)
    if parsed is None:
        return None
    code, report_type, date_seg = parsed
    root = user_root(user_id, base)
    try:
        root_resolved = root.resolve()
    except OSError:
        return None
    for d in report_dir_candidates(user_id, code, report_type, date_seg, base):
        try:
            resolved = (d / "report.html").resolve()
        except OSError:
            continue
        if not _is_within(resolved, root_resolved):
            continue
        if resolved.is_file():
            return d
    return None


def resolve_report_html(user_id: int | None, report_id: str,
                        base: str | Path | None = None) -> Path | None:
    """把 report_id 解析为**当前用户根内**已存在的 report.html；不满足一律 None。

    路径护栏全部在 `resolve_report_dir` 里（view 与 delete 共用），本函数只做最后一跳。
    """
    d = resolve_report_dir(user_id, report_id, base)
    return (d / "report.html") if d is not None else None


def describe_report(user_id: int | None, report_id: str,
                    base: str | Path | None = None) -> dict | None:
    """把 `report_id` 解析成**归一化描述**；解析不通过返回 None。

    用途（F-07 导出）：导出端点需要「这份报告叫什么、是什么类型、哪天生成的」来拼
    文件名与 Word 标题。这些都是**列表项的展示信息**，必须与列表同源 —— 否则会出现
    「列表里写着『贵州茅台 · 公司概况』，下载下来叫 `600519_overview`」，或者更糟的
    「列表有名字、导出名叫 report.pdf」。

    与 `resolve_report_html` 一样，路径护栏全部复用 `resolve_report_dir`（view / delete /
    export 三方共用的唯一入口）——于是「列表里点得开的」「删得掉的」「导得出的」永远是
    同一集合，不存在第二条解析路径。

    返回键：`report_id / code / report_type / report_type_label / date_seg /
    name / bk / bk_name / report_dir`。`report_dir` 是给调用方直接拿去导出的目录
    （避免再解析一次）。
    """
    parsed = parse_report_id(report_id)
    if parsed is None:
        return None
    d = resolve_report_dir(user_id, report_id, base)
    if d is None:
        return None
    code, report_type, date_seg = parsed
    meta = _read_meta(d) or {}
    return {
        "report_id": report_id,
        "code": code,
        "report_type": report_type,
        "report_type_label": type_label(report_type),
        "date_seg": date_seg,
        "name": str(meta.get("name") or ""),
        "bk": str(meta.get("bk") or ""),
        "bk_name": str(meta.get("bk_name") or ""),
        "report_dir": d,
    }


def _is_within(child: Path, parent: Path) -> bool:
    """child 是否在 parent 之内（含相等）。Python 3.9+ 的 is_relative_to 等价实现。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# ===== 删除（F-06 扩展，2026-09-26）：报告库的手动清理出口 =====
#
# 这是本模块**唯一会改动磁盘**的函数组。它比读取危险一个量级：读错了只是显示不对，
# 删错了不可逆。因此它的每一条约束都要能回答「删错会怎样」。

# 单次删除请求接受的条数上限。作用是给请求体一个硬上界（防止超大 payload），
# 不是业务规则 —— 与 `GET /api/report/list` 的 page_size 上限 100 同量级，
# 取 200 是因为批量清除要能一次覆盖某类报告的全部（列表上限 100/页，两页足够）。
MAX_DELETE_BATCH = 200


def _prune_empty_parents(d: Path, root: Path) -> int:
    """自底向上删除**空**目录，到 root 为止（不含 root 本身）。返回删除的目录数。

    只删空目录（`os.rmdir` 仅在空时成功），因此**不会误伤同 code 下的其他报告**：
    只要同级还有别的类型 / 日期，`rmdir` 立刻失败并停止上溯。

    边界由 `_is_within` 兜底：路径一旦不在用户根内立即停手，绝不碰 root 以上的任何东西。
    """
    n = 0
    try:
        root_r = root.resolve()
    except OSError:
        return 0
    p = d.parent
    while True:
        try:
            pr = p.resolve()
        except OSError:
            break
        if pr == root_r or not _is_within(pr, root_r):
            break
        try:
            p.rmdir()
        except OSError:
            break  # 非空（或权限不足）→ 停，这是正常的结束条件，不是错误
        n += 1
        p = p.parent
    return n


def delete_reports(
    user_id: int | None,
    report_ids: list[str],
    *,
    base: str | Path | None = None,
    busy_keys: set[tuple[str, str]] | None = None,
) -> dict:
    """**物理删除**当前用户的若干份已落盘报告（整个报告目录，不可恢复）。

    ⚠️ 五条硬约束（缺一条就会造成不可逆的数据损失）：

      1. **`user_id` 为空一律拒绝**（抛 `ValueError`）。空值会把基准落到 legacy 根
         `output/reports/` —— 那是**共享**目录（`include_legacy=true` 能列出别人的产物），
         删它等于跨用户删除。**读取侧可以宽松（view 允许 legacy），删除侧必须严格**：
         这是 view 与 delete 唯一有意不一致的地方，测试里单独锁一条用例。
      2. **路径解析与 view 共用 `resolve_report_dir`**，因此「列表里点得开的」与
         「删得掉的」永远是同一集合。删不到 legacy 条目**不是缺陷而是设计**（与 N-22
         「不放宽 view 路径边界」取向一致）。
      3. **只删报告目录本身**，随后自底向上清理**空**目录。绝不递归删父目录：
         `{code}/` 下可能还有别的类型 / 日期的报告。
      4. **活跃任务守卫**（`busy_keys`）：`(主体, 报告类型)` 正在生成中 → 跳过并计入
         `skipped_busy`。删掉正在写的目录会让 worker 的 `save_report()` 中途失败，
         或写出一个「用户以为已经删掉」的目录 —— 这是「活跃任务不可删」这条约束的
         第三个入口（前两个：任务中心的删除、`_evict_locked` 的容量淘汰）。
      5. **失败的必须如实回报**（`failed` 带 report_id 与原因）。批量场景下允许部分成功
         （比整批拒绝更有用），但**绝不静默少删** —— 「点了删除，条数没变」是最难排查的
         一类故障，而它恰恰是静默吞错的标准症状。

    返回 `{"deleted", "not_found", "failed":[{report_id, error}],
    "skipped_busy":[report_id], "removed_ids":[report_id], "pruned": int}`。

    `removed_ids` 交给调用方（`api/server.py`）用于清理任务中心里指向同一份报告的
    记录 —— 否则任务中心会留着一个 404 的 `view_url`（N-21 形状：广告了详情拒绝的条目）。
    """
    if not user_id:
        raise ValueError("delete_reports 拒绝 user_id 为空：legacy 根是共享目录，禁止删除")

    raw = [x for x in (report_ids or [])[:MAX_DELETE_BATCH] if isinstance(x, str) and x]
    # 去重且保序：重复 id 只处理一次（第二次必然 not_found，会把计数污染成「删了却没生效」）
    seen: set[str] = set()
    uniq: list[str] = []
    for rid in raw:
        if rid in seen:
            continue
        seen.add(rid)
        uniq.append(rid)

    busy = busy_keys or set()
    root = user_root(user_id, base)
    out: dict[str, Any] = {
        "deleted": 0,
        "not_found": 0,
        "failed": [],
        "skipped_busy": [],
        "removed_ids": [],
        "pruned": 0,
    }

    for rid in uniq:
        parsed = parse_report_id(rid)
        if parsed is None:
            out["not_found"] += 1
            continue
        code, report_type, _date_seg = parsed
        if (code, report_type) in busy:
            out["skipped_busy"].append(rid)
            continue
        d = resolve_report_dir(user_id, rid, base)
        if d is None:
            out["not_found"] += 1
            continue
        try:
            shutil.rmtree(d)
        except OSError as e:
            out["failed"].append({"report_id": rid, "error": str(e)[:200]})
            continue
        out["deleted"] += 1
        out["removed_ids"].append(rid)
        out["pruned"] += _prune_empty_parents(d, root)
    return out


# ===== 扫描 =====

def _walk_report_dirs(root: Path, prune_top=None):
    """产出 root 下「直接含 report.html 或 meta.json」的目录：(相对段元组, 目录)。

    prune_top 是一元谓词，用于在**顶层**剪枝（legacy 扫描时排除 `u{N}` 用户目录）。
    os.walk 允许就地修改 dirnames 来剪枝。
    """
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if d == root:
            if prune_top:
                dirnames[:] = [n for n in dirnames if not prune_top(n)]
            continue
        rel = d.relative_to(root).parts
        if prune_top and len(rel) == 1 and prune_top(rel[0]):
            dirnames[:] = []
            continue
        if "report.html" in filenames or "meta.json" in filenames:
            yield rel, d


def _read_meta(d: Path) -> dict | None:
    """读 meta.json；缺失/损坏返回 None（调用方跳过该条并计入 damaged）。"""
    p = d / "meta.json"
    if not p.is_file():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 单条损坏不该拖垮整个列表
        return None
    return obj if isinstance(obj, dict) else None


def _validation_warnings(meta: dict) -> int:
    """个股报告的校验告警条数（board 报告无 validation 字段 → 0）。"""
    v = meta.get("validation")
    if not isinstance(v, dict):
        return 0
    w = v.get("warnings")
    return len(w) if isinstance(w, list) else 0


def subject_url(report_type: str, code: str) -> str:
    """标的主体页链接。**只在标的主体合法时下发**。

    这条护栏直接堵住了 `stock_detail.html` 的 `|| '600519'` 默认值陷阱
    （缺 code 时静默打开贵州茅台）。前端因此不需要做任何类型判断。

    公开入口：报告库列表（`_build_item`）与 F-04 任务中心（`api/server.py`）共用，
    保证两处对"板块报告没有个股页"的判断永远一致。
    """
    if report_type == "board":
        if not is_bk(code):
            return ""
        page = SUBJECT_PAGE["board"]
    else:
        if not is_code(code):
            return ""
        page = SUBJECT_PAGE.get(report_type) or SUBJECT_PAGE["fundamental"]
    return "%s?code=%s" % (page, code)


# 兼容旧引用（内部 `_build_item` 与 `tests/test_report_index.py` 用旧名）
_subject_url = subject_url


def _build_item(rel: tuple[str, ...], d: Path, meta: dict,
                user_id: int | None, legacy: bool = False) -> dict | None:
    """把 (相对段, 目录, meta) 归一化成列表项；不可建 report_id 时返回 None。"""
    if len(rel) == 2:
        code, date_seg = rel
        report_type = FUNDAMENTAL_KEY
    elif len(rel) == 3:
        code, report_type, date_seg = rel
    else:
        return None  # 非报告目录：跳过并计入 skipped，不猜

    # 字符集护栏：任一段不合法就无法构造可回解析的 report_id → 跳过
    if not is_subject(code) or not _TYPE_RE.match(report_type) or not _DATE_RE.match(date_seg):
        return None

    report_id = encode_report_id(code, report_type, date_seg)
    # 回环校验：保证 view 端点能百分百反解（避免列表里出现点开就 404 的条目）
    if parse_report_id(report_id) is None:
        return None

    html = d / "report.html"
    try:
        size_kb = int(round(html.stat().st_size / 1024.0))
    except OSError:
        return None  # report.html 缺失 → 点不开，不进列表

    generated_at = str(meta.get("generated_at") or "").strip()
    try:
        mtime = d.stat().st_mtime
    except OSError:
        mtime = 0.0

    return {
        "report_id": report_id,
        "report_type": report_type,
        "report_type_label": type_label(report_type),
        "code": code,
        "name": str(meta.get("name") or ""),
        "bk": str(meta.get("bk") or ""),
        "bk_name": str(meta.get("bk_name") or ""),
        "generated_at": generated_at,
        "data_source": str(meta.get("data_source") or ""),
        "writer_mode": str(meta.get("writer_mode") or ""),
        # meta.user_id 是**对账与审计**用，不是权限依据（权限由路径决定）
        "user_id": meta.get("user_id"),
        "llm_ok": meta.get("llm_ok"),
        "validation_warnings": _validation_warnings(meta),
        "chart_offline": meta.get("chart_offline"),
        "size_kb": size_kb,
        "view_url": "/api/report/view/%s" % report_id,
        "subject_url": _subject_url(report_type, code),
        # 能否删除由**服务端**下发，前端不自建判断（与 subject_url 同一取向）：
        # legacy 条目位于共享根 `output/reports/`，不属于任何用户，删除侧
        # （`delete_reports`）结构上就拒绝它们 —— 若前端仍渲染删除按钮，
        # 用户的体验是「点了没反应」，正是最该避免的那类无提示失败。
        "deletable": not legacy,
        "_mtime": mtime,  # 排序兜底；出口前会被剔除
    }


def _scan(root: Path, user_id: int | None, prune_top=None,
          legacy: bool = False) -> tuple[list[dict], dict]:
    """扫描一个**已确认可读**的根，返回 (items, stats)。"""
    stats = {"damaged": 0, "skipped": 0, "owner_mismatch": 0}
    items: list[dict] = []

    for rel, d in _walk_report_dirs(root, prune_top=prune_top):
        meta = _read_meta(d)
        if meta is None:
            # 目录里有 report.html 却没有可用 meta → 元信息损坏，跳过并留痕
            if (d / "report.html").is_file():
                stats["damaged"] += 1
            continue
        item = _build_item(rel, d, meta, user_id, legacy=legacy)
        if item is None:
            stats["skipped"] += 1
            continue
        if user_id and item.get("user_id") not in (None, "", user_id):
            # 路径说属于本人、meta 说不是 → 以路径为准但记一笔，便于发现误搬/误迁移
            stats["owner_mismatch"] += 1
        items.append(item)
    return items, stats


def _scan_safe(root: Path, user_id: int | None, prune_top=None,
               legacy: bool = False) -> tuple[list[dict], dict, bool]:
    """带可用性判定的扫描：返回 (items, stats, root_available)。

    **空态与不可用态必须分开**（本项目已两次栽在这上面）：
      - 根不存在 → 全新用户 / 尚未生成过报告 → `available=True` + 空列表（空态）；
      - 根存在但不可读（权限/IO）→ `available=False`（不可用态，**不是「没有报告」**）。
    """
    stats = {"damaged": 0, "skipped": 0, "owner_mismatch": 0}
    try:
        if not root.exists():
            return [], stats, True
        if not root.is_dir():
            return [], stats, False  # 被同名文件占位 → 视为不可用
        if not os.access(root, os.R_OK | os.X_OK):
            return [], stats, False
        items, stats = _scan(root, user_id, prune_top=prune_top, legacy=legacy)
        return items, stats, True
    except OSError:
        return [], stats, False


def list_reports(
    user_id: int | None,
    *,
    base: str | Path | None = None,
    report_type: str = "",
    code: str = "",
    page: int = 1,
    page_size: int = 20,
    sort: str = "generated_at",
    include_legacy: bool = False,
) -> dict:
    """列出当前用户的已落盘报告（库视图）。

    - `types` 是**过滤前的全量计数镜像**（否则筛选后 chip 数字全为 0，用户切不回去）；
    - `items` 是过滤 + 分页后的结果；
    - `root_available=False` 表示用户根不可读 —— **不是「没有报告」**（前端文案必须分开）。
    """
    root = user_root(user_id, base)
    all_items, stats, root_available = _scan_safe(root, user_id)

    legacy_items: list[dict] = []
    if include_legacy and user_id:
        # legacy 根 = output/reports/，但必须排除其它用户的 u{N} 目录
        # legacy=True → 这些条目的 `deletable=False`（共享目录，删除侧一律拒绝）
        legacy_items, legacy_stats, legacy_ok = _scan_safe(
            Path(base) if base else _DEFAULT_BASE, user_id,
            prune_top=lambda n: bool(_USER_DIR_RE.match(n)),
            legacy=True,
        )
        for k, v in legacy_stats.items():
            stats[k] = stats.get(k, 0) + v
        if not legacy_ok:
            root_available = False

    merged = all_items + legacy_items
    # 去重：同一 report_id 以用户根那份为准（legacy 与个人根理论上不会重名）
    seen: set[str] = set()
    deduped: list[dict] = []
    for it in merged:
        if it["report_id"] in seen:
            continue
        seen.add(it["report_id"])
        deduped.append(it)

    # types：过滤前的全量镜像
    counts: dict[str, int] = {}
    for it in deduped:
        counts[it["report_type"]] = counts.get(it["report_type"], 0) + 1
    types = [
        {"key": k, "label": type_label(k), "count": counts[k],
         "has_code": has_code(k)}
        for k in sorted(counts.keys(), key=type_sort_key)
    ]

    items = deduped
    if report_type:
        items = [it for it in items if it["report_type"] == report_type]
    if code:
        items = [it for it in items if it["code"] == code]

    if sort == "size":
        items.sort(key=lambda it: it.get("size_kb") or 0, reverse=True)
    else:
        # generated_at 可能缺失（外部/早期产物）→ 用目录 mtime 兜底，
        # 否则 sort 比较 None 会抛 TypeError
        items.sort(key=lambda it: (it.get("generated_at") or "", it.get("_mtime") or 0.0),
                   reverse=True)

    total = len(items)
    # 分页参数**显式钳制**（不靠 `or` 的短路，否则 page_size=0 会被悄悄换成默认值，
    # 客户端传错参数时看不出任何异常）。非法/缺失统一回落默认值。
    try:
        page_v = int(page)
    except (TypeError, ValueError):
        page_v = 1
    try:
        size_v = int(page_size)
    except (TypeError, ValueError):
        size_v = 20
    page = max(1, page_v)
    page_size = min(100, max(1, size_v))
    start = (page - 1) * page_size
    page_items = items[start:start + page_size]
    for it in page_items:
        it.pop("_mtime", None)

    return {
        "ok": True,
        "total": total,
        "count": len(page_items),
        "page": page,
        "page_size": page_size,
        "root_available": root_available,
        "include_legacy": bool(include_legacy),
        "owner": {
            "user_id": user_id,
            "scope": "personal" if user_id else "shared",
        },
        "types": types,
        "items": page_items,
        "warnings": stats,
    }
