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

The manifest is read and validated with PyYAML rather than scanned line by line,
because earlier line-oriented versions of this check kept losing to legal YAML
the scanner did not model. Note what that does and does not claim: PyYAML is not
the parser flatpak-builder links against, and yaml.compose() below uses the
pure-Python loader even where libyaml is installed. The guarantee is "a real
YAML parser, and the rewrite re-read through one before it lands" -- see
check_written -- not "byte-for-byte the same implementation as the builder".
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import NoReturn

try:
    import yaml
except ModuleNotFoundError:
    sys.exit(
        f"Error: PyYAML is required but is not available to {sys.executable}.\n"
        "The manifest is parsed as YAML rather than scanned, so a structural\n"
        "check is possible at all. Install it (dnf install python3-pyyaml /\n"
        "apt install python3-yaml), or run this script with an interpreter that\n"
        "has it."
    )

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "ai.claude.Claude.yaml"
KEY = HERE / "anthropic-apt-key.asc"

PACKAGE = "claude-desktop"
# The manifest module these pins live in, and how --print-version labels them.
# The module is looked up by name: the two .debs are at a known place in a known
# file, so there is no reason to go hunting for them.
MODULE = "claude"
REPO_URI = "https://downloads.claude.ai/claude-desktop/apt/stable"
SUITE = "stable"
COMPONENT = "main"
# The arch as it appears in the .deb url, mapped to the value only-arches must
# carry for that source. Checking the pair is what catches a manifest that
# builds one architecture's flatpak around the other's binary.
ARCHES = {"amd64": "x86_64", "arm64": "aarch64"}
# apply_extra looks for exactly these two names, so the manifest's filename: is
# load-bearing at install time.
FILENAME = "claude-desktop-{arch}.deb"

# Vendored copy of https://downloads.claude.ai/claude-desktop/key.asc. APT trusts
# whatever Signed-By points at, so pinning the fingerprint here is what turns
# "APT accepted the repository" into "Anthropic signed it". It matches the
# fingerprint documented at https://code.claude.com/docs/en/desktop-linux.
FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"

POOL_URL = re.compile(
    re.escape(REPO_URI)
    + r"/pool/[^\s]+/"
    + re.escape(PACKAGE)
    + r"_(?P<version>[0-9][^_/\s]*)_(?P<arch>amd64|arm64)\.deb\Z"
)


def die(message: str) -> NoReturn:
    sys.exit(f"Error: {message}")


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
        field = line.split(":")
        if field[0] == "pub":
            want = True
        elif want and field[0] == "fpr":
            primaries.append(field[9])
            want = False

    if primaries != [FINGERPRINT]:
        die(
            f"{KEY.name} must hold exactly the pinned key {FINGERPRINT}, "
            f"found: {', '.join(primaries) or 'none'}"
        )
    return keyring


def apt_environment(work: Path, keyring: Path) -> dict:
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
        # Not APT's default everywhere: the built-in default for this has been
        # an absolute path in some versions, and Dir does not re-root absolute
        # values. Dropping it would let a non-Debian host's real dpkg status be
        # read, which is exactly what this throwaway root exists to avoid.
        f'Dir::State::status "{root}/var/lib/dpkg/status";\n'
        'Dir::Cache "var/cache/apt";\n'
        'Dir::Etc "etc/apt";\n'
        f'Dir::Bin::methods "{methods}";\n'
        f'APT::Architecture "{next(iter(ARCHES))}";\n'
        "APT::Architectures { " + " ".join(f'"{a}";' for a in ARCHES) + " };\n"
        'Acquire::Retries "3";\n'
        # Without these a stalled mirror blocks until the job's own limit, which
        # on Actions is six hours.
        'Acquire::http::Timeout "30";\n'
        'Acquire::https::Timeout "30";\n'
    )
    return {**os.environ, "APT_CONFIG": str(config), "LC_ALL": "C"}


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
        # Path.glob, not glob.glob: the latter treats its whole argument as a
        # pattern, and the argument here is built from $TMPDIR, so a temp
        # directory containing [ or * made this report a missing index that APT
        # had in fact produced.
        if not any(lists.glob(f"*_binary-{arch}_Packages")):
            die(f"APT produced no verified {arch} index")


def field(mapping, key):
    for k, v in mapping.value:
        if getattr(k, "value", None) == key:
            return v
    return None


