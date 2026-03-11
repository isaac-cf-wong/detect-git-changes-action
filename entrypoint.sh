#!/bin/bash

set -euo pipefail

# ────────────────────────────────────────────────
# Resolve effective BASE ref
# ────────────────────────────────────────────────
if [[ "${INPUT_USE_LATEST_TAG:-false}" == "true" ]]; then
	BASE_REF=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
	if [[ -z "$BASE_REF" ]]; then
		echo "No tags found → treating as first release (has changes = true)"
		echo "has_changes=true" >>"$GITHUB_OUTPUT"
		echo "changed_files_count=0" >>"$GITHUB_OUTPUT"
		exit 0
	fi
	echo "::notice::Using latest tag as base: $BASE_REF"
else
	BASE_REF="${INPUT_BASE_REF?Error: base-ref is required when use-latest-tag is not true}"
fi

HEAD_REF="${INPUT_HEAD_REF:-HEAD}"
PATHS="${INPUT_PATHS:-}"

echo "Comparing ${BASE_REF} ... ${HEAD_REF}"

# Resolve to real SHAs (handles origin/ prefix if needed)
BASE_SHA=$(git rev-parse --verify "${BASE_REF}" 2>/dev/null || git rev-parse --verify "origin/${BASE}" 2>/dev/null || echo "")
HEAD_SHA=$(git rev-parse --verify "${HEAD_REF}" 2>/dev/null || git rev-parse --verify "origin/${HEAD}" 2>/dev/null || echo "")

if [[ -z "${BASE_SHA}" ]]; then
	echo "::error::Cannot resolve base-ref '${BASE}'" >&2
	exit 1
fi

if [[ -z "${HEAD_SHA}" ]]; then
	echo "::error::Cannot resolve head-ref '${HEAD}'" >&2
	exit 1
fi

if [[ "${BASE_SHA}" == "${HEAD_SHA}" ]]; then
	echo "Refs point to the same commit → no changes"
	echo "has_changes=false" >>"${GITHUB_OUTPUT}"
	echo "changed_files_count=0" >>"${GITHUB_OUTPUT}"
	exit 0
fi

# Build diff command
diff_cmd=(git diff --quiet --exit-code "${BASE_SHA}" "${HEAD_SHA}")
count_cmd=(git diff --name-only "${BASE_SHA}" "${HEAD_SHA}")

if [[ -n "${PATHS}" ]]; then
	IFS=',' read -ra path_array <<<"${PATHS}"
	diff_cmd+=(--)
	count_cmd+=(--)
	for p in "${path_array[@]}"; do
		diff_cmd+=("${p}")
		count_cmd+=("${p}")
	done
fi

echo "Running: ${diff_cmd[*]}"

if "${diff_cmd[@]}"; then
	echo "No changes detected"
	echo "has_changes=false" >>"${GITHUB_OUTPUT}"
	echo "changed_files_count=0" >>"${GITHUB_OUTPUT}"
else
	count=$("${count_cmd[@]}" | sort -u | wc -l | tr -d '[:space:]')
	echo "Changes found (${count} file(s))"
	echo "has_changes=true" >>"${GITHUB_OUTPUT}"
	echo "changed_files_count=${count}" >>"${GITHUB_OUTPUT}"
fi
