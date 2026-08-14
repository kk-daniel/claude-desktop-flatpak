"""Shared helpers for the manifest pin updaters.

The manifest is read -- and every rewrite re-validated -- with PyYAML, which is
libyaml, the same parser flatpak-builder uses. That is the whole point: a
line-oriented scanner can be made to disagree with the builder about which
sources exist and where they point, and earlier versions of these scripts were.

Rewrites are applied by node mark rather than by re-serialising the document, so
comments and formatting survive untouched.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

# Imported here rather than in each updater, and re-exported, so that the
# explanation below is the single thing a user sees when PyYAML is missing.
# Nothing in this module calls it; the updaters do.
try:
    import yaml
except ModuleNotFoundError:
    sys.exit(
        f"Error: PyYAML is required but is not available to {sys.executable}.\n"
        "The manifest is parsed with the same libyaml flatpak-builder uses, so a\n"
        "structural check is possible at all. Install it (dnf install\n"
        "python3-pyyaml / apt install python3-yaml), or run this script with an\n"
        "interpreter that has it."
    )


def die(message: str) -> NoReturn:
    sys.exit(f"Error: {message}")


def field(mapping, key):
    """Return the value node for `key` in a composed mapping node, or None."""
    for k, v in mapping.value:
        if getattr(k, "value", None) == key:
            return v
    return None


def source_nodes(root, wanted_type: str) -> list:
    """Every source node of the given `type:` across all modules."""
    modules = field(root, "modules")
    if modules is None:
        die("manifest has no modules")
    found = []
    for module in modules.value:
        sources = field(module, "sources")
        if sources is None:
            continue
        for source in sources.value:
            type_node = field(source, "type")
            if type_node is not None and type_node.value == wanted_type:
                found.append(source)
    return found


def loaded_sources(document, wanted_type: str) -> list:
    """The same selection against a plain safe_load'ed document.

    Used for the post-condition: it asks the parser what the build will now
    fetch, without reference to how the edit was made.
    """
    return [
        source
        for module in document.get("modules", [])
        if isinstance(module, dict)
        for source in module.get("sources") or []
        if isinstance(source, dict) and source.get("type") == wanted_type
    ]


def replace_scalars(text: str, edits) -> str:
    """Replace scalar values in place, given (node, new_value) pairs."""
    lines = text.split("\n")
    # Right-to-left within a line, so earlier columns keep their offsets.
    for node, value in sorted(
        edits,
        key=lambda edit: (edit[0].start_mark.line, edit[0].start_mark.column),
        reverse=True,
    ):
        line_no = node.start_mark.line
        if node.end_mark.line != line_no:
            die(f"cannot rewrite a value spanning several lines (line {line_no + 1})")
        line = lines[line_no]
        lines[line_no] = line[: node.start_mark.column] + value + line[node.end_mark.column :]
    return "\n".join(lines)


def write_atomically(path: Path, text: str) -> None:
    """Replace `path` with `text`, keeping its mode.

    The temp file is created alongside the target so the rename stays on one
    filesystem, and so mode and SELinux label come from the repository rather
    than from $TMPDIR.
    """
    mode = path.stat().st_mode
    handle = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=path.name + ".", delete=False
    )
    try:
        handle.write(text)
        handle.close()
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except BaseException:
        os.unlink(handle.name)
        raise
