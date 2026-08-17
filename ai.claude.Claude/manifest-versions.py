#!/usr/bin/env python3
"""Print the pinned dependency versions from the Flatpak manifest.

    $ ./ai.claude.Claude/manifest-versions.py
    7zip=26.02
    claude=1.30096.1
    $ ./ai.claude.Claude/manifest-versions.py --pin claude
    1.30096.1

This exists so the shell around the updaters stops reading the manifest with
greps. Those depended on key ordering the updaters rewrite, parsed the version
out of a URL by slash-separated field position, and -- via `grep -m 1` -- looked
at one architecture only, which quietly undid the cross-arch agreement check the
updaters perform.

Because it audits, it can fail where a grep could not: a manifest with the two
architectures on different versions is reported here rather than surviving to
whichever step happens to notice.
"""

import argparse
from pathlib import Path

from manifest_pins import PINS, audit, compose, die, read_manifest

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "ai.claude.Claude.yaml"


def versions() -> dict:
    found = audit(compose(read_manifest(MANIFEST), MANIFEST.name), MANIFEST.name)
    # audit() has already required the arches of a pin to agree, so either one
    # answers for the family.
    return {pin.name: found[pin.name][next(iter(pin.arches))].version for pin in PINS}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pin", help="print only this pin's version, unlabelled")
    arguments = parser.parse_args()

    found = versions()
    if arguments.pin:
        if arguments.pin not in found:
            die(f"no pin named {arguments.pin!r}; known: {', '.join(sorted(found))}")
        print(found[arguments.pin])
        return
    # Sorted, `name=version` per line: the same shape the shell it replaces
    # produced, so the before/after comparison around it needs no change.
    for name in sorted(found):
        print(f"{name}={found[name]}")


if __name__ == "__main__":
    main()
