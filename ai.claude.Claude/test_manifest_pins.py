#!/usr/bin/env python3
"""Tests for the manifest audit and the scalar rewrite.

Run: python3 ai.claude.Claude/test_manifest_pins.py

Every case here is a pure string transformation -- no network, no apt, no gpg --
because the shapes that matter cannot be produced by the real manifest. Three
rounds of review found defects that a run against the committed manifest passes
happily: a source nested one module deep, a `type: git` alongside the pinned
ones, an anchored or quoted scalar, a CRLF checkout. Those are the cases below.

Fixtures are edits of the real manifest rather than hand-written miniatures, so
a change to its layout shows up here instead of silently invalidating the tests.
"""

import unittest
from pathlib import Path

import manifest_pins as mp

MANIFEST = Path(__file__).resolve().parent / "ai.claude.Claude.yaml"
REAL = MANIFEST.read_bytes().decode("utf-8")

# A pin from the real manifest, used as an anchor for edits.
X64_SHA = "41aaba7b1235304ab5aa0624530c67ae829496cd29e875925271efdccc28c03e"
AMD64_SHA = "09e41a20a5b47ea0e5bc226d4fffa77af43ad450c7cbf5e66e56d6e4fd4ad2e9"
LOCAL_SOURCE = "      - type: file\n        path: apply_extra\n"


def audit(text):
    return mp.audit(mp.compose(text, "fixture"), "fixture")


def rewrite_one(text, key, value):
    """Rewrite the first top-level `key` in `text`, returning the new text."""
    node = mp.compose(text, "fixture")
    return mp.replace_scalars(text, [(mp.field(node, key), value)])


class AuditAcceptsTheRealManifest(unittest.TestCase):
    def test_finds_both_pin_families(self):
        found = audit(REAL)
        self.assertEqual(sorted(found), ["7zip", "claude"])
        self.assertEqual(sorted(found["claude"]), ["amd64", "arm64"])
        self.assertEqual(sorted(found["7zip"]), ["arm64", "x64"])

    def test_versions_agree_within_each_family(self):
        found = audit(REAL)
        for pin in mp.PINS:
            versions = {found[pin.name][a].version for a in pin.arches}
            self.assertEqual(len(versions), 1, f"{pin.label}: {versions}")


class AuditRejects(unittest.TestCase):
    """Every case here passed at least one earlier version of this code."""

    def assertDies(self, text, fragment):
        with self.assertRaises(SystemExit) as caught:
            audit(text)
        self.assertIn(fragment, str(caught.exception))

    def test_source_nested_one_module_deep(self):
        # Reproduced against the shipped code: flatpak-builder resolved three
        # extra-data sources while both updaters exited 0.
        nested = REAL.replace(
            "  - name: claude\n",
            "  - name: claude\n"
            "    modules:\n"
            "      - name: fonts\n"
            "        buildsystem: simple\n"
            "        sources:\n"
            "          - type: extra-data\n"
            "            filename: fonts.bin\n"
            "            url: https://cdn.attacker.invalid/payload.bin\n"
            f"            sha256: {'0' * 64}\n"
            "            size: 1234\n",
            1,
        )
        self.assertDies(nested, "nothing in manifest_pins")

    def test_git_source_with_a_mutable_branch(self):
        text = REAL.replace(
            LOCAL_SOURCE,
            "      - type: git\n"
            "        url: https://attacker.invalid/toolkit.git\n"
            "        branch: main\n" + LOCAL_SOURCE,
            1,
        )
        self.assertDies(text, "nothing in manifest_pins")

    def test_file_source_with_a_url(self):
        text = REAL.replace(
            LOCAL_SOURCE,
            "      - type: file\n        url: https://attacker.invalid/x\n" + LOCAL_SOURCE,
            1,
        )
        self.assertDies(text, "nothing in manifest_pins")

    def test_svn_source(self):
        text = REAL.replace(
            LOCAL_SOURCE, "      - type: svn\n        url: svn://attacker.invalid/x\n" + LOCAL_SOURCE, 1
        )
        self.assertDies(text, "nothing in manifest_pins")

    def test_unmodelled_source_type(self):
        text = REAL.replace(LOCAL_SOURCE, "      - type: nonesuch\n        path: x\n" + LOCAL_SOURCE, 1)
        self.assertDies(text, "does not model")

    def test_string_include_under_sources(self):
        text = REAL.replace(LOCAL_SOURCE, "      - extras.yaml\n" + LOCAL_SOURCE, 1)
        self.assertDies(text, "included file")

    def test_string_include_under_modules(self):
        text = REAL.replace("  - name: 7zip\n", "  - shared-modules/x.json\n  - name: 7zip\n", 1)
        self.assertIn("shared-modules", text)  # the fixture must actually apply
        self.assertDies(text, "included file")

    def test_null_modules(self):
        self.assertDies(REAL[: REAL.index("modules:")] + "modules:\n", "must be a list")

    def test_duplicate_key(self):
        text = REAL.replace(f"        sha256: {X64_SHA}\n", f"        sha256: {X64_SHA}\n        sha256: {'9' * 64}\n", 1)
        self.assertDies(text, "more than one sha256")

    def test_only_arches_swapped(self):
        start, end = REAL.index("- type: archive"), REAL.index("- name: claude")
        block = (
            REAL[start:end]
            .replace("only-arches: [x86_64]", "\0")
            .replace("only-arches: [aarch64]", "only-arches: [x86_64]")
            .replace("\0", "only-arches: [aarch64]")
        )
        self.assertDies(REAL[:start] + block + REAL[end:], "expected [x86_64]")

    def test_filename_not_matching_the_arch(self):
        # apply_extra hard-codes both names, so this is load-bearing at install.
        text = REAL.replace("filename: claude-desktop-amd64.deb", "filename: claude-desktop-arm64.deb", 1)
        self.assertDies(text, "expected claude-desktop-amd64.deb")

    def test_version_skew_between_arches(self):
        text = REAL.replace("download/26.02/7z2602-linux-arm64", "download/26.01/7z2601-linux-arm64", 1)
        self.assertDies(text, "versions disagree")

    def test_release_tag_not_matching_the_filename_stem(self):
        text = REAL.replace("download/26.02/7z2602-linux-x64", "download/26.02/7z2603-linux-x64", 1)
        self.assertDies(text, "filename stem")

    def test_mirror_urls(self):
        text = REAL.replace(
            f"        sha256: {X64_SHA}\n",
            f"        sha256: {X64_SHA}\n        mirror-urls: [https://attacker.invalid/x]\n",
            1,
        )
        self.assertDies(text, "mirror-urls")


