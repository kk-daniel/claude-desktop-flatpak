#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

manifest="ai.claude.Claude.yaml"
key="anthropic-apt-key.asc"
package="claude-desktop"
repo_uri="https://downloads.claude.ai/claude-desktop/apt/stable"
suite="stable"
component="main"
# Vendored copy of https://downloads.claude.ai/claude-desktop/key.asc. APT trusts
# whatever Signed-By points at, so pinning the fingerprint here is what turns
# "APT accepted the repository" into "Anthropic signed it". It matches the
# fingerprint documented at https://code.claude.com/docs/en/desktop-linux.
fingerprint="31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"

die() {
  echo "Error: $*" >&2
  exit 1
}

work="$(mktemp -d)"
manifest_tmp=""
cleanup() {
  rm -rf "$work"
  # Guarded, and `return 0`, so a failing test here cannot become the script's
  # exit status and turn a successful run into exit 1.
  [ -n "$manifest_tmp" ] && rm -f "$manifest_tmp"
  return 0
}
trap cleanup EXIT

# Dearmor first, and hand APT this exact file, so that what we enumerate below
# and what APT trusts are the same bytes by construction. Enumerating the
# armored file instead would be a real bypass: `gpg --show-keys` parses only
# blocks labelled "PGP PUBLIC KEY BLOCK" and silently ignores one labelled, say,
# "PGP ARMORED FILE", while APT's verifier decodes every block and trusts every
# key it finds -- so a second key could ride along invisible to this check.
if ! gpg --dearmor < "$key" > "$work/trusted.gpg" 2>"$work/keys-err"; then
  die "cannot dearmor $key as an OpenPGP key file:
$(cat "$work/keys-err")"
fi

# Assert the keyring holds exactly the pinned primary key. Checking the whole
# set matters: APT trusts every key in it, so an appended one would be just as
# authoritative as the pinned one.
if ! gpg --batch --with-colons --show-keys "$work/trusted.gpg" >"$work/keys" 2>"$work/keys-err"; then
  die "cannot read $key as an OpenPGP key file:
$(cat "$work/keys-err")"
fi
primary_fprs="$(awk -F: '
  $1 == "pub" { want = 1; next }
  want && $1 == "fpr" { print $10; want = 0 }
' "$work/keys")"
[ "$primary_fprs" = "$fingerprint" ] ||
  die "$key must hold exactly the pinned key $fingerprint, found: ${primary_fprs:-none}"

# Locate APT's transport methods relative to its own binary, so this works both
# with a distro apt at /usr/bin and with one unpacked elsewhere (e.g. Homebrew
# on a non-Debian host) without hardcoding /usr/lib.
apt_bin="$(command -v apt-get)" ||
  die "apt-get not found; this script uses APT's tooling to verify the repository"
apt_methods="$(dirname "$(dirname "$(readlink -f "$apt_bin")")")/lib/apt/methods"
[ -x "$apt_methods/https" ] ||
  die "APT transport methods not found at $apt_methods"

# A throwaway APT root: no system state is read or written, and nothing is
# installed. APT only ever reads the indexes.
root="$work/apt"
mkdir -p "$root/etc/apt/sources.list.d" "$root/etc/apt/apt.conf.d" \
  "$root/etc/apt/preferences.d" "$root/var/lib/apt/lists/partial" \
  "$root/var/lib/dpkg" "$root/var/cache/apt/archives/partial"
: > "$root/var/lib/dpkg/status"

cat > "$root/etc/apt/sources.list.d/${package}.sources" <<EOF
Types: deb
URIs: $repo_uri
Suites: $suite
Components: $component
Architectures: amd64 arm64
Signed-By: $work/trusted.gpg
EOF

cat > "$root/apt.conf" <<EOF
Dir "$root";
Dir::State "var/lib/apt";
Dir::State::status "$root/var/lib/dpkg/status";
Dir::Cache "var/cache/apt";
Dir::Etc "etc/apt";
Dir::Bin::methods "$apt_methods";
Dir::Bin::dpkg "/bin/false";
APT::Architecture "amd64";
APT::Architectures { "amd64"; "arm64"; };
Acquire::Retries "3";
EOF
export APT_CONFIG="$root/apt.conf"

# This one command is the whole verification: it checks the Release signature
# against the pinned key, that Suite and Codename match what we asked for, and
# every index hash down the chain. Error-Mode=any promotes APT's warnings to
# failures so a degraded fetch cannot pass as success.
echo "Verifying $repo_uri ($suite/$component)..."
apt-get -o APT::Update::Error-Mode=any update >"$work/update.log" 2>&1 ||
  die "APT rejected the repository:
$(cat "$work/update.log")"

# APT does not write index files when verification fails, so their presence is
# a second, independent confirmation rather than a restatement of the exit code.
for arch in amd64 arm64; do
  compgen -G "$root/var/lib/apt/lists/*_binary-${arch}_Packages" >/dev/null ||
    die "APT produced no verified $arch index"
done

# Only a real `url:` key of a `type: extra-data` source counts, at exactly the
# indentation of that source's own keys. Anything looser also matches the URL in
# a comment, or one nested a level deeper under an ignored `x-` property --
# neither of which flatpak-builder fetches. That would let a manifest show
# genuine URLs to this script while building from entirely different ones.
manifest_url() {
  local arch="$1"
  awk -v suffix="_${arch}.deb" '
    { match($0, /^ */); ind = RLENGTH }
    /^ *- / { extra = 0; keyind = ind + 2 }
    /^ *(- +)?type: +extra-data *$/ { extra = 1 }
    extra && ind == keyind &&
      /^ *url: +https:\/\/downloads\.claude\.ai\/claude-desktop\/apt\// &&
      index($0, suffix) {
      print $2
      exit
    }
  ' "$manifest"
}

