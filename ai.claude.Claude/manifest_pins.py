"""Shared helpers for the manifest pin updaters.

The manifest is read -- and every rewrite re-validated -- with PyYAML, which is
libyaml, the same parser flatpak-builder uses. That is the whole point: a
line-oriented scanner can be made to disagree with the builder about which
sources exist and where they point, and earlier versions of these scripts were.

Rewrites are applied by node mark rather than by re-serialising the document, so
comments and formatting survive untouched.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
import shutil
import sys
import tempfile
import types
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


def field(mapping, key, where=""):
    """The value node for `key` in a composed mapping node, or None.

    A duplicate key is an error. PyYAML happily composes both pairs of
    `sha256: a` / `sha256: b`; a node walk like this one would take the first
    while safe_load -- and flatpak-builder's own YAML reader -- take the last.
    That is precisely the checker-disagrees-with-builder split these updaters
    exist to rule out, so it is refused rather than resolved.
    """
    found = None
    for k, v in mapping.value:
        if getattr(k, "value", None) == key:
            if found is not None:
                die(f"{where or 'manifest'} has more than one {key}: key")
            found = v
    return found


# ---------------------------------------------------------------------------
# What this repo knows how to pin
# ---------------------------------------------------------------------------

CLAUDE_REPO = "https://downloads.claude.ai/claude-desktop/apt/stable"

POOL_URL = re.compile(
    re.escape(CLAUDE_REPO)
    + r"/pool/[^\s]+/claude-desktop_(?P<version>[0-9][^_/\s]*)_(?P<arch>amd64|arm64)\.deb\Z"
)

# 7-Zip names its tarballs after the version with the dots removed: release
# 26.02 ships 7z2602-linux-x64.tar.xz.
ARCHIVE_URL = re.compile(
    r"https://github\.com/ip7z/7zip/releases/download/(?P<version>[0-9][0-9.]*)/"
    r"7z(?P<compact>[0-9]+)-linux-(?P<arch>x64|arm64)\.tar\.xz\Z"
)


def _stem_matches_release(match) -> str | None:
    version, compact = match.group("version"), match.group("compact")
    if version.replace(".", "") != compact:
        return f"release {version} does not match the filename stem 7z{compact}"
    return None


@dataclasses.dataclass(frozen=True)
class Pin:
    """A family of manifest sources this repo knows how to pin.

    `url` must carry named groups `arch` and `version`. `arches` maps the arch
    as it appears in the URL to the value that must appear in `only-arches` --
    the cross-check nothing performed before, which is why a swapped pair was
    maintained as though it were correct.
    """

    name: str
    label: str
    type: str
    url: "re.Pattern"
    arches: "Mapping[str, str]"
    pinned: tuple
    fixed: "Mapping[str, str]" = types.MappingProxyType({})
    check: "Callable" = lambda match: None


CLAUDE = Pin(
    name="claude",
    label="Claude Desktop .deb",
    type="extra-data",
    url=POOL_URL,
    arches=types.MappingProxyType({"amd64": "x86_64", "arm64": "aarch64"}),
    pinned=("sha256", "size"),
    # apply_extra looks for exactly these two names and nothing else, so the
    # manifest's filename: is load-bearing at install time and was unchecked.
    fixed=types.MappingProxyType({"filename": "claude-desktop-{arch}.deb"}),
)

SEVENZIP = Pin(
    name="7zip",
    label="7-Zip release archive",
    type="archive",
    url=ARCHIVE_URL,
    arches=types.MappingProxyType({"x64": "x86_64", "arm64": "aarch64"}),
    pinned=("sha256",),
    check=_stem_matches_release,
)

PINS = (CLAUDE, SEVENZIP)


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------

# flatpak-builder's source types. Anything outside this set means the manifest
# has grown a feature this module does not model, which is a reason to stop.
KNOWN_TYPES = frozenset(
    {
        "archive", "file", "git", "bzr", "svn", "extra-data",
        "dir", "patch", "shell", "script", "inline",
    }
)
# Types that always fetch, and types that fetch only when given a url: rather
# than a path:. Classifying by capability rather than by type name is what makes
# "everything that fetches is accounted for" a statement about the document
# instead of a statement about one type.
ALWAYS_REMOTE = frozenset({"extra-data", "svn", "bzr"})
URL_OR_PATH = frozenset({"archive", "file", "git"})


@dataclasses.dataclass(frozen=True)
class Source:
    """One pinned source: where it is, what it points at, what we rewrite."""

    where: str
    url: str
    version: str
    values: dict


def compose(text: str, where: str):
    """Parse, with the loader named explicitly and errors reported as errors."""
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        die(f"{where} is not valid YAML:\n{exc}")
    if node is None:
        die(f"{where} is empty")
    return node


def _sequence(node, where):
    if not isinstance(node, yaml.SequenceNode):
        die(f"{where} must be a list")
    return node.value


def _walk_sources(modules, where):
    """Yield every (source node, path) in this module list, recursively.

    flatpak-builder gives every module an optional `modules:` of its own, and it
    fetches their sources like any other. Walking only the top level meant a
    source nested one deep was invisible to both the check and the
    post-condition -- reproduced with an extra-data source pointing anywhere at
    all, which both updaters passed while reporting success.
    """
    for index, module in enumerate(_sequence(modules, where)):
        at = f"{where}[{index}]"
        if isinstance(module, yaml.ScalarNode):
            die(
                f"{at} is an included file ({module.value!r}). These updaters do "
                "not follow includes: the sources one brings in would be fetched "
                "by the build and pinned by nothing here."
            )
        if not isinstance(module, yaml.MappingNode):
            die(f"{at} is not a module")

        name_node = field(module, "name", at)
        name = name_node.value if name_node is not None else at

        sources = field(module, "sources", name)
        if sources is not None:
            for position, source in enumerate(_sequence(sources, f"{name}.sources")):
                spot = f"{name}.sources[{position}]"
                if isinstance(source, yaml.ScalarNode):
                    die(
                        f"{spot} is an included file ({source.value!r}). These "
                        "updaters do not follow includes: its sources would be "
                        "fetched by the build and pinned by nothing here."
                    )
                if not isinstance(source, yaml.MappingNode):
                    die(f"{spot} is not a source")
                yield source, spot

        children = field(module, "modules", name)
        if children is not None:
            yield from _walk_sources(children, f"{name}.modules")


def _classify(source, where, claimed):
    """Account for one source: local, or claimed by exactly one Pin."""
    type_node = field(source, "type", where)
    if type_node is None:
        die(f"{where} has no type:")
    kind = type_node.value
    if kind not in KNOWN_TYPES:
        die(
            f"{where} has source type {kind!r}, which manifest_pins does not "
            "model. Teach it about the type before adding one to the manifest."
        )
    if field(source, "mirror-urls", where) is not None:
        die(f"{where} has mirror-urls:, which these updaters do not model")

    url_node = field(source, "url", where)
    path_node = field(source, "path", where)
    if kind in URL_OR_PATH and (url_node is None) == (path_node is None):
        die(f"{where} must have exactly one of url: and path:")
    if url_node is None:
        if kind in ALWAYS_REMOTE:
            die(f"{where} is a {kind} source with no url:")
        return  # genuinely local; there is nothing here to pin

    for pin in PINS:
        if pin.type == kind:
            match = pin.url.fullmatch(url_node.value)
            if match is not None:
                _record(pin, match, source, where, claimed)
                return
    die(
        f"{where} fetches {url_node.value}, which nothing in manifest_pins "
        "pins. Every source the build downloads has to be accounted for here: "
        "add a Pin, and an updater that resolves it, before adding the source."
    )


def _record(pin, match, source, where, claimed):
    arch = match.group("arch")
    problem = pin.check(match)
    if problem is not None:
        die(f"{where}: {problem}")

    bucket = claimed.setdefault(pin.name, {})
    if arch in bucket:
        die(f"{pin.label}: two {arch} sources ({bucket[arch].where} and {where})")

    only = field(source, "only-arches", where)
    want = pin.arches[arch]
    if only is None or [n.value for n in _sequence(only, f"{where}.only-arches")] != [want]:
        got = "missing" if only is None else [n.value for n in only.value]
        die(
            f"{where} has a {arch} url but only-arches {got}, expected [{want}]. "
            "A swapped pair builds one architecture's flatpak around the other's "
            "binary, and nothing downstream would notice."
        )

    for key, template in pin.fixed.items():
        node = field(source, key, where)
        expected = template.format(arch=arch)
        if node is None or node.value != expected:
            die(f"{where} has {key}: {node.value if node else 'missing'}, expected {expected}")

    values = {}
    for key in pin.pinned:
        node = field(source, key, where)
        if node is None:
            die(f"{where} has no {key}:")
        if not isinstance(node, yaml.ScalarNode):
            die(f"{where} has a non-scalar {key}:")
        values[key] = node

    bucket[arch] = Source(where=where, url=match.group(0), version=match.group("version"), values=values)


def audit(root, where: str) -> dict:
    """Check every source in the manifest; return the pinned ones, by arch.

    Not "are the sources I found still correct" but "is what I found still all
    there is". Every module is walked including nested ones, every source is
    classified, and every source that fetches must be claimed by exactly one
    Pin. Anything this cannot model is an error rather than a skip -- the
    alternative is a source the build downloads that nothing here vouches for.
    """
    modules = field(root, "modules", where)
    if modules is None:
        die(f"{where} has no modules")

    claimed: dict = {}
    for source, spot in _walk_sources(modules, "modules"):
        _classify(source, spot, claimed)

    for pin in PINS:
        found = claimed.get(pin.name, {})
        missing = [a for a in pin.arches if a not in found]
        if missing:
            die(f"{where} has no {pin.label} source for {', '.join(missing)}")
        # Checked here so it runs before any network work: resolving first and
        # discovering the skew afterwards blamed the remote index for what is a
        # local inconsistency.
        versions = {found[a].version for a in pin.arches}
        if len(versions) != 1:
            die(
                f"{pin.label} versions disagree: "
                + ", ".join(f"{a}={found[a].version}" for a in pin.arches)
            )
    return claimed


def rewrite(text: str, pin: Pin, found: dict, resolved: dict, where: str) -> str:
    """Splice the resolved values in, then re-read the result and prove it.

    The post-condition re-runs the whole audit over the rewritten bytes, so it
    re-checks every invariant against the file as it now stands rather than only
    the values, and without reference to how the edit was made.
    """
    updated = replace_scalars(
        text,
        [(found[arch].values[key], resolved[arch][key]) for arch in pin.arches for key in pin.pinned],
    )
    after = audit(compose(updated, where), where)[pin.name]
    for arch in pin.arches:
        if after[arch].url != found[arch].url:
            die(f"after rewriting, the {arch} url changed to {after[arch].url}")
        for key in pin.pinned:
            written = after[arch].values[key].value
            if written != resolved[arch][key]:
                die(
                    f"after rewriting, the {arch} {key} is {written}, "
                    f"expected {resolved[arch][key]}"
                )
    return updated


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


def read_manifest(path: Path) -> str:
    """Read the manifest as UTF-8, with its line endings left alone.

    Path.read_text() would do neither. It opens in universal-newline mode, so a
    CRLF checkout is silently rewritten to LF -- a whole-file diff that
    verify-pins then reports as "pins do not match the signed index", with no
    pin having changed. And it decodes with the locale encoding, so the em
    dashes in this manifest's comments crash the run under a legacy non-UTF-8
    locale. YAML is defined to be UTF-8 and flatpak-builder reads it as UTF-8,
    so there is nothing here to negotiate with the environment.
    """
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        die(f"{path.name} is not valid UTF-8: {exc}")


def write_atomically(path: Path, text: str) -> None:
    """Replace the file `path` names with `text`, keeping its mode.

    Resolved first: without that, a symlinked manifest -- a worktree, a
    packaging overlay -- is *replaced by* the rewrite rather than written
    through, so the file that was meant to change still holds the old pins and
    the next run reads the new regular file and reports success.

    The temp file is created alongside the target so the rename stays on one
    filesystem, and so mode and SELinux label come from the repository rather
    than from $TMPDIR.
    """
    target = path.resolve()
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(text.encode("utf-8"))
        shutil.copymode(target, temporary)
        os.replace(temporary, target)
    except BaseException:
        # Suppressed: an unlink that itself raises would replace the exception
        # actually propagating with a FileNotFoundError naming a temp file that
        # no longer exists -- the one traceback that explains nothing.
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
