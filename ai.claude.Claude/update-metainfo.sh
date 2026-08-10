#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

url="$(grep -A 5 'filename: claude-desktop-amd64.deb' ai.claude.Claude.yaml | grep -o 'url: .*' | head -n 1 | cut -d' ' -f2)"
version="$(printf '%s\n' "$url" | sed -n 's#.*/claude-desktop_\([0-9][0-9.]*\)_amd64\.deb$#\1#p')"

if [ -z "$version" ]; then
  echo "Error: could not extract Claude version from URL: $url" >&2
  exit 1
fi

date="$(date +%Y-%m-%d)"

if grep -q "version=\"$version\"" ai.claude.Claude.metainfo.xml; then
  sed -i "s/<release version=\"$version\" date=\"[^\"]*\"/<release version=\"$version\" date=\"$date\"/" ai.claude.Claude.metainfo.xml
else
  sed -i "/<releases>/a\\    <release version=\"$version\" date=\"$date\"/>" ai.claude.Claude.metainfo.xml
fi

echo "Metainfo updated for Claude $version"
