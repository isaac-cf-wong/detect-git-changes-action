#!/usr/bin/env bats

# Suppress SC2031/SC2030 because BATS subshell behavior is intentional
# shellcheck disable=SC2030,SC2031

SCRIPT_PATH="$(pwd)/entrypoint.sh"

# The pyproject.toml the python preset is judged against. Each test edits one table of it.
write_pyproject() {
	cat >pyproject.toml <<'EOF'
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "demo"
version = "0.1.0"
readme = "README.md"
dependencies = ["typer>=0.26"]

[dependency-groups]
dev = ["ruff>=0.17.0"]

[tool.hatch.build.targets.wheel]
packages = ["src/demo"]

[tool.hatch.envs.default]
dependencies = ["pytest"]

[tool.ruff]
line-length = 120
EOF
}

commit_all() {
	git add -A && git commit -q -m "$1"
}

# Read one single-line output back from $GITHUB_OUTPUT.
output_value() {
	grep "^$1=" "$GITHUB_OUTPUT" | tail -n 1 | cut -d= -f2-
}

# Read the multi-line changed_files output (heredoc delimiter form) back from $GITHUB_OUTPUT.
changed_files() {
	awk '/^changed_files<</ { delim = substr($0, index($0, "<<") + 2); grab = 1; next }
		grab && $0 == delim { grab = 0; next }
		grab { print }' "$GITHUB_OUTPUT"
}

setup() {
	TEST_DIR=$(mktemp -d)
	export TEST_DIR
	export GITHUB_OUTPUT="$TEST_DIR/github_output"
	export GITHUB_STEP_SUMMARY="$TEST_DIR/step_summary"
	touch "$GITHUB_OUTPUT" "$GITHUB_STEP_SUMMARY"

	REPO="$TEST_DIR/repo"
	mkdir -p "$REPO"
	cd "$REPO" || exit 1
	git init -q
	git config user.email "test@example.com"
	git config user.name "Test User"

	mkdir -p src/demo docs .github/workflows
	echo "x = 1" >src/demo/__init__.py
	echo "# demo" >README.md
	echo "MIT" >LICENSE
	echo "docs" >docs/index.md
	echo "name: ci" >.github/workflows/ci.yml
	echo "lock" >uv.lock
	write_pyproject
	commit_all "Initial"
	git tag v0.1.0

	unset INPUT_PATHS INPUT_EXCLUDE_PATHS INPUT_PRESET INPUT_BASE_REF
	export INPUT_USE_LATEST_TAG="true"
}

teardown() {
	rm -rf "$TEST_DIR"
}

# ── Low-level path controls ─────────────────────────────────────────

@test "paths accepts newline-separated entries with surrounding whitespace" {
	echo "more" >>docs/index.md
	commit_all "docs"

	export INPUT_PATHS=$'  src/\n\n  docs/  \n'
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "docs/index.md" ]
}

@test "paths accepts comma-separated entries with spaces after commas" {
	echo "more" >>docs/index.md
	commit_all "docs"

	export INPUT_PATHS="src/, docs/"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
}

@test "exclude-paths removes matching files from the verdict" {
	echo "more" >>docs/index.md
	echo "y = 2" >>src/demo/__init__.py
	commit_all "docs and src"

	export INPUT_EXCLUDE_PATHS="docs/"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "src/demo/__init__.py" ]
	[ "$(output_value changed_files_count)" = "1" ]
}

@test "exclude-paths alone can turn the verdict to no changes" {
	echo "more" >>docs/index.md
	commit_all "docs"

	export INPUT_EXCLUDE_PATHS="docs/"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "false" ]
	[ "$(output_value changed_files_count)" = "0" ]
}

@test "exclude-paths honours pathspec magic such as :(glob)" {
	mkdir -p docs/deep
	echo "more" >>docs/index.md
	echo "deep" >docs/deep/page.md
	echo "y = 2" >>src/demo/__init__.py
	commit_all "docs and src"

	export INPUT_EXCLUDE_PATHS=":(glob)docs/**/*.md"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(changed_files)" = "src/demo/__init__.py" ]
}

@test "a file moved out of a tracked directory counts as a change to it" {
	mkdir -p scripts
	git mv src/demo/__init__.py scripts/moved.py
	commit_all "move"

	export INPUT_PATHS="src/"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "src/demo/__init__.py" ]
}

