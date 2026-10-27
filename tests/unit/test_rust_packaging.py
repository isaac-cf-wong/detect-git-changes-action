"""Unit tests for how rust_packaging.py locates a crate's files.

Each test builds a throwaway git repository with one Cargo layout, asks for
its package pathspecs, and checks which tracked files they match, which is what
the action's verdict depends on.

Run with: python3 -m unittest discover -s tests/unit -p 'test_*.py'
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import rust_packaging  # noqa: E402

CRATE = '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'
NOISE = {
    "tests/cli.rs": "",
    "benches/bench.rs": "",
    "examples/demo.rs": "",
    "docs/index.md": "",
    ".github/workflows/ci.yml": "",
    "rust-toolchain.toml": "",
}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class CrateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self._cwd = os.getcwd()
        os.chdir(self.repo)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test User")

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def commit(self, files, remove=()):
        for path in remove:
            git(self.repo, "rm", "-rq", path)
        for path, text in files.items():
            target = Path(self.repo, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(textwrap.dedent(text))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "commit", "--allow-empty")
        return git(self.repo, "rev-parse", "HEAD").strip()

    def resolve(self, files):
        sha = self.commit({**NOISE, **files})
        return rust_packaging.Project(sha).locate()

    def matched(self, collector):
        # ls-files, not ls-tree: ls-tree rejects :(glob) magic.
        if not collector.paths:
            return set()
        return set(git(self.repo, "ls-files", "--", *collector.paths).split())

    def assert_matches(self, files, expected):
        c = self.resolve(files)
        self.assertEqual(c.problems, [])
        self.assertEqual(self.matched(c), set(expected))
        return c


class SingleCrateTest(CrateTest):
    def test_binary_crate_tracks_sources_manifest_readme_license_and_lock(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "Cargo.lock": "",
                "src/main.rs": "",
                "src/lib.rs": "",
                "README.md": "",
                "LICENSE": "",
            },
            {"Cargo.toml", "Cargo.lock", "src/main.rs", "src/lib.rs", "README.md", "LICENSE"},
        )

    def test_library_crate_does_not_track_the_lock_file(self):
        self.assert_matches(
            {"Cargo.toml": CRATE, "Cargo.lock": "", "src/lib.rs": "", ".cargo/config.toml": ""},
            {"Cargo.toml", "src/lib.rs"},
        )

    def test_bin_directory_and_explicit_bin_count_as_binaries(self):
        for files in (
            {"src/lib.rs": "", "src/bin/tool.rs": ""},
            {"src/lib.rs": "", "Cargo.toml": CRATE + '[[bin]]\nname = "tool"\npath = "src/lib.rs"\n'},
        ):
            with self.subTest(files=files):
                c = self.resolve({"Cargo.toml": CRATE, "Cargo.lock": "", **files})
                self.assertIn("Cargo.lock", self.matched(c))

    def test_autobins_false_ignores_src_main(self):
        c = self.resolve({"Cargo.toml": CRATE + "autobins = false\n", "Cargo.lock": "", "src/main.rs": "", "src/lib.rs": ""})
        self.assertNotIn("Cargo.lock", self.matched(c))

    def test_cargo_config_counts_for_a_binary(self):
        c = self.resolve({"Cargo.toml": CRATE, "src/main.rs": "", ".cargo/config.toml": ""})
        self.assertIn(".cargo/config.toml", self.matched(c))

    def test_readme_setting(self):
        for readme, expected in (('readme = "docs/CRATE.md"\n', {"docs/CRATE.md"}), ("readme = false\n", set())):
            with self.subTest(readme=readme):
                c = self.resolve({"Cargo.toml": CRATE + readme, "src/lib.rs": "", "README.md": "", "docs/CRATE.md": ""})
                self.assertEqual(self.matched(c) & {"README.md", "docs/CRATE.md"}, expected)

    def test_license_file(self):
        c = self.resolve({"Cargo.toml": CRATE + 'license-file = "legal/TERMS"\n', "src/lib.rs": "", "legal/TERMS": ""})
        self.assertIn("legal/TERMS", self.matched(c))

    def test_no_root_manifest_is_unresolved(self):
        c = self.resolve({"src/lib.rs": ""})
        self.assertTrue(any("no Cargo.toml" in p for p in c.problems), c.problems)

    def test_unparsable_manifest_is_unresolved(self):
        c = self.resolve({"Cargo.toml": "[package\n", "src/lib.rs": ""})
        self.assertTrue(any("does not parse" in p for p in c.problems), c.problems)


class SourceScanTest(CrateTest):
    def test_include_macros_resolve_relative_to_the_file(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": 'pub mod data;\npub const A: &str = include_str!("../assets/a.txt");\n',
                "src/data/mod.rs": 'pub const B: &[u8] = include_bytes!("../../shared/b.bin");\n',
                "assets/a.txt": "",
                "assets/unused.txt": "",
                "shared/b.bin": "",
            },
            {"Cargo.toml", "src/lib.rs", "src/data/mod.rs", "assets/a.txt", "shared/b.bin"},
        )

    def test_raw_string_and_std_prefixed_include(self):
        self.assert_matches(
            {"Cargo.toml": CRATE, "src/lib.rs": 'const A: &str = std::include_str!(r#"../a.txt"#);\n', "a.txt": ""},
            {"Cargo.toml", "src/lib.rs", "a.txt"},
        )

    def test_path_attribute_module_outside_src(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": '#[path = "../platform/unix.rs"]\nmod unix;\n',
                "platform/unix.rs": "",
                "platform/windows.rs": "",
            },
            {"Cargo.toml", "src/lib.rs", "platform/unix.rs"},
        )

    def test_path_attribute_module_is_scanned_in_turn(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": '#[path = "../platform/unix.rs"]\nmod unix;\n',
                "platform/unix.rs": 'const T: &str = include_str!("table.txt");\n',
                "platform/table.txt": "",
            },
            {"Cargo.toml", "src/lib.rs", "platform/unix.rs", "platform/table.txt"},
        )

    def test_missing_path_attribute_target_is_unresolved(self):
        c = self.resolve({"Cargo.toml": CRATE, "src/lib.rs": '#[path = "../gone.rs"]\nmod gone;\n'})
        self.assertTrue(any("#[path]" in p for p in c.problems), c.problems)

    def explicit_bin_root(self, inferred):
        up = "../" * inferred.count("/")
        c = self.resolve(
            {
                "Cargo.toml": CRATE + 'autobins = false\n[[bin]]\nname = "tool"\n',
                inferred: f'mod inline {{\n    #[path = "{up}../external.rs"]\n    mod x;\n}}\nfn main() {{}}\n',
                "external.rs": "",
            }
        )
        self.assertEqual(c.problems, [])
        self.assertIn("external.rs", self.matched(c))

    def test_explicit_bin_without_path_is_a_crate_root(self):
        self.explicit_bin_root("src/bin/tool.rs")

    def test_explicit_bin_without_path_in_a_directory_is_a_crate_root(self):
        self.explicit_bin_root("src/bin/tool/main.rs")

    def test_path_attribute_file_keeps_its_modules_beside_it(self):
        # rustc treats a file loaded through #[path] like a mod.rs.
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": '#[path = "../platform/unix.rs"]\nmod unix;\n',
                "platform/unix.rs": "pub mod helper;\n",
                "platform/helper.rs": "",
                "platform/unix/helper.rs": "",
            },
            {"Cargo.toml", "src/lib.rs", "platform/unix.rs", "platform/helper.rs"},
        )

    def test_module_file_that_cannot_be_found_is_unresolved(self):
        c = self.resolve(
            {
                "Cargo.toml": CRATE + '[lib]\npath = "lib/root.rs"\n',
                "lib/root.rs": 'mod gone;\n#[path = "kept.rs"]\nmod renamed;\n',
                "lib/kept.rs": "",
            }
        )
        self.assertEqual([p for p in c.problems if "renamed" in p or "kept" in p], [])
        self.assertTrue(any("gone" in p for p in c.problems), c.problems)

    def test_quotes_in_char_literals_and_raw_strings_do_not_derail_the_scan(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": "const Q: char = '\"'; const URL: &str = \"https://example.com\"; "
                'const A: &str = include_str!("../a.txt");\n'
                'const J: &str = r#"{"url": "http://x"}"#; const B: &str = include_str!("../b.txt");\n'
                "fn f<'a>(x: &'a str) -> &'a str { x } const C: &str = include_str!(\"../c.txt\");\n",
                "a.txt": "",
                "b.txt": "",
                "c.txt": "",
            },
            {"Cargo.toml", "src/lib.rs", "a.txt", "b.txt", "c.txt"},
        )

    def test_library_outside_src_tracks_its_modules(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE + '[lib]\npath = "lib/root.rs"\n',
                "lib/root.rs": "pub mod helper;\nmod nested;\n",
                "lib/helper.rs": "",
                "lib/nested/mod.rs": "pub mod leaf;\n",
                "lib/nested/leaf.rs": "",
                "lib/unrelated.rs": "",
            },
            {"Cargo.toml", "lib/root.rs", "lib/helper.rs", "lib/nested/mod.rs", "lib/nested/leaf.rs"},
        )

    def test_whole_crate_embedded_by_a_root_crate_is_unresolved(self):
        c = self.resolve({"Cargo.toml": CRATE, "src/lib.rs": 'static D: Dir = include_dir!("$CARGO_MANIFEST_DIR");\n'})
        self.assertTrue(any("whole repository" in p for p in c.problems), c.problems)

    def test_module_inside_an_inline_module(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE + '[lib]\npath = "lib/root.rs"\n',
                "lib/root.rs": "mod inline {\n    const S: &str = \"}\";\n    const C: char = '}';\n    pub mod child;\n}\nmod top;\n",
                "lib/inline/child.rs": "",
                "lib/child.rs": "",
                "lib/top.rs": "",
            },
            {"Cargo.toml", "lib/root.rs", "lib/inline/child.rs", "lib/top.rs"},
        )

    def test_path_attribute_inside_an_inline_module(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": 'mod outer {\n    #[path = "../../extra/x.rs"]\n    mod x;\n}\n',
                "extra/x.rs": "",
            },
            # Inside an inline module of a mod-rs file, the base is src/outer/.
            {"Cargo.toml", "src/lib.rs", "extra/x.rs"},
        )

    def test_concat_in_a_macro_becomes_a_glob(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": 'macro_rules! t { ($l:literal) => { include_str!(concat!("../lang/", $l, ".txt")) }; }\n',
                "lang/en.txt": "",
                "lang/fr.txt": "",
                "lang/README.md": "",
            },
            {"Cargo.toml", "src/lib.rs", "lang/en.txt", "lang/fr.txt"},
        )

    def test_out_dir_include_is_left_to_the_build_script(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE + "build = false\n",
                "src/lib.rs": 'include!(concat!(env!("OUT_DIR"), "/gen.rs"));\n',
            },
            {"Cargo.toml", "src/lib.rs"},
        )

    def test_cargo_manifest_dir_paths(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": 'static D: Dir = include_dir!("$CARGO_MANIFEST_DIR/templates");\n'
                'const S: &str = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/schema.json"));\n',
                "templates/page.html": "",
                "schema.json": "",
            },
            {"Cargo.toml", "src/lib.rs", "templates/page.html", "schema.json"},
        )

    def test_proc_macro_attribute_paths_are_tracked(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": '#[derive(RustEmbed)]\n#[folder = "assets/"]\nstruct Assets;\n'
                'static M: Migrator = sqlx::migrate!("./migrations");\n'
                '#[derive(Parser)]\n#[grammar = "grammar.pest"]\nstruct P;\n',
                "assets/logo.svg": "",
                "migrations/0001.sql": "",
                "src/grammar.pest": "",
            },
            {"Cargo.toml", "src/lib.rs", "assets/logo.svg", "migrations/0001.sql", "src/grammar.pest"},
        )

    def test_a_plain_word_naming_a_directory_is_not_tracked(self):
        self.assert_matches(
            {"Cargo.toml": CRATE, "src/lib.rs": 'pub const SECTION: &str = "docs";\n'},
            {"Cargo.toml", "src/lib.rs"},
        )

    def test_unreadable_include_is_unresolved(self):
        for source in (
            "const A: &str = include_str!(PATH);\n",
            'const A: &str = include_str!(concat!(env!("HOME"), "/a.txt"));\n',
            'const A: &str = include_str!("../missing.txt");\n',
            'macro_rules! t { ($l:literal) => { include_str!(concat!("../none/", $l)) }; }\n',
        ):
            with self.subTest(source=source):
                c = self.resolve({"Cargo.toml": CRATE, "src/lib.rs": source})
                self.assertTrue(any("include_str!" in p for p in c.problems), c.problems)

    def test_includes_in_comments_are_ignored(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": '// include_str!("../missing.txt")\n/* include_bytes!(X) */\n'
                'const URL: &str = "https://example.com"; const A: &str = include_str!("../a.txt");\n',
                "a.txt": "",
            },
            {"Cargo.toml", "src/lib.rs", "a.txt"},
        )

    def test_doc_attribute_include_of_the_readme(self):
        self.assert_matches(
            {"Cargo.toml": CRATE + "readme = false\n", "src/lib.rs": '#![doc = include_str!("../README.md")]\n', "README.md": ""},
            {"Cargo.toml", "src/lib.rs", "README.md"},
        )

    def test_file_in_a_submodule_tracks_the_submodule(self):
        sub = tempfile.TemporaryDirectory()
        self.addCleanup(sub.cleanup)
        git(sub.name, "init", "-q")
        git(sub.name, "config", "user.email", "t@t")
        git(sub.name, "config", "user.name", "t")
        Path(sub.name, "asset.wasm").write_text("x")
        git(sub.name, "add", "-A")
        git(sub.name, "commit", "-qm", "s")
        git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", sub.name, "vendor/artifacts")
        c = self.resolve({"Cargo.toml": CRATE, "src/lib.rs": 'const W: &[u8] = include_bytes!("../vendor/artifacts/asset.wasm");\n'})
        self.assertEqual(c.problems, [])
        self.assertIn(":(literal)vendor/artifacts", c.paths)


class BuildScriptTest(CrateTest):
    def test_declared_inputs_are_tracked(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": "",
                "build.rs": 'fn main() {\n    println!("cargo:rerun-if-changed=proto/msg.proto");\n'
                '    println!("cargo::rerun-if-changed=schemas");\n}\n',
                "proto/msg.proto": "",
                "proto/other.proto": "",
                "schemas/a.json": "",
            },
            {"Cargo.toml", "src/lib.rs", "build.rs", "proto/msg.proto", "schemas/a.json"},
        )

    def test_whole_crate_as_input(self):
        # A member crate's directory is tracked whole; the root crate's would be
        # the whole repository, which is the same as not knowing.
        c = self.resolve(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": "",
                "build.rs": 'fn main() { println!("cargo:rerun-if-changed=."); }\n',
                "schema": "",
            }
        )
        self.assertTrue(any("whole repository" in p for p in c.problems), c.problems)
        c = self.resolve(
            {
                "Cargo.toml": '[workspace]\nmembers = ["gen"]\n',
                "gen/Cargo.toml": '[package]\nname = "gen"\n',
                "gen/src/lib.rs": "",
                "gen/build.rs": 'fn main() { println!("cargo:rerun-if-changed=."); }\n',
                "gen/schema": "",
            }
        )
        self.assertEqual(c.problems, [])
        self.assertIn("gen/schema", self.matched(c))

    def test_custom_build_script_path(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE + 'build = "tools/build.rs"\n',
                "src/lib.rs": "",
                "tools/build.rs": 'fn main() { println!("cargo:rerun-if-changed=tools/build.rs"); }\n',
            },
            {"Cargo.toml", "src/lib.rs", "tools/build.rs"},
        )

    def test_environment_only_directives_declare_no_file_inputs(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": "",
                "build.rs": 'fn main() { println!("cargo:rerun-if-env-changed=VERSION"); }\n',
            },
            {"Cargo.toml", "src/lib.rs", "build.rs"},
        )

    def test_undeclared_or_computed_inputs_are_unresolved(self):
        for script, reason in (
            ("fn main() {}\n", "declares no cargo:rerun-if-changed"),
            ('fn main() { println!("cargo:rerun-if-changed={}", dir.display()); }\n', "worked out at build time"),
        ):
            with self.subTest(script=script):
                c = self.resolve({"Cargo.toml": CRATE, "src/lib.rs": "", "build.rs": script})
                self.assertTrue(any(reason in p for p in c.problems), c.problems)

    def test_build_false_ignores_build_rs(self):
        self.assert_matches({"Cargo.toml": CRATE + "build = false\n", "src/lib.rs": "", "build.rs": "fn main() {}\n"}, {"Cargo.toml", "src/lib.rs"})

    def test_build_script_modules_are_tracked(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE,
                "src/lib.rs": "",
                "build.rs": 'mod codegen;\nfn main() { println!("cargo:rerun-if-changed=build.rs"); }\n',
                "codegen.rs": "",
            },
            {"Cargo.toml", "src/lib.rs", "build.rs", "codegen.rs"},
        )


class WorkspaceTest(CrateTest):
    WORKSPACE = (
        '[workspace]\nmembers = ["crates/*", "xtask"]\nexclude = ["crates/experimental"]\n'
        '[workspace.package]\nversion = "0.1.0"\nreadme = "README.md"\n'
        '[workspace.dependencies]\nshared = { path = "libs/shared" }\n'
    )

    def test_published_members_and_what_they_build_against(self):
        self.assert_matches(
            {
                "Cargo.toml": self.WORKSPACE,
                "Cargo.lock": "",
                "README.md": "",
                "crates/app/Cargo.toml": '[package]\nname = "app"\nreadme.workspace = true\n'
                '[dependencies]\ncore = { path = "../core" }\nshared = { workspace = true }\n'
                '[dev-dependencies]\ntestutil = { path = "../../libs/testutil" }\n',
                "crates/app/src/main.rs": "",
                "crates/core/Cargo.toml": '[package]\nname = "core"\npublish = false\n',
                "crates/core/src/lib.rs": "",
                "crates/experimental/Cargo.toml": '[package]\nname = "experimental"\n',
                "crates/experimental/src/lib.rs": "",
                "libs/shared/Cargo.toml": '[package]\nname = "shared"\npublish = false\n',
                "libs/shared/src/lib.rs": "",
                "libs/testutil/Cargo.toml": '[package]\nname = "testutil"\npublish = false\n',
                "libs/testutil/src/lib.rs": "",
                "xtask/Cargo.toml": '[package]\nname = "xtask"\npublish = false\n',
                "xtask/src/main.rs": "",
            },
            {
                "Cargo.toml",
                "Cargo.lock",
                "README.md",
                "crates/app/Cargo.toml",
                "crates/app/src/main.rs",
                "crates/core/Cargo.toml",
                "crates/core/src/lib.rs",
                "libs/shared/Cargo.toml",
                "libs/shared/src/lib.rs",
            },
        )

    def test_published_binary_reached_first_as_a_workspace_dependency(self):
        c = self.resolve(
            {
                "Cargo.toml": '[workspace]\nmembers = ["app", "common"]\n'
                '[workspace.dependencies]\ncommon = { path = "common" }\n',
                "Cargo.lock": "",
                ".cargo/config.toml": "",
                "app/Cargo.toml": '[package]\nname = "app"\n[dependencies]\ncommon.workspace = true\n',
                "app/src/lib.rs": "",
                "common/Cargo.toml": '[package]\nname = "common"\n',
                "common/src/lib.rs": "",
                "common/src/main.rs": "",
            }
        )
        self.assertEqual(c.problems, [])
        self.assertTrue({"Cargo.lock", ".cargo/config.toml"} <= self.matched(c))

    def test_a_workspace_of_unpublished_crates_tracks_them_all(self):
        c = self.resolve(
            {
                "Cargo.toml": '[workspace]\nmembers = ["a", "b"]\n',
                "a/Cargo.toml": '[package]\nname = "a"\npublish = false\n',
                "a/src/lib.rs": "",
                "b/Cargo.toml": '[package]\nname = "b"\npublish = false\n',
                "b/src/lib.rs": "",
            }
        )
        self.assertEqual(c.problems, [])
        self.assertTrue({"a/src/lib.rs", "b/src/lib.rs"} <= self.matched(c))

    def test_root_package_in_a_workspace(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE + '[workspace]\nmembers = ["helper"]\n[dependencies]\nhelper = { path = "helper" }\n',
                "src/lib.rs": "",
                "helper/Cargo.toml": '[package]\nname = "helper"\n',
                "helper/src/lib.rs": "",
            },
            {"Cargo.toml", "src/lib.rs", "helper/Cargo.toml", "helper/src/lib.rs"},
        )

    def test_target_specific_and_build_dependencies(self):
        self.assert_matches(
            {
                "Cargo.toml": CRATE + '[build-dependencies]\ngen = { path = "gen" }\n'
                "[target.'cfg(unix)'.dependencies]\nsys = { path = \"sys\" }\n"
                "[target.'cfg(unix)'.dev-dependencies]\nmock = { path = \"mock\" }\n",
                "src/lib.rs": "",
                "gen/Cargo.toml": '[package]\nname = "gen"\n',
                "gen/src/lib.rs": "",
                "sys/Cargo.toml": '[package]\nname = "sys"\n',
                "sys/src/lib.rs": "",
                "mock/Cargo.toml": '[package]\nname = "mock"\n',
                "mock/src/lib.rs": "",
            },
            {"Cargo.toml", "src/lib.rs", "gen/Cargo.toml", "gen/src/lib.rs", "sys/Cargo.toml", "sys/src/lib.rs"},
        )

    def test_patch_path_entries_are_tracked(self):
        c = self.resolve(
            {
                "Cargo.toml": CRATE + '[patch.crates-io]\nserde = { path = "vendor/serde" }\n',
                "src/lib.rs": "",
                "vendor/serde/Cargo.toml": '[package]\nname = "serde"\n',
                "vendor/serde/src/lib.rs": "",
            }
        )
        self.assertIn("vendor/serde/src/lib.rs", self.matched(c))

    def test_problems(self):
        for files, reason in (
            ({"Cargo.toml": CRATE + '[dependencies]\nx = { path = "../../x" }\n', "src/lib.rs": ""}, "leaves the repository"),
            ({"Cargo.toml": CRATE + '[dependencies]\nx = { path = "x" }\n', "src/lib.rs": ""}, "has no Cargo.toml"),
            ({"Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n'}, "matches no crate"),
            ({"Cargo.toml": '[workspace]\nmembers = ["a"]\n', "a/src/lib.rs": ""}, "matches no crate"),
        ):
            with self.subTest(reason=reason):
                c = self.resolve(files)
                self.assertTrue(any(reason in p for p in c.problems), c.problems)


class MaterialTest(CrateTest):
    def material(self, before, after, path="Cargo.toml"):
        base = self.commit({path: before})
        head = self.commit({path: after})
        return rust_packaging.material(base, head, path)

    def test_development_only_tables_are_not_material(self):
        base = CRATE + '[dependencies]\nclap = "4"\n'
        for extra in (
            '[dev-dependencies]\nassert_cmd = "2"\n',
            "[lints.clippy]\npedantic = \"warn\"\n",
            "[package.metadata.docs.rs]\nall-features = true\n",
            "[[bench]]\nname = \"b\"\nharness = false\n",
            "[target.'cfg(unix)'.dev-dependencies]\nmock = \"1\"\n",
            "[badges]\nmaintenance = { status = \"actively-developed\" }\n",
        ):
            with self.subTest(extra=extra):
                self.assertFalse(self.material(base, base + extra))

    def test_what_reaches_the_crate_is_material(self):
        base = CRATE + '[dependencies]\nclap = "4"\n'
        for after in (
            base.replace('clap = "4"', 'clap = "5"'),
            base.replace('version = "0.1.0"', 'version = "0.2.0"'),
            base + "[features]\nfast = []\n",
            base + "[profile.release]\nlto = true\n",
            base + "[target.'cfg(unix)'.dependencies]\nlibc = \"0.2\"\n",
        ):
            with self.subTest(after=after):
                self.assertTrue(self.material(base, after))

    def test_workspace_tables(self):
        ws = '[workspace]\nmembers = ["a"]\n'
        self.assertFalse(self.material(ws, ws + "[workspace.lints.rust]\nunsafe_code = \"forbid\"\n"))
        self.assertTrue(self.material(ws, ws + '[workspace.dependencies]\nserde = "1"\n'))
        self.assertTrue(self.material(ws, ws + '[workspace.package]\nversion = "1.0.0"\n'))

    def test_member_manifest(self):
        self.assertFalse(self.material(CRATE, CRATE + '[dev-dependencies]\nx = "1"\n', "crates/a/Cargo.toml"))

    def test_command_exit_codes(self):
        base = self.commit({"Cargo.toml": CRATE})
        head = self.commit({"Cargo.toml": "[package\n"})
        script = Path(rust_packaging.__file__)
        out = subprocess.run([sys.executable, str(script), "material", base, head, "Cargo.toml"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        out = subprocess.run([sys.executable, "-I", str(script), "package-paths", base, base], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
