#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "no modules requested"
  exit 0
fi

allowed_modules="requests httpx pydantic pytest ruff mypy GitPython openai psycopg"

for module in "$@"; do
  if [[ " ${allowed_modules} " != *" ${module} "* ]]; then
    echo "denied module: ${module}" >&2
    exit 2
  fi
done

python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install "$@"
