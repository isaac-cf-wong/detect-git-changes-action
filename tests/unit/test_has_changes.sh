#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

echo "→ Running unit tests for entrypoint.sh"

# Mock environment
GITHUB_OUTPUT=$(mktemp)
export GITHUB_OUTPUT

# Test 1: same commit
export INPUT_BASE_REF=HEAD
export INPUT_HEAD_REF=HEAD
bash entrypoint.sh
grep -q "has_changes=false" "${GITHUB_OUTPUT}" || {
	echo "Fail: same ref"
	exit 1
}
echo "✓ same ref → false"

# Test 2: invalid base → should fail
export INPUT_BASE_REF=does-not-exist-123abc
if bash entrypoint.sh 2>/dev/null; then
	echo "Fail: invalid ref did not error"
	exit 1
fi
echo "✓ invalid ref → errors"

# More tests: you can create a temp git repo here and commit files
echo "All quick unit tests passed"
rm -f "${GITHUB_OUTPUT}"
