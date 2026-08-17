#!/usr/bin/env python3
"""Refresh the Claude Desktop .deb pins in the Flatpak manifest.

Verification is delegated to APT. A throwaway APT root is pointed at Anthropic's
repository with the vendored key as its only trust anchor, and `apt-get update`
checks the Release signature and the whole index hash chain -- the same code
Debian and Ubuntu rely on. The pins are then read out of that verified index
with `apt-get download --print-uris`. Nothing is installed and no system APT
state is read or written.

Two things APT cannot know are asserted here: that the vendored key file holds
exactly the pinned fingerprint (APT trusts any key placed in the Signed-By
file), and that both architectures name the same version.

Every source in the manifest is accounted for before anything is written, and
the result is re-parsed and re-audited afterwards. See manifest_pins.py.
"""

import os
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

# yaml comes via manifest_pins so the missing-PyYAML message lives in one place.
from manifest_pins import (
    CLAUDE,
    CLAUDE_REPO,
    audit,
    compose,
    die,
    read_manifest,
    rewrite,
    write_atomically,
)

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "ai.claude.Claude.yaml"
KEY = HERE / "anthropic-apt-key.asc"

PACKAGE = "claude-desktop"
# The repository and the URL shape live with the Pin, so the audit and the APT
# root cannot drift apart about which tree this is.
REPO_URI = CLAUDE_REPO
SUITE = "stable"
COMPONENT = "main"
ARCHES = tuple(CLAUDE.arches)

# Vendored copy of https://downloads.claude.ai/claude-desktop/key.asc. APT trusts
# whatever Signed-By points at, so pinning the fingerprint here is what turns
# "APT accepted the repository" into "Anthropic signed it". It matches the
# fingerprint documented at https://code.claude.com/docs/en/desktop-linux.
FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"

def run(argv, *, env=None):
    return subprocess.run(argv, env=env, capture_output=True, text=True, check=False)


def pinned_keyring(work: Path) -> Path:
    """Dearmor the vendored key and assert it holds exactly the pinned key.

    Dearmoring first is load-bearing, not tidiness. `gpg --show-keys` parses only
    blocks labelled "PGP PUBLIC KEY BLOCK" and silently ignores one labelled
    anything else, while APT's verifier decodes every block and trusts every key
    it finds. Enumerating the armored file would let a relabelled second key ride
    along invisibly. Handing APT this same dearmored file makes what is asserted
    and what APT trusts identical bytes by construction.
    """
    keyring = work / "trusted.gpg"
    # Let gpg write the binary keyring itself rather than routing it through a
    # text-mode pipe, which would corrupt it.
    result = run(["gpg", "--batch", "--yes", "--dearmor", "-o", str(keyring), str(KEY)])
    if result.returncode != 0:
        die(f"cannot dearmor {KEY.name} as an OpenPGP key file:\n{result.stderr.strip()}")

    listing = run(["gpg", "--batch", "--with-colons", "--show-keys", str(keyring)])
    if listing.returncode != 0:
        die(f"cannot read {KEY.name} as an OpenPGP key file:\n{listing.stderr.strip()}")

    # Collect primary fingerprints only: the `fpr` record following each `pub`.
    primaries, want = [], False
    for line in listing.stdout.splitlines():
        record = line.split(":")
        if record[0] == "pub":
            want = True
        elif want and record[0] == "fpr":
            primaries.append(record[9])
            want = False

    if primaries != [FINGERPRINT]:
        die(
            f"{KEY.name} must hold exactly the pinned key {FINGERPRINT}, "
            f"found: {', '.join(primaries) or 'none'}"
        )
    return keyring


def apt_environment(work: Path, keyring: Path) -> tuple[dict, Path]:
    """Build a throwaway APT root and return an env that points APT at it."""
    apt_get = shutil.which("apt-get")
    if apt_get is None:
        die("apt-get not found; this script uses APT's tooling to verify the repository")
    # Resolve the transport methods relative to APT's own binary so this works
    # both with a distro apt in /usr/bin and one unpacked elsewhere.
    methods = Path(apt_get).resolve().parent.parent / "lib" / "apt" / "methods"
    if not (methods / "https").exists():
        die(f"APT transport methods not found at {methods}")

    root = work / "apt"
    for relative in (
        "etc/apt/sources.list.d",
        "etc/apt/apt.conf.d",
        "etc/apt/preferences.d",
        "var/lib/apt/lists/partial",
        "var/lib/dpkg",
        "var/cache/apt/archives/partial",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "var/lib/dpkg/status").touch()

    (root / f"etc/apt/sources.list.d/{PACKAGE}.sources").write_text(
        "Types: deb\n"
        f"URIs: {REPO_URI}\n"
        f"Suites: {SUITE}\n"
        f"Components: {COMPONENT}\n"
        f"Architectures: {' '.join(ARCHES)}\n"
        f"Signed-By: {keyring}\n"
    )
    config = root / "apt.conf"
    config.write_text(
        f'Dir "{root}";\n'
        'Dir::State "var/lib/apt";\n'
        # Not the default everywhere: APT's built-in default for this has been
        # an absolute path in some versions, and Dir does not re-root absolute
        # values. Dropping it would let a non-Debian host's real dpkg status be
        # read, which is exactly what this root exists to avoid.
        f'Dir::State::status "{root}/var/lib/dpkg/status";\n'
        'Dir::Cache "var/cache/apt";\n'
        'Dir::Etc "etc/apt";\n'
        f'Dir::Bin::methods "{methods}";\n'
        f'APT::Architecture "{ARCHES[0]}";\n'
        "APT::Architectures { " + " ".join(f'"{a}";' for a in ARCHES) + " };\n"
        'Acquire::Retries "3";\n'
        # Without this a stalled mirror blocks until the job's own limit, which
        # on Actions is six hours.
        'Acquire::http::Timeout "30";\n'
        'Acquire::https::Timeout "30";\n'
    )
    return {**os.environ, "APT_CONFIG": str(config), "LC_ALL": "C"}, root


