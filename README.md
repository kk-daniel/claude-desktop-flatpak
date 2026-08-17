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

The manifest is read, and the result validated, with PyYAML rather than scanned
line by line, because line-oriented versions of this check kept losing to legal
YAML the scanner did not model. PyYAML is not the parser flatpak-builder links
against, so what this buys is a real parser and a rewrite re-read through one —
not a byte-for-byte match with the builder. It refuses to run if the manifest holds any
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

## Updating 7-Zip

```sh
./ai.claude.Claude/update-7zip-checksum.py
```

There is nothing to verify against here. The ip7z releases publish neither a
checksum file nor a signature, so the script downloads each archive and hashes
it — trust on first use, re-affirmed on every bump. That is a property of
upstream, not a shortcut in this repo, and it is the reason the two updaters
read so differently.

What it does check, all before a byte is fetched: every source in the `7zip`
module is an ip7z release archive it knows how to pin, each source's
`only-arches` matches the architecture in its own URL, the release version
matches the filename stem (`26.02` ↔ `7z2602`), and both architectures are on
one version. The `only-arches` check is the one with teeth — a swapped pair
would build one architecture's flatpak around the other's binary, and nothing
downstream would notice.

The manifest edit works like the Claude one: the sources are located
structurally, each `sha256` is replaced by node mark so comments and formatting
survive, and the result is re-parsed and required to carry exactly what was
hashed. It needs `curl` and a `python3` with PyYAML.

Both updaters also take `--print-version`, which prints `name=version` and exits
without touching the network. CI uses it to check that refreshing checksums did
not move a version.
