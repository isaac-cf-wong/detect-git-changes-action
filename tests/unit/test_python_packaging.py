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

    def commit(self, files, remove=(), links=None):
        for path in remove:
            git(self.repo, "rm", "-rq", path)
        for path, text in files.items():
            target = Path(self.repo, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(textwrap.dedent(text))
        for path, referent in (links or {}).items():
            link = Path(self.repo, path)
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(referent, link)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "commit", "--allow-empty")
        return git(self.repo, "rev-parse", "HEAD").strip()

    def resolve(self, files, links=None):
        sha = self.commit({**NOISE, **files}, links=links)
        return python_packaging.package_paths(sha)

    def matched(self, collector):
        # ls-files, not ls-tree: ls-tree rejects :(glob) magic. Every test
        # commits all it writes, so the index is the HEAD tree.
        if not collector.paths:
            return set()
        out = git(self.repo, "ls-files", "--", *collector.paths)
        return set(out.split())

    def assert_matches(self, files, expected, links=None):
        c = self.resolve(files, links=links)
        self.assertEqual(c.problems, [])
        self.assertEqual(self.matched(c), set(expected))
        return c


class HatchTest(LayoutTest):
    def test_hatch_toml_configures_the_build(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "demo"\n',
                "hatch.toml": '[build.targets.wheel]\npackages = ["lib/actual"]\n',
                "demo/__init__.py": "",
                "lib/actual/__init__.py": "",
            },
            {"lib/actual/__init__.py"},
        )

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

    def test_wheel_shared_data_scripts_and_extra_metadata(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n'
                '[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n'
                '[tool.hatch.build.targets.wheel.shared-data]\n"share/icons" = "share/x/icons"\n'
                '[tool.hatch.build.targets.wheel.shared-scripts]\n"bin/x-run" = "x-run"\n'
                '[tool.hatch.build.targets.wheel.extra-metadata]\n"legal/NOTES" = "NOTES"\n',
                "pkg/__init__.py": "",
                "share/icons/x.svg": "",
                "bin/x-run": "",
                "legal/NOTES": "",
            },
            {"pkg/__init__.py", "share/icons/x.svg", "bin/x-run", "legal/NOTES"},
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

    def test_version_path_outside_package(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\ndynamic = ["version"]\n'
                '[tool.hatch.version]\npath = "VERSION.py"\n'
                '[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n',
                "pkg/__init__.py": "",
                "VERSION.py": "",
            },
            {"pkg/__init__.py", "VERSION.py"},
        )

    def test_code_version_source_search_paths(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\ndynamic = ["version"]\n'
                '[tool.hatch.version]\nsource = "code"\npath = "version_source.py"\n'
                'search-paths = ["version_helpers"]\n'
                '[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n',
                "pkg/__init__.py": "",
                "version_source.py": "",
                "version_helpers/version.py": "",
            },
            {"pkg/__init__.py", "version_source.py", "version_helpers/version.py"},
        )

    def test_git_derived_version_sources_are_resolved(self):
        for source in ("vcs", "uv-dynamic-versioning", "versioningit"):
            with self.subTest(source=source):
                self.assert_matches(
                    {
                        "pyproject.toml": HATCH + '[project]\nname = "x"\ndynamic = ["version"]\n'
                        f'[tool.hatch.version]\nsource = "{source}"\n'
                        '[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n',
                        "pkg/__init__.py": "",
                    },
                    {"pkg/__init__.py"},
                )

    def test_nodejs_version_source_tracks_package_json(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\ndynamic = ["version"]\n'
                '[tool.hatch.version]\nsource = "nodejs"\n'
                '[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n',
                "pkg/__init__.py": "",
                "package.json": "{}",
            },
            {"pkg/__init__.py", "package.json"},
        )

    def test_unknown_version_source_is_unresolved(self):
        c = self.resolve(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\ndynamic = ["version"]\n'
                '[tool.hatch.version]\nsource = "something-new"\n'
                '[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n',
                "pkg/__init__.py": "",
            }
        )
        self.assertTrue(any("version source" in p for p in c.problems))

    def test_metadata_hook_text_is_not_taken_for_a_pattern(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\ndynamic = ["readme"]\n'
                '[tool.hatch.metadata.hooks.fancy-pypi-readme]\ncontent-type = "text/markdown"\n'
                '[[tool.hatch.metadata.hooks.fancy-pypi-readme.fragments]]\npath = "README.md"\n'
                '[[tool.hatch.metadata.hooks.fancy-pypi-readme.fragments]]\ntext = "\\n* see [docs]\\n*\\n"\n'
                '[[tool.hatch.metadata.hooks.fancy-pypi-readme.substitutions]]\npattern = ".*"\nreplacement = "x"\n',
                "x/__init__.py": "",
                "README.md": "",
            },
            {"x/__init__.py", "README.md"},
        )

    def test_requirements_txt_metadata_hook(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\ndynamic = ["dependencies"]\n'
                '[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n'
                '[tool.hatch.metadata.hooks.requirements_txt]\nfiles = ["requirements.txt"]\n'
                '[tool.hatch.metadata.hooks.requirements_txt.optional-dependencies]\ncli = ["requirements-cli.txt"]\n',
                "pkg/__init__.py": "",
                "requirements.txt": "",
                "requirements-cli.txt": "",
                "requirements-dev.txt": "",
            },
            {"pkg/__init__.py", "requirements.txt", "requirements-cli.txt"},
        )

    def test_custom_or_unknown_metadata_hook_is_unresolved(self):
        for hook in ("custom", "something-new"):
            with self.subTest(hook=hook):
                c = self.resolve(
                    {
                        "pyproject.toml": HATCH + '[project]\nname = "x"\n'
                        '[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n'
                        f'[tool.hatch.metadata.hooks.{hook}]\n',
                        "pkg/__init__.py": "",
                    }
                )
                self.assertTrue(any("metadata hook" in p for p in c.problems))

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

    def test_setup_py_find_packages_with_a_list_where(self):
        for call in ('find_packages(where=["src"])', 'find_packages(["src"])', 'find_namespace_packages(where=["src"])'):
            with self.subTest(call=call):
                self.assert_matches(
                    {
                        "setup.py": "from setuptools import find_packages, find_namespace_packages, setup\n"
                        f'setup(name="x", package_dir={{"": "src"}}, packages={call})\n',
                        "src/pkg/__init__.py": "",
                    },
                    {"src/pkg/__init__.py"},
                )

    def test_find_that_matches_nothing_is_unresolved(self):
        for files in (
            {"setup.py": 'from setuptools import find_packages, setup\nsetup(name="x", packages=find_packages(where="nosuch"))\n'},
            {
                "pyproject.toml": SETUPTOOLS + '[project]\nname = "x"\n[tool.setuptools.packages.find]\nwhere = ["nosuch"]\n',
            },
        ):
            with self.subTest(files=files):
                c = self.resolve({**files, "lib/pkg/__init__.py": ""})
                self.assertTrue(any("matched no package" in p for p in c.problems), c.problems)

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

    def test_pyproject_data_files(self):
        self.assert_matches(
            {
                "pyproject.toml": SETUPTOOLS + '[project]\nname = "x"\n'
                '[tool.setuptools]\npackages = ["pkg"]\n'
                '[tool.setuptools.data-files]\n"share/x" = ["assets/*.dat", "assets/README"]\n',
                "pkg/__init__.py": "",
                "assets/a.dat": "",
                "assets/README": "",
                "assets/unused.txt": "",
            },
            {"pkg/__init__.py", "assets/a.dat", "assets/README"},
        )

    def test_setup_cfg_data_files(self):
        self.assert_matches(
            {
                "pyproject.toml": SETUPTOOLS,
                "setup.cfg": """\
                    [metadata]
                    name = x

                    [options]
                    packages = pkg

                    [options.data_files]
                    share/x =
                        assets/a.dat
                        assets/b.dat
                    """,
                "pkg/__init__.py": "",
                "assets/a.dat": "",
                "assets/b.dat": "",
                "assets/unused.dat": "",
            },
            {"pkg/__init__.py", "assets/a.dat", "assets/b.dat"},
        )

    def test_setup_py_data_files(self):
        self.assert_matches(
            {
                "setup.py": 'from setuptools import setup\n'
                'setup(name="x", packages=["pkg"], data_files=[("share/x", ["assets/a.dat"]), ("etc", ["conf/x.ini"])])\n',
                "pkg/__init__.py": "",
                "assets/a.dat": "",
                "conf/x.ini": "",
                "assets/unused.dat": "",
            },
            {"pkg/__init__.py", "assets/a.dat", "conf/x.ini"},
        )

    def test_setup_py_computed_data_files_is_unresolved(self):
        c = self.resolve(
            {
                "setup.py": 'from setuptools import setup\nimport glob\n'
                'setup(name="x", packages=["pkg"], data_files=[("share", glob.glob("assets/*"))])\n',
                "pkg/__init__.py": "",
            }
        )
        self.assertTrue(any("data_files" in p for p in c.problems))

    def test_pyproject_dynamic_file_and_attr(self):
        self.assert_matches(
            {
                "pyproject.toml": SETUPTOOLS + '[project]\nname = "x"\n'
                'dynamic = ["version", "dependencies", "optional-dependencies", "readme"]\n'
                '[tool.setuptools]\npackages = ["pkg"]\n'
                '[tool.setuptools.dynamic]\nversion = { attr = "_meta.__version__" }\n'
                'dependencies = { file = ["requirements.txt"] }\n'
                'readme = { file = ["README.rst", "CHANGES.rst"], content-type = "text/x-rst" }\n'
                '[tool.setuptools.dynamic.optional-dependencies]\ncli = { file = "requirements-cli.txt" }\n',
                "pkg/__init__.py": "",
                "_meta.py": "",
                "requirements.txt": "",
                "requirements-cli.txt": "",
                "README.rst": "",
                "CHANGES.rst": "",
                "requirements-dev.txt": "",
            },
            {
                "pkg/__init__.py",
                "_meta.py",
                "requirements.txt",
                "requirements-cli.txt",
                "README.rst",
                "CHANGES.rst",
            },
        )

    def test_setup_cfg_file_and_attr_directives(self):
        self.assert_matches(
            {
                "pyproject.toml": SETUPTOOLS,
                "setup.cfg": """\
                    [metadata]
                    name = x
                    version = attr: _meta.VERSION
                    long_description = file: README.rst, CHANGES.rst

                    [options]
                    packages = pkg
                    install_requires = file: requirements.txt

                    [options.extras_require]
                    cli = file: requirements-cli.txt
                    """,
                "pkg/__init__.py": "",
                "_meta.py": "",
                "README.rst": "",
                "CHANGES.rst": "",
                "requirements.txt": "",
                "requirements-cli.txt": "",
                "requirements-dev.txt": "",
            },
            {
                "pkg/__init__.py",
                "_meta.py",
                "README.rst",
                "CHANGES.rst",
                "requirements.txt",
                "requirements-cli.txt",
            },
        )

    def test_setup_py_files_it_names(self):
        self.assert_matches(
            {
                "setup.py": """\
                    import os
                    from pathlib import Path
                    from setuptools import setup

                    here = os.path.dirname(__file__)
                    with open(os.path.join(here, "README.md"), encoding="utf-8") as f:
                        long_description = f.read()
                    version = (Path(__file__).parent / "VERSION").read_text().strip()
                    setup(name="x", packages=["pkg"])
                    """,
                "pkg/__init__.py": "",
                "README.md": "",
                "VERSION": "",
                "NOTES.md": "",
            },
            {"pkg/__init__.py", "README.md", "VERSION"},
        )

    def test_setup_py_composed_paths(self):
        self.assert_matches(
            {
                "setup.py": """\
                    import os
                    from pathlib import Path
                    from setuptools import setup

                    here = os.path.abspath(os.path.dirname(__file__))
                    version = (Path(__file__).parent / "metadata" / "VERSION").read_text().strip()
                    with open(os.path.join(here, "docs", "LONG.md")) as f:
                        long_description = f.read()
                    setup(name="x", version=version, packages=["pkg"], long_description=long_description)
                    """,
                "pkg/__init__.py": "",
                "metadata/VERSION": "",
                "docs/LONG.md": "",
                "docs/other.md": "",
            },
            {"pkg/__init__.py", "metadata/VERSION", "docs/LONG.md"},
        )

    def test_setup_py_unresolvable_file_read_is_unresolved(self):
        c = self.resolve(
            {
                "setup.py": """\
                    import sys
                    from setuptools import setup

                    with open(sys.argv[-1]) as f:
                        notes = f.read()
                    setup(name="x", packages=["pkg"])
                    """,
                "pkg/__init__.py": "",
            }
        )
        self.assertTrue(any("reads a file" in p for p in c.problems))

    def test_setup_py_imports_a_root_module(self):
        self.assert_matches(
            {
                "setup.py": 'from setuptools import setup\nfrom _build_meta import VERSION\nimport helpers.ver\n'
                'setup(name="x", version=VERSION, packages=["pkg"])\n',
                "pkg/__init__.py": "",
                "_build_meta.py": "",
                "helpers/ver.py": "",
                "unrelated.py": "",
            },
            {"pkg/__init__.py", "_build_meta.py", "helpers/ver.py"},
        )

    def test_file_relative_path_is_not_masked_by_a_root_file(self):
        self.assert_matches(
            {
                "setup.py": 'from setuptools import setup\nfrom scripts.version import get_version\n'
                'setup(name="x", version=get_version(), packages=["pkg"])\n',
                "pkg/__init__.py": "",
                "scripts/__init__.py": "",
                "scripts/version.py": "from pathlib import Path\n\n"
                "def get_version():\n    return (Path(__file__).parent / 'VERSION').read_text()\n",
                "scripts/VERSION": "",
                "VERSION": "",
            },
            # The root VERSION is tracked too, since the helper names "VERSION": over-tracking,
            # not masking. What matters is that scripts/VERSION, the file it reads, is there.
            {"pkg/__init__.py", "scripts/__init__.py", "scripts/version.py", "scripts/VERSION", "VERSION"},
        )

    def test_parent_chain_from_a_nested_file(self):
        self.assert_matches(
            {
                "setup.py": "from setuptools import setup\nfrom tools.build.meta import VERSION\n"
                'setup(name="x", version=VERSION, packages=["pkg"])\n',
                "pkg/__init__.py": "",
                "tools/build/meta.py": "from pathlib import Path\n"
                "VERSION = (Path(__file__).parent.parent / 'VERSION').read_text()\n",
                "tools/VERSION": "",
                "tools/build/VERSION": "",
            },
            {"pkg/__init__.py", "tools/build/meta.py", "tools/VERSION"},
        )

    def test_name_bound_in_another_scope_is_not_trusted(self):
        c = self.resolve(
            {
                "setup.py": """\
                    from pathlib import Path
                    from setuptools import setup

                    VERSION_PATH = "decoy.txt"

                    def get_version():
                        VERSION_PATH = Path("metadata") / "VERSION"
                        return VERSION_PATH.read_text()

                    setup(name="x", version=get_version(), packages=["pkg"])
                    """,
                "pkg/__init__.py": "",
                "decoy.txt": "",
                "metadata/VERSION": "",
            }
        )
        # VERSION_PATH is bound twice, so the read is not guessed: it is reported, and the
        # composed path is still tracked.
        self.assertTrue(any("reads a file" in p for p in c.problems))
        self.assertIn("metadata/VERSION", self.matched(c))

    def test_dynamic_import_with_a_literal_name(self):
        self.assert_matches(
            {
                "setup.py": "import importlib\nfrom setuptools import setup\n"
                'setup(name="x", version=importlib.import_module("version_data").VERSION, packages=["pkg"])\n',
                "pkg/__init__.py": "",
                "version_data.py": "",
            },
            {"pkg/__init__.py", "version_data.py"},
        )

    def test_dynamic_import_with_a_computed_name_is_unresolved(self):
        c = self.resolve(
            {
                "setup.py": "import importlib, os\nfrom setuptools import setup\n"
                'mod = importlib.import_module(os.environ["META"])\nsetup(name="x", packages=["pkg"])\n',
                "pkg/__init__.py": "",
            }
        )
        self.assertTrue(any("imports a module" in p for p in c.problems))

    def test_sys_path_insertion_is_followed_by_imports(self):
        self.assert_matches(
            {
                "setup.py": "import os, sys\nfrom setuptools import setup\n"
                'sys.path.insert(0, os.path.join(os.path.dirname(__file__), "build_tools"))\n'
                "from versioning import VERSION\n"
                'setup(name="x", version=VERSION, packages=["pkg"])\n',
                "pkg/__init__.py": "",
                "build_tools/versioning.py": "",
            },
            {"pkg/__init__.py", "build_tools/versioning.py"},
        )

    def test_sys_path_insertion_through_a_module_alias(self):
        for snippet in (
            'import sys as system\nsystem.path.insert(0, "build_meta")\n',
            'import site as s\ns.addsitedir("build_meta")\n',
        ):
            with self.subTest(snippet=snippet):
                self.assert_matches(
                    {
                        "setup.py": "from setuptools import setup\n" + snippet
                        + 'from version import VERSION\nsetup(name="x", version=VERSION, packages=["pkg"])\n',
                        "pkg/__init__.py": "",
                        "build_meta/version.py": "",
                    },
                    {"pkg/__init__.py", "build_meta/version.py"},
                )

    def test_sys_path_insertion_of_a_str_wrapped_path(self):
        self.assert_matches(
            {
                "setup.py": "import sys\nfrom pathlib import Path\nfrom setuptools import setup\n"
                'package_dir = Path(__file__).parent / "mlpkg"\nsys.path.append(str(package_dir))\n'
                "from _version import __version__\n"
                'setup(name="x", version=__version__, packages=["mlpkg"])\n',
                "mlpkg/__init__.py": "",
                "mlpkg/_version.py": "",
            },
            {"mlpkg/__init__.py", "mlpkg/_version.py"},
        )

    def test_sys_path_insertion_of_the_project_root(self):
        self.assert_matches(
            {
                "setup.py": "import os, sys\nfrom setuptools import setup\n"
                "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
                "from version_data import VERSION\n"
                'setup(name="x", version=VERSION, packages=["pkg"])\n',
                "pkg/__init__.py": "",
                "version_data.py": "",
            },
            {"pkg/__init__.py", "version_data.py"},
        )

    def test_open_with_keyword_path(self):
        self.assert_matches(
            {
                "setup.py": "from pathlib import Path\nfrom setuptools import setup\n"
                'version = open(file=Path("metadata") / "VERSION").read()\n'
                'setup(name="x", version=version, packages=["pkg"])\n',
                "pkg/__init__.py": "",
                "metadata/VERSION": "",
            },
            {"pkg/__init__.py", "metadata/VERSION"},
        )

    def test_write_only_open_is_not_a_read(self):
        self.assert_matches(
            {
                "setup.py": "from setuptools import setup\n"
                'with open("generated.py", "w") as f:\n    f.write("X = 1")\n'
                'with open(file="log.txt", mode="a") as f:\n    f.write("built")\n'
                'setup(name="x", packages=["pkg"])\n',
                "pkg/__init__.py": "",
            },
            {"pkg/__init__.py"},
        )

    def test_reader_helper_resolved_through_its_call_sites(self):
        self.assert_matches(
            {
                "setup.py": """\
                    import os
                    from setuptools import setup

                    here = os.path.abspath(os.path.dirname(__file__))

                    def read(*parts):
                        with open(os.path.join(here, *parts), encoding="utf-8") as f:
                            return f.read()

                    setup(
                        name="x",
                        packages=["pkg"],
                        long_description=read("docs", "LONG.md"),
                        version=read("VERSION").strip(),
                    )
                    """,
                "pkg/__init__.py": "",
                "docs/LONG.md": "",
                "VERSION": "",
                "docs/other.md": "",
            },
            {"pkg/__init__.py", "docs/LONG.md", "VERSION"},
        )

    def test_reader_helper_with_a_computed_call_site_is_unresolved(self):
        c = self.resolve(
            {
                "setup.py": """\
                    import os, sys
                    from setuptools import setup

                    def read(name):
                        with open(name) as f:
                            return f.read()

                    notes = read(sys.argv[-1])
                    setup(name="x", packages=["pkg"])
                    """,
                "pkg/__init__.py": "",
            }
        )
        self.assertTrue(any("reads a file" in p for p in c.problems))

    def test_relative_import_module_uses_its_package_argument(self):
        self.assert_matches(
            {
                "setup.py": "import importlib\nfrom setuptools import setup\n"
                'v = importlib.import_module(".version", package="scripts").VERSION\n'
                'setup(name="x", version=v, packages=["pkg"])\n',
                "pkg/__init__.py": "",
                "scripts/__init__.py": "",
                "scripts/version.py": "",
                "version.py": "",
            },
            {"pkg/__init__.py", "scripts/__init__.py", "scripts/version.py"},
        )

    def test_relative_import_module_without_a_package_is_unresolved(self):
        c = self.resolve(
            {
                "setup.py": "import importlib\nfrom setuptools import setup\n"
                'v = importlib.import_module(".version")\nsetup(name="x", packages=["pkg"])\n',
                "pkg/__init__.py": "",
                "version.py": "",
            }
        )
        self.assertTrue(any("imports a module" in p for p in c.problems))

    def test_reader_referenced_other_than_by_a_call_is_unresolved(self):
        c = self.resolve(
            {
                "setup.py": """\
                    import os
                    from pathlib import Path
                    from setuptools import setup

                    def read(path):
                        return Path(path).read_text()

                    reader = read
                    version = reader(os.environ["VERSION_FILE"])
                    setup(name="x", version=version, packages=["pkg"])
                    """,
                "pkg/__init__.py": "",
                "metadata/VERSION": "",
            }
        )
        self.assertTrue(any("reads a file" in p for p in c.problems))

    def test_reader_with_a_direct_call_and_an_alias_is_unresolved(self):
        c = self.resolve(
            {
                "setup.py": """\
                    import os
                    from pathlib import Path
                    from setuptools import setup

                    def read(path):
                        return Path(path).read_text()

                    notes = read("NOTES")
                    reader = read
                    version = reader(os.environ["VERSION_FILE"])
                    setup(name="x", version=version, packages=["pkg"])
                    """,
                "pkg/__init__.py": "",
                "NOTES": "",
            }
        )
        # The direct call resolves, but the alias may be called with anything.
        self.assertTrue(any("reads a file" in p for p in c.problems))

    def test_reader_called_only_from_another_file_is_unresolved(self):
        c = self.resolve(
            {
                "setup.py": 'from setuptools import setup\nfrom helpers import read\n'
                'setup(name="x", version=read("VERSION"), packages=["pkg"])\n',
                "helpers.py": "def read(path):\n    with open(path) as f:\n        return f.read()\n",
                "pkg/__init__.py": "",
                "VERSION": "",
            }
        )
        self.assertTrue(any("helpers.py reads a file" in p for p in c.problems))

    def test_function_read_not_depending_on_parameters_is_resolved(self):
        self.assert_matches(
            {
                "setup.py": "from pathlib import Path\nfrom setuptools import setup\n\n"
                "def version(unused=None):\n    return (Path(__file__).parent / 'VERSION').read_text()\n\n"
                'setup(name="x", version=version(), packages=["pkg"])\n',
                "pkg/__init__.py": "",
                "VERSION": "",
            },
            {"pkg/__init__.py", "VERSION"},
        )

    def test_sys_path_used_other_than_by_insert_or_append_is_unresolved(self):
        for snippet in (
            'search = sys.path\nsearch.insert(0, "build_meta")\n',
            'sys.path[:0] = ["build_meta"]\n',
            'sys.path += ["build_meta"]\n',
            'from sys import path\npath.insert(0, "build_meta")\n',
            'import sys as system\nsystem.path[:0] = ["build_meta"]\n',
            'module = sys\nmodule.path.insert(0, "build_meta")\n',
            'getattr(sys, "path").insert(0, "build_meta")\n',
            'from sys import *\npath.insert(0, "build_meta")\n',
            'from site import addsitedir\naddsitedir("build_meta")\n',
        ):
            with self.subTest(snippet=snippet):
                c = self.resolve(
                    {
                        "setup.py": "import sys\nfrom setuptools import setup\n" + snippet
                        + 'from version import VERSION\nsetup(name="x", version=VERSION, packages=["pkg"])\n',
                        "pkg/__init__.py": "",
                        "build_meta/version.py": "",
                    }
                )
                self.assertTrue(any("sys.path" in p or "site" in p for p in c.problems), c.problems)

    def test_read_through_a_symlink_tracks_its_referent(self):
        self.assert_matches(
            {
                "setup.py": 'from pathlib import Path\nfrom setuptools import setup\n'
                'setup(name="x", version=Path("VERSION").read_text(), packages=["pkg"])\n',
                "pkg/__init__.py": "",
                "metadata/VERSION": "",
            },
            {"pkg/__init__.py", "VERSION", "metadata/VERSION"},
            links={"VERSION": "metadata/VERSION"},
        )

    def test_reader_helper_in_an_imported_module_is_unresolved(self):
        c = self.resolve(
            {
                "setup.py": "import os\nfrom setuptools import setup\nfrom helpers import read\n"
                'setup(name="x", version=read(os.environ["VERSION_FILE"]), packages=["pkg"])\n',
                "helpers.py": 'def read(path):\n    return open(path).read()\n\nNOTES = read("NOTES")\n',
                "NOTES": "",
                "pkg/__init__.py": "",
            }
        )
        self.assertTrue(any("helpers.py" in p for p in c.problems), c.problems)

    def test_reader_helper_in_a_module_run_by_path_is_unresolved(self):
        c = self.resolve(
            {
                "setup.py": "import os, runpy\nfrom setuptools import setup\n"
                'helpers = runpy.run_path("helpers.py")\n'
                'setup(name="x", version=helpers["read"](os.environ["VERSION_FILE"]), packages=["pkg"])\n',
                "helpers.py": 'def read(path):\n    return open(path).read()\n\nNOTES = read("NOTES")\n',
                "NOTES": "",
                "pkg/__init__.py": "",
            }
        )
        self.assertTrue(any("helpers.py" in p for p in c.problems), c.problems)

    def test_parse_warnings_do_not_reach_stderr(self):
        sha = self.commit(
            {
                "setup.py": 'from setuptools import setup\nPATTERN = "\\d+"\nsetup(name="x", packages=["pkg"])\n',
                "pkg/__init__.py": "",
            }
        )
        script = Path(python_packaging.__file__)
        out = subprocess.run(
            [sys.executable, "-W", "default", str(script), "package-paths", sha, sha],
            capture_output=True,
            text=True,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr, "")

    def test_os_open_with_write_only_flags_is_not_a_read(self):
        self.assert_matches(
            {
                "setup.py": "import os\nfrom setuptools import setup\n"
                'fd = os.open("generated.py", os.O_WRONLY | os.O_CREAT, 0o644)\n'
                'setup(name="x", packages=["pkg"])\n',
                "pkg/__init__.py": "",
            },
            {"pkg/__init__.py"},
        )

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


