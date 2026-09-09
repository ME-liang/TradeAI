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
#
# 为什么需要 --force：online 服务一旦在改动前启动，进程里加载的永远是旧代码。
# P3-4 实测：两服务启动于 18:27，而 P3 修复落在 18:45/19:00/21:34 —— 全部晚于启动，
# 导致线上实际跑的是修复前版本。改动后必须重启，且重启前应先确认旧进程已被替换。
set -uo pipefail

PY="/c/Users/Administrator/.workbuddy/binaries/python/envs/fundamental_agent/Scripts/python.exe"
ROOT="/d/workspace/fundamental_agent"
FRONTEND_PORT=8000
API_PORT=8002
API_HOST="127.0.0.1"

cd "$ROOT" || { echo "[FATAL] 项目根目录不存在: $ROOT"; exit 1; }

_log()  { printf '%s\n' "$*"; }
_pids_on() { # $1 = port -> 打印占用该端口的监听 PID
  # netstat -ano 列序: $1=协议 $2=本地地址:端口 $3=外部地址 $4=状态 $5=PID
  # （曾用 $4/$5 取字段导致永远匹配不到、端口占用时未中止而误启进程，勿再写错）
  netstat -ano 2>/dev/null | awk -v p=":$1" '$1=="TCP" && $2 ~ p"$" && $4=="LISTENING" {print $NF}' | sort -u
}

_health() { # 绕过 HTTP_PROXY 直连探测（环境常设 http_proxy，会把 127.0.0.1 也发往代理）
  "$PY" - "$1" <<'PYEOF'
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
PYEOF
}

check() {
  local ok=0
  local st
  st=$(_health "http://127.0.0.1:${API_PORT}/openapi.json")
  if [ "$st" = "200" ]; then _log "  [OK ] 报告 API  127.0.0.1:${API_PORT}"; else _log "  [BAD] 报告 API  :${API_PORT} 不可达 (status=$st)"; ok=1; fi
  st=$(_health "http://127.0.0.1:${FRONTEND_PORT}/")
  if [ "$st" = "200" ]; then _log "  [OK ] 前端      127.0.0.1:${FRONTEND_PORT}"; else _log "  [BAD] 前端      :${FRONTEND_PORT} 不可达 (status=$st)"; ok=1; fi
  # 代理真透传：不存在的 task_id 应返回后端 404（业务响应），而非 502「报告服务不可达」
  st=$(_health "http://127.0.0.1:${FRONTEND_PORT}/api/report/__healthcheck__")
  if [ "$st" = "404" ]; then _log "  [OK ] 前端->API 同源代理透传正常 (404 来自业务层)"; else _log "  [BAD] 代理异常 (status=$st，期望 404)"; ok=1; fi
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
  PYTHONPATH=src "$PY" -m uvicorn src.fundamental_agent.api.server:app \
      --host "$API_HOST" --port "$API_PORT" >> _svc_api_${API_PORT}.log 2>&1 &
  _log "启动前端      :${FRONTEND_PORT} ..."
  ( cd "$ROOT/frontend" && REPORT_API_BASE="http://127.0.0.1:${API_PORT}" \
      "$PY" server.py "$FRONTEND_PORT" >> "$ROOT/_svc_web_${FRONTEND_PORT}.log" 2>&1 & )
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
  --force)
    _log "=== --force：先终止占用 ${FRONTEND_PORT}/${API_PORT} 的进程 ==="
    stop_existing
    _log "=== 启动服务 ==="; start || exit 1
    _log "=== 健康自检 ==="; check; exit $?
    ;;
  "")
    local_pids="$(_pids_on "$FRONTEND_PORT") $(_pids_on "$API_PORT")"
    if [ -n "${local_pids// /}" ]; then
      _log "[ABORT] 端口已被占用，未做任何改动。占用进程 PID: ${local_pids}"
      _log "        若要替换（改动后重启请选此项）：./start_services.sh --force"
      exit 1
    fi
    _log "=== 启动服务 ==="; start || exit 1
    _log "=== 健康自检 ==="; check; exit $?
    ;;
  *)
    _log "未知参数: $1"; _log "用法: ./start_services.sh [--force|--check]"; exit 1
    ;;
esac
