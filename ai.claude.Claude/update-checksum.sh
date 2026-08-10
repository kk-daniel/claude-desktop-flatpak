#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

manifest="ai.claude.Claude.yaml"

# Renovate rewrites the .deb URLs itself (the path is fully derived from the
# version), so this script only refreshes the sha256/size of whatever URLs the
# manifest currently points at.
manifest_url() {
  local arch="$1"
  awk -v suffix="_${arch}.deb" '
    /url: https:\/\/downloads\.claude\.ai\/claude-desktop\/apt\// && index($0, suffix) {
      print $2
      exit
    }
  ' "$manifest"
}

update_arch() {
  local arch="$1"
  local url tmp sha size
  url="$(manifest_url "$arch")"
  if [ -z "$url" ]; then
    echo "Error: could not find Claude Desktop $arch .deb URL in $manifest" >&2
    exit 1
  fi

  tmp="$(mktemp)"
  trap 'rm -f "$tmp" "$tmp.manifest"' RETURN

  echo "Downloading Claude Desktop $arch..."
  curl -fL "$url" -o "$tmp"
  sha="$(sha256sum "$tmp" | cut -d' ' -f1)"
  size="$(wc -c < "$tmp" | tr -d ' ')"

  awk -v suffix="_${arch}.deb" -v sha="$sha" -v size="$size" '
    /url: https:\/\/downloads\.claude\.ai\/claude-desktop\/apt\// && index($0, suffix) {
      in_block = 1
    }
    in_block && /sha256:/ { sub(/sha256: .*/, "sha256: " sha) }
    in_block && /size:/ {
      sub(/size: .*/, "size: " size)
      in_block = 0
    }
    { print }
  ' "$manifest" > "$tmp.manifest"
  mv "$tmp.manifest" "$manifest"

  echo "Updated Claude Desktop $arch: sha256=$sha size=$size"
}

update_arch amd64
update_arch arm64

echo "Manifest checksums updated"
