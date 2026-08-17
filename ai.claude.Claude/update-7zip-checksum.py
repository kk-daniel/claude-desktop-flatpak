#!/usr/bin/env python3
"""Refresh the 7-Zip archive checksums in the Flatpak manifest.

Unlike the Claude Desktop updater there is nothing to verify against: the ip7z
releases publish neither a checksum file nor a signature, so fetching each
archive and hashing it is the only option available -- trust on first use,
re-affirmed on every bump. That is a property of upstream, not a shortcut here.

What can still be got right is the manifest edit. Every source in the manifest
is accounted for structurally before anything is written, and the result is
re-parsed and required to carry exactly what was just hashed. See
manifest_pins.py for both.
"""

import hashlib
import time
import urllib.error
import urllib.request
from pathlib import Path

# yaml comes via manifest_pins so the missing-PyYAML message lives in one place.
from manifest_pins import (
    SEVENZIP,
    audit,
    compose,
    die,
    read_manifest,
    rewrite,
    write_atomically,
)

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "ai.claude.Claude.yaml"

# Per-read inactivity, not a deadline for the whole transfer.
TIMEOUT = 60
ATTEMPTS = 3


def sha256_of(url: str) -> str:
    """Download and hash, refusing a short read.

    urlopen's bounded reads never raise when the connection dies mid-body --
    http.client says as much in a comment, having chosen compatibility over
    IncompleteRead -- so what `curl -fL` reported as exit 18 became a silently
    hashed prefix, written into the manifest as though it were the archive.
    response.length is the unread remainder of Content-Length, so a complete
    read leaves it at zero.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "claude-desktop-flatpak"})
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                if response.length is None:
                    die(
                        f"{url} sent no Content-Length; refusing to hash a stream "
                        "whose end cannot be checked"
                    )
                digest = hashlib.file_digest(response, "sha256")
                if response.length:
                    die(f"{url} stopped {response.length} bytes short of its Content-Length")
            return digest.hexdigest()
        except urllib.error.HTTPError as exc:
            # A 404 is an answer, not a hiccup: the asset is not published.
            if exc.code < 500 or attempt == ATTEMPTS:
                die(f"{url} returned HTTP {exc.code} {exc.reason}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == ATTEMPTS:
                die(f"cannot fetch {url}: {exc}")
        print(f"  retrying ({attempt}/{ATTEMPTS})...", flush=True)
        time.sleep(2**attempt)
    die(f"cannot fetch {url}")  # unreachable; keeps the return type honest


def main() -> None:
    text = read_manifest(MANIFEST)
    # Audits the whole manifest, not just the archives: arch coverage,
    # only-arches, the release/filename-stem rule and version agreement all
    # happen here, before a byte is fetched.
    found = audit(compose(text, MANIFEST.name), MANIFEST.name)[SEVENZIP.name]
    version = found[next(iter(SEVENZIP.arches))].version

    resolved = {}
    for arch in SEVENZIP.arches:
        url = found[arch].url
        # Flushed: print() block-buffers to a pipe, which is how Actions
        # captures a step, so an unflushed line means a stalled download logs
        # nothing at all until the job times out.
        print(f"Hashing 7-Zip {arch}...", flush=True)
        resolved[arch] = {"sha256": sha256_of(url)}

    write_atomically(MANIFEST, rewrite(text, SEVENZIP, found, resolved, MANIFEST.name))

    print(f"Hashed 7-Zip {version} from its release assets")
    for arch in SEVENZIP.arches:
        print(f"  {arch} sha256={resolved[arch]['sha256']}")


if __name__ == "__main__":
    main()
