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

# This key signs Anthropic's claude-code APT repo too, and the two Release files
# agree on Origin, Label, Codename, Components and Architectures — so a valid
# signature alone does not tell us which tree we are looking at. Suite (absent
# from claude-code) and Description are the only fields that distinguish them.
expect_suite="stable"
expect_description="Anthropic Claude Desktop (stable) package repository"

die() {
  echo "Error: $*" >&2
  exit 1
}

work="$(mktemp -d)"
manifest_tmp=""
cleanup() {
  rm -rf "$work"
  [ -n "$manifest_tmp" ] && rm -f "$manifest_tmp"
  return 0
}
trap cleanup EXIT

# Read one field out of the verified Release file. Splitting on the first run of
# spaces rather than on ": " keeps values that contain colons (dates) intact.
release_field() {
  awk -v k="$1:" '$1 == k { sub(/^[^ ]+[ ]+/, ""); print; exit }' "$work/Release"
}

# gpg --import would trust every key in the file, and gpgv every key in the
# keyring, so assert the file holds exactly the pinned key — not merely that its
# first key matches.
export GNUPGHOME="$work/gnupg"
mkdir -p -m 700 "$GNUPGHOME"
primary_fprs="$(gpg --batch --quiet --no-permission-warning --with-colons --show-keys "$key" 2>/dev/null |
  awk -F: '$1 == "pub" { want = 1; next } want && $1 == "fpr" { print $10; want = 0 }')"
[ "$primary_fprs" = "$fingerprint" ] ||
  die "$key must contain exactly the pinned key $fingerprint, found: ${primary_fprs:-none}"
gpg --dearmor < "$key" > "$work/keyring.gpg"

# The signed release index is the trust root: it carries the SHA256 of each
# Packages index, which in turn carries the SHA256 of every .deb in the pool.
echo "Verifying APT release index..."
curl -fsSL "$apt_base/dists/stable/InRelease" -o "$work/InRelease"

# gpgv writes --output even for a bad signature and only then exits non-zero, so
# this exit status is the gate: nothing may read $work/Release above this line.
gpgv --status-fd 3 --keyring "$work/keyring.gpg" \
  --output "$work/Release" "$work/InRelease" \
  3>"$work/gpg-status" 2>"$work/gpg-error" ||
  die "signature check failed on $apt_base/dists/stable/InRelease
$(cat "$work/gpg-error")"

# VALIDSIG is emitted for revoked and expired keys as well, so it is not a
# sufficient positive signal on its own. gpgv exits 0 in both of those cases and
# (unlike gpg) prints nothing about it, so reject on the markers explicitly.
! grep -qE '^\[GNUPG:\] (REVKEYSIG|EXPKEYSIG|KEYREVOKED|KEYEXPIRED)' "$work/gpg-status" ||
  die "InRelease is signed by a revoked or expired key"
grep -q '^\[GNUPG:\] GOODSIG ' "$work/gpg-status" ||
  die "InRelease carries no good signature"

# Field 3 of VALIDSIG is the signing key, the last field its primary key; match
# on the primary so a future signing subkey under the same key still passes.
awk -v fpr="$fingerprint" '
  $2 == "VALIDSIG" && $NF == fpr { found = 1 }
  END { exit !found }
' "$work/gpg-status" || die "InRelease is not signed by $fingerprint"

suite="$(release_field Suite)"
[ "$suite" = "$expect_suite" ] ||
  die "signed Release has Suite '$suite', expected '$expect_suite'"
description="$(release_field Description)"
[ "$description" = "$expect_description" ] ||
  die "signed Release has Description '$description', expected '$expect_description'"

# Release files carry a short Valid-Until (currently a week), so refusing an
# expired one closes off replay of a stale but validly signed index. Resolve the
# timestamp before comparing: inside an `if` condition set -e is suppressed, so
# an unparseable date would otherwise skip the check instead of failing it.
valid_until="$(release_field Valid-Until)"
[ -n "$valid_until" ] || die "signed Release carries no Valid-Until"
valid_until_epoch="$(date -u -d "$valid_until" +%s)" ||
  die "cannot parse Valid-Until '$valid_until' (GNU date required)"
[ "$valid_until_epoch" -gt "$(date -u +%s)" ] ||
  die "APT release index expired at $valid_until"

echo "Release index signed by $fingerprint, valid until $valid_until"

