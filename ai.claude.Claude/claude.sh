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

# Claude's CLI/MCP code writes config to ~/.claude.json. Only ~/.claude is
# persisted (--persist=.claude), so a real file at $HOME/.claude.json would
# be lost across sandbox restarts. Redirect via a relative symlink into the
# persisted dir; create an empty JSON if no config has been written yet.
ensure_claude_json_link() {
  local link="$HOME/.claude.json"
  local rel_target=".claude/claude.json"
  local abs_target="$HOME/$rel_target"

  mkdir -p "$HOME/.claude"
  [ -e "$abs_target" ] || printf '{}\n' > "$abs_target"

  if [ ! -L "$link" ] || [ "$(readlink "$link")" != "$rel_target" ]; then
    ln -sfn "$rel_target" "$link"
    log "Linked $link -> $rel_target"
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
ensure_claude_json_link

export ELECTRON_FORCE_IS_PACKAGED=true
export CHROME_DESKTOP=ai.claude.Claude.desktop
export ELECTRON_OZONE_PLATFORM_HINT="${ELECTRON_OZONE_PLATFORM_HINT:-wayland}"
export PATH="/app/tools/podman/bin:$PATH"

exec /app/bin/zypak-wrapper.sh /app/extra/claude/claude-desktop "${electron_args[@]}" "$@"
