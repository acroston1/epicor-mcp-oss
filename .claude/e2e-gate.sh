#!/usr/bin/env bash
# Deterministic offline OSS gate; no tenant data, credentials or network tests.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${EPICOR_MCP_TEST_LIVE:-0}" == "1" ]]; then
  echo 'Refusing live Epicor tests in the open-source release gate.' >&2
  exit 2
fi
if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="$PYTHON"
elif [[ -x "$PWD/.venv/bin/python" ]]; then
  PYTHON_BIN="$PWD/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
export HEADLESS=1
"$PYTHON_BIN" scripts/check_repo_docs.py
"$PYTHON_BIN" -m pytest tests/ -q
