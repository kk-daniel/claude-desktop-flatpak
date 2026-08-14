# Flatpak for Claude Desktop

Unofficial Flatpak packaging for Claude Desktop on Linux.

This repository is standalone. The `claude-desktop-debian` checkout may be kept
nearby as implementation reference, but the Flatpak build does not vendor or call
into it.

## Building and installing

1. `flatpak remote-add --if-not-exists --user flathub https://dl.flathub.org/repo/flathub.flatpakrepo`
2. `flatpak-builder --force-clean --user --install-deps-from=flathub --repo=repo --install builddir ai.claude.Claude/ai.claude.Claude.yaml`

## Keyring and staying signed in

Claude Desktop keeps its session token through Electron's `safeStorage`, which
is Chromium's `os_crypt` underneath. That picks a keyring backend from
`XDG_CURRENT_DESKTOP` and reaches it over the session bus, so the backend has to
be granted in the manifest. This build grants only KWallet
(`org.kde.kwalletd5`, `org.kde.kwalletd6`).

**On GNOME and other non-KDE desktops the sign-in will not be saved.** There
Chromium picks `gnome-libsecret`, which talks to `org.freedesktop.secrets`, and
that name is deliberately not granted. The app notices the backend is missing
and says so — "Your sign-in won't be saved on this device. Install and unlock a
system keyring…" — and you get a fresh login prompt on every start. Everything
else works.

The grant is withheld because Secret Service has no per-application access
control: a client that can reach `org.freedesktop.secrets` can enumerate
collections and read secrets from any *unlocked* one, and on GNOME the login
keyring is unlocked automatically at login by `gnome-keyring`'s PAM module. The
grant would therefore hand this app every other unlocked secret on the session
bus — saved browser passwords, Wi-Fi keys, other apps' tokens. Flatpak's D-Bus
proxy filters by bus name only, so there is no way to narrow it to one
collection.

`xdg-desktop-portal`'s `org.freedesktop.portal.Secret` is the right shape for
this — one per-application secret, no access to anyone else's — but it cannot be
used yet. Its key provider lives in `os_crypt::async`, whereas Electron's
`safeStorage` is built on the synchronous `OSCrypt`, whose backends are only
`kwallet*`, `gnome-libsecret` and `basic`. Wiring the two together is an
upstream Electron change; once it lands, the KWallet grants can go too.

If you would rather have the persistent login than the isolation, add the grant
locally — it is your call, not something this repo will ship:

```sh
flatpak override --user --talk-name=org.freedesktop.secrets ai.claude.Claude
```

Undo it with `--no-talk-name=org.freedesktop.secrets`.

## Config location

Only `~/.claude` is persisted (`--persist=.claude` in the manifest), so it is
mapped to `~/.var/app/ai.claude.Claude/.claude/` on the host and anything
written elsewhere in `$HOME` is discarded when the sandbox restarts. The
launcher therefore exports `CLAUDE_CONFIG_DIR="$HOME/.claude"`, which moves the
global config from `$HOME/.claude.json` to `$HOME/.claude/.claude.json` — inside
the persisted directory. Earlier builds symlinked `$HOME/.claude.json` to
`.claude/claude.json` instead; the launcher renames that file and removes the
link on first start.

The export lives in `claude.sh`, so anything that bypasses the launcher — say
`flatpak run --command=claude ai.claude.Claude` — falls back to
`$HOME/.claude.json` and will not see the app's config. Set the variable for
the whole sandbox to cover those too:

```sh
flatpak override --user --env=CLAUDE_CONFIG_DIR="$HOME/.claude" ai.claude.Claude
```

`flatpak override` does not expand variables, so `$HOME` above is expanded by
your shell and stored as an absolute path. That is fine because the sandbox
keeps the host's home path, but the override has to be redone if that path ever
changes. Undo it with `--unset-env=CLAUDE_CONFIG_DIR`.

## Adding tools to the sandbox

The sandbox ships Node (via the auto-installed `org.freedesktop.Sdk.Extension.node24`,
so `npx`-based MCP servers work out of the box). Anything beyond that can be
added with Flatpak extensions, using the same two mechanisms as the VS Code
Flatpak.

**Tool extensions** are picked up automatically. The manifest declares the
`com.visualstudio.code.tool` extension point, so the extensions built for VS Code
install here unmodified — note the `//25.08` branch, which has to match:

```sh
flatpak install flathub com.visualstudio.code.tool.podman//25.08
```

Each one mounts at `/app/tools/<name>`, and its `bin/` is appended to `PATH` at
launch. `com.visualstudio.code.tool.fish` and `.tool.git-lfs` work the same way.

**SDK extensions** are opt-in, gated by `FLATPAK_ENABLE_SDK_EXT`. Because the app
runs on `org.freedesktop.Sdk`, any installed `org.freedesktop.Sdk.Extension.*` is
already mounted at `/usr/lib/sdk/<name>`; the variable selects which of them to
actually enable, as a comma-separated list of short names:

```sh
flatpak install flathub org.freedesktop.Sdk.Extension.golang//25.08
flatpak run --env=FLATPAK_ENABLE_SDK_EXT=golang ai.claude.Claude
```

Use `*` to enable everything installed. To make it stick across launches — so the
desktop entry and URL handler get it too:

```sh
flatpak override --user --env=FLATPAK_ENABLE_SDK_EXT=golang,dotnet ai.claude.Claude
```

Each extension is enabled through its own `enable.sh` where it ships one, falling
back to putting `bin/` on `PATH`. Requesting one that isn't installed logs a line
and is otherwise ignored. The launcher records what it wired up:

```sh
tail ~/.var/app/ai.claude.Claude/cache/claude-flatpak/launcher.log
```

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