def manifest_sources(node) -> dict:
    """Map url-arch -> the manifest's extra-data source for that arch.

    The module is found by name and only its own sources are read. Scanning the
    whole document for every `type: extra-data` would be both wider and weaker:
    wider because a source elsewhere is not this script's to pin, and weaker
    because a single-level walk does not see one nested inside another module
    anyway. What a source is pinned to belongs to whoever put it there.

    Within this module, though, an extra-data source that is not one of the two
    .debs would be fetched by the build and hashed by nothing here, so it is
    refused rather than ignored.
    """
    modules = field(node, "modules")
    if modules is None:
        die(f"{MANIFEST.name} has no modules")

    found = [m for m in modules.value if getattr(field(m, "name"), "value", None) == MODULE]
    if len(found) != 1:
        die(f"{MANIFEST.name} has {len(found)} modules named {MODULE}, expected 1")

    sources = field(found[0], "sources")
    if sources is None:
        die(f"the {MODULE} module has no sources")

    by_arch = {}
    for source in sources.value:
        # The module also carries local `type: file` sources, which fetch
        # nothing and are not ours to check.
        if getattr(field(source, "type"), "value", None) != "extra-data":
            continue

        url_node = field(source, "url")
        if url_node is None:
            die(f"an extra-data source in the {MODULE} module has no url")
        match = POOL_URL.fullmatch(url_node.value)
        if match is None:
            die(
                f"the {MODULE} module fetches {url_node.value}, which is not a "
                f"{REPO_URI} pool path for {PACKAGE}"
            )

        arch = match.group("arch")
        if arch in by_arch:
            die(f"{MANIFEST.name} has two {arch} extra-data sources")

        only = field(source, "only-arches")
        listed = [getattr(n, "value", n) for n in only.value] if only is not None else None
        want = ARCHES[arch]
        if listed != [want]:
            die(
                f"the {arch} .deb has only-arches {listed or 'missing'}, expected "
                f"[{want}]. A swapped pair builds one architecture's flatpak "
                "around the other's binary, and nothing downstream would notice."
            )

        filename = field(source, "filename")
        expected = FILENAME.format(arch=arch)
        if filename is None or filename.value != expected:
            die(
                f"the {arch} .deb has filename "
                f"{filename.value if filename is not None else 'missing'}, expected "
                f"{expected}. apply_extra looks the payload up by that name."
            )

        for key in ("sha256", "size"):
            if field(source, key) is None:
                die(f"the {arch} extra-data source has no {key}")

        by_arch[arch] = {
            "url": url_node.value,
            "version": match.group("version"),
            "sha256": field(source, "sha256"),
            "size": field(source, "size"),
        }

    missing = [a for a in ARCHES if a not in by_arch]
    if missing:
        die(f"the {MODULE} module has no extra-data source for {', '.join(missing)}")
    return by_arch


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


def value_span(text: str, node) -> tuple[int, int]:
    """The span of a scalar's own value, excluding any &anchor or !!tag.

    A ScalarNode's start_mark points at its properties, not its value, so
    replacing the whole extent of `sha256: &pin !!str abc` deletes `&pin !!str`
    -- silently when nothing references the anchor, and leaving a dangling alias
    when something does. Properties are whitespace-delimited tokens starting
    with ! or &, and a value cannot start with either, so skipping them is
    unambiguous.
    """
    start, end = node.start_mark.index, node.end_mark.index
    while start < end and text[start] in "!&":
        while start < end and not text[start].isspace():
            start += 1
        while start < end and text[start].isspace():
            start += 1
    return start, end


def write_manifest(text: str, sources: dict, resolved: dict) -> str:
    """Replace each sha256/size value in place, by node mark.

    Editing by mark keeps every comment and every byte we are not pinning. Only
    the value is replaced, and it is re-emitted in the style it already had:
    dropping the quotes off a quoted scalar still parses, so nothing downstream
    notices, but it is a whole-line diff that verify-pins reports as "pins do
    not match the signed index" -- which would be false.

    Offsets are absolute, not (line, column): PyYAML counts NEL, LS and PS as
    line breaks where str.split("\\n") does not, so line bookkeeping can land a
    rewrite on the wrong line of a file containing any of them.
    """
    edits = []
    for arch in ARCHES:
        for key in ("sha256", "size"):
            node = sources[arch][key]
            # The C loader reports a plain scalar's style as '' where the Python
            # one gives None; normalise so this does not depend on which is used.
            style = node.style or None
            value = resolved[arch][key]
            if style in ("'", '"'):
                value = f"{style}{value}{style}"
            elif style is not None:
                die(f"cannot rewrite a block scalar (style {style!r}) for {arch} {key}")
            edits.append((node, value))

    # Descending, so replacing one value cannot shift the offsets of the next.
    for node, value in sorted(edits, key=lambda e: e[0].start_mark.index, reverse=True):
        start, end = value_span(text, node)
        text = text[:start] + value + text[end:]
    return text


