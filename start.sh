#!/bin/bash
# 开源录音豆（open-recording-bean）：一条命令装好并启动。
#
#   ./start.sh            第一次：安装需要的东西并下载模型；之后：直接启动
#   ./start.sh models     下载或补全本机识别模型（实时文字和说话人）
#   ./start.sh doctor     检查这台电脑还缺什么
#   ./start.sh test       运行全部测试
#
# 可以用的环境变量：
#   RECORDING_PORT=8875        网页的端口
#   RECORDING_NO_OPEN=1        启动后不自动打开浏览器
#   RECORDING_SKIP_MODELS=1    第一次启动时不下载模型
#   HF_ENDPOINT=https://hf-mirror.com   模型下载地址（国内网络默认会自动换用这个镜像）
set -euo pipefail
cd "$(dirname "$0")"

PORT="${RECORDING_PORT:-8875}"
PY=.venv/bin/python

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# 依赖在 Python 3.10 到 3.13 上都有现成的安装包，优先用 3.12。
suitable_python() {
  local candidate
  for candidate in python3.12 python3.13 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

ensure_python() {
  [ -x "$PY" ] && return 0
  say '准备 Python 环境（只做一次）'
  local found
  if found=$(suitable_python); then
    note "使用 $("$found" --version 2>&1)"
    "$found" -m venv .venv
  elif command -v uv >/dev/null 2>&1; then
    note '没有找到 Python 3.10 到 3.13，改用 uv 准备 Python 3.12'
    uv venv --seed --python 3.12 .venv
  else
    fail '没有找到 Python 3.10 到 3.13。
请先安装其中一个版本，然后重新运行 ./start.sh：
  用 Homebrew：  brew install python@3.12
  或者到 https://www.python.org/downloads/ 下载安装包'
  fi
}

# 依赖清单变了才重新安装。
ensure_packages() {
  local stamp=.venv/.installed wanted
  wanted=$(cat backend/requirements.txt backend/requirements-live.txt | shasum -a 256 | cut -d' ' -f1)
  [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$wanted" ] && return 0
  say '安装本机服务需要的组件（只做一次，约一两分钟）'
  "$PY" -m pip install --quiet --disable-pip-version-check --upgrade pip
  # 只用现成的安装包，不在这台电脑上编译：没有编译工具的电脑也能装，Intel Mac 会自动选到还提供安装包的版本。
  "$PY" -m pip install --quiet --disable-pip-version-check --only-binary=:all: -r backend/requirements.txt -r backend/requirements-live.txt
  echo "$wanted" > "$stamp"
}

ensure_page() {
  local newest
  if [ -f dist/index.html ]; then
    newest=$(find src index.html package.json public -newer dist/index.html -type f 2>/dev/null | head -1)
    [ -z "$newest" ] && return 0
  fi
  say '构建网页（源文件有变化时才做）'
  command -v node >/dev/null 2>&1 || fail '没有找到 Node.js。请先安装 Node.js 20 或更新的版本：
  用 Homebrew：  brew install node
  或者到 https://nodejs.org 下载安装包'
  if command -v pnpm >/dev/null 2>&1; then
    pnpm install --silent
    pnpm build
  else
    npm install --no-audit --no-fund --loglevel=error
    npm run build
  fi
}

ensure_models() {
  [ "${RECORDING_SKIP_MODELS:-}" = 1 ] && return 0
  "$PY" scripts/fetch_models.py --quiet-if-complete || {
    note '模型没有下载完整。实时文字暂时不能用，其他功能不受影响。'
    note '网络恢复后运行 ./start.sh models 可以接着下载。'
  }
}

open_when_ready() {
  [ "${RECORDING_NO_OPEN:-}" = 1 ] && return 0
  local url="$1"
  (
    for _ in $(seq 1 60); do
      if curl -s -m 2 -o /dev/null "$url/api/health"; then
        open "$url" 2>/dev/null || true
        exit 0
      fi
      sleep 0.5
    done
  ) &
}

case "${1:-run}" in
  run)
    ensure_python
    ensure_packages
    ensure_page
    ensure_models
    say "开源录音豆已启动：http://127.0.0.1:$PORT"
    note '这个窗口不要关，关掉它服务就停了。按 Ctrl+C 可以停止。'
    note '第一次搜索录音豆时，系统会询问是否允许“终端”使用蓝牙，请点允许。'
    open_when_ready "http://127.0.0.1:$PORT"
    exec "$PY" -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port "$PORT" --no-access-log
    ;;
  models)
    ensure_python
    ensure_packages
    exec "$PY" scripts/fetch_models.py
    ;;
  doctor)
    if [ -x "$PY" ]; then exec "$PY" scripts/doctor.py; fi
    found=$(suitable_python) || found=python3
    exec "$found" scripts/doctor.py
    ;;
  test)
    ensure_python
    ensure_packages
    "$PY" -m pip install --quiet --disable-pip-version-check --only-binary=:all: -r backend/requirements-dev.txt
    "$PY" -m pytest -q
    if command -v pnpm >/dev/null 2>&1; then pnpm install --silent && pnpm test; else npm install --no-audit --no-fund --loglevel=error && npm test; fi
    ;;
  -h|--help|help)
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    fail "不认识的命令：$1。运行 ./start.sh help 查看用法。"
    ;;
esac
