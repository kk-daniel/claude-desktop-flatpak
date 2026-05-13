#!/usr/bin/env bash
# resolve_titlebar_style, cleanup_stale_lock, cleanup_stale_cowork_socket,
# and the Electron env/flag setup below are derived from
# aaddrick/claude-desktop-debian/scripts/launcher-common.sh. MIT licensed
# — see LICENSE.
set -euo pipefail

log_dir="${XDG_CACHE_HOME:-$HOME/.cache}/claude-flatpak"
mkdir -p "$log_dir"
log_file="$log_dir/launcher.log"

log() {
  printf '%s\n' "$*" >> "$log_file"
}

resolve_titlebar_style() {
  case "${CLAUDE_TITLEBAR_STYLE:-hybrid}" in
    hybrid|native|hidden) printf '%s\n' "${CLAUDE_TITLEBAR_STYLE:-hybrid}" ;;
    *) printf '%s\n' "hybrid" ;;
  esac
}

cleanup_stale_lock() {
  local lock_file="${XDG_CONFIG_HOME:-$HOME/.config}/Claude/SingletonLock"
  [ -L "$lock_file" ] || return 0
  local target pid
  target="$(readlink "$lock_file" 2>/dev/null)" || return 0
  pid="${target##*-}"
  case "$pid" in
    ''|*[!0-9]*) return 0 ;;
  esac
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$lock_file"
    log "Removed stale SingletonLock for PID $pid"
  fi
}

cleanup_stale_cowork_socket() {
  local sock="${XDG_RUNTIME_DIR:-/tmp}/cowork-vm-service.sock"
  [ -S "$sock" ] || return 0
  if ! pgrep -f 'cowork-vm-service\.js' >/dev/null 2>&1; then
    rm -f "$sock"
    log "Removed stale cowork-vm-service socket"
  fi
}

electron_args=()
style="$(resolve_titlebar_style)"
if [ "$style" = "hidden" ]; then
  electron_args+=(--enable-features=WindowControlsOverlay)
else
  export ELECTRON_USE_SYSTEM_TITLE_BAR=1
  electron_args+=(--disable-features=CustomTitlebar)
fi

if [ -n "${WAYLAND_DISPLAY:-}" ] && [ "${ELECTRON_OZONE_PLATFORM_HINT:-wayland}" = "wayland" ]; then
  electron_args+=(--enable-features=UseOzonePlatform,WaylandWindowDecorations --enable-wayland-ime --wayland-text-input-version=3)
fi

if [ -n "${XRDP_SESSION:-}" ]; then
  electron_args+=(--disable-gpu --disable-software-rasterizer)
fi

cleanup_stale_lock
cleanup_stale_cowork_socket

export ELECTRON_FORCE_IS_PACKAGED=true
export CHROME_DESKTOP=ai.claude.Claude.desktop
export ELECTRON_OZONE_PLATFORM_HINT="${ELECTRON_OZONE_PLATFORM_HINT:-wayland}"

exec /app/bin/zypak-wrapper.sh /app/electron/electron "${electron_args[@]}" "$@"
