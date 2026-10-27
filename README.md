# detect-git-changes-action

[![Integration Test Action](https://github.com/isaac-cf-wong/detect-git-changes-action/actions/workflows/test_action.yml/badge.svg)](https://github.com/isaac-cf-wong/detect-git-changes-action/actions/workflows/test_action.yml)
[![Unit Tests](https://github.com/isaac-cf-wong/detect-git-changes-action/actions/workflows/test_unit.yml/badge.svg)](https://github.com/isaac-cf-wong/detect-git-changes-action/actions/workflows/test_unit.yml)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/isaac-cf-wong/detect-git-changes-action/main.svg)](https://results.pre-commit.ci/latest/github/isaac-cf-wong/detect-git-changes-action/main)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/isaac-cf-wong/detect-git-changes-action/blob/main/LICENSE)
[![DOI](https://zenodo.org/badge/1178081717.svg)](https://doi.org/10.5281/zenodo.18955820)

A lightweight, robust GitHub Action to detect if changes have occurred in your
repository compared to a specific base reference (e.g., the latest tag, a
specific branch, or a commit SHA).

## Usage

To use this action, you must checkout your repository with full history
(fetch-depth: 0).

### Basic Usage (Comparing to latest tag)

```yml
- uses: actions/checkout@v6
  with:
      fetch-depth: 0

- name: Detect changes
  id: detect
  uses: isaac-cf-wong/detect-git-changes-action@v0.0.2
  with:
      use-latest-tag: 'true'

- name: Do something if changes exist
  if: steps.detect.outputs.has-changes == 'true'
  run:
      echo "Changes detected in ${{ steps.detect.outputs.changed-files-count }}
      files!"
```

### Path Filtering

Limit detection to specific files or directories with `paths`, and leave some
out with `exclude-paths`. Both take
[git pathspecs](https://git-scm.com/docs/gitglossary#Documentation/gitglossary.txt-aiddefpathspecapathspec),
separated by commas or newlines. A plain entry such as `docs/` matches
everything under it; prefix an entry with `:(glob)` for shell-style globbing
where `*` stays within one directory and `**` crosses directories. In
`exclude-paths` an entry is excluded as given; the short forms `:!docs/` and
`:^docs/` mean the same as `docs/`.

```yaml
- name: Detect changes in docs
  id: detect_docs
  uses: isaac-cf-wong/detect-git-changes-action@v0.0.2
  with:
      base-ref: 'main'
      paths: |
          docs/
          README.md
      exclude-paths: ':(glob)docs/**/*.svg'
```

### Presets

A preset tracks the files that materially affect a released package, so that a
scheduled release is skipped when only tooling, CI, docs or the lock file
changed.

```yaml
- name: Detect package changes since the last release
  id: detect
  uses: isaac-cf-wong/detect-git-changes-action@v0.0.2
  with:
      use-latest-tag: 'true'
      preset: python
```

`paths` adds to a preset and `exclude-paths` removes from it, so a project that
does not want README edits to trigger a release can drop them
(`exclude-paths: README.md`).

#### `python`

The preset reads the build configuration to find what goes into the installed
package (the wheel), rather than assuming a layout. The configuration is read,
never executed.

The source distribution is deliberately not followed: backends put tests, docs
and other repository files into the sdist (hatchling includes almost everything
by default), so tracking it would bring back a release for every docs edit.
Files that only reach the sdist, such as those a `MANIFEST.in` pulls in from
outside the package, therefore do not count; add them to `paths` if a change to
them should trigger a release.

| Tracked                                                                  | Why                              |
| ------------------------------------------------------------------------ | -------------------------------- |
| The package sources the build backend selects (see below)                | The package itself.              |
| `pyproject.toml`, `hatch.toml` (packaging tables only, see below)        | Metadata, dependencies, build.   |
| `setup.py`, `setup.cfg`, `MANIFEST.in`, `hatch_build.py`, `pdm_build.py` | Build scripts and manifests.     |
| `readme`, `license` and `license-files` in pyproject.toml                | Shipped in the package metadata. |
| Files dynamic metadata is read from (see below)                          | Version, dependencies, readme.   |
| `LICEN[CS]E*`, `LICEN[CS]ES/**`, `COPYING*`, `NOTICE*`, `AUTHORS*`       | Default license files.           |

Files that fill in metadata are tracked too, since changing one changes the
wheel's `METADATA`: setuptools `[tool.setuptools.dynamic]` `file` and `attr`
directives and `setup.cfg` `file:` / `attr:` directives (e.g. a
`requirements.txt` feeding `dependencies`), hatchling's `[tool.hatch.version]`
`path` and `search-paths` and the files given to the `requirements_txt` and
`fancy-pypi-readme` metadata hooks, pdm's `[tool.pdm.version]` file or `call`
getter module, and poetry's `[tool.poetry]` `readme`.

Python that runs during the build (`setup.py`, a hatch `code` version file, a
pdm version getter) is read, not run. Tracked: the files it names; the paths it
composes with `/`, `os.path.join()`, `Path()`, `.parent` or `dirname()`,
anchored where they point (the file's own directory for `__file__`); and the
local modules it imports, statically, through a literal
`importlib.import_module()` (relative names through its `package` argument) or
from a directory added by a direct `sys.path.insert()` / `append()` or
`site.addsitedir()` (under whatever name `import sys` or `import site` binds),
read the same way in turn. A name is followed only when it is bound once in the
file, and a reader helper such as `read("README.md")` is followed through its
direct call sites in the file the backend runs. A helper in a module that file
imports or runs can be called with anything, so its parameter-dependent reads
count as not located. Anything else counts as not located (see below): a read or
import whose path or name cannot be worked out, a reader function referenced
other than by a direct call (an alias, a callback, a caller in another file),
any other use of `sys.path` or of the `sys` and `site` modules themselves, a
hatch custom or unknown metadata hook, and an unknown version source. `open()`
and `os.open()` in a write-only mode are not reads. Version sources that take
the version from git (`vcs`, `uv-dynamic-versioning`, `versioningit`) read no
repository file.

This reading is a heuristic: arbitrary Python cannot be followed completely
without running it. It is built to fail open, reporting what it cannot work out
rather than guessing. For a build whose inputs it does not see, list them in
`paths`.

A symlink changes nothing in the diff when the file it points to is edited, so
for every symlink at, under or above a tracked path, or matched by a tracked
pattern, the target is tracked too, through chains of symlinks. A symlink that
points outside the repository, or symlinks that loop or nest more than 40 deep,
count as not located.

Package sources are located per build backend:

- **hatchling**: `packages`, `only-include`, `include` and `force-include` under
  `[tool.hatch.build]` or `[tool.hatch.build.targets.wheel]` (or the same tables
  in `hatch.toml`, which hatchling merges over them), plus the wheel's
  `shared-data`, `shared-scripts` and `extra-metadata`; otherwise hatchling's
  default, a package named after the project at the root or under `src/`.
- **setuptools**: `packages` (lists, `find:` and `find_packages()`),
  `package-dir`, `py-modules`, extension `sources` and `data-files` from
  `[tool.setuptools]`, `setup.cfg` `[options]` and the `setup()` call in
  `setup.py`; otherwise setuptools' src-layout / flat-layout auto-discovery. A
  `find` that matches no package directory counts as not located. Also used when
  there is no `[build-system]` table.
- **poetry-core**: `[tool.poetry]` `packages` and wheel `include`s; otherwise a
  package named after the project.
- **flit-core**: `[tool.flit.module]` `name`, or the project name.
- **pdm-backend**: `[tool.pdm.build]` `includes`, or `package-dir` (default
  `src/` when it exists), plus `wheel-data`.
- **uv_build**: `[tool.uv.build-backend]` `module-root` and `module-name`.
- **maturin**: `Cargo.toml`, `Cargo.lock`, `build.rs` and `src/` (or the
  `manifest-path` crate, with the `Cargo.toml` and `Cargo.lock` of any directory
  above it, where a Cargo workspace keeps them), the local crates it builds
  against through `path` dependencies (its own, `[workspace.dependencies]` and
  `[patch]`, but not dev-dependencies), plus `python-source` and `data`.

When the sources cannot be located, the action warns and says why: a `setup.py`
that computes its arguments, a build hook (a hatch custom hook, `pdm_build.py`,
a poetry build script, `setup.py` `cmdclass`) whose code may add files,
scikit-build-core and meson-python (whose native sources are not declared in
`pyproject.toml`), an unknown backend, or Python older than 3.11 on the runner.
Missing a release is worse than an extra one, so it then counts **every**
changed file, unless `paths` is set, in which case `paths` stands in for the
sources:

```yaml
with:
    use-latest-tag: 'true'
    preset: python
    paths: cpp/ # native sources of a scikit-build-core project
```

A change to `pyproject.toml` or `hatch.toml` counts only when it touches a table
that reaches the built distribution: `[build-system]`, `[project]`, and the
build backend's own configuration (`[tool.hatch.build]`, `[tool.hatch.version]`,
`[tool.hatch.metadata]`, `[tool.setuptools]`, `[tool.setuptools_scm]`,
`[tool.flit]`, `[tool.pdm.build]`, `[tool.pdm.version]`, `[tool.maturin]`,
`[tool.scikit-build]`, `[tool.meson-python]`, `[tool.uv.build-backend]`, and
`[tool.poetry]` apart from its dependency groups). Edits to
`[dependency-groups]`, `[tool.ruff]`, `[tool.pytest]`, hatch environments and
similar development-only tables do not, so routine dev-dependency bumps no
longer look like a release. When every changed file counts because the sources
were not located, this narrowing is off too, and any change to them counts.

Not tracked: `uv.lock` and other lock files (not shipped in a wheel), tests,
docs, CI workflows and tool configuration files.

Reading the build configuration needs Python 3.11 or later (`tomllib`) on the
runner, which GitHub-hosted runners provide; without it the sources count as not
located, as above: every changed file counts, or, with `paths` set, `paths` and
any change to `pyproject.toml`, `hatch.toml` or `README*`. A `pyproject.toml`
that cannot be parsed is treated as material.

#### `rust`

The preset reads `Cargo.toml` to find the crates that are released and what they
are built from. Each published crate counts: the root package, and every
workspace member that does not set `publish = false`. A member with
`publish = false` (an `xtask`, a fuzzing or benchmark crate) counts only when a
published crate builds against it.

| Tracked                                                       | Why                                    |
| ------------------------------------------------------------- | -------------------------------------- |
| Each crate's `src/`, and `lib`/`bin` targets placed elsewhere | The crate itself.                      |
| The root's and each tracked crate's `Cargo.toml` (narrowed)   | Dependencies, features, metadata.      |
| Files the code pulls in (see below)                           | Compiled into the crate.               |
| The build script and the inputs it declares                   | Its output is compiled into the crate. |
| Local crates reached through `path` dependencies              | Built into the crate.                  |
| The `readme`, `license-file` and default license files        | Shipped with the crate.                |
| `Cargo.lock`, `.cargo/config.toml`: binaries only (narrowed)  | Fix what a binary is built with.       |

Tests, benches and examples, `[dev-dependencies]`, docs, CI workflows and
`rust-toolchain.toml` do not count.

Files the code pulls in are found by reading the sources, never by building:

- `include_str!`, `include_bytes!` and `include!` with a string literal,
  resolved relative to the file that uses it; a `concat!` of literals and macro
  variables, as in `include_str!(concat!("../lang/", $lang, ".txt"))`, counts
  every file it can name; `env!("OUT_DIR")` paths are the build script's output,
  covered by its inputs.
- `#[path = "..."]` modules, and the modules of a target outside `src/`.
- Any path-like string, or one passed to a macro or attribute, that names a
  tracked file or directory, relative to the file, the crate or its `src/`: this
  covers proc macros that read files, such as `include_dir!`, rust-embed's
  `#[folder]`, `sqlx::migrate!` and pest's `#[grammar]`. `$CARGO_MANIFEST_DIR`
  and `env!("CARGO_MANIFEST_DIR")` stand for the crate's directory.
- A file inside a git submodule counts through the submodule's commit.

A build script's inputs are those it declares with `cargo:rerun-if-changed`.
When it declares no `rerun-if-changed` or `rerun-if-env-changed` at all, cargo
treats every file in the crate as its input, and so does the preset: the sources
count as not located. The same holds for an include whose path is not a string
literal, a `rerun-if-changed` path formatted at build time, a path dependency
outside the repository, and a manifest that does not parse.

A change to a `Cargo.toml` counts only outside `[dev-dependencies]` (including
target-specific ones), `[lints]`, `[badges]`, `[package.metadata]`,
`[workspace.metadata]`, `[workspace.lints]` and the `[[bench]]`, `[[test]]` and
`[[example]]` targets. A change to `Cargo.lock` counts only when it changes a
locked package that a published crate builds with: one reachable from its normal
and build dependencies. Cargo.lock does not record which features are enabled,
so a package switched on only by a dev-dependency's feature can still count. A
project that does not want lock-file updates to trigger a release can drop them
with `exclude-paths: Cargo.lock`.

The preset needs Python 3.11 or later on the runner, as the `python` preset
does; without it, every changed file counts, or, with `paths` set, `paths` and
any change to a `Cargo.toml`, `Cargo.lock` or `README*`.

## Inputs

| Input            | Description                                                            | Required | Default |
| ---------------- | ---------------------------------------------------------------------- | -------- | ------- |
| `base-ref`       | Base ref to compare from (branch/tag/sha).                             | No       | -       |
| `use-latest-tag` | Automatically use the latest tag as base.                              | No       | `false` |
| `head-ref`       | Head ref to compare to.                                                | No       | `HEAD`  |
| `paths`          | Pathspecs to limit the comparison to, comma- or newline-separated.     | No       | -       |
| `exclude-paths`  | Pathspecs to leave out of the comparison, comma- or newline-separated. | No       | -       |
| `preset`         | Track the files that materially affect a package: `python` or `rust`.  | No       | -       |

## Outputs

| Output                | Description                                             |
| --------------------- | ------------------------------------------------------- |
| `has-changes`         | `true` if changes exist, `false` otherwise.             |
| `changed-files-count` | Number of changed files that count towards the verdict. |
| `changed-files`       | Newline-separated list of the changed files that count. |

A file renamed or moved counts under both its old and its new path, so moving a
file out of a tracked directory is detected. With `use-latest-tag` and no tag
yet, the first release is reported as a change with no files listed. The changed
files are also written to the job summary.

## Important Notes

- `fetch-depth: 0`: This action performs git operations that require local
  access to your commit history. Ensure your `actions/checkout` step includes
  `fetch-depth: 0` to avoid "bad revision" errors.
