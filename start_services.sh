#!/usr/bin/env bash
# P3-4：拉起「修复后」的概况报告服务栈，并做健康自检。
#
#   前端        :8000   (frontend/server.py)
#   报告 API    :8002   (uvicorn src.fundamental_agent.api.server:app)
#
# 用法（Git Bash）：
#   ./start_services.sh            # 端口空闲则启动；端口被占用则报错并列出占用进程
#   ./start_services.sh --force    # 先终止占用 8000/8002 的进程，再启动（用于替换跑旧代码的陈旧服务）
#   ./start_services.sh --check    # 只做健康自检，不启动
#   ./start_services.sh --setup    # 只建 .venv + 装依赖，不启动
#
# 首次运行会检测 $ROOT/.venv，缺失则自动创建并安装 requirements.txt；
# 想跳过自动装环境，设 SKIP_SETUP=1。
#
# 为什么需要 --force：online 服务一旦在改动前启动，进程里加载的永远是旧代码。
# P3-4 实测：两服务启动于 18:27，而 P3 修复落在 18:45/19:00/21:34 —— 全部晚于启动，
# 导致线上实际跑的是修复前版本。改动后必须重启，且重启前应先确认旧进程已被替换。
set -uo pipefail

# --- 路径自动定位 ---------------------------------------------------------
# 原实现把 PY / ROOT 硬编码为 C:\Users\Administrator\... 与 /d/workspace/fundamental_agent，
# 换一台机器即失效。改为以「脚本自身所在目录」为项目根，可随仓库迁移。
ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_PORT=8000
API_PORT=8002
API_HOST="127.0.0.1"
VENV_PY="$ROOT/.venv/Scripts/python.exe"

cd "$ROOT" || { echo "[FATAL] 项目根目录不存在: $ROOT"; exit 1; }

_log()  { printf '%s\n' "$*"; }

# Git Bash 的 /e/xxx 传给「Windows 原生 python.exe」时不会被自动转换，会被当成
# 当前盘下的 \e\xxx（实测把 venv 建到了 E:\e\TradeAI\.venv）。凡把路径作为参数
# 交给原生解释器，一律先转成 Windows 形式。
_win() { cygpath -w "$1" 2>/dev/null || printf '%s\n' "$1"; }

_pids_on() { # $1 = port -> 打印占用该端口的监听 PID
  # netstat -ano 列序: $1=协议 $2=本地地址:端口 $3=外部地址 $4=状态 $5=PID
  # （曾用 $4/$5 取字段导致永远匹配不到、端口占用时未中止而误启进程，勿再写错）
  netstat -ano 2>/dev/null | awk -v p=":$1" '$1=="TCP" && $2 ~ p"$" && $4=="LISTENING" {print $NF}' | sort -u
}

# --- Python 解释器解析 ----------------------------------------------------
_managed_py() { # 找一个非项目 .venv 的基础解释器（用于创建 venv）
  local c
  for c in "$(command -v python.exe 2>/dev/null)" "$(command -v python3.exe 2>/dev/null)" \
           "$(command -v python 2>/dev/null)" "$(command -v python3 2>/dev/null)"; do
    [ -n "$c" ] && { printf '%s\n' "$c"; return 0; }
  done
  c="$(ls -1 "$HOME"/.workbuddy/binaries/python/versions/*/python.exe 2>/dev/null | sort -V | tail -1)"
  [ -n "$c" ] && { printf '%s\n' "$c"; return 0; }
  return 1
}

