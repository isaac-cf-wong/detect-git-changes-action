---
name: 🐛 Bug Report
about: Create a report to help us improve
title: '[BUG]: '
labels: bug
assignees: ''
---

## 📝 Description

A clear and concise description of what the bug is.

## 🚀 Reproduction Steps

Steps to reproduce the behavior:

1. '...'
2. '...'
3. '...'

## 💻 Workflow Step

The step that uses the action, with its inputs. Use a code block for
readability:

```yaml
- uses: actions/checkout@v6
  with:
      fetch-depth: 0

- id: detect
  uses: isaac-cf-wong/detect-git-changes-action@<version>
  with:
      use-latest-tag: 'true'
      preset: python
```

If the action reads your build configuration (`preset: python` or
`preset: rust`), a link to the repository, or the relevant `pyproject.toml`,
`setup.py` or `Cargo.toml`, helps.

## 📋 Expected Behavior

A clear and concise description of what you expected to happen, for example
which files should or should not have counted as a change.

## 💥 Actual Behavior / Log Output

The outputs you got (`has-changes`, `changed-files-count`, `changed-files`) and
the step's log, including any `::warning::` lines, **after redacting secrets**
(API keys, tokens, credentials, private URLs/paths, personal data). If this is a
potential security vulnerability, do **not** post it publicly; use the Security
reporting channel instead.

```text
Comparing v1.2.3 ... HEAD
...
```

## 🛠 Environment Information

- **Action Version:** (e.g., v0.1.0 or a commit SHA)
- **Runner:** (e.g., ubuntu-latest, macos-latest, self-hosted)
- **Python Version on the Runner:** (for the presets, e.g., 3.12)
- **Base and Head Refs:** (e.g., `use-latest-tag`, or the `base-ref` and
  `head-ref` used)

## 📎 Additional Context

Add any other context about the problem here (e.g., screenshots, links to the
workflow run).
