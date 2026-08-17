#!/usr/bin/env bash
# cleanup_stale_lock and the Electron env/flag setup below are derived from
# aaddrick/claude-desktop-debian/scripts/launcher-common.sh. MIT licensed
# — see LICENSE.
set -euo pipefail

log_dir="${XDG_CACHE_HOME:-$HOME/.cache}/claude-flatpak"
mkdir -p "$log_dir"
log_file="$log_dir/launcher.log"

log() {
  printf '%s\n' "$*" >> "$log_file"
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

# Claude's CLI/MCP code writes config to $CLAUDE_CONFIG_DIR/.claude.json when
# that variable is set, and to $HOME/.claude.json otherwise. Only ~/.claude is
# persisted (--persist=.claude), so a real file at $HOME/.claude.json would be
# lost across sandbox restarts. Pointing CLAUDE_CONFIG_DIR at the persisted dir
# keeps the config there without leaving anything at the home level; the app
# names this the supported relocation and refuses symlinks below the config
# root. Earlier builds redirected $HOME/.claude.json to .claude/claude.json
# instead, so fold that file into its new name and drop the stale link.
migrate_claude_config() {
  local dir="$HOME/.claude"
  local config="$dir/.claude.json"
  local legacy="$dir/claude.json"
  local link="$HOME/.claude.json"

  mkdir -p "$dir"

  if [ ! -e "$config" ] && [ -f "$legacy" ]; then
    mv "$legacy" "$config"
    log "Migrated $legacy -> $config"
  fi

  if [ -L "$link" ]; then
    rm -f "$link"
    log "Removed legacy $link symlink"
  fi
}

# No titlebar flags: the official Linux build gates titleBarOverlay itself, so
# forcing CustomTitlebar/WindowControlsOverlay here would fight its own logic.
electron_args=()

if [ -n "${WAYLAND_DISPLAY:-}" ] && [ "${ELECTRON_OZONE_PLATFORM_HINT:-wayland}" = "wayland" ]; then
  electron_args+=(--enable-features=UseOzonePlatform,WaylandWindowDecorations --enable-wayland-ime --wayland-text-input-version=3)
fi

if [ -n "${XRDP_SESSION:-}" ]; then
  electron_args+=(--disable-gpu --disable-software-rasterizer)
fi

cleanup_stale_lock
migrate_claude_config

export CLAUDE_CONFIG_DIR="$HOME/.claude"
export ELECTRON_FORCE_IS_PACKAGED=true
export CHROME_DESKTOP=ai.claude.Claude.desktop
export ELECTRON_OZONE_PLATFORM_HINT="${ELECTRON_OZONE_PLATFORM_HINT:-wayland}"
export PATH="/app/tools/podman/bin:$PATH"

exec /app/bin/zypak-wrapper.sh /app/extra/claude/claude-desktop "${electron_args[@]}" "$@"
