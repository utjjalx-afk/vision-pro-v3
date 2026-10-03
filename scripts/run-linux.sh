#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$repo_root/.venv/bin/python" ]]; then
    printf '%s\n' 'Run setup-linux.sh first' >&2
    exit 1
fi
exec "$repo_root/.venv/bin/python" -m vision "$@"