class AuditAcceptsLocalSources(unittest.TestCase):
    """A source with no url fetches nothing, so it needs no pin."""

    def accepts(self, snippet):
        audit(REAL.replace(LOCAL_SOURCE, snippet + LOCAL_SOURCE, 1))

    def test_dir(self):
        self.accepts("      - type: dir\n        path: .\n")

    def test_patch(self):
        self.accepts("      - type: patch\n        path: x.patch\n")

    def test_archive_with_a_path(self):
        self.accepts("      - type: archive\n        path: vendored.tar.xz\n")


class RewritePreservesTheFile(unittest.TestCase):
    def test_round_trip_is_byte_identical(self):
        """Rewriting with the values already there must change nothing."""
        found = audit(REAL)
        resolved = {
            arch: {key: found["claude"][arch].values[key].value for key in mp.CLAUDE.pinned}
            for arch in mp.CLAUDE.arches
        }
        self.assertEqual(mp.rewrite(REAL, mp.CLAUDE, found["claude"], resolved, "fixture"), REAL)

    def test_anchor_survives(self):
        out = rewrite_one("sha256: &pin 41aaba7b\n", "sha256", "deadbeef")
        self.assertEqual(out, "sha256: &pin deadbeef\n")

    def test_tag_survives(self):
        out = rewrite_one("sha256: !!str 41aaba7b\n", "sha256", "deadbeef")
        self.assertEqual(out, "sha256: !!str deadbeef\n")

    def test_double_quotes_survive(self):
        self.assertEqual(rewrite_one('sha256: "41aaba7b"\n', "sha256", "dead"), 'sha256: "dead"\n')

    def test_single_quotes_survive(self):
        self.assertEqual(rewrite_one("sha256: '41aaba7b'\n", "sha256", "dead"), "sha256: 'dead'\n")

    def test_trailing_comment_survives(self):
        out = rewrite_one("sha256: 41aaba7b  # keep me\n", "sha256", "dead")
        self.assertEqual(out, "sha256: dead  # keep me\n")

    def test_crlf_manifest_keeps_its_line_endings(self):
        crlf = REAL.replace("\n", "\r\n")
        found = audit(crlf)
        resolved = {arch: {"sha256": "b" * 64} for arch in mp.SEVENZIP.arches}
        out = mp.rewrite(crlf, mp.SEVENZIP, found["7zip"], resolved, "fixture")
        self.assertNotIn("\n", out.replace("\r\n", ""))
        self.assertEqual(out.count("\r\n"), crlf.count("\r\n"))

    def test_unicode_line_separator_does_not_shift_the_target(self):
        # PyYAML counts U+2028 as a line break; str.split("\n") does not, so the
        # old line-based rewrite landed a line early on any file containing one.
        text = 'note: "one two"\nsha256: 41aaba7b\n'
        self.assertEqual(rewrite_one(text, "sha256", "dead"), 'note: "one two"\nsha256: dead\n')

    def test_block_scalar_is_refused(self):
        with self.assertRaises(SystemExit):
            rewrite_one("sha256: >-\n  41aaba7b\n", "sha256", "dead")

    def test_value_needing_quotes_is_refused(self):
        with self.assertRaises(SystemExit):
            rewrite_one("sha256: abc\n", "sha256", "a: b # no")


if __name__ == "__main__":
    unittest.main(verbosity=2)