ensure_venv() { # 环境缺失则建 .venv 并装依赖；依赖齐备则直接通过
  # 判据不能只看 .venv 目录是否存在——上一次安装中断会留下「空 venv」，
  # 那种情况下服务必然起不来，必须按关键依赖是否可导入来判断。
  local base index req need=0
  if [ -x "$VENV_PY" ]; then
    "$VENV_PY" -c "import fastapi, uvicorn, pandas, yaml" >/dev/null 2>&1 || need=1
  else
    need=2
  fi
  [ "$need" -eq 0 ] && return 0
  if [ "${SKIP_SETUP:-0}" = "1" ]; then
    _log "[FATAL] $VENV_PY 不可用（依赖缺失），且 SKIP_SETUP=1，已跳过环境安装。"
    return 1
  fi
  base="$(_managed_py)" || { _log "[FATAL] 找不到可用于创建 venv 的 Python，请先安装 Python >=3.10。"; return 1; }
  req="$(_win "$ROOT/requirements.txt")"
  if [ "$need" -eq 2 ]; then
    _log "未检测到 .venv，用 $base 创建 ..."
    "$base" -m venv "$(_win "$ROOT/.venv")" || { _log "[FATAL] 创建 .venv 失败。"; return 1; }
    [ -x "$VENV_PY" ] || { _log "[FATAL] venv 已创建但未生成 $VENV_PY，请检查 Python 安装。"; return 1; }
  else
    _log "检测到 .venv 但依赖不完整，补装 ..."
  fi
  _log "安装 requirements.txt（首次较慢，约数分钟）..."
  # 本机实测 pypi.org 直连可用；而清华镜像的旧式索引（repository-version 1.0）
  # 会被 pip 26 判为「无可用版本」，故默认走官方源。
  # 受限网络下需换源时：PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple ./start_services.sh
  index="${PIP_INDEX_URL:-https://pypi.org/simple}"
  # 不要在这里做 `pip install --upgrade pip`：实测升级中断会让 venv 彻底丢掉 pip 模块，
  # 后续连 `-m pip` 都跑不起来。装依赖前只做「pip 可用性」校验与自愈。
  if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    _log "pip 不可用，尝试用 ensurepip 修复 ..."
    "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1 || true
    "$VENV_PY" -m pip --version >/dev/null 2>&1 \
      || { _log "[FATAL] venv 内 pip 不可用，建议删除 .venv 后重试：rm -rf \"$ROOT/.venv\""; return 1; }
  fi
  PIP_INDEX_URL="$index" "$VENV_PY" -m pip install -r "$req" \
    || { _log "[FATAL] 依赖安装失败，请检查网络后重试（可设 PIP_INDEX_URL 换源）。"; return 1; }
  _log "依赖安装完成。"
  return 0
}

_health() { # 绕过 HTTP_PROXY 直连探测（环境常设 http_proxy，会把 127.0.0.1 也发往代理）
  # 用 `-c` 而非 here-document：TMPDIR 缺失或不可写的环境（实测部分沙箱/受限会话）
  # 下 here-document 会以「cannot create temp file for here-document」失败，
  # 于是自检恒报 BAD —— 把「环境写不了临时文件」误显示成「服务异常」，
  # 且报错与真实原因毫无关联，很难排查。
  "$VENV_PY" -c '
import sys, urllib.request, urllib.error
url = sys.argv[1]
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    r = op.open(url, timeout=8)
    print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print("ERR")
' "$1"
}

check() {
  local ok=0
  local st
  st=$(_health "http://127.0.0.1:${API_PORT}/openapi.json")
  if [ "$st" = "200" ]; then _log "  [OK ] 报告 API  127.0.0.1:${API_PORT}"; else _log "  [BAD] 报告 API  :${API_PORT} 不可达 (status=$st)"; ok=1; fi
  st=$(_health "http://127.0.0.1:${FRONTEND_PORT}/")
  if [ "$st" = "200" ]; then _log "  [OK ] 前端      127.0.0.1:${FRONTEND_PORT}"; else _log "  [BAD] 前端      :${FRONTEND_PORT} 不可达 (status=$st)"; ok=1; fi
  # 代理真透传：请求应抵达后端业务层并返回业务级响应（401 未登录门禁 / 404 无此 task），
  # 而非 502「报告服务不可达」。注意 /api/report* 受登录门禁保护，未带会话时是 401，
  # 因此 401 同样证明透传正常——原实现只认 404，会把正常的门禁响应误判为代理异常。
  st=$(_health "http://127.0.0.1:${FRONTEND_PORT}/api/report/__healthcheck__")
  case "$st" in
    404) _log "  [OK ] 前端->API 同源代理透传正常 (404 来自业务层)" ;;
    401) _log "  [OK ] 前端->API 同源代理透传正常 (401 来自登录门禁)" ;;
    *)   _log "  [BAD] 代理异常 (status=$st，期望 401/404，502=报告服务不可达)"; ok=1 ;;
  esac
  return $ok
}

