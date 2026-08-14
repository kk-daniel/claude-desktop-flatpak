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
./ai.claude.Claude/update-checksum.py
./ai.claude.Claude/update-metainfo.sh
```

The checksum updater hands the verification to APT itself. It builds a
throwaway APT root pointed at Anthropic's repository, with `Signed-By` set to
the key vendored at `ai.claude.Claude/anthropic-apt-key.asc`, and runs
`apt-get update` against it — so the Release signature and every index hash are
checked by the same code Debian and Ubuntu rely on, rather than by this repo. It
then resolves each `.deb` against that verified index with `apt-get download
--print-uris`, and writes the `sha256` and `size` into the manifest.

Nothing is installed and no system APT state is read or written.

Two things APT cannot know are checked here: that the vendored key file holds
exactly the pinned fingerprint
(`31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE` — APT would trust any key
placed in that file), and that both architectures name the same version.

The manifest is read, and the result validated, with PyYAML — the same libyaml
flatpak-builder uses — so that the sources this script verifies and the sources
the build fetches cannot disagree. It refuses to run if the manifest holds any
`extra-data` source other than the two `.deb`s, and after rewriting it re-parses
the file and requires the two sources to carry exactly the URLs, checksums and
sizes it just verified. Only then is the new manifest moved into place.

It needs `apt-get` (any recent APT — a Homebrew one on a non-Debian host is
fine, the transport methods are located relative to the binary), `gpg`, and a
`python3` with PyYAML. Note those two can pull in different directions: if APT
comes from Homebrew, that prefix's `python3` may shadow the system one and lack
PyYAML. The script names the interpreter it is running under when the import
fails, and `/usr/bin/python3 ./ai.claude.Claude/update-checksum.py` is the
straightforward fix.
