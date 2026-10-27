"""Unit tests for how python_packaging.py locates a package's files.

Each test builds a throwaway git repository with one project layout, asks for
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

import python_packaging  # noqa: E402

HATCH = '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
SETUPTOOLS = '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
NOISE = {
    "tests/test_core.py": "",
    "docs/index.md": "",
    "scripts/release.py": "",
    "uv.lock": "",
    ".github/workflows/ci.yml": "",
}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class LayoutTest(unittest.TestCase):
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
        return python_packaging.package_paths(sha)

    def matched(self, collector):
        # ls-files, not ls-tree: ls-tree rejects :(glob) magic. Every test
        # commits all it writes, so the index is the HEAD tree.
        if not collector.paths:
            return set()
        out = git(self.repo, "ls-files", "--", *collector.paths)
        return set(out.split())

    def assert_matches(self, files, expected):
        c = self.resolve(files)
        self.assertEqual(c.problems, [])
        self.assertEqual(self.matched(c), set(expected))
        return c


class HatchTest(LayoutTest):
    def test_default_src_layout(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "my-pkg"\n',
                "src/my_pkg/__init__.py": "",
                "src/my_pkg/core.py": "",
            },
            {"src/my_pkg/__init__.py", "src/my_pkg/core.py"},
        )

    def test_default_flat_layout(self):
        self.assert_matches(
            {"pyproject.toml": HATCH + '[project]\nname = "my-pkg"\n', "my_pkg/__init__.py": ""},
            {"my_pkg/__init__.py"},
        )

    def test_default_single_module(self):
        self.assert_matches(
            {"pyproject.toml": HATCH + '[project]\nname = "my_pkg"\n', "my_pkg.py": ""},
            {"my_pkg.py"},
        )

    def test_default_namespace_package(self):
        self.assert_matches(
            {"pyproject.toml": HATCH + '[project]\nname = "my_pkg"\n', "ns/my_pkg/__init__.py": ""},
            {"ns/my_pkg/__init__.py"},
        )

    def test_explicit_packages_outside_src(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH
                + '[project]\nname = "x"\n[tool.hatch.build.targets.wheel]\npackages = ["lib/my_pkg"]\n',
                "lib/my_pkg/__init__.py": "",
                "lib/other/__init__.py": "",
            },
            {"lib/my_pkg/__init__.py"},
        )

    def test_wheel_target_overrides_build_wide_option(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n'
                '[tool.hatch.build]\npackages = ["old"]\n'
                '[tool.hatch.build.targets.wheel]\npackages = ["new"]\n',
                "old/__init__.py": "",
                "new/__init__.py": "",
            },
            {"new/__init__.py"},
        )

    def test_only_include_and_force_include(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n'
                '[tool.hatch.build.targets.wheel]\nonly-include = ["pkg"]\n'
                '[tool.hatch.build.targets.wheel.force-include]\n"data/schema.json" = "pkg/schema.json"\n',
                "pkg/__init__.py": "",
                "data/schema.json": "",
            },
            {"pkg/__init__.py", "data/schema.json"},
        )

    def test_custom_build_hook_is_tracked_but_unresolved(self):
        c = self.resolve(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n'
                '[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n'
                '[tool.hatch.build.hooks.custom]\npath = "build_hooks.py"\n',
                "pkg/__init__.py": "",
                "build_hooks.py": "",
            }
        )
        self.assertTrue(any("build_hooks.py" in p for p in c.problems))
        self.assertEqual(self.matched(c), {"pkg/__init__.py", "build_hooks.py"})

    def test_include_patterns(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n'
                '[tool.hatch.build.targets.wheel]\ninclude = ["/lib", "*.typed"]\n',
                "lib/pkg/__init__.py": "",
                "lib/pkg/sub/mod.py": "",
                "other/py.typed": "",
            },
            {"lib/pkg/__init__.py", "lib/pkg/sub/mod.py", "other/py.typed"},
        )

    def test_nothing_found_is_unresolved(self):
        c = self.resolve({"pyproject.toml": HATCH + '[project]\nname = "absent"\n', "lib/pkg/__init__.py": ""})
        self.assertTrue(c.problems)


class SetuptoolsTest(LayoutTest):
    def test_auto_discovery_src_layout(self):
        self.assert_matches(
            {"pyproject.toml": SETUPTOOLS + '[project]\nname = "x"\n', "src/pkg/__init__.py": "", "src/solo.py": ""},
            {"src/pkg/__init__.py", "src/solo.py"},
        )

    def test_auto_discovery_flat_layout_skips_reserved_names(self):
        self.assert_matches(
            {
                "pyproject.toml": SETUPTOOLS + '[project]\nname = "x"\n',
                "pkg/__init__.py": "",
                "solo.py": "",
                "noxfile.py": "",
                "conftest.py": "",
                "_private/thing.py": "",
            },
            {"pkg/__init__.py", "solo.py"},
        )

    def test_pyproject_find_with_where_and_exclude(self):
        self.assert_matches(
            {
                "pyproject.toml": SETUPTOOLS + '[project]\nname = "x"\n'
                '[tool.setuptools.packages.find]\nwhere = ["lib"]\nexclude = ["pkg_tests*"]\n',
                "lib/pkg/__init__.py": "",
                "lib/pkg_tests/__init__.py": "",
                "src/decoy/__init__.py": "",
            },
            {"lib/pkg/__init__.py"},
        )

    def test_pyproject_explicit_packages_with_package_dir(self):
        self.assert_matches(
            {
                "pyproject.toml": SETUPTOOLS + '[project]\nname = "x"\n'
                '[tool.setuptools]\npackages = ["pkg", "pkg.sub"]\npy-modules = ["solo"]\n'
                '[tool.setuptools.package-dir]\n"" = "lib"\n',
                "lib/pkg/__init__.py": "",
                "lib/pkg/sub/__init__.py": "",
                "lib/solo.py": "",
                "lib/unlisted.py": "",
            },
            {"lib/pkg/__init__.py", "lib/pkg/sub/__init__.py", "lib/solo.py"},
        )

    def test_setup_cfg_find(self):
        self.assert_matches(
            {
                "pyproject.toml": SETUPTOOLS,
                "setup.cfg": """\
                    [metadata]
                    name = x

                    [options]
                    package_dir =
                        =lib
                    packages = find:

                    [options.packages.find]
                    where = lib
                    """,
                "lib/pkg/__init__.py": "",
                "lib/notpkg/data.txt": "",
            },
            {"lib/pkg/__init__.py"},
        )

    def test_setup_py_only_with_find_packages(self):
        self.assert_matches(
            {
                "setup.py": """\
                    from setuptools import find_packages, setup

                    setup(
                        name="x",
                        package_dir={"": "lib"},
                        packages=find_packages(where="lib", exclude=["tests", "tests.*"]),
                    )
                    """,
                "lib/pkg/__init__.py": "",
                "lib/tests/__init__.py": "",
            },
            {"lib/pkg/__init__.py"},
        )

    def test_setup_py_follows_a_module_level_name(self):
        self.assert_matches(
            {
                "setup.py": 'from setuptools import setup\nPACKAGES = ["pkg"]\nsetup(name="x", packages=PACKAGES)\n',
                "pkg/__init__.py": "",
                "other/__init__.py": "",
            },
            {"pkg/__init__.py"},
        )

    def test_setup_py_py_modules_and_extension_sources(self):
        self.assert_matches(
            {
                "setup.py": """\
                    from setuptools import Extension, setup

                    setup(
                        name="x",
                        py_modules=["solo"],
                        ext_modules=[Extension("fast", sources=["csrc/fast.c"])],
                    )
                    """,
                "solo.py": "",
                "csrc/fast.c": "",
                "csrc/unused.c": "",
            },
            {"solo.py", "csrc/fast.c"},
        )

    def test_setup_py_cmdclass_is_unresolved(self):
        c = self.resolve(
            {
                "setup.py": 'from setuptools import setup\nsetup(name="x", packages=["a"], cmdclass={"build_py": B})\n',
                "a/__init__.py": "",
            }
        )
        self.assertTrue(any("cmdclass" in p for p in c.problems))

    def test_setup_py_computed_packages_is_unresolved(self):
        c = self.resolve(
            {
                "setup.py": 'from setuptools import setup\nsetup(name="x", packages=[p for p in ["a"]])\n',
                "a/__init__.py": "",
            }
        )
        self.assertTrue(any("computed" in p for p in c.problems))

    def test_setup_py_kwargs_spread_is_unresolved(self):
        c = self.resolve({"setup.py": "from setuptools import setup\nsetup(**CONFIG)\n", "a/__init__.py": ""})
        self.assertTrue(c.problems)


class OtherBackendsTest(LayoutTest):
    def test_poetry_packages_from(self):
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "poetry.core.masonry.api"\n'
                '[tool.poetry]\nname = "x"\npackages = [{ include = "pkg", from = "lib" }]\n'
                'include = [{ path = "sdist_only.txt", format = "sdist" }]\n',
                "lib/pkg/__init__.py": "",
                "sdist_only.txt": "",
            },
            {"lib/pkg/__init__.py"},
        )

    def test_poetry_build_script_is_unresolved(self):
        c = self.resolve(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "poetry.core.masonry.api"\n'
                '[project]\nname = "x"\n[tool.poetry.build]\nscript = "build.py"\n',
                "x/__init__.py": "",
                "build.py": "",
            }
        )
        self.assertTrue(any("build.py" in p for p in c.problems))
        self.assertIn("build.py", self.matched(c))

    def test_pdm_build_hook_file_is_unresolved(self):
        c = self.resolve(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "pdm.backend"\n[project]\nname = "x"\n',
                "src/x/__init__.py": "",
                "pdm_build.py": "",
            }
        )
        self.assertTrue(any("pdm_build.py" in p for p in c.problems))

    def test_poetry_default(self):
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "poetry.core.masonry.api"\n[project]\nname = "my-pkg"\n',
                "src/my_pkg/__init__.py": "",
            },
            {"src/my_pkg/__init__.py"},
        )

    def test_flit_module_name(self):
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "flit_core.buildapi"\n'
                '[project]\nname = "dist-name"\n[tool.flit.module]\nname = "importable"\n',
                "importable/__init__.py": "",
                "dist_name/__init__.py": "",
            },
            {"importable/__init__.py"},
        )

    def test_pdm_default_and_includes(self):
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "pdm.backend"\n[project]\nname = "x"\n',
                "src/pkg/__init__.py": "",
            },
            {"src/pkg/__init__.py"},
        )
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "pdm.backend"\n[project]\nname = "x"\n'
                '[tool.pdm.build]\nincludes = ["mypkg/"]\n',
                "mypkg/__init__.py": "",
            },
            {"mypkg/__init__.py"},
        )

    def test_uv_build_backend(self):
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "uv_build"\n[project]\nname = "My-Pkg"\n',
                "src/my_pkg/__init__.py": "",
            },
            {"src/my_pkg/__init__.py"},
        )
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "uv_build"\n[project]\nname = "x"\n'
                '[tool.uv.build-backend]\nmodule-root = ""\nmodule-name = "foo.bar"\n',
                "foo/bar/__init__.py": "",
            },
            {"foo/bar/__init__.py"},
        )

    def test_maturin_mixed_project(self):
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "maturin"\n[project]\nname = "x"\n'
                '[tool.maturin]\npython-source = "python"\nmodule-name = "x._core"\n',
                "Cargo.toml": "",
                "Cargo.lock": "",
                "src/lib.rs": "",
                "python/x/__init__.py": "",
            },
            {"Cargo.toml", "Cargo.lock", "src/lib.rs", "python/x/__init__.py"},
        )

    def test_scikit_build_is_unresolved_but_tracks_cmake(self):
        c = self.resolve(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "scikit_build_core.build"\n[project]\nname = "x"\n',
                "src/x/__init__.py": "",
                "CMakeLists.txt": "",
                "cpp/CMakeLists.txt": "",
            }
        )
        self.assertTrue(c.problems)
        self.assertEqual(self.matched(c), {"src/x/__init__.py", "CMakeLists.txt", "cpp/CMakeLists.txt"})

    def test_unknown_backend_is_unresolved(self):
        c = self.resolve({"pyproject.toml": '[build-system]\nbuild-backend = "exotic.api"\n[project]\nname = "x"\n'})
        self.assertTrue(c.problems)

    def test_no_build_configuration_is_unresolved(self):
        c = self.resolve({})
        self.assertTrue(c.problems)


class DeclaredFilesTest(LayoutTest):
    def test_readme_table_and_license_file(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n'
                'readme = { file = "docs/PYPI.md", content-type = "text/markdown" }\n'
                'license = { file = "legal/TERMS" }\n'
                "[tool.hatch.build.targets.wheel]\npackages = [\"x\"]\n",
                "x/__init__.py": "",
                "docs/PYPI.md": "",
                "legal/TERMS": "",
            },
            {"x/__init__.py", "docs/PYPI.md", "legal/TERMS"},
        )


class CommandTest(LayoutTest):
    def test_package_paths_covers_both_revisions(self):
        base = self.commit(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n[tool.hatch.build.targets.wheel]\npackages = ["lib/x"]\n',
                "lib/x/__init__.py": "",
            }
        )
        head = self.commit(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n[tool.hatch.build.targets.wheel]\npackages = ["src/x"]\n',
                "src/x/__init__.py": "",
            },
            remove=["lib"],
        )
        out = subprocess.run(
            [sys.executable, python_packaging.__file__, "package-paths", base, head],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(":(literal)lib/x", out.stdout.split())
        self.assertIn(":(literal)src/x", out.stdout.split())

    def test_package_paths_exit_code_when_unresolved(self):
        sha = self.commit({"setup.py": "from setuptools import setup\nsetup(**CONFIG)\n"})
        out = subprocess.run(
            [sys.executable, python_packaging.__file__, "package-paths", sha, sha],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(out.returncode, python_packaging.UNRESOLVED)
        self.assertTrue(out.stderr.strip())


if __name__ == "__main__":
    unittest.main()
