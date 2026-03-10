# Contributing to detect-git-changes-action

🎉 Thank you for your interest in contributing to `detect-git-changes-action`!
Your ideas, fixes, and improvements are welcome and appreciated.

Whether you’re fixing a typo, reporting a bug, suggesting a feature, or
submitting a pull request—this guide will help you get started.

## How to Contribute

<!-- prettier-ignore-start -->

1. Open an Issue

    - Have a question, bug report, or feature suggestion?
    [Open an issue](https://github.com/isaac-cf-wong/detect-git-changes-action/issues/new/choose)
    and describe your idea clearly.
    - Check for existing issues before opening a new one.

2. Fork and Clone the Repository

    ```shell
    git clone git@github.com:isaac-cf-wong/detect-git-changes-action.git
    cd detect-git-changes-action
    ```

3. Set Up Pre-commit Hooks

    We use **pre-commit** to ensure code quality and consistency,
    and **commitlint** to enforce commit message conventions.
    After installing dependencies, run:

    ```shell
    pre-commit install
    pre-commit install --hook-type commit-msg
    ```

    This ensures checks like code formatting, linting, and basic hygiene run automatically when you commit.

4. Create a New Branch

    Give it a meaningful name like fix-typo-in-docs or feature-add-summary-option.

5. Make Changes

    - Write clear, concise, and well-documented code.
    - **Keep changes atomic and focused**: one type of change per pull request
      (e.g., do not mix refactoring with feature addition).

6. Open a Pull Request

    Clearly describe the motivation and scope of your change. Link it to the relevant issue if applicable.
    The pull request titles should match the [Conventional Commits spec](https://www.conventionalcommits.org/).

<!-- prettier-ignore-end -->

## Commit Message Guidelines

**Why this matters:** Our changelog is automatically generated from commit
messages using git-cliff. Commit messages must follow the Conventional Commits
format and adhere to strict rules.

### Rules

<!-- prettier-ignore-start -->

1. **One type of change per pull request**

    - Do not mix different types of changes (e.g., bug fixes, features, refactoring) in a single pull request.
    - Example: if you refactor code AND add a feature, make two separate pull requests.

2. **Descriptive and meaningful messages**

    - Describe _what_ changed and _why_, not just _what_ was edited.
    - Avoid vague messages like "fix bug" or "update code";
      instead use "fix: Fix the logic to compare branches" or "feat: Add support for comparing changes in a specific file".

3. **Follow Conventional Commits format**

    - All pull request titles must follow the [Conventional Commits](https://www.conventionalcommits.org/) standard.
    - Format: `<type>(<scope>): <subject>`
    - Allowed types:
        - build: Changes that affect the build system or external dependencies
        - ci: Changes to our CI configuration files and scripts
        - docs: Documentation only changes
        - feat: A new feature
        - fix: A bug fix
        - perf: A code change that improves performance
        - refactor: A code change that neither fixes a bug nor adds a feature
        - style: Changes that do not affect the meaning of the code (white-space, formatting, missing semi-colons, etc.)
        - test: Adding missing tests or correcting existing tests

<!-- prettier-ignore-end -->

## 💡 Tips

- Be kind and constructive in your communication.
- Keep PRs focused and atomic—smaller changes are easier to review.
- Document new features and update existing docs if needed.
- Tag your PR with relevant labels if you can.

## Licensing

By contributing, you agree that your contributions will be licensed under the
project’s MIT License.

---

Thanks again for being part of the `detect-git-changes-action` community!

---
