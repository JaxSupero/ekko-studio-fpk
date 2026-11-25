#!/bin/bash

CMD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${CMD_DIR}/.." && pwd)"
APP_DEST="${TRIM_APPDEST:-${APP_ROOT}/target}"
SERVICE="${APP_DEST}/bin/service.mjs"
NODE="${APP_DEST}/runtime/node/bin/node"
export TRIM_PKGHOME
export HOME="${TRIM_PKGHOME}/home"
export HERMES_WEB_UI_HOME="${TRIM_PKGHOME}/studio-data"
export PORT=8648
export NODE_ENV=production
export PATH="${APP_DEST}/runtime/node/bin:${PATH}"

log_error() {
  local message="$1"
  if [ -n "${TRIM_TEMP_LOGFILE:-}" ]; then
    printf '%s\n' "$message" > "$TRIM_TEMP_LOGFILE"
  else
    printf '%s\n' "$message" >&2
  fi
}

require_runtime() {
  if [ ! -x "$NODE" ] || [ ! -f "$SERVICE" ]; then
    log_error '缺少随包提供的Node.js运行时或服务管理器。'
    return 1
  fi
}

service_call() {
  if [ -z "${TRIM_PKGHOME:-}" ]; then log_error 'fnOS 未提供TRIM_PKGHOME，无法安全定位持久化目录。'; return 1; fi
  require_runtime || return 1
  "$NODE" "$SERVICE" "$1"
}
