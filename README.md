# Flatpak for Claude Desktop

Unofficial Flatpak packaging for Claude Desktop on Linux.

This repository is standalone. The `claude-desktop-debian` checkout may be kept
nearby as implementation reference, but the Flatpak build does not vendor or call
into it.

## Building and installing

1. `flatpak remote-add --if-not-exists --user flathub https://dl.flathub.org/repo/flathub.flatpakrepo`
2. `flatpak-builder --force-clean --user --install-deps-from=flathub --repo=repo --install builddir ai.claude.Claude/ai.claude.Claude.yaml`

## Updating Claude

Run:

```sh
./ai.claude.Claude/update-checksum.sh
./ai.claude.Claude/update-metainfo.sh
```

The checksum updater verifies Anthropic's APT release index against the signing
key pinned in `ai.claude.Claude/anthropic-apt-key.asc`
(`31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE`), then copies each .deb's
`sha256` and `size` out of the signed `Packages` index into the manifest,
keeping the `x86_64` and `aarch64` entries in sync. Anything it cannot trace
back to that signature is a hard error: a revoked or expired key, an expired or
missing `Valid-Until`, a `Release` that does not identify itself as this
repository, or a version skew between the two architectures. It needs `gpg`,
`gpgv`, `curl` and GNU `date`.

It reads the `.deb` checksums out of the signed index rather than downloading
them. To additionally fetch both `.deb`s and hash them — worth it before cutting
a release, since nothing else ever verifies the aarch64 bytes — run:

```sh
VERIFY_DEB_BYTES=1 ./ai.claude.Claude/update-checksum.sh
```