@test "changed-files-count is exact and changed-files lists every file" {
	echo "a" >>docs/index.md
	echo "b" >>uv.lock
	echo "c" >>src/demo/__init__.py
	commit_all "three"

	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value changed_files_count)" = "3" ]
	[ "$(changed_files)" = $'docs/index.md\nsrc/demo/__init__.py\nuv.lock' ]
}

# ── python preset ───────────────────────────────────────────────────

@test "python preset: source change is material" {
	echo "y = 2" >>src/demo/__init__.py
	commit_all "src"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "src/demo/__init__.py" ]
}

@test "python preset: lock file, docs, CI and tooling config are not material" {
	echo "more" >>uv.lock
	echo "more" >>docs/index.md
	echo "on: push" >>.github/workflows/ci.yml
	echo "repos: []" >.pre-commit-config.yaml
	commit_all "noise"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "false" ]
	[ "$(output_value changed_files_count)" = "0" ]
}

@test "python preset: dev dependency-group bump in pyproject.toml is not material" {
	sed -i.bak 's/ruff>=0.17.0/ruff>=0.17.3/' pyproject.toml && rm pyproject.toml.bak
	echo "lock2" >uv.lock
	commit_all "bump ruff"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "false" ]
}

@test "python preset: tool config and hatch envs in pyproject.toml are not material" {
	sed -i.bak -e 's/line-length = 120/line-length = 100/' -e 's/\["pytest"\]/["pytest", "mock"]/' pyproject.toml
	rm pyproject.toml.bak
	commit_all "tooling"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "false" ]
}

@test "python preset: runtime dependency change in pyproject.toml is material" {
	sed -i.bak 's/typer>=0.26/typer>=0.27/' pyproject.toml && rm pyproject.toml.bak
	commit_all "bump typer"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "pyproject.toml" ]
}

@test "python preset: build-system change in pyproject.toml is material" {
	sed -i.bak 's/requires = \["hatchling"\]/requires = ["hatchling>=1.27"]/' pyproject.toml && rm pyproject.toml.bak
	commit_all "pin backend"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "pyproject.toml" ]
}

@test "python preset: wheel build config in pyproject.toml is material" {
	sed -i.bak 's|packages = \["src/demo"\]|packages = ["src/demo", "src/extra"]|' pyproject.toml && rm pyproject.toml.bak
	commit_all "build config"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "pyproject.toml" ]
}

@test "python preset: unparsable pyproject.toml is treated as material" {
	echo "this is = = not toml" >>pyproject.toml
	commit_all "broken"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[[ "$output" == *"::warning::"* ]]
}

@test "python preset: the readme named in pyproject.toml is material" {
	echo "more" >>README.md
	commit_all "readme"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "README.md" ]
}

@test "python preset: a readme not named in pyproject.toml is not material" {
	echo "notes" >README.rst
	commit_all "other readme"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "false" ]
}

@test "python preset: license files are material" {
	echo "more" >>LICENSE
	commit_all "license"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "LICENSE" ]
}

@test "python preset: license-files globs declared in pyproject.toml are material" {
	sed -i.bak 's/^readme = "README.md"$/readme = "README.md"\nlicense-files = ["legal\/**"]/' pyproject.toml
	rm pyproject.toml.bak
	mkdir -p legal/sub
	echo "terms" >legal/sub/TERMS.txt
	commit_all "add legal"
	git tag v0.2.0
	echo "more" >>legal/sub/TERMS.txt
	commit_all "edit legal"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "legal/sub/TERMS.txt" ]
}

@test "python preset: paths extends the preset for a flat layout" {
	mkdir -p demo
	echo "z = 3" >demo/core.py
	commit_all "flat package"

	export INPUT_PRESET="python"
	export INPUT_PATHS="demo/"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "demo/core.py" ]
}

@test "python preset: exclude-paths carves files out of the preset" {
	echo "more" >>README.md
	commit_all "readme"

	export INPUT_PRESET="python"
	export INPUT_EXCLUDE_PATHS="README.md"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "false" ]
}

