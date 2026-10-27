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
