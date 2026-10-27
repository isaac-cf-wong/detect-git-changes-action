#!/bin/bash

set -euo pipefail

ACTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${DETECT_CHANGES_PYTHON:-python3}"
PRESET="${INPUT_PRESET:-}"

# Split a comma- and/or newline-separated list into one trimmed entry per line,
# dropping empty entries.
split_list() {
	tr ',' '\n' <<<"${1}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | sed '/^$/d'
}

# Write the three outputs. Arguments are the changed files, one per argument;
# FIRST_RELEASE=true reports a change with no files to compare against.
write_outputs() {
	local delimiter
	delimiter="EOF_$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
	if [[ $# -gt 0 || "${FIRST_RELEASE:-false}" == "true" ]]; then
		echo "has_changes=true" >>"${GITHUB_OUTPUT}"
	else
		echo "has_changes=false" >>"${GITHUB_OUTPUT}"
	fi
	echo "changed_files_count=$#" >>"${GITHUB_OUTPUT}"
	{
		echo "changed_files<<${delimiter}"
		if [[ $# -gt 0 ]]; then
			printf '%s\n' "$@"
		fi
		echo "${delimiter}"
	} >>"${GITHUB_OUTPUT}"
}

case "${PRESET}" in
"" | none | python) ;;
*)
	echo "::error::Unknown preset '${PRESET}'. Supported presets: python" >&2
	exit 1
	;;
esac

# ────────────────────────────────────────────────
# Resolve effective BASE ref
# ────────────────────────────────────────────────
if [[ "${INPUT_USE_LATEST_TAG:-false}" == "true" ]]; then
	BASE_REF=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
	if [[ -z "$BASE_REF" ]]; then
		echo "No tags found → treating as first release (has changes = true)"
		FIRST_RELEASE=true write_outputs
		exit 0
	fi
	echo "::notice::Using latest tag as base: $BASE_REF"
else
	BASE_REF="${INPUT_BASE_REF?Error: base-ref is required when use-latest-tag is not true}"
fi

HEAD_REF="${INPUT_HEAD_REF:-HEAD}"

echo "Comparing ${BASE_REF} ... ${HEAD_REF}"

# Resolve to real SHAs (handles origin/ prefix if needed)
BASE_SHA=$(git rev-parse --verify "${BASE_REF}" 2>/dev/null || git rev-parse --verify "origin/${BASE_REF}" 2>/dev/null || echo "")
HEAD_SHA=$(git rev-parse --verify "${HEAD_REF}" 2>/dev/null || git rev-parse --verify "origin/${HEAD_REF}" 2>/dev/null || echo "")

if [[ -z "${BASE_SHA}" ]]; then
	echo "::error::Cannot resolve base-ref '${BASE_REF}'" >&2
	exit 1
fi

if [[ -z "${HEAD_SHA}" ]]; then
	echo "::error::Cannot resolve head-ref '${HEAD_REF}'" >&2
	exit 1
fi

if [[ "${BASE_SHA}" == "${HEAD_SHA}" ]]; then
	echo "Refs point to the same commit → no changes"
	write_outputs
	exit 0
fi

# ────────────────────────────────────────────────
# Build the pathspec: preset + paths, minus exclude-paths
# ────────────────────────────────────────────────
includes=()
excludes=()

while IFS= read -r p; do
	includes+=("${p}")
done < <(split_list "${INPUT_PATHS:-}")

# Git takes one magic signature per pathspec, so an entry that already carries
# long-form magic such as ":(glob)docs/**" gets "exclude" merged into it, and
# the short exclude forms ":!docs/" and ":^docs/" are taken as already excluding.
while IFS= read -r p; do
	if [[ "${p}" == ":!"* || "${p}" == ":^"* ]]; then
		excludes+=(":(exclude)${p:2}")
	elif [[ "${p}" == ":("* ]]; then
		excludes+=(":(exclude,${p#:(}")
	else
		excludes+=(":(exclude)${p}")
	fi
done < <(split_list "${INPUT_EXCLUDE_PATHS:-}")

HAS_TOMLLIB=false
# Whether a pyproject.toml/hatch.toml change outside the packaging tables may be
# dropped: not when every file counts, which must hold for those files too.
NARROW_CONFIG=false
if [[ "${PRESET}" == "python" ]]; then
	# Files that shape the build whatever the backend: pyproject.toml and
	# hatch.toml (narrowed below to their packaging tables), the setuptools/hatch/pdm build scripts and
	# manifests, and the license files backends pick up by default.
	preset_paths=(
		"pyproject.toml"
		"hatch.toml"
		"setup.py"
		"setup.cfg"
		"MANIFEST.in"
		"hatch_build.py"
		"pdm_build.py"
		":(glob)LICEN[CS]E*"
		":(glob)LICEN[CS]ES/**"
		":(glob)COPYING*"
		":(glob)NOTICE*"
		":(glob)AUTHORS*"
	)
	sources_found=false
	if "${PYTHON}" -c 'import tomllib' >/dev/null 2>&1; then
		HAS_TOMLLIB=true
		# The package sources, located from the build backend's configuration,
		# and the readme and license files pyproject.toml declares.
		problems_file=$(mktemp)
		rc=0
		located=$("${PYTHON}" "${ACTION_DIR}/python_packaging.py" package-paths "${BASE_SHA}" "${HEAD_SHA}" 2>"${problems_file}") || rc=$?
		while IFS= read -r p; do
			[[ -n "${p}" ]] && preset_paths+=("${p}")
		done <<<"${located}"
		while IFS= read -r problem; do
			[[ -n "${problem}" ]] && echo "::warning::python preset: ${problem}"
		done <"${problems_file}"
		rm -f "${problems_file}"
		[[ ${rc} -eq 0 ]] && sources_found=true
	else
		echo "::warning::Python 3.11+ (tomllib) not found; the python preset cannot read the build configuration, so every changed file counts, or, with 'paths' set, 'paths' plus any pyproject.toml, hatch.toml or README* change"
		preset_paths+=(":(glob)README*")
	fi

	if [[ "${sources_found}" == "true" ]]; then
		includes+=("${preset_paths[@]}")
		NARROW_CONFIG=${HAS_TOMLLIB}
	elif [[ ${#includes[@]} -gt 0 ]]; then
		echo "::warning::python preset: package sources not fully located; relying on 'paths' for them"
		includes+=("${preset_paths[@]}")
		NARROW_CONFIG=${HAS_TOMLLIB}
	else
		# Missing a release is worse than an extra one, so with nothing to go
		# on every file counts. Setting 'paths' to the package sources fixes it.
		echo "::warning::python preset: package sources not located and 'paths' is empty; every changed file counts. Set 'paths' to the package's source directories."
		includes=()
	fi
fi

diff_cmd=(git -c core.quotePath=false diff --no-renames --name-only "${BASE_SHA}" "${HEAD_SHA}")
if [[ ${#includes[@]} -gt 0 || ${#excludes[@]} -gt 0 ]]; then
	diff_cmd+=(-- ${includes[@]+"${includes[@]}"} ${excludes[@]+"${excludes[@]}"})
fi

echo "Running: ${diff_cmd[*]}"

# Captured rather than read through a process substitution, whose exit status
# is lost: a failed diff must stop the action, not read as "no changes".
if ! diff_output=$("${diff_cmd[@]}"); then
	echo "::error::git diff failed; check paths and exclude-paths" >&2
	exit 1
fi

changed=()
while IFS= read -r f; do
	if [[ "${NARROW_CONFIG}" == "true" && ("${f}" == "pyproject.toml" || "${f}" == "hatch.toml") ]]; then
		rc=0
		"${PYTHON}" "${ACTION_DIR}/python_packaging.py" material "${BASE_SHA}" "${HEAD_SHA}" "${f}" </dev/null || rc=$?
		if [[ ${rc} -eq 1 ]]; then
			echo "${f} changed outside its packaging tables → not material"
			continue
		elif [[ ${rc} -ne 0 ]]; then
			echo "::warning::Could not compare the ${f} packaging tables; treating the change as material"
		fi
	fi
	changed+=("${f}")
done < <(if [[ -n "${diff_output}" ]]; then sort -u <<<"${diff_output}"; fi)

if [[ ${#changed[@]} -eq 0 ]]; then
	echo "No changes detected"
	write_outputs
else
	echo "Changes found (${#changed[@]} file(s)):"
	printf '  %s\n' "${changed[@]}"
	write_outputs "${changed[@]}"
	if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
		{
			echo "### Changes since ${BASE_REF} (${#changed[@]} file(s))"
			# shellcheck disable=SC2016 # literal Markdown backticks
			printf -- '- `%s`\n' "${changed[@]}"
		} >>"${GITHUB_STEP_SUMMARY}"
	fi
fi
