#!/usr/bin/env bash
set -euo pipefail

if [[ ! -x ".venv/bin/python" ]]; then
  echo "missing .venv. run scripts/agency_setup.sh first."
  exit 1
fi

make verify
