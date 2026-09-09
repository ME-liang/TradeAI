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
from datetime import datetime
from pathlib import Path

import threading
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from .schemas import ReportRequest, TaskStatus
from .tasks import TaskManager
from fundamental_agent.main import run_pipeline
from fundamental_agent.storage import report_output_path, save_report

# server.py -> api -> fundamental_agent -> src -> project_root
PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = PROJECT_ROOT / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="公司基本面分析 Agent API", version="0.1.0")
tm = TaskManager()


def _code_ok(code: str) -> bool:
    return bool(code) and code.isdigit() and len(code) == 6


def _bk_ok(bk: str) -> bool:
    import re as _re
    return bool(_re.fullmatch(r"BK\d+", bk or ""))


def _reuse_today(code: str, report_type: str, bk: str = "") -> str | None:
    """性能优化·措施4：同 code+report_type+bk 当日已有 SUCCESS 产物时，返回其 html 路径。

    命中条件（全部满足）：
      - 当日落盘路径 output/reports/{code}/[{report_type}/]{yyyymmdd}/report.html 存在；
      - 同目录 meta.json 存在且 report_type 与请求一致；
      - report_type=industry 时 meta.bk 必须与请求 bk 一致（早于 bk 落盘的历史产物
        仅在请求 bk 为空时命中，避免误复用其他对比基准的旧报告）。
    命中则直接复用，不启动新线程、不重复取数、不重复消耗 LLM。
    返回 None 表示需要重新生成。任何读取异常都按「未命中」处理，绝不阻塞请求。
    """
    try:
        if not _code_ok(code) or not report_type:
            return None
        html = report_output_path(code, report_type=report_type)
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
        return str(html)
    except Exception:  # noqa: BLE001 — 复用判定失败只意味着多跑一次生成
        return None


def _worker(task_id: str, code: str, use_llm: bool, report_type: str = "fundamental",
            bk: str = "", bk_name: str = "") -> None:
    """后台线程：跑完整 pipeline，进度实时回写 TaskManager。"""
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
            html_path = str(report_output_path(bk, report_type="board"))
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
            }
            save_report(bk, html_path=html_path, meta=meta, report_type="board")
            tm.update(task_id, status="SUCCESS", progress=100, stage="完成", html_path=html_path)
            return

        # 基本面：output/reports/{code}/{date}/report.html（原路径，report_type 为空）
        # 概况：output/reports/{code}/overview/{date}/report.html（物理隔离）
        html_path = str(report_output_path(code, report_type=report_type))
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
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "data_source": "akshare",
            "validation": info.get("validation", {}),
        }
        save_report(code, html_path=final, profile=info.get("profile_json"), meta=meta,
                    report_type=report_type)
        tm.update(task_id, status="SUCCESS", progress=100, stage="完成", html_path=final)
    except Exception as e:  # noqa: BLE001 — 任务级异常必须被吞掉，转为 FAILED 状态
        tm.update(task_id, status="FAILED", progress=100,
                  stage="失败", error=str(e)[:300])


@app.post("/api/report", response_model=TaskStatus, status_code=202)
def create_report(req: ReportRequest) -> TaskStatus:
    # 板块深度报告：入口是板块代码(bk)，无需个股代码
    if req.report_type == "board":
        if not _bk_ok(req.bk):
            raise HTTPException(status_code=400, detail="板块报告必须提供有效的板块代码 bk（如 BK1036）")
        task = tm.create("", bk=req.bk)
        threading.Thread(target=_worker,
                         args=(task.task_id, "", req.use_llm, "board", req.bk, req.bk_name),
                         daemon=True).start()
        return task

    code = req.code
    # 有名称无代码时尝试解析（AkShare 路径支持中文名解析）
    if not _code_ok(code) and req.name:
        from fundamental_agent.data.akshare import AkShareClient

        resolved = AkShareClient().resolve_code(req.name)
        if resolved:
            code = resolved
    if not _code_ok(code):
        raise HTTPException(status_code=400, detail="code 必须是 6 位纯数字股票代码（或提供可解析的 name）")

    # 性能优化·措施4：当日同 code+type+bk 已有 SUCCESS 产物且未要求强制重跑 → 直接复用
    if not req.force:
        reuse = _reuse_today(code, req.report_type, req.bk)
        if reuse:
            task = tm.create(code, bk=req.bk)
            tm.update(task.task_id, status="SUCCESS", progress=100,
                      stage="完成（复用当日已生成报告）", html_path=reuse)
            return task

    task = tm.create(code, bk=req.bk)
    threading.Thread(target=_worker,
                     args=(task.task_id, code, req.use_llm, req.report_type, req.bk, req.bk_name),
                     daemon=True).start()
    return task


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