stop_existing() {
  local port pid
  for port in "$FRONTEND_PORT" "$API_PORT"; do
    for pid in $(_pids_on "$port"); do
      _log "  终止占用 :${port} 的进程 PID ${pid}"
      taskkill //F //PID "$pid" >/dev/null 2>&1 || true
    done
  done
  sleep 2
}

start() {
  # 日志一律「追加」并写入时间戳分隔线：绝不用 > 截断。
  # 原因：截断会毁掉前一次 live 运行的护栏证据（P3-4 实测踩过——3 次 LLM 拦截日志被冲掉），
  # 且旧进程仍持有原 inode、会按旧偏移量续写，导致日志内容错位难以解读。
  local stamp; stamp="$(date '+%Y-%m-%d %H:%M:%S')"
  printf '\n===== [%s] start_services.sh 启动 =====\n' "$stamp" >> _svc_api_${API_PORT}.log
  printf '\n===== [%s] start_services.sh 启动 =====\n' "$stamp" >> _svc_web_${FRONTEND_PORT}.log
  _log "启动报告 API  :${API_PORT} ..."
  # 三个 fd 全部接管：stdin 接 /dev/null、stdout/stderr 进日志。
  # 少接管 stdin 时，服务会继承调用方的管道，导致 `./start_services.sh | tail` 之类的
  # 管道调用在脚本退出后仍不返回 EOF（实测挂住不退出）。
  # nohup + 子 shell：脱离脚本的作业控制，脚本退出后服务继续存活。
  ( cd "$ROOT" && PYTHONPATH=src nohup "$VENV_PY" -m uvicorn src.fundamental_agent.api.server:app \
      --host "$API_HOST" --port "$API_PORT" >> "$ROOT/_svc_api_${API_PORT}.log" 2>&1 </dev/null & )
  _log "启动前端      :${FRONTEND_PORT} ..."
  ( cd "$ROOT/frontend" && REPORT_API_BASE="http://127.0.0.1:${API_PORT}" \
      nohup "$VENV_PY" server.py "$FRONTEND_PORT" >> "$ROOT/_svc_web_${FRONTEND_PORT}.log" 2>&1 </dev/null & )
  _log "等待服务就绪..."
  local i
  for i in $(seq 1 20); do
    sleep 2
    if [ "$(_health "http://127.0.0.1:${API_PORT}/openapi.json")" = "200" ] \
    && [ "$(_health "http://127.0.0.1:${FRONTEND_PORT}/")" = "200" ]; then
      _log "服务已就绪（等待 $((i*2))s）"; return 0
    fi
  done
  _log "[WARN] 20 次探测内未就绪，请查看 _svc_api_${API_PORT}.log / _svc_web_${FRONTEND_PORT}.log"
  return 1
}

case "${1:-}" in
  --check)
    _log "=== 健康自检 ==="; check; exit $?
    ;;
  --setup)
    _log "=== 安装运行环境 ==="; ensure_venv || exit 1
    _log "  [OK ] $VENV_PY"
    exit 0
    ;;
  --force)
    _log "=== --force：先终止占用 ${FRONTEND_PORT}/${API_PORT} 的进程 ==="
    stop_existing
    ensure_venv || exit 1
    _log "=== 启动服务 ==="; start || exit 1
    _log "=== 健康自检 ==="; check; exit $?
    ;;
  "")
    # 注意：此处不可用 local —— 原实现在函数外写 `local local_pids=...`，
    # 在 set -u 下会直接以 unbound variable 退出（无参数运行必崩），已改为普通变量。
    occupied="$(_pids_on "$FRONTEND_PORT") $(_pids_on "$API_PORT")"
    if [ -n "${occupied// /}" ]; then
      _log "[ABORT] 端口已被占用，未做任何改动。占用进程 PID: ${occupied}"
      _log "        若要替换（改动后重启请选此项）：./start_services.sh --force"
      exit 1
    fi
    ensure_venv || exit 1
    _log "=== 启动服务 ==="; start || exit 1
    _log "=== 健康自检 ==="; check; exit $?
    ;;
  *)
    _log "未知参数: $1"; _log "用法: ./start_services.sh [--force|--check|--setup]"; exit 1
    ;;
esac
