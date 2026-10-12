#!/usr/bin/env bash
# Run the Linal test suite. Works headless (GUI test auto-skips without a display).
set -euo pipefail
cd "$(dirname "$0")/../frontend"
exec python3 -m unittest discover -s tests -p 'test_*.py' "$@"
