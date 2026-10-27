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
where `*` stays within one directory and `**` crosses directories.

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
| `pyproject.toml` (packaging tables only, see below)                      | Metadata, dependencies, build.   |
| `setup.py`, `setup.cfg`, `MANIFEST.in`, `hatch_build.py`, `pdm_build.py` | Build scripts and manifests.     |
| `readme`, `license` and `license-files` in pyproject.toml                | Shipped in the package metadata. |
| `LICEN[CS]E*`, `LICEN[CS]ES/**`, `COPYING*`, `NOTICE*`, `AUTHORS*`       | Default license files.           |

Package sources are located per build backend:

- **hatchling**: `packages`, `only-include`, `include` and `force-include` under
  `[tool.hatch.build]` or `[tool.hatch.build.targets.wheel]`, plus the wheel's
  `shared-data`, `shared-scripts` and `extra-metadata`; otherwise hatchling's
  default, a package named after the project at the root or under `src/`.
- **setuptools**: `packages` (lists, `find:` and `find_packages()`),
  `package-dir`, `py-modules`, extension `sources` and `data-files` from
  `[tool.setuptools]`, `setup.cfg` `[options]` and the `setup()` call in
  `setup.py`; otherwise setuptools' src-layout / flat-layout auto-discovery.
  Also used when there is no `[build-system]` table.
- **poetry-core**: `[tool.poetry]` `packages` and wheel `include`s; otherwise a
  package named after the project.
- **flit-core**: `[tool.flit.module]` `name`, or the project name.
- **pdm-backend**: `[tool.pdm.build]` `includes`, or `package-dir` (default
  `src/` when it exists), plus `wheel-data`.
- **uv_build**: `[tool.uv.build-backend]` `module-root` and `module-name`.
- **maturin**: `Cargo.toml`, `Cargo.lock`, `build.rs` and `src/` (or the
  `manifest-path` crate), plus `python-source` and `data`.

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

A change to `pyproject.toml` counts only when it touches a table that reaches
the built distribution: `[build-system]`, `[project]`, and the build backend's
own configuration (`[tool.hatch.build]`, `[tool.hatch.version]`,
`[tool.hatch.metadata]`, `[tool.setuptools]`, `[tool.setuptools_scm]`,
`[tool.flit]`, `[tool.pdm.build]`, `[tool.pdm.version]`, `[tool.maturin]`,
`[tool.scikit-build]`, `[tool.meson-python]`, `[tool.uv.build-backend]`, and
`[tool.poetry]` apart from its dependency groups). Edits to
`[dependency-groups]`, `[tool.ruff]`, `[tool.pytest]`, hatch environments and
similar development-only tables do not, so routine dev-dependency bumps no
longer look like a release.

Not tracked: `uv.lock` and other lock files (not shipped in a wheel), tests,
docs, CI workflows and tool configuration files.

Reading the build configuration needs Python 3.11 or later (`tomllib`) on the
runner, which GitHub-hosted runners provide; without it the sources count as not
located, as above, and any change to `pyproject.toml` or `README*` counts. A
`pyproject.toml` that cannot be parsed is treated as material.

## Inputs

| Input            | Description                                                            | Required | Default |
| ---------------- | ---------------------------------------------------------------------- | -------- | ------- |
| `base-ref`       | Base ref to compare from (branch/tag/sha).                             | No       | -       |
| `use-latest-tag` | Automatically use the latest tag as base.                              | No       | `false` |
| `head-ref`       | Head ref to compare to.                                                | No       | `HEAD`  |
| `paths`          | Pathspecs to limit the comparison to, comma- or newline-separated.     | No       | -       |
| `exclude-paths`  | Pathspecs to leave out of the comparison, comma- or newline-separated. | No       | -       |
| `preset`         | Track the files that materially affect a package. Supported: `python`. | No       | -       |

## Outputs

| Output                | Description                                             |
| --------------------- | ------------------------------------------------------- |
| `has-changes`         | `true` if changes exist, `false` otherwise.             |
| `changed-files-count` | Number of changed files that count towards the verdict. |
| `changed-files`       | Newline-separated list of the changed files that count. |

A file renamed or moved counts under both its old and its new path, so moving a
file out of a tracked directory is detected. The changed files are also written
to the job summary.

## Important Notes

- `fetch-depth: 0`: This action performs git operations that require local
  access to your commit history. Ensure your `actions/checkout` step includes
  `fetch-depth: 0` to avoid "bad revision" errors.
