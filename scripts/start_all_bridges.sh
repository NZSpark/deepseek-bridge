#!/usr/bin/env bash
# 一键启停 ~/.pi/agent/models.json 里声明的所有 Web-to-API bridge。
#
# 设计要点：
#   * 端口 / provider 不硬编码，从 models.json 的 baseUrl 反推，仓库名按 provider
#     别名映射（glmbridge -> GLMBridge 等）。新增 provider 时改 REPO_OF 即可。
#   * 端口已被监听 -> 跳过启动，不重复拉起、也不误杀别人的进程。
#   * CDP 模式（ClaudeBridge / GrokBridge 共用 9222 + 同一个 user_data 目录）：
#     driver 会在端口没人监听时自己 Popen 一个 Chrome，所以脚本不需要预先开浏览器。
#     但两个桥共用同一 CDP 端点时，谁先起谁“拥有”那个 Chrome；因此这两个按顺序
#     串行启动，并在中间等 9222 就绪，避免两个同时 Popen 抢同一个 user-data-dir。
#   * 每个 bridge 的 stdout/stderr -> logs/<provider>.log，PID -> logs/<provider>.pid。
#   * 启动后逐个探活 /v1/models，最后打印汇总表。
#
# 用法：
#   scripts/start_all_bridges.sh            # 启动全部（已在跑则跳过）
#   scripts/start_all_bridges.sh start      # 同上
#   scripts/start_all_bridges.sh stop       # 按 pid 文件停止（不碰浏览器）
#   scripts/start_all_bridges.sh restart    # stop + start
#   scripts/start_all_bridges.sh status     # 只看状态，不启停
#   scripts/start_all_bridges.sh start qwen-web grok-web   # 只操作指定 provider
#
# 环境变量：
#   MODELS_JSON   默认 ~/.pi/agent/models.json
#   BRIDGES_HOME  默认脚本所在仓库的上一级目录（即 ~/Github）
#   WAIT_S        启动后等待端口就绪的上限秒数，默认 45

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGES_HOME="${BRIDGES_HOME:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
MODELS_JSON="${MODELS_JSON:-$HOME/.pi/agent/models.json}"
WAIT_S="${WAIT_S:-45}"
LOG_DIR="$SCRIPT_DIR/logs"

# provider（models.json 里的键）-> 仓库目录名。
# 大多数只差大小写，glmbridge 全小写且仓库叫 GLMBridge，需单独列。
repo_of() {
  case "$1" in
    glmbridge)   echo "GLMBridge" ;;
    doubao-web)  echo "DoubaoBridge" ;;
    qwen-web)    echo "QwenBridge" ;;
    grok-web)    echo "GrokBridge" ;;
    deepseek-web) echo "DeepseekBridge" ;;
    gemini-web)  echo "GeminiBridge" ;;
    chatgpt-web) echo "ChatGPTBridge" ;;
    claude-web)  echo "ClaudeBridge" ;;
    *)           echo "" ;;
  esac
}

