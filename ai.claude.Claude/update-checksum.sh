#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

redirect_x64="https://claude.ai/redirect/claudedotcom.v1.290130bf-1c36-4eb0-9a93-2410ca43ae53/api/desktop/win32/x64/exe/latest/redirect"
redirect_arm64="https://claude.ai/redirect/claudedotcom.v1.290130bf-1c36-4eb0-9a93-2410ca43ae53/api/desktop/win32/arm64/exe/latest/redirect"

resolve_url() {
  local redirect="$1"
  local url
  url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$redirect" 2>/dev/null || true)"
  if [ -z "$url" ] || [ "$url" = "$redirect" ]; then
    echo "Warning: could not resolve Claude installer redirect: $redirect" >&2
    return 1
  fi
  printf '%s\n' "$url"
}

manifest_url() {
  local filename="$1"
  grep -A 5 "filename: $filename" ai.claude.Claude.yaml \
    | grep -o 'url: .*' \
    | head -n 1 \
    | cut -d' ' -f2
}

derive_arm64_url() {
  printf '%s\n' "$1" | sed 's#/win32/x64/#/win32/arm64/#g; s#nest-win-x64#nest-win-arm64#g; s#Claude-Setup-x64.exe#Claude-Setup-arm64.exe#g; s#-x64.exe#-arm64.exe#g'
}

update_arch() {
  local flatpak_arch="$1"
  local filename="$2"
  local url="$3"
  local tmp
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' RETURN

  echo "Downloading $filename for $flatpak_arch..."
  curl -fL "$url" -o "$tmp"
  local sha size
  sha="$(sha256sum "$tmp" | cut -d' ' -f1)"
  size="$(wc -c < "$tmp" | tr -d ' ')"

  awk -v arch="$flatpak_arch" -v filename="$filename" -v url="$url" -v sha="$sha" -v size="$size" '
    $0 ~ "filename: " filename { in_block = 1 }
    in_block && /only-arches:/ && index($0, "[" arch "]") == 0 { in_block = 0 }
    in_block && /url:/ { sub(/url: .*/, "url: " url) }
    in_block && /sha256:/ { sub(/sha256: .*/, "sha256: " sha) }
    in_block && /size:/ { sub(/size: .*/, "size: " size); in_block = 0 }
    { print }
  ' ai.claude.Claude.yaml > "$tmp.manifest"
  mv "$tmp.manifest" ai.claude.Claude.yaml

  echo "Updated $flatpak_arch: sha256=$sha size=$size"
}

x64_url="$(resolve_url "$redirect_x64" || true)"
if [ -z "$x64_url" ]; then
  x64_url="$(manifest_url Claude-Setup-x64.exe)"
  echo "Using manifest x64 URL: $x64_url"
fi

arm64_url="$(resolve_url "$redirect_arm64" || true)"
if [ -z "$arm64_url" ]; then
  arm64_url="$(derive_arm64_url "$x64_url")"
  if ! curl -fsIL "$arm64_url" >/dev/null 2>&1; then
    arm64_url="$(manifest_url Claude-Setup-arm64.exe)"
  fi
  echo "Using arm64 URL: $arm64_url"
fi

update_arch x86_64 Claude-Setup-x64.exe "$x64_url"
update_arch aarch64 Claude-Setup-arm64.exe "$arm64_url"

echo "Manifest checksums updated"
