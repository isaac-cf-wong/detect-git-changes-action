#!/bin/bash

set -euo pipefail

BASE="${INPUT_BASE_REF}"
HEAD="${INPUT_HEAD_REF:-HEAD}"
PATHS="${INPUT_PATHS:-}"

echo "Comparing ${BASE} ... ${HEAD}"

# Resolve to real SHAs (handles origin/ prefix if needed)
base_sha=$(git rev-parse --verify "${BASE}" 2>/dev/null || git rev-parse --verify "origin/${BASE}" 2>/dev/null || echo "")
head_sha=$(git rev-parse --verify "${HEAD}" 2>/dev/null || git rev-parse --verify "origin/${HEAD}" 2>/dev/null || echo "")

if [[ -z "${base_sha}" ]]; then
	echo "::error::Cannot resolve base-ref '${BASE}'" >&2
	exit 1
fi

if [[ -z "${head_sha}" ]]; then
	echo "::error::Cannot resolve head-ref '${HEAD}'" >&2
	exit 1
fi

if [[ "${base_sha}" == "${head_sha}" ]]; then
	echo "Refs point to the same commit → no changes"
	echo "has_changes=false" >>"${GITHUB_OUTPUT}"
	echo "changed_files_count=0" >>"${GITHUB_OUTPUT}"
	exit 0
fi

# Build diff command
diff_cmd=(git diff --quiet --exit-code "${base_sha}" "${head_sha}")
count_cmd=(git diff --name-only "${base_sha}" "${head_sha}")

if [[ -n "${PATHS}" ]]; then
	IFS=',' read -ra path_array <<<"${PATHS}"
	for p in "${path_array[@]}"; do
		diff_cmd+=(-- "${p}")
		count_cmd+=(-- "${p}")
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
