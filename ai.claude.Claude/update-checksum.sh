#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

manifest="ai.claude.Claude.yaml"
apt_base="https://downloads.claude.ai/claude-desktop/apt/stable"
key="anthropic-apt-key.asc"
# Vendored copy of https://downloads.claude.ai/claude-desktop/key.asc. The
# fingerprint is pinned here as well so a swapped key file fails loudly instead
# of silently becoming the new trust root; it matches the one Anthropic
# documents at https://code.claude.com/docs/en/desktop-linux.
fingerprint="31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"

die() {
  echo "Error: $*" >&2
  exit 1
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Import the pinned key into a throwaway keyring so the caller's ~/.gnupg is
# neither read nor written.
export GNUPGHOME="$work/gnupg"
mkdir -p -m 700 "$GNUPGHOME"
gpg --batch --quiet --import "$key"

key_fingerprint="$(gpg --batch --with-colons --show-keys "$key" |
  awk -F: '$1 == "fpr" { print $10; exit }')"
[ "$key_fingerprint" = "$fingerprint" ] ||
  die "$key has fingerprint $key_fingerprint, expected $fingerprint"

# The signed release index is the trust root: it carries the SHA256 of each
# Packages index, which in turn carries the SHA256 of every .deb in the pool.
echo "Verifying APT release index..."
curl -fsSL "$apt_base/dists/stable/InRelease" -o "$work/InRelease"
gpg --batch --status-fd 3 --output "$work/Release" --decrypt "$work/InRelease" \
  3>"$work/gpg-status" 2>"$work/gpg-error" ||
  die "signature check failed on $apt_base/dists/stable/InRelease
$(cat "$work/gpg-error")"

# Field 3 of VALIDSIG is the signing key, the last field its primary key; match
# on the primary so a future signing subkey under the same key still passes.
awk -v fpr="$fingerprint" '
  $2 == "VALIDSIG" && $NF == fpr { found = 1 }
  END { exit !found }
' "$work/gpg-status" || die "InRelease is not signed by $fingerprint"

# Release files carry a short Valid-Until (currently a week), so refusing an
# expired one closes off replay of a stale but validly signed index.
valid_until="$(awk -F': ' '$1 == "Valid-Until" { print $2; exit }' "$work/Release")"
if [ -n "$valid_until" ] && [ "$(date -u -d "$valid_until" +%s)" -lt "$(date -u +%s)" ]; then
  die "APT release index expired at $valid_until"
fi

echo "Release index signed by $fingerprint, valid until $valid_until"

# Fetch a Packages index and check it against the SHA256 recorded in the
# verified Release file.
fetch_packages() {
  local arch="$1"
  local path="main/binary-${arch}/Packages"
  local expected actual

  expected="$(awk -v path="$path" '
    /^SHA256:/ { in_sha256 = 1; next }
    /^[^ ]/ { in_sha256 = 0 }
    in_sha256 && $3 == path { print $1; exit }
  ' "$work/Release")"
  [ -n "$expected" ] || die "Release file lists no SHA256 for $path"

  curl -fsSL "$apt_base/dists/stable/$path" -o "$work/Packages-$arch"
  actual="$(sha256sum "$work/Packages-$arch" | cut -d' ' -f1)"
  [ "$actual" = "$expected" ] ||
    die "$path has sha256 $actual, signed Release says $expected"
}

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
  local url filename entry sha size remote_size tmp
  url="$(manifest_url "$arch")"
  [ -n "$url" ] ||
    die "could not find Claude Desktop $arch .deb URL in $manifest"
  case "$url" in
    "$apt_base/"*) ;;
    *) die "$arch URL $url is outside the verified repo $apt_base" ;;
  esac
  filename="${url#"$apt_base/"}"

  # Take sha256/size straight from the signed index rather than downloading
  # ~170 MB per arch and hashing whatever the CDN happens to hand back.
  entry="$(awk -v RS='' -v fn="Filename: $filename" '
    {
      sha = ""; size = ""; found = 0
      n = split($0, lines, "\n")
      for (i = 1; i <= n; i++) {
        if (lines[i] == fn) found = 1
        else if (lines[i] ~ /^SHA256: /) sha = substr(lines[i], 9)
        else if (lines[i] ~ /^Size: /) size = substr(lines[i], 7)
      }
      if (found) { print sha, size; exit }
    }
  ' "$work/Packages-$arch")"
  [ -n "$entry" ] ||
    die "$filename is not listed in the signed $arch Packages index"
  sha="${entry% *}"
  size="${entry#* }"
  [ -n "$sha" ] && [ -n "$size" ] ||
    die "signed $arch Packages entry for $filename lacks SHA256 or Size"

  # The index can be published before the pool catches up; a HEAD is enough to
  # catch that here instead of at install time on a user's machine.
  remote_size="$(curl -fsSL --head "$url" |
    awk 'tolower($1) == "content-length:" { sub(/\r$/, "", $2); size = $2 } END { print size }')" ||
    die "HEAD request failed for $url"
  [ "$remote_size" = "$size" ] ||
    die "$url is ${remote_size:-unknown} bytes, signed index says $size"

  tmp="$(mktemp)"
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
  ' "$manifest" > "$tmp"
  mv "$tmp" "$manifest"

  echo "Updated Claude Desktop $arch: sha256=$sha size=$size"
}

fetch_packages amd64
fetch_packages arm64
update_arch amd64
update_arch arm64

echo "Manifest checksums updated"
