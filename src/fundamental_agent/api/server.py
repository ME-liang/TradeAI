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
import re
from datetime import datetime
from pathlib import Path

import threading
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .schemas import ReportRequest, TaskStatus
from .tasks import TaskManager
from fundamental_agent.main import run_pipeline
from fundamental_agent.storage import report_output_path, save_report
from fundamental_agent.auth import service as auth_service
from fundamental_agent.auth.db import REMEMBER_TTL_DAYS, SESSION_TTL_DAYS

# server.py -> api -> fundamental_agent -> src -> project_root
PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = PROJECT_ROOT / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="公司基本面分析 Agent API", version="0.1.0")
tm = TaskManager()

# CORS（方案阶段 1: APP 跨域调用需要）。Cookie 走前端 8000 同源代理, 此处不放开凭据。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _code_ok(code: str) -> bool:
    return bool(code) and code.isdigit() and len(code) == 6


def _bk_ok(bk: str) -> bool:
    import re as _re
    return bool(_re.fullmatch(r"BK\d+", bk or ""))


# 大陆手机号(注册接口服务端二次校验用, 与 fundamental_agent.auth.service 同口径)
_PHONE_RE = re.compile(r"^1\d{10}$")


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
        task = tm.create("", bk=req.bk, report_type="board")
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
            task = tm.create(code, bk=req.bk, report_type=req.report_type)
            tm.update(task.task_id, status="SUCCESS", progress=100,
                      stage="完成（复用当日已生成报告）", html_path=reuse)
            return task

    task = tm.create(code, bk=req.bk, report_type=req.report_type)
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
