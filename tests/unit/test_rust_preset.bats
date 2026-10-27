#!/usr/bin/env bats

# Suppress SC2031/SC2030 because BATS subshell behavior is intentional
# shellcheck disable=SC2030,SC2031

SCRIPT_PATH="$(pwd)/entrypoint.sh"

# A crate with a library and a binary, as the Rust package template has.
write_manifest() {
	cat >Cargo.toml <<'EOF'
[package]
name = "demo"
version = "0.1.0"
edition = "2021"
readme = "README.md"

[dependencies]
clap = "4"

[dev-dependencies]
assert_cmd = "2"

[lints.clippy]
pedantic = "warn"
EOF
}

commit_all() {
	git add -A && git commit -q -m "$1"
}

output_value() {
	grep "^$1=" "$GITHUB_OUTPUT" | tail -n 1 | cut -d= -f2-
}

changed_files() {
	awk '/^changed_files<</ { delim = substr($0, index($0, "<<") + 2); grab = 1; next }
		grab && $0 == delim { grab = 0; next }
		grab { print }' "$GITHUB_OUTPUT"
}

setup() {
	TEST_DIR=$(mktemp -d)
	export TEST_DIR
	export GITHUB_OUTPUT="$TEST_DIR/github_output"
	touch "$GITHUB_OUTPUT"

	REPO="$TEST_DIR/repo"
	mkdir -p "$REPO"
	cd "$REPO" || exit 1
	git init -q
	git config user.email "test@example.com"
	git config user.name "Test User"

	mkdir -p src tests benches examples docs .github/workflows
	echo "pub fn hi() {}" >src/lib.rs
	echo "fn main() {}" >src/main.rs
	echo "# demo" >README.md
	echo "BSD" >LICENSE
	echo "lock" >Cargo.lock
	echo "#[test] fn t() {}" >tests/cli.rs
	echo "fn main() {}" >benches/b.rs
	echo "fn main() {}" >examples/e.rs
	echo "docs" >docs/index.md
	echo "name: ci" >.github/workflows/ci.yml
	echo '[toolchain]' >rust-toolchain.toml
	write_manifest
	commit_all "Initial"
	git tag v0.1.0

	unset INPUT_PATHS INPUT_EXCLUDE_PATHS INPUT_BASE_REF DETECT_CHANGES_PYTHON
	export INPUT_USE_LATEST_TAG="true"
	export INPUT_PRESET="rust"
}

teardown() {
	rm -rf "$TEST_DIR"
}

@test "rust preset: a source change counts" {
	echo "pub fn bye() {}" >>src/lib.rs
	commit_all "src"

	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "src/lib.rs" ]
}

@test "rust preset: tests, benches, examples, docs, CI and toolchain do not count" {
	echo "// more" >>tests/cli.rs
	echo "// more" >>benches/b.rs
	echo "// more" >>examples/e.rs
	echo "more" >>docs/index.md
	echo "  on: push" >>.github/workflows/ci.yml
	echo 'channel = "stable"' >>rust-toolchain.toml
	commit_all "noise"

	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "false" ]
	[[ "$output" != *"::warning::"* ]]
}

@test "rust preset: dev-dependency and lint changes in Cargo.toml do not count" {
	sed -i.bak -e 's/assert_cmd = "2"/assert_cmd = "2.1"/' -e 's/pedantic = "warn"/pedantic = "deny"/' Cargo.toml && rm Cargo.toml.bak
	commit_all "dev deps and lints"

	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "false" ]
	[[ "$output" == *"Cargo.toml changed outside its packaging tables"* ]]
}

@test "rust preset: a runtime dependency change in Cargo.toml counts" {
	sed -i.bak 's/clap = "4"/clap = "4.6"/' Cargo.toml && rm Cargo.toml.bak
	commit_all "bump clap"

	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "Cargo.toml" ]
}

@test "rust preset: Cargo.lock counts for a crate with a binary, not for a library" {
	echo "lock 2" >Cargo.lock
	commit_all "lock maintenance"

	run bash "$SCRIPT_PATH"

	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "Cargo.lock" ]

	git rm -q src/main.rs
	commit_all "library only"
	git tag v0.2.0
	echo "lock 3" >Cargo.lock
	commit_all "lock maintenance"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "false" ]
}

@test "rust preset: readme and license count" {
	echo "more" >>README.md
	commit_all "readme"

	run bash "$SCRIPT_PATH"

	[ "$(output_value has_changes)" = "true" ]

	git tag v0.2.0
	echo "more" >>LICENSE
	commit_all "license"
	: >"$GITHUB_OUTPUT"
	run bash "$SCRIPT_PATH"

	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "LICENSE" ]
}

@test "rust preset: a build script without declared inputs makes every file count" {
	echo "fn main() {}" >build.rs
	commit_all "build script"
	git tag v0.2.0
	echo "more" >>docs/index.md
	commit_all "docs"

	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "docs/index.md" ]
	[[ "$output" == *"::warning::rust preset: build.rs declares no cargo:rerun-if-changed"* ]]
}

@test "rust preset: without tomllib and paths, every changed file counts" {
	echo "more" >>docs/index.md
	commit_all "docs"

	export DETECT_CHANGES_PYTHON="/nonexistent/python3"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	grep -q '^::warning::Python 3.11+.*rust preset.*every changed file counts' <<<"$output"
}

@test "rust preset: without tomllib, a member crate's readme counts when paths is set" {
	mkdir -p crates/app
	echo "# app" >crates/app/README.md
	commit_all "member readme"
	git tag v0.2.0
	echo "more" >>crates/app/README.md
	commit_all "edit member readme"

	export INPUT_PATHS="src/"
	export DETECT_CHANGES_PYTHON="/nonexistent/python3"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	[ "$(output_value has_changes)" = "true" ]
	[ "$(changed_files)" = "crates/app/README.md" ]
}
