#!/usr/bin/env bash
# cleanup_stale_lock and the Electron env/flag setup below are derived from
# aaddrick/claude-desktop-debian/scripts/launcher-common.sh. MIT licensed
# — see LICENSE.
set -euo pipefail
# The extension scans below iterate over mount points that are empty unless the
# user installed something; without nullglob the loops would run once on a
# literal "*". No other glob in this script relies on the default behaviour.
shopt -s nullglob

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

# Tool extensions land at /app/tools/<name> via the com.visualstudio.code.tool
# extension point declared in the manifest, so the Flathub extensions built for
# VS Code work here unmodified:
#   flatpak install flathub com.visualstudio.code.tool.podman//25.08
# Nothing gates this — as in VS Code's launcher, whatever is mounted is wired up.
enable_tool_extensions() {
  local tool_dir tool_bindir
  for tool_dir in /app/tools/*; do
    tool_bindir="$tool_dir/bin"
    [ -d "$tool_bindir" ] || continue
    export PATH="$PATH:$tool_bindir"
    log "Added $tool_bindir to PATH"
  done
}

# Opt-in SDK extensions, same contract as the VS Code Flatpak. Nothing is
# declared in the manifest for these: we run on org.freedesktop.Sdk, whose own
# extension point already mounts any installed org.freedesktop.Sdk.Extension.*
# at /usr/lib/sdk/<name>. FLATPAK_ENABLE_SDK_EXT takes a comma-separated list of
# short names, or "*" for everything installed:
#   flatpak install flathub org.freedesktop.Sdk.Extension.golang//25.08
#   flatpak run --env=FLATPAK_ENABLE_SDK_EXT=golang ai.claude.Claude
enable_sdk_extensions() {
  local spec="${FLATPAK_ENABLE_SDK_EXT:-}"
  local sdk=() dir ext
  [ -n "$spec" ] || return 0

  if [ "$spec" = "*" ]; then
    for dir in /usr/lib/sdk/*; do
      sdk+=("${dir##*/}")
    done
  else
    IFS=',' read -ra sdk <<< "$spec"
  fi

  for ext in "${sdk[@]}"; do
    [ -n "$ext" ] || continue
    if [ ! -d "/usr/lib/sdk/$ext" ]; then
      log "Requested SDK extension \"$ext\" is not installed"
      continue
    fi
    log "Enabling SDK extension \"$ext\""
    if [ -f "/usr/lib/sdk/$ext/enable.sh" ]; then
      # Third-party script we don't control: drop errexit/nounset so a stray
      # unset variable or failing command in it can't take the launcher down.
      # VS Code's launcher sources these under plain `set -e`, without -u.
      set +eu
      # shellcheck source=/dev/null
      . "/usr/lib/sdk/$ext/enable.sh"
      set -eu
    else
      export PATH="$PATH:/usr/lib/sdk/$ext/bin"
    fi
  done
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
enable_tool_extensions
enable_sdk_extensions

export CLAUDE_CONFIG_DIR="$HOME/.claude"
export ELECTRON_FORCE_IS_PACKAGED=true
export CHROME_DESKTOP=ai.claude.Claude.desktop
export ELECTRON_OZONE_PLATFORM_HINT="${ELECTRON_OZONE_PLATFORM_HINT:-wayland}"

exec /app/bin/zypak-wrapper.sh /app/extra/claude/claude-desktop "${electron_args[@]}" "$@"
