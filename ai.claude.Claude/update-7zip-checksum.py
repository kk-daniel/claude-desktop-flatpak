#!/usr/bin/env python3
"""Refresh the 7-Zip archive pins in the Flatpak manifest.

Unlike the Claude Desktop updater there is nothing to verify against: the ip7z
releases publish neither a checksum file nor a signature, so fetching each
archive and hashing it is the only option available -- trust on first use,
re-affirmed on every bump. That is a property of upstream, not a shortcut here.

What can still be got right is the manifest edit, and it is done the way
update-checksum.py does it: the sources are located structurally with PyYAML,
each sha256 is replaced by node mark so every comment and every unpinned byte
survives, and the result is re-parsed and required to say exactly what was
hashed. The awk version this replaces matched a url line and rewrote the next
sha256 line, which is a guess about layout rather than a statement about the
document.

The scope is deliberately two sources in one module. Nothing here tries to be a
general manifest checker.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
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

MODULE = "7zip"
# The arch as it appears in the archive url, mapped to the value only-arches
# must carry for that source. Checking the pair is what catches a manifest that
# builds one architecture's flatpak around the other's binary.
ARCHES = {"x64": "x86_64", "arm64": "aarch64"}

# 7-Zip names its tarballs after the version with the dots removed: release
# 26.02 ships 7z2602-linux-x64.tar.xz.
ARCHIVE_URL = re.compile(
    r"https://github\.com/ip7z/7zip/releases/download/(?P<version>[0-9][0-9.]*)/"
    r"7z(?P<compact>[0-9]+)-linux-(?P<arch>x64|arm64)\.tar\.xz\Z"
)


def die(message: str) -> NoReturn:
    sys.exit(f"Error: {message}")


def run(argv):
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def field(mapping, key):
    for k, v in mapping.value:
        if getattr(k, "value", None) == key:
            return v
    return None


def manifest_sources(node) -> dict:
    """Map url-arch -> the manifest's archive source for that arch.

    Structural, not textual: whatever the file's layout, these are exactly the
    archives flatpak-builder will fetch for this module.
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
        url_node = field(source, "url")
        # Every source in this module has to be one of the archives we pin.
        # Anything else would be fetched by the build and hashed by nothing
        # here, and this module has no reason to grow one.
        if url_node is None:
            die(f"a source in the {MODULE} module has no url")
        match = ARCHIVE_URL.fullmatch(url_node.value)
        if match is None:
            die(
                f"the {MODULE} module fetches {url_node.value}, which is not an "
                "ip7z release archive this script knows how to pin"
            )

        arch, version, compact = match.group("arch", "version", "compact")
        if arch in by_arch:
            die(f"{MANIFEST.name} has two {arch} archive sources")
        if version.replace(".", "") != compact:
            die(f"release {version} does not match the filename stem 7z{compact}")

        only = field(source, "only-arches")
        listed = [getattr(n, "value", n) for n in only.value] if only is not None else None
        want = ARCHES[arch]
        if listed != [want]:
            die(
                f"the {arch} archive has only-arches {listed or 'missing'}, "
                f"expected [{want}]. A swapped pair builds one architecture's "
                "flatpak around the other's binary, and nothing downstream "
                "would notice."
            )

        sha256 = field(source, "sha256")
        if sha256 is None:
            die(f"the {arch} archive source has no sha256")

        by_arch[arch] = {"url": url_node.value, "version": version, "sha256": sha256}

    missing = [a for a in ARCHES if a not in by_arch]
    if missing:
        die(f"{MANIFEST.name} has no {MODULE} archive source for {', '.join(missing)}")
    return by_arch


def sha256_of(url: str, work: Path) -> str:
    """Download the archive and hash it.

    curl rather than urllib: a connection that dies mid-body is a failed
    transfer here (exit 18), where urllib's bounded reads return the short
    prefix without raising and it gets hashed as though it were the archive.
    """
    archive = work / "archive.tar.xz"
    result = run(["curl", "-fL", "--retry", "3", "-o", str(archive), url])
    if result.returncode != 0:
        die(f"cannot fetch {url}: curl exited {result.returncode}\n{result.stderr.strip()}")
    with archive.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_manifest(text: str, sources: dict, resolved: dict) -> str:
    """Replace each sha256 scalar in place, by node mark.

    Editing by mark keeps every comment and every byte we are not pinning, while
    still targeting the exact scalars the parser identified.
    """
    lines = text.split("\n")
    # One edit per line, so applying them independently is safe.
    for arch in ARCHES:
        node = sources[arch]["sha256"]
        line = lines[node.start_mark.line]
        lines[node.start_mark.line] = (
            line[: node.start_mark.column] + resolved[arch]["sha256"] + line[node.end_mark.column :]
        )
    return "\n".join(lines)


def check_written(text: str, resolved: dict) -> None:
    """Re-parse the result and assert it says what we meant it to say.

    This is the post-condition that matters. It does not care how the edit was
    made -- it asks the parser what the build will now fetch, and requires that
    to be exactly what was hashed. safe_load also resolves a duplicate key to
    the last one where the node walk above took the first, so a manifest with
    two sha256 keys in a source fails here rather than being half-rewritten.
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
        match = ARCHIVE_URL.fullmatch(str(source.get("url", "")))
        if match is None:
            die(f"after rewriting, the {MODULE} module fetches {source.get('url')}")
        seen[match.group("arch")] = source

    for arch in ARCHES:
        source, want = seen.get(arch), resolved[arch]
        if source is None:
            die(f"after rewriting, {MANIFEST.name} has no {arch} archive source")
        if source.get("url") != want["url"]:
            die(f"after rewriting, the {arch} url is {source.get('url')}, expected {want['url']}")
        if str(source.get("sha256")) != want["sha256"]:
            die(
                f"after rewriting, the {arch} sha256 is {source.get('sha256')}, "
                f"expected {want['sha256']}"
            )


def main() -> None:
    text = MANIFEST.read_text()
    sources = manifest_sources(yaml.compose(text))

    # Checked before anything is fetched: resolving first and finding the skew
    # afterwards blames the download for what is a local inconsistency.
    versions = {sources[arch]["version"] for arch in ARCHES}
    if len(versions) != 1:
        die(
            "manifest arch versions disagree: "
            + ", ".join(f"{a}={sources[a]['version']}" for a in ARCHES)
        )

    with tempfile.TemporaryDirectory() as tmp:
        resolved = {}
        for arch in ARCHES:
            url = sources[arch]["url"]
            # Flushed: print() block-buffers to a pipe, which is how Actions
            # captures a step, so a stalled download would log nothing at all.
            print(f"Hashing 7-Zip {arch}...", flush=True)
            resolved[arch] = {"url": url, "sha256": sha256_of(url, Path(tmp))}

    updated = write_manifest(text, sources, resolved)
    check_written(updated, resolved)

    # Write beside the manifest so the rename stays on one filesystem, and
    # carry the manifest's own mode rather than the temp file's 0600.
    mode = MANIFEST.stat().st_mode
    handle = tempfile.NamedTemporaryFile(
        "w", dir=MANIFEST.parent, prefix=MANIFEST.name + ".", delete=False
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
    print(f"Hashed 7-Zip {version} from its release assets")
    for arch in ARCHES:
        print(f"  {arch} sha256={resolved[arch]['sha256']}")


if __name__ == "__main__":
    main()
