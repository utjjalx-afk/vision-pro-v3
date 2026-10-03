#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
"$repo_root/.venv/bin/python" -m ruff check .
"$repo_root/.venv/bin/python" -m ruff format --check .
"$repo_root/.venv/bin/python" -m pytest