# Resolve one arch to "version sha256 size" from the verified index, touching
# nothing, so that every way this can fail happens before anything is written.
resolve_arch() {
  local arch="$1"
  local url version resolved resolved_url sha size
  url="$(manifest_url "$arch")"
  [ -n "$url" ] || die "could not find a $arch .deb URL in $manifest"

  # Match the whole shape before stripping it: a plain ${url##*_} would happily
  # return a nonsense "version" for a URL that carries none.
  case "$url" in
    "$repo_uri/pool/"*"/${package}_"*"_${arch}.deb") ;;
    *) die "$arch URL is not a $repo_uri pool path for $package: $url" ;;
  esac
  version="${url##*/${package}_}"
  version="${version%_${arch}.deb}"
  case "$version" in
    [0-9]*) ;;
    *) die "could not parse a version out of $arch URL: $url" ;;
  esac

  # Resolve against the verified index. --print-uris emits one line,
  # "'<url>' <filename> <size> SHA256:<hash>", and downloads nothing. It is
  # preferred over `apt-cache show` because a version that is not in the index
  # makes it exit non-zero, where apt-cache exits 0 with empty output -- a
  # missing version cannot be mistaken here for a successful lookup.
  resolved="$(apt-get download --print-uris "${package}:${arch}=${version}" 2>"$work/uris-err")" ||
    die "APT could not resolve ${package}:${arch}=${version} in the signed $arch index:
$(cat "$work/uris-err")"
  read -r resolved_url _ size sha <<<"${resolved//\'/}"
  sha="${sha#SHA256:}"

  [ -n "$resolved_url" ] && [ -n "$sha" ] && [ -n "$size" ] ||
    die "APT returned no usable entry for ${package}:${arch}=${version}: $resolved"

  # APT's own URL for the resolved package must be the one the manifest points
  # at: this is what ties the verified index entry to the file the build fetches.
  [ "$resolved_url" = "$url" ] ||
    die "signed index resolves ${package}:${arch}=${version} to $resolved_url, manifest points at $url"

  printf '%s %s %s\n' "$version" "$sha" "$size"
}

# Rewrite both arches in one pass, into a temp file beside the manifest so the
# rename stays on one filesystem and the mode and SELinux label come from the
# repo rather than $TMPDIR. awk reports how many fields it replaced: an awk that
# quietly matches nothing is otherwise indistinguishable from "already correct",
# both to this script and to the CI check that runs it.
write_manifest() {
  local counts hits urls strays
  manifest_tmp="$(mktemp "$manifest.XXXXXX")"
  chmod --reference="$manifest" "$manifest_tmp"
  counts="$(awk -v out="$manifest_tmp" \
    -v sha_amd64="$1" -v size_amd64="$2" \
    -v sha_arm64="$3" -v size_arm64="$4" '
    { match($0, /^ */); ind = RLENGTH; real = 0 }
    /^ *- / { block = ""; extra = 0; keyind = ind + 2 }
    /^ *(- +)?type: +extra-data *$/ { extra = 1 }
    extra && ind == keyind &&
      /^ *url: +https:\/\/downloads\.claude\.ai\/claude-desktop\/apt\// {
      if (index($0, "_amd64.deb")) { block = "amd64"; urls++; real = 1 }
      else if (index($0, "_arm64.deb")) { block = "arm64"; urls++; real = 1 }
    }
    # Any other mention of a pool .deb is a stray: it means the file holds a URL
    # this script would verify but flatpak-builder would not fetch, or vice versa.
    !real && /downloads\.claude\.ai\/claude-desktop\/apt\/.*\/pool\/.*claude-desktop_.*\.deb/ { strays++ }
    block != "" && ind == keyind && /^ *sha256: / {
      sub(/sha256: .*/, "sha256: " (block == "amd64" ? sha_amd64 : sha_arm64))
      hits++
    }
    block != "" && ind == keyind && /^ *size: / {
      sub(/size: .*/, "size: " (block == "amd64" ? size_amd64 : size_arm64))
      hits++
    }
    { print > out }
    END { print hits + 0, urls + 0, strays + 0 }
  ' "$manifest")"
  read -r hits urls strays <<<"$counts"
  { [ "$hits" = "4" ] && [ "$urls" = "2" ] && [ "$strays" = "0" ]; } ||
    die "manifest layout check failed: rewrote $hits/4 fields across $urls/2 extra-data sources, with $strays stray pool URL(s) the build would not fetch"
  mv "$manifest_tmp" "$manifest"
  manifest_tmp=""
}

resolved_amd64="$(resolve_arch amd64)"
resolved_arm64="$(resolve_arch arm64)"
read -r ver_amd64 sha_amd64 size_amd64 <<<"$resolved_amd64"
read -r ver_arm64 sha_arm64 size_arm64 <<<"$resolved_arm64"

# Renovate resolves each arch against its own index, so the two can drift apart.
# Nothing downstream would catch it: CI builds, installs and smoke-tests x86_64
# only, so a skew would ship an older Claude to aarch64 users unnoticed.
[ "$ver_amd64" = "$ver_arm64" ] ||
  die "manifest arch versions disagree: amd64=$ver_amd64 arm64=$ver_arm64"

write_manifest "$sha_amd64" "$size_amd64" "$sha_arm64" "$size_arm64"

echo "Verified $package $ver_amd64 against the signed index"
echo "  amd64 sha256=$sha_amd64 size=$size_amd64"
echo "  arm64 sha256=$sha_arm64 size=$size_arm64"