# Fetch a Packages index and check it against the SHA256 recorded in the
# verified Release file. Preferring the by-hash path keeps a publish that lands
# mid-run from rotating the index out from under the Release we just verified.
fetch_packages() {
  local arch="$1"
  local path="main/binary-${arch}/Packages"
  local expected actual url

  expected="$(awk -v path="$path" '
    /^SHA256:/ { in_sha256 = 1; next }
    /^[^ ]/ { in_sha256 = 0 }
    in_sha256 && $3 == path { print $1; exit }
  ' "$work/Release")"
  [ -n "$expected" ] || die "Release file lists no SHA256 for $path"

  if [ "$(release_field Acquire-By-Hash)" = "yes" ]; then
    url="$apt_base/dists/stable/main/binary-${arch}/by-hash/SHA256/$expected"
  else
    url="$apt_base/dists/stable/$path"
  fi

  curl -fsSL "$url" -o "$work/Packages-$arch"
  actual="$(sha256sum "$work/Packages-$arch" | cut -d' ' -f1)"
  [ "$actual" = "$expected" ] ||
    die "$url has sha256 $actual, signed Release says $expected"
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

# Resolve one arch to "version sha256 size" without touching the manifest, so
# that every way this can fail happens before anything has been written.
# Everything printed here except that line has to go to stderr.
resolve_arch() {
  local arch="$1"
  local url filename entry sha size actual remote_size version
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
  entry="$(awk -v RS='' -v FS='\n' -v fn="Filename: $filename" '
    {
      sha = ""; size = ""; found = 0
      for (i = 1; i <= NF; i++) {
        if ($i == fn) found = 1
        else if ($i ~ /^SHA256: /) sha = substr($i, 9)
        else if ($i ~ /^Size: /) size = substr($i, 7)
      }
      if (found) { print sha, size; exit }
    }
  ' "$work/Packages-$arch")"
  [ -n "$entry" ] ||
    die "$filename is not listed in the signed $arch Packages index"
  read -r sha size <<<"$entry"
  [ -n "$sha" ] && [ -n "$size" ] ||
    die "signed $arch Packages entry for $filename lacks SHA256 or Size"

  # The index can be published before the pool catches up; a HEAD is enough to
  # catch that here instead of at install time on a user's machine. curl's
  # write-out reports the final response, so a redirect hop cannot supply it.
  remote_size="$(curl -fsSLI -o /dev/null -w '%header{content-length}' "$url")" ||
    die "HEAD request failed for $url"
  [ "$remote_size" = "$size" ] ||
    die "$url is ${remote_size:-unknown} bytes, signed index says $size"

  # Nothing downstream ever hashes the aarch64 .deb — CI builds and installs
  # x86_64 only — so allow an opt-in end-to-end check for release builds, where
  # the extra ~340 MB buys proof that the pool matches what was signed.
  if [ "${VERIFY_DEB_BYTES:-0}" = "1" ]; then
    echo "Downloading $arch .deb to verify its bytes..." >&2
    curl -fsSL "$url" -o "$work/deb-$arch"
    actual="$(sha256sum "$work/deb-$arch" | cut -d' ' -f1)"
    [ "$actual" = "$sha" ] ||
      die "$url has sha256 $actual, signed index says $sha"
  fi

  version="${filename##*/claude-desktop_}"
  version="${version%%_*}"
  [ -n "$version" ] || die "could not parse a version out of $filename"

  printf '%s %s %s\n' "$version" "$sha" "$size"
}

# Rewrite both arches in a single pass, into a temp file alongside the manifest
# so the rename stays on one filesystem — atomic, and the mode and SELinux label
# come from the repo rather than from $TMPDIR.
write_manifest() {
  manifest_tmp="$(mktemp "$manifest.XXXXXX")"
  chmod --reference="$manifest" "$manifest_tmp"
  awk -v sha_amd64="$1" -v size_amd64="$2" -v sha_arm64="$3" -v size_arm64="$4" '
    # A new list item always ends the previous source block, so a stanza that
    # lacks a size: line can no longer bleed its arch into the next one.
    /^[[:space:]]*-[[:space:]]/ { block = "" }
    /url: https:\/\/downloads\.claude\.ai\/claude-desktop\/apt\// {
      if (index($0, "_amd64.deb")) block = "amd64"
      else if (index($0, "_arm64.deb")) block = "arm64"
    }
    block != "" && /^[[:space:]]*sha256:/ {
      sub(/sha256: .*/, "sha256: " (block == "amd64" ? sha_amd64 : sha_arm64))
    }
    block != "" && /^[[:space:]]*size:/ {
      sub(/size: .*/, "size: " (block == "amd64" ? size_amd64 : size_arm64))
    }
    { print }
  ' "$manifest" > "$manifest_tmp"
  mv "$manifest_tmp" "$manifest"
  manifest_tmp=""
}

fetch_packages amd64
fetch_packages arm64

resolved_amd64="$(resolve_arch amd64)"
resolved_arm64="$(resolve_arch arm64)"
read -r ver_amd64 sha_amd64 size_amd64 <<<"$resolved_amd64"
read -r ver_arm64 sha_arm64 size_arm64 <<<"$resolved_arm64"

# Nothing else compares the two: Renovate resolves versions from the amd64 index
# alone, and CI builds, installs and smoke-tests x86_64 only, so a skew here
# would ship an older Claude to aarch64 users unnoticed.
[ "$ver_amd64" = "$ver_arm64" ] ||
  die "manifest arch versions disagree: amd64=$ver_amd64 arm64=$ver_arm64"

write_manifest "$sha_amd64" "$size_amd64" "$sha_arm64" "$size_arm64"

echo "Updated Claude Desktop $ver_amd64 amd64: sha256=$sha_amd64 size=$size_amd64"
echo "Updated Claude Desktop $ver_arm64 arm64: sha256=$sha_arm64 size=$size_arm64"
echo "Manifest checksums updated"
