#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

# Read the version through the same audit the updaters use. The grep this
# replaces depended on url: falling within five lines of filename:, an ordering
# the updaters are free to rewrite.
version="$(./manifest-versions.py --pin claude)"

if [ -z "$version" ]; then
  echo "Error: could not read the Claude version from the manifest" >&2
  exit 1
fi

date="$(date +%Y-%m-%d)"

if grep -q "version=\"$version\"" ai.claude.Claude.metainfo.xml; then
  sed -i "s/<release version=\"$version\" date=\"[^\"]*\"/<release version=\"$version\" date=\"$date\"/" ai.claude.Claude.metainfo.xml
else
  sed -i "/<releases>/a\\    <release version=\"$version\" date=\"$date\"/>" ai.claude.Claude.metainfo.xml
fi

echo "Metainfo updated for Claude $version"
