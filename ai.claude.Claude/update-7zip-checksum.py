#!/usr/bin/env python3
"""Refresh the 7-Zip archive checksums in the Flatpak manifest.

Unlike the Claude Desktop updater there is nothing to verify against: the ip7z
releases publish neither a checksum file nor a signature, so fetching each
archive and hashing it is the only option available -- trust on first use,
re-affirmed on every bump. That is a property of upstream, not a shortcut here.

What can still be got right is the manifest edit. The sources are located
structurally with PyYAML, their sha256 scalars are replaced by node mark, and
the result is re-parsed and required to carry exactly what was just hashed. The
line-oriented predecessor could silently rewrite nothing -- indistinguishable,
to itself and to CI, from "already correct".
"""

from __future__ import annotations

import hashlib
import re
import urllib.request
from pathlib import Path

# yaml comes via manifest_pins so the missing-PyYAML message lives in one place.
from manifest_pins import (
    die,
    field,
    loaded_sources,
    replace_scalars,
    source_nodes,
    write_atomically,
    yaml,
)

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "ai.claude.Claude.yaml"

# 7-Zip names its tarballs after the version with the dots removed, e.g. release
# 26.02 ships 7z2602-linux-x64.tar.xz. Capturing both lets us check they agree,
# which is what would catch a mistake in Renovate's replacement template.
ARCHIVE_URL = re.compile(
    r"https://github\.com/ip7z/7zip/releases/download/"
    r"(?P<version>[0-9][0-9.]*)/"
    r"7z(?P<compact>[0-9]+)-linux-(?P<arch>x64|arm64)\.tar\.xz\Z"
)
ARCHES = ("x64", "arm64")


def archive_sources(root) -> dict:
    """Map arch -> the manifest's 7-Zip archive source node for that arch."""
    nodes = source_nodes(root, "archive")
    if len(nodes) != len(ARCHES):
        die(
            f"expected {len(ARCHES)} archive sources in {MANIFEST.name}, "
            f"found {len(nodes)}"
        )

    by_arch = {}
    for node in nodes:
        url_node = field(node, "url")
        if url_node is None:
            die(f"an archive source in {MANIFEST.name} has no url")
        match = ARCHIVE_URL.fullmatch(url_node.value)
        if match is None:
            die(f"archive source url is not a 7-Zip release asset: {url_node.value}")

        version, compact = match.group("version"), match.group("compact")
        if version.replace(".", "") != compact:
            die(
                f"7-Zip release {version} does not match the filename stem "
                f"7z{compact} in {url_node.value}"
            )

        arch = match.group("arch")
        if arch in by_arch:
            die(f"{MANIFEST.name} has two {arch} archive sources")
        sha_node = field(node, "sha256")
        if sha_node is None:
            die(f"the {arch} archive source has no sha256")
        by_arch[arch] = {"url": url_node.value, "version": version, "sha256": sha_node}

    missing = [arch for arch in ARCHES if arch not in by_arch]
    if missing:
        die(f"{MANIFEST.name} has no archive source for {', '.join(missing)}")
    return by_arch


def sha256_of(url: str) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url) as response:  # noqa: S310 - fixed https host
        for chunk in iter(lambda: response.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_written(text: str, resolved: dict) -> None:
    """Re-parse the result and require it to say what we meant it to say."""
    sources = loaded_sources(yaml.safe_load(text), "archive")
    if len(sources) != len(ARCHES):
        die(f"after rewriting, {MANIFEST.name} has {len(sources)} archive sources")

    seen = {}
    for source in sources:
        match = ARCHIVE_URL.fullmatch(str(source.get("url", "")))
        if match is None:
            die("after rewriting, an archive url is not a 7-Zip release asset")
        seen[match.group("arch")] = source

    for arch in ARCHES:
        source, want = seen.get(arch), resolved[arch]
        if source is None:
            die(f"after rewriting, {MANIFEST.name} has no {arch} archive source")
        if source.get("url") != want["url"]:
            die(f"after rewriting, the {arch} url is {source.get('url')}")
        if str(source.get("sha256")) != want["sha256"]:
            die(
                f"after rewriting, the {arch} sha256 is {source.get('sha256')}, "
                f"expected {want['sha256']}"
            )


def main() -> None:
    text = MANIFEST.read_text()
    sources = archive_sources(yaml.compose(text))

    versions = {sources[arch]["version"] for arch in ARCHES}
    if len(versions) != 1:
        die(
            "manifest 7-Zip versions disagree: "
            + ", ".join(f"{a}={sources[a]['version']}" for a in ARCHES)
        )

    resolved = {}
    for arch in ARCHES:
        url = sources[arch]["url"]
        print(f"Hashing 7-Zip {arch}...")
        resolved[arch] = {"url": url, "sha256": sha256_of(url)}

    updated = replace_scalars(
        text, [(sources[arch]["sha256"], resolved[arch]["sha256"]) for arch in ARCHES]
    )
    check_written(updated, resolved)
    write_atomically(MANIFEST, updated)

    print(f"Hashed 7-Zip {versions.pop()} from its release assets")
    for arch in ARCHES:
        print(f"  {arch} sha256={resolved[arch]['sha256']}")


if __name__ == "__main__":
    main()
