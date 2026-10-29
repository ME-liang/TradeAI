"""FastAPI 服务：把「生成报告」暴露为异步 HTTP 接口。

启动：
    uvicorn fundamental_agent.api.server:app --host 0.0.0.0 --port 8000 --reload

调用顺序（前端）：
    1) POST /api/report  {"code":"600519"}            -> 202 {task_id, status}
    2) GET  /api/report/{task_id}                      -> 轮询 status/progress/stage
    3) GET  /api/report/{task_id}/html                 -> 200 text/html（SUCCESS 后）
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import threading
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from .schemas import (
    ReportRemoveRequest,
    ReportRequest,
    TaskRemoveRequest,
    TaskStatus,
    WatchAddRequest,
    WatchListResponse,
    WatchOrderRequest,
    WatchRemoveRequest,
)
from .tasks import ACTIVE_STATUSES as ACTIVE_TASK_STATUSES
from .tasks import TaskManager
from fundamental_agent.main import run_pipeline

# 项目根（src/fundamental_agent/api/server.py → 上溯 3 级）；config.yaml 的读取基准
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
from fundamental_agent.storage import industry_date_suffix, report_output_path, save_report
from fundamental_agent import report_index
from fundamental_agent import report_export
from fundamental_agent.auth import service as auth_service
from fundamental_agent.auth import watchlist as watchlist_service
from fundamental_agent.auth.db import REMEMBER_TTL_DAYS, SESSION_TTL_DAYS

# server.py -> api -> fundamental_agent -> src -> project_root
PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = PROJECT_ROOT / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="公司基本面分析 Agent API", version="0.1.0")
tm = TaskManager()

# CORS（方案阶段 1: APP 跨域调用需要）。Cookie 走前端 8000 同源代理, 此处不放开凭据。
#
# expose_headers：导出接口（F-07）把「实际字节数 / 图表告警」放在响应头里，因为响应体是
# **文件字节**，没有位置再放结构化信息。同源代理（8000）下这些头本就可读，但直连 8002
# 的调试/第三方客户端不受此保护 —— 不显式 expose，浏览器只会给一个"空白的成功"。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Disposition",
        "X-Export-Format",
        "X-Export-Bytes",
        "X-Export-Warnings",
    ],
)


def _code_ok(code: str) -> bool:
    return bool(code) and code.isdigit() and len(code) == 6


def _bk_ok(bk: str) -> bool:
    import re as _re
    return bool(_re.fullmatch(r"BK\d+", bk or ""))


def _unlink_quiet(p: str | os.PathLike) -> None:
    """尽力删除一个临时文件；失败不抛（用于导出临时产物与 background cleanup）。

    为什么必须"安静"：它同时作为 `BackgroundTask` 在响应**发送完毕之后**执行 —— 那时
    已经没有任何地方能承接异常，抛出来只会变成一条无意义的 ASGI 报错；而临时目录由
    操作系统回收，删不掉只是延迟释放，不影响正确性。
    """
    try:
        Path(p).unlink()
    except OSError:
        pass


# 大陆手机号(注册接口服务端二次校验用, 与 fundamental_agent.auth.service 同口径)
_PHONE_RE = re.compile(r"^1\d{10}$")


def _reuse_today(code: str, report_type: str, bk: str = "",
                 user_id: int | None = None) -> str | None:
    """性能优化·措施4：同一 user+code+report_type+bk 当日已有 SUCCESS 产物时，返回其 html 路径。

    命中条件（全部满足）：
      - 当日落盘路径 output/reports/[u{user_id}/]{code}/[{report_type}/]{date}[__bk]/report.html 存在；
      - 同目录 meta.json 存在且 report_type 与请求一致；
      - report_type=industry 时 meta.bk 必须与请求 bk 一致（早于 bk 落盘的历史产物
        仅在请求 bk 为空时命中，避免误复用其他对比基准的旧报告）；
      - F-06 D1：meta.user_id 必须与请求者一致 —— **二次校验**。路径已按用户隔离，
        这里是对账，用于发现被手工搬动/迁移脚本误放的产物，避免静默错归属。
    命中则直接复用，不启动新线程、不重复取数、不重复消耗 LLM。
    返回 None 表示需要重新生成。任何读取异常都按「未命中」处理，绝不阻塞请求。
    """
    try:
        if not _code_ok(code) or not report_type:
            return None
        html = report_output_path(
            code, report_type=report_type, user_id=user_id,
            date_suffix=industry_date_suffix(report_type, bk),
        )
        if not html.exists():
            return None
        meta_p = html.parent / "meta.json"
        if not meta_p.exists():
            return None
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        if str(meta.get("report_type") or "") != report_type:
            return None
        if report_type == "industry" and str(meta.get("bk") or "") != str(bk or ""):
            return None
        # F-06 D1：归属二次校验（路径隔离为主，此处用于对账）
        if meta.get("user_id") not in (None, "", user_id):
            return None
        # Step 4：overview 产物按成稿模式（contract/direct）区分复用，切模式不误命中旧产物
        if report_type == "overview":
            try:
                with open(_PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
                    cur_mode = str(
                        ((yaml.safe_load(f) or {}).get("overview_writer") or {}).get("mode")
                        or "contract"
                    )
            except Exception:  # noqa: BLE001 — 读不到配置按 contract
                cur_mode = "contract"
            if str(meta.get("writer_mode") or "contract") != cur_mode:
                return None
        return str(html)
    except Exception:  # noqa: BLE001 — 复用判定失败只意味着多跑一次生成
        return None


def _disk_task_fields(html_path: str | Path) -> dict:
    """从**已落盘**的产物反查任务中心展示字段（`name` / `bk_name`）。

    只用于「当日产物复用」分支：那条分支不跑 pipeline，拿不到 `info`，若不回读 meta
    任务中心就只剩一串代码可显示。读不到就返回空 dict（展示降级，不阻断请求）。
    """
    out: dict = {}
    try:
        meta_p = Path(html_path).parent / "meta.json"
        if meta_p.is_file():
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            out["name"] = str(meta.get("name") or "")
            out["bk_name"] = str(meta.get("bk_name") or "")
    except Exception:  # noqa: BLE001 — 展示字段读不到不影响复用本身
        pass
    return out


def _worker(task_id: str, code: str, use_llm: bool, report_type: str = "fundamental",
            bk: str = "", bk_name: str = "", user_id: int | None = None) -> None:
    """后台线程：跑完整 pipeline，进度实时回写 TaskManager。

    user_id 必须与 `create_report` 传给 `TaskManager.create` 的一致，且必须与
    `report_output_path` / `save_report` 的两处调用一致 —— 否则 HTML 与 meta/profile
    会落到不同目录（`save_report` 会重算目录，不沿用 html_path 的所在目录）。

    **F-04**：每条 SUCCESS 路径都必须同时回填 `report_id`（持久）与 `name`（展示）。
    `report_id` 用 `report_index.report_id_for_path()` **从真实目录名反推**，不要按
    「今天」现算 —— 行业对比报告的日期段带 `__{bk}` 后缀，现算会得到打不开的 id。
    顺序上 `report_id` 在 `save_report` **之后**才赋值：SUCCESS 一旦对前端可见，
    报告库里就必须已经有这份产物（这正是「生成完成后自动进入报告库」的实现基础）。
    """
    tm.update(task_id, status="RUNNING", stage="初始化")

    def _on_progress(stage: str, pct: int) -> None:
        tm.update(task_id, stage=stage, progress=pct)

    try:
        # 板块深度报告：以板块代码为入口，独立于个股 pipeline（延迟导入，物理隔离）
        if report_type == "board":
            from fundamental_agent.report_types.board import (
                build_board_report_data,
                render_board,
            )
            html_path = str(report_output_path(bk, report_type="board", user_id=user_id))
            data = build_board_report_data(bk, bk_name=bk_name, use_llm=use_llm,
                                           progress=_on_progress)
            template_dir = str(Path(__file__).resolve().parents[1] / "report")
            render_board(data, template_dir=template_dir, out_path=html_path)
            meta = {
                "bk": bk,
                "name": data.get("bk_name", ""),
                "report_type": report_type,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "data_source": "eastmoney",
                "llm_ok": data.get("llm_ok", False),
                "user_id": user_id,                                    # F-06 D1：归属
                "chart_offline": data.get("chart_offline"),            # F-06 D4：图表库已内联
            }
            save_report(bk, html_path=html_path, meta=meta, report_type="board",
                        user_id=user_id)
            tm.update(task_id, status="SUCCESS", progress=100, stage="完成", html_path=html_path,
                      name=str(data.get("bk_name") or bk_name or bk),
                      report_id=report_index.report_id_for_path(html_path, bk, "board"))
            return

        # 基本面：output/reports/{code}/{date}/report.html（原路径，report_type 为空）
        # 概况：output/reports/{code}/overview/{date}/report.html（物理隔离）
        # F-06：user_id 非空时整体前缀 u{user_id}/；industry 的日期段带 __{bk} 后缀（D8）
        date_suffix = industry_date_suffix(report_type, bk)
        html_path = str(report_output_path(code, report_type=report_type,
                                           user_id=user_id, date_suffix=date_suffix))
        info = run_pipeline(
            code=code,
            use_dummy=False,
            use_llm=use_llm,
            report_type=report_type,
            out_path=html_path,
            on_progress=_on_progress,
            bk=bk,
            bk_name=bk_name,
        )
        final = info.get("out_path") or html_path
        # 分离落盘：结构化 JSON 契约 + 元信息（改样式只重渲染，不重跑 LLM）
        meta = {
            "code": code,
            "name": info.get("name", ""),
            "report_type": report_type,
            "bk": bk,  # industry 报告落盘绑定对比基准板块，供措施4「当日产物复用」按 bk 精确匹配
            # 基准板块名称：报告库列表项靠它把 industry 行的「对比基准」显示成人话
            # （只有 BK0890 的话用户读不懂）。入参里本来就有，落盘补一行即可。
            "bk_name": bk_name,
            "writer_mode": str(info.get("writer_mode") or ""),  # Step 4：contract/direct 溯源
            "search_used": bool(info.get("search_used")),  # Step 2：检索摘要是否注入
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "data_source": "akshare",
            "validation": info.get("validation", {}),
            "user_id": user_id,                                   # F-06 D1：归属
            "chart_offline": info.get("chart_offline"),           # F-06 D4：图表库已内联
        }
        save_report(code, html_path=final, profile=info.get("profile_json"), meta=meta,
                    report_type=report_type, user_id=user_id, date_suffix=date_suffix)
        tm.update(task_id, status="SUCCESS", progress=100, stage="完成", html_path=final,
                  name=str(info.get("name") or ""),
                  report_id=report_index.report_id_for_path(final, code, report_type))
    except Exception as e:  # noqa: BLE001 — 任务级异常必须被吞掉，转为 FAILED 状态
        tm.update(task_id, status="FAILED", progress=100,
                  stage="失败", error=str(e)[:300])


@app.post("/api/report", response_model=TaskStatus, status_code=202)
def create_report(payload: ReportRequest, req: Request) -> TaskStatus:
    """创建报告生成任务。

    F-06 D1：本入口**必须**带上用户身份 —— 报告按用户落盘（`u{user_id}/`），
    否则两个用户同日同标的会互相覆盖（只写 meta 字段做不到隔离）。
    `_auth_gate` 中间件已拦住未登录请求，此处再取一次用户用于拿 user_id
    （与 watchlist 端点同范式；正常路径不会走到 401）。
    """
    user = _current_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    user_id = user.get("id")

    # 板块深度报告：入口是板块代码(bk)，无需个股代码
    if payload.report_type == "board":
        if not _bk_ok(payload.bk):
            raise HTTPException(status_code=400, detail="板块报告必须提供有效的板块代码 bk（如 BK1036）")
        task = tm.create("", bk=payload.bk, report_type="board", user_id=user_id,
                         name=payload.bk_name, bk_name=payload.bk_name)
        threading.Thread(target=_worker,
                         args=(task.task_id, "", payload.use_llm, "board", payload.bk,
                               payload.bk_name, user_id),
                         daemon=True).start()
        return task

    code = payload.code
    # 有名称无代码时尝试解析（AkShare 路径支持中文名解析）
    if not _code_ok(code) and payload.name:
        from fundamental_agent.data.akshare import AkShareClient

        resolved = AkShareClient().resolve_code(payload.name)
        if resolved:
            code = resolved
    if not _code_ok(code):
        raise HTTPException(status_code=400, detail="code 必须是 6 位纯数字股票代码（或提供可解析的 name）")

    # 性能优化·措施4：当日同 code+type+bk 已有 SUCCESS 产物且未要求强制重跑 → 直接复用
    if not payload.force:
        reuse = _reuse_today(code, payload.report_type, payload.bk, user_id=user_id)
        if reuse:
            task = tm.create(code, bk=payload.bk, report_type=payload.report_type,
                             user_id=user_id, name=payload.name, bk_name=payload.bk_name)
            # F-04：复用路径同样要回填 report_id（从真实目录名反推）+ 展示名，
            # 否则任务中心里这条「已完成」既没有可点链接、也没有中文名。
            tm.update(task.task_id, status="SUCCESS", progress=100,
                      stage="完成（复用当日已生成报告）", html_path=reuse,
                      report_id=report_index.report_id_for_path(reuse, code,
                                                                payload.report_type),
                      **_disk_task_fields(reuse))
            return task

    task = tm.create(code, bk=payload.bk, report_type=payload.report_type, user_id=user_id,
                     name=payload.name, bk_name=payload.bk_name)
    threading.Thread(target=_worker,
                     args=(task.task_id, code, payload.use_llm, payload.report_type,
                           payload.bk, payload.bk_name, user_id),
                     daemon=True).start()
    return task


# ===== 报告任务中心（F-04）：把「跑中任务」这一瞬态暴露给用户 =====
#
# 分工（**这条边界要在改动时反复确认**）：
#   - **F-04（本组端点）管跑中任务** —— 活在 `TaskManager._tasks` 这个进程内字典里，
#     8002 重启即清空；
#   - **F-06（/api/report/list|view）管已生成产物** —— 活在磁盘上，跨重启仍在。
# 两者的接缝是 `TaskStatus.report_id`：任务 SUCCESS 时由 `report_index.report_id_for_path()`
# 从真实目录名反推，任务中心据此给出 `/api/report/view/{report_id}`。
# **绝不要**在任务中心里拼 `/api/report/{task_id}/html` —— 那是内存凭证，
# 8002 一重启链接即失效（F-06 交付时修掉的正是这个缺陷）。
#
# 本组端点与报告库的分工也解释了「为什么任务中心里能看到、报告库里却没有」：
# 前者是"将要有的"，后者是"已经有的"。SUCCESS 一旦可见 ⟹ `_worker` 已先
# `save_report()` 再改状态，故报告库里必然已能查到（即「生成完成后自动进入报告库」）。

def _task_item(t: TaskStatus) -> dict:
    """任务中心列表项：任务状态 + 报告库侧的展示契约。

    `report_type_label` / `subject_url` 一律取自 `report_index`（报告类型注册表的
    **单一事实源**），前端不得自建类型→中文名映射 —— 否则将来新增报告类型时，
    报告库认得它、任务中心不认识（与 N-15/N-16/N-18 的「分散硬编码」同一反模式）。
    """
    subject = t.bk if t.report_type == "board" else t.code
    view_url = ""
    if t.status == "SUCCESS" and t.report_id:
        view_url = "/api/report/view/%s" % t.report_id
    return {
        "task_id": t.task_id,
        "status": t.status,
        "progress": t.progress,
        "stage": t.stage,
        "error": t.error,
        "code": t.code,
        "bk": t.bk,
        "name": t.name,
        "bk_name": t.bk_name,
        "report_type": t.report_type,
        "report_type_label": report_index.type_label(t.report_type),
        "created_at": t.created_at,
        "updated_at": t.updated_at,
        "report_id": t.report_id,
        "view_url": view_url,
        "subject_url": report_index.subject_url(t.report_type, subject) if subject else "",
    }


@app.get("/api/tasks")
def list_tasks(req: Request, limit: int = 50):
    """列出**当前用户**的报告生成任务（跑中 + 最近完成），最新的在前。

    ⚠️ 三条硬约束：
      1. `user_id` 一律取自会话，**不接受**查询参数 —— 参数化等于把越权做成开关；
      2. `TaskManager.list()` 按 user_id 硬过滤（任务带 `html_path` 与错误文本，
         不过滤即跨用户泄露，与 F-06 D1「键漏一个维度」同型）；
      3. 路由前缀是 `/api/tasks`，与 `/api/report/{task_id}` 不重叠，
         故**不受** F-06 那条「`/api/report/list` 必须注册在 `{task_id}` 之前」的顺序约束。
    """
    user = _current_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    user_id = user.get("id")
    items = tm.list(user_id, limit=limit)
    return {
        "ok": True,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "active": sum(1 for t in items if t.status in ACTIVE_TASK_STATUSES),
        "count": len(items),
        "items": [_task_item(t) for t in items],
    }


# 单次删除请求接受的条数上限。与 `GET /api/tasks` 的 MAX_LIST_LIMIT 对齐：前端不可能
# 一次删掉比它看得到的更多任务。作用是给请求体一个硬上界（防止超大 payload），不是业务规则。
MAX_REMOVE_BATCH = 200


@app.post("/api/tasks/remove")
def remove_tasks(payload: TaskRemoveRequest, req: Request):
    """删除当前用户的若干条**终态**任务记录（任务中心「需要处理」区的清理出口）。

    ⚠️ 四条硬约束：
      1. `user_id` 一律取自会话；`TaskManager.remove()` 对「不属于自己」与「不存在」
         返回**同一个结果**，不做 403/404 区分 —— 区分即让本端点成为他人 task_id 的
         存在性探测器（可据此推断别人在生成什么报告）。
      2. 只删**终态**（SUCCESS / FAILED）：删掉活跃任务会让该 worker 的 `update()` 静默
         no-op、前端进度条**永久卡死**。未被删除的活跃任务由 `skipped_active` 显式回报，
         **绝不静默少删**（静默少删是"删除按钮点了没用"这类无从排查的故障来源）。
      3. **删任务 ≠ 删报告**：SUCCESS 的产物仍在报告库（报告库扫磁盘，不看 `_tasks`）。
         本端点只清理任务中心里那条记录，因此前端只在**失败 / 异常**行提供入口。
      4. 本操作**不落盘**：任务列表本就是瞬态（见模块 docstring），8002 重启即清空 ——
         删除只是让用户不必等到重启才能清掉那堆失败记录。

    ⚠️ 与 GET /api/tasks 的关系：不同于 F-06 那条「`/api/report/list` 必须注册在
       `/api/report/{task_id}` 之前」的顺序约束，本路由路径与方法都与 `GET /api/tasks`
       不同，当前无冲突；但若将来新增 `GET /api/tasks/{task_id}`，本路由**必须**仍注册在
       它之前，否则会被 `{task_id}` 吞掉并返回「任务不存在」。
    """
    user = _current_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    user_id = user.get("id")
    ids = (payload.task_ids or [])[:MAX_REMOVE_BATCH]
    res = tm.remove_many(ids, user_id)
    return {
        "ok": True,
        "deleted": res["deleted"],
        "not_found": res["not_found"],
        "skipped_active": res["skipped_active"],
        # 剩余条数供前端立刻更新计数（不必等下一次轮询）——与 GET 的 count 同义
        "count": len(tm.list(user_id, limit=0)),
    }


# ===== 报告库（F-06）：给已落盘报告一个与任务无关、与用户绑定的稳定入口 =====
#
# 为什么需要：此前报告的**唯一访问凭证**是内存里的 task_id（TaskManager._tasks 是
# 进程内字典），8002 一重启，磁盘上已生成的 report.html 就再也没有任何 URL 能打开。
# 本组端点用持久化的 report_id 取代 task_id，且 report_id 是**用户相对**标识
# （不含 user_id）——解析始终以「当前用户根」为基准，越权在结构上不可能发生。
#
# ⚠️ 路由注册顺序是硬约束：`/api/report/list` 只有一段，会被下面的
# `@app.get("/api/report/{task_id}")` 捕获。若注册在它之后，GET /api/report/list
# 会返回 404「任务不存在」—— 症状极具误导性（看起来像列表功能写错了）。
# 已用 tests/test_report_index.py 的用例锁定顺序，重排会立刻红灯。

@app.get("/api/report/list")
def list_reports(
    req: Request,
    type: str = "",
    code: str = "",
    page: int = 1,
    page_size: int = 20,
    sort: str = "generated_at",
    include_legacy: bool = False,
):
    """列出**当前用户**已落盘的报告（跨标的的库视图）。

    不是 `/api/report/{task_id}` 的变体，故必须注册在它**之前**（见上方说明）。
    """
    user = _current_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    res = report_index.list_reports(
        user.get("id"),
        report_type=type,
        code=code,
        page=page,
        page_size=page_size,
        sort=sort,
        include_legacy=include_legacy,
    )
    # 导出能力（F-07）**由服务端下发**，前端不自己猜「本机有没有浏览器」。
    # 与报告项里的 `deletable` 同一取向：能力判断只做一次，前端如实呈现 ——
    # 否则用户要点一次才知道「点了没反应」。
    res["export"] = report_export.capabilities()
    return res


@app.get("/api/report/view/{report_id}")
def view_report(report_id: str, req: Request):
    """打开**当前用户**某份已落盘报告（返回 text/html，**不依赖 task**）。

    校验不通过一律 404 且不回显拼接后的路径（避免路径探测）。
    """
    user = _current_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    html = report_index.resolve_report_html(user.get("id"), report_id)
    if html is None:
        raise HTTPException(status_code=404, detail="报告不存在或不属于当前用户")
    # 不带 filename → Content-Disposition 为 inline（带 filename 会变成 attachment，
    # 浏览器会改为「下载」而不是在新标签里直接渲染，与 D3「新标签直开」相悖）。
    return FileResponse(html, media_type="text/html")


# 单次删除请求接受的条数上限（与 `report_index.MAX_DELETE_BATCH` 同值）。
MAX_REPORT_REMOVE_BATCH = 200


@app.post("/api/report/remove")
def remove_reports(payload: ReportRemoveRequest, req: Request):
    """删除当前用户报告库中的若干份**已落盘**报告（**物理删除，不可恢复**）。

    ⚠️ 与「删任务记录」（`POST /api/tasks/remove`）是两件事，务必分清：
      前者删内存里的任务（`TaskManager._tasks`，重启即全没）；后者**删磁盘上的产物**
      （`report.html` + `meta.json` + `profile.json`）—— 删了就真的没有了。

    ⚠️ 五条硬约束：
      1. **`user_id` 一律取自会话**，绝不接受参数 —— 参数化等于把越权做成开关。
      2. **`user_id` 为空一律 403**：空值在 `report_index` 里代表 legacy 共享根
         `output/reports/`。`GET /api/report/list?include_legacy=true` **能看到**那些条目，
         但**删除侧结构上拒绝**（`delete_reports` 直接抛 `ValueError`）—— 与 N-22
         「不放宽 view 路径边界」同一取向。列表项用服务端下发的 `deletable=false` 表达
         这条差异，前端据此不渲染按钮（而不是让用户点了没反应）。
      3. **路径解析复用 view 的 `resolve_report_dir`**：于是「列表里点得开的」与
         「删得掉的」永远是同一集合，越权在结构上不可能（`..` / `/` 在
         `parse_report_id` 的字符集层面就已被排除）。
      4. **活跃任务守卫**：同一 `(主体, 报告类型)` 正在生成中 → 跳过并回报
         `skipped_busy`，不删 —— 否则 worker 正写着的目录会中途消失。
      5. **删报告 ≠ 删任务，但必须收回任务中心的入口**：删除成功后，任务中心里指向
         同一 `report_id` 的成功记录被一并移除（`forget_report_ids`），否则那里会留下
         一个点开就 404 的链接 —— 即 N-21 的形状。回报 `tasks_cleared` 让这件事可见。

    ⚠️ 路由注册顺序：本路由必须位于 `@app.get("/api/report/{task_id}")` **之前**。
      FastAPI 按 (路径, 方法) 匹配，POST 不会被 GET 的 `{task_id}` 吞掉；把它放在前面是
      **零成本的保险** —— 将来若有人给这个功能补一个 GET 变体（如
      `/api/report/remove/preview`），顺序错了的症状是极具误导性的「任务不存在」
      （与 `/api/report/list` 那条硬约束同型，已用测试锁定）。
    """
    user = _current_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    user_id = user.get("id")
    if not user_id:
        # legacy / 共享视角：结构上无法限定「删谁的」，直接拒绝而不是删到共享根
        raise HTTPException(status_code=403, detail="当前会话不属于任何用户，无法删除报告")
    ids = (payload.report_ids or [])[:MAX_REPORT_REMOVE_BATCH]
    res = report_index.delete_reports(user_id, ids, busy_keys=tm.active_keys(user_id))
    # 报告没了 → 任务中心里指向它的链接必须同步消失（否则是 404 死链）
    res["tasks_cleared"] = tm.forget_report_ids(user_id, res["removed_ids"])
    res["ok"] = True
    return res


@app.get("/api/report/export/{report_id}")
def export_report_file(report_id: str, req: Request, fmt: str = "pdf"):
    """把**当前用户**某份已落盘报告导出为 PDF / 长图 / Word（F-07）。

    目标与场景：「报告能发给别人」。打开报告需要账号（`/api/report/view/…` 要会话），
    而"发给同事/客户"的人不会给对方一个登录入口。导出 = 把报告换成**对方也打得开的载体**：
      - `pdf`  发给同事/客户、存档打印（通用载体，对方不需要账号）；
      - `png`  一张长图，发微信/钉钉、贴进文档；
      - `docx` 结构化文档，用于摘取与二次编辑（**不是像素还原**，见下）。

    ⚠️ 为什么导出的是"浏览器渲染结果"而不是直接把 HTML 转 PDF：
      报告里的图表是 **JS 运行时画到 `<canvas>` 上的**（概况报告 6 个 Chart.js canvas、
      板块报告 ECharts 动态建 canvas）。任何不走 JS 引擎的 HTML→PDF 方案
      （如 `fitz.Story`）产出的 PDF 里**图表整块空白** —— 即本项目明令禁止的
      "看起来有文件、实际缺内容"。本端点用真实浏览器（`--print-to-pdf` / `--screenshot`）
      渲染，与用户点开报告走**同一条渲染路径**，于是有一条朴素而重要的推论：
      **报告里能看到什么，导出里就能看到什么。**

    ⚠️ Word 版是 HTML 的**简化与重排**（标题层级 + 段落 + 表格 + 图表图片），不是像素还原：
      报告里的时间轴/徽标/卡片是视觉设计，硬塞进 OOXML 只会得到一坨难维护的模拟物。
      故 Word 版定位为"可编辑、可引用文字与数据"，这一点在前端导出成功的提示里写明。

    ⚠️ 五条硬约束（与 F-06 保持一致，不新开一条路）：
      1. **`user_id` 一律取自会话**，绝不接受参数。
      2. **路径解析复用 `report_index.describe_report`**（其内部就是 view / delete 共用的
         `resolve_report_dir`）→ **「能打开的报告」⟺「能导出的报告」**，越权在结构上不可能。
         解析不通过一律 404 且不回显路径。legacy 报告（`output/reports/{code}/…`）不落在
         任何用户根之内，因此正常用户对它**天然 404** —— 与 view 同口径，不是额外规则。
      3. **活跃任务守卫**：同一 `(主体, 类型)` 正在重新生成中 → 409 让用户稍后重试。
         不加这条会导出到**写了一半的目录**（worker 正覆盖 `report.html`），产出一份
         内容错位的文件 —— 比"导出失败"坏得多。守卫来源与删除端点同源
         （`tm.active_keys`），只看 `(主体, 类型)`、**不看日期段**（日期段由 worker 自算，
         从任务侧复算等于把 `storage` 的路径规则抄第二份）。
      4. **能力不可用/并发已满 → 503 + 明确原因**，绝不降级成"另一种方式"偷偷导出一份
         缺图表的东西。前端据 `GET /api/report/list` 下发的 `export.available` 提前禁用按钮。
      5. **产物是纯派生数据，不落盘缓存**：报告被重新生成后导出物何时失效是第二个失效模型，
         而 F-06 已把"文件即索引"定死。实测单次导出 4~6 秒，按需生成即可。
         临时文件在响应结束后由 background task 删除。

    ⚠️ 路由注册顺序：本路由必须位于 `@app.get("/api/report/{task_id}")` **之前**。
      `/api/report/{task_id}` 只吃一段路径，理论上吞不掉本路由（两段），但把它放在前面是
      **零成本的保险** —— 与 `/api/report/list`、`/api/report/remove` 同一条纪律
      （顺序错了的症状是极具误导性的「任务不存在」），已用测试锁定。

    ⚠️ 不做 `do_GET` 之外的方法：导出是**只读、幂等**操作，用 GET 让浏览器能直接
      `<a download>` / 新标签打开，不引入新的请求方法（8000 侧静态服务只有 do_GET/do_POST）。
    """
    user = _current_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")

    fmt = (fmt or "").strip().lower()
    if fmt not in report_export.FORMATS:
        raise HTTPException(
            status_code=400,
            detail="不支持的导出格式：%s（可选：%s）"
                   % (fmt or "(空)", " / ".join(report_export.FORMAT_ORDER)),
        )

    caps = report_export.capabilities()
    if not caps.get("available"):
        # 503 而不是 500：这是「环境暂时不具备能力」，不是服务端 bug；detail 带可操作建议
        raise HTTPException(status_code=503, detail=caps.get("reason") or "导出能力当前不可用")

    user_id = user.get("id")
    desc = report_index.describe_report(user_id, report_id)
    if desc is None:
        raise HTTPException(status_code=404, detail="报告不存在或不属于当前用户")

    if (desc["code"], desc["report_type"]) in tm.active_keys(user_id):
        raise HTTPException(status_code=409, detail="该报告正在重新生成，请稍后再导出")

    meta = report_export.FORMATS[fmt]
    subject = desc["name"] or desc["bk_name"] or desc["code"]
    filename = report_export.export_filename(subject, desc["report_type_label"],
                                             desc["date_seg"], fmt)
    # ASCII 兜底名：不看 `filename*` 的老客户端会用它保存文件。用代码拼，
    # 否则「贵州茅台_公司概况_20260926.pdf」的兜底会退化成全下划线、主人名全丢。
    ascii_name = report_export.export_filename_ascii(desc["code"], desc["report_type"],
                                                     desc["date_seg"], fmt)
    title = "%s %s" % (subject, desc["report_type_label"])

    # 临时产物：mkstemp 只用来**原子地占一个唯一文件名**，随即关闭 fd 并删除占位文件 ——
    # 让浏览器/写入器从"文件不存在"开始创建。保留一个 0 字节占位文件的话，导出器失败时
    # 会报成「为空文件」，把"压根没生成"和"生成了空的"两种故障混成一种（排错时差别很大）。
    fd, tmp_name = tempfile.mkstemp(prefix="tradeai_export_", suffix="." + meta["ext"])
    out_path = Path(tmp_name)
    try:
        os.close(fd)
    except OSError:
        pass
    _unlink_quiet(out_path)

    try:
        res = report_export.export_report(desc["report_dir"], fmt, out_path, title=title)
    except report_export.ExportBusy as e:
        _unlink_quiet(out_path)
        raise HTTPException(status_code=503, detail=str(e)) from e
    except report_export.ExportUnavailable as e:
        _unlink_quiet(out_path)
        raise HTTPException(status_code=503, detail=str(e)) from e
    except report_export.ExportError as e:
        _unlink_quiet(out_path)
        raise HTTPException(status_code=500, detail="导出失败：%s" % e) from e
    except Exception as e:  # noqa: BLE001 — 兜底：不让半成品临时文件留在盘上
        _unlink_quiet(out_path)
        raise HTTPException(status_code=500, detail="导出失败：%s" % e) from e

    headers = {
        # 中文文件名必须走 RFC 5987（filename*=UTF-8''…），否则浏览器拿到乱码名甚至拒绝保存
        "Content-Disposition": report_export.content_disposition(filename, ascii_name),
        "X-Export-Format": fmt,
        "X-Export-Bytes": str(res.get("bytes") or 0),
        "Cache-Control": "no-store",
    }
    warn = report_export.warnings_header(res.get("warnings") or [])
    if warn:
        headers["X-Export-Warnings"] = warn

    # 不传 filename：一旦传了，Starlette 会自己设 Content-Disposition，盖掉上面那行 RFC 5987 版本
    return FileResponse(
        out_path,
        media_type=meta["mime"],
        headers=headers,
        background=BackgroundTask(_unlink_quiet, out_path),
    )


@app.get("/api/report/{task_id}", response_model=TaskStatus)
def get_status(task_id: str) -> TaskStatus:
    t = tm.get(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return t


@app.get("/api/report/{task_id}/html")
def get_html(task_id: str):
    t = tm.get(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if t.status != "SUCCESS" or not t.html_path:
        raise HTTPException(status_code=409, detail="报告尚未生成（请轮询 /api/report/{id} 状态）")
    p = Path(t.html_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="报告文件缺失，可能已被清理")
    return FileResponse(p, media_type="text/html", filename=p.name)


# ===== 账号认证（方案阶段 1: /api/auth/login、/logout、/me） =====

def _session_cookie(token: str, max_age: int | None = None, clear: bool = False) -> str:
    """会话 Cookie 串: HttpOnly + SameSite=Lax; 上 HTTPS 后由 Nginx/部署层补 Secure。

    max_age=None  时不带 Max-Age → 浏览器会话 Cookie(关闭浏览器即失效, 对应未勾选「记住我」);
    勾选「记住我」时传 REMEMBER_TTL_DAYS*86400 → 持久化 30 天。
    """
    parts = ["%s=%s" % (auth_service.SESSION_COOKIE, token),
             "Path=/", "HttpOnly", "SameSite=Lax"]
    if clear:
        parts += ["Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"]
    elif max_age is not None:
        parts.append("Max-Age=%d" % max_age)
    return "; ".join(parts)


def _auth_token(req: Request) -> str | None:
    """会话双通道: 优先 Cookie, 其次 Authorization: Bearer（APP 端用）。"""
    token = req.cookies.get(auth_service.SESSION_COOKIE)
    if token:
        return token
    authz = req.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip()
    return None

# ===== 登录态门禁（AUTH-B 2026-09-11）: /api/* 除认证端点外一律需有效会话 =====
# 前端经 8000 同源代理携带 Cookie; APP/直连 8002 可用 Authorization: Bearer 双通道。
# OPTIONS 放行（CORS 预检）; 认证端点（/api/auth/*）保持公开。
@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    _p = request.url.path
    if (request.method != "OPTIONS" and _p.startswith("/api/")
            and not _p.startswith("/api/auth/")):
        if auth_service.get_user_by_session(_auth_token(request)) is None:
            return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
    return await call_next(request)


@app.post("/api/auth/login")
async def auth_login(req: Request):
    """账号密码登录: 验证通过创建会话并下发 HttpOnly Cookie。

    remember=True(「记住我」勾选) → 会话与 Cookie 均延长至 REMEMBER_TTL_DAYS 天;
    未勾选 → 服务端会话仍为 SESSION_TTL_DAYS 天, 但 Cookie 为浏览器会话级
    (关闭浏览器即要求重新登录), 服务端过期后 /me 自动 401 回退登录页。
    """
    try:
        data = await req.json()
    except Exception:
        data = {}
    account = (data.get("account") or "").strip()
    password = data.get("password") or ""
    remember = bool(data.get("remember"))
    user, token = auth_service.login(account, password, remember=remember)
    if user is None:
        return JSONResponse({"detail": "账号或密码不正确"}, status_code=401)
    max_age = REMEMBER_TTL_DAYS * 86400 if remember else None
    return JSONResponse(
        {"ok": True, "user": user, "redirect": "/index.html"},
        headers={"Set-Cookie": _session_cookie(token, max_age=max_age)},
    )


@app.post("/api/auth/logout")
def auth_logout(req: Request):
    """注销当前会话并清除 Cookie。"""
    token = _auth_token(req)
    auth_service.logout(token)
    return JSONResponse(
        {"ok": True},
        headers={"Set-Cookie": _session_cookie("", clear=True)},
    )


@app.get("/api/auth/me")
def auth_me(req: Request):
    """当前登录用户(会话校验): Cookie 或 Bearer 双通道。"""
    user = auth_service.get_user_by_session(_auth_token(req))
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


@app.get("/api/auth/captcha")
def auth_captcha():
    """图形验证码: 返回 captcha_id 与 SVG 图(前端点击可换一张)。"""
    captcha_id, svg = auth_service.gen_captcha()
    return JSONResponse({"captcha_id": captcha_id, "svg": svg})


@app.post("/api/auth/register")
async def auth_register(req: Request):
    """注册: 校验图形验证码(错→400 captcha_failed, 前端提示重新输入)后创建账号。

    服务端二次校验手机号格式与重复; 成功后跳登录页并预填手机号(不自动登录)。
    """
    try:
        data = await req.json()
    except Exception:
        data = {}
    phone = (data.get("phone") or "").strip()
    password = data.get("password") or ""
    if not _PHONE_RE.fullmatch(phone):
        return JSONResponse({"detail": "请输入正确的手机号"}, status_code=400)
    if not password:
        return JSONResponse({"detail": "请输入密码"}, status_code=400)
    if not auth_service.verify_captcha(data.get("captcha_id"), data.get("captcha_text")):
        return JSONResponse(
            {"detail": "图形验证码错误，请重新输入", "captcha_failed": True}, status_code=400
        )
    try:
        user = auth_service.register_user(phone, password)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    return JSONResponse({
        "ok": True,
        "user": user,
        "redirect": "/login.html?account=%s&registered=1" % user["username"],
    })


@app.post("/api/auth/reset_password")
async def auth_reset_password(req: Request):
    """找回密码: 先校验图形验证码, 再按手机号重置密码。

    - 验证码错误/过期 → 400 + captcha_failed(前端提示重新输入并刷新验证码)
    - 账号不存在     → 404 "账号不存在"
    - 成功           → 200, 旧会话已全部作废, 引导回登录页重新登录
    """
    try:
        data = await req.json()
    except Exception:
        data = {}
    phone = (data.get("phone") or "").strip()
    password = data.get("password") or ""
    if not _PHONE_RE.fullmatch(phone):
        return JSONResponse({"detail": "请输入正确的手机号"}, status_code=400)
    if not password:
        return JSONResponse({"detail": "请输入新密码"}, status_code=400)
    if not auth_service.verify_captcha(data.get("captcha_id"), data.get("captcha_text")):
        return JSONResponse(
            {"detail": "图形验证码错误，请重新输入", "captcha_failed": True}, status_code=400
        )
    user = auth_service.reset_password(phone, password)
    if user is None:
        return JSONResponse({"detail": "账号不存在"}, status_code=404)
    return JSONResponse({"ok": True, "redirect": "/login.html?account=%s&reset=1" % user["phone"]})


# ===== 自选股（F-03）: CRUD 在 8002, 行情富化在 8000 侧完成 =====
#
# 落点决策：用户身份与 auth.db 只在 8002，且 get_conn() 每次连接执行
# executescript(SCHEMA_SQL) —— 新增表天然幂等、零迁移脚本；_auth_gate 中间件
# 已自动保护 /api/*，故本组端点无需改动任何鉴权逻辑。行情取数能力只在 8000，
# 因此本文件只负责「存储与身份」，行情拼装在 frontend/server.py 侧完成。
#
# 删除语义（路线 1）：8000 静态服务的 Handler 只实现了 do_GET / do_POST，
# 全仓没有 do_DELETE —— 任何 DELETE 请求会在 8000 层直接 501、到不了这里。
# 故删除用 POST /api/watchlist/remove 表达，零改动 HTTP 方法层。
#
# 鉴权注意：_auth_gate 只做「拦截/放行」，**不把 user 注入 request**，
# 故下方端点内需自行再取一次会话用户（防御性判断，正常路径已被中间件拦住）。

def _current_user(req: Request):
    """取当前会话用户；未登录/过期返回 None。"""
    return auth_service.get_user_by_session(_auth_token(req))


@app.get("/api/watchlist", response_model=WatchListResponse)
def watchlist_list(req: Request) -> WatchListResponse:
    """列出当前用户自选（不含行情，行情由 8000 侧 /api/watchlist_quotes 拼装）。"""
    user = _current_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    items = watchlist_service.list_items(user["id"])
    return WatchListResponse(ok=True, items=items, count=len(items),
                             limit=watchlist_service.MAX_WATCHLIST)


@app.post("/api/watchlist/add")
def watchlist_add(payload: WatchAddRequest, req: Request):
    """添加自选。**幂等**：重复添加返回 added=False 而非报错。"""
    user = _current_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    try:
        res = watchlist_service.add(user["id"], payload.code, payload.name)
    except ValueError as e:
        # 代码格式非法 / 超出上限 —— 均为可预期的用户输入问题，返回 400 + 友好文案
        return JSONResponse({"detail": str(e)}, status_code=400)
    return {"ok": True, "added": res["added"], "count": res["count"],
            "limit": watchlist_service.MAX_WATCHLIST}


@app.post("/api/watchlist/remove")
def watchlist_remove(payload: WatchRemoveRequest, req: Request):
    """移除自选。**幂等**：不存在返回 removed=False 而非报错。"""
    user = _current_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    try:
        res = watchlist_service.remove(user["id"], payload.code)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    return {"ok": True, "removed": res["removed"], "count": res["count"],
            "limit": watchlist_service.MAX_WATCHLIST}


@app.post("/api/watchlist/order")
def watchlist_order(payload: WatchOrderRequest, req: Request):
    """按给定顺序重排自选（v1.5 预留，仅影响本人数据）。"""
    user = _current_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    n = watchlist_service.reorder(user["id"], payload.codes)
    return {"ok": True, "updated": n}


@app.get("/api/watchlist/check")
def watchlist_check(code: str, req: Request):
    """查询单只标的是否已自选（供详情页按钮回显）。

    独立成端点而非并入 /api/watchlist：详情页只需一个布尔值，
    不应为此拉全量列表。
    """
    user = _current_user(req)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return {"ok": True, "code": watchlist_service.normalize_code(code),
            "watched": watchlist_service.has(user["id"], code)}
