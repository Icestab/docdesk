#!/usr/bin/env bash
# docdesk —— 本地一次性文档分析台
#   ./docdesk.sh              # 只监听 127.0.0.1:8081
#   ./docdesk.sh --lan        # 监听局域网，带一次性令牌
#   ./docdesk.sh --model defiant --idle 15 [--ctx 65536]
#   ./docdesk.sh stop         # 停掉正在跑的服务
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/.venv/bin/python"
PORT=8081
LLAMA_PORT=8080
LAN=0
MODEL="main"
CTX=""
IDLE=30
DEBUG=0
export DOCDESK_MODEL_DIR="${DOCDESK_MODEL_DIR:-/models}"

# 模型服务二进制：环境变量 > PATH > 常见编译目录
BIN="${DOCDESK_LLAMA:-$(command -v llama-server || true)}"
if [[ -z "$BIN" || ! -x "$BIN" ]]; then
  for c in $HOME/src/llama.cpp-*/build/bin/llama-server \
           /opt/llama.cpp/build/bin/llama-server \
           "$HOME/src/llama.cpp/build/bin/llama-server"; do
    if [[ -x "$c" ]]; then BIN="$c"; break; fi
  done
fi

# 模型清单放在 models.conf（不进仓库，参考 models.conf.example）
mimo_path=""; defiant_path=""; ornith_path=""
CONF="${DOCDESK_CONF:-$HERE/models.conf}"
[[ -f "$CONF" ]] && . "$CONF"

usage(){ sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lan) LAN=1 ;;
    --model) MODEL="${2:?}"; shift ;;
    --ctx) CTX="${2:?}"; shift ;;
    --idle) IDLE="${2:?}"; shift ;;
    --port) PORT="${2:?}"; shift ;;
    --debug) DEBUG=1 ;;
    stop) MODE=stop ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 1 ;;
  esac
  shift
done

PIDS=/dev/shm/docdesk
mkdir -p "$PIDS"

stop_all(){
  for f in "$PIDS"/app-"$PORT".pid "$PIDS"/llama-"$LLAMA_PORT".pid; do
    [[ -f "$f" ]] || continue
    pid=$(cat "$f" 2>/dev/null || true)
    [[ -n "${pid:-}" ]] && kill "$pid" 2>/dev/null
    rm -f "$f"
  done
  sleep 0.5
  for pid in $(pgrep -f "$HERE/app.py" 2>/dev/null); do
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    kill -9 "$pid" 2>/dev/null
  done
}

if [[ "${MODE:-}" == "stop" ]]; then
  stop_all
  echo "docdesk 已停止，内存与端口都已释放。"
  exit 0
fi

# --model 可以是 models.conf 里的名字，也可以直接给 .gguf 路径
var="${MODEL}_path"
GGUF="${!var:-}"
if [[ -z "$GGUF" && -f "$MODEL" ]]; then GGUF="$MODEL"; fi
if [[ -z "$GGUF" ]]; then
  echo "没有叫 \"$MODEL\" 的模型。在 models.conf 里加一行 ${MODEL}_path=/path/to.gguf，"
  echo "或者直接：./docdesk.sh --model /path/to/model.gguf"
  exit 1
fi
[[ -f "$GGUF" ]] || { echo "模型文件不存在: $GGUF"; exit 1; }

# 首次运行自动建 venv（只有解析文档用的四个库）
if [[ ! -x "$PY" ]]; then
  echo "首次运行：创建 .venv 并安装解析库（约 40 MB）…"
  python3 -m venv "$HERE/.venv" || { echo "venv 创建失败"; exit 1; }
  "$HERE/.venv/bin/python" -m pip install -q --disable-pip-version-check \
      pypdfium2 pillow python-docx openpyxl || { echo "依赖安装失败（需要联网）"; exit 1; }
fi

if [[ -z "$BIN" || ! -x "$BIN" ]]; then
  echo "找不到 llama-server。装一个，或者：DOCDESK_LLAMA=/path/to/llama-server ./docdesk.sh"
  exit 1
fi
if [[ -z "$CTX" ]]; then
  # IQ4_XS 的 Defiant 权重更大，96k 会 OOM，降到 64k
  case "$MODEL" in defiant) CTX=65536 ;; *) CTX=98304 ;; esac
fi

if ss -tln 2>/dev/null | grep -q ":$PORT "; then
  echo "端口 $PORT 被占用，可能已有一个 docdesk 在跑。"; exit 1
fi
if curl -s --max-time 1 "http://127.0.0.1:$LLAMA_PORT/health" | grep -q ok; then
  echo "8080 上已有 llama-server。先 ./docdesk.sh stop 或手动停掉。"; exit 1
fi

ulimit -c 0                    # 不产生 core dump（core 里会有文档内容）
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1

LLAMA_LOG=/dev/null
[[ $DEBUG == 1 ]] && LLAMA_LOG="/tmp/docdesk-llama-$$.log"

"$BIN" -m "$GGUF" -ngl 99 -c "$CTX" -fa on -ctk q8_0 -ctv q8_0 -ub 512 -np 1 \
  --reasoning off --host 127.0.0.1 --port "$LLAMA_PORT" --jinja \
  >"$LLAMA_LOG" 2>&1 &
LLAMA_PID=$!
echo "$LLAMA_PID" > "$PIDS/llama-$LLAMA_PORT.pid"

cleanup(){
  trap - EXIT INT TERM HUP
  [[ -n "${APP_PID:-}" ]] && kill "$APP_PID" 2>/dev/null
  if [[ -n "${LLAMA_PID:-}" ]] && kill -0 "$LLAMA_PID" 2>/dev/null; then
    kill "$LLAMA_PID" 2>/dev/null
    for _ in $(seq 1 25); do kill -0 "$LLAMA_PID" 2>/dev/null || break; sleep 0.2; done
    kill -9 "$LLAMA_PID" 2>/dev/null
  fi
  rm -f "$PIDS/llama-$LLAMA_PORT.pid" "$PIDS/app-$PORT.pid"
  rmdir "$PIDS" 2>/dev/null
  echo
  echo "已退出：内存清空，显存释放。"
}
trap cleanup EXIT INT TERM HUP

printf '加载 %s …' "$(basename "$GGUF")"
for i in $(seq 1 240); do
  if curl -s --max-time 1 "http://127.0.0.1:$LLAMA_PORT/health" | grep -q ok; then
    printf ' 就绪\n'; break
  fi
  if ! kill -0 "$LLAMA_PID" 2>/dev/null; then
    echo; echo "模型进程退出了。加 --debug 重跑，日志在 /tmp/docdesk-llama-*.log"; exit 1
  fi
  sleep 0.75
  [[ $((i % 20)) == 0 ]] && printf '.'
done
if ! curl -s --max-time 1 "http://127.0.0.1:$LLAMA_PORT/health" | grep -q ok; then
  echo; echo "模型 180 秒内没就绪，退出（--debug 可留日志）。"; exit 1
fi

ARGS=(--port "$PORT" --idle "$IDLE")
[[ $LAN == 1 ]] && ARGS+=(--lan)
"$PY" "$HERE/app.py" "${ARGS[@]}" &
APP_PID=$!
echo "$APP_PID" > "$PIDS/app-$PORT.pid"

sleep 0.8
if ! kill -0 "$APP_PID" 2>/dev/null; then
  echo "界面进程没起来。手动跑 .venv/bin/python app.py 看看报错。"; exit 1
fi
echo
echo "浏览器打开上面那个地址。Ctrl-C 或页面上的「销毁并退出」结束会话。"
wait "$APP_PID"