class SymlinkTest(LayoutTest):
    def test_symlink_in_the_package_tracks_its_referent(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n',
                "pkg/__init__.py": "",
                "shared/schema.json": "",
            },
            {"pkg/__init__.py", "pkg/schema.json", "shared/schema.json"},
            links={"pkg/schema.json": "../shared/schema.json"},
        )

    def test_symlink_out_of_the_repository_is_unresolved(self):
        c = self.resolve(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n',
                "pkg/__init__.py": "",
            },
            links={"pkg/data.json": "../../outside/data.json"},
        )
        self.assertTrue(any("symlink" in p for p in c.problems))


    def test_symlink_matched_by_a_glob_tracks_its_referent(self):
        self.assert_matches(
            {
                "pyproject.toml": SETUPTOOLS + '[project]\nname = "x"\ndynamic = ["dependencies"]\n'
                '[tool.setuptools]\npackages = ["pkg"]\n'
                '[tool.setuptools.dynamic]\ndependencies = { file = ["requirements/*.txt"] }\n',
                "pkg/__init__.py": "",
                "shared/requirements.txt": "",
            },
            {"pkg/__init__.py", "requirements/input.txt", "shared/requirements.txt"},
            links={"requirements/input.txt": "../shared/requirements.txt"},
        )

    def test_symlink_matched_by_a_leading_double_star_tracks_its_referent(self):
        # hatchling's include = ["*.json"] means **/*.json, which matches at the root too.
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n'
                '[tool.hatch.build.targets.wheel]\ninclude = ["x", "*.json"]\n',
                "x/__init__.py": "",
                "shared/schema.src": "",
            },
            {"x/__init__.py", "schema.json", "shared/schema.src"},
            links={"schema.json": "shared/schema.src"},
        )

    def test_symlinked_directory_above_a_glob_tracks_its_referent(self):
        self.assert_matches(
            {
                "pyproject.toml": SETUPTOOLS + '[project]\nname = "x"\ndynamic = ["dependencies"]\n'
                '[tool.setuptools]\npackages = ["pkg"]\n'
                '[tool.setuptools.dynamic]\ndependencies = { file = ["requirements/*.txt"] }\n',
                "pkg/__init__.py": "",
                "shared/base.txt": "",
                "shared/notes.md": "",
            },
            {"pkg/__init__.py", "requirements", "shared/base.txt"},
            links={"requirements": "shared"},
        )

    def test_symlinked_directory_above_a_path_tracks_its_referent(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n[tool.hatch.build.targets.wheel]\npackages = ["src/pkg"]\n',
                "lib/pkg/__init__.py": "",
                "lib/other.py": "",
            },
            {"src", "lib/pkg/__init__.py"},
            links={"src": "lib"},
        )

    def test_long_symlink_chain_reaches_its_end(self):
        links = {f"link{i}": f"link{i + 1}" for i in range(12)}
        links["link12"] = "metadata/VERSION"
        self.assert_matches(
            {
                "setup.py": 'from pathlib import Path\nfrom setuptools import setup\n'
                'setup(name="x", version=Path("link0").read_text(), packages=["pkg"])\n',
                "pkg/__init__.py": "",
                "metadata/VERSION": "",
            },
            {"pkg/__init__.py", "metadata/VERSION", *links},
            links=links,
        )

    def test_symlink_loop_is_unresolved(self):
        c = self.resolve(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n',
                "pkg/__init__.py": "",
            },
            links={"pkg/data": "../pkg/data/nested"},
        )
        self.assertTrue(any("symlink" in p for p in c.problems), c.problems)

    def test_cycle_of_symlinks_is_unresolved(self):
        c = self.resolve(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n',
                "pkg/__init__.py": "",
            },
            links={"pkg/a": "b", "pkg/b": "a"},
        )
        self.assertTrue(any("symlink pkg/a loops" in p or "symlink pkg/b loops" in p for p in c.problems), c.problems)


    def test_symlink_recorded_on_the_way_to_a_narrower_path_is_still_followed(self):
        sha = self.commit({"lib/pkg/__init__.py": "", "lib/other.py": ""}, links={"src": "lib"})
        c = python_packaging.Collector(python_packaging.Tree(sha), sha)
        c.add("src/pkg")
        c.add("src")
        self.assertEqual(c.problems, [])
        self.assertEqual(self.matched(c), {"src", "lib/pkg/__init__.py", "lib/other.py"})

    def test_symlink_target_with_a_newline_keeps_later_targets_paired(self):
        c = self.resolve(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n',
                "pkg/__init__.py": "",
                "shared/data.json": "",
            },
            links={"pkg/a_odd": "x\ny", "pkg/b_data.json": "../shared/data.json"},
        )
        self.assertIn("shared/data.json", self.matched(c))
        self.assertTrue(all("\n" not in spec for spec in c.paths), c.paths)


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

    def test_poetry_readme(self):
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "poetry.core.masonry.api"\n'
                '[tool.poetry]\nname = "x"\nreadme = ["README.md", "docs/USAGE.md"]\n',
                "x/__init__.py": "",
                "README.md": "",
                "docs/USAGE.md": "",
            },
            {"x/__init__.py", "README.md", "docs/USAGE.md"},
        )

    def test_pdm_version_file(self):
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "pdm.backend"\n[project]\nname = "x"\n'
                '[tool.pdm.version]\nsource = "file"\npath = "meta/version.py"\n',
                "src/x/__init__.py": "",
                "meta/version.py": "",
            },
            {"src/x/__init__.py", "meta/version.py"},
        )

    def test_pdm_call_version_getter_is_scanned(self):
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "pdm.backend"\n[project]\nname = "x"\n'
                'dynamic = ["version"]\n[tool.pdm.version]\nsource = "call"\n'
                'getter = "scripts.version:get_version"\n',
                "src/x/__init__.py": "",
                "scripts/version.py": "from pathlib import Path\nfrom scripts import util\n\n"
                "def get_version():\n    return (Path(__file__).parent / 'VERSION').read_text()\n",
                "scripts/util.py": "",
                "scripts/VERSION": "",
                "scripts/other.py": "",
            },
            {"src/x/__init__.py", "scripts/version.py", "scripts/util.py", "scripts/VERSION"},
        )

    def test_code_version_source_file_is_scanned(self):
        c = self.resolve(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\ndynamic = ["version"]\n'
                '[tool.hatch.version]\nsource = "code"\npath = "version_source.py"\n'
                '[tool.hatch.build.targets.wheel]\npackages = ["pkg"]\n',
                "pkg/__init__.py": "",
                "version_source.py": "import os\nVERSION = open(os.environ['VERSION_FILE']).read()\n",
            }
        )
        self.assertTrue(any("reads a file" in p for p in c.problems))

    def test_pdm_unknown_version_source_is_unresolved(self):
        c = self.resolve(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "pdm.backend"\n[project]\nname = "x"\n'
                '[tool.pdm.version]\nsource = "exotic"\n',
                "src/x/__init__.py": "",
            }
        )
        self.assertTrue(any("version source" in p for p in c.problems))

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

    def test_pdm_wheel_data(self):
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "pdm.backend"\n[project]\nname = "x"\n'
                '[tool.pdm.build.wheel-data]\nscripts = ["bin/x-run"]\n'
                'data = [{ path = "share/**/*.svg", relative-to = "share" }]\n',
                "src/x/__init__.py": "",
                "bin/x-run": "",
                "share/icons/x.svg": "",
                "share/icons/unused.png": "",
            },
            {"src/x/__init__.py", "bin/x-run", "share/icons/x.svg"},
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
                '[tool.maturin]\npython-source = "python"\nmodule-name = "x._core"\ndata = "x.data"\n',
                "Cargo.toml": "",
                "Cargo.lock": "",
                "src/lib.rs": "",
                "python/x/__init__.py": "",
                "x.data/scripts/x-tool": "",
            },
            {"Cargo.toml", "Cargo.lock", "src/lib.rs", "python/x/__init__.py", "x.data/scripts/x-tool"},
        )

    def test_maturin_workspace_member_tracks_the_workspace_manifest_and_lock(self):
        self.assert_matches(
            {
                "pyproject.toml": '[build-system]\nbuild-backend = "maturin"\n[project]\nname = "x"\n'
                '[tool.maturin]\nmanifest-path = "crates/x/Cargo.toml"\n',
                "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
                "Cargo.lock": "",
                "crates/x/Cargo.toml": "",
                "crates/x/src/lib.rs": "",
                "crates/other/src/lib.rs": "",
            },
            {"Cargo.toml", "Cargo.lock", "crates/x/Cargo.toml", "crates/x/src/lib.rs"},
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


    def test_license_files_glob_follows_symlinks(self):
        self.assert_matches(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\nlicense-files = ["LICENSES/**"]\n'
                "[tool.hatch.build.targets.wheel]\npackages = [\"x\"]\n",
                "x/__init__.py": "",
                "shared/MIT.txt": "",
            },
            {"x/__init__.py", "LICENSES/MIT.txt", "shared/MIT.txt"},
            links={"LICENSES/MIT.txt": "../shared/MIT.txt"},
        )

    def test_default_license_file_follows_symlinks(self):
        c = self.resolve(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n[tool.hatch.build.targets.wheel]\npackages = ["x"]\n',
                "x/__init__.py": "",
                "legal/terms.txt": "",
            },
            links={"LICENSE": "legal/terms.txt"},
        )
        self.assertEqual(c.problems, [])
        self.assertIn("legal/terms.txt", self.matched(c))


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

    def test_material_reads_hatch_toml(self):
        base = self.commit(
            {
                "pyproject.toml": HATCH + '[project]\nname = "x"\n',
                "hatch.toml": '[build.targets.wheel]\npackages = ["x"]\n[envs.default]\ndependencies = ["pytest"]\n',
            }
        )
        envs_only = self.commit({"hatch.toml": '[build.targets.wheel]\npackages = ["x"]\n[envs.default]\ndependencies = ["pytest>=8"]\n'})
        build = self.commit({"hatch.toml": '[build.targets.wheel]\npackages = ["y"]\n[envs.default]\ndependencies = ["pytest>=8"]\n'})
        self.assertFalse(python_packaging.material(base, envs_only))
        self.assertTrue(python_packaging.material(envs_only, build))

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