# 入口文件名 = 仓库里唯一的 *_api_server.py
entry_of() {
  local repo="$1" f
  f=$(ls "$BRIDGES_HOME/$repo"/*_api_server.py 2>/dev/null | head -1)
  [ -n "$f" ] && basename "$f"
}

# 解析 models.json：每行输出 "provider<TAB>port"
list_providers() {
  python3 - "$MODELS_JSON" <<'PY'
import json, sys
from urllib.parse import urlparse
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    sys.exit(f"读 models.json 失败: {e}")
for name, p in cfg.get("providers", {}).items():
    url = p.get("baseUrl", "")
    try:
        port = urlparse(url).port
    except Exception:
        port = None
    if port:
        print(f"{name}\t{port}")
PY
}

port_open() { (exec 3<>/dev/tcp/127.0.0.1/"$1") >/dev/null 2>&1; }

wait_port() {
  local port="$1" n=$(( WAIT_S * 3 ))
  while (( n-- > 0 )); do
    port_open "$port" && return 0
    sleep 0.3
  done
  return 1
}

healthy() {
  curl -fsS -m 3 "http://127.0.0.1:$1/v1/models" >/dev/null 2>&1
}

pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

cmd_status() {
  printf '%-14s %-6s %-10s %-8s %s\n' PROVIDER PORT STATE PID HEALTH
  local provider port repo pid state health
  while IFS=$'\t' read -r provider port; do
    repo=$(repo_of "$provider")
    pid=$(cat "$LOG_DIR/$provider.pid" 2>/dev/null || true)
    if port_open "$port"; then state=up; else state=down; fi
    if pid_alive "$pid"; then pid="$pid"; else pid=-; fi
    if healthy "$port"; then health=ok; elif [ "$state" = up ]; then health=no-http; else health=-; fi
    printf '%-14s %-6s %-10s %-8s %s\n' "$provider" "$port" "$state" "$pid" "$health"
  done < <(list_providers)
}

start_one() {
  local provider="$1" port="$2"
  local repo entry pidfile logfile
  repo=$(repo_of "$provider")
  if [ -z "$repo" ] || [ ! -d "$BRIDGES_HOME/$repo" ]; then
    echo "  [skip] $provider: 找不到仓库目录 ($BRIDGES_HOME/${repo:-?})"
    return 0
  fi
  entry=$(entry_of "$repo")
  if [ -z "$entry" ]; then
    echo "  [skip] $provider: $repo 里没有 *_api_server.py"
    return 0
  fi
  if port_open "$port"; then
    echo "  [skip] $provider: 端口 $port 已在监听"
    return 0
  fi
  local py="$BRIDGES_HOME/$repo/.venv/bin/python"
  [ -x "$py" ] || py=$(command -v python3)

  pidfile="$LOG_DIR/$provider.pid"
  logfile="$LOG_DIR/$provider.log"
  # 后台启动且彻底脱离本脚本：setsid 新会话 + 三件套重定向（含 stdin </dev/null），
  # 否则父 shell 会等这个子进程，脚本卡在 start_one 里不返回。
  (
    cd "$BRIDGES_HOME/$repo" || exit 1
    if command -v setsid >/dev/null 2>&1; then
      setsid "$py" "$entry" </dev/null >>"$logfile" 2>&1 &
    else
      nohup "$py" "$entry" </dev/null >>"$logfile" 2>&1 &
    fi
    echo $! >"$pidfile"
  )
  local pid; pid=$(cat "$pidfile" 2>/dev/null || true)
  echo "  [start] $provider: port=$port pid=$pid repo=$repo"
  if wait_port "$port"; then
    echo "  [ ok  ] $provider: 端口 $port 已就绪"
  else
    echo "  [fail] $provider: ${WAIT_S}s 内端口 $port 未就绪，见 $logfile"
  fi
}

cmd_start() {
  local want=("$@")
  mkdir -p "$LOG_DIR"
  local providers=() ports=() provider port
  while IFS=$'\t' read -r provider port; do
    if [ ${#want[@]} -gt 0 ]; then
      local hit=0 w
      for w in "${want[@]}"; do [ "$w" = "$provider" ] && hit=1; done
      [ $hit -eq 1 ] || continue
    fi
    providers+=("$provider"); ports+=("$port")
  done < <(list_providers)

  echo "bridges 根目录: $BRIDGES_HOME"
  echo "日志目录:     $LOG_DIR"
  echo

  # 第一遍：非 CDP 的（互不干扰，可直接全起）
  local i
  for i in "${!providers[@]}"; do
    case "${providers[i]}" in claude-web|grok-web) continue ;; esac
    start_one "${providers[i]}" "${ports[i]}"
  done

  # 第二遍：CDP 的串行，且中间等 9222 就绪（两者共用同一个 Chrome 端点）。
  for i in "${!providers[@]}"; do
    case "${providers[i]}" in
      claude-web|grok-web)
        start_one "${providers[i]}" "${ports[i]}"
        if ! port_open 9222; then
          local n=80  # 80 * 0.3s ≈ 24s 上限
          while (( n-- > 0 )) && ! port_open 9222; do sleep 0.3; done
          port_open 9222 && echo "  [info] CDP 9222 已就绪" || echo "  [warn] CDP 9222 未就绪（该桥可能要等浏览器起来）"
        fi
        ;;
    esac
  done

  echo
  cmd_status
}

cmd_stop() {
  local want=("$@") provider port pid
  while IFS=$'\t' read -r provider port; do
    if [ ${#want[@]} -gt 0 ]; then
      local hit=0 w
      for w in "${want[@]}"; do [ "$w" = "$provider" ] && hit=1; done
      [ $hit -eq 1 ] || continue
    fi
    pid=$(cat "$LOG_DIR/$provider.pid" 2>/dev/null || true)
    if pid_alive "$pid"; then
      echo "  [stop] $provider: pid=$pid"
      kill "$pid" 2>/dev/null || true
      local n=50  # 50 * 0.2s = 10s 上限
      while (( n-- > 0 )) && pid_alive "$pid"; do sleep 0.2; done
      pid_alive "$pid" && { echo "  [kill] $provider: 未按时退出，SIGKILL"; kill -9 "$pid" 2>/dev/null || true; }
    else
      echo "  [skip] $provider: 无存活 pid（端口若在监听说明不是本脚本起的，不动）"
    fi
    rm -f "$LOG_DIR/$provider.pid"
  done < <(list_providers)
}

main() {
  local cmd="${1:-start}"; shift || true
  case "$cmd" in
    start|up)       cmd_start "$@" ;;
    stop|down)      cmd_stop "$@" ;;
    restart)        cmd_stop "$@"; echo; cmd_start "$@" ;;
    status|ps)      cmd_status ;;
    -h|--help|help) sed -n '1,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
    *) echo "未知子命令: $cmd（start|stop|restart|status）" >&2; exit 2 ;;
  esac
}

main "$@"
