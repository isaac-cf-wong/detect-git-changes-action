#!/usr/bin/env bats

# Suppress SC2031/SC2030 because BATS subshell behavior is intentional
# shellcheck disable=SC2030,SC2031

SCRIPT_PATH="$(pwd)/entrypoint.sh"

# Helper: Setup a temp git repo
setup() {
	TEST_DIR=$(mktemp -d)
	export TEST_DIR

	# Define a temporary file and EXPORT it so the child process (entrypoint.sh) sees it
	export GITHUB_OUTPUT="$TEST_DIR/github_output"
	touch "$GITHUB_OUTPUT"

	cd "$TEST_DIR" || exit 1
	git init
	git config user.email "test@example.com"
	git config user.name "Test User"
	# Create initial commit
	touch file1.txt && git add . && git commit -m "Initial"
}

teardown() {
	rm -rf "$TEST_DIR"
}

@test "detects initial release when no tags exist" {
	export INPUT_USE_LATEST_TAG="true"
	run bash "$SCRIPT_PATH"

	if [ "$status" -ne 0 ]; then
		echo "Status: $status"
		echo "Output: $output"
		# Also check stderr if BATS version supports it
		[ -n "$stderr" ] && echo "Stderr: $stderr"
	fi

	[ "$status" -eq 0 ]
	grep "has_changes=true" "$GITHUB_OUTPUT"
}

@test "identifies latest tag as base" {
	git tag v1.0.0
	touch file2.txt && git add . && git commit -m "New"

	export INPUT_USE_LATEST_TAG="true"
	run bash "$SCRIPT_PATH"

	[ "$status" -eq 0 ]
	# Check if logic picked up v1.0.0
	[[ "$output" == *"Using latest tag as base: v1.0.0"* ]]
}