@test "python preset: falls back to any pyproject.toml change when tomllib is unavailable" {
	sed -i.bak 's/ruff>=0.17.0/ruff>=0.17.3/' pyproject.toml && rm pyproject.toml.bak
	commit_all "bump ruff"

	export INPUT_PRESET="python"
	export DETECT_CHANGES_PYTHON="/nonexistent/python3"
	run bash "$SCRIPT_PATH"
	unset DETECT_CHANGES_PYTHON

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[[ "$output" == *"::warning::"* ]]
}

@test "unknown preset fails with an error" {
	export INPUT_PRESET="cobol"
	run bash "$SCRIPT_PATH"

	[ "$status" -ne 0 ]
	[[ "$output" == *"::error::"* ]]
}

@test "an invalid pathspec fails instead of reporting no changes" {
	echo "y = 2" >>src/demo/__init__.py
	commit_all "src"

	export INPUT_PATHS=":(nosuchmagic)src/"
	run bash "$SCRIPT_PATH"

	[ "$status" -ne 0 ]
	if grep -q "^has_changes=false" "$GITHUB_OUTPUT"; then false; fi
}

@test "python preset: package location comes from pyproject.toml, not a fixed src/" {
	mkdir -p lib/demo
	echo "z = 3" >lib/demo/core.py
	sed -i.bak 's|packages = \["src/demo"\]|packages = ["lib/demo"]|' pyproject.toml && rm pyproject.toml.bak
	commit_all "move package to lib"
	git tag v0.2.0
	echo "y = 2" >>src/demo/__init__.py
	commit_all "edit the now-unpackaged src copy"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "false" ]

	git tag v0.3.0
	echo "w = 4" >>lib/demo/core.py
	commit_all "edit the package"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(changed_files)" = "lib/demo/core.py" ]
}

@test "python preset: sources that cannot be located make every file count" {
	git rm -q pyproject.toml
	printf 'from setuptools import setup\nsetup(**CONFIG)\n' >setup.py
	commit_all "opaque setup.py"
	git tag v0.2.0
	echo "more" >>docs/index.md
	commit_all "docs"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "docs/index.md" ]
	[[ "$output" == *"::warning::python preset: setup.py"* ]]
}

@test "python preset: paths stands in for sources that cannot be located" {
	git rm -q pyproject.toml
	printf 'from setuptools import setup\nsetup(**CONFIG)\n' >setup.py
	commit_all "opaque setup.py"
	git tag v0.2.0
	echo "more" >>docs/index.md
	commit_all "docs"

	export INPUT_PRESET="python"
	export INPUT_PATHS="src/"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "false" ]

	echo "y = 2" >>src/demo/__init__.py
	commit_all "src"
	run bash "$SCRIPT_PATH"

	[ "$(output_value has_changes)" = "true" ]
}

@test "python preset: files installed as wheel data count" {
	mkdir -p share/icons
	echo "<svg/>" >share/icons/demo.svg
	printf '[tool.hatch.build.targets.wheel.shared-data]\n"share/icons" = "share/demo/icons"\n' >>pyproject.toml
	commit_all "ship icons"
	git tag v0.2.0
	echo "<svg></svg>" >share/icons/demo.svg
	commit_all "edit icon"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "share/icons/demo.svg" ]
}

@test "python preset: sdist-only files are not material" {
	mkdir -p docs/assets
	echo "data" >docs/assets/schema.dat
	echo "recursive-include docs/assets *.dat" >MANIFEST.in
	commit_all "manifest"
	git tag v0.2.0
	echo "more" >>docs/assets/schema.dat
	commit_all "edit sdist-only file"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "false" ]
}

@test "python preset: a requirements file feeding dynamic dependencies counts" {
	cat >pyproject.toml <<'TOML'
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "demo"
version = "0.1.0"
dynamic = ["dependencies"]

[tool.setuptools]
package-dir = { "" = "src" }
packages = ["demo"]

[tool.setuptools.dynamic]
dependencies = { file = ["requirements.txt"] }
TOML
	echo "typer>=0.26" >requirements.txt
	commit_all "dynamic deps"
	git tag v0.2.0
	echo "typer>=0.27" >requirements.txt
	commit_all "bump runtime dependency"

	export INPUT_PRESET="python"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "requirements.txt" ]
}
