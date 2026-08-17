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
import re
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


# What we are willing to splice into the manifest as a plain scalar. Deliberately
# narrow: every value these updaters write is a hex digest, a byte count or a
# version, so anything needing quoting or escaping is a bug upstream of here.
_PLAIN_VALUE = re.compile(r"\A[0-9A-Za-z][0-9A-Za-z._+~-]*\Z")

# PyYAML counts all of these as line breaks; str.split("\n") counts only the
# first. Spelled with escapes so the set survives an editor or a paste.
_LINE_BREAKS = "\n\r\x85\u2028\u2029"


def _value_extent(text: str, node) -> tuple[int, int]:
    """The span of a scalar node's own value, excluding its properties.

    A ScalarNode's start_mark points at its *properties*, not its value, so
    replacing start..end of `sha256: &pin !!str abc` deletes `&pin !!str` --
    silently when nothing references the anchor, and leaving a dangling alias
    when something does. Properties are whitespace-delimited tokens beginning
    with `!` or `&`, and a value token can begin with neither, so skipping them
    is unambiguous.
    """
    start, end = node.start_mark.index, node.end_mark.index
    while start < end and text[start] in "!&":
        while start < end and not text[start].isspace():
            start += 1
        while start < end and text[start].isspace():
            start += 1

    span = text[start:end]
    if any(ch in span for ch in _LINE_BREAKS):
        die(f"cannot rewrite a value spanning several lines (line {node.start_mark.line + 1})")

    # Cheap belt to the property-skipping braces: re-read the span we are about
    # to overwrite and require it to be the value we think it is. Any
    # mis-located extent becomes an error rather than a corrupted manifest.
    # compose, not safe_load: safe_load("172617692") is an int, and we are
    # comparing against the node's raw string.
    try:
        reparsed = yaml.compose(span, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        reparsed = None
    if reparsed is None or reparsed.value != node.value:
        die(f"cannot locate the value of the scalar at line {node.start_mark.line + 1}")
    return start, end


def _restyle(value: str, style) -> str:
    """Re-emit `value` in the style the manifest already uses for that scalar.

    Splicing the extent verbatim strips the quotes off a quoted scalar. The file
    still parses, so nothing downstream notices -- but it is a whole-line diff,
    and the verify-pins job reports a whole-line diff as "pins do not match the
    signed index", which is false and sends the reader somewhere else entirely.
    """
    # The C loader reports a plain scalar's style as '' where the Python one
    # gives None. Normalise so this does not depend on which is installed.
    style = style or None
    if style in ("'", '"'):
        return f"{style}{value}{style}"
    if style is None:
        return value
    die(f"cannot rewrite a block scalar (style {style!r})")


def replace_scalars(text: str, edits) -> str:
    """Splice new values into the scalars the parser identified.

    Splicing is by absolute character index rather than by (line, column):
    PyYAML counts NEL, LS and PS as line breaks and str.split("\\n") does not, so
    line bookkeeping can land a rewrite on the wrong line of any file containing
    one. Marks carry .index, which removes the bookkeeping rather than fixing it.
    """
    # Descending, with a cursor at the last span we touched: later edits cannot
    # shift earlier offsets, and an overlap -- two arches whose "different"
    # scalars are one aliased node -- is caught rather than silently applied.
    limit = len(text)
    for node, value in sorted(edits, key=lambda edit: edit[0].start_mark.index, reverse=True):
        if not _PLAIN_VALUE.match(value):
            die(f"refusing to write {value!r} into the manifest as a plain scalar")
        start, end = _value_extent(text, node)
        if end > limit:
            die("two pins resolve to the same scalar (an alias?); refusing to rewrite")
        text = text[:start] + _restyle(value, node.style) + text[end:]
        limit = start
    return text


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
