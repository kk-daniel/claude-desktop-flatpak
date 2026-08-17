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
./ai.claude.Claude/update-checksum.py       # the Claude Desktop .debs
./ai.claude.Claude/update-metainfo.sh       # the AppStream release list
./ai.claude.Claude/update-7zip-checksum.py  # the 7-Zip archives
```

The Claude updater hands the verification to APT itself. It builds a throwaway
APT root pointed at Anthropic's repository, with `Signed-By` set to the key
vendored at `ai.claude.Claude/anthropic-apt-key.asc`, and runs `apt-get update`
against it — so the Release signature and every index hash are checked by the
same code Debian and Ubuntu rely on, rather than by this repo. It then resolves
each `.deb` against that verified index with `apt-get download --print-uris`,
and writes the `sha256` and `size` into the manifest.

Nothing is installed and no system APT state is read or written.

Two things APT cannot know are checked here: that the vendored key file holds
exactly the pinned fingerprint
(`31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE` — APT would trust any key
placed in that file), and that both architectures name the same version.

The 7-Zip updater has nothing to verify against: the ip7z releases publish
neither a checksum file nor a signature, so it downloads each archive and hashes
it — trust on first use, re-affirmed on every bump. That asymmetry is upstream's,
not something this repo can close, and it is why the two pairs of `sha256:`
values in the manifest carry comments saying where each came from.

### How the manifest is edited

Both updaters share `ai.claude.Claude/manifest_pins.py`, which must sit beside
them — the import is by directory adjacency. It parses the manifest as YAML
rather than scanning it line by line, and **accounts for every source**: it
walks nested `modules:`, refuses string includes, and requires each source that
fetches over the network to be claimed by a declared pin. A `type: git` beside
the pinned sources, or an `extra-data` nested one module deep, is an error
rather than something to step past. After rewriting it re-parses the file and
re-runs the whole audit before the result is moved into place.

It does **not** use the same YAML implementation flatpak-builder links against —
PyYAML is a different library, and `yaml.SafeLoader` is pinned deliberately. The
guarantee is a real parser and a re-read result, not a byte-identical one.

`ai.claude.Claude/manifest-versions.py` prints the pinned versions from that
same audit; CI and `update-metainfo.sh` use it instead of grepping the manifest.
`python3 ai.claude.Claude/test_manifest_pins.py` runs the audit and rewrite
tests, which need no network.

### Requirements

`apt-get` for the Claude updater (any recent APT — a Homebrew one on a
non-Debian host is fine, the transport methods are located relative to the
binary), `gpg`, and a `python3` with PyYAML for all three scripts.

Those can pull in different directions: if APT comes from Homebrew, that
prefix's `python3` may shadow the system one and lack PyYAML. Each script names
the interpreter it is running under when the import fails, and putting a
suitable one first — `PATH=/usr/bin:$PATH ./ai.claude.Claude/update-checksum.py`
— is the straightforward fix.