def apt_update(env: dict, root: Path) -> None:
    """Run the verification. This one command is the whole trust decision."""
    print(f"Verifying {REPO_URI} ({SUITE}/{COMPONENT})...")
    result = run(
        ["apt-get", "-o", "APT::Update::Error-Mode=any", "update"], env=env
    )
    if result.returncode != 0:
        die(f"APT rejected the repository:\n{(result.stdout + result.stderr).strip()}")

    # APT writes no index when verification fails, so their presence is a second
    # confirmation rather than a restatement of the exit status.
    lists = root / "var/lib/apt/lists"
    for arch in ARCHES:
        # Path.glob, not glob.glob: the latter treats the whole string as a
        # pattern, so a $TMPDIR containing [ or * made this report a missing
        # index that APT had in fact produced.
        if not any(lists.glob(f"*_binary-{arch}_Packages")):
            die(f"APT produced no verified {arch} index")


def resolve(arch: str, version: str, env: dict) -> tuple[str, str, str]:
    """Resolve one arch against the verified index.

    --print-uris downloads nothing and prints one line:
    "'<url>' <filename> <size> SHA256:<hash>". It is preferred over
    `apt-cache show`, which exits 0 with empty output for a version that is not
    in the index -- there, a missing version was indistinguishable from success.
    """
    result = run(
        ["apt-get", "download", "--print-uris", f"{PACKAGE}:{arch}={version}"], env=env
    )
    if result.returncode != 0:
        die(
            f"APT could not resolve {PACKAGE}:{arch}={version} in the signed "
            f"{arch} index:\n{(result.stdout + result.stderr).strip()}"
        )

    lines = [l for l in result.stdout.splitlines() if l.startswith("'")]
    if len(lines) != 1:
        die(f"expected one URI line for {PACKAGE}:{arch}={version}, got: {result.stdout!r}")

    fields = lines[0].replace("'", "").split()
    if len(fields) != 4:
        die(f"cannot parse APT's URI line for {arch}: {lines[0]!r}")
    url, _name, size, digest = fields

    # Assert the algorithm rather than stripping a prefix that may not be there:
    # APT prints the strongest hash the index carries, so if the repository ever
    # gains SHA512 an unchecked strip would write it into a sha256 field.
    if not digest.startswith("SHA256:"):
        die(f"APT reported a {digest.split(':')[0]} digest for {arch}, expected SHA256")

    # APT percent-encodes characters that are legal in Debian versions (+ and ~),
    # so compare the decoded URL against the manifest's literal one.
    return urllib.parse.unquote(url), size, digest[len("SHA256:") :]


def main() -> None:
    text = read_manifest(MANIFEST)
    # The whole manifest is audited before gpg or apt is touched. Resolving
    # first and finding the skew afterwards blamed the signed index for what is
    # a local inconsistency.
    found = audit(compose(text, MANIFEST.name), MANIFEST.name)[CLAUDE.name]
    version = found[ARCHES[0]].version

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        env, root = apt_environment(work, pinned_keyring(work))
        apt_update(env, root)

        resolved = {}
        for arch in ARCHES:
            url, size, sha256 = resolve(arch, version, env)
            if url != found[arch].url:
                die(
                    f"signed index resolves {PACKAGE}:{arch}={version} to {url}, "
                    f"manifest points at {found[arch].url}"
                )
            resolved[arch] = {"sha256": sha256, "size": size}

    write_atomically(MANIFEST, rewrite(text, CLAUDE, found, resolved, MANIFEST.name))

    print(f"Verified {PACKAGE} {version} against the signed index")
    for arch in ARCHES:
        print(f"  {arch} sha256={resolved[arch]['sha256']} size={resolved[arch]['size']}")


if __name__ == "__main__":
    main()
