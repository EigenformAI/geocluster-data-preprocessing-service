#!/usr/bin/env bash
set -euo pipefail

print_env=0
if [ "${1:-}" = "--print-env" ]; then
  print_env=1
elif [ "${1:-}" != "" ]; then
  printf 'usage: %s [--print-env]\n' "$0" >&2
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"

repo="${PEACE_TOOL_POOL_REPO:-https://github.com/Lexikat-Pte-Ltd/peace-tool-pool.git}"
ref="${PEACE_TOOL_POOL_REF:-main}"
root="${PEACE_TOOL_POOL_ROOT:-$project_root/.cache/peace-tool-pool}"

case "$root" in
  /*) ;;
  *) root="$project_root/$root" ;;
esac

status() {
  printf 'peace-tool-pool: %s\n' "$*" >&2
}

run_git() {
  git -C "$root" "$@" >&2
}

if [ -e "$root" ] && [ ! -d "$root/.git" ]; then
  status "$root exists but is not a git checkout"
  exit 1
fi

if [ ! -d "$root/.git" ]; then
  status "cloning $repo ($ref) into $root"
  mkdir -p "$(dirname -- "$root")"
  git clone --branch "$ref" --single-branch "$repo" "$root" >&2
else
  configured_repo="$(git -C "$root" remote get-url origin)"
  if [ "$configured_repo" != "$repo" ]; then
    status "$root tracks $configured_repo, expected $repo"
    exit 1
  fi

  status "polling $repo ($ref)"
  run_git fetch origin "$ref" --prune
  run_git checkout "$ref"
  run_git pull --ff-only origin "$ref"
fi

mkdir -p "$project_root/output/.geomap-cache"
mkdir -p "$root/data" "$root/dependencies/models" "$root/dependencies/knowledge"

cache_root="${GEOMAP_CACHE_ROOT:-$project_root/output/.geomap-cache}"
data_root="${GEOMAP_DATA_ROOT:-$root/data}"
model_root="${GEOMAP_MODEL_ROOT:-$root/dependencies/models}"
knowledge_root="${GEOMAP_KNOWLEDGE_ROOT:-$root/dependencies/knowledge}"
allowed_roots="${GEOMAP_MCP_ALLOWED_ROOTS:-$project_root/output:$root/dependencies:$root/data}"

if [ "$print_env" -eq 1 ]; then
  printf 'export PEACE_TOOL_POOL_REPO=%q\n' "$repo"
  printf 'export PEACE_TOOL_POOL_REF=%q\n' "$ref"
  printf 'export PEACE_TOOL_POOL_ROOT=%q\n' "$root"
  printf 'export GEOMAP_CACHE_ROOT=%q\n' "$cache_root"
  printf 'export GEOMAP_DATA_ROOT=%q\n' "$data_root"
  printf 'export GEOMAP_MODEL_ROOT=%q\n' "$model_root"
  printf 'export GEOMAP_KNOWLEDGE_ROOT=%q\n' "$knowledge_root"
  printf 'export GEOMAP_MCP_ALLOWED_ROOTS=%q\n' "$allowed_roots"
fi
