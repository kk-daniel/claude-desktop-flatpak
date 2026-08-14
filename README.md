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

The checksum updater hands the verification to APT itself. It builds a
throwaway APT root pointed at Anthropic's repository, with `Signed-By` set to
the key vendored at `ai.claude.Claude/anthropic-apt-key.asc`, and runs
`apt-get update` against it — so the Release signature, the suite and codename,
and every index hash are checked by the same code Debian and Ubuntu rely on,
rather than by this repo. It then reads each `.deb`'s `sha256` and `size` out of
the verified index with `apt-cache show`, and writes them into the manifest.

Nothing is installed and no system APT state is read or written.

Two things APT cannot know are checked here: that the vendored key file holds
exactly the pinned fingerprint
(`31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE` — APT would trust any key
placed in that file), and that both architectures name the same version. The
manifest is rewritten only after both architectures resolve, and the rewrite
fails unless it replaces exactly the four expected fields.

It needs `apt-get` and `apt-cache` (any recent APT — a Homebrew one on a
non-Debian host is fine, the transport methods are located relative to the
binary), plus `gpg` and GNU coreutils.
