# detect-git-changes-action

[![Integration Test Action](https://github.com/isaac-cf-wong/detect-git-changes-action/actions/workflows/test_action.yml/badge.svg)](https://github.com/isaac-cf-wong/detect-git-changes-action/actions/workflows/test_action.yml)
[![Unit Tests](https://github.com/isaac-cf-wong/detect-git-changes-action/actions/workflows/test_unit.yml/badge.svg)](https://github.com/isaac-cf-wong/detect-git-changes-action/actions/workflows/test_unit.yml)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/isaac-cf-wong/detect-git-changes-action/main.svg)](https://results.pre-commit.ci/latest/github/isaac-cf-wong/detect-git-changes-action/main)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/isaac-cf-wong/detect-git-changes-action/blob/main/LICENSE)

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
  uses: isaac-cf-wong/detect-git-changes-action@v0
  with:
      use-latest-tag: 'true'

- name: Do something if changes exist
  if: steps.detect.outputs.has-changes == 'true'
  run:
      echo "Changes detected in ${{ steps.detect.outputs.changed-files-count }}
      files!"
```

### Advanced Usage (Path Filtering)

You can limit the detection to specific directories or file globs.

```yaml
- name: Detect changes in docs
  id: detect_docs
  uses: isaac-cf-wong/detect-git-changes-action@v0
  with:
      base-ref: 'main'
      paths: 'docs/**,README.md'
```

## Inputs

| Input            | Description                                      | Required | Default |
| ---------------- | ------------------------------------------------ | -------- | ------- |
| `base-ref`       | Base ref to compare from (branch/tag/sha).       | No       | -       |
| `use-latest-tag` | Automatically use the latest tag as base.        | No       | `false` |
| `head-ref`       | Head ref to compare to.                          | No       | `HEAD`  |
| `paths`          | Comma-separated paths/globs to limit comparison. | No       | -       |

## Outputs

| Output                | Description                                 |
| --------------------- | ------------------------------------------- |
| `has-changes`         | `true` if changes exist, `false` otherwise. |
| `changed-files-count` | Approximate number of changed files.        |

## Important Notes

- `fetch-depth: 0`: This action performs git operations that require local
  access to your commit history. Ensure your `actions/checkout` step includes
  `fetch-depth: 0` to avoid "bad revision" errors.