def check_written(text: str, resolved: dict) -> None:
    """Re-parse the result and assert it says what we meant it to say.

    This is the post-condition that matters. It does not care how the edit was
    made -- it asks the parser flatpak-builder uses what the build will now
    fetch, and requires that to be exactly the verified sources.
    """
    document = yaml.safe_load(text)
    modules = [
        m
        for m in document.get("modules", [])
        if isinstance(m, dict) and m.get("name") == MODULE
    ]
    if len(modules) != 1:
        die(f"after rewriting, {MANIFEST.name} has {len(modules)} modules named {MODULE}")

    seen = {}
    for source in modules[0].get("sources", []) or []:
        if not isinstance(source, dict):
            die(f"after rewriting, the {MODULE} module has a source that is not a mapping")
        if source.get("type") != "extra-data":
            continue
        match = POOL_URL.fullmatch(str(source.get("url", "")))
        if match is None:
            die(f"after rewriting, the {MODULE} module fetches {source.get('url')}")
        seen[match.group("arch")] = source

    for arch in ARCHES:
        source, want = seen.get(arch), resolved[arch]
        if source is None:
            die(f"after rewriting, {MANIFEST.name} has no {arch} extra-data source")
        if source.get("url") != want["url"]:
            die(f"after rewriting, the {arch} url is {source.get('url')}, expected {want['url']}")
        if str(source.get("sha256")) != want["sha256"]:
            die(f"after rewriting, the {arch} sha256 is {source.get('sha256')}, expected {want['sha256']}")
        if str(source.get("size")) != want["size"]:
            die(f"after rewriting, the {arch} size is {source.get('size')}, expected {want['size']}")


def main() -> None:
    # Read and validate the manifest before gpg or apt is touched. A manifest
    # this script cannot make sense of is a local problem, and spending a
    # repository fetch to discover it reports the remote as the culprit.
    text = MANIFEST.read_text(encoding="utf-8")
    sources = manifest_sources(yaml.compose(text))

    # Renovate resolves each arch against its own index, so the two can drift
    # apart. Nothing downstream would catch it: CI builds, installs and
    # smoke-tests x86_64 only, so a skew would ship an older Claude to aarch64
    # users unnoticed.
    versions = {sources[arch]["version"] for arch in ARCHES}
    if len(versions) != 1:
        die(
            "manifest arch versions disagree: "
            + ", ".join(f"{a}={sources[a]['version']}" for a in ARCHES)
        )

    if sys.argv[1:]:
        # The CI version check runs this instead of grepping the manifest, so it
        # reads the pins through the same structural code the update path uses.
        if sys.argv[1:] != ["--print-version"]:
            die(f"usage: {Path(sys.argv[0]).name} [--print-version]")
        print(f"{MODULE}={versions.pop()}")
        return

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        keyring = pinned_keyring(work)
        env = apt_environment(work, keyring)
        apt_update(env, work / "apt")

        resolved = {}
        for arch in ARCHES:
            url, size, sha256 = resolve(arch, sources[arch]["version"], env)
            if url != sources[arch]["url"]:
                die(
                    f"signed index resolves {PACKAGE}:{arch}="
                    f"{sources[arch]['version']} to {url}, manifest points at "
                    f"{sources[arch]['url']}"
                )
            resolved[arch] = {"url": url, "sha256": sha256, "size": size}

        updated = write_manifest(text, sources, resolved)
        check_written(updated, resolved)

        # Write beside the manifest so the rename stays on one filesystem, and
        # carry the manifest's own mode rather than the temp file's 0600.
        mode = MANIFEST.stat().st_mode
        handle = tempfile.NamedTemporaryFile(
            "w", dir=MANIFEST.parent, prefix=MANIFEST.name + ".", delete=False, encoding="utf-8"
        )
        try:
            handle.write(updated)
            handle.close()
            os.chmod(handle.name, mode)
            os.replace(handle.name, MANIFEST)
        except BaseException:
            os.unlink(handle.name)
            raise

    version = versions.pop()
    print(f"Verified {PACKAGE} {version} against the signed index")
    for arch in ARCHES:
        print(f"  {arch} sha256={resolved[arch]['sha256']} size={resolved[arch]['size']}")


if __name__ == "__main__":
    main()
